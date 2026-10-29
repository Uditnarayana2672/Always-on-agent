"""Console service (F15): what each console page shows, and every console action, on top of the runtime.

Routes stay thin; this class does the work. Every write goes through the same services the agent
uses: approvals (content hash), the ledger (only the user closes a task), undo (Report actions only),
controls (pause, resume, reset with a confirmation token) and the recipe loader. Nothing here calls
an adapter's write method.

Text shown in the console is restored from tokens on this laptop (the token map never leaves it). A
token the map does not know stays as it is.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError
from sqlalchemy import func, select

from agent.api import models as m
from agent.core.errors import AgentError, ApprovalConflict, ConfigError, ControlError, IllegalTransition
from agent.core.ids import new_ulid
from agent.db import schema as s
from agent.db.repositories import ActionRequest, Task
from agent.policy.modes import RANK
from agent.recipes.schema import RecipeSpec, Trigger
from agent.recipes.views import BRIEF_KEY
from agent.runtime.assembly import Runtime
from agent.runtime.coordinator import Event
from agent.runtime.settings import (
    GROUPS,
    OVERRIDES_KEY,
    POLICY_GROUPS,
    POLICY_KEY,
    TO_MODE,
    apply_overrides,
    check_row,
    group_mode,
    load_overrides,
    policy_with,
)

log = logging.getLogger(__name__)

MODE_LABEL: dict[str, m.Mode] = {
    "silent": "Silent",
    "report": "Report",
    "ask": "Ask",
    "hand_off": "Hand off",
    "blocked": "Blocked",
}
COLUMN: dict[str, m.Column] = {
    "needs_you": "needs",
    "failed": "needs",
    "new": "progress",
    "in_progress": "progress",
    "waiting": "waiting",
    "scheduled": "scheduled",
    "done": "done",
}
PROJECT_LABEL: dict[str, Literal["Core", "Doxis", "Power BI"]] = {
    "doxis": "Doxis",
    "powerbi": "Power BI",
    "general": "Core",
}
PROJECT_KEY: dict[str, str] = {v: k for k, v in PROJECT_LABEL.items()}
ConnStatus = Literal["ok", "part", "pend", "no"]
HEALTH_STATUS: dict[str, tuple[ConnStatus, str]] = {
    "ok": ("ok", "Connected"),
    "degraded": ("part", "Partly working"),
    "down": ("no", "Not working"),
}
STATUS_LABEL: dict[str, Literal["Live", "Shadow", "Off"]] = {"live": "Live", "shadow": "Shadow", "off": "Off"}
OUTPUT_LABEL = {
    "approval_card": "Approval card",
    "digest": "Nightly digest",
    "dm": "Main DM",
    "report": "Brief section",
    "none": "Task thread",
}
KIND_LABEL = {
    "send_mail": "Email",
    "save_draft": "Email (draft in Outlook)",
    "post_chat": "Teams message",
    "trigger_refresh": "Power BI refresh",
    "trigger_dataflow_refresh": "Power BI dataflow refresh",
    "update_refresh_schedule": "Refresh schedule change",
    "edit_shared_list": "Shared list edit",
    "add_comment_by_reply": "ServiceNow comment",
}
VIA_LABEL = {"send_mail": "Outlook", "save_draft": "Outlook", "post_chat": "Teams"}
NAMED_SCHEDULE_LABEL = {
    "brief": "morning brief",
    "checkin": "each check-in",
    "nightly_digest": "nightly digest",
    "chase_window": "chase window",
}
CONTROL_LABEL = {
    "pause": "Paused the agent",
    "resume": "Resumed the agent",
    "checkin_now": "Ran a check-in now",
    "leave_on": "Turned leave mode on",
    "leave_off": "Turned leave mode off",
    "reset_requested": "Asked for a reset",
    "reset_refused": "Reset refused",
}
FACT_PASS = {"known", "pass", "ok", "confirmed", "yes", "true"}
FACT_FAIL = {"failed", "fail", "no", "false", "missing"}
STALE_DAYS = 90
ACTIVITY_ROWS = 800
CONNECTORS: tuple[tuple[str, str, str, list[str], str], ...] = (
    # adapter, name, via, what it can do, scopes
    (
        "outlook",
        "Outlook mail",
        "Microsoft Graph, or Outlook on this laptop",
        ["Read and search mail", "Move, flag, save drafts (Report)", "Send only after approval (Ask)"],
        "Mail.ReadWrite · Mail.Send",
    ),
    (
        "calendar",
        "Outlook calendar",
        "Microsoft Graph",
        ["Read today's meetings for the brief", "Tentative holds on your own calendar"],
        "Calendars.ReadWrite",
    ),
    (
        "teams",
        "Microsoft Teams",
        "Graph, polled every few minutes",
        ["Read chats and mentions", "Post in chats after approval"],
        "Chat.ReadWrite",
    ),
    (
        "slack",
        "Slack",
        "Socket Mode, no public URL needed",
        ["Your main DM and approval cards", "Mention digest"],
        "Bot in your workspace",
    ),
    (
        "powerbi",
        "Power BI",
        "REST API, delegated as you",
        ["Refresh history and schedules", "Trigger refreshes after approval"],
        "Dataset.ReadWrite · Dataflow.ReadWrite · Report.Read",
    ),
    (
        "servicenow",
        "ServiceNow",
        "No API · notification emails and the hourly report",
        ["New tickets from assignment emails", "State changes: you, in ServiceNow"],
        "Arrives in Phase 2",
    ),
    (
        "ica",
        "ICA gateway",
        "Claude models",
        ["Haiku: default for every run", "Sonnet: caller drafts and long transcripts"],
        "Key stored in Windows Credential Manager",
    ),
)


class NotFound(AgentError):
    """No such task, approval, recipe or row (404)."""


class Conflict(AgentError):
    """The request no longer applies (409): stale hash, already decided, wrong state."""


class Refused(AgentError):
    """The console may not do this (422): a locked row, the data rule, a Hand off tool."""


class ConsoleService:
    def __init__(self, rt: Runtime, *, fake: bool = False) -> None:
        self.rt = rt
        self.fake = fake

    # ------------------------------------------------------------ helpers

    def _local(self, dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return self.rt.clock.to_local(dt)

    def _when(self, dt: datetime | None) -> str:
        """HH:MM today, else 'Tue 18:40', else '12 Oct'."""
        local = self._local(dt)
        if local is None:
            return "—"
        today = self.rt.clock.now_local().date()
        if local.date() == today:
            return f"{local:%H:%M}"
        if (today - local.date()).days < 7:
            return f"{local:%a %H:%M}"
        return f"{local:%d %b}"

    async def _restore(self, text: str, task_id: str | None) -> str:
        try:
            return await self.rt.pseudo.restore(text, task_id)
        except AgentError:
            return text

    async def _real(self, token: str | None, task_id: str) -> str:
        if not token:
            return "—"
        return await self.rt.token_map.real_value(token, task_id) or token

    async def _age(self, task: Task) -> float | None:
        if task.state in ("done", "scheduled"):
            return None
        return await self.rt.ledger.age_hours(task.id)

    @staticmethod
    def _has_clock(task: Task) -> bool:
        return (
            task.project == "doxis"
            and task.clock_started_at is not None
            and task.state
            not in (
                "done",
                "scheduled",
            )
        )

    async def _open_actions(self) -> list[ActionRequest]:
        return await self.rt.approvals.pending()

    # ------------------------------------------------------------ shell

    async def status(self) -> m.StatusOut:
        state = await self.rt.controls.state()
        tasks = await self.rt.tasks.list(["needs_you", "failed"])
        return m.StatusOut(
            paused=state.paused,
            leave=state.leave,
            model=self.rt.config.models.default.capitalize(),
            needs_you=len(tasks),
            approvals=len(await self._open_actions()),
            fake=self.fake,
        )

    # ------------------------------------------------------------ brief

    async def brief(self) -> m.BriefOut:
        cfg = self.rt.config
        now_local = self.rt.clock.now_local()
        data = await self.rt.views.brief_data(_day_args())
        state = await self.rt.controls.state()
        approvals = await self._open_actions()
        open_tasks = await self.rt.tasks.list(["new", "in_progress", "needs_you", "waiting", "failed"])
        needs = [
            m.NeedLine(
                id=line.id,
                title=line.title,
                project=PROJECT_LABEL.get(line.project, line.project.title()),
                reason=line.next_action or "Needs your decision.",
            )
            for line in data.needs_you
        ]
        clocks = [
            m.ClockOut(id=c.id, title=c.title, age_hours=c.age_hours, paused=c.paused, estimated=c.estimated)
            for c in data.clocks
        ]
        watched = len(cfg.powerbi.watch.datasets)
        failed = len(data.powerbi)
        stats = [
            m.StatOut(value=f"{watched - failed} / {watched}", label="datasets normal")
            if watched
            else m.StatOut(value="—", label="no datasets watched"),
            m.StatOut(value=str(len(data.replies_owed)), label="replies owed"),
            m.StatOut(value=str(len(data.stalled)), label="stalled tasks"),
        ]
        items = [
            m.PowerBIItem(
                title=f"Refresh of {r.dataset_id} {r.status.lower()}",
                meta=(f"Error {r.error_code}. " if r.error_code else "") + f"Last run {self._when(r.at)}.",
                severity="alert",
            )
            for r in data.powerbi
        ]
        start = datetime.combine(now_local.date() - timedelta(days=1), datetime.min.time(), now_local.tzinfo)
        done = await self.rt.views.done_lines(start.astimezone(UTC), self.rt.clock.now_utc() + timedelta(1))
        done_out = []
        for line in reversed(done[-20:]):
            row = await self.rt.audit.get(line.audit_id)
            done_out.append(
                m.DoneOut(
                    audit_id=line.audit_id,
                    text=line.text,
                    time=self._when(row["at"] if row else None),
                    ref=line.task_id or _recipe_ref(line.recipe),
                    undo=line.undo,
                )
            )
        meetings_note = None
        if any(u.startswith("calendar") for u in data.unavailable):
            meetings_note = "The calendar could not be read just now."
        elif "calendar" not in self.rt.adapters:
            meetings_note = "The calendar is not connected."
        runs = await self.rt.schedule_runs.runs("checkin")
        last = max(
            (r["ran_at"] or r["due_at"] for r in runs if r["status"] in ("ran", "caught_up")), default=None
        )
        next_checkin = None
        if not state.paused:
            from agent.runtime.scheduler import checkin_times

            upcoming = []
            for hh_mm in checkin_times(cfg):
                try:
                    upcoming.append(self.rt.clock.next_occurrence(hh_mm))
                except (AgentError, ValueError):
                    continue
            next_checkin = self._when(min(upcoming)) if upcoming else None
        sonnet = ", Sonnet for caller drafts" if "caller_drafts" in cfg.models.sonnet_for else ""
        name = _first_name(self.rt.personal.read_file("profile.md"))
        count = len(needs)
        things = "Nothing needs you." if count == 0 else f"{count} thing{'s' if count != 1 else ''} need you."
        return m.BriefOut(
            day_label=f"{now_local:%a %d %b} · {cfg.schedule.brief} {now_local:%Z} brief".replace("  ", " "),
            greeting=f"{_greeting(now_local.hour)}{', ' + name if name else ''}. {things}",
            paused=state.paused,
            last_checkin=self._when(last) if last else None,
            next_checkin=next_checkin,
            open_tasks=len(open_tasks),
            approvals=len(approvals),
            model_line=f"{cfg.models.default.capitalize()}{sonnet}",
            needs=needs,
            clocks=clocks,
            thresholds=sorted(cfg.clock.thresholds_hours),
            powerbi_stats=stats,
            powerbi_items=items,
            done=done_out,
            meetings=[
                m.MeetingOut(start=x.start, end=x.end, subject=x.subject, join_url=x.join_url)
                for x in data.meetings
            ],
            meetings_note=meetings_note,
            latest_text=await self.rt.kv.get(BRIEF_KEY),
            connectors_down=[
                f"{c.connector}: {c.status}" if c.status else c.connector for c in data.connectors
            ]
            + ([data.missed] if data.missed else []),
        )

    # ------------------------------------------------------------ tasks

    async def _card(self, task: Task, open_ids: set[str]) -> m.TaskCardOut:
        age = await self._age(task)
        return m.TaskCardOut(
            id=task.id,
            project=PROJECT_LABEL.get(task.project, task.project.title()),
            title=task.title,
            column=COLUMN.get(task.state, "progress"),
            state=task.state,
            age_hours=round(age, 1) if age is not None else None,
            clock=self._has_clock(task),
            next=task.next_action or _state_line(task.state),
            has_approval=task.id in open_ids,
            recipe=task.recipe,
        )

    async def tasks(self, project: str | None = None) -> list[m.TaskCardOut]:
        key = (
            PROJECT_KEY.get(project or "", (project or "").lower()) if project and project != "All" else None
        )
        open_ids = {a.task_id for a in await self._open_actions() if a.task_id}
        week_ago = self.rt.clock.now_utc() - timedelta(days=7)
        out = []
        for task in await self.rt.tasks.list():
            if key and task.project != key:
                continue
            if task.state == "done" and (task.updated_at or week_ago) < week_ago:
                continue  # the board keeps a week of done tasks
            out.append(await self._card(task, open_ids))
        return out

    async def task(self, task_id: str) -> m.TaskDetailOut:
        task = await self.rt.tasks.get(task_id.upper()) or await self.rt.tasks.get(task_id)
        if task is None:
            raise NotFound(f"no task {task_id}")
        open_actions = [a for a in await self._open_actions() if a.task_id == task.id]
        card = await self._card(task, {task.id} if open_actions else set())
        summary = await self.rt.task_memory.summary(task.id)
        checks = []
        for fact in await self.rt.memory.facts(task.id):
            key = str(fact["key"])
            if key.startswith("_") or key == "participants":
                continue
            status = str(fact["status"]).lower()
            checks.append(
                m.CheckOut(
                    label=key.replace("_", " ").capitalize(),
                    status="pass" if status in FACT_PASS else "fail" if status in FACT_FAIL else "unknown",
                    detail=await self._restore(str(fact["value"]), task.id),
                )
            )
        precedents = []
        try:
            for hit in await self.rt.knowledge.precedents(await self.rt.task_memory.card(task.id), k=3):
                precedents.append(
                    m.PrecedentOut(id=str(hit.meta.get("task_id") or hit.ref), text=hit.snippet or hit.title)
                )
        except Exception:
            log.warning("precedent search failed", extra={"task": task.id})
        events = []
        for row in reversed(await self.rt.memory.task_events(task.id, limit=60)):
            events.append(await self._event(row, task.id))
        return m.TaskDetailOut(
            task=card,
            kind=task.kind.replace("_", " ").capitalize(),
            recipe_label=self._recipe_name(task.recipe),
            caller=await self._real(task.caller_token, task.id),
            affected=await self._real(task.affected_token, task.id),
            site=task.site or "—",
            priority=task.priority if task.priority != "normal" else "—",
            summary=await self._restore(summary.text, task.id) if summary.text else None,
            checks=checks,
            precedents=precedents,
            events=events,
            approval=await self._approval(open_actions[0]) if open_actions else None,
        )

    async def _event(self, row: dict[str, Any], task_id: str) -> m.EventOut:
        actor = str(row["actor"])
        channel = str(row.get("channel") or "")
        who: Literal["agent", "you", "system", "caller"]
        if actor in ("agent", "worker"):
            who, name = "agent", "Agent"
        elif actor == "user" or channel == "console":
            who, name = "you", "You"
        elif actor in ("system", "scheduler", "source_system") or channel == "ledger":
            who, name = "system", {"ledger": "Ledger"}.get(channel, channel.capitalize() or "System")
        else:
            who, name = "caller", await self._restore(actor, task_id)
        text = humanise(channel, str(row.get("text_translated") or row["text_pseudonymised"]))
        return m.EventOut(
            time=self._when(row["at"]), who=who, name=name, text=await self._restore(text, task_id)
        )

    async def message(self, task_id: str, text: str) -> m.EventOut:
        """The user's words to this task's agent. Queued as a console event on the task (R17 in 2.14)."""
        task = await self.rt.tasks.get(task_id)
        if task is None:
            raise NotFound(f"no task {task_id}")
        if task.state == "done":
            raise Conflict(f"{task_id} is done; reopen it in its source system first")
        now = self.rt.clock.now_utc()
        result = await self.rt.coordinator.ingest(
            Event(
                source="api",
                kind="api",
                external_id=f"console:{new_ulid()}",
                received_at=now,
                payload={"text": text, "sender": "user", "channel": "console"},
                task_hint=task.id,
            ),
            "api",
        )
        if result.event_id is not None:
            # In the thread at once; the worker's own record of the same event is skipped (source_ref).
            await self.rt.task_memory.record_trigger(task.id, {"event_id": result.event_id, "source": "api"})
        return m.EventOut(time=self._when(now), who="you", name="You", text=text)

    async def mark_done(self, task_id: str) -> m.TaskCardOut:
        task = await self.rt.tasks.get(task_id)
        if task is None:
            raise NotFound(f"no task {task_id}")
        if task.state == "done":
            return await self._card(task, set())
        try:
            task = await self.rt.ledger.move_to(task.id, "done", "user", "closed in the console")
        except IllegalTransition as exc:
            raise Conflict(str(exc)) from exc
        # Nothing drafted for a closed task may still go out.
        await self.rt.approvals.invalidate_for_task(task.id, "system", reason="the task was closed")
        return await self._card(task, set())

    # ------------------------------------------------------------ approvals

    async def _approval(self, action: ActionRequest) -> m.ApprovalOut:
        card, status = await self.rt.approvals.card(action.id)
        args = json.loads(action.args_json)
        task = await self.rt.tasks.get(action.task_id) if action.task_id else None
        now = self.rt.clock.now_utc()
        left = (action.expires_at - now) if action.expires_at else None
        if left is None:
            label = "No expiry"
        elif left.total_seconds() <= 0:
            label = "Expired"
        elif left < timedelta(hours=1):
            label = f"Expires in {max(1, int(left.total_seconds() // 60))} min"
        else:
            label = f"Expires in {int(left.total_seconds() // 3600)} h"
        to = args.get("to") or args.get("recipients") or args.get("dataset_id") or args.get("dataflow_id")
        to_text = ", ".join(to) if isinstance(to, list) else str(to or "—")
        content = await self._restore(card.content, action.task_id)
        return m.ApprovalOut(
            id=action.id,
            task_id=action.task_id,
            task_title=task.title if task else None,
            kind=KIND_LABEL.get(action.tool, action.tool.replace("_", " ").capitalize()),
            to=to_text,
            via=VIA_LABEL.get(action.tool, "Power BI" if "refresh" in action.tool else "Agent"),
            recipe=self._recipe_name(task.recipe) if task and task.recipe else None,
            text=content,
            content_hash=action.content_hash,
            status=status,
            edited=action.edited,
            expires_at=action.expires_at,
            expires_label=label,
            urgent=left is not None and left < timedelta(hours=4),
            note=card.note,
        )

    async def approvals(self) -> list[m.ApprovalOut]:
        out = [await self._approval(a) for a in await self._open_actions()]
        out.sort(key=lambda a: a.expires_at or datetime.max.replace(tzinfo=UTC))
        return out

    async def approve(self, action_id: str, content_hash: str) -> m.DecisionOut:
        try:
            outcome = await self.rt.approvals.approve(action_id, content_hash, actor="console")
        except KeyError as exc:
            raise NotFound(f"no approval {action_id}") from exc
        except ApprovalConflict as exc:
            raise Conflict(str(exc)) from exc
        return _decision(outcome, "Approved. It runs now." if outcome.changed else "Already approved.")

    async def edit(self, action_id: str, text: str, content_hash: str | None) -> m.DecisionOut:
        action = await self.rt.actions.get(action_id)
        if action is None:
            raise NotFound(f"no approval {action_id}")
        # The console shows real names; what is stored and hashed is what will be sent.
        try:
            outcome = await self.rt.approvals.edit(
                action_id, text, actor="console", expected_hash=content_hash
            )
        except ApprovalConflict as exc:
            raise Conflict(str(exc)) from exc
        message = "Edit saved. Approve the edited version to send it." if outcome.changed else "No change."
        return _decision(outcome, message)

    async def reject(self, action_id: str, reason: str) -> m.DecisionOut:
        try:
            outcome = await self.rt.approvals.reject(action_id, reason, actor="console")
        except KeyError as exc:
            raise NotFound(f"no approval {action_id}") from exc
        except ApprovalConflict as exc:
            raise Conflict(str(exc)) from exc
        return _decision(outcome, "Rejected. Nothing was sent." if outcome.changed else "Already rejected.")

    # ------------------------------------------------------------ recipes

    def _recipe_name(self, slug: str | None) -> str:
        if not slug:
            return "—"
        return slug.replace("-", " ").capitalize()

    def tools(self) -> list[m.ToolOut]:
        reg = self.rt.registry
        out = []
        for name in reg.names():
            tool = reg.get(name)
            assert tool is not None
            mode = reg.policy.mode_of(name) or tool.mode
            out.append(m.ToolOut(name=name, mode=MODE_LABEL[mode], locked=reg.must_hand_off(name)))
        for name, entry in reg.policy.tools.items():
            if reg.get(name) is None and entry.mode == "hand_off":
                out.append(m.ToolOut(name=name, mode="Hand off", locked=True))
        return out

    async def recipes(self) -> list[m.RecipeOut]:
        states = {r["recipe"]: r for r in await self.rt.recipes_state.all()}
        out = []
        for path in sorted(self.rt.loader.directory.glob("*.yaml")):
            out.append(self._recipe_out(path, states.get(path.stem)))
        return out

    async def recipe(self, slug: str) -> m.RecipeOut:
        path = self._recipe_path(slug)
        if not path.exists():
            raise NotFound(f"no recipe {slug}")
        return self._recipe_out(path, await self.rt.recipes_state.get(slug))

    def _recipe_path(self, slug: str) -> Path:
        if not re.match(r"^[a-z0-9][a-z0-9_-]{1,48}$", slug):
            raise NotFound(f"no recipe {slug}")
        return self.rt.loader.directory / f"{slug}.yaml"

    def _recipe_out(self, path: Path, state: dict[str, Any] | None) -> m.RecipeOut:
        raw: dict[str, Any] = {}
        error = (state or {}).get("error") or self.rt.loader.errors.get(path.stem)
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            raw = loaded if isinstance(loaded, dict) else {}
        except (OSError, yaml.YAMLError) as exc:
            error = error or f"cannot read the file: {exc}"
        trigger: dict[str, Any] = raw["trigger"] if isinstance(raw.get("trigger"), dict) else {}
        stats = json.loads((state or {}).get("stats_json") or "{}")
        last = (state or {}).get("last_run_at")
        status = str(raw.get("status") or "off")
        return m.RecipeOut(
            slug=path.stem,
            name=str(raw.get("title") or path.stem.replace("-", " ").capitalize()),
            project=PROJECT_LABEL.get(str(raw.get("project")), "Core"),
            trigger=_trigger_text(trigger),
            trigger_label=_trigger_label(trigger),
            model=str(raw.get("model") or "haiku").capitalize(),
            status="Off" if error else STATUS_LABEL.get(status, "Off"),
            last_run=self._when(last) if last else "—",
            tools=[str(t) for t in raw.get("tools") or []],
            instructions=str(raw.get("instructions") or "").strip(),
            output=str(raw.get("output") or "none"),
            output_label=OUTPUT_LABEL.get(str(raw.get("output") or "none"), "Task thread"),
            has_steps=bool(raw.get("steps")),
            file=f"recipes/{path.name}",
            error=error,
            runs=int(stats.get("runs", 0)),
            would_do=int(stats.get("would_do", 0)),
        )

    async def save_recipe(self, slug: str, body: m.RecipeIn) -> m.RecipeOut:
        """Create or change a recipe file. New recipes start in Shadow; Hand off tools are refused.

        Fields the console does not edit (steps, data classes, context, creates_task, exclusive_with,
        the header comment) are kept from the file.
        """
        path = self._recipe_path(slug)
        existing: dict[str, Any] = {}
        header = ""
        if path.exists():
            text = path.read_text(encoding="utf-8")
            header = "".join(line + "\n" for line in text.splitlines() if line.startswith("#"))
            loaded = yaml.safe_load(text)
            existing = loaded if isinstance(loaded, dict) else {}
        refused = [t for t in body.tools if self.rt.registry.must_hand_off(t)]
        if refused:
            raise Refused(f"Hand off tools can't be in a recipe: {', '.join(refused)}")
        unknown = [t for t in body.tools if self.rt.registry.get(t) is None]
        if unknown:
            raise Refused(f"unknown tools: {', '.join(unknown)}")
        trigger = _parse_trigger(body.trigger)
        status = body.status or STATUS_LABEL.get(str(existing.get("status")), "Shadow")
        if not existing:
            status = "Shadow"  # a new recipe drafts and logs until you switch it to Live
        data = {
            **existing,
            "name": slug,
            "title": body.name,
            "project": PROJECT_KEY[body.project],
            "status": status.lower(),
            "trigger": trigger,
            "model": body.model.lower(),
            "tools": body.tools,
            "instructions": body.instructions,
            "output": body.output,
        }
        if existing and existing.get("version"):
            data["version"] = int(existing["version"]) + 1
        try:
            RecipeSpec.model_validate(data)
        except ValidationError as exc:
            raise Refused(_first_error(exc)) from exc
        dumped = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(header + dumped, encoding="utf-8")
        tmp.replace(path)
        await self.rt.load_recipes()
        return await self.recipe(slug)

    async def set_recipe_status(self, slug: str, status: str) -> m.RecipeOut:
        """Only the status line changes; comments and everything else stay as written."""
        path = self._recipe_path(slug)
        if not path.exists():
            raise NotFound(f"no recipe {slug}")
        text = path.read_text(encoding="utf-8")
        new, count = re.subn(r"(?m)^status:\s*\S+", f"status: {status.lower()}", text, count=1)
        if count == 0:
            raise Refused("the recipe file has no status line")
        if status == "Live" and slug in self.rt.loader.errors:
            raise Refused(f"fix the recipe first: {self.rt.loader.errors[slug]}")
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(new, encoding="utf-8")
        tmp.replace(path)
        await self.rt.load_recipes()
        return await self.recipe(slug)

    async def test_recipe(self, slug: str) -> m.RecipeTestOut:
        """A shadow run on fixture data in a throwaway database: nothing real is read or changed."""
        from agent.recipes.harness import run_recipe_on_fixture

        path = self._recipe_path(slug)
        if not path.exists():
            raise NotFound(f"no recipe {slug}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        trigger = raw.get("trigger") if isinstance(raw, dict) else {}
        source = Trigger.model_validate(trigger).source if isinstance(trigger, dict) and trigger else None
        fixture, sample = None, "a scheduled run on the sample day"
        if source == "mail":
            fixture, sample = "emails/messages.json#AAMkAGFAKE0012", "sample mail: a question about a report"
        elif source is not None:
            sample = f"a run with no {source} sample"
        try:
            result = await run_recipe_on_fixture(slug, fixture, recipes_dir=self.rt.loader.directory)
        except Exception as exc:
            return m.RecipeTestOut(
                sample=sample,
                proposals=[],
                notes=[f"The test could not run: {str(exc)[:200]}"],
                needs_approval=False,
            )
        proposals = []
        for p in result.proposals:
            tool = self.rt.registry.get(p.tool)
            mode = self.rt.registry.policy.mode_of(p.tool) or (tool.mode if tool else "ask")
            proposals.append(m.ProposalOut(tool=p.tool, mode=MODE_LABEL[mode], note=p.purpose or ""))
        return m.RecipeTestOut(
            sample=sample,
            proposals=proposals,
            notes=[*result.notes[:10], "Shadow run on sample data: nothing was sent or changed."],
            needs_approval=any(RANK.get(TO_MODE.get(x.mode, "ask"), 2) >= RANK["ask"] for x in proposals),
        )

    # ------------------------------------------------------------ activity

    async def activity(self, mode: str | None = None) -> list[m.ActivityOut]:
        rows = await self.rt.audit.recent(ACTIVITY_ROWS)
        groups: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for row in rows:  # newest first
            key = str(row["action_id"]) if row.get("action_id") else f"row:{row['id']}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(row)
        wanted = TO_MODE.get(mode or "", None) if mode and mode != "All" else None
        out = []
        for key in order:
            group = groups[key]
            latest, first = group[0], group[-1]
            tool = str(first["tool"]).removeprefix("undo:")
            row_mode = str(first["mode"])
            if wanted and row_mode != wanted:
                continue
            undone = any(str(r["result"]) == "undo: done" for r in group)
            done_row = next(
                (r for r in group if str(r["result"]) == "done" and not str(r["tool"]).startswith("undo:")),
                None,
            )
            undo: Literal["available", "done", "none"] = "none"
            if done_row is not None and row_mode == "report":
                op = await self.rt.undo_repo.for_audit(int(done_row["id"]))
                if op is not None:
                    undo = "done" if op["status"] == "done" or undone else "available"
            out.append(
                m.ActivityOut(
                    audit_id=int(done_row["id"]) if done_row else int(first["id"]),
                    time=self._when(first["at"]),
                    at=first["at"],
                    ref=str(first.get("task_id") or _recipe_ref(first.get("recipe")) or "Agent"),
                    action=await self._action_text(first),
                    tool=tool,
                    mode=MODE_LABEL.get(row_mode, "Ask"),
                    result="Undone" if undone else _result_text(str(latest["result"])),
                    undo=undo,
                )
            )
        return out[:300]

    async def _action_text(self, row: dict[str, Any]) -> str:
        tool = str(row["tool"])
        if tool.startswith("control:"):
            what = tool.removeprefix("control:")
            return CONTROL_LABEL.get(what, what.replace("_", " ").capitalize())
        return await self.rt.views.label({**row, "tool": tool.removeprefix("undo:")})

    async def undo(self, audit_id: int) -> m.UndoOut:
        row = await self.rt.audit.get(audit_id)
        if row is None:
            raise NotFound(f"no activity {audit_id}")
        if str(row["mode"]) != "report":
            raise Refused("only actions the agent did on its own (Report) can be undone here")
        result = await self.rt.undo.undo(audit_id, actor="user")
        if result.status == "refused":
            raise Conflict(result.message)
        return m.UndoOut(status=result.status, message=result.message)

    async def workers(self) -> m.WorkersOut:
        pool = await self.rt.pool.state()
        state = await self.rt.controls.state()
        watching = [
            n
            for a, n in (("outlook", "Outlook"), ("teams", "Teams"), ("powerbi", "Power BI"))
            if a in self.rt.adapters
        ]
        coordinator = (
            "Paused" if state.paused else ("Watching " + ", ".join(watching) if watching else "Idle")
        )
        out = []
        for i, w in enumerate(pool.workers, start=1):
            busy = w.job_id is not None
            doing = " · ".join(x for x in (w.task_id, w.step or w.kind) if x) if busy else "Idle"
            note = f"{int(w.elapsed_seconds or 0)} s" if busy else "—"
            out.append(m.WorkerOut(name=f"Worker {i}", busy=busy, doing=doing or "Working", note=note))
        if not out:
            out = [
                m.WorkerOut(name=f"Worker {i}", busy=False, doing="Not running", note="—")
                for i in range(1, self.rt.config.workers.pool_size + 1)
            ]
        return m.WorkersOut(coordinator=coordinator, queue=pool.queue_length, workers=out)

    # ------------------------------------------------------------ memory

    async def memory(self, section: str) -> m.MemoryOut:
        if section == "personal":
            return m.MemoryOut(
                section=section,
                profile=self.rt.personal.read_file("profile.md"),
                preferences=self.rt.personal.read_file("preferences.md"),
                style_samples=self.rt.personal.style_count(),
            )
        if section == "directory":
            return m.MemoryOut(section=section, directory=await self._directory())
        if section == "lessons":
            rows = await self.rt.lessons.by_status("proposed", "active")
            return m.MemoryOut(
                section=section,
                lessons=[
                    m.LessonOut(
                        id=str(r["id"]),
                        text=str(r["text"]),
                        source=str(r.get("source_ref") or f"Scope: {r['scope']}"),
                        status="approved" if r["status"] == "active" else str(r["status"]),
                    )
                    for r in rows
                ],
            )
        if section == "sources":
            return m.MemoryOut(section=section, sources=await self._sources())
        if section == "tasks":
            return m.MemoryOut(section=section)
        raise NotFound(f"no memory section {section}")

    async def _directory(self) -> list[m.DirectoryRow]:
        by_site: dict[str, dict[str, Any]] = {}
        for row in await self.rt.directory.all():
            site = row.get("site")
            if not site:
                continue
            entry = by_site.setdefault(site, {"km": [], "lba": [], "name": row.get("team") or "", "at": None})
            role = str(row.get("role") or "").upper()
            if role in ("KM", "LBA"):
                entry[role.lower()].append(str(row["name"]))
            at = row["updated_at"]
            entry["at"] = at if entry["at"] is None or at < entry["at"] else entry["at"]
        now = self.rt.clock.now_utc()
        out = []
        for site, e in sorted(by_site.items()):
            at = e["at"]
            if at is not None and at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            out.append(
                m.DirectoryRow(
                    site=site,
                    name=e["name"] or "—",
                    km=", ".join(e["km"]) or "—",
                    lba=", ".join(e["lba"]) or "—",
                    last_checked=f"{self._local(at):%d %b}" if at else "—",
                    stale=at is not None and now - at > timedelta(days=STALE_DAYS),
                    source="Directory",
                )
            )
        return out

    async def _sources(self) -> list[m.SourceOut]:
        labels = {"precedent": "Closed tasks saved as precedents", "workbook": "Doxis incident workbook"}
        query = select(s.documents.c.source, func.count()).group_by(s.documents.c.source)
        async with self.rt.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [
            m.SourceOut(source=labels.get(str(src), str(src)), items=int(n), last_indexed="—")
            for src, n in rows
        ]

    async def lesson(self, lesson_id: str, action: str) -> m.LessonOut:
        row = await self.rt.lessons.get(lesson_id)
        if row is None:
            raise NotFound(f"no lesson {lesson_id}")
        to = "active" if action == "approve" else "discarded"
        if row["status"] != to and not await self.rt.lessons.decide(lesson_id, "proposed", to):
            raise Conflict(f"this lesson is already {row['status']}")
        await self.rt.audit.append(
            tool=f"lesson:{action}", mode="silent", args_digest="-", result="done", actor="user"
        )
        return m.LessonOut(
            id=lesson_id,
            text=str(row["text"]),
            source=str(row.get("source_ref") or ""),
            status="approved" if to == "active" else to,
        )

    # ------------------------------------------------------------ connectors

    async def connectors(self) -> list[m.ConnectorOut]:
        health = {r["connector"]: r for r in await self.rt.health.all()}
        out = []
        for adapter, name, via, can, scopes in CONNECTORS:
            row = health.get(adapter)
            present = adapter in self.rt.adapters or (adapter == "slack" and "slack" in health)
            status: ConnStatus
            if self.fake and adapter in self.rt.adapters:
                status, label = "ok", "Sample data"
            elif row is not None:
                status, label = HEALTH_STATUS.get(str(row["status"]), ("part", str(row["status"])))
            elif present:
                status, label = "pend", "Not checked yet"
            else:
                status, label = "pend", "Not set up"
            detail = None
            if row is not None and row.get("last_error") and status != "ok":
                detail = str(row["last_error"])[:200]
            out.append(
                m.ConnectorOut(
                    name=name,
                    status=status,
                    status_label=label,
                    via=via,
                    can=can,
                    scopes=scopes,
                    detail=detail,
                )
            )
        return out

    # ------------------------------------------------------------ settings

    def settings(self) -> m.SettingsOut:
        cfg = self.rt.config
        rows = []
        for group in POLICY_GROUPS:
            rows.append(
                m.PolicyRow(
                    key=group.key,
                    label=group.label,
                    mode=group_mode(group, self.rt.registry, cfg),
                    options=list(group.options),
                    locked=group.options == ("Hand off",),
                    reaches_people=group.reaches_people,
                    tools=list(group.tools),
                )
            )
        return m.SettingsOut(
            work_start=cfg.work.start,
            work_end=cfg.work.end,
            brief=cfg.schedule.brief,
            nightly_digest=cfg.schedule.nightly_digest,
            checkin_every_minutes=cfg.schedule.checkin_every_minutes,
            max_pushes_per_day=cfg.notifications.max_pushes_per_day,
            quiet_start=cfg.notifications.quiet_hours.start,
            quiet_end=cfg.notifications.quiet_hours.end,
            default_model=cfg.models.default.capitalize(),
            sonnet_for=list(cfg.models.sonnet_for),
            sonnet_daily_limit=cfg.models.sonnet_daily_limit,
            data_rule=cfg.data_rule.level,
            data_rule_approved=cfg.data_rule.level != "metadata",
            policy=rows,
        )

    async def save_settings(self, body: m.SettingsIn) -> m.SettingsOut:
        changes = body.model_dump(exclude_none=True)
        policy_rows = changes.pop("policy", None) or {}
        overrides, modes = await load_overrides(self.rt.kv)
        retry = None
        for key, mode in policy_rows.items():
            group = GROUPS.get(key)
            if group is None:
                raise Refused(f"unknown policy row {key!r}")
            try:
                check_row(group, mode)
            except ConfigError as exc:
                raise Refused(str(exc)) from exc
            if group.setting == "retry_own_transient":
                retry = mode == "Report"
                continue
            for tool in group.tools:
                modes[tool] = TO_MODE[mode]
        if retry is not None:
            changes["retry_own_transient"] = retry
        start = changes.get("work_start", self.rt.config.work.start)
        end = changes.get("work_end", self.rt.config.work.end)
        if start >= end:
            raise Refused("working hours must end after they start")
        merged = {**overrides, **changes}
        base = self.rt.config
        try:
            config = apply_overrides(base, changes)
            policy = policy_with(self.rt.registry.policy, {t: md for t, md in modes.items()})
            self.rt.registry.set_policy(policy)
        except (ConfigError, AgentError) as exc:
            raise Refused(str(exc)) from exc
        await self.rt.kv.set(OVERRIDES_KEY, json.dumps(merged, sort_keys=True))
        await self.rt.kv.set(POLICY_KEY, json.dumps(modes, sort_keys=True))
        self.rt.reconfigure(config)
        await self.rt.audit.append(
            tool="control:settings", mode="silent", args_digest="-", result="done", actor="user"
        )
        return self.settings()

    # ------------------------------------------------------------ controls

    async def pause(self) -> m.ControlOut:
        await self.rt.controls.pause("user")
        return m.ControlOut(paused=True, message="Paused. Nothing runs until you resume.")

    async def resume(self) -> m.ControlOut:
        result = await self.rt.controls.resume("user")
        extra = f" {len(result.released)} held actions released." if result.released else ""
        return m.ControlOut(paused=False, message="Resumed. Catching up on what was missed." + extra)

    async def checkin(self) -> m.ControlOut:
        try:
            await self.rt.controls.checkin_now("user")
        except ControlError as exc:
            raise Conflict(str(exc)) from exc
        return m.ControlOut(
            paused=await self.rt.controls.paused(), message="Check-in started. Results arrive in your DM."
        )

    async def request_reset(self) -> m.ResetTokenOut:
        if not await self.rt.controls.paused():
            raise Conflict("pause the agent before a reset")
        return m.ResetTokenOut(token=await self.rt.controls.request_reset("user"))

    async def reset(self, token: str) -> m.ControlOut:
        try:
            result = await self.rt.controls.reset(token, "user")
        except ControlError as exc:
            raise Conflict(str(exc)) from exc
        return m.ControlOut(
            paused=True,
            message=f"Reset done: {result.tasks} tasks and their memory deleted. The audit log is kept.",
        )


# ---------------------------------------------------------------- helpers


def _day_args() -> Any:
    from agent.recipes.views import DayArgs

    return DayArgs()


def _decision(outcome: Any, message: str) -> m.DecisionOut:
    return m.DecisionOut(
        id=outcome.action_id,
        status=outcome.status,
        content_hash=outcome.content_hash,
        changed=outcome.changed,
        message=message,
    )


def _greeting(hour: int) -> str:
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"


def _first_name(profile: str) -> str | None:
    """The first word of the profile's first non-heading line ("Udit · App developer ...")."""
    for line in profile.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "<!--", "-", "*")):
            continue
        word = re.split(r"[\s·,|]+", line)[0]
        return word if word.isalpha() and len(word) < 30 else None
    return None


