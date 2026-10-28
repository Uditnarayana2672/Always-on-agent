from dataclasses import dataclass, field
from typing import Any

import pytest

from agent.policy.gate import Decision, Gate, GateContext, digest_args
from tests.policy import tools as t
from tests.policy.tools import CALLER, ME

L2 = "doxis-l2@corp.example"
OUTSIDER = "bob@partner.example"
STRANGER = "someone.else@corp.example"
TASK = frozenset({CALLER})

SEND = {"draft_id": "D1", "to": [CALLER]}


@dataclass
class Case:
    name: str
    tool: str
    args: dict[str, Any]
    ctx: dict[str, Any] = field(default_factory=dict)
    mode: str = "ask"
    flagged: bool = False
    record_only: bool = False
    held: bool = False
    lowering_refused: bool = False
    reason: str | None = None
    data_level: str = "metadata"


CASES = [
    # unknown tools
    Case("unknown tool is ask", "make_coffee", {"x": 1}, mode="ask", reason="unknown tool"),
    Case("unknown delete is hand off", "delete_folder", {"x": 1}, mode="hand_off"),
    Case("unknown password tool is hand off", "type_password", {}, mode="hand_off"),
    Case("unknown grant tool is hand off", "grant_role", {}, mode="hand_off"),
    Case("unknown tool with outside to", "notify", {"to": [OUTSIDER]}, mode="hand_off"),
    Case("unknown tool with outside single recipient", "notify", {"recipient": OUTSIDER}, mode="hand_off"),
    Case("unknown tool listed hand off in policy", "change_ticket_state", {}, mode="hand_off"),
    # base modes
    Case("silent read", "search_mail", {"query": "INC1"}, mode="silent"),
    Case("report move", "move_mail", {"message_id": "M", "folder": "Done"}, mode="report"),
    Case("report ledger note", "update_ledger", {"text": "x"}, mode="report"),
    Case("ask send to caller", "send_mail", SEND, {"participants": TASK}, mode="ask"),
    Case("ask chat post", "post_chat", {"chat_id": "c", "body": "hi"}, mode="ask"),
    Case("registered hand off", "delete_item", {"item_id": "1"}, mode="hand_off", reason="never automated"),
    # args validation
    Case("invalid args blocked", "move_mail", {"message_id": "M"}, mode="blocked", reason="folder"),
    Case("wrong type blocked", "send_mail", {"draft_id": 3, "to": "x"}, mode="blocked"),
    # args-dependent mode
    Case(
        "own dataset after transient error is report",
        "trigger_refresh",
        {"dataset_id": "ds1"},
        {"facts": {"owned_datasets": ["ds1"], "last_error": "transient"}},
        mode="report",
    ),
    Case(
        "own dataset, other error is ask",
        "trigger_refresh",
        {"dataset_id": "ds1"},
        {"facts": {"owned_datasets": ["ds1"], "last_error": "credentials"}},
        mode="ask",
    ),
    Case(
        "shared dataset is ask",
        "trigger_refresh",
        {"dataset_id": "ds9"},
        {"facts": {"last_error": "transient"}},
    ),
    # recipients
    Case(
        "outside To",
        "send_mail",
        {"draft_id": "D", "to": [OUTSIDER]},
        {"participants": TASK},
        mode="hand_off",
    ),
    Case(
        "outside Cc",
        "send_mail",
        {"draft_id": "D", "to": [CALLER], "cc": [OUTSIDER]},
        {"participants": TASK},
        mode="hand_off",
        reason="outside the company",
    ),
    Case(
        "outside Bcc",
        "send_mail",
        {"draft_id": "D", "to": [CALLER], "bcc": [OUTSIDER]},
        {"participants": TASK},
        mode="hand_off",
    ),
    Case(
        "reply-all quietly reaches an outsider",
        "send_mail",
        {"draft_id": "D", "to": [CALLER], "reply_all": True},
        {"participants": TASK, "reply_all_recipients": (CALLER, OUTSIDER)},
        mode="hand_off",
    ),
    Case(
        "reply-all inside the company",
        "send_mail",
        {"draft_id": "D", "to": [CALLER], "reply_all": True},
        {"participants": TASK, "reply_all_recipients": (CALLER, L2)},
        mode="ask",
    ),
    Case(
        "upper-case outside domain",
        "send_mail",
        {"draft_id": "D", "to": ["Bob@PARTNER.example"]},
        mode="hand_off",
    ),
    Case(
        "look-alike domain is outside",
        "send_mail",
        {"draft_id": "D", "to": ["x@corp.example.evil.test"]},
        mode="hand_off",
    ),
    Case(
        "subdomain is inside",
        "send_mail",
        {"draft_id": "D", "to": ["ops@mail.corp.example"]},
        {"participants": frozenset({"ops@mail.corp.example"})},
        mode="ask",
    ),
    Case(
        "model-invented colleague not on the task",
        "send_mail",
        {"draft_id": "D", "to": [STRANGER]},
        {"participants": TASK},
        mode="hand_off",
        reason="not on the task",
    ),
    Case("configured group allowed", "send_mail", {"draft_id": "D", "to": [L2]}, mode="ask"),
    Case("sending to myself only", "send_mail", {"draft_id": "D", "to": [ME]}, {"me": ME}, mode="ask"),
    Case(
        "chat recipients checked too",
        "post_chat",
        {"chat_id": "c", "body": "hi", "recipients": [OUTSIDER]},
        mode="hand_off",
    ),
    Case(
        "saving a draft to anyone stays report",
        "save_draft",
        {"to": [STRANGER, OUTSIDER], "subject": "s", "body": "b"},
        mode="report",
    ),
    # data rule
    Case("images below the data rule", "ocr_local", {"image_ref": "s1"}, mode="blocked", reason="data rule"),
    Case(
        "images allowed at images level", "ocr_local", {"image_ref": "s1"}, mode="silent", data_level="images"
    ),
    # lowering
    Case(
        "model asks for silent send",
        "send_mail",
        SEND,
        {"participants": TASK, "requested_mode": "silent"},
        mode="ask",
        lowering_refused=True,
    ),
    Case(
        "recipe asks report for hand off",
        "delete_item",
        {"item_id": "1"},
        {"requested_mode": "report"},
        mode="hand_off",
        lowering_refused=True,
    ),
    Case(
        "recipe may raise a mode", "move_mail", {"message_id": "M", "folder": "F"}, {"requested_mode": "ask"}
    ),
    Case(
        "lowering cannot beat outside recipient",
        "send_mail",
        {"draft_id": "D", "to": [OUTSIDER]},
        {"requested_mode": "report"},
        mode="hand_off",
        lowering_refused=True,
    ),
    # fetched content
    Case(
        "injection raises silent to ask",
        "search_mail",
        {"query": "x"},
        {"triggered_by_fetched_content": True},
        mode="ask",
        flagged=True,
    ),
    Case(
        "injection raises report to ask",
        "move_mail",
        {"message_id": "M", "folder": "F"},
        {"triggered_by_fetched_content": True},
        mode="ask",
        flagged=True,
    ),
    Case(
        "injection keeps hand off",
        "delete_item",
        {"item_id": "1"},
        {"triggered_by_fetched_content": True},
        mode="hand_off",
        flagged=True,
    ),
    # shadow
    Case(
        "shadow report records only",
        "move_mail",
        {"message_id": "M", "folder": "F"},
        {"recipe_stage": "shadow"},
        mode="report",
        record_only=True,
    ),
    Case(
        "shadow ask records only",
        "send_mail",
        SEND,
        {"participants": TASK, "recipe_stage": "shadow"},
        record_only=True,
    ),
    Case(
        "shadow silent still runs", "search_mail", {"query": "x"}, {"recipe_stage": "shadow"}, mode="silent"
    ),
    # paused
    Case(
        "paused report waits",
        "move_mail",
        {"message_id": "M", "folder": "F"},
        {"paused": True},
        mode="report",
        held=True,
    ),
    Case("paused ask waits", "send_mail", SEND, {"participants": TASK, "paused": True}, held=True),
    Case("paused silent runs", "search_mail", {"query": "x"}, {"paused": True}, mode="silent"),
]


