"""Task workers and the job queue (F04).

A pool of asyncio workers. Each loops: claim the best job whose task has no running job, run its
handler with a timeout, then mark it done, parked (waiting for an approval), queued for a retry or
failed. The per-task lock lives in JobRepo.claim; a lock older than the TTL is taken over, so a
crash never blocks a task for more than lock_ttl_minutes. Jobs are finished only by the worker that
still holds them.

Shutdown drains: stop claiming, let running handlers reach their next checkpoint
(JobContext.should_stop), wait up to 30 s, then cancel what is left. A cancelled job stays
'running' and is recovered by the lock TTL after restart.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from agent.core.clock import Clock
from agent.core.config import WorkersConfig
from agent.core.errors import AgentError
from agent.db.repositories import Job, JobRepo
from agent.ledger.service import Ledger

log = logging.getLogger(__name__)

JobOutcome = Literal["done", "parked"]
MAX_ATTEMPTS = 2
BACKOFF = (timedelta(minutes=2), timedelta(minutes=10))
DRAIN_SECONDS = 30.0
ERROR_SUMMARY_CHARS = 300


class JobContext:
    """What a handler sees: the job, a step label for the console, and the stop signal."""

    def __init__(self, job: Job, worker_id: str, stopping: asyncio.Event) -> None:
        self.job = job
        self.worker_id = worker_id
        self.step = "starting"
        self._stopping = stopping

    def set_step(self, step: str) -> None:
        self.step = step

    @property
    def should_stop(self) -> bool:
        """Checked at checkpoints: the pool is draining, so finish or park now."""
        return self._stopping.is_set()


Handler = Callable[[Job, JobContext], Awaitable[JobOutcome]]


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler) -> None:
        if kind in self._handlers:
            raise ValueError(f"a handler for {kind!r} is already registered")
        self._handlers[kind] = handler

    def get(self, kind: str) -> Handler | None:
        return self._handlers.get(kind)


class NotificationRequest(BaseModel):
    """Picked up by the channels (F14): the user's DM, never anyone else."""

    kind: Literal["job_failed"]
    task_id: str | None
    job_id: int
    text: str


Notify = Callable[[NotificationRequest], Awaitable[None]]


class WorkerState(BaseModel):
    worker_id: str
    job_id: int | None = None
    task_id: str | None = None
    kind: str | None = None
    step: str | None = None
    started_at: datetime | None = None
    elapsed_seconds: float | None = None


class PoolState(BaseModel):
    running: bool
    draining: bool
    queue_length: int
    workers: list[WorkerState]


class WorkerPool:
    def __init__(
        self,
        jobs: JobRepo,
        handlers: HandlerRegistry,
        clock: Clock,
        config: WorkersConfig,
        ledger: Ledger | None = None,
        notify: Notify | None = None,
        poll_interval: float = 0.5,
        job_timeout: float | None = None,
        drain_seconds: float = DRAIN_SECONDS,
    ) -> None:
        self._jobs = jobs
        self._handlers = handlers
        self._clock = clock
        self._size = config.pool_size
        self._timeout = job_timeout if job_timeout is not None else float(config.job_timeout_seconds)
        self._ledger = ledger
        self._notify = notify
        self._poll = poll_interval
        self._drain = drain_seconds
        self._stopping = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._current: dict[str, tuple[Job, JobContext, datetime] | None] = {}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._tasks:
            raise RuntimeError("pool already started")
        self._stopping.clear()
        for n in range(self._size):
            worker_id = f"worker-{n + 1}"
            self._current[worker_id] = None
            self._tasks.append(asyncio.create_task(self._loop(worker_id), name=worker_id))

    async def stop(self) -> None:
        """Graceful drain: stop claiming, wait for running jobs up to drain_seconds, then cancel."""
        self._stopping.set()
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=self._drain)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if pending:
            log.warning("drain timed out; cancelled running jobs", extra={"cancelled": len(pending)})
        self._tasks = []

    async def state(self) -> PoolState:
        now = self._clock.now_utc()
        workers = []
        for worker_id, current in sorted(self._current.items()):
            if current is None:
                workers.append(WorkerState(worker_id=worker_id))
                continue
            job, ctx, started = current
            workers.append(
                WorkerState(
                    worker_id=worker_id,
                    job_id=job.id,
                    task_id=job.task_id,
                    kind=job.kind,
                    step=ctx.step,
                    started_at=started,
                    elapsed_seconds=(now - started).total_seconds(),
                )
            )
        return PoolState(
            running=bool(self._tasks) and not self._stopping.is_set(),
            draining=bool(self._tasks) and self._stopping.is_set(),
            queue_length=await self._jobs.queue_length(),
            workers=workers,
        )

    # ------------------------------------------------------------ worker loop

    async def _loop(self, worker_id: str) -> None:
        while not self._stopping.is_set():
            try:
                job = await self._jobs.claim(worker_id)
            except Exception:
                log.exception("claim failed", extra={"worker": worker_id})
                job = None
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._poll)
                continue
            await self._run(worker_id, job)

    async def _run(self, worker_id: str, job: Job) -> None:
        ctx = JobContext(job, worker_id, self._stopping)
        self._current[worker_id] = (job, ctx, self._clock.now_utc())
        try:
            if job.task_id and self._ledger is not None:
                await self._ledger.touch(job.task_id)
            outcome, error = await self._call(job, ctx)
            if error is None:
                if outcome == "parked":
                    await self._jobs.park(job.id, worker_id)
                else:
                    await self._jobs.complete(job.id, worker_id)
            else:
                await self._failed(worker_id, job, error)
        finally:
            self._current[worker_id] = None

    async def _call(self, job: Job, ctx: JobContext) -> tuple[JobOutcome | None, str | None]:
        handler = self._handlers.get(job.kind)
        if handler is None:
            return None, f"no handler for job kind {job.kind!r}"
        try:
            outcome = await asyncio.wait_for(handler(job, ctx), timeout=self._timeout)
        except TimeoutError:
            return None, f"timed out after {self._timeout:g} s at step {ctx.step!r}"
        except AgentError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            log.exception("job handler crashed", extra={"job": job.id, "kind": job.kind})
            return None, f"{type(exc).__name__}: {exc}"
        if outcome not in ("done", "parked"):
            return None, f"handler returned {outcome!r}"
        return outcome, None

    async def _failed(self, worker_id: str, job: Job, error: str) -> None:
        summary = error[:ERROR_SUMMARY_CHARS]
        if job.attempts < MAX_ATTEMPTS:
            delay = BACKOFF[min(job.attempts, len(BACKOFF)) - 1]
            await self._jobs.fail(
                job.id, summary, retry_at=self._clock.now_utc() + delay, worker_id=worker_id
            )
            log.info("job will retry", extra={"job": job.id, "attempt": job.attempts})
            return
        if not await self._jobs.fail(job.id, summary, worker_id=worker_id):
            return  # another worker took the job over after our lock expired
        log.warning("job failed for good", extra={"job": job.id, "kind": job.kind})
        if job.task_id and self._ledger is not None:
            try:
                await self._ledger.surface_failure(job.task_id, summary)
            except (AgentError, KeyError):
                log.exception("could not move task to needs_you", extra={"task": job.task_id})
        if self._notify is not None:
            label = job.task_id or f"{job.kind} job"
            await self._notify(
                NotificationRequest(
                    kind="job_failed",
                    task_id=job.task_id,
                    job_id=job.id,
                    text=f"{label} stopped after {job.attempts} tries: {summary}",
                )
            )
