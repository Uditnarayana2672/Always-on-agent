"""Event intake and coordinator (F03).

Every input becomes an Event. The coordinator dedupes on (source, external_id), resolves which task
the event belongs to, creates the task when needed, and enqueues one job per task. Events that match
no task are kept for the digests.

Resolution order: explicit task_hint (our own code only) -> ids in the text (INC, ENH, REF) -> task
links (mail conversation, Teams chat, Slack thread, dataset) -> recipe create rules -> digest-only.

Fetched content is data: ids found in text only route the event to a ledger entry; they never make
anything run above Silent (golden rule 4). Deleted messages are recorded and never actionable.
"""

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from agent.adapters.base import ChatMessage, MailMessage, SlackEvent
from agent.core.clock import Clock
from agent.db.repositories import EventRepo, JobRepo, MemoryRepo, Task, TaskRepo

log = logging.getLogger(__name__)

Source = Literal["mail", "teams", "slack", "powerbi", "servicenow_report", "schedule", "api"]
JobKind = Literal["new_event", "resume", "schedule"]

# ---------------------------------------------------------------- priority (F03)

PRIORITY_URGENT = 100  # clock at or past the urgent threshold
PRIORITY_RESUME = 90  # approval decided, resume the graph
PRIORITY_CALLER_REPLY = 80
PRIORITY_NEW_TICKET = 70
PRIORITY_POWERBI = 60
PRIORITY_SCHEDULE = 40
PRIORITY_DIGEST = 10
URGENT_CLOCK_HOURS = 22.0

# Age boost is applied at claim time by JobRepo.claim (one point per minute waited).

PrioritySignal = Literal["resume", "caller_reply", "new_ticket", "powerbi", "schedule", "digest"]

_BASE: dict[PrioritySignal, int] = {
    "resume": PRIORITY_RESUME,
    "caller_reply": PRIORITY_CALLER_REPLY,
    "new_ticket": PRIORITY_NEW_TICKET,
    "powerbi": PRIORITY_POWERBI,
    "schedule": PRIORITY_SCHEDULE,
    "digest": PRIORITY_DIGEST,
}


def priority_for(signal: PrioritySignal, clock_hours: float | None = None) -> int:
    """F03 priority. A task whose 24-hour clock is at 22 h or more is urgent whatever the signal."""
    if clock_hours is not None and clock_hours >= URGENT_CLOCK_HOURS:
        return PRIORITY_URGENT
    return _BASE[signal]


# ---------------------------------------------------------------- events

EventKind = Literal[
    "mail",
    "message",
    "message_edited",
    "message_deleted",
    "slack_dm",
    "slack_thread_reply",
    "slack_button",
    "slack_command",
    "refresh",
    "report_row",
    "schedule",
    "api",
]


class Event(BaseModel):
    id: int | None = None  # set once stored
    source: Source
    kind: str
    external_id: str
    received_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    task_hint: str | None = None  # set only by our own code (schedules, API, approvals)

    @property
    def actionable(self) -> bool:
        return self.kind != "message_deleted"

    def text(self) -> str:
        """Subject and body text the id scan looks at."""
        return "\n".join(str(self.payload.get(k) or "") for k in ("subject", "body", "text", "number"))


class TaskSpec(BaseModel):
    """What a recipe create rule wants the new task to look like."""

    id: str
    project: str
    kind: str
    title: str
    recipe: str | None = None
    links: list[tuple[str, str]] = Field(default_factory=list)  # (kind, external_id)


class CreateRule(Protocol):
    """A recipe's rule for making a task from an event that matched nothing else (filled in by 1.9)."""

    name: str

    def match(self, event: Event) -> TaskSpec | None: ...


CREATE_RULES: list[CreateRule] = []


class IngestResult(BaseModel):
    event_id: int | None
    duplicate: bool = False
    task_ids: list[str] = Field(default_factory=list)
    job_ids: list[int] = Field(default_factory=list)
    created: list[str] = Field(default_factory=list)  # tasks this event created
    status: Literal["routed", "digest", "ignored", "duplicate"] = "routed"
    resolved_by: Literal["hint", "ids", "link", "rule", "none"] = "none"


