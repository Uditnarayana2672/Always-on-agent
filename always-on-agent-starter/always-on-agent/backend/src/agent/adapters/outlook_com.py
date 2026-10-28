"""Outlook mail and calendar through classic Outlook desktop COM automation (A01, A02 fallback).

Works without an app registration: it uses the classic Outlook (OUTLOOK.EXE) session already signed
in on the laptop. The new Outlook (olk.exe) has no COM interface, so classic Outlook must be running.

COM objects belong to the thread that created them, so every call runs on one dedicated thread.
Message bodies are never logged.
"""

import asyncio
import logging
import re
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.adapters.base import (
    Attachment,
    AttachmentInfo,
    CalendarEvent,
    HealthStatus,
    MailMessage,
    Person,
    ToolSpec,
)
from agent.adapters.outlook_graph import digest_text, label_names
from agent.core.errors import AdapterError

log = logging.getLogger(__name__)

# Outlook enumerations
OL_FOLDER_INBOX = 6
OL_FOLDER_SENT = 5
OL_FOLDER_CALENDAR = 9
OL_MAIL_ITEM = 0
OL_APPOINTMENT_ITEM = 1
OL_CLASS_MAIL = 43
OL_SENSITIVITY_PRIVATE = 2
OL_BUSY_TENTATIVE = 1
OL_RECIPIENT_TO, OL_RECIPIENT_CC = 1, 2
PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
THREAD_SCAN_LIMIT = 500
OL_FOLDER_OUTBOX = 4
OL_FLAG_MARKED = 2
_MAPI_STRING = "http://schemas.microsoft.com/mapi/string/"
IDEMPOTENCY_HEADER = _MAPI_STRING + "{00020386-0000-0000-C000-000000000046}/X-Agent-Idempotency"
AGENT_DRAFT = _MAPI_STRING + "{00020329-0000-0000-C000-000000000046}/AgentDraft"
BODY_DIGEST = _MAPI_STRING + "{00020329-0000-0000-C000-000000000046}/AgentBodyDigest"
TEXT_DIGEST = _MAPI_STRING + "{00020329-0000-0000-C000-000000000046}/AgentTextDigest"
REPLY_TO = _MAPI_STRING + "{00020329-0000-0000-C000-000000000046}/AgentReplyTo"
AGENT_HOLD = _MAPI_STRING + "{00020329-0000-0000-C000-000000000046}/AgentHold"
_TEAMS_LINK = re.compile(r"https://teams\.microsoft\.com/l/meetup-join/[^\s>\"]+")
_SHOW_AS = {0: "free", 1: "tentative", 2: "busy", 3: "oof", 4: "workingElsewhere"}


def com_time_to_utc(value: Any) -> datetime:
    """Outlook returns local wall-clock times; pywin32 may label them UTC. Read the wall clock as local."""
    naive = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second)
    return naive.astimezone().astimezone(UTC)


