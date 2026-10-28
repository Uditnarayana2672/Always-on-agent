"""The policy gate (F06): every tool call is checked here before anything runs (golden rule 1).

check() is pure and synchronous: it returns a Decision and never calls an adapter. Rules, in order:

1. Unknown tool -> Ask (Hand off if its name says delete, password, grant ...).
2. Args must validate against the tool's model, else blocked.
3. Base mode from policy.yaml / the registration / the tool's mode_fn.
4. Hard rules -> Hand off: locked or hard-category tools; any final recipient (To, Cc, Bcc and the
   reply-all expansion) outside company_domains; any recipient who is not a task participant, a
   configured group or the user.
5. Args needing a higher data-rule level than allowed -> blocked.
6. A recipe or the model asking for a lower mode -> kept stricter, attempt noted.
7. Triggered by fetched content -> at least Ask, flagged as possible injection.
8. Recipe in shadow -> record only (nothing above Silent executes).
9. Agent paused -> held (nothing above Silent runs until resume).
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.adapters.base import Mode
from agent.core.config import DataRuleLevel
from agent.policy.modes import RANK, stricter
from agent.policy.registry import Registry, ToolDef

DecisionMode = Literal["silent", "report", "ask", "hand_off", "blocked"]

_DATA_RANK: dict[str, int] = {"metadata": 0, "text": 1, "chat": 2, "images": 3}
_RECIPIENT_KEYS = ("to", "cc", "bcc", "recipients", "recipient")


class GateContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str | None = None
    recipe: str | None = None
    recipe_stage: Literal["live", "shadow"] = "live"
    actor: str = "agent"  # agent | user | recipe name
    requested_mode: Mode | None = None  # what the recipe or model claimed the call needs
    triggered_by_fetched_content: bool = False
    paused: bool = False
    participants: frozenset[str] = frozenset()  # addresses on the task (callers, L2, stakeholders)
    reply_all_recipients: tuple[str, ...] = ()  # everyone a reply-all would reach, from the thread
    me: str | None = None
    facts: Mapping[str, Any] = Field(default_factory=dict)  # for mode_fn (owned datasets, last error)


class Decision(BaseModel):
    tool: str
    mode: DecisionMode
    reasons: list[str]
    args_digest: str
    args: dict[str, Any]
    flagged: bool = False  # possible injection
    lowering_refused: bool = False
    record_only: bool = False  # shadow: logged as would-do
    held: bool = False  # paused: waits for resume

    @property
    def executable_now(self) -> bool:
        """Can run without anyone's approval right now."""
        return self.mode in ("silent", "report") and not self.record_only and not self.held

    @property
    def needs_approval(self) -> bool:
        return self.mode == "ask" and not self.record_only


