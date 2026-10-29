"""Supervisor and health monitor (prompt 1.17): one instance, wake catch-up, incidents said once."""

import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.adapters.base import HealthStatus
from agent.channels.notifier import Notification
from agent.core.clock import FrozenClock
from agent.core.config import CONFIG_DIR, load_config
from agent.runtime.fake import build_fake_runtime
from agent.runtime.health import INCIDENTS_KEY, HealthMonitor
from agent.runtime.pollers import LAST_OK
from agent.runtime.supervisor import (
    HEARTBEAT_FILE,
    LOCK_FILE,
    Heartbeat,
    InstanceLock,
    RunParts,
    Supervisor,
    data_dir_for,
    lock_is_held,
    read_heartbeat,
    status_lines,
    write_heartbeat,
)

CONFIG = load_config(CONFIG_DIR / "config.example.yaml")
T0 = datetime(2026, 3, 25, 4, 0, tzinfo=UTC)


# ---------------------------------------------------------------- the lock


def test_lock_prevents_a_second_instance(tmp_path: Path) -> None:
    first, second = InstanceLock(tmp_path / LOCK_FILE), InstanceLock(tmp_path / LOCK_FILE)
    assert first.acquire()
    assert not second.acquire()
    assert lock_is_held(tmp_path / LOCK_FILE)
    first.release()
    assert second.acquire()
    second.release()
    assert not lock_is_held(tmp_path / LOCK_FILE)


async def test_second_supervisor_exits_quietly_without_building(tmp_path: Path) -> None:
    holder = InstanceLock(tmp_path / LOCK_FILE)
    assert holder.acquire()
    built: list[bool] = []
    said: list[str] = []

    async def build() -> RunParts:
        built.append(True)
        raise AssertionError("must not build")

    try:
        code = await Supervisor(tmp_path, build, FrozenClock(CONFIG, T0), echo=said.append).run()
    finally:
        holder.release()
    assert code == 0 and built == [] and "already running" in said[0]


def test_a_crashed_instance_leaves_no_stale_lock(tmp_path: Path) -> None:
    """The OS drops the lock with the process; nothing to clean up by hand after a crash."""
    script = (
        "import sys, time; sys.path.insert(0, sys.argv[2]);"
        "from agent.runtime.supervisor import InstanceLock; from pathlib import Path;"
        "lock = InstanceLock(Path(sys.argv[1])); assert lock.acquire();"
        "print('locked', flush=True); time.sleep(60)"
    )
    src = str(Path(__file__).resolve().parents[2] / "src")
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path / LOCK_FILE), src], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "locked"
        assert lock_is_held(tmp_path / LOCK_FILE)
    finally:
        proc.kill()
        proc.wait()
    deadline = time.monotonic() + 5
    while lock_is_held(tmp_path / LOCK_FILE) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert InstanceLock(tmp_path / LOCK_FILE).acquire()


# ---------------------------------------------------------------- heartbeat and wake


async def test_heartbeat_gap_triggers_catch_up(tmp_path: Path) -> None:
    runtime, _ = await build_fake_runtime(CONFIG, tmp_path / "data")
    try:
        await runtime.load_recipes()
        clock = FrozenClock(CONFIG, T0)
        parts = RunParts(runtime=runtime)

        async def build() -> RunParts:
            return parts

        sup = Supervisor(tmp_path, build, clock, mode="fake")
        sup.parts = parts
        sup.state = "running"
        calls: list[tuple[datetime, datetime | None]] = []
        real_catch_up = runtime.scheduler.catch_up

        async def spy(now: datetime | None = None, since: datetime | None = None) -> Any:
            calls.append((now or T0, since))
            return await real_catch_up(now=now, since=since)

        runtime.scheduler.catch_up = spy  # type: ignore[method-assign]
        health_runs: list[int] = []
        real_health = runtime.health_monitor.run_once

        async def health_spy() -> Any:
            health_runs.append(1)
            return await real_health()

        runtime.health_monitor.run_once = health_spy  # type: ignore[method-assign]

        assert await sup.beat() is False  # first beat: nothing to compare with
        clock.advance(timedelta(seconds=30))
        assert await sup.beat() is False and calls == []  # a normal beat
        before = len(health_runs)
        clock.advance(timedelta(hours=3))  # the laptop slept
        assert await sup.beat() is True
        assert len(calls) == 1
        now, since = calls[0]
        assert since is not None and now - since == timedelta(hours=3)  # in the runtime's own time
        assert len(health_runs) == before + 1  # health checked at once after waking
        caught = [r for r in await runtime.schedule_runs.runs() if r["status"] in ("caught_up", "missed")]
        assert caught, "something missed during the sleep was caught up or recorded"
        beat = read_heartbeat(tmp_path)
        assert beat is not None and beat.woke_at == T0 + timedelta(hours=3, seconds=30)
        assert beat.beat_at == clock.now_utc()  # wall clock, not the fixture clock
    finally:
        await runtime.stop()


