"""Adapter base protocol, typed method protocols and the models they exchange (A01-A11).

Real adapters and their fakes implement the same protocols. Write methods are only ever called
through the policy gate and outbox (golden rule 1).
"""

from datetime import date, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

Mode = Literal["silent", "report", "ask", "hand_off"]
HealthState = Literal["ok", "degraded", "down"]


class HealthStatus(BaseModel):
    connector: str
    status: HealthState
    detail: str = ""


class ToolSpec(BaseModel):
    """A tool an adapter offers. The mode here is a default; policy.yaml has the final say."""

    name: str
    mode: Mode
    description: str


class Adapter(Protocol):
    name: str

    async def health(self) -> HealthStatus: ...
    def tools(self) -> list[ToolSpec]: ...


# ------------------------------------------------------------------ models


class Person(BaseModel):
    name: str
    address: str


class Attachment(BaseModel):
    name: str
    content_type: str
    data: bytes


class AttachmentInfo(BaseModel):
    """Attachment metadata only; the bytes are fetched on demand (get_attachments)."""

    name: str
    content_type: str
    size: int = 0
    inline: bool = False


class MailMessage(BaseModel):
    id: str
    conversation_id: str
    subject: str
    sender: Person
    to: list[Person] = Field(default_factory=list)
    cc: list[Person] = Field(default_factory=list)
    received_at: datetime
    body: str
    has_attachments: bool = False
    categories: list[str] = Field(default_factory=list)
    importance: str = "normal"
    folder: str = "Inbox"  # Inbox | SentItems | Drafts | a user folder path
    flagged: bool = False
    is_draft: bool = False
    attachments: list[AttachmentInfo] = Field(default_factory=list)
    sensitivity: str = "normal"  # normal | personal | private | confidential
    # Labelled, encrypted or signed-and-sealed mail: body blanked, never sent to the model (A01).
    metadata_only: bool = False


class MailChanges(BaseModel):
    """One delta poll of a folder. commit_token is stored only after every item was ingested."""

    folder: str
    messages: list[MailMessage]
    commit_token: str | None
    resynced: bool = False  # the old token had expired: a 7-day resync, duplicates are expected


class CalendarEvent(BaseModel):
    id: str
    subject: str
    start: datetime
    end: datetime
    private: bool = False
    show_as: str = "busy"
    organizer: Person | None = None
    join_url: str | None = None
    all_day: bool = False
    cancelled: bool = False
    response: str | None = None  # accepted | tentativelyAccepted | declined | organizer | none
    attendees: int = 0
    categories: list[str] = Field(default_factory=list)
    series_id: str | None = None  # occurrences and exceptions of one recurring meeting share it


class ChatMessage(BaseModel):
    id: str
    chat_id: str
    chat_topic: str | None
    sender: str
    sent_at: datetime
    edited_at: datetime | None = None
    deleted: bool = False
    text: str
    mentions_me: bool = False
    sender_id: str | None = None
    from_me: bool = False


class SlackEvent(BaseModel):
    kind: Literal["dm", "thread_reply", "button", "command"]
    user: str
    text: str = ""
    thread_ts: str | None = None
    action_id: str | None = None
    value: str | None = None
    at: datetime


class ReportCandidate(BaseModel):
    report_id: str
    name: str
    dataset_id: str | None
    group_id: str | None = None


class PowerBILink(BaseModel):
    group_id: str | None = None
    report_id: str | None = None
    app_id: str | None = None
    dashboard_id: str | None = None
    dataset_id: str | None = None
    report_ids: list[str] = Field(default_factory=list)  # an app can bundle several reports
    candidates: list[ReportCandidate] = Field(default_factory=list)

    @property
    def needs_choice(self) -> bool:
        """An app or dashboard with several datasets behind it: list them and ask (A05)."""
        return self.dataset_id is None and len({c.dataset_id for c in self.candidates}) > 1


class Refresh(BaseModel):
    request_id: str
    status: Literal["Completed", "Failed", "Unknown", "Disabled", "Cancelled"]  # Unknown = running
    refresh_type: str
    start: datetime
    end: datetime | None
    error_code: str | None = None


class Dataflow(BaseModel):
    id: str
    name: str
    last_status: str | None
    last_end: datetime | None


class DataflowTransaction(BaseModel):
    id: str
    status: str  # InProgress | Success | Failed | Cancelled ...
    refresh_type: str = ""
    start: datetime | None = None
    end: datetime | None = None


class RefreshCap(BaseModel):
    """Today's refreshes against the shared-capacity cap. The service counts per UTC day."""

    used: int
    cap: int | None  # None on dedicated capacity: no daily cap for API refreshes
    day: date  # the UTC day counted
    resets_at: datetime

    @property
    def remaining(self) -> int | None:
        return None if self.cap is None else max(0, self.cap - self.used)

    @property
    def at_cap(self) -> bool:
        return self.cap is not None and self.used >= self.cap

    @property
    def last_one(self) -> bool:
        return self.cap is not None and self.used == self.cap - 1


class RefreshTrigger(BaseModel):
    """What trigger_refresh did. 'already_running' means no new refresh was started (E32)."""

    status: Literal["started", "already_running"]
    request_id: str | None
    warning: str | None = None


