"""The 24-hour clock (F08, R08 clock rules).

Pure functions so the rules are easy to test. The clock is wall time, not work hours: a ticket
assigned on Friday evening is at 60 h by Monday morning unless a pause covers the weekend.
"""

from datetime import datetime, timedelta
from typing import Literal

ClockStart = Literal["opened", "assigned", "email_received"]

# When the configured start is unknown, use the next best one in this order and mark it estimated.
_FALLBACK: tuple[ClockStart, ...] = ("email_received", "opened", "assigned")


def choose_start(
    rule: ClockStart,
    opened_at: datetime | None,
    assigned_at: datetime | None,
    email_received_at: datetime | None,
) -> tuple[datetime | None, bool]:
    """(start, estimated). Estimated means the configured start was missing and another was used."""
    known: dict[ClockStart, datetime | None] = {
        "opened": opened_at,
        "assigned": assigned_at,
        "email_received": email_received_at,
    }
    if known[rule] is not None:
        return known[rule], False
    for other in _FALLBACK:
        if known[other] is not None:
            return known[other], True
    return None, False


def paused_time(start: datetime, now: datetime, pauses: list[tuple[datetime, datetime | None]]) -> timedelta:
    """Total paused time inside [start, now]. An open pause runs to now; overlaps count once."""
    clipped = sorted(
        (max(p_start, start), min(p_end or now, now)) for p_start, p_end in pauses if (p_end or now) > start
    )
    total = timedelta(0)
    cur_start: datetime | None = None
    cur_end: datetime | None = None
    for p_start, p_end in clipped:
        if p_end <= p_start:
            continue
        if cur_end is None or p_start > cur_end:
            if cur_start is not None and cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = p_start, p_end
        else:
            cur_end = max(cur_end, p_end)
    if cur_start is not None and cur_end is not None:
        total += cur_end - cur_start
    return total


def age(
    start: datetime,
    now: datetime,
    pauses: list[tuple[datetime, datetime | None]],
    pause_on_hold: bool,
) -> timedelta:
    """Clock age at `now`. Never negative (a start in the future reads as zero)."""
    elapsed = now - start
    if pause_on_hold:
        elapsed -= paused_time(start, now, pauses)
    return max(elapsed, timedelta(0))


def crossed(age_hours: float, thresholds: list[int]) -> list[int]:
    """Thresholds at or below the current age, lowest first."""
    return sorted(h for h in thresholds if age_hours >= h)