async def test_run_starts_in_order_and_drains(tmp_path: Path) -> None:
    runtime, _ = await build_fake_runtime(CONFIG, tmp_path / "data")
    events: list[str] = []

    async def extra_start() -> None:
        events.append("extra started")
        await sup._stop.wait()

    async def extra_stop() -> None:
        events.append("extra stopped")

    async def build() -> RunParts:
        events.append("built")
        return RunParts(runtime=runtime, extras=[("extra", extra_start, extra_stop)], console="http://x")

    sup = Supervisor(
        tmp_path, build, beat_seconds=0.05, mode="fake", start_scheduler=False, echo=events.append
    )
    original = runtime.health_monitor.run_once

    async def health() -> Any:
        events.append("health")
        return await original()

    runtime.health_monitor.run_once = health  # type: ignore[method-assign]

    import asyncio

    task = asyncio.create_task(sup.run())
    for _ in range(200):
        beat = read_heartbeat(tmp_path)
        if beat is not None and beat.state == "running":
            break
        await asyncio.sleep(0.02)
    assert lock_is_held(tmp_path / LOCK_FILE) is False or sup.lock.held  # held by this process
    sup.request_stop()
    assert await asyncio.wait_for(task, 10) == 0
    assert events.index("built") < events.index("health") < events.index("extra started")
    assert "extra stopped" in events
    beat = read_heartbeat(tmp_path)
    assert beat is not None and beat.state == "stopped"
    assert not sup.lock.held and not lock_is_held(tmp_path / LOCK_FILE)


# ---------------------------------------------------------------- agentd status


def test_status_running_stuck_and_stopped(tmp_path: Path) -> None:
    now = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
    code, lines = status_lines(tmp_path, now)
    assert code == 1 and any("not running" in line for line in lines)

    lock = InstanceLock(tmp_path / LOCK_FILE)
    assert lock.acquire()
    try:
        beat = Heartbeat(
            pid=123,
            mode="real",
            state="running",
            started_at=now - timedelta(hours=3),
            beat_at=now - timedelta(seconds=20),
            queue=2,
            last_checkin=now - timedelta(minutes=40),
            health=["Teams is not working: sign in again."],
            console="http://127.0.0.1:8765",
        )
        write_heartbeat(tmp_path, beat)
        code, lines = status_lines(tmp_path, now)
        text = "\n".join(lines)
        assert code == 0
        assert "running (pid 123, real), up 3 h 0 min" in text and "Queue: 2 jobs" in text
        assert "Last check-in: 40 min ago" in text and "Teams is not working" in text

        write_heartbeat(tmp_path, beat.model_copy(update={"beat_at": now - timedelta(minutes=10)}))
        code, lines = status_lines(tmp_path, now)
        assert code == 2 and "not responding" in "\n".join(lines)
    finally:
        lock.release()
    code, lines = status_lines(tmp_path, now)
    assert code == 1 and "last seen" in "\n".join(lines)


