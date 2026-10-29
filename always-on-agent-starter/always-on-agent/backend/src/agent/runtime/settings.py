"""Console settings (F15): the subset of config and policy the user may change from the console.

Overrides live in kv, on top of config.yaml and policy.yaml, so the files stay the reviewed baseline:

- settings.overrides  schedule times, check-in interval, push cap, Sonnet options, the free refresh retry
- settings.policy     tool -> mode for the action-policy rows

What can never change here: the data rule level (it needs the client's written approval), Hand off
rows (locked), and anything that reaches another person, which never goes below Ask. Writes never go
below Report either: a Silent write would leave nothing in the digest to undo.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

from agent.core.config import Config
from agent.core.errors import ConfigError
from agent.db.repositories import KvRepo
from agent.policy.modes import Policy, PolicyEntry
from agent.policy.registry import Registry

OVERRIDES_KEY = "settings.overrides"
POLICY_KEY = "settings.policy"

UiMode = Literal["Silent", "Report", "Ask", "Hand off"]
TO_MODE: dict[str, str] = {"Silent": "silent", "Report": "report", "Ask": "ask", "Hand off": "hand_off"}
FROM_MODE: dict[str, UiMode] = {v: k for k, v in TO_MODE.items()}  # type: ignore[misc]


@dataclass(frozen=True)
class PolicyGroup:
    """One row of the Settings action-policy table: a plain label over the tools it sets."""

    key: str
    label: str
    tools: tuple[str, ...]
    options: tuple[UiMode, ...]  # what the row's select offers; one option means fixed
    reaches_people: bool = False
    setting: str | None = None  # rows backed by a config flag instead of tool modes


POLICY_GROUPS: tuple[PolicyGroup, ...] = (
    PolicyGroup(
        "read",
        "Read mail, chats, reports and files",
        (
            "read_mail",
            "search_mail",
            "read_report_email",
            "read_chat",
            "read_calendar",
            "get_transcript",
            "search_workbook",
            "search_memory",
            "ocr_local",
            "read_directory",
            "get_refresh_history",
            "read_sharepoint_list",
            "sql_count",
        ),
        ("Silent", "Report", "Ask"),
    ),
    PolicyGroup(
        "file_mail",
        "File or flag your own mail",
        ("move_mail", "flag_mail", "categorise", "set_flag"),
        ("Report", "Ask"),
    ),
    PolicyGroup("save_draft", "Save a draft", ("save_draft", "discard_own_draft"), ("Report", "Ask")),
    PolicyGroup(
        "calendar",
        "Add a task or a hold on your own calendar",
        ("add_hold", "discard_own_hold", "update_ledger", "open_reply_task"),
        ("Report", "Ask"),
    ),
    PolicyGroup(
        "retry_refresh",
        "Retry your own failed refresh (known transient error)",
        (),
        ("Report", "Ask"),
        setting="retry_own_transient",
    ),
    PolicyGroup(
        "send",
        "Send an email or chat message",
        ("send_mail", "post_chat", "add_comment_by_reply"),
        ("Ask",),
        reaches_people=True,
    ),
    PolicyGroup(
        "refresh",
        "Trigger a Prod refresh",
        ("trigger_refresh", "trigger_dataflow_refresh", "update_refresh_schedule"),
        ("Ask",),  # loosening is done by the retry row; a mode_fn cannot be overridden downwards
    ),
    PolicyGroup("shared_edit", "Edit a shared list or document", ("edit_shared_list",), ("Report", "Ask")),
    PolicyGroup("ticket_state", "Change a ServiceNow ticket state", ("change_ticket_state",), ("Hand off",)),
    PolicyGroup("delete", "Delete anything", ("delete_item",), ("Hand off",)),
    PolicyGroup("outside", "Message anyone outside the company", ("message_outside_company",), ("Hand off",)),
    PolicyGroup("access", "Grant access", ("grant_access",), ("Hand off",)),
    PolicyGroup("password", "Type a password", ("enter_credentials",), ("Hand off",)),
)
GROUPS = {g.key: g for g in POLICY_GROUPS}


# ---------------------------------------------------------------- config overrides


def apply_overrides(config: Config, overrides: dict[str, Any]) -> Config:
    """config.yaml plus the console's overrides. Unknown keys are ignored (an older agentd wrote them)."""
    work = config.work.model_copy(
        update={k: overrides[o] for k, o in (("start", "work_start"), ("end", "work_end")) if o in overrides}
    )
    schedule = config.schedule.model_copy(
        update={
            k: overrides[k] for k in ("brief", "nightly_digest", "checkin_every_minutes") if k in overrides
        }
    )
    notifications = config.notifications.model_copy(
        update={"max_pushes_per_day": overrides["max_pushes_per_day"]}
        if "max_pushes_per_day" in overrides
        else {}
    )
    models = config.models.model_copy(
        update={k: overrides[k] for k in ("sonnet_for", "sonnet_daily_limit") if k in overrides}
    )
    powerbi = config.powerbi.model_copy(
        update={"retry_own_transient": overrides["retry_own_transient"]}
        if "retry_own_transient" in overrides
        else {}
    )
    updated = config.model_copy(
        update={
            "work": work,
            "schedule": schedule,
            "notifications": notifications,
            "models": models,
            "powerbi": powerbi,
        }
    )
    # Validate the result as a whole (frozen model_copy skips validators).
    try:
        return Config.model_validate(updated.model_dump())
    except ValueError as exc:
        raise ConfigError(f"invalid settings: {exc}") from exc


def policy_with(base: Policy, modes: dict[str, str]) -> Policy:
    """policy.yaml with tool -> mode overrides. Locked rows are kept as they are."""
    tools = dict(base.tools)
    for tool, mode in modes.items():
        current = tools.get(tool)
        if current is not None and current.locked:
            continue
        tools[tool] = PolicyEntry(mode=mode)  # type: ignore[arg-type]
    return Policy(tools=tools)


async def load_overrides(kv: KvRepo) -> tuple[dict[str, Any], dict[str, str]]:
    raw, pol = await kv.get(OVERRIDES_KEY), await kv.get(POLICY_KEY)
    return (json.loads(raw) if raw else {}), (json.loads(pol) if pol else {})


def group_mode(group: PolicyGroup, registry: Registry, config: Config) -> UiMode:
    """The row's current mode: the strictest of its tools (what the agent actually does)."""
    if group.setting == "retry_own_transient":
        return "Report" if config.powerbi.retry_own_transient else "Ask"
    order = ["silent", "report", "ask", "hand_off"]
    modes = [registry.policy.mode_of(t) for t in group.tools]
    known = [m for m in modes if m is not None]
    if not known:
        return group.options[-1]
    return FROM_MODE[max(known, key=order.index)]


def check_row(group: PolicyGroup, mode: str) -> None:
    if mode not in group.options:
        if group.options == ("Hand off",):
            raise ConfigError(f"{group.label}: Hand off rows can't be changed")
        if group.reaches_people:
            raise ConfigError(f"{group.label}: anything other people see always asks first")
        raise ConfigError(f"{group.label}: choose one of {', '.join(group.options)}")
