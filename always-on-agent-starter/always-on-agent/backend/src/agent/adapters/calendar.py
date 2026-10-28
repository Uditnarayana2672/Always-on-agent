"""Calendar through Microsoft Graph (A02).

- calendarView expands recurring series, including moved or cancelled occurrences (exceptions).
- Times are asked for in UTC (Prefer: outlook.timezone="UTC"); an event that still comes back in a
  Windows zone name ("W. Europe Standard Time") is converted with WINDOWS_ZONES, so a meeting set in
  Zurich lands at the right IST time on both sides of Europe's clock change.
- All-day events are floating dates: they are read as midnight-to-midnight in the user's zone.
- Private events come back masked: no subject, organizer, join link or categories.
- Holds go on the user's own calendar only: tentative, no attendees, no reminder, tagged "Agent hold"
  and an AgentHold property. Graph's transactionId makes a retried create return the same event.
  discard_hold() deletes only a hold the agent made that nobody was invited to since.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agent.adapters.base import CalendarEvent, HealthStatus, Person, ToolSpec
from agent.adapters.graph_client import GraphClient, NotFound
from agent.adapters.outlook_graph import PS_PUBLIC_STRINGS, parse_graph_time
from agent.core.errors import AdapterError, AgentError

HOLD_CATEGORY = "Agent hold"
HOLD_PROP = f"String {PS_PUBLIC_STRINGS} Name AgentHold"
MAX_HOLD = timedelta(hours=8)
HEADERS = {"Prefer": 'outlook.timezone="UTC", IdType="ImmutableId"'}
SELECT = ",".join(
    [
        "id",
        "subject",
        "start",
        "end",
        "isAllDay",
        "isCancelled",
        "sensitivity",
        "showAs",
        "organizer",
        "isOrganizer",
        "attendees",
        "onlineMeeting",
        "responseStatus",
        "categories",
        "seriesMasterId",
        "type",
    ]
)

# The Windows zone names Graph uses for the zones this agent meets; anything else falls back to UTC
# only after trying the name as an IANA zone.
WINDOWS_ZONES = {
    "India Standard Time": "Asia/Kolkata",
    "W. Europe Standard Time": "Europe/Zurich",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Eastern Standard Time": "America/New_York",
    "Pacific Standard Time": "America/Los_Angeles",
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
}


def zone_for(name: str | None) -> ZoneInfo:
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(WINDOWS_ZONES.get(name, name))
    except Exception:  # unknown or malformed zone name
        return ZoneInfo("UTC")


def graph_when(part: dict[str, Any]) -> datetime:
    """A Graph dateTimeTimeZone as an aware UTC datetime."""
    raw = str(part.get("dateTime") or "")
    zone = str(part.get("timeZone") or "UTC")
    if zone in ("UTC", "Coordinated Universal Time") or raw.endswith("Z"):
        return parse_graph_time(raw)
    naive = parse_graph_time(raw).replace(tzinfo=None)
    return naive.replace(tzinfo=zone_for(zone)).astimezone(UTC)


def to_event(raw: dict[str, Any], local_zone: ZoneInfo) -> CalendarEvent:
    private = str(raw.get("sensitivity") or "normal") == "private"
    all_day = bool(raw.get("isAllDay"))
    if all_day:
        # Floating dates: the date part, in the user's own zone.
        first = date.fromisoformat(str(raw["start"]["dateTime"])[:10])
        last = date.fromisoformat(str(raw["end"]["dateTime"])[:10])
        start = datetime.combine(first, time(0), local_zone).astimezone(UTC)
        end = datetime.combine(last, time(0), local_zone).astimezone(UTC)
    else:
        start, end = graph_when(raw["start"]), graph_when(raw["end"])
    organizer = raw.get("organizer") or {}
    email = organizer.get("emailAddress") or {}
    response = (raw.get("responseStatus") or {}).get("response")
    return CalendarEvent(
        id=str(raw["id"]),
        subject="(private)" if private else str(raw.get("subject") or ""),
        start=start,
        end=end,
        private=private,
        show_as=str(raw.get("showAs") or "busy"),
        organizer=None
        if private or not email
        else Person(name=str(email.get("name") or ""), address=str(email.get("address") or "").lower()),
        join_url=None if private else ((raw.get("onlineMeeting") or {}).get("joinUrl")),
        all_day=all_day,
        cancelled=bool(raw.get("isCancelled")),
        response="organizer" if raw.get("isOrganizer") else (str(response) if response else None),
        attendees=len(raw.get("attendees") or []),
        categories=[] if private else list(raw.get("categories") or []),
        series_id=raw.get("seriesMasterId"),
    )


class GraphCalendarAdapter:
    """Implements CalendarAdapter over Graph for the user's default calendar."""

    name = "calendar"

    def __init__(self, client: GraphClient, local_zone: str, now: Callable[[], datetime]) -> None:
        self._g = client
        self._zone = ZoneInfo(local_zone)
        self._now = now

    async def health(self) -> HealthStatus:
        try:
            await self._g.get("me/calendar", {"$select": "id"}, headers=HEADERS)
        except AgentError as exc:
            return HealthStatus(connector=self.name, status="down", detail=str(exc)[:200])
        return HealthStatus(connector=self.name, status="ok", detail="Graph calendar reachable")

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="read_calendar", mode="silent", description="Read events"),
            ToolSpec(name="add_hold", mode="report", description="Add a tentative hold"),
        ]

    async def events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        raw = await self._g.get_all(
            "me/calendarView",
            {
                "startDateTime": _iso(start),
                "endDateTime": _iso(end),
                "$select": SELECT,
                "$orderby": "start/dateTime",
                "$top": "100",
            },
            headers=HEADERS,
        )
        events = [to_event(r, self._zone) for r in raw]
        return sorted((e for e in events if e.end > start and e.start < end), key=lambda e: e.start)

    async def get_event(self, event_id: str) -> CalendarEvent | None:
        try:
            raw = await self._g.get(f"me/events/{event_id}", {"$select": SELECT}, headers=HEADERS)
        except NotFound:
            return None
        return to_event(raw, self._zone)

    async def add_hold(
        self, subject: str, start: datetime, end: datetime, idempotency_key: str | None = None
    ) -> str:
        check_hold(subject, start, end, self._now())
        body: dict[str, Any] = {
            "subject": subject,
            "start": {"dateTime": _naive_utc(start), "timeZone": "UTC"},
            "end": {"dateTime": _naive_utc(end), "timeZone": "UTC"},
            "showAs": "tentative",
            "isReminderOn": False,
            "responseRequested": False,
            "categories": [HOLD_CATEGORY],
            "singleValueExtendedProperties": [{"id": HOLD_PROP, "value": "1"}],
        }
        if idempotency_key:
            body["transactionId"] = idempotency_key[:200]
        created = await self._g.post("me/events", body, headers=HEADERS)
        return str(created["id"])

    async def discard_hold(self, event_id: str) -> None:
        """The add_hold inverse. Only an agent hold with no attendees is removed."""
        try:
            raw = await self._g.get(
                f"me/events/{event_id}",
                {"$select": SELECT, "$expand": f"singleValueExtendedProperties($filter=id eq '{HOLD_PROP}')"},
                headers=HEADERS,
            )
        except NotFound:
            return
        props = {
            str(p.get("id", "")).lower(): p.get("value")
            for p in raw.get("singleValueExtendedProperties") or []
        }
        if props.get(HOLD_PROP.lower()) != "1":
            raise AdapterError("this event was not created by the agent; it is left alone")
        if raw.get("attendees"):
            raise AdapterError(
                "people were invited to this hold since; removing it would cancel their meeting"
            )
        if not raw.get("isOrganizer", True):
            raise AdapterError("this event is not on your own calendar")
        await self._g.delete(f"me/events/{event_id}", headers=HEADERS)


def check_hold(subject: str, start: datetime, end: datetime, now: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise AdapterError("hold times must be timezone-aware")
    if end <= start:
        raise AdapterError("a hold must end after it starts")
    if end - start > MAX_HOLD:
        raise AdapterError("a hold longer than 8 hours needs you to add it")
    if end <= now:
        raise AdapterError("this hold would be in the past")
    if not subject.strip():
        raise AdapterError("a hold needs a subject")


def _naive_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
