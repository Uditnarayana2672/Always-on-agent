"""Pydantic models for every console API body and response (F15). Shapes follow the console pages."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["Silent", "Report", "Ask", "Hand off", "Blocked"]
Column = Literal["needs", "progress", "waiting", "scheduled", "done"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- shell


class StatusOut(BaseModel):
    paused: bool
    leave: bool
    model: str  # "Haiku"
    needs_you: int
    approvals: int
    phase: str = "Phase 1 · on your laptop"
    fake: bool = False


# ---------------------------------------------------------------- brief


class NeedLine(BaseModel):
    id: str
    title: str
    project: str
    reason: str


class ClockOut(BaseModel):
    id: str
    title: str
    age_hours: float
    paused: bool = False
    estimated: bool = False


class StatOut(BaseModel):
    value: str
    label: str


class PowerBIItem(BaseModel):
    title: str
    meta: str
    task_id: str | None = None
    severity: Literal["alert", "info"] = "info"


class DoneOut(BaseModel):
    audit_id: int
    text: str
    time: str
    ref: str
    undo: Literal["available", "done", "none"]


class MeetingOut(BaseModel):
    start: str
    end: str
    subject: str
    join_url: str | None = None


class BriefOut(BaseModel):
    day_label: str  # "Wed 25 Mar · 09:00 IST brief"
    greeting: str  # "Good morning, Udit. 3 things need you."
    paused: bool
    last_checkin: str | None
    next_checkin: str | None
    open_tasks: int
    approvals: int
    model_line: str
    needs: list[NeedLine]
    clocks: list[ClockOut]
    thresholds: list[int]
    powerbi_stats: list[StatOut]
    powerbi_items: list[PowerBIItem]
    done: list[DoneOut]
    meetings: list[MeetingOut]
    meetings_note: str | None = None  # "Calendar could not be read" etc.
    latest_text: str | None = None  # the last brief sent to Slack, as text
    connectors_down: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- tasks


class TaskCardOut(BaseModel):
    id: str
    project: str
    title: str
    column: Column
    state: str
    age_hours: float | None
    clock: bool  # an open Doxis ticket with a 24-hour clock
    next: str
    has_approval: bool
    recipe: str | None = None


class EventOut(BaseModel):
    time: str
    who: Literal["agent", "you", "system", "caller"]
    name: str
    text: str


class CheckOut(BaseModel):
    label: str
    status: Literal["pass", "fail", "unknown"]
    detail: str


class PrecedentOut(BaseModel):
    id: str
    text: str


class ApprovalOut(BaseModel):
    id: str
    task_id: str | None
    task_title: str | None
    kind: str  # "Email", "Teams message", "Power BI refresh"
    to: str
    via: str
    recipe: str | None
    text: str
    content_hash: str
    status: str
    edited: bool
    expires_at: datetime | None
    expires_label: str
    urgent: bool
    note: str | None = None


class TaskDetailOut(BaseModel):
    task: TaskCardOut
    kind: str
    recipe_label: str
    caller: str
    affected: str
    site: str
    priority: str
    summary: str | None
    checks: list[CheckOut]
    precedents: list[PrecedentOut]
    events: list[EventOut]
    approval: ApprovalOut | None


class MessageIn(_In):
    text: str = Field(min_length=1, max_length=4000)


class ApproveIn(_In):
    content_hash: str = Field(min_length=8)


class EditIn(_In):
    text: str = Field(min_length=1, max_length=20000)
    content_hash: str | None = None


class RejectIn(_In):
    reason: str = Field(default="", max_length=500)


class DecisionOut(BaseModel):
    id: str
    status: str
    content_hash: str
    changed: bool
    message: str


# ---------------------------------------------------------------- recipes


class ToolOut(BaseModel):
    name: str
    mode: Mode
    locked: bool


class RecipeOut(BaseModel):
    slug: str
    name: str
    project: Literal["Core", "Doxis", "Power BI"]
    trigger: str  # "schedule: brief"
    trigger_label: str  # "Schedule · morning brief"
    model: str
    status: Literal["Live", "Shadow", "Off"]
    last_run: str
    tools: list[str]
    instructions: str
    output: str  # approval_card | digest | dm | report | none
    output_label: str
    has_steps: bool
    file: str
    error: str | None = None
    runs: int = 0
    would_do: int = 0


class RecipeIn(_In):
    name: str = Field(min_length=1, max_length=80)
    project: Literal["Core", "Doxis", "Power BI"]
    trigger: str = Field(min_length=3, max_length=300)  # "schedule: 09:00", "poll: mail, every: 10m"
    model: Literal["Haiku", "Sonnet", "Opus", "None"] = "Haiku"
    status: Literal["Live", "Shadow", "Off"] | None = None
    tools: list[str] = Field(default_factory=list, max_length=40)
    instructions: str = Field(default="", max_length=4000)
    output: Literal["approval_card", "digest", "dm", "report", "none"] = "dm"


class RecipeStatusIn(_In):
    status: Literal["Live", "Shadow", "Off"]


class ProposalOut(BaseModel):
    tool: str
    mode: Mode
    note: str


class RecipeTestOut(BaseModel):
    sample: str
    proposals: list[ProposalOut]
    notes: list[str]
    needs_approval: bool


class RecipeToolsOut(BaseModel):
    tools: list[ToolOut]


# ---------------------------------------------------------------- activity


class ActivityOut(BaseModel):
    audit_id: int
    time: str
    at: datetime
    ref: str
    action: str
    tool: str
    mode: Mode
    result: str
    undo: Literal["available", "done", "none"]


class WorkerOut(BaseModel):
    name: str
    busy: bool
    doing: str
    note: str


class WorkersOut(BaseModel):
    coordinator: str
    queue: int
    workers: list[WorkerOut]


class UndoOut(BaseModel):
    status: str
    message: str


# ---------------------------------------------------------------- memory, connectors, settings


class DirectoryRow(BaseModel):
    site: str
    name: str
    km: str
    lba: str
    last_checked: str
    stale: bool
    source: str


class LessonOut(BaseModel):
    id: str
    text: str
    source: str
    status: str


class LessonIn(_In):
    action: Literal["approve", "discard"]


class SourceOut(BaseModel):
    source: str
    items: int
    last_indexed: str


class MemoryOut(BaseModel):
    section: str
    profile: str | None = None
    preferences: str | None = None
    directory: list[DirectoryRow] = Field(default_factory=list)
    lessons: list[LessonOut] = Field(default_factory=list)
    sources: list[SourceOut] = Field(default_factory=list)


class ConnectorOut(BaseModel):
    name: str
    status: Literal["ok", "part", "pend", "no"]
    status_label: str
    via: str
    can: list[str]
    scopes: str
    detail: str | None = None


class PolicyRow(BaseModel):
    key: str  # a row of the Settings table: one plain action over one or more tools
    label: str
    mode: Mode
    options: list[Mode]  # what the select offers; Hand off rows offer nothing else
    locked: bool
    reaches_people: bool  # never below Ask
    tools: list[str]


class SettingsOut(BaseModel):
    work_start: str
    work_end: str
    brief: str
    nightly_digest: str
    checkin_every_minutes: int
    max_pushes_per_day: int
    quiet_start: str
    quiet_end: str
    default_model: str
    sonnet_for: list[str]
    sonnet_daily_limit: int
    data_rule: str
    data_rule_approved: bool
    policy: list[PolicyRow]


class SettingsIn(_In):
    """Only what the console may change. Never the data rule level (it needs the client's approval)."""

    work_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    work_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    brief: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    nightly_digest: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    checkin_every_minutes: int | None = Field(default=None, ge=30, le=480)
    max_pushes_per_day: int | None = Field(default=None, ge=0, le=50)
    sonnet_for: list[Literal["caller_drafts", "long_transcripts", "weekly_reports"]] | None = None
    sonnet_daily_limit: int | None = Field(default=None, ge=0, le=500)
    policy: dict[str, Literal["Silent", "Report", "Ask"]] | None = None


class ControlOut(BaseModel):
    paused: bool
    message: str


class ResetTokenOut(BaseModel):
    token: str
    expires_minutes: int = 10


class ResetIn(_In):
    token: str = Field(min_length=8)
