"""Task ledger: states, clocks, heartbeats and links (F08).

The ledger is the source of truth for the board, the brief and the KPI watchdog. Every state change
is compare-and-set and leaves a task event, so the history explains itself.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Literal

from agent.core.clock import Clock
from agent.core.config import ClockConfig
from agent.core.errors import IllegalTransition, LinkConflict
from agent.db.repositories import ClockRepo, MemoryRepo, Task, TaskRepo
from agent.ledger import clocks
from agent.ledger.states import INITIAL_STATES, check_transition, path

LinkKind = Literal["mail_conversation", "teams_chat", "slack_thread", "servicenow", "powerbi_dataset"]
LINK_KINDS: frozenset[str] = frozenset(
    {"mail_conversation", "teams_chat", "slack_thread", "servicenow", "powerbi_dataset"}
)

STALL_AFTER = timedelta(hours=24)

log = logging.getLogger(__name__)

# (task_id, from_state, to_state, actor): memory writes a precedent when a task closes.
StateListener = Callable[[str, str, str, str], Awaitable[None]]


class Ledger:
    def __init__(
        self, tasks: TaskRepo, memory: MemoryRepo, clock_repo: ClockRepo, clock: Clock, config: ClockConfig
    ) -> None:
        self._tasks = tasks
        self._memory = memory
        self._clock_repo = clock_repo
        self._clock = clock
        self._config = config
        self._listeners: list[StateListener] = []

    def add_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    # ------------------------------------------------------------ tasks and states

    async def create(self, task: Task) -> Task:
        if task.state not in INITIAL_STATES:
            raise IllegalTransition(f"a task starts as new or scheduled, not {task.state}")
        return await self._tasks.create(task)

    async def get(self, task_id: str) -> Task:
        task = await self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    async def transition(
        self, task_id: str, to_state: str, actor: str, reason: str = "", source_state: str | None = None
    ) -> Task:
        """One legal step. Raises IllegalTransition, or if someone else changed the state first."""
        task = await self.get(task_id)
        check_transition(task.state, to_state, actor, source_state)
        if not await self._tasks.transition(task_id, task.state, to_state):
            raise IllegalTransition(f"{task_id} changed state while moving {task.state} -> {to_state}")
        note = f"state {task.state} -> {to_state}" + (f": {reason}" if reason else "")
        await self._memory.add_task_event(
            task_id,
            actor=actor,
            channel="ledger",
            text_pseudonymised=note,
            kind="ledger",
            headline=note,
            importance=1,
        )
        for listener in self._listeners:
            try:
                await listener(task_id, task.state, to_state, actor)
            except Exception:
                log.exception("state listener failed", extra={"task": task_id, "to": to_state})
        return await self.get(task_id)

    async def move_to(self, task_id: str, to_state: str, actor: str, reason: str = "") -> Task:
        """Walk the shortest legal route to a state (never through done). No-op if already there."""
        task = await self.get(task_id)
        for step in path(task.state, to_state):
            task = await self.transition(task_id, step, actor, reason)
        return task

    async def surface_failure(self, task_id: str, error: str) -> Task:
        """A job failed twice: task goes to failed, then to needs_you so the user sees it (F04)."""
        await self.move_to(task_id, "failed", "worker", error)
        return await self.transition(task_id, "needs_you", "worker", "surfaced to user")

    async def reassigned_away(self, task_id: str, note: str) -> Task:
        """The ticket now belongs to someone else: wait, note it, stop chasing (E22)."""
        task = await self.get(task_id)
        if task.state != "done":
            task = await self.move_to(task_id, "waiting", "source_system", f"reassigned: {note}")
        await self._memory.set_fact(task_id, "assigned_to_me", "false", source_ref="servicenow")
        await self._memory.set_fact(task_id, "chasing", "stopped", source_ref="servicenow")
        await self._tasks.update(task_id, next_action="Reassigned away; no chasing")
        return await self.get(task_id)

    async def reopen(self, task_id: str, note: str = "ticket reopened", actor: str = "source_system") -> Task:
        """done -> in_progress (E23). Clears a 'chasing stopped' fact left by reassignment."""
        task = await self.transition(task_id, "in_progress", actor, note)
        await self._memory.set_fact(task_id, "chasing", "active", source_ref=actor)
        return task

    async def close_from_source(self, task_id: str, source_state: str) -> Task:
        """The ServiceNow report shows the ticket closed: the one case the agent may close a task."""
        task = await self.get(task_id)
        if task.state == "done":
            return task
        if task.state not in ("in_progress", "needs_you", "waiting"):
            await self.move_to(task_id, "in_progress", "source_system", "closed in source")
        return await self.transition(
            task_id, "done", "source_system", "closed in ServiceNow", source_state=source_state
        )

    async def set_next_action(self, task_id: str, text: str) -> None:
        """The one-line 'what happens next' on the task card."""
        await self._tasks.update(task_id, next_action=text)

    # ------------------------------------------------------------ clock

    async def set_clock_times(
        self,
        task_id: str,
        opened_at: datetime | None = None,
        assigned_at: datetime | None = None,
        email_received_at: datetime | None = None,
    ) -> Task:
        """Record whichever start times are known and recompute the clock start from config.

        Store all of them: the report says Opened 13:02 CET while the email arrives 18:00 IST.
        """
        task = await self.get(task_id)
        opened = opened_at or task.opened_at
        assigned = assigned_at or task.assigned_at
        received = email_received_at or task.email_received_at
        start, estimated = clocks.choose_start(self._config.start, opened, assigned, received)
        await self._tasks.update(
            task_id,
            opened_at=opened,
            assigned_at=assigned,
            email_received_at=received,
            clock_started_at=start,
            clock_estimated=estimated,
        )
        return await self.get(task_id)

    async def pause_clock(self, task_id: str, at: datetime | None = None, reason: str = "on_hold") -> None:
        """Ticket went On Hold. Recorded always; counted only when pause_on_hold is set."""
        if await self._clock_repo.open_pause(task_id, at or self._clock.now_utc(), reason):
            await self._tasks.update(task_id, clock_paused=True)

    async def resume_clock(self, task_id: str, at: datetime | None = None) -> None:
        if await self._clock_repo.close_pause(task_id, at or self._clock.now_utc()):
            await self._tasks.update(task_id, clock_paused=False)

    async def age(self, task_id: str, now: datetime | None = None) -> timedelta | None:
        task = await self.get(task_id)
        if task.clock_started_at is None:
            return None
        pauses = await self._clock_repo.pauses(task_id)
        return clocks.age(
            task.clock_started_at, now or self._clock.now_utc(), pauses, self._config.pause_on_hold
        )

    async def age_hours(self, task_id: str, now: datetime | None = None) -> float | None:
        value = await self.age(task_id, now)
        return None if value is None else value.total_seconds() / 3600

    async def due_thresholds(self, task_id: str, now: datetime | None = None) -> list[int]:
        """Thresholds (12, 18, 22 h) crossed and not fired before. Each fires once per task."""
        task = await self.get(task_id)
        if task.state == "done":
            return []
        hours = await self.age_hours(task_id, now)
        if hours is None:
            return []
        return [
            h
            for h in clocks.crossed(hours, self._config.thresholds_hours)
            if await self._clock_repo.fire_threshold(task_id, h)
        ]

    # ------------------------------------------------------------ heartbeats

    async def touch(self, task_id: str) -> None:
        """A worker worked on the task."""
        await self._tasks.heartbeat(task_id)

    async def stalled(self, now: datetime | None = None, after: timedelta = STALL_AFTER) -> list[Task]:
        """Open tasks no worker has touched for `after` (24 h); never-touched tasks count from creation."""
        cutoff = (now or self._clock.now_utc()) - after
        open_tasks = await self._tasks.list(
            states=["new", "scheduled", "in_progress", "needs_you", "waiting", "failed"]
        )
        stalled = []
        for task in open_tasks:
            last = task.last_heartbeat_at or task.created_at
            if last is not None and last <= cutoff:
                stalled.append(task)
        return sorted(stalled, key=lambda t: t.last_heartbeat_at or t.created_at or cutoff)

    # ------------------------------------------------------------ links

    async def add_link(self, task_id: str, kind: str, external_id: str) -> None:
        """Link a conversation, chat, thread, ticket or dataset. Idempotent; refuses to steal a link."""
        if kind not in LINK_KINDS:
            raise ValueError(f"unknown link kind {kind!r}")
        if not external_id:
            raise ValueError("external_id is empty")
        owner = await self._memory.task_for_link(kind, external_id)
        if owner is not None and owner != task_id:
            raise LinkConflict(f"{kind} {external_id} already belongs to {owner}")
        await self._memory.link(task_id, kind, external_id)