def gate(level: str = "metadata") -> Gate:
    return Gate(t.build(), t.COMPANY, t.GROUPS, data_level=level)  # type: ignore[arg-type]


def test_at_least_thirty_cases() -> None:
    assert len(CASES) >= 30


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_gate_rules(case: Case) -> None:
    decision = gate(case.data_level).check(case.tool, case.args, GateContext(task_id="INC1", **case.ctx))
    assert decision.mode == case.mode, decision.reasons
    assert decision.flagged is case.flagged
    assert decision.record_only is case.record_only
    assert decision.held is case.held
    assert decision.lowering_refused is case.lowering_refused
    if case.reason:
        assert any(case.reason in r for r in decision.reasons), decision.reasons
    assert len(decision.args_digest) == 64


def test_executable_and_approval_flags() -> None:
    g = gate()
    run = g.check("move_mail", {"message_id": "M", "folder": "F"}, GateContext())
    assert run.executable_now and not run.needs_approval
    ask = g.check("send_mail", SEND, GateContext(participants=TASK))
    assert ask.needs_approval and not ask.executable_now
    shadow = g.check("send_mail", SEND, GateContext(participants=TASK, recipe_stage="shadow"))
    assert not shadow.needs_approval and not shadow.executable_now
    held = g.check("move_mail", {"message_id": "M", "folder": "F"}, GateContext(paused=True))
    assert not held.executable_now
    assert not Decision(tool="x", mode="blocked", reasons=[], args_digest="", args={}).executable_now


def test_digest_is_stable_and_normalised() -> None:
    g = gate()
    a = g.check("send_mail", {"to": [CALLER], "draft_id": "D"}, GateContext(participants=TASK))
    b = g.check("send_mail", {"draft_id": "D", "to": [CALLER], "cc": []}, GateContext(participants=TASK))
    assert a.args_digest == b.args_digest  # defaults filled, key order ignored
    c = g.check("send_mail", {"draft_id": "D2", "to": [CALLER]}, GateContext(participants=TASK))
    assert c.args_digest != a.args_digest
    assert digest_args({"b": 1, "a": "é"}) == digest_args({"a": "é", "b": 1})


def test_pydantic_args_accepted() -> None:
    decision = gate().check("move_mail", t.MoveMail(message_id="M", folder="F"), GateContext())
    assert decision.mode == "report" and decision.args == {"message_id": "M", "folder": "F"}


def test_no_company_domains_means_every_recipient_is_outside() -> None:
    g = Gate(t.build(), [], {})
    decision = g.check("send_mail", SEND, GateContext(participants=TASK))
    assert decision.mode == "hand_off"


def test_group_value_lists() -> None:
    g = Gate(t.build(), t.COMPANY, {"l2": "a@corp.example, b@corp.example", "empty": ""})
    ok = g.check("send_mail", {"draft_id": "D", "to": ["b@corp.example"]}, GateContext())
    assert ok.mode == "ask"
