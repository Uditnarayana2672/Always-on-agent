import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.core.config import CONFIG_DIR, Config, load_config
from agent.db.repositories import AuditRepo, EventRepo, JobRepo, KvRepo, MemoryRepo, ScheduleRunRepo, TaskRepo
from agent.runtime.controls import Controls
from agent.runtime.coordinator import Coordinator
from agent.runtime.scheduler import (
    HEARTBEAT_KEY,
    Scheduler,
    WorkCalendar,
    build_specs,
    checkin_times,
    occurrences,
)

# Wed 2026-03-25 18:00 IST, and Thu 10:30 IST.
WED_1800 = datetime(2026, 3, 25, 12, 30, tzinfo=UTC)
THU_1030 = datetime(2026, 3, 26, 5, 0, tzinfo=UTC)


def config(**work: Any) -> Config:
    data = load_config(CONFIG_DIR / "config.example.yaml").model_dump()
    data["work"].update(work)
    return Config.model_validate(data)


class Env:
    def __init__(self, engine: AsyncEngine, cfg: Config, at: datetime, db_url: str = "") -> None:
        self.clock = FrozenClock(cfg, at)
        self.jobs = JobRepo(engine, self.clock)
        self.events = EventRepo(engine, self.clock)
        self.coordinator = Coordinator(
            TaskRepo(engine, self.clock),
            self.events,
            self.jobs,
            MemoryRepo(engine, self.clock),
            self.clock,
            create_rules=[],
        )
        self.runs = ScheduleRunRepo(engine, self.clock)
        self.kv = KvRepo(engine, self.clock)
        self.audit = AuditRepo(engine, self.clock)
        self.scheduler = Scheduler(
            cfg, self.clock, self.coordinator.ingest, self.runs, self.kv, database_url=db_url or None
        )
        self.controls = Controls(self.kv, self.clock, self.audit, scheduler=self.scheduler)
        self.scheduler.attach_controls(self.controls)

    async def statuses(self, schedule: str) -> Counter[str]:
        return Counter(r["status"] for r in await self.runs.runs(schedule))


@pytest.fixture
def env(engine: AsyncEngine) -> Env:
    return Env(engine, config(), WED_1800)


# ---------------------------------------------------------------- specs and slots


def test_specs_from_config() -> None:
    cfg = config()
    assert checkin_times(cfg) == ["09:00", "11:00", "13:00", "15:00", "17:00"]
    specs = {s.name: s for s in build_specs(cfg)}
    assert specs["brief"].times == ("09:00",) and specs["brief"].leave_skips
    assert specs["chase_window"].zone == "Europe/Zurich"
    assert {n for n in specs if n.startswith("poll:")} == {
        "poll:mail",
        "poll:teams",
        "poll:powerbi",
        "poll:servicenow_report",
    }
    assert specs["poll:mail"].every_minutes == 5


def test_chase_window_follows_european_clock_change() -> None:
    cfg = config()
    spec = {s.name: s for s in build_specs(cfg)}["chase_window"]
    cal = WorkCalendar(cfg)
    before = occurrences(spec, datetime(2026, 3, 27, tzinfo=UTC), datetime(2026, 3, 28, tzinfo=UTC), cal)
    after = occurrences(spec, datetime(2026, 3, 30, tzinfo=UTC), datetime(2026, 3, 31, tzinfo=UTC), cal)
    ist = FrozenClock(cfg, WED_1800)
    assert [ist.to_local(s).strftime("%H:%M") for s in before] == ["13:30"]  # 09:00 CET
    assert [ist.to_local(s).strftime("%H:%M") for s in after] == ["12:30"]  # 09:00 CEST


def test_weekends_and_holidays_have_no_slots() -> None:
    cfg = config(holidays=["2026-03-26"])
    spec = {s.name: s for s in build_specs(cfg)}["brief"]
    slots = occurrences(
        spec, datetime(2026, 3, 25, tzinfo=UTC), datetime(2026, 3, 30, 12, tzinfo=UTC), WorkCalendar(cfg)
    )
    assert [s.date().isoformat() for s in slots] == ["2026-03-25", "2026-03-27", "2026-03-30"]


# ---------------------------------------------------------------- catch-up


