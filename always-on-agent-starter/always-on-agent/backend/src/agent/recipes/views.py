"""Agent-side tools for the Phase 1 recipes (R01-R06): read views and publishing to the user.

Scheduled recipes run without a task, so their steps cannot see the ledger, the audit log or the
event store directly. These Silent tools give them read-only views, through the gate and the outbox
like every other tool (audited, golden rule 1):

- read_brief_data      needs-you tasks, 24-hour clocks, stalled tasks, replies owed, today's meetings,
                       Power BI failures, connector problems, and whether yesterday's brief was missed;
- read_digest_data     today's Report actions with their undo state, open approvals, hand-offs,
                       shadow would-do counts per recipe;
- read_mentions        Teams mentions and one-to-one messages nobody has answered yet;
- read_refresh_facts   what decides whether a refresh retry may run on its own (owned, retries today);
- read_mail_rules      the user's sender rules for inbox triage;
- read_reply_context   the mail a reply task answers (message and conversation ids).

Publishing goes to the user only, so it is Silent too (architecture R01: "post to Slack DM and
console S"): publish_brief, publish_digest and notify_me go through the Notifier (quiet hours, the
push cap, the channel fallback chain) and keep the latest brief and digest for the console.

open_reply_task (Report) opens an RPL-#### task for a mail that needs a reply, linked to its
conversation, and queues the draft-replies recipe on it. A conversation that already has a task is
not opened twice.

Every source is read on its own: a calendar or Power BI outage leaves a note in the brief instead of
failing it.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.channels import cards
from agent.channels.cards import BriefSection, DigestItem
from agent.channels.notifier import Kind, Notification, Notifier
from agent.core.clock import Clock
from agent.core.config import Config
from agent.core.errors import AgentError
from agent.db.repositories import (
    ActionRepo,
    AuditRepo,
    EventRepo,
    HealthRepo,
    JobRepo,
    KvRepo,
    LessonRepo,
    MemoryRepo,
    Task,
    TaskRepo,
    UndoRepo,
)
from agent.ledger.service import Ledger
from agent.policy.registry import Registry
from agent.privacy.pseudonymiser import LedgerPeople, people_from_mail

log = logging.getLogger(__name__)

BRIEF_KEY = "brief.latest"
BRIEF_DATE_KEY = "brief.last_date"
DIGEST_KEY = "digest.latest"
REPLY_PREFIX = "RPL"
REPLY_LINK = "mail_conversation"
OPEN_STATES = ["new", "in_progress", "needs_you", "waiting", "scheduled"]
TOOL_LABELS = {
    "move_mail": "Moved a mail",
    "flag_mail": "Flagged a mail",
    "categorise": "Set categories on a mail",
    "save_draft": "Saved a draft",
    "add_hold": "Added a calendar hold",
    "trigger_refresh": "Started a refresh",
    "trigger_dataflow_refresh": "Started a dataflow refresh",
    "open_reply_task": "Opened a reply task",
    "update_ledger": "Updated a task",
    "write_memory": "Saved a note",
}


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- view models


class TaskLine(BaseModel):
    id: str
    title: str
    project: str
    state: str
    next_action: str | None = None
    age_hours: float | None = None


class ClockLine(BaseModel):
    id: str
    title: str
    age_hours: float
    crossed: list[int]  # thresholds already passed (12, 18, 22)
    paused: bool = False
    estimated: bool = False


class MeetingLine(BaseModel):
    start: str  # HH:MM local
    end: str
    subject: str
    join_url: str | None = None


class RefreshLine(BaseModel):
    dataset_id: str
    status: str
    error_code: str | None = None
    at: datetime | None = None


class ConnectorLine(BaseModel):
    connector: str
    status: str
    detail: str | None = None


class BriefData(BaseModel):
    day: date
    missed: str | None = None  # "The brief of Tue 24 Mar did not run (the agent was off)."
    connectors: list[ConnectorLine] = Field(default_factory=list)
    needs_you: list[TaskLine] = Field(default_factory=list)
    clocks: list[ClockLine] = Field(default_factory=list)
    stalled: list[TaskLine] = Field(default_factory=list)
    replies_owed: list[TaskLine] = Field(default_factory=list)
    meetings: list[MeetingLine] = Field(default_factory=list)
    powerbi: list[RefreshLine] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)  # sources that could not be read


class DoneLine(BaseModel):
    audit_id: int
    tool: str
    text: str
    task_id: str | None = None
    recipe: str | None = None
    undo: Literal["available", "done", "none"] = "none"


class OpenLine(BaseModel):
    action_id: str
    tool: str
    task_id: str | None = None
    status: str
    expires_at: datetime | None = None
    reason: str | None = None


class DigestData(BaseModel):
    day: date
    done: list[DoneLine] = Field(default_factory=list)
    approvals: list[OpenLine] = Field(default_factory=list)
    hand_offs: list[OpenLine] = Field(default_factory=list)
    shadow: dict[str, int] = Field(default_factory=dict)


class MentionLine(BaseModel):
    chat_id: str
    chat_topic: str | None = None
    sender: str
    at: datetime
    message_id: str
    mention: bool  # True: @mention in a group chat; False: a one-to-one message


class RefreshFacts(BaseModel):
    dataset_id: str
    owned: bool
    retry_on_own: bool = True  # Settings: "Retry your own failed refresh" is Report (True) or Ask
    agent_retries_today: int
    last_error_code: str | None = None
    running: bool = False


class MailRules(BaseModel):
    me: list[str]
    manager: list[str]
    newsletter_senders: list[str]
    notification_senders: list[str]
    folder_notifications: str
    folder_newsletters: str


class ReplyContext(BaseModel):
    task_id: str
    message_id: str | None = None
    conversation_id: str | None = None


# ---------------------------------------------------------------- tool args


class Empty(_Args):
    pass


class DayArgs(_Args):
    day: date | None = None  # local date; today when empty


class MentionArgs(_Args):
    hours: int = Field(default=72, ge=1, le=24 * 14)


class DatasetArgs(_Args):
    dataset_id: str = Field(min_length=1)


class TaskArgs(_Args):
    task_id: str = Field(min_length=1)


class BriefOut(_Args):
    title: str = Field(min_length=1, max_length=150)
    sections: list[BriefSection] = Field(default_factory=list)
    missed: str | None = None


class DigestOut(_Args):
    title: str = Field(min_length=1, max_length=150)
    items: list[DigestItem] = Field(default_factory=list)
    open_approvals: int = 0
    hand_offs: int = 0
    shadow_counts: dict[str, int] = Field(default_factory=dict)


class NotifyMe(_Args):
    kind: Kind = "info"
    text: str = Field(min_length=1, max_length=3000)
    task_id: str | None = None
    urgent: bool = False


class OpenReplyTask(_Args):
    message_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    subject: str = Field(default="", max_length=300)
    event_id: int | None = None  # the intake event, so the mail lands in the task's memory


# ---------------------------------------------------------------- the views


@dataclass
class ViewDeps:
    config: Config
    clock: Clock
    tasks: TaskRepo
    ledger: Ledger
    memory: MemoryRepo
    jobs: JobRepo
    actions: ActionRepo
    audit: AuditRepo
    undo: UndoRepo
    events: EventRepo
    health: HealthRepo
    kv: KvRepo
    lessons: LessonRepo | None
    adapters: Mapping[str, Any]
    notifier: Notifier | None = None
    console_url: str = "http://127.0.0.1:8765"
    people: LedgerPeople | None = None


class AgentViews:
    def __init__(self, deps: ViewDeps) -> None:
        self.d = deps

    # ------------------------------------------------------------ helpers

    def _today(self, day: date | None = None) -> date:
        return day or self.d.clock.now_local().date()

    def _day_start_utc(self, day: date) -> datetime:
        zone = self.d.clock.now_local().tzinfo
        return datetime.combine(day, time(0), zone).astimezone(UTC)

    @staticmethod
    def _line(task: Task, age: float | None = None) -> TaskLine:
        return TaskLine(
            id=task.id,
            title=task.title,
            project=task.project,
            state=task.state,
            next_action=task.next_action,
            age_hours=round(age, 1) if age is not None else None,
        )

    # ------------------------------------------------------------ brief

    async def brief_data(self, args: DayArgs) -> BriefData:
        day = self._today(args.day)
        data = BriefData(day=day)
        data.missed = await self._missed(day)
        for row in await self.d.health.all():
            if row["status"] != "ok":
                data.connectors.append(
                    ConnectorLine(
                        connector=row["connector"], status=row["status"], detail=row.get("last_error")
                    )
                )
        thresholds = sorted(self.d.config.clock.thresholds_hours)
        for task in await self.d.tasks.list(OPEN_STATES):
            age = await self.d.ledger.age_hours(task.id)
            if task.state == "needs_you":
                data.needs_you.append(self._line(task, age))
            if task.id.startswith(REPLY_PREFIX) and task.state != "done":
                data.replies_owed.append(self._line(task, age))
            if task.project == "doxis" and task.clock_started_at is not None and age is not None:
                data.clocks.append(
                    ClockLine(
                        id=task.id,
                        title=task.title,
                        age_hours=round(age, 1),
                        crossed=[h for h in thresholds if age >= h],
                        paused=task.clock_paused,
                        estimated=task.clock_estimated,
                    )
                )
        for task in await self.d.ledger.stalled():
            data.stalled.append(self._line(task, await self.d.ledger.age_hours(task.id)))
        data.clocks.sort(key=lambda c: -c.age_hours)
        data.needs_you.sort(key=lambda t: -(t.age_hours or 0))
        await self._meetings(day, data)
        await self._powerbi(data)
        return data

    async def _missed(self, day: date) -> str | None:
        """Yesterday's work-day brief that never ran (the laptop was off), said at the top (R01)."""
        last = await self.d.kv.get(BRIEF_DATE_KEY)
        previous = day - timedelta(days=1)
        is_work_day = getattr(self.d.clock, "is_work_day", None)
        for _ in range(10):  # skip weekends and holidays: Monday's "yesterday" is Friday
            if is_work_day is None or is_work_day(previous):
                break
            previous -= timedelta(days=1)
        if last is None:
            return None  # first brief ever: nothing was missed
        if date.fromisoformat(last) < previous:
            return (
                f"No brief ran on {previous:%a %d %b}; the agent was not running. "
                "Items below include that day."
            )
        return None

    async def _meetings(self, day: date, data: BriefData) -> None:
        calendar = self.d.adapters.get("calendar")
        if calendar is None:
            return
        start = self._day_start_utc(day)
        try:
            events = await calendar.events_between(start, start + timedelta(days=1))
        except AgentError as exc:
            data.unavailable.append(f"calendar ({str(exc)[:80]})")
            return
        for event in events:
            if event.private or event.cancelled or event.response == "declined":
                continue
            local_start = self.d.clock.to_local(event.start)
            local_end = self.d.clock.to_local(event.end)
            data.meetings.append(
                MeetingLine(
                    start="all day" if event.all_day else f"{local_start:%H:%M}",
                    end="" if event.all_day else f"{local_end:%H:%M}",
                    subject=event.subject,
                    join_url=event.join_url,
                )
            )

    async def _powerbi(self, data: BriefData) -> None:
        pbi = self.d.adapters.get("powerbi")
        if pbi is None:
            return
        for dataset_id in self.d.config.powerbi.watch.datasets:
            try:
                history = await pbi.refresh_history(dataset_id, 1)
            except AgentError as exc:
                data.unavailable.append(f"Power BI ({str(exc)[:80]})")
                return
            if history and history[0].status in ("Failed", "Cancelled"):
                last = history[0]
                data.powerbi.append(
                    RefreshLine(
                        dataset_id=dataset_id, status=last.status, error_code=last.error_code, at=last.end
                    )
                )

    # ------------------------------------------------------------ digest

    async def digest_data(self, args: DayArgs) -> DigestData:
        day = self._today(args.day)
        start = self._day_start_utc(day)
        data = DigestData(day=day)
        data.done = await self.done_lines(start, start + timedelta(days=1))
        for row in await self.d.audit.since(start):
            if row["at"] >= start + timedelta(days=1):
                break
            if str(row["result"]).startswith("decision:shadow"):
                recipe = str(row.get("recipe") or "manual")
                data.shadow[recipe] = data.shadow.get(recipe, 0) + 1
        for action in await self.d.actions.with_statuses(("proposed", "edited")):
            data.approvals.append(
                OpenLine(
                    action_id=action.id,
                    tool=action.tool,
                    task_id=action.task_id,
                    status=action.status,
                    expires_at=action.expires_at,
                )
            )
        for action in await self.d.actions.with_statuses(("handed_off",)):
            if action.created_at >= start - timedelta(days=7):  # a week back: older ones are in the console
                data.hand_offs.append(
                    OpenLine(
                        action_id=action.id, tool=action.tool, task_id=action.task_id, status=action.status
                    )
                )
        return data

    async def done_lines(self, start: datetime, end: datetime) -> list[DoneLine]:
        """Report actions that ran between start and end, with their undo state (digest, console)."""
        out = []
        for row in await self.d.audit.since(start):
            if row["at"] >= end:
                break
            if (
                row["mode"] != "report"
                or str(row["result"]) != "done"
                or str(row["tool"]).startswith("undo:")
            ):
                continue
            op = await self.d.undo.for_audit(int(row["id"]))
            undo: Literal["available", "done", "none"] = "none"
            if op is not None:
                undo = "done" if op["status"] == "done" else "available"
            out.append(
                DoneLine(
                    audit_id=int(row["id"]),
                    tool=str(row["tool"]),
                    text=await self._label(row),
                    task_id=row.get("task_id"),
                    recipe=row.get("recipe"),
                    undo=undo,
                )
            )
        return out

    async def label(self, row: dict[str, Any]) -> str:
        return await self._label(row)

    async def _label(self, row: dict[str, Any]) -> str:
        tool = str(row["tool"])
        label = TOOL_LABELS.get(tool, tool.replace("_", " ").capitalize())
        if row.get("action_id"):
            action = await self.d.actions.get(str(row["action_id"]))
            if action is not None:
                args = json.loads(action.args_json)
                if tool == "move_mail" and args.get("folder"):
                    label += f" to {args['folder']}"
                elif tool == "categorise" and args.get("categories"):
                    label += f": {', '.join(args['categories'])[:60]}"
                elif tool in ("trigger_refresh",) and args.get("dataset_id"):
                    label += f" of {args['dataset_id']}"
        return label

    # ------------------------------------------------------------ mentions

    async def mentions(self, args: MentionArgs) -> list[MentionLine]:
        """Teams @mentions and one-to-one messages from others with no later message from the user.

        Deleted messages and messages the user already answered in the same chat drop out. Oldest first.
        """
        since = self.d.clock.now_utc() - timedelta(hours=args.hours)
        open_by_chat: dict[str, list[MentionLine]] = {}
        for row in await self.d.events.by_source_since("teams", since):
            payload = json.loads(row["payload_json"])
            chat = str(payload.get("chat_id") or "")
            if not chat:
                continue
            if payload.get("from_me"):
                open_by_chat[chat] = []  # the user wrote in this chat: everything before is answered
                continue
            if row["kind"] == "message_deleted":
                message_id = str(payload.get("message_id") or "")
                open_by_chat[chat] = [m for m in open_by_chat.get(chat, []) if m.message_id != message_id]
                continue
            one_to_one = payload.get("chat_topic") is None
            if not (payload.get("mentions_me") or one_to_one) or row["kind"] == "message_edited":
                continue
            open_by_chat.setdefault(chat, []).append(
                MentionLine(
                    chat_id=chat,
                    chat_topic=payload.get("chat_topic"),
                    sender=str(payload.get("sender") or "?"),
                    at=row["received_at"],
                    message_id=str(payload.get("message_id") or row["external_id"]),
                    mention=bool(payload.get("mentions_me")),
                )
            )
        out = [m for items in open_by_chat.values() for m in items]
        return sorted(out, key=lambda m: m.at)

    # ------------------------------------------------------------ refresh facts and rules

    async def refresh_facts(self, args: DatasetArgs) -> RefreshFacts:
        start = self._day_start_utc(self._today())
        retries = 0
        # Every attempt counts, also one that failed: "one retry" means one try, not one success.
        for action in await self.d.actions.with_statuses(("done", "executing", "ready", "failed")):
            if (
                action.tool == "trigger_refresh"
                and action.created_at >= start
                and json.loads(action.args_json).get("dataset_id") == args.dataset_id
            ):
                retries += 1
        facts = RefreshFacts(
            dataset_id=args.dataset_id,
            owned=args.dataset_id in self.d.config.powerbi.owned_datasets,
            retry_on_own=self.d.config.powerbi.retry_own_transient,
            agent_retries_today=retries,
        )
        pbi = self.d.adapters.get("powerbi")
        if pbi is not None:
            try:
                history = await pbi.refresh_history(args.dataset_id, 5)
            except AgentError:
                history = []
            facts.running = any(r.status == "Unknown" for r in history)
            failed = next((r for r in history if r.status == "Failed"), None)
            facts.last_error_code = failed.error_code if failed else None
        return facts

    async def mail_rules(self, args: Empty) -> MailRules:
        mail = self.d.config.mail
        return MailRules(
            me=[m.lower() for m in self.d.config.me],
            manager=[m.lower() for m in mail.manager],
            newsletter_senders=[m.lower() for m in mail.newsletter_senders],
            notification_senders=[m.lower() for m in mail.notification_senders],
            folder_notifications=mail.folder_notifications,
            folder_newsletters=mail.folder_newsletters,
        )

    async def reply_context(self, args: TaskArgs) -> ReplyContext:
        facts = {f["key"]: f["value"] for f in await self.d.memory.facts(args.task_id)}
        links = await self.d.memory.links(args.task_id)
        conversation = next((str(link["external_id"]) for link in links if link["kind"] == REPLY_LINK), None)
        return ReplyContext(
            task_id=args.task_id, message_id=facts.get("_reply_message_id"), conversation_id=conversation
        )

    async def known_fix(self, args: "FixArgs") -> list[str]:
        """Lessons that mention this error code (R05: known fixes from memory)."""
        if self.d.lessons is None or not args.error_code:
            return []
        needle = args.error_code.lower()
        return [
            str(row["text"])[:300]
            for row in await self.d.lessons.active()
            if needle in str(row["text"]).lower()
        ]

    # ------------------------------------------------------------ publishing

    async def publish_brief(self, args: BriefOut) -> dict[str, Any]:
        now = self.d.clock.now_utc()
        record = {**args.model_dump(mode="json"), "at": now.isoformat()}
        await self.d.kv.set(BRIEF_KEY, json.dumps(record))
        await self.d.kv.set(BRIEF_DATE_KEY, self.d.clock.to_local(now).date().isoformat())
        sections = list(args.sections)
        text = brief_text(args.title, args.missed, sections)
        _, blocks = cards.brief_blocks(args.title, sections, self.d.console_url)
        if args.missed:
            blocks.insert(
                1, {"type": "context", "elements": [{"type": "mrkdwn", "text": cards.escape(args.missed)}]}
            )
        status = await self._notify(Notification(kind="brief", text=text, blocks=blocks))
        return {"stored": True, "pushed": status}

    async def publish_digest(self, args: DigestOut) -> dict[str, Any]:
        now = self.d.clock.now_utc()
        await self.d.kv.set(DIGEST_KEY, json.dumps({**args.model_dump(mode="json"), "at": now.isoformat()}))
        _, blocks = cards.digest_blocks(
            args.title,
            args.items,
            self.d.console_url,
            args.open_approvals,
            args.hand_offs,
            args.shadow_counts,
        )
        lines = [args.title] + [
            f"- {i.text}" + (f" (undo {i.audit_id})" if i.audit_id else "") for i in args.items
        ]
        if args.open_approvals:
            lines.append(f"{args.open_approvals} waiting for your approval")
        if args.hand_offs:
            lines.append(f"{args.hand_offs} only you can do")
        for recipe, count in sorted(args.shadow_counts.items()):
            lines.append(f"{recipe} would have acted {count} time(s) (trial mode)")
        if not args.items:
            lines.append("No actions on my own today.")
        status = await self._notify(Notification(kind="digest", text="\n".join(lines), blocks=blocks))
        return {"stored": True, "pushed": status}

    async def notify_me(self, args: NotifyMe) -> dict[str, Any]:
        note = Notification(kind=args.kind, text=args.text, task_id=args.task_id, urgent=args.urgent)
        return {"pushed": await self._notify(note)}

    async def _notify(self, note: Notification) -> str:
        if self.d.notifier is None:
            return "no channel"
        try:
            return await self.d.notifier.notify(note)
        except AgentError as exc:
            log.warning("notification not delivered", extra={"kind": note.kind, "error": str(exc)[:200]})
            return "not delivered"

    # ------------------------------------------------------------ reply tasks

    async def open_reply_task(self, args: OpenReplyTask) -> str:
        existing = await self.d.memory.task_for_link(REPLY_LINK, args.conversation_id)
        if existing is not None:
            return existing  # one task per conversation, even when several mails arrive
        highest = 0
        for task_id in await self.d.tasks.ids_with_prefix(f"{REPLY_PREFIX}-"):
            suffix = task_id.split("-", 1)[1]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        task_id = f"{REPLY_PREFIX}-{highest + 1:04d}"
        title = " ".join((args.subject or "Reply owed").split())[:200]
        await self.d.ledger.create(
            Task(
                id=task_id, project="general", kind="reply", title=title, state="new", recipe="draft-replies"
            )
        )
        await self.d.memory.link(task_id, REPLY_LINK, args.conversation_id)
        await self._remember_people(task_id, args.message_id)
        await self.d.memory.set_fact(
            task_id, "_reply_message_id", args.message_id, source_ref=f"mail:{args.message_id}"
        )
        await self.d.jobs.enqueue(
            "new_event",
            task_id=task_id,
            payload={
                "source": "mail",
                "kind": "reply_task",
                "event_id": args.event_id,
                "recipes": ["draft-replies"],
            },
            priority=80,
        )
        return task_id

    async def _remember_people(self, task_id: str, message_id: str) -> None:
        """Sender, To and Cc become the task's participants: the gate only lets a reply reach them."""
        outlook = self.d.adapters.get("outlook")
        if self.d.people is None or outlook is None:
            return
        try:
            mail = await outlook.get_message(message_id)
        except AgentError:
            return
        me = {m.lower() for m in self.d.config.me}
        people = [p for p in people_from_mail(mail, {mail.sender.address: "requester"}) if p.email not in me]
        await self.d.people.remember(task_id, people)


