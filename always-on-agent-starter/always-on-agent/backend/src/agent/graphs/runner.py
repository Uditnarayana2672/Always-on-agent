"""Worker handlers that drive the task graph (F04, F07).

new_event / schedule (with a task): start a run for the task. If the thread is parked on an approval
that is still open (the user's own message arrived), the event is only recorded and the graph keeps
waiting. If the approval was settled meanwhile (invalidated, rejected, expired), the old run is
finished first, then a new run starts.

resume: resume the parked run with Command(resume=...) when it waits on that action; a stale or
duplicate resume job does nothing.

Crash recovery: a run that died mid-node (thread has pending nodes but no interrupt) is continued from
its last checkpoint before anything else; the outbox makes the re-run of execute safe.
"""

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from agent.db.repositories import ActionRepo, Job, TaskRepo
from agent.graphs.task_graph import initial_state
from agent.runtime.workers import HandlerRegistry, JobContext, JobOutcome

log = logging.getLogger(__name__)

# The graph is still waiting on these: a new event (from the user) leaves the run parked.
STILL_PENDING = ("proposed", "edited", "approved", "held", "ready", "executing")

ScheduleHandler = Callable[[Job], Awaitable[None]]


@contextlib.asynccontextmanager
async def open_checkpointer(path: str | Path) -> AsyncIterator[AsyncSqliteSaver]:
    """LangGraph checkpoints in their own SQLite file (checkpoints.db)."""
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        yield saver


class TaskRunner:
    def __init__(
        self,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        actions: ActionRepo,
        tasks: TaskRepo,
        max_steps: int = 12,
        schedule_handlers: Mapping[str, ScheduleHandler] | None = None,
        schedule_listeners: Sequence[ScheduleHandler] = (),
    ) -> None:
        self._graph = graph
        self._actions = actions
        self._tasks = tasks
        # Each recipe step can pass through up to six nodes; leave room for the edges back.
        self._recursion_limit = max_steps * 8 + 25
        self._schedule_handlers = dict(schedule_handlers or {})
        # Called for every task-less schedule job after its own handler (recipes on that schedule).
        self._schedule_listeners = list(schedule_listeners)

    def add_schedule_listener(self, listener: ScheduleHandler) -> None:
        self._schedule_listeners.append(listener)

    def register(self, handlers: HandlerRegistry) -> None:
        handlers.register("new_event", self.run_event)
        handlers.register("schedule", self.run_event)
        handlers.register("resume", self.resume)

    def _config(self, task_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": task_id}, "recursion_limit": self._recursion_limit}

    async def waiting_on(self, task_id: str) -> dict[str, Any] | None:
        """The interrupt the task's thread is parked on ({kind, action_id}), if any."""
        snapshot = await self._graph.aget_state(self._config(task_id))
        for item in snapshot.interrupts:
            if isinstance(item.value, dict):
                return dict(item.value)
        return None

    async def forget(self, task_id: str) -> None:
        """Drop the task's checkpoints (reset)."""
        checkpointer = self._graph.checkpointer
        if isinstance(checkpointer, AsyncSqliteSaver):
            await checkpointer.adelete_thread(task_id)

    # ------------------------------------------------------------ handlers

    async def run_event(self, job: Job, ctx: JobContext) -> JobOutcome:
        if job.task_id is None:
            return await self._taskless(job, ctx)
        task_id = job.task_id
        if await self._tasks.get(task_id) is None:
            log.warning("job for a task that no longer exists", extra={"job": job.id, "task": task_id})
            return "done"
        config = self._config(task_id)
        ctx.set_step("recovering")
        await self._recover(task_id)

        pending = await self.waiting_on(task_id)
        if pending is not None:
            if not await self._settled(task_id):
                # The user's own message on a task with an open approval: recorded, graph keeps waiting.
                log.info("event recorded; task still waits for a decision", extra={"task": task_id})
                return "done"
            ctx.set_step("closing previous run")
            out = await self._graph.ainvoke(Command(resume={"settle": True}), config)
            if _interrupted(out):
                return "done"

        ctx.set_step("running")
        trigger = json.loads(job.payload_json or "{}")
        out = await self._graph.ainvoke(initial_state(task_id, job.kind, trigger), config)
        return "parked" if _interrupted(out) else "done"

    async def resume(self, job: Job, ctx: JobContext) -> JobOutcome:
        if job.task_id is None:
            return "done"
        task_id = job.task_id
        action_id = json.loads(job.payload_json or "{}").get("action_id")
        ctx.set_step("recovering")
        recovered = await self._recover(task_id)
        pending = await self.waiting_on(task_id)
        if pending is None or pending.get("action_id") != action_id:
            log.info("resume not needed", extra={"task": task_id, "action": action_id})
            return recovered if recovered == "parked" else "done"
        ctx.set_step("resuming")
        out = await self._graph.ainvoke(Command(resume={"action_id": action_id}), self._config(task_id))
        return "parked" if _interrupted(out) else "done"

    # ------------------------------------------------------------ internals

    async def _recover(self, task_id: str) -> JobOutcome | None:
        """Finish a run that crashed mid-node. Returns None when there was nothing to recover."""
        snapshot = await self._graph.aget_state(self._config(task_id))
        if not snapshot.next or snapshot.interrupts:
            return None
        log.warning("continuing a task run after a crash", extra={"task": task_id, "next": snapshot.next})
        out = await self._graph.ainvoke(None, self._config(task_id))
        return "parked" if _interrupted(out) else "done"

    async def _settled(self, task_id: str) -> bool:
        """True when the action the thread waits on no longer needs the user."""
        pending = await self.waiting_on(task_id)
        if pending is None:
            return True
        action = await self._actions.get(str(pending.get("action_id")))
        return action is None or action.status not in STILL_PENDING

    async def _taskless(self, job: Job, ctx: JobContext) -> JobOutcome:
        """Schedule jobs that belong to no task (brief, check-in, digest, pollers)."""
        payload = json.loads(job.payload_json or "{}")
        name = str(payload.get("schedule") or "")
        handler = self._schedule_handlers.get(name)
        ctx.set_step(f"schedule {name}")
        if handler is not None:
            await handler(job)
        for listener in self._schedule_listeners:
            await listener(job)
        if handler is None and not self._schedule_listeners:
            log.info("no handler for schedule", extra={"schedule": name})
        return "done"


def _interrupted(out: Any) -> bool:
    return isinstance(out, dict) and bool(out.get("__interrupt__"))
