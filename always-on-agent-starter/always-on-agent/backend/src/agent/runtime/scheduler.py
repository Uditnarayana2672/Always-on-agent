"""Scheduler, catch-up and wake detection (F02).

APScheduler (AsyncIOScheduler) keeps the jobs in agent.db (SQLAlchemy job store):

- cron, in the user's zone: the morning brief, check-ins every checkin_every_minutes through the work
  day, the nightly digest;
- cron, in the caller's zone: the chase window, so it follows Europe's clock changes (E05);
- intervals: one poller per source (config.schedule.poll_minutes) and the approval-expiry sweep.

Every fire becomes a schedule Event through the coordinator, and every slot (schedule, due time) gets
one schedule_runs row, so a live fire and a catch-up can never both run the same slot.

Skips (recorded, never run): agent paused, leave mode (brief and chase window), public holidays and
non-work days for work-day schedules (E06). Quiet hours and the push cap are not applied here: they
are enforced when a notification is sent (channels.notifier).

Catch-up runs on start, on resume and when the heartbeat shows a gap (the laptop slept, E01):
- a missed brief or chase window runs once, and only if its latest missed slot is today; otherwise
  the next one runs normally and says what was missed ("yesterday" noted);
- missed check-ins and digests coalesce into one run;
- each poller runs once;
- at most seven days back; a heartbeat in the future (clock changed after hibernation) resets it.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import BaseModel, ConfigDict

from agent.core.clock import Clock
from agent.core.config import Config
from agent.db.engine import sync_url
from agent.db.repositories import PAUSED_DETAIL, KvRepo, ScheduleRunRepo
from agent.runtime.coordinator import IngestResult

log = logging.getLogger(__name__)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_CATCH_UP = timedelta(days=7)
HEARTBEAT_SECONDS = 60
GAP = timedelta(minutes=3)  # three missed heartbeats: the laptop slept or the process was stopped
EXPIRY_SWEEP_MINUTES = 15
RETENTION_AT = "02:30"  # nightly, local time, every day
HEARTBEAT_KEY = "scheduler.heartbeat"
MISSED_KEY = "schedule.missed.{name}"
JOB_PREFIX = "schedule:"

CatchUp = Literal["latest_today", "coalesce", "poll"]
RunStatus = Literal["ran", "caught_up", "skipped", "duplicate", "missed"]


class ScheduleSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    kind: Literal["cron", "interval"]
    zone: str
    times: tuple[str, ...] = ()  # HH:MM in `zone`
    every_minutes: int | None = None
    work_days_only: bool = True
    catch_up: CatchUp = "coalesce"
    leave_skips: bool = False  # paused in leave mode


class FireResult(BaseModel):
    schedule: str
    due_at: datetime
    status: RunStatus
    detail: str | None = None
    job_ids: list[int] = []


class ScheduleControls(Protocol):
    async def paused(self) -> bool: ...
    async def on_leave(self) -> bool: ...


class Ingest(Protocol):
    async def __call__(self, raw: Any, source: Any) -> IngestResult: ...


def _hh_mm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def checkin_times(config: Config) -> list[str]:
    """Check-ins from the start of the work day, every checkin_every_minutes, before the end."""
    start, end = _hh_mm(config.work.start), _hh_mm(config.work.end)
    minute, last = start.hour * 60 + start.minute, end.hour * 60 + end.minute
    out = []
    while minute < last:
        out.append(f"{minute // 60:02d}:{minute % 60:02d}")
        minute += config.schedule.checkin_every_minutes
    return out


def build_specs(config: Config) -> list[ScheduleSpec]:
    tz, sched = config.timezone, config.schedule
    specs = [
        ScheduleSpec(
            name="brief",
            kind="cron",
            zone=tz,
            times=(sched.brief,),
            catch_up="latest_today",
            leave_skips=True,
        ),
        ScheduleSpec(
            name="checkin", kind="cron", zone=tz, times=tuple(checkin_times(config)), catch_up="coalesce"
        ),
        ScheduleSpec(
            name="nightly_digest", kind="cron", zone=tz, times=(sched.nightly_digest,), catch_up="coalesce"
        ),
        ScheduleSpec(
            name="chase_window",
            kind="cron",
            zone=config.caller_timezone,
            times=(sched.chase_window_local_caller,),
            catch_up="latest_today",
            leave_skips=True,
        ),
    ]
    for source, minutes in sched.poll_minutes.model_dump().items():
        specs.append(
            ScheduleSpec(
                name=f"poll:{source}",
                kind="interval",
                zone=tz,
                every_minutes=minutes,
                work_days_only=False,
                catch_up="poll",
            )
        )
    specs.append(
        ScheduleSpec(
            name="retention",
            kind="cron",
            zone=tz,
            times=(RETENTION_AT,),
            work_days_only=False,
            catch_up="coalesce",
        )
    )
    specs.append(
        ScheduleSpec(
            name="expire_approvals",
            kind="interval",
            zone=tz,
            every_minutes=EXPIRY_SWEEP_MINUTES,
            work_days_only=False,
            catch_up="poll",
        )
    )
    return specs


class WorkCalendar:
    def __init__(self, config: Config) -> None:
        self._days = set(config.work.days)
        self._holidays = {date.fromisoformat(d) for d in config.work.holidays}

    def is_holiday(self, day: date) -> bool:
        return day in self._holidays

    def is_work_day(self, day: date) -> bool:
        return WEEKDAYS[day.weekday()] in self._days and not self.is_holiday(day)

    @property
    def days(self) -> list[str]:
        return [d for d in WEEKDAYS if d in self._days]


def occurrences(
    spec: ScheduleSpec, start: datetime, end: datetime, calendar: WorkCalendar, include_off_days: bool = False
) -> list[datetime]:
    """Cron slots in (start, end], in UTC. Wall times are taken in the spec's zone on each local date,
    so a 09:00 Zurich slot is 13:30 IST in winter and 12:30 IST in summer."""
    zone = ZoneInfo(spec.zone)
    day, last = start.astimezone(zone).date(), end.astimezone(zone).date()
    out = []
    while day <= last:
        if include_off_days or not spec.work_days_only or calendar.is_work_day(day):
            for hh_mm in spec.times:
                slot = datetime.combine(day, _hh_mm(hh_mm), tzinfo=zone).astimezone(UTC)
                if start < slot <= end:
                    out.append(slot)
        day += timedelta(days=1)
    return sorted(out)


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


# APScheduler persists jobs by a text reference to a module-level function, so the running scheduler
# is found through this module.
_ACTIVE: "Scheduler | None" = None


async def fire_job(name: str) -> None:
    if _ACTIVE is None:
        log.warning("schedule fired with no scheduler running", extra={"schedule": name})
        return
    try:
        await _ACTIVE.fire(name)
    except Exception:
        log.exception("schedule fire failed", extra={"schedule": name})


async def heartbeat_job() -> None:
    if _ACTIVE is not None:
        try:
            await _ACTIVE.tick()
        except Exception:
            log.exception("scheduler heartbeat failed")


class Scheduler:
    def __init__(
        self,
        config: Config,
        clock: Clock,
        ingest: Ingest,
        runs: ScheduleRunRepo,
        kv: KvRepo,
        controls: ScheduleControls | None = None,
        database_url: str | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._ingest = ingest
        self._runs = runs
        self._kv = kv
        self._controls = controls
        self._calendar = WorkCalendar(config)
        self._url = database_url or config.database.url
        self.specs = {s.name: s for s in build_specs(config)}
        self._aps: AsyncIOScheduler | None = None

    def attach_controls(self, controls: ScheduleControls) -> None:
        self._controls = controls

    def set_recipe_specs(self, specs: list[ScheduleSpec]) -> None:
        """Replace the recipe schedules (after a recipe load or reload); re-register when running."""
        base = {n: s for n, s in self.specs.items() if not n.startswith("recipe:")}
        self.specs = base | {s.name: s for s in specs if s.name.startswith("recipe:")}
        if self._aps is not None:
            self.register()

    # ------------------------------------------------------------ APScheduler

    async def start(self) -> list[FireResult]:
        """Register jobs in the persistent job store, start firing, then catch up on anything missed."""
        global _ACTIVE
        self._aps = AsyncIOScheduler(
            timezone=UTC,
            jobstores={"default": SQLAlchemyJobStore(url=sync_url(self._url))},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
        )
        _ACTIVE = self
        self._aps.start(paused=True)
        self.register()
        self._aps.resume()
        return await self.catch_up()

    async def stop(self) -> None:
        global _ACTIVE
        if self._aps is not None:
            self._aps.shutdown(wait=False)
            self._aps = None
        if _ACTIVE is self:
            _ACTIVE = None

    def register(self) -> list[str]:
        """(Re)write every job from config; remove jobs config no longer has (a removed poller)."""
        aps = self._aps
        assert aps is not None
        wanted: list[str] = []
        for spec in self.specs.values():
            if spec.kind == "cron":
                zone = ZoneInfo(spec.zone)
                days = ",".join(self._calendar.days) if spec.work_days_only else "*"
                for hh_mm in spec.times:
                    at = _hh_mm(hh_mm)
                    job_id = f"{JOB_PREFIX}{spec.name}:{hh_mm}"
                    trigger = CronTrigger(day_of_week=days, hour=at.hour, minute=at.minute, timezone=zone)
                    aps.add_job(
                        f"{__name__}:fire_job",
                        trigger,
                        args=[spec.name],
                        id=job_id,
                        name=spec.name,
                        replace_existing=True,
                    )
                    wanted.append(job_id)
            else:
                job_id = f"{JOB_PREFIX}{spec.name}"
                trigger = IntervalTrigger(minutes=spec.every_minutes or 1, timezone=UTC)
                aps.add_job(
                    f"{__name__}:fire_job",
                    trigger,
                    args=[spec.name],
                    id=job_id,
                    name=spec.name,
                    replace_existing=True,
                )
                wanted.append(job_id)
        aps.add_job(
            f"{__name__}:heartbeat_job",
            IntervalTrigger(seconds=HEARTBEAT_SECONDS, timezone=UTC),
            id="scheduler:heartbeat",
            replace_existing=True,
        )
        for job in aps.get_jobs():
            if job.id.startswith(JOB_PREFIX) and job.id not in wanted:
                job.remove()
                log.info("removed a schedule no longer in config", extra={"job": job.id})
        return wanted

    def job_ids(self) -> list[str]:
        return sorted(job.id for job in self._aps.get_jobs()) if self._aps else []

    # ------------------------------------------------------------ firing

    async def fire(
        self,
        name: str,
        due_at: datetime | None = None,
        *,
        status: Literal["ran", "caught_up"] = "ran",
        detail: str | None = None,
        manual: bool = False,
    ) -> FireResult:
        spec = self.specs[name]
        now = self._clock.now_utc()
        due = now if manual else (due_at or self._due_now(spec, now))
        skip = None if manual else await self._skip_reason(spec, due)
        if skip is not None:
            await self._runs.claim(name, due, "skipped", skip)
            return FireResult(schedule=name, due_at=due, status="skipped", detail=skip)

        missed_key = MISSED_KEY.format(name=name)
        missed_since = await self._kv.get(missed_key)
        payload: dict[str, Any] = {"schedule": name, "due_at": due.isoformat(), "late": status == "caught_up"}
        if detail:
            payload["detail"] = detail
        if missed_since:
            payload["missed_since"] = missed_since  # "the brief of <date> was missed"
        if manual:
            payload["manual"] = True
        external_id = f"{name}@{due.isoformat()}" + ("@manual" if manual else "")
        result = await self._ingest(
            {"kind": "schedule", "external_id": external_id, "received_at": now, "payload": payload},
            "schedule",
        )
        if result.duplicate:
            return FireResult(schedule=name, due_at=due, status="duplicate")
        await self._runs.claim(name, due, status, detail or ("manual" if manual else None), over_paused=True)
        if missed_since:
            await self._kv.delete(missed_key)
        return FireResult(schedule=name, due_at=due, status=status, detail=detail, job_ids=result.job_ids)

    def _due_now(self, spec: ScheduleSpec, now: datetime) -> datetime:
        if spec.kind == "cron":
            slots = occurrences(spec, now - timedelta(days=8), now, self._calendar, include_off_days=True)
            if slots:
                return slots[-1]
        return _floor_minute(now)

    async def _skip_reason(self, spec: ScheduleSpec, due: datetime) -> str | None:
        if self._controls is not None and await self._controls.paused():
            return PAUSED_DETAIL
        if spec.leave_skips and self._controls is not None and await self._controls.on_leave():
            return "leave mode"
        if spec.kind == "cron" and spec.work_days_only:
            day = due.astimezone(ZoneInfo(spec.zone)).date()
            if self._calendar.is_holiday(day):
                return "holiday"
            if not self._calendar.is_work_day(day):
                return "not a work day"
        return None

    # ------------------------------------------------------------ catch-up and heartbeat

    async def catch_up(self, now: datetime | None = None, since: datetime | None = None) -> list[FireResult]:
        """Run what was missed between the last heartbeat (or `since`, e.g. the pause) and now."""
        now = now or self._clock.now_utc()
        last = since or await self._heartbeat()
        if last is None or last >= now:
            if last is not None and last > now:
                log.warning("heartbeat is in the future; clock changed. Not catching up")
            await self._beat(now)
            return []
        start = max(last, now - MAX_CATCH_UP)
        results: list[FireResult] = []
        for spec in self.specs.values():
            if spec.kind == "interval":
                if now - last >= timedelta(minutes=spec.every_minutes or 1):
                    results.append(
                        await self.fire(
                            spec.name, _floor_minute(now), status="caught_up", detail="after a gap"
                        )
                    )
                continue
            results += await self._catch_up_cron(spec, start, now)
        await self._beat(now)
        return results

    async def _catch_up_cron(self, spec: ScheduleSpec, start: datetime, now: datetime) -> list[FireResult]:
        recorded = await self._runs.recorded_since(spec.name, start)
        due = [
            slot
            for slot in occurrences(spec, start, now, self._calendar)
            if slot not in recorded
            or (recorded[slot]["status"] == "skipped" and recorded[slot]["detail"] == PAUSED_DETAIL)
        ]
        if not due:
            return []
        zone = ZoneInfo(spec.zone)
        latest: datetime | None = due[-1]
        earlier = due[:-1]
        if spec.catch_up == "latest_today" and due[-1].astimezone(zone).date() != now.astimezone(zone).date():
            latest, earlier = None, due  # not today: the next one runs on time and notes what was missed
            key = MISSED_KEY.format(name=spec.name)
            if await self._kv.get(key) is None:
                await self._kv.set(key, due[0].astimezone(zone).date().isoformat())
        for slot in earlier:
            note = "coalesced into a later run" if latest is not None else "missed while the agent was off"
            await self._runs.claim(spec.name, slot, "missed", note, over_paused=True)
        if latest is None:
            return [
                FireResult(schedule=spec.name, due_at=due[-1], status="missed", detail=f"{len(due)} missed")
            ]
        detail = f"{len(due)} missed runs coalesced" if len(due) > 1 else "missed run"
        return [await self.fire(spec.name, latest, status="caught_up", detail=detail)]

    async def tick(self, now: datetime | None = None) -> list[FireResult]:
        """The heartbeat. A gap longer than three beats means the laptop slept: catch up."""
        now = now or self._clock.now_utc()
        last = await self._heartbeat()
        if last is not None and now - last > GAP:
            return await self.catch_up(now)
        await self._beat(now)
        return []

    async def _heartbeat(self) -> datetime | None:
        value = await self._kv.get(HEARTBEAT_KEY)
        return datetime.fromisoformat(value) if value else None

    async def _beat(self, now: datetime) -> None:
        await self._kv.set(HEARTBEAT_KEY, now.isoformat())
