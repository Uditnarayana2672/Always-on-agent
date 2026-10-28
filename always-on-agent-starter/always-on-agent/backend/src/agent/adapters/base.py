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


class CalendarEvent(BaseModel):
    id: str
    subject: str
    start: datetime
    end: datetime
    private: bool = False
    show_as: str = "busy"
    organizer: Person | None = None
    join_url: str | None = None


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


class SlackEvent(BaseModel):
    kind: Literal["dm", "thread_reply", "button", "command"]
    user: str
    text: str = ""
    thread_ts: str | None = None
    action_id: str | None = None
    value: str | None = None
    at: datetime


class PowerBILink(BaseModel):
    group_id: str | None = None
    report_id: str | None = None
    app_id: str | None = None
    dataset_id: str | None = None
    report_ids: list[str] = Field(default_factory=list)  # an app can bundle several reports


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
    async def get_thread(self, conversation_id: str) -> list[MailMessage]: ...
    async def get_attachments(self, message_id: str) -> list[Attachment]: ...
    async def move(self, message_id: str, folder: str) -> None: ...
    async def categorise(self, message_id: str, categories: list[str]) -> None: ...
    async def flag(self, message_id: str) -> None: ...
    async def create_draft(
        self, to: list[str], subject: str, body: str, cc: list[str] | None = None, reply_to: str | None = None
    ) -> str: ...
    async def send(self, draft_id: str, idempotency_key: str) -> None: ...


class CalendarAdapter(Adapter, Protocol):
    async def events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...
    async def add_hold(self, subject: str, start: datetime, end: datetime) -> str: ...


class TeamsAdapter(Adapter, Protocol):
    async def list_chat_messages(self, since: datetime) -> list[ChatMessage]: ...
    async def post(self, chat_id: str, body: str, tag: str) -> str: ...
    async def get_transcript(self, meeting_id: str) -> str | None: ...


class SlackAdapter(Adapter, Protocol):
    async def send_dm(self, text: str, blocks: list[dict[str, Any]] | None = None) -> str: ...
    async def post_thread(self, thread_ts: str, text: str) -> str: ...
    async def next_events(self) -> list[SlackEvent]: ...


class PowerBIAdapter(Adapter, Protocol):
    async def groups(self) -> list[Group]: ...
    async def resolve_link(self, url: str) -> PowerBILink: ...
    async def refresh_history(self, dataset_id: str, top: int = 10) -> list[Refresh]: ...
    async def refreshes_today(self, dataset_id: str, day: date) -> int: ...
    async def upstream_dataflows(self, dataset_id: str) -> list[Dataflow]: ...
    async def trigger_refresh(self, dataset_id: str) -> str: ...
    async def trigger_dataflow_refresh(self, dataflow_id: str) -> str: ...


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
