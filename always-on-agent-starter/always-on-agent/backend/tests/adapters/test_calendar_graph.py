from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from agent.adapters.calendar import HOLD_PROP, GraphCalendarAdapter, graph_when, to_event
from agent.core.errors import AdapterError
from tests.adapters.graph_helpers import NOW, client
from tests.adapters.graph_sim import CalendarSim

IST = "Asia/Kolkata"


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def sim() -> AsyncIterator[CalendarSim]:
    with respx.mock(assert_all_called=False) as router:
        yield CalendarSim(router)


def cal(http: httpx.AsyncClient) -> GraphCalendarAdapter:
    return GraphCalendarAdapter(client(http, "calendar"), IST, now=lambda: NOW)


def test_windows_zone_follows_europes_clock_change() -> None:
    # 10:00 in Zurich is 09:00 UTC before the change (CET) and 08:00 UTC after it (CEST).
    before = graph_when({"dateTime": "2026-03-27T10:00:00.0000000", "timeZone": "W. Europe Standard Time"})
    after = graph_when({"dateTime": "2026-03-30T10:00:00.0000000", "timeZone": "W. Europe Standard Time"})
    assert before == datetime(2026, 3, 27, 9, tzinfo=UTC)
    assert after == datetime(2026, 3, 30, 8, tzinfo=UTC)
    assert graph_when({"dateTime": "2026-03-25T04:00:00.0000000", "timeZone": "UTC"}) == datetime(
        2026, 3, 25, 4, tzinfo=UTC
    )


def test_private_all_day_and_cancelled() -> None:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(IST)
    private = to_event(
        {
            "id": "p",
            "subject": "Doctor",
            "sensitivity": "private",
            "start": {"dateTime": "2026-03-25T11:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-03-25T12:00:00", "timeZone": "UTC"},
            "organizer": {"emailAddress": {"name": "Me", "address": "test.user@corp.example"}},
            "onlineMeeting": {"joinUrl": "https://teams.example/x"},
            "categories": ["Health"],
        },
        zone,
    )
    assert private.subject == "(private)" and private.organizer is None and private.join_url is None
    assert private.categories == []
    holiday = to_event(
        {
            "id": "h",
            "subject": "Holi",
            "isAllDay": True,
            "start": {"dateTime": "2026-03-04T00:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-03-05T00:00:00", "timeZone": "UTC"},
        },
        zone,
    )
    # Floating dates in the user's zone: midnight IST is 18:30 UTC the day before.
    assert holiday.all_day and holiday.start == datetime(2026, 3, 3, 18, 30, tzinfo=UTC)
    cancelled = to_event(
        {
            "id": "c",
            "subject": "Moved",
            "isCancelled": True,
            "start": {"dateTime": "2026-03-25T05:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-03-25T06:00:00", "timeZone": "UTC"},
            "seriesMasterId": "series-1",
        },
        zone,
    )
    assert cancelled.cancelled and cancelled.series_id == "series-1"


async def test_calendar_view_asks_for_utc_and_immutable_ids(
    http: httpx.AsyncClient, sim: CalendarSim
) -> None:
    sim.view = [
        {
            "id": "e1",
            "subject": "Stand-up",
            "start": {"dateTime": "2026-03-25T04:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-03-25T04:15:00.0000000", "timeZone": "UTC"},
        }
    ]
    events = await cal(http).events_between(NOW - timedelta(hours=2), NOW + timedelta(hours=2))
    assert [e.id for e in events] == ["e1"]
    request = sim.router.calls.last.request
    assert 'outlook.timezone="UTC"' in request.headers["Prefer"]
    with pytest.raises(ValueError):
        await cal(http).events_between(datetime(2026, 3, 25), datetime(2026, 3, 26))  # naive


async def test_add_hold_is_own_calendar_tentative_and_idempotent(
    http: httpx.AsyncClient, sim: CalendarSim
) -> None:
    start = NOW + timedelta(hours=1)
    adapter = cal(http)
    first = await adapter.add_hold(
        "Focus: INC30114872", start, start + timedelta(hours=1), idempotency_key="k1"
    )
    again = await adapter.add_hold(
        "Focus: INC30114872", start, start + timedelta(hours=1), idempotency_key="k1"
    )
    assert first == again and sim.creates == 1
    sent = sim.events[first]
    assert sent["showAs"] == "tentative" and sent["isReminderOn"] is False
    assert "attendees" not in sim.router.calls.last.request.content.decode() or sent["attendees"] == []
    assert sent["categories"] == ["Agent hold"] and sent["transactionId"] == "k1"
    with pytest.raises(AdapterError, match="past"):
        await adapter.add_hold("Late", NOW - timedelta(hours=2), NOW - timedelta(hours=1))
    with pytest.raises(AdapterError, match="8 hours"):
        await adapter.add_hold("Long", start, start + timedelta(hours=9))


async def test_discard_hold_only_for_agent_holds_without_attendees(
    http: httpx.AsyncClient, sim: CalendarSim
) -> None:
    start = NOW + timedelta(hours=1)
    adapter = cal(http)
    hold = await adapter.add_hold("Focus", start, start + timedelta(minutes=30))
    sim.events[hold]["attendees"] = [{"emailAddress": {"address": "anna.keller@corp.example"}}]
    with pytest.raises(AdapterError, match="invited"):
        await adapter.discard_hold(hold)
    sim.events[hold]["attendees"] = []
    await adapter.discard_hold(hold)
    assert hold not in sim.events
    sim.events["meeting"] = {"id": "meeting", "attendees": [], "singleValueExtendedProperties": []}
    with pytest.raises(AdapterError, match="not created by the agent"):
        await adapter.discard_hold("meeting")
    assert HOLD_PROP.endswith("AgentHold")
