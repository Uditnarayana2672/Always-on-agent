"""Clocks for work hours, quiet hours and caller time conversion (F02, F08).

Never call datetime.now() elsewhere; take a Clock so tests can freeze time.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from agent.core.config import Config

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_hh_mm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


class Clock(Protocol):
    def now_utc(self) -> datetime: ...
    def now_local(self) -> datetime: ...
    def to_local(self, dt: datetime) -> datetime: ...
    def is_work_time(self, dt: datetime) -> bool: ...
    def is_quiet(self, dt: datetime) -> bool: ...
    def next_occurrence(self, local_time: str, days: list[str] | None = None) -> datetime: ...
    def caller_time_to_local(self, hh_mm: str, on: date) -> datetime: ...


class _BaseClock:
    """Shared calendar logic; subclasses supply now_utc()."""

    def __init__(self, config: Config) -> None:
        self._local = ZoneInfo(config.timezone)
        self._caller = ZoneInfo(config.caller_timezone)
        self._work_days = set(config.work.days)
        self._work_start = _parse_hh_mm(config.work.start)
        self._work_end = _parse_hh_mm(config.work.end)
        self._holidays = {date.fromisoformat(d) for d in config.work.holidays}
        self._quiet_start = _parse_hh_mm(config.notifications.quiet_hours.start)
        self._quiet_end = _parse_hh_mm(config.notifications.quiet_hours.end)

    def now_utc(self) -> datetime:
        raise NotImplementedError

    def now_local(self) -> datetime:
        return self.now_utc().astimezone(self._local)

    def to_local(self, dt: datetime) -> datetime:
        _require_aware(dt)
        return dt.astimezone(self._local)

    def is_holiday(self, day: date) -> bool:
        return day in self._holidays

    def is_work_day(self, day: date) -> bool:
        return _WEEKDAYS[day.weekday()] in self._work_days and not self.is_holiday(day)

    def is_work_time(self, dt: datetime) -> bool:
        local = self.to_local(dt)
        return self.is_work_day(local.date()) and self._work_start <= local.time() < self._work_end

    def is_quiet(self, dt: datetime) -> bool:
        t = self.to_local(dt).time()
        if self._quiet_start <= self._quiet_end:
            return self._quiet_start <= t < self._quiet_end
        return t >= self._quiet_start or t < self._quiet_end

    def next_occurrence(self, local_time: str, days: list[str] | None = None) -> datetime:
        """Next moment after now at local_time on one of days (default: work days), skipping holidays.

        Returned in UTC.
        """
        wanted = set(days) if days is not None else self._work_days
        at = _parse_hh_mm(local_time)
        now = self.now_utc()
        day = self.now_local().date()
        for _ in range(400):
            if _WEEKDAYS[day.weekday()] in wanted and not self.is_holiday(day):
                candidate = datetime.combine(day, at, tzinfo=self._local).astimezone(UTC)
                if candidate > now:
                    return candidate
            day += timedelta(days=1)
        raise ValueError("no occurrence within 400 days")

    def caller_time_to_local(self, hh_mm: str, on: date) -> datetime:
        """The caller's wall-clock time on a date, expressed in the user's local zone."""
        caller_dt = datetime.combine(on, _parse_hh_mm(hh_mm), tzinfo=self._caller)
        return caller_dt.astimezone(self._local)


class SystemClock(_BaseClock):
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock(_BaseClock):
    """A clock fixed at a given instant; move it with advance() or set()."""

    def __init__(self, config: Config, at: datetime) -> None:
        super().__init__(config)
        self.set(at)

    def set(self, at: datetime) -> None:
        _require_aware(at)
        self._now = at.astimezone(UTC)

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def now_utc(self) -> datetime:
        return self._now


class ShiftedClock(_BaseClock):
    """Real time, moved to start at a given instant (agentd run --fake: the fixtures' Wednesday)."""

    def __init__(self, config: Config, start: datetime) -> None:
        super().__init__(config)
        _require_aware(start)
        self._offset = start.astimezone(UTC) - datetime.now(UTC)

    def now_utc(self) -> datetime:
        return datetime.now(UTC) + self._offset