async def test_sleep_from_1800_to_1030_runs_brief_once_and_one_checkin(env: Env) -> None:
    assert await env.scheduler.catch_up() == []  # first start: only the heartbeat
    env.clock.set(THU_1030)
    results = await env.scheduler.tick()  # the gap is noticed on the first heartbeat after wake

    fired = Counter(r.schedule for r in results if r.status == "caught_up")
    assert fired["brief"] == 1 and fired["checkin"] == 1 and fired["nightly_digest"] == 1
    assert fired["chase_window"] == 0  # 09:00 Zurich is 13:30 IST: not due yet
    assert all(fired[f"poll:{s}"] == 1 for s in ("mail", "teams", "powerbi", "servicenow_report"))
    assert await env.statuses("brief") == Counter({"caught_up": 1})
    assert await env.statuses("checkin") == Counter({"caught_up": 1})

    # Each fire is a schedule event and one task-less job.
    jobs = [await env.jobs.get(j) for r in results for j in r.job_ids]
    assert jobs and all(j is not None and j.kind == "schedule" and j.task_id is None for j in jobs)

    # Waking twice, or the live trigger firing the same slot, runs nothing again.
    again = await env.scheduler.catch_up(since=WED_1800)
    assert again and {r.status for r in again} == {"duplicate"}  # only pollers retry, and they dedupe
    brief_slot = next(r.due_at for r in results if r.schedule == "brief")
    assert (await env.scheduler.fire("brief", brief_slot)).status == "duplicate"
    assert await env.statuses("brief") == Counter({"caught_up": 1})


async def test_missed_checkins_coalesce(engine: AsyncEngine) -> None:
    env = Env(engine, config(), datetime(2026, 3, 25, 3, 0, tzinfo=UTC))  # Wed 08:30 IST
    await env.scheduler.catch_up()
    env.clock.set(datetime(2026, 3, 25, 11, 0, tzinfo=UTC))  # Wed 16:30: missed 09, 11, 13, 15
    await env.scheduler.tick()
    assert await env.statuses("checkin") == Counter({"caught_up": 1, "missed": 3})


async def test_asleep_through_a_whole_day_notes_yesterday(engine: AsyncEngine) -> None:
    env = Env(engine, config(), datetime(2026, 3, 24, 12, 30, tzinfo=UTC))  # Tue 18:00 IST
    await env.scheduler.catch_up()
    env.clock.set(datetime(2026, 3, 26, 2, 30, tzinfo=UTC))  # Thu 08:00 IST, before today's brief
    results = await env.scheduler.tick()
    assert [r.status for r in results if r.schedule == "brief"] == ["missed"]
    assert await env.statuses("brief") == Counter({"missed": 1})

    env.clock.set(datetime(2026, 3, 26, 3, 30, tzinfo=UTC))  # Thu 09:00: the live brief
    live = await env.scheduler.fire("brief")
    assert live.status == "ran"
    event = await env.events.get(
        int(json.loads((await env.jobs.get(live.job_ids[0])).payload_json)["event_id"])
    )  # type: ignore[union-attr]
    assert event is not None and json.loads(event["payload_json"])["missed_since"] == "2026-03-25"
    assert await env.kv.get("schedule.missed.brief") is None  # noted once


async def test_catch_up_is_bounded_and_survives_a_clock_going_back(env: Env) -> None:
    await env.kv.set(HEARTBEAT_KEY, (WED_1800 - timedelta(days=30)).isoformat())
    results = await env.scheduler.catch_up()
    assert Counter(r.schedule for r in results if r.status == "caught_up")["checkin"] == 1
    assert all(r["due_at"] >= WED_1800 - timedelta(days=7) for r in await env.runs.runs())

    await env.kv.set(HEARTBEAT_KEY, (WED_1800 + timedelta(hours=5)).isoformat())  # heartbeat in the future
    assert await env.scheduler.catch_up() == []
    assert await env.kv.get(HEARTBEAT_KEY) == WED_1800.isoformat()


async def test_no_gap_no_catch_up(env: Env) -> None:
    await env.scheduler.catch_up()
    env.clock.advance(timedelta(seconds=60))
    assert await env.scheduler.tick() == []


# ---------------------------------------------------------------- skips


async def test_holiday_skip(engine: AsyncEngine) -> None:
    env = Env(engine, config(holidays=["2026-03-26"]), datetime(2026, 3, 26, 3, 30, tzinfo=UTC))
    result = await env.scheduler.fire("brief")
    assert (result.status, result.detail) == ("skipped", "holiday")
    assert result.job_ids == [] and await env.jobs.queue_length() == 0
    assert (await env.scheduler.fire("poll:mail")).status == "ran"  # pollers ignore holidays


