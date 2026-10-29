"""Supervisor (F01): one agentd per laptop, started in order, stopped cleanly, awake after sleep.

    lock held by another agentd?  -> exit quietly (Task Scheduler starts us at log-on, on unlock and
                                     every 15 minutes; only the first one stays)
    build: config, database + migrations, secrets, adapters, model
    health check (connectors) before any work
    start: recipes, catch-up, scheduler, workers; then the console API and Slack
    every 30 s: heartbeat file; a gap between beats far longer than 30 s means the laptop slept or the
                process was frozen -> catch up on what was missed since the last beat and check health
                at once; otherwise health every 5 minutes
    stop (Ctrl+C, SIGTERM, Windows shutdown): stop intake, let running jobs reach a checkpoint, close
                the database, release the lock

The lock is an OS file lock (msvcrt on Windows, flock elsewhere): it is released by the OS when the
process dies, so a crash never leaves a stale lock behind. The heartbeat file (JSON, written
atomically) is what `agentd status` reads; it holds no message text.
"""

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Literal

from pydantic import BaseModel

from agent import __version__
from agent.core.clock import Clock
from agent.runtime.assembly import Runtime
from agent.runtime.health import INTERVAL as HEALTH_INTERVAL

log = logging.getLogger(__name__)

LOCK_FILE = "agentd.lock"
HEARTBEAT_FILE = "agentd.heartbeat.json"
BEAT_SECONDS = 30.0
WAKE_GAP = timedelta(seconds=90)  # three beats
STALE_AFTER = timedelta(seconds=120)  # `agentd status`: a running agentd that stopped beating

State = Literal["starting", "running", "draining", "stopped"]


# ---------------------------------------------------------------- the lock


