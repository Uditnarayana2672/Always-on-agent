"""Tool registry (F06, F13).

Every tool is registered once with its args model, base mode, the adapter method it calls, an
optional args-dependent mode function and an optional inverse for undo. Registration enforces the
Hand off rules: a tool that deletes, handles credentials, grants access or changes a ticket state,
or that policy.yaml locks, can only ever be Hand off.

    @tool("move_mail", mode="report", adapter="outlook", method="move", inverse=MOVE_BACK)
    class MoveMail(BaseModel):
        message_id: str
        folder: str
"""

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from agent.adapters.base import Mode
from agent.core.config import DataRuleLevel
from agent.core.errors import PolicyViolation
from agent.policy.modes import RANK, Policy, load_policy

if TYPE_CHECKING:
    from agent.policy.gate import GateContext

Adapters = Mapping[str, Any]
HardCategory = Literal["delete", "credentials", "access_grant", "ticket_state", "outside_message"]

# Names that always mean Hand off, whatever a registration says.
HAND_OFF_NAME = re.compile(
    r"(^|_)(delete|remove|password|passwords|credential|credentials|grant|ticket_state)(_|$)"
)


@dataclass(frozen=True)
class Inverse:
    """How to undo a Report action.

    prepare runs before the action and captures what undo needs (for example the original folder);
    finish adds the action's result (for example the new draft id); check runs at undo time and
    returns a reason to refuse when the item changed since.
    """

    tool: str
    prepare: Callable[[Adapters, BaseModel], Awaitable[dict[str, Any]]]
    finish: Callable[[dict[str, Any], Any], dict[str, Any]] | None = None
    check: Callable[[Adapters, dict[str, Any]], Awaitable[str | None]] | None = None


ModeFn = Callable[[BaseModel, "GateContext"], Mode]
RecipientsFn = Callable[[BaseModel], list[str]]
Handler = Callable[[BaseModel, str], Awaitable[Any]]


@dataclass(frozen=True)
class ToolDef:
    name: str
    args_model: type[BaseModel]
    mode: Mode
    adapter: str | None = None
    method: str | None = None
    handler: Handler | None = None  # for tools that are not an adapter call (ledger, memory)
    mode_fn: ModeFn | None = None
    inverse: Inverse | None = None
    recipients: RecipientsFn | None = None  # final To + Cc + Bcc
    data_level: DataRuleLevel | None = None  # data-rule level the args need
    hard: HardCategory | None = None
    lookup: str | None = None  # adapter method: did a side effect with this idempotency key happen?
    description: str = ""
    tags: frozenset[str] = field(default_factory=frozenset)


class Registry:
    def __init__(self, policy: Policy | None = None) -> None:
        self._policy = policy
        self._tools: dict[str, ToolDef] = {}

    @property
    def policy(self) -> Policy:
        if self._policy is None:
            self._policy = load_policy()
        return self._policy

    def register(self, tool: ToolDef) -> ToolDef:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        must_hand_off = (
            tool.hard is not None
            or HAND_OFF_NAME.search(tool.name) is not None
            or self.policy.locked(tool.name)
        )
        if must_hand_off and tool.mode != "hand_off":
            raise PolicyViolation(f"{tool.name} is a Hand off action and cannot be registered as {tool.mode}")
        if must_hand_off and tool.mode_fn is not None:
            raise PolicyViolation(f"{tool.name} is Hand off; it cannot compute its mode")
        if tool.mode == "hand_off" and tool.inverse is not None:
            raise PolicyViolation(f"{tool.name} is Hand off; the agent never runs it, so it has no undo")
        if (tool.adapter is None) == (tool.handler is None) and tool.mode != "hand_off":
            raise ValueError(f"{tool.name} needs exactly one of adapter or handler")
        if tool.adapter is not None and tool.method is None:
            raise ValueError(f"{tool.name} names an adapter but no method")
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        name: str,
        *,
        mode: Mode,
        adapter: str | None = None,
        method: str | None = None,
        handler: Handler | None = None,
        mode_fn: ModeFn | None = None,
        inverse: Inverse | None = None,
        recipients: RecipientsFn | None = None,
        data_level: DataRuleLevel | None = None,
        hard: HardCategory | None = None,
        lookup: str | None = None,
        description: str = "",
    ) -> Callable[[type[BaseModel]], type[BaseModel]]:
        """Class decorator on the tool's Pydantic args model."""

        def decorate(args_model: type[BaseModel]) -> type[BaseModel]:
            self.register(
                ToolDef(
                    name=name,
                    args_model=args_model,
                    mode=mode,
                    adapter=adapter,
                    method=method,
                    handler=handler,
                    mode_fn=mode_fn,
                    inverse=inverse,
                    recipients=recipients,
                    data_level=data_level,
                    hard=hard,
                    lookup=lookup,
                    description=description or (args_model.__doc__ or "").strip(),
                )
            )
            return args_model

        return decorate

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def must_hand_off(self, name: str) -> bool:
        """True for names that are Hand off whether or not they are registered."""
        tool = self._tools.get(name)
        return (
            (tool is not None and (tool.hard is not None or tool.mode == "hand_off"))
            or HAND_OFF_NAME.search(name) is not None
            or self.policy.locked(name)
            or self.policy.mode_of(name) == "hand_off"
        )

    def base_mode(self, tool: ToolDef, args: BaseModel, ctx: "GateContext") -> tuple[Mode, list[str]]:
        """Mode before the gate's hard rules.

        policy.yaml has the final say over the registration's mode. A mode_fn may set the mode from
        the args (trigger_refresh is Report for the user's own dataset after a transient error) but
        only while policy.yaml has not made the tool stricter than its registration.
        """
        reasons: list[str] = []
        configured = self.policy.mode_of(tool.name)
        if configured is not None and configured != tool.mode:
            reasons.append(f"policy sets {configured}")
            if tool.mode_fn is None or RANK[configured] > RANK[tool.mode]:
                return configured, reasons
        mode = configured or tool.mode
        if tool.mode_fn is not None:
            computed = tool.mode_fn(args, ctx)
            if computed != mode:
                reasons.append(f"{computed} for these arguments")
            mode = computed
        return mode, reasons


REGISTRY = Registry()
tool = REGISTRY.tool