# ---------------------------------------------------------------- text helpers

_TASK_ID = re.compile(r"\b(INC\d{8}|ENH-\d+|REF-\d+)\b", re.IGNORECASE)
_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg|tr|sv|vs|antw)\s*(?:\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_FORWARD_HEADER = re.compile(r"^\s*From:\s*.*?([\w.+-]+@[\w.-]+)", re.IGNORECASE | re.MULTILINE)
_FORWARDED_MARKERS = ("forwarded message", "original message", "message d'origine", "ursprüngliche nachricht")


def find_task_ids(text: str) -> list[str]:
    """Every INC, ENH and REF id in the text, upper-cased, in first-seen order without repeats."""
    seen: dict[str, None] = {}
    for match in _TASK_ID.finditer(text):
        seen.setdefault(match.group(1).upper(), None)
    return list(seen)


def strip_reply_prefixes(subject: str) -> str:
    """'RE: RE: FW: Incident INC…' -> 'Incident INC…' (also German, French, Nordic prefixes)."""
    return _REPLY_PREFIX.sub("", subject).strip()


def forwarded_from(body: str) -> str | None:
    """The original sender of a forwarded mail, from the first 'From:' header in the body."""
    lowered = body.lower()
    if not any(marker in lowered for marker in _FORWARDED_MARKERS) and "from:" not in lowered:
        return None
    match = _FORWARD_HEADER.search(body)
    return match.group(1).lower() if match else None


def _task_shape(task_id: str) -> tuple[str, str]:
    """(project, kind) for a task id."""
    if task_id.startswith("INC"):
        return "doxis", "incident"
    if task_id.startswith("ENH"):
        return "powerbi", "enhancement"
    return "powerbi", "refresh_request"


# ---------------------------------------------------------------- normalise


def normalise(raw: Any, source: Source) -> Event:
    """Turn an adapter item (or a dict for powerbi, servicenow_report, schedule, api) into an Event."""
    if isinstance(raw, Event):
        return raw
    if source == "mail" and isinstance(raw, MailMessage):
        return _from_mail(raw)
    if source == "teams" and isinstance(raw, ChatMessage):
        return _from_teams(raw)
    if source == "slack" and isinstance(raw, SlackEvent):
        return _from_slack(raw)
    if isinstance(raw, dict):
        return _from_dict(raw, source)
    raise TypeError(f"cannot normalise {type(raw).__name__} from source {source!r}")


def _from_mail(mail: MailMessage) -> Event:
    subject = strip_reply_prefixes(mail.subject)
    original = forwarded_from(mail.body) if subject != mail.subject.strip() else None
    return Event(
        source="mail",
        kind="mail",
        external_id=mail.id,
        received_at=mail.received_at,
        payload={
            "subject": subject,
            "raw_subject": mail.subject,
            "conversation_id": mail.conversation_id,
            "sender": mail.sender.address.lower(),
            "to": [p.address.lower() for p in mail.to],
            "cc": [p.address.lower() for p in mail.cc],
            "body": mail.body,
            "forwarded_from": original,
        },
    )


def _from_teams(msg: ChatMessage) -> Event:
    payload: dict[str, Any] = {
        "chat_id": msg.chat_id,
        "chat_topic": msg.chat_topic,
        "message_id": msg.id,
        "sender": msg.sender,
        "mentions_me": msg.mentions_me,
    }
    if msg.deleted:
        # Keep the fact it was deleted; drop the text so nothing can act on it.
        return Event(
            source="teams",
            kind="message_deleted",
            external_id=f"{msg.id}#deleted",
            received_at=msg.sent_at,
            payload=payload,
        )
    payload["text"] = msg.text
    if msg.edited_at is not None:
        return Event(
            source="teams",
            kind="message_edited",
            external_id=f"{msg.id}#edit@{msg.edited_at.isoformat()}",
            received_at=msg.edited_at,
            payload=payload | {"edited_at": msg.edited_at.isoformat()},
        )
    return Event(source="teams", kind="message", external_id=msg.id, received_at=msg.sent_at, payload=payload)


def _from_slack(ev: SlackEvent) -> Event:
    ref = ev.thread_ts or ev.action_id or ""
    return Event(
        source="slack",
        kind=f"slack_{ev.kind}",
        external_id=f"{ev.kind}:{ref}:{ev.user}:{ev.at.isoformat()}",
        received_at=ev.at,
        payload={
            "user": ev.user,
            "text": ev.text,
            "thread_ts": ev.thread_ts,
            "action_id": ev.action_id,
            "value": ev.value,
        },
    )


def _from_dict(raw: dict[str, Any], source: Source) -> Event:
    for key in ("kind", "external_id", "received_at"):
        if key not in raw:
            raise ValueError(f"{source} item is missing {key!r}")
    return Event(
        source=source,
        kind=raw["kind"],
        external_id=str(raw["external_id"]),
        received_at=raw["received_at"],
        payload=raw.get("payload", {}),
        task_hint=raw.get("task_hint"),
    )


# ---------------------------------------------------------------- coordinator

_LINK_KEYS: dict[str, tuple[str, str]] = {
    # source -> (task_links.kind, payload key)
    "mail": ("mail_conversation", "conversation_id"),
    "teams": ("teams_chat", "chat_id"),
    "slack": ("slack_thread", "thread_ts"),
    "powerbi": ("powerbi_dataset", "dataset_id"),
}

_SYSTEM_SENDERS = ("servicenow@", "noreply@", "no-reply@", "powerbi@")


class Coordinator:
    def __init__(
        self,
        tasks: TaskRepo,
        events: EventRepo,
        jobs: JobRepo,
        memory: MemoryRepo,
        clock: Clock,
        create_rules: Sequence[CreateRule] | None = None,
        clock_hours: Callable[[Task], Awaitable[float | None]] | None = None,
    ) -> None:
        self._tasks = tasks
        self._events = events
        self._jobs = jobs
        self._memory = memory
        self._clock = clock
        self._rules = CREATE_RULES if create_rules is None else create_rules
        # Pass Ledger.age_hours-based lookups for pause-aware age; default is wall time since clock start.
        self._clock_hours = clock_hours or self._wall_clock_hours

    async def ingest(self, raw: Any, source: Source) -> IngestResult:
        event = normalise(raw, source)
        payload = dict(event.payload)
        event_id = await self._events.add(
            event.source, event.kind, event.external_id, payload, received_at=event.received_at
        )
        if event_id is None:
            log.info("duplicate event dropped", extra={"source": event.source, "kind": event.kind})
            return IngestResult(event_id=None, duplicate=True, status="duplicate")
        event.id = event_id

        task_ids, resolved_by, created = await self._resolve(event)

        if not task_ids:
            await self._events.route(event_id, None, status="digest")
            return IngestResult(event_id=event_id, status="digest")

        if not event.actionable:
            # Recorded against the task for history; never queued.
            await self._events.route(event_id, task_ids[0], status="ignored")
            return IngestResult(
                event_id=event_id, task_ids=task_ids, status="ignored", resolved_by=resolved_by
            )

        await self._events.route(event_id, task_ids[0])
        job_ids = []
        for task_id in task_ids:
            signal = self._signal(event, task_id, task_id in created)
            job_ids.append(await self._enqueue(task_id, event, signal))
        return IngestResult(
            event_id=event_id,
            task_ids=task_ids,
            job_ids=job_ids,
            created=created,
            resolved_by=resolved_by,
        )

    async def enqueue_resume(self, task_id: str, action_id: str) -> int:
        """Called by approvals (1.5) when the user decides."""
        return await self._jobs.enqueue(
            "resume", task_id=task_id, payload={"action_id": action_id}, priority=PRIORITY_RESUME
        )

    async def digest_events(self, limit: int = 500) -> list[dict[str, Any]]:
        return await self._events.by_status("digest", limit)

    # ------------------------------------------------------------ resolution

    async def _resolve(
        self, event: Event
    ) -> tuple[list[str], Literal["hint", "ids", "link", "rule", "none"], list[str]]:
        created: list[str] = []

        if event.task_hint:
            if await self._tasks.get(event.task_hint) is None:
                return [], "none", created
            return [event.task_hint], "hint", created

        ids = find_task_ids(event.text()) if event.actionable else []
        if ids:
            for task_id in ids:
                if await self._ensure_task(task_id, event):
                    created.append(task_id)
            await self._link_event(event, ids)
            return ids, "ids", created

        linked = await self._linked_task(event)
        if linked:
            return [linked], "link", created

        if event.actionable:
            for rule in self._rules:
                spec = rule.match(event)
                if spec is None:
                    continue
                if await self._tasks.get(spec.id) is None:
                    await self._tasks.create(
                        Task(
                            id=spec.id,
                            project=spec.project,
                            kind=spec.kind,
                            title=spec.title,
                            state="new",
                            recipe=spec.recipe or rule.name,
                        )
                    )
                    created.append(spec.id)
                for kind, external_id in spec.links:
                    await self._memory.link(spec.id, kind, external_id)
                await self._link_event(event, [spec.id])
                return [spec.id], "rule", created

        return [], "none", created

    async def _ensure_task(self, task_id: str, event: Event) -> bool:
        """Create a ledger entry for an id seen for the first time. Returns True if it was created."""
        if await self._tasks.get(task_id) is not None:
            return False
        project, kind = _task_shape(task_id)
        title = str(event.payload.get("subject") or event.payload.get("text") or task_id)[:200]
        await self._tasks.create(Task(id=task_id, project=project, kind=kind, title=title, state="new"))
        return True

    async def _link_event(self, event: Event, task_ids: list[str]) -> None:
        """Remember the conversation, chat or thread so later replies without an id find the task.

        A link maps to one task, so an event naming several tasks links to none of them: a later
        reply in that thread has to name its id again rather than guess.
        """
        key = _LINK_KEYS.get(event.source)
        if key is None or len(task_ids) != 1:
            return
        link_kind, field = key
        external_id = event.payload.get(field)
        if external_id:
            await self._memory.link(task_ids[0], link_kind, str(external_id))

    async def _linked_task(self, event: Event) -> str | None:
        key = _LINK_KEYS.get(event.source)
        if key is None:
            return None
        link_kind, field = key
        external_id = event.payload.get(field)
        if not external_id:
            return None
        return await self._memory.task_for_link(link_kind, str(external_id))

    # ------------------------------------------------------------ jobs

    def _signal(self, event: Event, task_id: str, created: bool) -> PrioritySignal:
        if event.source == "schedule":
            return "schedule"
        powerbi = task_id.startswith(("ENH", "REF")) or event.source == "powerbi"
        if created:
            return "powerbi" if powerbi else "new_ticket"
        if self._from_person(event):
            return "caller_reply"
        return "powerbi" if powerbi else "new_ticket"

    @staticmethod
    def _from_person(event: Event) -> bool:
        if event.source in ("teams", "slack"):
            return True
        if event.source != "mail":
            return False
        if event.payload.get("forwarded_from"):
            # A forwarded system notification counts as the system, not as a caller reply.
            return not str(event.payload["forwarded_from"]).startswith(_SYSTEM_SENDERS)
        return not str(event.payload.get("sender", "")).startswith(_SYSTEM_SENDERS)

    async def _enqueue(self, task_id: str, event: Event, signal: PrioritySignal) -> int:
        task = await self._tasks.get(task_id)
        hours = await self._clock_hours(task) if task else None
        kind: JobKind = "schedule" if event.source == "schedule" else "new_event"
        return await self._jobs.enqueue(
            kind,
            task_id=task_id,
            payload={"event_id": event.id, "source": event.source, "kind": event.kind},
            priority=priority_for(signal, hours),
        )

    async def _wall_clock_hours(self, task: Task) -> float | None:
        if task.clock_started_at is None:
            return None
        return (self._clock.now_utc() - task.clock_started_at).total_seconds() / 3600