class InstanceLock:
    """A non-blocking exclusive lock on <data dir>/agentd.lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: IO[bytes] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - held open for the life of the process
        try:
            _lock(handle)
        except OSError:
            handle.close()
            return False
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is None:
            return
        with contextlib.suppress(OSError):
            _unlock(self._file)
        self._file.close()
        self._file = None

    @property
    def held(self) -> bool:
        return self._file is not None


def lock_is_held(path: Path) -> bool:
    """True when another process holds the lock (used by `agentd status`)."""
    if not path.exists():
        return False
    probe = InstanceLock(path)
    if probe.acquire():
        probe.release()
        return False
    return True


if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- the heartbeat file


class Heartbeat(BaseModel):
    pid: int
    version: str = __version__
    mode: Literal["real", "fake"]
    state: State
    started_at: datetime
    beat_at: datetime
    paused: bool = False
    leave: bool = False
    queue: int = 0
    last_checkin: datetime | None = None
    health: list[str] = []
    console: str | None = None
    woke_at: datetime | None = None  # the last wake from sleep that triggered a catch-up


def read_heartbeat(data_dir: Path) -> Heartbeat | None:
    try:
        return Heartbeat.model_validate_json((data_dir / HEARTBEAT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_heartbeat(data_dir: Path, beat: Heartbeat) -> None:
    path = data_dir / HEARTBEAT_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(beat.model_dump_json(indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- the supervisor


@dataclass
class RunParts:
    """What a build returns: the runtime and the long-running extras started after it (console API,
    Slack). Each extra is (name, start, stop); start may run until stop is called."""

    runtime: Runtime
    extras: list[tuple[str, Callable[[], Awaitable[Any]], Callable[[], Awaitable[Any]]]] = field(
        default_factory=list
    )
    console: str | None = None


Build = Callable[[], Awaitable[RunParts]]


class Supervisor:
    def __init__(
        self,
        data_dir: Path,
        build: Build,
        clock: Clock | None = None,
        *,
        mode: Literal["real", "fake"] = "real",
        beat_seconds: float = BEAT_SECONDS,
        echo: Callable[[str], None] = print,
        start_scheduler: bool = True,
    ) -> None:
        self.data_dir = data_dir
        self.lock = InstanceLock(data_dir / LOCK_FILE)
        self._build = build
        self._clock = clock
        self._mode = mode
        self._beat_seconds = beat_seconds
        self._echo = echo
        self._start_scheduler = start_scheduler
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self.parts: RunParts | None = None
        self.started_at = self._now()
        self.last_beat: datetime | None = None
        self.last_health: datetime | None = None
        self.woke_at: datetime | None = None
        self.state: State = "starting"

    def _now(self) -> datetime:
        """Wall-clock time for the heartbeat and wake detection (`agentd status` compares with it)."""
        return self._clock.now_utc() if self._clock is not None else datetime.now(UTC)

    def _runtime_now(self) -> datetime:
        """The runtime's own time (fake runs live on the fixtures' shifted clock)."""
        return self.parts.runtime.clock.now_utc() if self.parts is not None else self._now()

    # ------------------------------------------------------------ lifecycle

    async def run(self) -> int:
        """Exit code 0: ran and stopped cleanly, or another agentd already runs."""
        if not self.lock.acquire():
            self._echo("agentd is already running; this one exits.")
            return 0
        try:
            await self._write()
            self.parts = await self._build()
            self.started_at = self._now()
            rt = self.parts.runtime
            await self._health(force=True)  # connectors before any work
            await rt.start(scheduler=self._start_scheduler, workers=True)
            for name, start, _ in self.parts.extras:
                self._tasks.append(asyncio.create_task(_guard(name, start), name=name))
            self.state = "running"
            self._install_signals()
            self._echo(f"agentd running ({self._mode}); Ctrl+C to stop")
            while not self._stop.is_set():
                await self.beat()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self._beat_seconds)
            return 0
        finally:
            await self.shutdown()

    def request_stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        """Graceful drain: stop intake and extras, let jobs reach a checkpoint, close, unlock."""
        if not self.lock.held:
            return
        self.state = "draining"
        with contextlib.suppress(Exception):
            await self._write()
        if self.parts is not None:
            for name, _, stop in reversed(self.parts.extras):
                try:
                    await asyncio.wait_for(stop(), timeout=10)
                except Exception:
                    log.exception("could not stop cleanly", extra={"part": name})
            if self._tasks:
                # Let each part finish its own shutdown (uvicorn closes connections) before cancelling.
                _, pending = await asyncio.wait(self._tasks, timeout=10)
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            try:
                await self.parts.runtime.stop()
            except Exception:
                log.exception("runtime stop failed")
        self.state = "stopped"
        with contextlib.suppress(Exception):
            await self._write()
        self.lock.release()

    # ------------------------------------------------------------ the beat

    async def beat(self) -> bool:
        """Write the heartbeat; after a gap (sleep), catch up and check health. True after a wake."""
        assert self.parts is not None
        rt = self.parts.runtime
        now = self._now()
        woke = self.last_beat is not None and now - self.last_beat > WAKE_GAP
        if woke:
            assert self.last_beat is not None
            gap = now - self.last_beat
            log.info("woke after a gap; catching up", extra={"gap_seconds": int(gap.total_seconds())})
            self.woke_at = now
            try:
                rt_now = self._runtime_now()
                await rt.scheduler.catch_up(now=rt_now, since=rt_now - gap)
            except Exception:
                log.exception("catch-up after wake failed")
        await self._health(force=woke)
        self.last_beat = now
        await self._write()
        return woke

    async def _health(self, force: bool = False) -> None:
        if self.parts is None:
            return
        now = self._now()
        if not force and self.last_health is not None and now - self.last_health < HEALTH_INTERVAL:
            return
        self.last_health = now
        try:
            await self.parts.runtime.health_monitor.run_once()
        except Exception:
            log.exception("health monitor failed")

    async def _write(self) -> None:
        now = self._now()
        beat = Heartbeat(
            pid=os.getpid(),
            mode=self._mode,
            state=self.state,
            started_at=self.started_at,
            beat_at=now,
            console=self.parts.console if self.parts else None,
            woke_at=self.woke_at,
        )
        if self.parts is not None and self.state == "running":
            rt = self.parts.runtime
            with contextlib.suppress(Exception):
                controls = await rt.controls.state()
                beat.paused, beat.leave = controls.paused, controls.leave
                beat.queue = await rt.jobs.queue_length()
                beat.health = await rt.health_monitor.summary()
                runs = [
                    r for r in await rt.schedule_runs.runs("checkin") if r["status"] in ("ran", "caught_up")
                ]
                if runs:
                    # Runs are stamped with the runtime's clock; the file speaks wall-clock time.
                    last = max(r["ran_at"] or r["due_at"] for r in runs)
                    beat.last_checkin = now - (self._runtime_now() - last)
        write_heartbeat(self.data_dir, beat)

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        names = ["SIGINT", "SIGTERM", "SIGBREAK"]  # SIGBREAK: Ctrl+Break and console close on Windows
        for name in names:
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows' event loop has no add_signal_handler: fall back to signal.signal.
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self.request_stop))


async def _guard(name: str, start: Callable[[], Awaitable[Any]]) -> None:
    try:
        await start()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("part stopped with an error", extra={"part": name})


# ---------------------------------------------------------------- agentd status


def status_lines(data_dir: Path, now: datetime) -> tuple[int, list[str]]:
    """(exit code, lines) for `agentd status`: 0 running, 1 not running, 2 not responding."""
    beat = read_heartbeat(data_dir)
    running = lock_is_held(data_dir / LOCK_FILE)
    lines = [f"Always-On Agent {__version__}", f"Data folder: {data_dir}"]
    if not running:
        when = f"; last seen {_ago(now - beat.beat_at)} ago" if beat else ""
        lines.append(f"State: not running{when}")
        lines.append("Start it with: uv run agentd run (or the scheduled task from scripts/install_task.ps1)")
        return 1, lines
    if beat is None:
        lines.append("State: starting (no heartbeat yet)")
        return 0, lines
    age = now - beat.beat_at
    code = 0
    if beat.state == "running" and age > STALE_AFTER:
        lines.append(f"State: not responding (pid {beat.pid}); last heartbeat {_ago(age)} ago")
        code = 2
    else:
        lines.append(f"State: {beat.state} (pid {beat.pid}, {beat.mode}), up {_ago(now - beat.started_at)}")
        lines.append(f"Last heartbeat: {_ago(age)} ago")
    lines.append(f"Paused: {'yes' if beat.paused else 'no'}" + (" · on leave" if beat.leave else ""))
    lines.append(
        "Last check-in: " + (f"{_ago(now - beat.last_checkin)} ago" if beat.last_checkin else "none yet")
    )
    lines.append(f"Queue: {beat.queue} job{'s' if beat.queue != 1 else ''}")
    if beat.woke_at:
        lines.append(f"Last wake from sleep: {_ago(now - beat.woke_at)} ago (caught up)")
    if beat.health:
        lines.append("Health:")
        lines += [f"  - {line}" for line in beat.health]
    else:
        lines.append("Health: all good")
    if beat.console:
        lines.append(f"Console: {beat.console} (agentd console prints a sign-in link)")
    return code, lines


def _ago(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    return f"{hours // 24} days"


def data_dir_for(database_url: str) -> Path:
    """The folder of the SQLite file in a sqlite URL (relative paths from the working directory)."""
    path = database_url.split("///", 1)[-1] if "///" in database_url else "agent.db"
    return Path(path).resolve().parent


__all__ = [
    "HEARTBEAT_FILE",
    "LOCK_FILE",
    "Heartbeat",
    "InstanceLock",
    "RunParts",
    "Supervisor",
    "data_dir_for",
    "lock_is_held",
    "read_heartbeat",
    "status_lines",
]
