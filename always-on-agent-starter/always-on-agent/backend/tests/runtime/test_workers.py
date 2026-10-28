import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.core.config import ClockConfig, WorkersConfig
from agent.db.repositories import ClockRepo, Job, JobRepo, MemoryRepo, Task, TaskRepo
from agent.ledger.service import Ledger
from agent.runtime.workers import (
    HandlerRegistry,
    JobContext,
    JobOutcome,
    NotificationRequest,
    WorkerPool,
)

CONFIG = WorkersConfig(pool_size=4, max_steps_per_run=12, job_timeout_seconds=300, lock_ttl_minutes=10)


async def until(cond: Callable[[], Awaitable[bool]], timeout: float = 5.0) -> None:
    async def poll() -> None:
        while not await cond():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


class Harness:
    def __init__(self, engine: AsyncEngine, clock: FrozenClock) -> None:
        self.clock = clock
        self.tasks = TaskRepo(engine, clock)
        self.jobs = JobRepo(engine, clock, lock_ttl_minutes=10)
        self.memory = MemoryRepo(engine, clock)
        self.ledger = Ledger(
            self.tasks,
            self.memory,
            ClockRepo(engine, clock),
            clock,
            ClockConfig(start="opened", pause_on_hold=False, thresholds_hours=[12, 18, 22]),
        )
        self.handlers = HandlerRegistry()
        self.notes: list[NotificationRequest] = []
        self.pool: WorkerPool | None = None

    async def notify(self, req: NotificationRequest) -> None:
        self.notes.append(req)

    def make_pool(self, **kw: object) -> WorkerPool:
        self.pool = WorkerPool(
            self.jobs,
            self.handlers,
            self.clock,
            CONFIG,
            ledger=self.ledger,
            notify=self.notify,
            poll_interval=0.01,
            **kw,  # type: ignore[arg-type]
        )
        return self.pool

    async def task(self, task_id: str) -> None:
        await self.ledger.create(Task(id=task_id, project="doxis", kind="incident", title="t", state="new"))

    async def statuses(self) -> dict[int, str]:
        out = {}
        for task in await self.tasks.list():
            for job in await self.jobs.for_task(task.id):
                out[job.id] = job.status
        return out


@pytest.fixture
async def h(engine: AsyncEngine, clock: FrozenClock) -> AsyncIterator[Harness]:
    harness = Harness(engine, clock)
    yield harness
    if harness.pool is not None:
        await harness.pool.stop()


async def test_no_task_runs_twice_at_once(h: Harness) -> None:
    running: set[str] = set()
    overlaps: list[str] = []
    seen_workers: set[str] = set()
    max_parallel = 0

    async def handler(job: Job, ctx: JobContext) -> JobOutcome:
        nonlocal max_parallel
        assert job.task_id
        if job.task_id in running:
            overlaps.append(job.task_id)
        running.add(job.task_id)
        seen_workers.add(ctx.worker_id)
        max_parallel = max(max_parallel, len(running))
        await asyncio.sleep(0.02)
        running.discard(job.task_id)
        return "done"

    h.handlers.register("new_event", handler)
    for n in range(10):
        task_id = f"INC300000{n:02d}"
        await h.task(task_id)
        await h.jobs.enqueue("new_event", task_id=task_id, priority=70)
        await h.jobs.enqueue("new_event", task_id=task_id, priority=70)
    pool = h.make_pool()
    await pool.start()

    async def all_done() -> bool:
        statuses = await h.statuses()
        return len(statuses) == 20 and set(statuses.values()) == {"done"}

    await until(all_done)
    assert overlaps == []
    assert max_parallel > 1 and len(seen_workers) > 1  # the pool really ran in parallel
    assert all(t.last_heartbeat_at is not None for t in await h.tasks.list())


async def test_poison_job_fails_twice_then_surfaces(h: Harness) -> None:
    calls = 0

    async def poison(job: Job, ctx: JobContext) -> JobOutcome:
        nonlocal calls
        calls += 1
        raise ValueError("cannot parse the report")

    h.handlers.register("new_event", poison)
    await h.task("INC30114872")
    job_id = await h.jobs.enqueue("new_event", task_id="INC30114872", priority=70)
    pool = h.make_pool()
    await pool.start()

    async def requeued() -> bool:
        job = await h.jobs.get(job_id)
        return job is not None and job.status == "queued" and job.attempts == 1

    await until(requeued)
    job = await h.jobs.get(job_id)
    assert job is not None and job.available_at == h.clock.now_utc() + timedelta(minutes=2)
    assert "cannot parse" in (job.last_error or "")
    await asyncio.sleep(0.05)
    assert calls == 1  # backing off, not retried yet
    h.clock.advance(timedelta(minutes=2))

    async def failed() -> bool:
        j = await h.jobs.get(job_id)
        return j is not None and j.status == "failed"

    await until(failed)

    async def surfaced() -> bool:
        return len(h.notes) == 1

    await until(surfaced)
    assert calls == 2
    task = await h.tasks.get("INC30114872")
    assert task is not None and task.state == "needs_you"
    assert len(h.notes) == 1
    note = h.notes[0]
    assert note.task_id == "INC30114872" and note.job_id == job_id
    assert "2 tries" in note.text and "cannot parse the report" in note.text


