"""Controls (F16): pause, resume, check in now, leave mode, reset. State lives in kv.

- pause: schedules, pollers and every action above Silent stop (the gate marks them held); intake
  continues so nothing is lost.
- resume: catch-up from the moment of the pause, then held actions are released: approved ones run,
  unapproved ones are shown again.
- leave mode: no chases, no caller drafts, the brief is paused (E53).
- reset: deletes task memory, lessons, schedules and checkpoints; keeps the audit log and the action
  history it points to. Needs the agent paused and a one-time confirmation token (ten minutes).

Every control is audited.
"""

import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import Clock
from agent.core.errors import ControlError
from agent.db import schema as s
from agent.db.repositories import ActionRepo, AuditRepo, KvRepo
from agent.runtime.scheduler import FireResult, Scheduler

log = logging.getLogger(__name__)

STATE_KEY = "controls"
RESET_KEY = "controls.reset_token"
RESET_TOKEN_TTL = timedelta(minutes=10)
LEAVE_SUPPRESSES = frozenset({"chase", "caller_draft"})
# Open requests a reset cancels before it forgets their tasks.
_OPEN_ACTIONS = ("proposed", "edited", "approved", "held", "ready")


class ControlState(BaseModel):
    paused: bool = False
    paused_at: datetime | None = None
    leave: bool = False
    leave_since: datetime | None = None
    changed_by: str | None = None


class ResumeResult(BaseModel):
    caught_up: list[FireResult]
    released: list[str]


class ResetResult(BaseModel):
    tasks: int
    actions_detached: int
    threads_forgotten: int


class HeldReleaser(Protocol):
    """ApprovalService.release_held."""

    async def release_held(self) -> list[str]: ...


