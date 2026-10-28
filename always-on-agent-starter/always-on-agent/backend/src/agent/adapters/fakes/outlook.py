"""Fake Outlook mail and calendar (A01, A02)."""

import base64
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.adapters.base import Attachment, CalendarEvent, MailMessage, Person, ToolSpec
from agent.adapters.fakes._base import FIXTURES_DIR, FakeAdapter, parse_time
from agent.core.clock import Clock
from agent.core.ids import new_ulid

_WINDOWS_ZONES = {
    "India Standard Time": "Asia/Kolkata",
    "W. Europe Standard Time": "Europe/Zurich",
    "UTC": "UTC",
}


def _person(raw: dict[str, Any]) -> Person:
    email = raw["emailAddress"]
    return Person(name=email["name"], address=email["address"])


def to_mail(raw: dict[str, Any]) -> MailMessage:
    return MailMessage(
        id=raw["id"],
        conversation_id=raw["conversationId"],
        subject=raw["subject"],
        sender=_person(raw["from"]),
        to=[_person(p) for p in raw.get("toRecipients", [])],
        cc=[_person(p) for p in raw.get("ccRecipients", [])],
        received_at=parse_time(raw["receivedDateTime"]),
        body=raw["body"]["content"],
        has_attachments=raw.get("hasAttachments", False),
        categories=raw.get("categories", []),
        importance=raw.get("importance", "normal"),
    )


class FakeOutlook(FakeAdapter):
    name = "outlook"

    def __init__(self, clock: Clock, fixtures: Path = FIXTURES_DIR) -> None:
        super().__init__(clock, fixtures)
        self.raw: dict[str, dict[str, Any]] = {m["id"]: m for m in self._load("emails/messages.json")}
        self.folders: dict[str, str] = dict.fromkeys(self.raw, "Inbox")
        self.drafts: dict[str, dict[str, Any]] = {}
        self.sent: list[str] = []

    def deliver(self, raw: dict[str, Any]) -> MailMessage:
        """Deliver a new Graph-style message now (tests)."""
        self.raw[raw["id"]] = raw
        self.folders[raw["id"]] = "Inbox"
        return to_mail(raw)

    async def list_new(self, since: datetime) -> list[MailMessage]:
        self._check("list_new")
        mails = [to_mail(r) for r in self.raw.values()]
        return sorted((m for m in mails if m.received_at > since), key=lambda m: m.received_at)

    async def get_thread(self, conversation_id: str) -> list[MailMessage]:
        self._check("get_thread")
        thread = [to_mail(r) for r in self.raw.values() if r["conversationId"] == conversation_id]
        return sorted(thread, key=lambda m: m.received_at)

    async def get_attachments(self, message_id: str) -> list[Attachment]:
        self._check("get_attachments")
        return [
            Attachment(
                name=a["name"], content_type=a["contentType"], data=base64.b64decode(a["contentBytes"])
            )
            for a in self.raw[message_id].get("attachments", [])
        ]

    async def move(self, message_id: str, folder: str) -> None:
        self._record("move", message_id=message_id, folder=folder)
        self.folders[message_id] = folder

    async def categorise(self, message_id: str, categories: list[str]) -> None:
        self._record("categorise", message_id=message_id, categories=categories)
        self.raw[message_id]["categories"] = categories

    async def flag(self, message_id: str) -> None:
        self._record("flag", message_id=message_id)

    async def create_draft(
        self, to: list[str], subject: str, body: str, cc: list[str] | None = None, reply_to: str | None = None
    ) -> str:
        self._record("create_draft", to=to, subject=subject, body=body, cc=cc or [], reply_to=reply_to)
        draft_id = f"DRAFT-{new_ulid()}"
        self.drafts[draft_id] = {
            "to": to,
            "cc": cc or [],
            "subject": subject,
            "body": body,
            "reply_to": reply_to,
        }
        return draft_id

    async def send(self, draft_id: str, idempotency_key: str) -> None:
        """Sending twice with the same idempotency key sends once."""
        if any(w.args.get("idempotency_key") == idempotency_key for w in self.writes_to("send")):
            return
        self._record("send", draft_id=draft_id, idempotency_key=idempotency_key)
        self.sent.append(draft_id)

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="read_mail", mode="silent", description="Read mail"),
            ToolSpec(name="move_mail", mode="report", description="Move a message to a folder"),
            ToolSpec(name="categorise", mode="report", description="Set categories"),
            ToolSpec(name="flag_mail", mode="report", description="Flag a message"),
            ToolSpec(name="save_draft", mode="report", description="Save a draft"),
            ToolSpec(name="send_mail", mode="ask", description="Send a draft"),
        ]


class FakeCalendar(FakeAdapter):
    name = "calendar"

    def __init__(self, clock: Clock, fixtures: Path = FIXTURES_DIR) -> None:
        super().__init__(clock, fixtures)
        self.raw: list[dict[str, Any]] = self._load("calendar/events.json")

    def deliver(self, raw: dict[str, Any]) -> None:
        self.raw.append(raw)

    async def events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        self._check("events_between")
        events = [self._to_event(r) for r in self.raw]
        return sorted((e for e in events if e.end > start and e.start < end), key=lambda e: e.start)

    async def add_hold(self, subject: str, start: datetime, end: datetime) -> str:
        self._record("add_hold", subject=subject, start=start.isoformat(), end=end.isoformat())
        event_id = f"HOLD-{new_ulid()}"
        self.raw.append(
            {
                "id": event_id,
                "subject": subject,
                "sensitivity": "normal",
                "showAs": "tentative",
                "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
                "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
                "organizer": None,
                "onlineMeeting": None,
            }
        )
        return event_id

    @staticmethod
    def _to_event(raw: dict[str, Any]) -> CalendarEvent:
        from zoneinfo import ZoneInfo

        def when(part: dict[str, str]) -> datetime:
            dt = datetime.fromisoformat(part["dateTime"][:19])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo(_WINDOWS_ZONES[part["timeZone"]]))
            return dt

        private = raw.get("sensitivity") == "private"
        meeting = raw.get("onlineMeeting") or {}
        return CalendarEvent(
            id=raw["id"],
            subject="(private)" if private else raw["subject"],
            start=when(raw["start"]),
            end=when(raw["end"]),
            private=private,
            show_as=raw.get("showAs", "busy"),
            organizer=_person(raw["organizer"]) if raw.get("organizer") else None,
            join_url=meeting.get("joinUrl"),
        )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="read_calendar", mode="silent", description="Read events"),
            ToolSpec(name="add_hold", mode="report", description="Add a tentative hold"),
        ]