class RefreshSchedule(BaseModel):
    enabled: bool
    days: list[str] = Field(default_factory=list)
    times: list[str] = Field(default_factory=list)  # HH:MM in local_time_zone
    local_time_zone: str = "UTC"
    notify_option: str = "MailOnFailure"


class TranscriptResult(BaseModel):
    available: bool
    text: str = ""
    reason: str = ""  # why not, in plain words


class Group(BaseModel):
    id: str
    name: str
    dedicated_capacity: bool
    capacity_sku: str | None = None


class AssignmentNotice(BaseModel):
    number: str
    short_description: str
    caller: str
    site: str | None
    priority: str
    opened_at: datetime


class TicketRow(BaseModel):
    number: str
    short_description: str
    state: str
    priority: str
    opened: datetime
    updated: datetime
    assigned_to: str
    assignment_group: str
    caller: str


class OcrLine(BaseModel):
    text: str
    box: tuple[int, int, int, int]


class OcrResult(BaseModel):
    lines: list[OcrLine]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


# ------------------------------------------------------------------ method protocols


class MailAdapter(Adapter, Protocol):
    async def list_new(self, since: datetime) -> list[MailMessage]: ...
    async def get_message(self, message_id: str) -> MailMessage: ...
    async def get_thread(self, conversation_id: str) -> list[MailMessage]: ...
    async def search(self, query: str, top: int = 25) -> list[MailMessage]: ...
    async def get_attachments(self, message_id: str) -> list[Attachment]: ...
    async def get_folder(self, message_id: str) -> str: ...
    async def move(self, message_id: str, folder: str) -> str: ...
    async def categorise(self, message_id: str, categories: list[str]) -> None: ...
    async def flag(self, message_id: str) -> None: ...
    async def unflag(self, message_id: str) -> None: ...
    async def create_draft(
        self, to: list[str], subject: str, body: str, cc: list[str] | None = None, reply_to: str | None = None
    ) -> str: ...
    async def discard_draft(self, draft_id: str) -> None: ...
    async def send(
        self,
        draft_id: str,
        idempotency_key: str,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        body: str | None = None,
    ) -> str: ...
    async def find_sent(self, idempotency_key: str) -> str | None: ...


class CalendarAdapter(Adapter, Protocol):
    async def events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...
    async def add_hold(
        self, subject: str, start: datetime, end: datetime, idempotency_key: str | None = None
    ) -> str: ...
    async def get_event(self, event_id: str) -> CalendarEvent | None: ...
    async def discard_hold(self, event_id: str) -> None: ...


class TeamsAdapter(Adapter, Protocol):
    async def list_chat_messages(self, since: datetime) -> list[ChatMessage]: ...
    async def chat_members(self, chat_id: str) -> list[str]: ...
    async def post(self, chat_id: str, body: str, tag: str, recipients: list[str] | None = None) -> str: ...
    async def find_tagged(self, chat_id: str, tag: str) -> str | None: ...
    async def get_transcript(self, meeting_id: str) -> str | None: ...
    async def transcript(self, meeting_id: str) -> TranscriptResult: ...


class SlackAdapter(Adapter, Protocol):
    async def send_dm(self, text: str, blocks: list[dict[str, Any]] | None = None) -> str: ...
    async def post_thread(self, thread_ts: str, text: str) -> str: ...
    async def next_events(self) -> list[SlackEvent]: ...


class PowerBIAdapter(Adapter, Protocol):
    async def groups(self) -> list[Group]: ...
    async def resolve_link(self, url: str) -> PowerBILink: ...
    async def refresh_history(self, dataset_id: str, top: int = 60) -> list[Refresh]: ...
    async def running_refresh(self, dataset_id: str) -> Refresh | None: ...
    async def refreshes_today(self, dataset_id: str, day: date) -> int: ...
    async def refresh_cap(self, dataset_id: str) -> RefreshCap: ...
    async def upstream_dataflows(self, dataset_id: str) -> list[Dataflow]: ...
    async def dataflow_transactions(self, dataflow_id: str, top: int = 10) -> list[DataflowTransaction]: ...
    async def refresh_schedule(self, dataset_id: str) -> RefreshSchedule: ...
    async def dataset_owner(self, dataset_id: str) -> str | None: ...
    async def trigger_refresh(self, dataset_id: str) -> RefreshTrigger: ...
    async def trigger_dataflow_refresh(self, dataflow_id: str) -> RefreshTrigger: ...
    async def update_refresh_schedule(self, dataset_id: str, schedule: RefreshSchedule) -> None: ...


class SharePointAdapter(Adapter, Protocol):
    async def list_items(self, list_id: str, since: datetime | None = None) -> list[dict[str, Any]]: ...


class ServiceNowEmailAdapter(Adapter, Protocol):
    def parse_assignment(self, mail: MailMessage) -> AssignmentNotice | None: ...
    def parse_report(self, attachment: Attachment) -> list[TicketRow]: ...
    async def list_open_tickets(self) -> list[TicketRow]: ...


class SqlAdapter(Adapter, Protocol):
    async def count(self, table: str) -> int: ...
    async def max_date(self, table: str) -> date | None: ...


class OcrAdapter(Adapter, Protocol):
    async def read(self, image: bytes) -> OcrResult: ...
