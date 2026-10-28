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

from agent.adapters.base import Attachment, CalendarEvent, HealthStatus, MailMessage, Person, ToolSpec
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


def to_mail(item: Any) -> MailMessage:
    to: list[Person] = []
    cc: list[Person] = []
    for recipient in item.Recipients:
        person = Person(name=str(recipient.Name), address=smtp_of_recipient(recipient))
        (to if recipient.Type == OL_RECIPIENT_TO else cc if recipient.Type == OL_RECIPIENT_CC else []).append(
            person
        )
    categories = [c.strip() for c in str(item.Categories or "").split(",") if c.strip()]
    return MailMessage(
        id=str(item.EntryID),
        conversation_id=str(item.ConversationID or item.EntryID),
        subject=str(item.Subject or ""),
        sender=Person(name=str(item.SenderName or ""), address=smtp_of_sender(item)),
        to=to,
        cc=cc,
        received_at=com_time_to_utc(item.ReceivedTime),
        body=str(item.Body or ""),
        has_attachments=item.Attachments.Count > 0,
        categories=categories,
        importance={0: "low", 1: "normal", 2: "high"}.get(int(item.Importance), "normal"),
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

    # -------------------------------------------------- mail writes (only via the policy gate)

    async def move(self, message_id: str, folder: str) -> None:
        await self._run(lambda: self._item(message_id).Move(self._folder(folder)))

    async def categorise(self, message_id: str, categories: list[str]) -> None:
        def write() -> None:
            item = self._item(message_id)
            item.Categories = ", ".join(categories)
            item.Save()

        await self._run(write)

    async def flag(self, message_id: str) -> None:
        def write() -> None:
            item = self._item(message_id)
            item.MarkAsTask(0)  # olMarkToday
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
            draft.Save()
            return str(draft.EntryID)

        return await self._run(write)

    async def send(self, draft_id: str, idempotency_key: str) -> None:
        """Send a saved draft. If the draft no longer exists it was already sent; do nothing.

        Exactly-once across restarts is the outbox's job (prompt 1.3); this guard covers a retry.
        """

        def write() -> None:
            try:
                draft = self._item(draft_id)
            except Exception:
                log.info("Draft already gone, treating as sent", extra={"idempotency_key": idempotency_key})
                return
            draft.Send()

        await self._run(write)

    # -------------------------------------------------- calendar

    async def events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        def read() -> list[CalendarEvent]:
            items = self._ns().GetDefaultFolder(OL_FOLDER_CALENDAR).Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            return [to_event(i) for i in items.Restrict(dasl_calendar_overlap(start, end))]

        return await self._run(read)

    async def add_hold(self, subject: str, start: datetime, end: datetime) -> str:
        def write() -> str:
            if self._app is None:
                self._ns()
            item = self._app.CreateItem(OL_APPOINTMENT_ITEM)
            item.Subject = subject
            item.Start = utc_to_com_local(start)
            item.End = utc_to_com_local(end)
            item.BusyStatus = OL_BUSY_TENTATIVE
            item.ReminderSet = False
            item.Save()
            return str(item.EntryID)

        return await self._run(write)


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
