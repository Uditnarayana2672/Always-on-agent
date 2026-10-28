from datetime import UTC, date, datetime, time

import pytest

from agent.core.clock import FrozenClock
from agent.core.config import Config, load_config
from tests.core.conftest import with_overrides


@pytest.fixture
def clock(config: Config) -> FrozenClock:
    # Wednesday 2026-03-25 10:00 IST
    return FrozenClock(config, datetime(2026, 3, 25, 4, 30, tzinfo=UTC))


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 3, 28), time(13, 30)),  # Saturday before the March change: CET
        (date(2026, 3, 29), time(12, 30)),  # last Sunday of March: CEST from 02:00
        (date(2026, 3, 30), time(12, 30)),
        (date(2026, 10, 24), time(12, 30)),  # Saturday before the October change: CEST
        (date(2026, 10, 25), time(13, 30)),  # last Sunday of October: CET from 03:00
        (date(2026, 10, 26), time(13, 30)),
    ],
)
def test_chase_window_across_dst(clock: FrozenClock, day: date, expected: time) -> None:
    local = clock.caller_time_to_local("09:00", day)
    assert local.date() == day
    assert local.time() == expected
    assert local.utcoffset() is not None and local.utcoffset().total_seconds() == 5.5 * 3600  # type: ignore[union-attr]


def test_kolkata_has_no_dst(clock: FrozenClock) -> None:
    offsets = {clock.to_local(datetime(2026, m, 15, 12, tzinfo=UTC)).utcoffset() for m in range(1, 13)}
    assert len(offsets) == 1


@pytest.mark.parametrize(
    ("ist", "quiet"),
    [
        (time(21, 29), False),
        (time(21, 30), True),
        (time(23, 59), True),
        (time(0, 0), True),
        (time(8, 29), True),
        (time(8, 30), False),
        (time(12, 0), False),
    ],
)
def test_quiet_hours_span_midnight(clock: FrozenClock, ist: time, quiet: bool) -> None:
    dt = datetime.combine(date(2026, 3, 25), ist, tzinfo=clock.now_local().tzinfo)
    assert clock.is_quiet(dt) is quiet


def test_work_time(clock: FrozenClock) -> None:
    tz = clock.now_local().tzinfo
    assert clock.is_work_time(datetime(2026, 3, 25, 9, 0, tzinfo=tz))
    assert not clock.is_work_time(datetime(2026, 3, 25, 18, 30, tzinfo=tz))
    assert not clock.is_work_time(datetime(2026, 3, 28, 10, 0, tzinfo=tz))  # Saturday


def test_next_occurrence_skips_holiday(config: Config) -> None:
    cfg = with_overrides(config, work={"holidays": ["2026-03-26"]})
    # Wednesday 2026-03-25 10:00 IST: brief at 09:00 already passed today, Thursday is a holiday
    clock = FrozenClock(cfg, datetime(2026, 3, 25, 4, 30, tzinfo=UTC))
    nxt = clock.to_local(clock.next_occurrence("09:00"))
    assert nxt.date() == date(2026, 3, 27)
    assert nxt.time() == time(9, 0)


def test_next_occurrence_skips_weekend_and_returns_utc(config: Config) -> None:
    # Friday 2026-03-27 19:00 IST
    clock = FrozenClock(config, datetime(2026, 3, 27, 13, 30, tzinfo=UTC))
    nxt = clock.next_occurrence("09:00")
    assert nxt.tzinfo is UTC
    assert clock.to_local(nxt).date() == date(2026, 3, 30)


def test_next_occurrence_later_today(clock: FrozenClock) -> None:
    nxt = clock.to_local(clock.next_occurrence("19:00", ["wed"]))
    assert nxt.date() == date(2026, 3, 25)


def test_naive_datetime_rejected(clock: FrozenClock) -> None:
    with pytest.raises(ValueError):
        clock.to_local(datetime(2026, 3, 25, 10, 0))


def test_example_config_loads() -> None:
    assert load_config().timezone == "Asia/Kolkata"