def test_cli_status_exit_code(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from agent.cli import app

    result = CliRunner().invoke(app, ["status", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1 and "not running" in result.output


def test_data_dir_for_sqlite_urls(tmp_path: Path) -> None:
    assert data_dir_for(f"sqlite+aiosqlite:///{(tmp_path / 'agent.db').as_posix()}") == tmp_path.resolve()
    assert data_dir_for("sqlite+aiosqlite:///agent.db") == Path.cwd().resolve()
    assert (tmp_path / HEARTBEAT_FILE).exists() is False


# ---------------------------------------------------------------- health monitor


class Adapter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "ok"
        self.detail = "fine"

    async def health(self) -> HealthStatus:
        return HealthStatus(connector=self.name, status=self.status, detail=self.detail)  # type: ignore[arg-type]


class Kv:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


class Health:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str | None]] = {}

    async def report(self, connector: str, status: str, error: str | None = None) -> None:
        self.rows[connector] = (status, error)


class Env:
    def __init__(self) -> None:
        self.clock = FrozenClock(CONFIG, T0)
        self.teams = Adapter("teams")
        self.outlook = Adapter("outlook")
        self.kv = Kv()
        self.health = Health()
        self.sent: list[Notification] = []
        self.stalled: list[Any] = []
        self.failed: list[Any] = []
        self.runs: list[dict[str, Any]] = []
        self.free = 50 * 1024**3
        self.ica: str | None = None

        async def notify(note: Notification) -> None:
            self.sent.append(note)

        async def stalled(now: datetime) -> list[Any]:
            return self.stalled

        async def failed_since(at: datetime) -> list[Any]:
            return self.failed

        async def runs() -> list[dict[str, Any]]:
            return self.runs

        async def ica() -> str | None:
            return self.ica

        self.monitor = HealthMonitor(
            CONFIG,
            self.clock,
            adapters={"teams": self.teams, "outlook": self.outlook},
            health=self.health,
            kv=self.kv,
            jobs=SimpleNamespace(failed_since=failed_since),
            ledger=SimpleNamespace(stalled=stalled),
            schedule_runs=SimpleNamespace(runs=runs),
            notify=notify,
            ica_check=ica,
            disk_usage=lambda _: SimpleNamespace(free=self.free, total=100 * 1024**3),
        )


async def test_an_incident_is_announced_once_and_again_only_after_it_cleared() -> None:
    env = Env()
    assert (await env.monitor.run_once()).incidents == [] and env.sent == []
    assert env.health.rows["teams"] == ("ok", None)

    env.teams.status, env.teams.detail = "down", "sign in again"
    report = await env.monitor.run_once()
    assert report.new == ["connector:teams"] and len(env.sent) == 1
    assert "Teams is not working: sign in again" in env.sent[0].text
    assert env.health.rows["teams"] == ("down", "sign in again")

    for _ in range(3):  # still down: no repeat
        env.clock.advance(timedelta(minutes=5))
        assert (await env.monitor.run_once()).new == []
    assert len(env.sent) == 1
    since = (await env.monitor.open_incidents())[0].since
    assert since == T0  # the incident keeps its start

    env.teams.status = "ok"
    assert (await env.monitor.run_once()).cleared == ["connector:teams"]
    env.teams.status = "down"
    await env.monitor.run_once()
    assert len(env.sent) == 2  # a new incident


async def test_new_incidents_of_one_run_share_one_dm_and_info_never_dms() -> None:
    env = Env()
    env.free = 200 * 1024**2  # 200 MB
    env.stalled = [SimpleNamespace(id="INC30114872", title="Cannot see documents")]
    env.ica = "connection refused"
    env.failed = [SimpleNamespace(kind="task")]  # the worker pool already DM'd it
    env.runs = [{"schedule": "brief", "status": "missed", "due_at": T0 - timedelta(hours=1)}]
    report = await env.monitor.run_once()
    keys = {i.key for i in report.incidents}
    assert {"disk", "stalled:INC30114872", "connector:ica", "jobs:failed", "missed:brief"} <= keys
    assert len(env.sent) == 1
    text = env.sent[0].text
    assert "Disk space is low" in text and "INC30114872" in text and "ICA" in text
    assert "job" not in text and "missed" not in text  # info only: brief, status, console
    summary = await env.monitor.summary()
    assert summary[0].startswith(("Disk", "INC", "The model")) and any("missed" in s for s in summary)


async def test_late_polls_and_a_broken_check() -> None:
    env = Env()
    env.kv.data[LAST_OK.format(connector="outlook")] = (T0 - timedelta(minutes=10)).isoformat()
    assert (await env.monitor.run_once()).incidents == []  # 10 min: fine (mail every 5 min, late at 30)
    env.clock.advance(timedelta(hours=2))
    report = await env.monitor.run_once()
    late = [i for i in report.incidents if i.key == "poll:outlook"]
    assert late and "has not been read for 2 h" in late[0].text

    async def broken(now: datetime) -> list[Any]:
        raise RuntimeError("database is locked")

    env.monitor._stalled = broken  # type: ignore[method-assign]
    report = await env.monitor.run_once()
    assert any(i.key == "check:stalled" for i in report.incidents)
    assert any(i.key == "poll:outlook" for i in report.incidents)  # the others still ran
    stored = json.loads(env.kv.data[INCIDENTS_KEY])
    assert {i["key"] for i in stored} == {i.key for i in report.incidents}


async def test_alerts_reach_the_top_of_the_brief(tmp_path: Path) -> None:
    runtime, _ = await build_fake_runtime(CONFIG, tmp_path)
    try:
        await runtime.kv.set(
            INCIDENTS_KEY,
            json.dumps(
                [
                    {"key": "disk", "severity": "alert", "text": "Disk space is low: 0.2 GB free."},
                    {"key": "jobs:failed", "severity": "info", "text": "1 job failed in the last 24 h."},
                    {"key": "connector:teams", "severity": "alert", "text": "Teams is not working."},
                ]
            ),
        )
        from agent.recipes.views import DayArgs

        data = await runtime.views.brief_data(DayArgs())
        texts = [c.connector for c in data.connectors]
        assert "Disk space is low: 0.2 GB free." in texts
        assert not any("job failed" in t for t in texts)  # info stays out of the brief
        assert "Teams is not working." not in texts  # connectors come from their own rows
    finally:
        await runtime.stop()


@pytest.mark.parametrize("platform_ok", [True])
def test_install_script_is_present_and_complete(platform_ok: bool) -> None:
    script = (Path(__file__).resolve().parents[3] / "scripts" / "install_task.ps1").read_text(
        encoding="utf-8"
    )
    for needed in (
        "Always-On Agent",
        "New-ScheduledTaskTrigger -AtLogOn",
        "SessionUnlock",
        "RepetitionInterval",
        "RestartCount",
        "MultipleInstances IgnoreNew",
        "agentd run",
        "-Uninstall",
        "Unregister-ScheduledTask",
    ):
        assert needed in script, needed


async def test_real_run_starts_with_nothing_set_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No app registration, no Outlook desktop, no ICA, no Slack: it starts and says what is missing."""
    from agent.runtime import real

    monkeypatch.delenv(CONFIG.ica.base_url_env, raising=False)
    monkeypatch.setattr("agent.model.ica_client.load_env_file", lambda *a, **k: None)
    monkeypatch.setattr(real.sys, "platform", "linux")
    database = CONFIG.database.model_copy(
        update={
            "url": f"sqlite+aiosqlite:///{(tmp_path / 'agent.db').as_posix()}",
            "checkpoints_path": str(tmp_path / "checkpoints.db"),
        }
    )
    config = CONFIG.model_copy(update={"database": database})
    parts = await real.build_real_parts(config)
    try:
        assert parts.runtime.adapters == {}
        assert [name for name, _, _ in parts.extras] == ["console", "http"]
        await parts.runtime.health_monitor.run_once()
        summary = "\n".join(await parts.runtime.health_monitor.summary())
        assert "Not set up: Microsoft 365" in summary and "Not set up: Slack" in summary
        assert "Not set up: the model (ICA)" in summary
    finally:
        for _, _, stop in parts.extras:
            await stop()
        await parts.runtime.stop()