STATE_WORDS = {
    "new": "New",
    "in_progress": "In progress",
    "needs_you": "Needs you",
    "waiting": "Waiting on others",
    "scheduled": "Scheduled",
    "done": "Done",
    "failed": "Failed",
}
DID = {
    "send_mail": "Sent the email",
    "post_chat": "Posted the message",
    "save_draft": "Saved the draft in Outlook",
    "move_mail": "Moved the mail",
    "flag_mail": "Flagged the mail",
    "categorise": "Set categories on the mail",
    "add_hold": "Added a calendar hold",
    "trigger_refresh": "Started the refresh",
    "trigger_dataflow_refresh": "Started the dataflow refresh",
    "open_reply_task": "Opened a reply task",
}
VERB = {
    "sent": "send",
    "posted": "post",
    "saved": "save",
    "moved": "move",
    "flagged": "flag",
    "set": "set",
    "added": "add",
    "started": "start",
    "opened": "open",
}
OUTCOME_WORDS = {
    "done": "{did}.",
    "shadow": "Would have done: {what} (Shadow: logged only).",
    "proposed": "Drafted: {what}. Waiting for your approval.",
    "rejected": "You rejected: {what}.",
    "expired": "Approval expired: {what}.",
    "handed_off": "Handed to you: {what}.",
    "blocked": "Blocked by the policy gate: {what}.",
    "failed": "Failed: {what}.",
    "invalidated": "No longer current: {what}.",
    "held": "Held while paused: {what}.",
}
_STATE_RE = re.compile(r"^state (\w+) -> (\w+)(?::\s*(.*))?$", re.DOTALL)
_OUTCOME_RE = re.compile(r"^([a-z_]+): ([a-z_]+)(?: \((.*)\))?$")