async def test_failure_without_task_still_notifies(h: Harness) -> None:
    async def boom(job: Job, ctx: JobContext) -> JobOutcome:
        raise RuntimeError("x" * 1000)

    h.handlers.register("schedule", boom)
    job_id = await h.jobs.enqueue("schedule", priority=40)
    pool = h.make_pool()
    await pool.start()
    await until(lambda: _status(h, job_id, "queued", attempts=1))
    h.clock.advance(timedelta(minutes=2))
    await until(lambda: _status(h, job_id, "failed"))

    async def surfaced() -> bool:
        return len(h.notes) == 1

    await until(surfaced)
    job = await h.jobs.get(job_id)
    assert job is not None and len(job.last_error or "") == 300  # summary is capped
    assert h.notes[0].task_id is None and h.notes[0].text.startswith("schedule job stopped")


async def _status(h: Harness, job_id: int, status: str, attempts: int | None = None) -> bool:
    job = await h.jobs.get(job_id)
    return job is not None and job.status == status and (attempts is None or job.attempts == attempts)


async def test_shutdown_drains_running_jobs(h: Harness) -> None:
    started = asyncio.Event()
    checkpoints: list[bool] = []

    async def slow(job: Job, ctx: JobContext) -> JobOutcome:
        started.set()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if ctx.should_stop:
                checkpoints.append(True)
                ctx.set_step("stopping at checkpoint")
                return "parked"
        return "done"

    h.handlers.register("new_event", slow)
    await h.task("INC1")
    await h.task("INC2")
    first = await h.jobs.enqueue("new_event", task_id="INC1", priority=70)
    pool = h.make_pool()
    pool._size = 1  # one worker so the second job is certainly not claimed
    await pool.start()
    await asyncio.wait_for(started.wait(), 5)
    later = await h.jobs.enqueue("new_event", task_id="INC2", priority=70)
    await pool.stop()
    assert checkpoints == [True]
    assert await _status(h, first, "parked")
    assert await _status(h, later, "queued")  # never claimed after stop
    state = await pool.state()
    assert not state.running and state.queue_length == 1


async def test_drain_timeout_cancels_and_ttl_recovers(h: Harness) -> None:
    async def stubborn(job: Job, ctx: JobContext) -> JobOutcome:
        await asyncio.sleep(60)  # ignores checkpoints
        return "done"

    h.handlers.register("new_event", stubborn)
    await h.task("INC1")
    job_id = await h.jobs.enqueue("new_event", task_id="INC1", priority=70)
    pool = h.make_pool(drain_seconds=0.05)
    await pool.start()
    await until(lambda: _status(h, job_id, "running"))
    await pool.stop()
    assert await _status(h, job_id, "running")  # left for the lock TTL

    # Restart with a working handler: the job is recovered only once the lock expires.
    h.handlers = HandlerRegistry()

    async def ok(job: Job, ctx: JobContext) -> JobOutcome:
        return "done"

    h.handlers.register("new_event", ok)
    pool2 = h.make_pool()
    await pool2.start()
    await asyncio.sleep(0.05)
    assert await _status(h, job_id, "running")
    h.clock.advance(timedelta(minutes=10, seconds=1))
    await until(lambda: _status(h, job_id, "done"))


async def test_stuck_lock_is_recovered_after_ttl(h: Harness) -> None:
    async def ok(job: Job, ctx: JobContext) -> JobOutcome:
        return "done"

    h.handlers.register("new_event", ok)
    await h.task("INC1")
    job_id = await h.jobs.enqueue("new_event", task_id="INC1", priority=70)
    other = await h.jobs.enqueue("new_event", task_id="INC1", priority=70)
    dead = await h.jobs.claim("crashed-worker")
    assert dead is not None and dead.id == job_id
    pool = h.make_pool()
    await pool.start()
    await asyncio.sleep(0.1)
    # The task is locked by the dead worker: neither job runs yet.
    assert await _status(h, job_id, "running") and await _status(h, other, "queued")
    h.clock.advance(timedelta(minutes=9))
    await asyncio.sleep(0.05)
    assert await _status(h, other, "queued")
    h.clock.advance(timedelta(minutes=1, seconds=1))
    await until(lambda: _status(h, job_id, "done"))
    await until(lambda: _status(h, other, "done"))
    # The crashed worker coming back late cannot overwrite the result.
    assert not await h.jobs.complete(job_id, "crashed-worker")
    assert not await h.jobs.fail(job_id, "late", worker_id="crashed-worker")


