"""Live updates for the console (F15): server-sent events on GET /api/stream.

Events: task, approval, activity, worker, control. Each carries a small JSON body; the console uses it
to refetch the affected queries, so no task or message text goes over the stream.

Two sources feed the hub:
- the API itself publishes right after a change it made (approve, edit, undo, pause ...);
- a watcher compares a cheap signature of the database every second (newest audit row, open
  approvals, newest task change, the worker pool, the controls), so changes made by workers, Slack or
  the scheduler show up too.

A slow browser cannot hold the agent up: each subscriber has a bounded queue; when it is full the
oldest event is dropped (the console refetches anyway). A heartbeat comment every 15 s keeps proxies
and the browser from closing the connection.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from agent.db import schema as s
from agent.runtime.assembly import Runtime

log = logging.getLogger(__name__)

KINDS = ("task", "approval", "activity", "worker", "control")
QUEUE_SIZE = 100
HEARTBEAT_SECONDS = 15.0
WATCH_SECONDS = 1.0


@dataclass
class Message:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    id: int = 0

    def encode(self) -> str:
        return f"id: {self.id}\nevent: {self.kind}\ndata: {json.dumps(self.data, default=str)}\n\n"


class Hub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Message]] = set()
        self._next = 0

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[Message]:
        queue: asyncio.Queue[Message] = asyncio.Queue(QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Message]) -> None:
        self._subscribers.discard(queue)

    async def publish(self, kind: str, data: dict[str, Any] | None = None) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown stream event {kind!r}")
        self._next += 1
        message = Message(kind, data or {}, self._next)
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(message)

    async def events(
        self,
        is_disconnected: Callable[[], Awaitable[bool]],
        heartbeat: float = HEARTBEAT_SECONDS,
    ) -> AsyncIterator[str]:
        """The text/event-stream body for one browser."""
        queue = self.subscribe()
        try:
            yield "retry: 3000\n\n"
            while True:
                if await is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield message.encode()
        finally:
            self.unsubscribe(queue)


class Watcher:
    """Turns database changes into stream events (one per kind per change, never per row)."""

    def __init__(self, rt: Runtime, hub: Hub, interval: float = WATCH_SECONDS) -> None:
        self._rt = rt
        self._hub = hub
        self._interval = interval
        self._last: dict[str, Any] = {}
        self._task: asyncio.Task[None] | None = None

    async def signature(self) -> dict[str, Any]:
        async with self._rt.engine.connect() as conn:
            audit = (await conn.execute(select(func.max(s.audit_log.c.id)))).scalar()
            tasks = (await conn.execute(select(func.max(s.tasks.c.updated_at), func.count()))).one()
            # Every open card, not a count and a max: approving a draft closes one card and opens the
            # send card, which can leave both unchanged.
            requests = s.action_requests.c
            approvals = (
                await conn.execute(
                    select(requests.id, requests.status, requests.content_hash)
                    .where(requests.status.in_(("proposed", "edited")))
                    .order_by(requests.id)
                )
            ).all()
        pool = await self._rt.pool.state()
        controls = await self._rt.controls.state()
        return {
            "activity": audit,
            "task": (str(tasks[0]), tasks[1]),
            "approval": tuple(tuple(row) for row in approvals),
            "worker": (
                pool.queue_length,
                tuple((w.worker_id, w.job_id, w.step) for w in pool.workers),
            ),
            "control": (controls.paused, controls.leave),
        }

    async def check(self) -> list[str]:
        """Publish what changed since the last check. Returns the kinds published."""
        now = await self.signature()
        changed = [k for k, v in now.items() if self._last and self._last.get(k) != v]
        self._last = now
        for kind in changed:
            await self._hub.publish(kind, {"source": "agent"})
        return changed

    async def run(self) -> None:
        while True:
            try:
                if self._hub.subscribers:
                    await self.check()
                else:
                    self._last = {}
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("stream watcher failed; retrying")
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="console-stream-watcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