def humanise(channel: str, text: str) -> str:
    """Ledger and graph notes are written for the log; the console says them in plain words."""
    if channel == "ledger":
        match = _STATE_RE.match(text.strip())
        if match:
            to = STATE_WORDS.get(match.group(2), match.group(2))
            reason = (match.group(3) or "").strip()
            return f"Moved to {to}" + (f" ({reason})." if reason else ".")
        return text
    if channel != "graph":
        return text
    out = []
    for part in text.split("; "):
        match = _OUTCOME_RE.match(part.strip())
        if not match:
            out.append(part)
            continue
        tool, status = match.group(1), match.group(2)
        did = DID.get(tool, tool.replace("_", " ").capitalize())
        what = did[0].lower() + did[1:]
        what = re.sub(r"^(\w+?)(ed|t)\b", lambda m: VERB.get(m.group(0), m.group(0)), what)
        out.append(OUTCOME_WORDS.get(status, "{what}: " + status).format(did=did, what=what))
    return " ".join(out)


def _state_line(state: str) -> str:
    return {
        "new": "Queued",
        "in_progress": "Working on it",
        "needs_you": "Needs your decision",
        "waiting": "Waiting on others",
        "scheduled": "Scheduled",
        "done": "Done",
        "failed": "Failed; needs a look",
    }.get(state, state)