async def test_timeout_counts_as_error(h: Harness) -> None:
    async def hang(job: Job, ctx: JobContext) -> JobOutcome:
        ctx.set_step("calling model")
        await asyncio.sleep(10)
        return "done"

    h.handlers.register("new_event", hang)
    await h.task("INC1")
    job_id = await h.jobs.enqueue("new_event", task_id="INC1", priority=70)
    pool = h.make_pool(job_timeout=0.05)
    await pool.start()
    await until(lambda: _status(h, job_id, "queued", attempts=1))
    job = await h.jobs.get(job_id)
    assert (
        job is not None
        and "timed out" in (job.last_error or "")
        and "calling model" in (job.last_error or "")
    )


async def test_parked_unknown_kind_and_bad_outcome(h: Harness) -> None:
    async def park(job: Job, ctx: JobContext) -> JobOutcome:
        return "parked"

    async def weird(job: Job, ctx: JobContext) -> JobOutcome:
        return "maybe"  # type: ignore[return-value]

    h.handlers.register("resume", park)
    h.handlers.register("schedule", weird)
    with pytest.raises(ValueError, match="already registered"):
        h.handlers.register("resume", park)
    parked = await h.jobs.enqueue("resume", priority=90)
    unknown = await h.jobs.enqueue("mystery", priority=10)
    bad = await h.jobs.enqueue("schedule", priority=40)
    pool = h.make_pool()
    await pool.start()
    await until(lambda: _status(h, parked, "parked"))
    await until(lambda: _status(h, unknown, "queued", attempts=1))
    await until(lambda: _status(h, bad, "queued", attempts=1))
    assert "no handler" in ((await h.jobs.get(unknown)).last_error or "")  # type: ignore[union-attr]
    assert "returned 'maybe'" in ((await h.jobs.get(bad)).last_error or "")  # type: ignore[union-attr]


async def test_pool_state_shows_current_work(h: Harness) -> None:
    release = asyncio.Event()

    async def wait(job: Job, ctx: JobContext) -> JobOutcome:
        ctx.set_step("drafting reply")
        await release.wait()
        return "done"

    h.handlers.register("new_event", wait)
    await h.task("INC1")
    await h.jobs.enqueue("new_event", task_id="INC1", priority=70)
    await h.jobs.enqueue("new_event", task_id="INC1", priority=70)  # waits for the task lock
    pool = h.make_pool()
    idle = await pool.state()
    assert not idle.running and idle.workers == []
    await pool.start()
    with pytest.raises(RuntimeError):
        await pool.start()

    async def busy() -> bool:
        # Wait for the step too: the worker shows the task a moment before the handler names its step.
        state = await pool.state()
        return any(w.task_id and w.step == "drafting reply" for w in state.workers)

    await until(busy)
    h.clock.advance(timedelta(seconds=42))
    state = await pool.state()
    active = [w for w in state.workers if w.task_id]
    assert len(active) == 1 and len(state.workers) == 4
    assert active[0].task_id == "INC1" and active[0].step == "drafting reply"
    assert active[0].elapsed_seconds == 42 and active[0].kind == "new_event"
    assert state.running and state.queue_length == 1
    release.set()

    async def drained() -> bool:
        return (await pool.state()).queue_length == 0 and not any(
            w.task_id for w in (await pool.state()).workers
        )

    await until(drained)


async def test_claim_errors_do_not_kill_the_worker(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(job: Job, ctx: JobContext) -> JobOutcome:
        return "done"

    h.handlers.register("new_event", ok)
    real = h.jobs.claim
    fails = {"n": 0}

    async def flaky(worker_id: str) -> Job | None:
        if fails["n"] < 3:
            fails["n"] += 1
            raise OSError("database is locked")
        return await real(worker_id)

    monkeypatch.setattr(h.jobs, "claim", flaky)
    await h.task("INC1")
    job_id = await h.jobs.enqueue("new_event", task_id="INC1", priority=70)
    pool = h.make_pool()
    await pool.start()
    await until(lambda: _status(h, job_id, "done"))