class FixArgs(_Args):
    error_code: str = Field(default="", max_length=200)


def brief_text(title: str, missed: str | None, sections: list[BriefSection]) -> str:
    lines = [title]
    if missed:
        lines.append(missed)
    filled = [s for s in sections if s.lines]
    if not filled:
        lines.append("Nothing needs you right now.")
    for section in filled:
        lines.append(f"{section.heading}:")
        lines.extend(f"- {line}" for line in section.lines)
    return "\n".join(lines)


Handler = Callable[[BaseModel, str], Awaitable[Any]]


def _handler(fn: Callable[[Any], Awaitable[Any]]) -> Handler:
    async def run(args: BaseModel, key: str) -> Any:
        result = await fn(args)
        if isinstance(result, BaseModel):
            return result.model_dump(mode="json")
        if isinstance(result, list):
            return [r.model_dump(mode="json") if isinstance(r, BaseModel) else r for r in result]
        return result

    return run


def register_view_tools(reg: Registry, views: AgentViews) -> None:
    silent: list[tuple[str, type[BaseModel], Callable[[Any], Awaitable[Any]]]] = [
        ("read_brief_data", DayArgs, views.brief_data),
        ("read_digest_data", DayArgs, views.digest_data),
        ("read_mentions", MentionArgs, views.mentions),
        ("read_refresh_facts", DatasetArgs, views.refresh_facts),
        ("read_mail_rules", Empty, views.mail_rules),
        ("read_reply_context", TaskArgs, views.reply_context),
        ("find_known_fix", FixArgs, views.known_fix),
        ("publish_brief", BriefOut, views.publish_brief),
        ("publish_digest", DigestOut, views.publish_digest),
        ("notify_me", NotifyMe, views.notify_me),
    ]
    for name, model, fn in silent:
        reg.tool(name, mode="silent", handler=_handler(fn))(model)
    reg.tool("open_reply_task", mode="report", handler=_handler(views.open_reply_task))(OpenReplyTask)