def utc_to_com_local(value: datetime) -> datetime:
    """Aware datetime to the naive local wall-clock time Outlook expects."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone().replace(tzinfo=None)


def dasl_received_after(since: datetime) -> str:
    """DASL filter on received time. DASL compares dates in UTC."""
    stamp = since.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    return f"@SQL=\"urn:schemas:httpmail:datereceived\" > '{stamp}'"


def dasl_calendar_overlap(start: datetime, end: datetime) -> str:
    fmt = "%Y-%m-%d %H:%M"
    s, e = start.astimezone(UTC).strftime(fmt), end.astimezone(UTC).strftime(fmt)
    return f"@SQL=\"urn:schemas:calendar:dtstart\" < '{e}' AND \"urn:schemas:calendar:dtend\" > '{s}'"


def smtp_of_sender(item: Any) -> str:
    """Exchange senders come as X.500 paths; resolve to SMTP when possible."""
    address = str(item.SenderEmailAddress or "")
    if getattr(item, "SenderEmailType", "SMTP") == "EX":
        try:
            user = item.Sender.GetExchangeUser()
            if user is not None and user.PrimarySmtpAddress:
                return str(user.PrimarySmtpAddress)
        except Exception:  # COM raises its own error types
            pass
    return address


def smtp_of_recipient(recipient: Any) -> str:
    try:
        return str(recipient.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS))
    except Exception:
        return str(recipient.Address or "")


_SENSITIVITY = {0: "normal", 1: "personal", 2: "private", 3: "confidential"}
LABELS_HEADER = _MAPI_STRING + "{00020386-0000-0000-C000-000000000046}/msip_labels"
OPEN_LABELS = frozenset({"public", "general"})


def is_protected(item: Any, sensitivity: str) -> bool:
    """Private/confidential, encrypted (IRM, S/MIME) or carrying a non-open sensitivity label."""
    if sensitivity in ("private", "confidential"):
        return True
    message_class = str(getattr(item, "MessageClass", "") or "")
    if message_class.startswith(("IPM.Note.SMIME", "IPM.Note.rpmsg", "IPM.Note.Microsoft.Rpmsg")):
        return True
    try:
        if int(getattr(item, "Permission", 0) or 0) != 0:  # IRM restricted
            return True
    except (TypeError, ValueError):
        return True
    names = label_names(_get_prop(item, LABELS_HEADER))
    return any(n.lower() not in OPEN_LABELS for n in names)


def to_mail(item: Any) -> MailMessage:
    to: list[Person] = []
    cc: list[Person] = []
    for recipient in item.Recipients:
        person = Person(name=str(recipient.Name), address=smtp_of_recipient(recipient).lower())
        (to if recipient.Type == OL_RECIPIENT_TO else cc if recipient.Type == OL_RECIPIENT_CC else []).append(
            person
        )
    categories = [c.strip() for c in str(item.Categories or "").split(",") if c.strip()]
    sensitivity = _SENSITIVITY.get(int(getattr(item, "Sensitivity", 0) or 0), "normal")
    protected = is_protected(item, sensitivity)
    attachments = []
    for att in item.Attachments:
        name = str(getattr(att, "FileName", "") or "")
        attachments.append(
            AttachmentInfo(
                name=name, content_type=_content_type(name), size=int(getattr(att, "Size", 0) or 0)
            )
        )
    return MailMessage(
        id=str(item.EntryID),
        conversation_id=str(item.ConversationID or item.EntryID),
        subject=str(item.Subject or ""),
        sender=Person(name=str(item.SenderName or ""), address=smtp_of_sender(item).lower()),
        to=to,
        cc=cc,
        received_at=com_time_to_utc(item.ReceivedTime),
        body="" if protected else str(item.Body or ""),
        has_attachments=item.Attachments.Count > 0,
        categories=categories,
        importance={0: "low", 1: "normal", 2: "high"}.get(int(item.Importance), "normal"),
        flagged=int(getattr(item, "FlagStatus", 0) or 0) == OL_FLAG_MARKED,
        is_draft=not bool(getattr(item, "Sent", True)),
        attachments=attachments,
        sensitivity=sensitivity,
        metadata_only=protected,
    )


def to_event(item: Any) -> CalendarEvent:
    private = int(item.Sensitivity) == OL_SENSITIVITY_PRIVATE
    link = _TEAMS_LINK.search(str(item.Body or "")) if not private else None
    organizer = str(item.Organizer or "")
    return CalendarEvent(
        id=str(item.EntryID),
        subject="(private)" if private else str(item.Subject or ""),
        start=com_time_to_utc(item.Start),
        end=com_time_to_utc(item.End),
        private=private,
        show_as=_SHOW_AS.get(int(item.BusyStatus), "busy"),
        organizer=Person(name=organizer, address="") if organizer else None,
        join_url=link.group(0) if link else None,
    )


def _default_connect() -> Any:
    import win32com.client

    return win32com.client.Dispatch("Outlook.Application")


def _com_init() -> None:
    import pythoncom

    pythoncom.CoInitialize()


class OutlookComAdapter:
    """Implements MailAdapter and CalendarAdapter over classic Outlook COM."""

    name = "outlook_com"

    def __init__(
        self,
        connect: Callable[[], Any] = _default_connect,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._connect = connect
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, initializer=_com_init, thread_name_prefix="outlook-com"
        )
        self._app: Any = None

    # -------------------------------------------------- plumbing

    async def _run[T](self, fn: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, fn)
        except AdapterError:
            raise
        except Exception as exc:  # pywintypes.com_error and friends
            self._app = None  # reconnect next time (Outlook may have been closed)
            raise AdapterError(
                f"Outlook COM: {type(exc).__name__}: {str(exc)[:200]}", retryable=True
            ) from exc

    def _ns(self) -> Any:
        if self._app is None:
            self._app = self._connect()
        return self._app.GetNamespace("MAPI")

    def _item(self, entry_id: str) -> Any:
        return self._ns().GetItemFromID(entry_id)

    def _folder(self, path: str) -> Any:
        """'Inbox/Done' resolves under the default mailbox root."""
        folder = self._ns().GetDefaultFolder(OL_FOLDER_INBOX).Parent
        for part in [p for p in path.split("/") if p]:
            try:
                folder = folder.Folders[part]
            except Exception as exc:
                raise AdapterError(f"Outlook folder not found: {path}") from exc
        return folder

    # -------------------------------------------------- Adapter

    async def health(self) -> HealthStatus:
        def check() -> HealthStatus:
            inbox = self._ns().GetDefaultFolder(OL_FOLDER_INBOX)
            return HealthStatus(
                connector=self.name, status="ok", detail=f"Inbox has {inbox.Items.Count} items"
            )

        try:
            return await self._run(check)
        except AdapterError as exc:
            return HealthStatus(
                connector=self.name,
                status="down",
                detail=f"{exc}. Start classic Outlook (OUTLOOK.EXE); the new Outlook has no COM.",
            )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="read_mail", mode="silent", description="Read mail"),
            ToolSpec(name="read_calendar", mode="silent", description="Read events"),
            ToolSpec(name="move_mail", mode="report", description="Move a message to a folder"),
            ToolSpec(name="categorise", mode="report", description="Set categories"),
            ToolSpec(name="flag_mail", mode="report", description="Flag a message"),
            ToolSpec(name="save_draft", mode="report", description="Save a draft"),
            ToolSpec(name="add_hold", mode="report", description="Add a tentative hold"),
            ToolSpec(name="send_mail", mode="ask", description="Send a draft"),
        ]

    # -------------------------------------------------- mail reads

    async def list_new(self, since: datetime) -> list[MailMessage]:
        def read() -> list[MailMessage]:
            items = self._ns().GetDefaultFolder(OL_FOLDER_INBOX).Items.Restrict(dasl_received_after(since))
            items.Sort("[ReceivedTime]")
            return [to_mail(i) for i in items if int(i.Class) == OL_CLASS_MAIL]

        return await self._run(read)

    async def get_thread(self, conversation_id: str) -> list[MailMessage]:
        def read() -> list[MailMessage]:
            found = []
            for folder_id in (OL_FOLDER_INBOX, OL_FOLDER_SENT):
                items = self._ns().GetDefaultFolder(folder_id).Items
                items.Sort("[ReceivedTime]", True)  # newest first, scan a bounded window
                for index, item in enumerate(items):
                    if index >= THREAD_SCAN_LIMIT:
                        break
                    if int(item.Class) == OL_CLASS_MAIL and str(item.ConversationID) == conversation_id:
                        found.append(to_mail(item))
            return sorted(found, key=lambda m: m.received_at)

        return await self._run(read)

    async def get_attachments(self, message_id: str) -> list[Attachment]:
        def read() -> list[Attachment]:
            out = []
            with tempfile.TemporaryDirectory(prefix="agent-att-") as tmp:
                for index, att in enumerate(self._item(message_id).Attachments):
                    name = str(att.FileName)
                    path = Path(tmp) / f"{index}-{re.sub(r'[^\\w.-]', '_', name)}"
                    att.SaveAsFile(str(path))
                    out.append(
                        Attachment(name=name, content_type=_content_type(name), data=path.read_bytes())
                    )
            return out

        return await self._run(read)

    # -------------------------------------------------- more reads

    async def get_message(self, message_id: str) -> MailMessage:
        return await self._run(lambda: to_mail(self._item(message_id)))

    async def search(self, query: str, top: int = 25) -> list[MailMessage]:
        cleaned = query.replace("'", "''").replace('"', " ").strip()
        if not cleaned:
            return []

        def read() -> list[MailMessage]:
            flt = (
                f"@SQL=\"urn:schemas:httpmail:subject\" ci_phrasematch '{cleaned}' OR "
                f"\"urn:schemas:httpmail:textdescription\" ci_phrasematch '{cleaned}'"
            )
            items = self._ns().GetDefaultFolder(OL_FOLDER_INBOX).Items.Restrict(flt)
            items.Sort("[ReceivedTime]", True)
            out = []
            for item in items:
                if int(item.Class) == OL_CLASS_MAIL:
                    out.append(to_mail(item))
                if len(out) >= top:
                    break
            return out

        return await self._run(read)

    async def get_folder(self, message_id: str) -> str:
        return await self._run(lambda: folder_path(str(self._item(message_id).Parent.FolderPath)))

    async def get_categories(self, message_id: str) -> list[str]:
        def read() -> list[str]:
            raw = str(self._item(message_id).Categories or "")
            return [c.strip() for c in raw.split(",") if c.strip()]

        return await self._run(read)

    async def is_flagged(self, message_id: str) -> bool:
        return await self._run(lambda: int(self._item(message_id).FlagStatus or 0) == OL_FLAG_MARKED)

    # -------------------------------------------------- mail writes (only via the policy gate)

    async def move(self, message_id: str, folder: str) -> str:
        def write() -> str:
            if folder.strip().lower().replace(" ", "") in ("deleteditems", "inbox/deleteditems"):
                raise AdapterError("moving to Deleted Items is deleting; that is a hand-off")
            moved = self._item(message_id).Move(self._folder(folder))
            return str(getattr(moved, "EntryID", None) or message_id)

        return await self._run(write)

    async def categorise(self, message_id: str, categories: list[str]) -> None:
        def write() -> None:
            item = self._item(message_id)
            item.Categories = ", ".join(categories)
            item.Save()

        await self._run(write)

    async def flag(self, message_id: str) -> None:
        await self.set_flag(message_id, True)

    async def unflag(self, message_id: str) -> None:
        await self.set_flag(message_id, False)

    async def set_flag(self, message_id: str, flagged: bool) -> None:
        def write() -> None:
            item = self._item(message_id)
            if flagged:
                item.MarkAsTask(0)  # olMarkToday
            else:
                item.ClearTaskFlag()
            item.Save()

        await self._run(write)

    async def create_draft(
        self, to: list[str], subject: str, body: str, cc: list[str] | None = None, reply_to: str | None = None
    ) -> str:
        def write() -> str:
            if reply_to:
                draft = self._item(reply_to).Reply()
                draft.Body = body + "\n\n" + str(draft.Body or "")
            else:
                if self._app is None:
                    self._ns()
                draft = self._app.CreateItem(OL_MAIL_ITEM)
                draft.Subject = subject
                draft.Body = body
            draft.To = "; ".join(to)
            draft.CC = "; ".join(cc or [])
            _set_prop(draft, AGENT_DRAFT, "1")
            _set_prop(draft, BODY_DIGEST, digest_text(str(draft.Body or "")))
            _set_prop(draft, TEXT_DIGEST, digest_text(body))
            _set_prop(draft, REPLY_TO, reply_to or "")
            draft.Save()
            return str(draft.EntryID)

        return await self._run(write)

    async def discard_draft(self, draft_id: str) -> None:
        """The save_draft inverse: deletes only an unsent draft the agent created."""

        def write() -> None:
            try:
                draft = self._item(draft_id)
            except Exception:
                return  # already gone
            if getattr(draft, "Sent", False):
                raise AdapterError("this message was sent; it cannot be discarded")
            if _get_prop(draft, AGENT_DRAFT) != "1":
                raise AdapterError("this draft was not created by the agent; it is left alone")
            draft.Delete()

        await self._run(write)

    async def send(
        self,
        draft_id: str,
        idempotency_key: str,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body: str | None = None,
    ) -> str:
        """Send a saved draft. A missing draft is only treated as sent when Sent Items shows it (E18)."""

        def rebuild_needed() -> tuple[bool, str, str | None]:
            try:
                draft = self._item(draft_id)
            except Exception:
                return False, "", None
            saved = _get_prop(draft, TEXT_DIGEST)
            changed = bool(body) and saved is not None and saved != digest_text(body or "")
            return changed, str(draft.Subject or ""), _get_prop(draft, REPLY_TO)

        target = draft_id
        changed, subject, reply_to = await self._run(rebuild_needed)
        if changed and body:
            target = await self.create_draft(to or [], subject, body, cc, reply_to or None)
            await self.discard_draft(draft_id)

        def write() -> str | None:
            try:
                draft = self._item(target)
            except Exception:
                return None
            if getattr(draft, "Sent", False):
                return None
            if to is not None:
                actual = _split(draft.To), _split(draft.CC)
                approved = sorted(a.lower() for a in to), sorted(a.lower() for a in cc or [])
                if actual != approved:
                    raise AdapterError(
                        "the draft's recipients changed in Outlook after approval; approve it again"
                    )
            expected = _get_prop(draft, BODY_DIGEST)
            if expected and digest_text(str(draft.Body or "")) != expected:
                raise AdapterError("the draft was edited in Outlook after approval; approve the new text")
            _set_prop(draft, IDEMPOTENCY_HEADER, idempotency_key)
            draft.Send()
            return target

        sent = await self._run(write)
        if sent is not None:
            return sent
        found = await self.find_sent(idempotency_key)
        if found is None:
            raise AdapterError("the draft no longer exists and nothing was sent; make a new draft")
        return found

    async def find_sent(self, idempotency_key: str) -> str | None:
        """Sent Items and the Outbox, newest first, by the X-Agent-Idempotency header property."""

        def read() -> str | None:
            for folder_id in (OL_FOLDER_SENT, OL_FOLDER_OUTBOX):
                try:
                    items = self._ns().GetDefaultFolder(folder_id).Items
                except Exception:
                    continue
                items.Sort("[CreationTime]", True)
                for index, item in enumerate(items):
                    if index >= THREAD_SCAN_LIMIT:
                        break
                    if _get_prop(item, IDEMPOTENCY_HEADER) == idempotency_key:
                        return str(item.EntryID)
            return None

        return await self._run(read)

    # -------------------------------------------------- calendar

    async def events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        def read() -> list[CalendarEvent]:
            items = self._ns().GetDefaultFolder(OL_FOLDER_CALENDAR).Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            return [to_event(i) for i in items.Restrict(dasl_calendar_overlap(start, end))]

        return await self._run(read)

    async def get_event(self, event_id: str) -> CalendarEvent | None:
        def read() -> CalendarEvent | None:
            try:
                return to_event(self._item(event_id))
            except AdapterError:
                raise
            except Exception:
                return None

        return await self._run(read)

    async def add_hold(
        self, subject: str, start: datetime, end: datetime, idempotency_key: str | None = None
    ) -> str:
        from agent.adapters.calendar import check_hold

        check_hold(subject, start, end, datetime.now(UTC))

        def write() -> str:
            if idempotency_key:
                existing = self._find_hold(idempotency_key)
                if existing is not None:
                    return existing
            if self._app is None:
                self._ns()
            item = self._app.CreateItem(OL_APPOINTMENT_ITEM)
            item.Subject = subject
            item.Start = utc_to_com_local(start)
            item.End = utc_to_com_local(end)
            item.BusyStatus = OL_BUSY_TENTATIVE
            item.ReminderSet = False
            item.Categories = "Agent hold"
            _set_prop(item, AGENT_HOLD, idempotency_key or "1")
            item.Save()
            return str(item.EntryID)

        return await self._run(write)

    def _find_hold(self, key: str) -> str | None:
        items = self._ns().GetDefaultFolder(OL_FOLDER_CALENDAR).Items
        for item in items.Restrict("[Categories] = 'Agent hold'"):
            if _get_prop(item, AGENT_HOLD) == key:
                return str(item.EntryID)
        return None

    async def discard_hold(self, event_id: str) -> None:
        """The add_hold inverse: removes only an agent hold nobody was invited to."""

        def write() -> None:
            try:
                item = self._item(event_id)
            except Exception:
                return
            if not _get_prop(item, AGENT_HOLD):
                raise AdapterError("this event was not created by the agent; it is left alone")
            if int(getattr(item.Recipients, "Count", 0) or 0) > 0:
                raise AdapterError(
                    "people were invited to this hold since; removing it would cancel their meeting"
                )
            item.Delete()

        await self._run(write)


def folder_path(raw: str) -> str:
    """'\\\\user@corp.example\\Inbox\\Done' -> 'Inbox/Done'."""
    parts = [p for p in raw.replace("/", "\\").split("\\") if p]
    return "/".join(parts[1:]) if len(parts) > 1 else (parts[0] if parts else "")


def _split(value: Any) -> list[str]:
    return sorted(a.strip().lower() for a in str(value or "").split(";") if a.strip())


def _get_prop(item: Any, path: str) -> str | None:
    try:
        value = item.PropertyAccessor.GetProperty(path)
    except Exception:  # property not set
        return None
    return str(value) if value not in (None, "") else None


def _set_prop(item: Any, path: str, value: str) -> None:
    item.PropertyAccessor.SetProperty(path, value)


def _content_type(name: str) -> str:
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "pdf": "application/pdf",
        "txt": "text/plain",
    }.get(suffix, "application/octet-stream")