def digest_args(args: Mapping[str, Any]) -> str:
    """Stable sha256 over canonical JSON; the content hash approvals bind to."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Gate:
    def __init__(
        self,
        registry: Registry,
        company_domains: Iterable[str],
        allowed_groups: Mapping[str, str] | None = None,
        data_level: DataRuleLevel = "metadata",
    ) -> None:
        self._registry = registry
        self._domains = {d.strip().lower().lstrip("@") for d in company_domains if d.strip()}
        self._groups = {
            addr.strip().lower()
            for value in (allowed_groups or {}).values()
            for addr in value.split(",")
            if addr.strip()
        }
        self._data_level = data_level

    def check(self, tool: str, args: Mapping[str, Any] | BaseModel, ctx: GateContext) -> Decision:
        raw = args.model_dump(mode="json") if isinstance(args, BaseModel) else dict(args)
        tool_def = self._registry.get(tool)

        if tool_def is None:
            return self._finish(tool, self._unknown_mode(tool), ["unknown tool"], raw, raw, ctx)

        try:
            model = tool_def.args_model.model_validate(raw)
        except ValidationError as exc:
            fields = ", ".join(str(e["loc"][0]) for e in exc.errors() if e["loc"])
            return Decision(
                tool=tool,
                mode="blocked",
                reasons=[f"invalid arguments: {fields or 'model'}"],
                args_digest=digest_args(raw),
                args=raw,
            )
        normalised = model.model_dump(mode="json")

        mode, reasons = self._registry.base_mode(tool_def, model, ctx)

        if tool_def.hard is not None or self._registry.must_hand_off(tool):
            mode = "hand_off"
            reasons.append("never automated")

        recipients = self._recipients(tool_def, model, normalised, ctx, mode)
        outside = [r for r in recipients if not self._inside(r)]
        if outside:
            mode = "hand_off"
            reasons.append(f"{len(outside)} recipient(s) outside the company")
        strangers = [r for r in recipients if self._inside(r) and not self._known(r, ctx)]
        if strangers:
            mode = "hand_off"
            reasons.append(f"{len(strangers)} recipient(s) not on the task")

        if tool_def.data_level and _DATA_RANK[tool_def.data_level] > _DATA_RANK[self._data_level]:
            return Decision(
                tool=tool,
                mode="blocked",
                reasons=[*reasons, f"needs data rule {tool_def.data_level}, allowed {self._data_level}"],
                args_digest=digest_args(normalised),
                args=normalised,
            )

        return self._finish(tool, mode, reasons, normalised, raw, ctx)

    # ------------------------------------------------------------ rules 6-9

    def _finish(
        self,
        tool: str,
        mode: Mode,
        reasons: list[str],
        normalised: dict[str, Any],
        raw: dict[str, Any],
        ctx: GateContext,
    ) -> Decision:
        lowering_refused = False
        if ctx.requested_mode is not None:
            if RANK[ctx.requested_mode] < RANK[mode]:
                lowering_refused = True
                reasons.append(f"request for {ctx.requested_mode} refused")
            else:
                mode = ctx.requested_mode

        if self._registry.get(tool) is None:
            # Unknown tools: still catch outside recipients in obvious argument names.
            outside = [r for r in _addresses(raw) if not self._inside(r)]
            if outside:
                mode = "hand_off"
                reasons.append(f"{len(outside)} recipient(s) outside the company")

        flagged = ctx.triggered_by_fetched_content
        if flagged:
            mode = stricter(mode, "ask")
            reasons.append("triggered by fetched content: possible injection")

        record_only = ctx.recipe_stage == "shadow" and mode != "silent"
        if record_only:
            reasons.append("recipe in shadow: recorded only")
        held = ctx.paused and mode != "silent"
        if held:
            reasons.append("agent paused")

        return Decision(
            tool=tool,
            mode=mode,
            reasons=reasons,
            args_digest=digest_args(normalised),
            args=normalised,
            flagged=flagged,
            lowering_refused=lowering_refused,
            record_only=record_only,
            held=held,
        )

    # ------------------------------------------------------------ helpers

    def _unknown_mode(self, tool: str) -> Mode:
        return "hand_off" if self._registry.must_hand_off(tool) else "ask"

    def _recipients(
        self, tool_def: ToolDef, model: BaseModel, normalised: dict[str, Any], ctx: GateContext, mode: Mode
    ) -> list[str]:
        """The final list a send would reach. Reply-all counts the whole thread, not the draft's To.

        Tools that declare recipients are always checked. Otherwise addresses in to/cc/bcc-style args
        are checked only for tools that reach other people (Ask and up): saving a draft reaches no
        one, and the send that follows is checked on its own.
        """
        if tool_def.recipients is not None:
            found = tool_def.recipients(model)
        elif RANK[mode] >= RANK["ask"]:
            found = _addresses(normalised)
        else:
            found = []
        if normalised.get("reply_all"):
            found = [*found, *ctx.reply_all_recipients]
        out: dict[str, None] = {}
        for addr in found:
            clean = addr.strip().lower()
            if clean and clean != (ctx.me or "").lower():
                out.setdefault(clean, None)
        return list(out)

    def _inside(self, address: str) -> bool:
        domain = address.rsplit("@", 1)[-1].lower() if "@" in address else ""
        return any(domain == d or domain.endswith("." + d) for d in self._domains)

    def _known(self, address: str, ctx: GateContext) -> bool:
        return address in {p.lower() for p in ctx.participants} or address in self._groups


def _addresses(args: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for key in _RECIPIENT_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(v for v in value if isinstance(v, str))
    return out