class Controls:
    def __init__(
        self,
        kv: KvRepo,
        clock: Clock,
        audit: AuditRepo,
        scheduler: Scheduler | None = None,
        approvals: HeldReleaser | None = None,
        resetter: "Resetter | None" = None,
    ) -> None:
        self._kv = kv
        self._clock = clock
        self._audit = audit
        self._scheduler = scheduler
        self._approvals = approvals
        self._resetter = resetter

    # ------------------------------------------------------------ reads (gate, graph, scheduler)

    async def state(self) -> ControlState:
        raw = await self._kv.get(STATE_KEY)
        return ControlState.model_validate_json(raw) if raw else ControlState()

    async def paused(self) -> bool:
        return (await self.state()).paused

    async def on_leave(self) -> bool:
        return (await self.state()).leave

    async def suppressed_purposes(self) -> set[str]:
        return set(LEAVE_SUPPRESSES) if await self.on_leave() else set()

    # ------------------------------------------------------------ controls

    async def pause(self, actor: str = "user") -> ControlState:
        state = await self.state()
        if state.paused:
            return state
        state = state.model_copy(
            update={"paused": True, "paused_at": self._clock.now_utc(), "changed_by": actor}
        )
        await self._save(state, "pause", actor)
        return state

    async def resume(self, actor: str = "user") -> ResumeResult:
        state = await self.state()
        if not state.paused:
            return ResumeResult(caught_up=[], released=[])
        since = state.paused_at
        await self._save(
            state.model_copy(update={"paused": False, "paused_at": None, "changed_by": actor}),
            "resume",
            actor,
        )
        caught_up = await self._scheduler.catch_up(since=since) if self._scheduler else []
        released = await self._approvals.release_held() if self._approvals else []
        return ResumeResult(caught_up=caught_up, released=released)

    async def checkin_now(self, actor: str = "user") -> FireResult:
        """An explicit request: runs even when paused or on leave (a check-in only reads and reports)."""
        if self._scheduler is None:
            raise ControlError("the scheduler is not running")
        await self._record("checkin_now", actor)
        return await self._scheduler.fire("checkin", manual=True)

    async def leave_mode(self, on: bool, actor: str = "user") -> ControlState:
        state = await self.state()
        if state.leave == on:
            return state
        state = state.model_copy(
            update={"leave": on, "leave_since": self._clock.now_utc() if on else None, "changed_by": actor}
        )
        await self._save(state, "leave_on" if on else "leave_off", actor)
        return state

    async def request_reset(self, actor: str = "user") -> str:
        """Step one: a token the console shows with its 'are you sure' screen."""
        token = secrets.token_urlsafe(16)
        expires = self._clock.now_utc() + RESET_TOKEN_TTL
        await self._kv.set(RESET_KEY, f"{_digest(token)}|{expires.isoformat()}")
        await self._record("reset_requested", actor)
        return token

    async def reset(self, token: str, actor: str = "user") -> ResetResult:
        """Step two. The token works once, for ten minutes, and only while the agent is paused."""
        stored = await self._kv.get(RESET_KEY)
        if not stored:
            raise ControlError("ask for a reset first")
        digest, _, expires = stored.partition("|")
        if self._clock.now_utc() >= datetime.fromisoformat(expires):
            await self._kv.delete(RESET_KEY)
            raise ControlError("the reset confirmation expired; ask again")
        if not hmac.compare_digest(digest, _digest(token)):
            await self._record("reset_refused", actor)
            raise ControlError("the confirmation does not match")
        if not await self.paused():
            raise ControlError("pause the agent before a reset")
        if self._resetter is None:
            raise ControlError("reset is not available")
        await self._kv.delete(RESET_KEY)
        result = await self._resetter.reset()
        await self._record(f"reset ({result.tasks} tasks)", actor)
        if self._scheduler is not None and self._scheduler._aps is not None:
            self._scheduler.register()
        return result

    # ------------------------------------------------------------ internals

    async def _save(self, state: ControlState, what: str, actor: str) -> None:
        await self._kv.set(STATE_KEY, state.model_dump_json())
        await self._record(what, actor)

    async def _record(self, what: str, actor: str) -> None:
        await self._audit.append(
            tool=f"control:{what}", mode="silent", args_digest="-", result="done", actor=actor
        )


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Resetter:
    """Deletes task memory. The audit log and the action rows it references are kept (task id cleared)."""

    KEEP_KV_PREFIXES = ("delta.", "cursor.", "controls")

    def __init__(
        self,
        engine: AsyncEngine,
        actions: ActionRepo,
        clock: Clock,
        forget_thread: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._engine = engine
        self._actions = actions
        self._clock = clock
        self._forget = forget_thread

    async def reset(self) -> ResetResult:
        now = self._clock.now_utc()
        for action in await self._actions.with_statuses(_OPEN_ACTIONS):
            await self._actions.decide(
                action.id,
                (action.status,),
                "invalidated",
                decided_at=now,
                decided_by="system",
                reason="reset",
            )
        async with self._engine.connect() as conn:
            task_ids = [r[0] for r in (await conn.execute(select(s.tasks.c.id))).all()]
        forgotten = 0
        if self._forget is not None:
            for task_id in task_ids:
                await self._forget(task_id)
                forgotten += 1
        detached = await self._actions.detach_tasks()
        async with self._engine.begin() as conn:
            await conn.execute(delete(s.jobs))
            await conn.execute(delete(s.events))
            await conn.execute(delete(s.token_map))
            await conn.execute(delete(s.tasks))  # cascades to events, summaries, facts, links, clocks
            await conn.execute(delete(s.lessons))
            await conn.execute(delete(s.schedule_runs))
            rows = (await conn.execute(select(s.kv.c.key))).all()
            for (key,) in rows:
                if not key.startswith(self.KEEP_KV_PREFIXES):
                    await conn.execute(delete(s.kv).where(s.kv.c.key == key))
        log.warning("agent reset", extra={"tasks": len(task_ids)})
        return ResetResult(tasks=len(task_ids), actions_detached=detached, threads_forgotten=forgotten)
