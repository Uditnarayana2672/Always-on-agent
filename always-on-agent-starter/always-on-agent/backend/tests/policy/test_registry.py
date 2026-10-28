from pathlib import Path

import pytest
from pydantic import BaseModel

from agent.core.errors import ConfigError, PolicyViolation
from agent.policy.gate import GateContext
from agent.policy.modes import Policy, PolicyEntry, load_policy, stricter
from agent.policy.registry import REGISTRY, Registry, ToolDef, tool
from tests.policy.tools import MOVE_BACK, build


class Args(BaseModel):
    item_id: str


def empty() -> Registry:
    return Registry(Policy(tools={"locked_thing": PolicyEntry(mode="hand_off", locked=True)}))


async def _h(args: BaseModel, key: str) -> None:
    return None


def test_real_policy_loads_and_locks_hand_off() -> None:
    policy = load_policy()
    for name in (
        "delete_item",
        "change_ticket_state",
        "grant_access",
        "message_outside_company",
        "enter_credentials",
    ):
        assert policy.mode_of(name) == "hand_off" and policy.locked(name)
    assert policy.mode_of("send_mail") == "ask" and not policy.locked("send_mail")
    assert policy.mode_of("nope") is None


def test_policy_file_errors(tmp_path: Path) -> None:
    bad = tmp_path / "p.yaml"
    bad.write_text("tools:\n  x: { mode: ask, locked: true }\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="locked"):
        load_policy(bad)
    with pytest.raises(ConfigError):
        load_policy(tmp_path / "missing.yaml")


def test_stricter() -> None:
    assert stricter("report", "ask") == "ask"
    assert stricter("hand_off", "silent") == "hand_off"


@pytest.mark.parametrize("mode", ["silent", "report", "ask"])
@pytest.mark.parametrize(
    "name",
    [
        "delete_item",
        "delete_draft",
        "remove_member",
        "enter_password",
        "store_credentials",
        "grant_access",
        "set_ticket_state",
        "locked_thing",
    ],
)
def test_hand_off_tools_cannot_be_registered_lower(name: str, mode: str) -> None:
    with pytest.raises(PolicyViolation, match="Hand off"):
        empty().tool(name, mode=mode, handler=_h)(Args)  # type: ignore[arg-type]


@pytest.mark.parametrize("hard", ["delete", "credentials", "access_grant", "ticket_state", "outside_message"])
def test_hard_category_forces_hand_off(hard: str) -> None:
    with pytest.raises(PolicyViolation):
        empty().tool("tidy_up", mode="report", handler=_h, hard=hard)(Args)  # type: ignore[arg-type]
    reg = empty()
    reg.tool("tidy_up", mode="hand_off", hard=hard)(Args)  # type: ignore[arg-type]
    assert reg.must_hand_off("tidy_up")


def test_hand_off_cannot_compute_mode_or_undo() -> None:
    with pytest.raises(PolicyViolation, match="compute"):
        empty().tool("delete_x", mode="hand_off", mode_fn=lambda a, c: "report")(Args)
    with pytest.raises(PolicyViolation, match="no undo"):
        empty().tool("share_out", mode="hand_off", inverse=MOVE_BACK)(Args)


def test_registration_shape_errors() -> None:
    reg = empty()
    reg.tool("a", mode="report", handler=_h)(Args)
    with pytest.raises(ValueError, match="already registered"):
        reg.tool("a", mode="report", handler=_h)(Args)
    with pytest.raises(ValueError, match="exactly one"):
        reg.tool("b", mode="report")(Args)
    with pytest.raises(ValueError, match="exactly one"):
        reg.tool("c", mode="report", handler=_h, adapter="outlook", method="m")(Args)
    with pytest.raises(ValueError, match="no method"):
        reg.tool("d", mode="report", adapter="outlook")(Args)


def test_decorator_returns_model_and_records_description() -> None:
    class Documented(BaseModel):
        """Archive a ledger item."""

        item_id: str

    reg = empty()
    assert reg.tool("archive", mode="report", handler=_h)(Documented) is Documented
    found = reg.get("archive")
    assert isinstance(found, ToolDef) and found.description == "Archive a ledger item."
    assert reg.names() == ["archive"] and reg.get("missing") is None


def test_policy_overrides_registration() -> None:
    reg = Registry(Policy(tools={"move_mail": PolicyEntry(mode="ask"), "loose": PolicyEntry(mode="silent")}))
    reg.tool("move_mail", mode="report", handler=_h)(Args)
    reg.tool("loose", mode="report", handler=_h)(Args)
    move = reg.get("move_mail")
    loose = reg.get("loose")
    assert move and loose
    ctx = GateContext()
    assert reg.base_mode(move, Args(item_id="1"), ctx) == ("ask", ["policy sets ask"])
    assert reg.base_mode(loose, Args(item_id="1"), ctx)[0] == "silent"


def test_mode_fn_cannot_undercut_a_tightened_policy() -> None:
    reg = Registry(Policy(tools={"trigger_refresh": PolicyEntry(mode="hand_off")}))
    reg.tool("trigger_refresh", mode="ask", handler=_h, mode_fn=lambda a, c: "report")(Args)
    td = reg.get("trigger_refresh")
    assert td and reg.base_mode(td, Args(item_id="1"), GateContext())[0] == "hand_off"
    loose = Registry(Policy(tools={"trigger_refresh": PolicyEntry(mode="report")}))
    loose.tool("trigger_refresh", mode="ask", handler=_h, mode_fn=lambda a, c: "ask")(Args)
    td2 = loose.get("trigger_refresh")
    assert td2 and loose.base_mode(td2, Args(item_id="1"), GateContext())[0] == "ask"


def test_global_registry_loads_real_policy_lazily() -> None:
    assert isinstance(REGISTRY.policy, Policy)
    assert tool == REGISTRY.tool
    assert build().policy.locked("delete_item")