async def test_catch_up_across_a_holiday_does_not_run_its_brief(engine: AsyncEngine) -> None:
    env = Env(engine, config(holidays=["2026-03-26"]), WED_1800)
    await env.scheduler.catch_up()
    env.clock.set(datetime(2026, 3, 26, 6, 0, tzinfo=UTC))  # Thu (holiday) 11:30
    results = await env.scheduler.tick()
    assert not [r for r in results if r.schedule in ("brief", "checkin") and r.status == "caught_up"]


async def test_weekend_fire_is_skipped(engine: AsyncEngine) -> None:
    env = Env(engine, config(), datetime(2026, 3, 28, 3, 30, tzinfo=UTC))  # Saturday 09:00
    assert (await env.scheduler.fire("brief")).detail == "not a work day"


async def test_pause_skips_everything_and_resume_catches_up(env: Env) -> None:
    await env.scheduler.catch_up()
    await env.controls.pause()
    env.clock.set(datetime(2026, 3, 26, 3, 30, tzinfo=UTC))  # Thu 09:00
    for name in ("brief", "checkin", "poll:mail"):
        result = await env.scheduler.fire(name)
        assert (result.status, result.detail) == ("skipped", "paused")
    assert await env.jobs.queue_length() == 0

    env.clock.set(datetime(2026, 3, 26, 4, 0, tzinfo=UTC))  # 09:30
    resumed = await env.controls.resume()
    ran = Counter(r.schedule for r in resumed.caught_up if r.status == "caught_up")
    assert ran["brief"] == 1 and ran["checkin"] == 1 and ran["poll:mail"] == 1
    assert await env.statuses("brief") == Counter({"caught_up": 1})  # the paused slot was taken over


async def test_leave_mode_pauses_brief_and_chases_not_checkins(env: Env) -> None:
    await env.controls.leave_mode(True)
    assert await env.controls.suppressed_purposes() == {"chase", "caller_draft"}
    env.clock.set(datetime(2026, 3, 26, 3, 30, tzinfo=UTC))
    assert (await env.scheduler.fire("brief")).detail == "leave mode"
    env.clock.set(datetime(2026, 3, 26, 8, 0, tzinfo=UTC))
    assert (await env.scheduler.fire("chase_window")).detail == "leave mode"
    assert (await env.scheduler.fire("checkin")).status == "ran"
    await env.controls.leave_mode(False)
    assert await env.controls.suppressed_purposes() == set()
    env.clock.set(datetime(2026, 3, 27, 3, 30, tzinfo=UTC))
    assert (await env.scheduler.fire("brief")).status == "ran"


async def test_checkin_now_runs_even_when_paused(env: Env) -> None:
    await env.controls.pause()
    result = await env.controls.checkin_now()
    assert result.status == "ran" and len(result.job_ids) == 1
    again = await env.controls.checkin_now()
    assert again.status in ("ran", "duplicate")


# ---------------------------------------------------------------- APScheduler


async def test_jobs_are_persisted_and_stale_ones_removed(engine: AsyncEngine, db_url: str) -> None:
    env = Env(engine, config(), WED_1800, db_url=db_url)
    await env.scheduler.start()
    try:
        ids = env.scheduler.job_ids()
        assert "schedule:brief:09:00" in ids and "schedule:chase_window:09:00" in ids
        assert "schedule:poll:mail" in ids and "scheduler:heartbeat" in ids
        assert len([i for i in ids if i.startswith("schedule:checkin:")]) == 5
        aps = env.scheduler._aps
        assert aps is not None
        assert str(aps.get_job("schedule:chase_window:09:00").trigger.timezone) == "Europe/Zurich"
        aps.add_job(
            "agent.runtime.scheduler:fire_job",
            "interval",
            minutes=1,
            args=["poll:fax"],
            id="schedule:poll:fax",
        )
    finally:
        await env.scheduler.stop()

    # Restart: the store still has the jobs; the one config no longer has is removed.
    again = Env(engine, config(), WED_1800, db_url=db_url)
    await again.scheduler.start()
    try:
        assert "schedule:poll:fax" not in again.scheduler.job_ids()
        assert "schedule:brief:09:00" in again.scheduler.job_ids()
    finally:
        await again.scheduler.stop()