def _recipe_ref(recipe: Any) -> str:
    return str(recipe).replace("-", " ").capitalize() if recipe else ""


def _result_text(result: str) -> str:
    if result.startswith("decision:"):
        status = result.removeprefix("decision:").split(" ", 1)[0]
        return {
            "ready": "Queued",
            "proposed": "Waiting for approval",
            "handed_off": "Handed to you",
            "shadow": "Shadow: logged only",
            "blocked": "Blocked",
            "held": "Held while paused",
            "done": "Done",
        }.get(status, status.replace("_", " ").capitalize())
    if result.startswith("invalidated"):
        return "No longer current"
    if result.startswith("undo: refused"):
        return "Undo refused"
    if result.startswith("failed"):
        return "Failed"
    return {
        "done": "Done",
        "approved": "Approved",
        "rejected": "Rejected",
        "edited": "Edited",
        "expired": "Expired",
    }.get(result, result[:60])


def _trigger_text(trigger: dict[str, Any]) -> str:
    return ", ".join(f"{k}: {v}" for k, v in trigger.items())


def _trigger_label(trigger: dict[str, Any]) -> str:
    if not trigger:
        return "—"
    if "schedule" in trigger:
        value = str(trigger["schedule"])
        return "Schedule · " + NAMED_SCHEDULE_LABEL.get(value, f"weekdays {value}")
    if "poll" in trigger:
        every = f" every {trigger['every']}" if trigger.get("every") else ""
        return f"Poll · {str(trigger['poll']).replace('_', ' ')}{every}"
    if "event" in trigger:
        return f"Event · {trigger['event']}" + (" matching a pattern" if trigger.get("match") else "")
    return _trigger_text(trigger)


def _parse_trigger(text: str) -> dict[str, Any]:
    """'schedule: 09:00' or '{poll: mail, every: 10m}' -> a validated trigger mapping."""
    raw = text.strip()
    if not raw.startswith("{"):
        raw = "{" + raw + "}"
    try:
        # Times like 09:00 must stay strings: quote bare HH:MM values before YAML reads them.
        raw = re.sub(r"(?<![\"'\d])(\b[0-2]\d:[0-5]\d\b(?:\s*,\s*[0-2]\d:[0-5]\d)*)", r'"\1"', raw)
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise Refused(f"the trigger is not readable: {exc}") from exc
    if not isinstance(value, dict):
        raise Refused("the trigger needs one of schedule, poll or event")
    value = {str(k): str(v) for k, v in value.items() if v is not None}
    try:
        Trigger.model_validate(value)
    except ValidationError as exc:
        raise Refused(_first_error(exc)) from exc
    return value


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ()))
    return f"{where}: {err['msg']}" if where else str(err["msg"])
