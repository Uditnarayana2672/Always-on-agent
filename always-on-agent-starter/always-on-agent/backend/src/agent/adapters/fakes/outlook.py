"""Fake Outlook mail and calendar (A01, A02)."""

import base64
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.adapters.base import Attachment, AttachmentInfo, CalendarEvent, MailMessage, Person, ToolSpec
from agent.adapters.calendar import check_hold
from agent.adapters.fakes._base import FIXTURES_DIR, FakeAdapter, parse_time
from agent.adapters.html_text import body_text
from agent.adapters.outlook_graph import digest_text
from agent.core.clock import Clock
from agent.core.errors import AdapterError
from agent.core.ids import new_ulid

_WINDOWS_ZONES = {
    "India Standard Time": "Asia/Kolkata",
    "W. Europe Standard Time": "Europe/Zurich",
    "UTC": "UTC",
}


def _person(raw: dict[str, Any]) -> Person:
    email = raw["emailAddress"]
    return Person(name=email["name"], address=email["address"])


def to_mail(raw: dict[str, Any], folder: str = "Inbox") -> MailMessage:
    sensitivity = raw.get("sensitivity", "normal")
    protected = sensitivity in ("private", "confidential") or any(
        a["name"].lower() in ("message.rpmsg", "smime.p7m") for a in raw.get("attachments", [])
    )
    return MailMessage(
        id=raw["id"],
        conversation_id=raw["conversationId"],
        subject=raw["subject"],
        sender=_person(raw["from"]),
        to=[_person(p) for p in raw.get("toRecipients", [])],
        cc=[_person(p) for p in raw.get("ccRecipients", [])],
        received_at=parse_time(raw["receivedDateTime"]),
        body="" if protected else body_text(raw["body"]),
        has_attachments=raw.get("hasAttachments", False),
        categories=raw.get("categories", []),
        importance=raw.get("importance", "normal"),
        folder=folder,
        flagged=(raw.get("flag") or {}).get("flagStatus") == "flagged",
        attachments=[
            AttachmentInfo(
                name=a["name"],
                content_type=a["contentType"],
                size=len(base64.b64decode(a.get("contentBytes", ""))),
            )
            for a in raw.get("attachments", [])
        ],
        sensitivity=sensitivity,
        metadata_only=protected,
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
        mails = [self._mail(i) for i in self.raw if self.folders[i] == "Inbox"]
        return sorted((m for m in mails if m.received_at > since), key=lambda m: m.received_at)

    def _mail(self, message_id: str) -> MailMessage:
        return to_mail(self.raw[message_id], self.folders.get(message_id, "Inbox"))

    async def get_message(self, message_id: str) -> MailMessage:
        self._check("get_message")
        if message_id not in self.raw:
            raise AdapterError(f"outlook: message not found: {message_id}", status_code=404)
        return self._mail(message_id)

    async def get_thread(self, conversation_id: str) -> list[MailMessage]:
        self._check("get_thread")
        thread = [self._mail(i) for i, r in self.raw.items() if r["conversationId"] == conversation_id]
        return sorted(thread, key=lambda m: m.received_at)

    async def search(self, query: str, top: int = 25) -> list[MailMessage]:
        self._check("search")
        words = query.lower().split()
        hits = [
            m
            for m in (self._mail(i) for i in self.raw)
            if words and all(w in f"{m.subject} {m.body}".lower() for w in words)
        ]
        return sorted(hits, key=lambda m: m.received_at, reverse=True)[:top]

    async def get_attachments(self, message_id: str) -> list[Attachment]:
        self._check("get_attachments")
        if self._mail(message_id).metadata_only:
            raise AdapterError("this message is protected; its attachments stay in Outlook")
        return [
            Attachment(
                name=a["name"], content_type=a["contentType"], data=base64.b64decode(a["contentBytes"])
            )
            for a in self.raw[message_id].get("attachments", [])
        ]

    async def move(self, message_id: str, folder: str) -> str:
        if folder.strip().lower().replace(" ", "") == "deleteditems":
            raise AdapterError("moving to Deleted Items is deleting; that is a hand-off")
        self._record("move", message_id=message_id, folder=folder)
        self.folders[message_id] = folder
        return message_id  # immutable ids: a move keeps the id

    async def categorise(self, message_id: str, categories: list[str]) -> None:
        self._record("categorise", message_id=message_id, categories=categories)
        self.raw[message_id]["categories"] = categories

    async def get_categories(self, message_id: str) -> list[str]:
        self._check("get_categories")
        return list(self.raw[message_id].get("categories", []))

    async def flag(self, message_id: str) -> None:
        await self.set_flag(message_id, True)

    async def unflag(self, message_id: str) -> None:
        await self.set_flag(message_id, False)

    async def set_flag(self, message_id: str, flagged: bool) -> None:
        self._record("flag" if flagged else "unflag", message_id=message_id)
        self.raw[message_id]["flag"] = {"flagStatus": "flagged" if flagged else "notFlagged"}

    async def is_flagged(self, message_id: str) -> bool:
        self._check("is_flagged")
        return (self.raw[message_id].get("flag") or {}).get("flagStatus") == "flagged"

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
            "digest": digest_text(body),
            "agent": True,
        }
        return draft_id

    def seed_draft(
        self, draft_id: str, to: list[str], body: str = "Draft text", cc: list[str] | None = None
    ) -> None:
        """A draft the agent saved earlier (tests)."""
        self.drafts[draft_id] = {
            "to": to,
            "cc": cc or [],
            "subject": "Draft",
            "body": body,
            "reply_to": None,
            "digest": digest_text(body),
            "agent": True,
        }

    def edit_draft(self, draft_id: str, **changes: Any) -> None:
        """The user edits a draft in Outlook (tests)."""
        self.drafts[draft_id].update(changes)

    async def send(
        self,
        draft_id: str,
        idempotency_key: str,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body: str | None = None,
    ) -> str:
        """Same contract as the real adapters: once per key, refuse a draft changed since approval."""
        done = await self.find_sent(idempotency_key)
        if done is not None:
            return done
        draft = self.drafts.get(draft_id)
        if draft is None:
            raise AdapterError("the draft no longer exists and nothing was sent; make a new draft")
        if to is not None:
            approved = sorted(a.lower() for a in to), sorted(a.lower() for a in cc or [])
            actual = sorted(a.lower() for a in draft["to"]), sorted(a.lower() for a in draft["cc"])
            if approved != actual:
                raise AdapterError(
                    "the draft's recipients changed in Outlook after approval; approve it again"
                )
        if digest_text(draft["body"]) != draft["digest"]:
            raise AdapterError("the draft was edited in Outlook after approval; approve the new text")
        if body and digest_text(body) != draft["digest"]:
            draft["body"] = body  # the approver edited the text: the draft is rebuilt with it
            draft["digest"] = digest_text(body)
        self._record("send", draft_id=draft_id, idempotency_key=idempotency_key)
        self.sent.append(draft_id)
        self.drafts.pop(draft_id)
        return draft_id

    async def get_folder(self, message_id: str) -> str:
        self._check("get_folder")
        return self.folders[message_id]

    async def discard_draft(self, draft_id: str) -> None:
        """Delete a draft the agent created (the save_draft inverse); nothing else can be deleted."""
        draft = self.drafts.get(draft_id)
        if draft is None:
            return
        if not draft.get("agent"):
            raise AdapterError("this draft was not created by the agent; it is left alone")
        self._record("discard_draft", draft_id=draft_id)
        self.drafts.pop(draft_id, None)

    async def find_sent(self, idempotency_key: str) -> str | None:
        """Sent Items lookup by the X-Agent-Idempotency header; the draft id if it was sent."""
        self._check("find_sent")
        for write in self.writes_to("send"):
            if write.args.get("idempotency_key") == idempotency_key:
                return str(write.args["draft_id"])
        return None

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="read_mail", mode="silent", description="Read mail"),
            ToolSpec(name="search_mail", mode="silent", description="Search mail"),
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

    async def add_hold(
        self, subject: str, start: datetime, end: datetime, idempotency_key: str | None = None
    ) -> str:
        check_hold(subject, start, end, self.clock.now_utc())
        if idempotency_key:
            for raw in self.raw:
                if raw.get("transactionId") == idempotency_key:
                    return str(raw["id"])  # Graph's transactionId: a retry returns the same event
        self._record("add_hold", subject=subject, start=start.isoformat(), end=end.isoformat())
        event_id = f"HOLD-{new_ulid()}"
        self.raw.append(
            {
                "id": event_id,
                "subject": subject,
                "sensitivity": "normal",
                "showAs": "tentative",
                "start": {"dateTime": start.astimezone(UTC).isoformat(), "timeZone": "UTC"},
                "end": {"dateTime": end.astimezone(UTC).isoformat(), "timeZone": "UTC"},
                "organizer": None,
                "onlineMeeting": None,
                "categories": ["Agent hold"],
                "transactionId": idempotency_key,
                "agentHold": True,
                "attendees": [],
            }
        )
        return event_id

    async def get_event(self, event_id: str) -> CalendarEvent | None:
        self._check("get_event")
        raw = next((r for r in self.raw if r["id"] == event_id), None)
        return None if raw is None else self._to_event(raw)

    async def discard_hold(self, event_id: str) -> None:
        raw = next((r for r in self.raw if r["id"] == event_id), None)
        if raw is None:
            return
        if not raw.get("agentHold"):
            raise AdapterError("this event was not created by the agent; it is left alone")
        if raw.get("attendees"):
            raise AdapterError(
                "people were invited to this hold since; removing it would cancel their meeting"
            )
        self._record("discard_hold", event_id=event_id)
        self.raw.remove(raw)

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
            organizer=_person(raw["organizer"]) if raw.get("organizer") and not private else None,
            join_url=None if private else meeting.get("joinUrl"),
            cancelled=bool(raw.get("isCancelled")),
            attendees=len(raw.get("attendees") or []),
            categories=[] if private else list(raw.get("categories") or []),
        )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="read_calendar", mode="silent", description="Read events"),
            ToolSpec(name="add_hold", mode="report", description="Add a tentative hold"),
        ]
