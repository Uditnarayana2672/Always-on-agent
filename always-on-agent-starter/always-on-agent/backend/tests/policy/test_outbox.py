import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.adapters.fakes.world import FakeWorld
from agent.core.clock import FrozenClock
from agent.core.errors import PolicyViolation
from agent.db.repositories import ActionRepo, AuditRepo, UndoRepo
from agent.policy.gate import Gate, GateContext
from agent.policy.modes import Policy, PolicyEntry
from agent.policy.registry import Registry
from agent.policy.undo import UndoService
from agent.runtime.outbox import Outbox
from tests.policy import tools as t
from tests.policy.tools import CALLER

TASK = GateContext(task_id=None, participants=frozenset({CALLER}))
MSG = "AAMkAGFAKE0011"  # an Inbox message in the fixtures


class Env:
    def __init__(self, engine: AsyncEngine, clock: FrozenClock, registry: Registry | None = None) -> None:
        self.world = FakeWorld(clock)
        self.registry = registry or t.build()
        self.gate = Gate(self.registry, t.COMPANY, t.GROUPS)
        self.actions = ActionRepo(engine, clock)
        self.audit = AuditRepo(engine, clock)
        self.undo_repo = UndoRepo(engine, clock)
        self.adapters: dict[str, Any] = {
            "outlook": self.world.mail,
            "teams": self.world.teams,
            "powerbi": self.world.powerbi,
        }
        self.outbox = Outbox(
            self.gate, self.registry, self.actions, self.audit, self.undo_repo, self.adapters, clock
        )
        self.undo = UndoService(self.registry, self.outbox, self.audit, self.undo_repo, self.adapters)

    def retarget(self, registry: Registry) -> None:
        """Simulate an edited policy.yaml: same database, new gate."""
        self.registry = registry
        self.gate = Gate(registry, t.COMPANY, t.GROUPS)
        self.outbox = Outbox(
            self.gate, registry, self.actions, self.audit, self.undo_repo, self.adapters, self.outbox._clock
        )


@pytest.fixture
def env(engine: AsyncEngine, clock: FrozenClock) -> Env:
    return Env(engine, clock)


SEND = {"draft_id": "D1", "to": [CALLER]}


async def approve(env: Env, action_id: str) -> None:
    action = await env.actions.get(action_id)
    assert action is not None
    await env.actions.transition(action_id, "proposed", "approved", expected_hash=action.content_hash)


# ---------------------------------------------------------------- submit


async def test_silent_runs_and_is_audited(env: Env) -> None:
    res = await env.outbox.submit("search_mail", {"query": "INC"}, GateContext())
    assert res.status == "done" and res.result == ["hit"]
    rows = await env.audit.for_action(res.action_id)
    assert [r["result"] for r in rows] == ["decision:ready", "done"]


async def test_report_runs_and_registers_inverse(env: Env) -> None:
    res = await env.outbox.submit("move_mail", {"message_id": MSG, "folder": "Done"}, GateContext())
    assert res.status == "done" and env.world.mail.folders[MSG] == "Done"
    assert res.audit_id is not None
    op = await env.undo_repo.for_audit(res.audit_id)
    assert op is not None and op["inverse_tool"] == "move_mail"
    assert json.loads(op["inverse_args_json"]) == {"message_id": MSG, "folder": "Inbox", "moved_to": "Done"}


async def test_ask_waits_for_approval(env: Env) -> None:
    res = await env.outbox.submit("send_mail", SEND, TASK)
    assert res.status == "proposed" and env.world.mail.sent == []
    action = await env.actions.get(res.action_id)
    assert action is not None and action.expires_at is not None
    with pytest.raises(PolicyViolation, match="proposed"):
        await env.outbox.execute(res.action_id, TASK)
    await approve(env, res.action_id)
    done = await env.outbox.execute(res.action_id, TASK)
    assert done.status == "done" and env.world.mail.sent == ["D1"]


@pytest.mark.parametrize(
    ("tool", "args", "ctx", "status"),
    [
        ("delete_item", {"item_id": "1"}, GateContext(), "handed_off"),
        ("send_mail", {"draft_id": "D", "to": ["x@partner.example"]}, GateContext(), "handed_off"),
        ("move_mail", {"message_id": MSG, "folder": "F"}, GateContext(recipe_stage="shadow"), "shadow"),
        ("move_mail", {"message_id": MSG, "folder": "F"}, GateContext(paused=True), "held"),
        ("ocr_local", {"image_ref": "s"}, GateContext(), "blocked"),
        ("move_mail", {"message_id": MSG}, GateContext(), "blocked"),
    ],
)
async def test_nothing_runs_for_non_executable_decisions(
    env: Env, tool: str, args: dict[str, Any], ctx: GateContext, status: str
) -> None:
    res = await env.outbox.submit(tool, args, ctx)
    assert res.status == status
    assert env.world.all_writes() == []
    rows = await env.audit.for_action(res.action_id)
    assert len(rows) == 1 and rows[0]["result"].startswith(f"decision:{status}")


async def test_flagged_decision_is_audited(env: Env) -> None:
    res = await env.outbox.submit(
        "move_mail", {"message_id": MSG, "folder": "F"}, GateContext(triggered_by_fetched_content=True)
    )
    assert res.status == "proposed"
    (row,) = await env.audit.for_action(res.action_id)
    assert "FLAGGED" in row["result"] and row["mode"] == "ask"


async def test_lowering_attempt_is_audited(env: Env) -> None:
    res = await env.outbox.submit(
        "send_mail", SEND, GateContext(participants=frozenset({CALLER}), requested_mode="silent")
    )
    (row,) = await env.audit.for_action(res.action_id)
    assert "LOWERING_REFUSED" in row["result"] and res.status == "proposed"


async def test_same_key_twice_is_one_effect(env: Env) -> None:
    a = await env.outbox.submit("move_mail", {"message_id": MSG, "folder": "Done"}, GateContext(task_id=None))
    b = await env.outbox.submit("move_mail", {"message_id": MSG, "folder": "Done"}, GateContext(task_id=None))
    assert a.action_id == b.action_id and b.status == "done"
    assert len(env.world.mail.writes_to("move")) == 1
    p1 = await env.outbox.submit("send_mail", SEND, TASK)
    p2 = await env.outbox.submit("send_mail", SEND, TASK)
    assert p1.action_id == p2.action_id and p2.status == "proposed"


async def test_execute_done_returns_stored_result(env: Env) -> None:
    res = await env.outbox.submit("update_ledger", {"text": "hello"}, GateContext())
    again = await env.outbox.execute(res.action_id, GateContext())
    assert again.status == "done" and again.result == res.result
    with pytest.raises(KeyError):
        await env.outbox.execute("missing", GateContext())


# ---------------------------------------------------------------- crash recovery


async def crash_after_adapter(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter call succeeds, then the process dies before 'done' is recorded."""
    real = env.actions.transition

    async def dying(action_id: str, from_status: str, to_status: str, **kw: Any) -> None:
        if (from_status, to_status) == ("executing", "done"):
            raise SystemExit("power cut")
        await real(action_id, from_status, to_status, **kw)

    monkeypatch.setattr(env.actions, "transition", dying)


async def test_crash_after_send_does_not_send_twice(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    res = await env.outbox.submit("send_mail", SEND, TASK)
    await approve(env, res.action_id)
    with monkeypatch.context() as m:
        await crash_after_adapter(env, m)
        with pytest.raises(SystemExit):
            await env.outbox.execute(res.action_id, TASK)
    stuck = await env.actions.get(res.action_id)
    assert stuck is not None and stuck.status == "executing" and env.world.mail.sent == ["D1"]
    # Restart: the outbox finds the idempotency header in Sent Items and records done.
    recovered = await env.outbox.execute(res.action_id, TASK)
    assert recovered.status == "done" and recovered.result == "D1"
    assert len(env.world.mail.writes_to("send")) == 1


async def test_crash_after_chat_post_does_not_post_twice(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    res = await env.outbox.submit("post_chat", {"chat_id": "c1", "body": "update"}, TASK)
    await approve(env, res.action_id)
    with monkeypatch.context() as m:
        await crash_after_adapter(env, m)
        with pytest.raises(SystemExit):
            await env.outbox.execute(res.action_id, TASK)
    assert (await env.outbox.execute(res.action_id, TASK)).status == "done"
    assert len(env.world.teams.posted) == 1


async def test_crash_before_send_sends_once_on_recovery(env: Env) -> None:
    res = await env.outbox.submit("send_mail", SEND, TASK)
    await approve(env, res.action_id)
    await env.actions.transition(res.action_id, "approved", "executing")  # died before calling the adapter
    assert (await env.outbox.execute(res.action_id, TASK)).status == "done"
    assert env.world.mail.sent == ["D1"]


async def test_ask_tool_without_lookup_is_not_resent_after_crash(
    env: Env, engine: AsyncEngine, clock: FrozenClock
) -> None:
    res = await env.outbox.submit("trigger_refresh", {"dataset_id": "ds9"}, GateContext())
    await approve(env, res.action_id)
    await env.actions.transition(res.action_id, "approved", "executing")
    out = await env.outbox.execute(res.action_id, GateContext())
    assert out.status == "failed" and "check by hand" in (out.error or "")
    assert env.world.powerbi.writes_to("trigger_refresh") == []


async def test_report_without_lookup_reruns_after_crash(env: Env) -> None:
    res = await env.outbox.submit(
        "move_mail", {"message_id": MSG, "folder": "Done"}, GateContext(paused=True)
    )
    assert res.status == "held"
    await env.actions.transition(res.action_id, "held", "executing")
    out = await env.outbox.execute(res.action_id, GateContext())
    assert out.status == "done" and env.world.mail.folders[MSG] == "Done"


# ---------------------------------------------------------------- failures and rechecks


async def test_adapter_failure_is_recorded_and_report_retry_reuses_action(env: Env) -> None:
    env.world.mail.fail_next("500", method="move")
    first = await env.outbox.submit("move_mail", {"message_id": MSG, "folder": "Done"}, GateContext())
    assert first.status == "failed" and "server error" in (first.error or "")
    assert [r["result"] for r in await env.audit.for_action(first.action_id)] == [
        "decision:ready",
        "failed: AdapterError",
    ]
    retry = await env.outbox.submit("move_mail", {"message_id": MSG, "folder": "Done"}, GateContext())
    assert retry.action_id == first.action_id and retry.status == "done"


async def test_failed_ask_needs_a_fresh_approval(env: Env) -> None:
    env.world.mail.fail_next("500", method="send")
    res = await env.outbox.submit("send_mail", SEND, TASK)
    await approve(env, res.action_id)
    assert (await env.outbox.execute(res.action_id, TASK)).status == "failed"
    again = await env.outbox.submit("send_mail", SEND, TASK)
    assert again.status == "failed" and env.world.mail.sent == []


async def test_policy_tightened_while_approval_pending(env: Env) -> None:
    res = await env.outbox.submit(
        "move_mail", {"message_id": MSG, "folder": "Done"}, GateContext(triggered_by_fetched_content=True)
    )
    await approve(env, res.action_id)
    # The approval was for "ask"; policy.yaml now says hand off.
    tightened = Registry(Policy(tools={"move_mail": PolicyEntry(mode="hand_off")}))
    tightened.tool("move_mail", mode="report", adapter="outlook", method="move")(t.MoveMail)
    env.retarget(tightened)
    out = await env.outbox.execute(res.action_id, GateContext())
    assert out.status == "blocked" and "hand_off" in (out.error or "")
    assert env.world.mail.writes_to("move") == []


async def test_policy_stricter_than_approved_mode(env: Env) -> None:
    res = await env.outbox.submit("update_ledger", {"text": "x"}, GateContext(paused=True))
    stricter = Registry(Policy(tools={"update_ledger": PolicyEntry(mode="ask")}))
    stricter.tool("update_ledger", mode="report", handler=t._note)(t.Note)
    env.retarget(stricter)
    await env.actions.transition(res.action_id, "held", "ready")
    out = await env.outbox.execute(res.action_id, GateContext())
    assert out.status == "blocked" and "needs ask" in (out.error or "")


async def test_pause_between_approval_and_execution_holds(env: Env) -> None:
    res = await env.outbox.submit("send_mail", SEND, TASK)
    await approve(env, res.action_id)
    out = await env.outbox.execute(res.action_id, GateContext(participants=frozenset({CALLER}), paused=True))
    assert out.status == "held" and env.world.mail.sent == []
    assert (await env.actions.get(res.action_id)).status == "held"  # type: ignore[union-attr]


async def test_shadow_at_execution_blocks(env: Env) -> None:
    res = await env.outbox.submit("send_mail", SEND, TASK)
    await approve(env, res.action_id)
    out = await env.outbox.execute(
        res.action_id, GateContext(participants=frozenset({CALLER}), recipe_stage="shadow")
    )
    assert out.status == "blocked" and env.world.mail.sent == []


async def test_recipient_dropped_from_task_blocks_send(env: Env) -> None:
    res = await env.outbox.submit("send_mail", SEND, TASK)
    await approve(env, res.action_id)
    out = await env.outbox.execute(res.action_id, GateContext())  # caller no longer a participant
    assert out.status == "blocked"


async def test_tampered_args_are_refused(env: Env, engine: AsyncEngine) -> None:
    from sqlalchemy import update

    from agent.db import schema as s

    res = await env.outbox.submit("send_mail", SEND, TASK)
    await approve(env, res.action_id)
    async with engine.begin() as conn:
        await conn.execute(
            update(s.action_requests)
            .where(s.action_requests.c.id == res.action_id)
            .values(args_json=json.dumps({"draft_id": "EVIL", "to": [CALLER]}))
        )
    out = await env.outbox.execute(res.action_id, TASK)
    assert out.status == "blocked" and "content changed" in (out.error or "")


async def test_unregistered_tool_cannot_execute(env: Env) -> None:
    res = await env.outbox.submit("make_coffee", {}, GateContext())
    assert res.status == "proposed"
    await approve(env, res.action_id)
    with pytest.raises(PolicyViolation, match="not a registered tool"):
        await env.outbox.execute(res.action_id, GateContext())


async def test_concurrent_claim_loses_cleanly(env: Env) -> None:
    res = await env.outbox.submit("update_ledger", {"text": "y"}, GateContext(paused=True))
    await env.actions.transition(res.action_id, "held", "ready")
    real = env.actions.transition
    calls = {"n": 0}

    async def racing(action_id: str, from_status: str, to_status: str, **kw: Any) -> None:
        if to_status == "executing" and calls["n"] == 0:
            calls["n"] += 1
            # Another worker claims and finishes it first.
            await real(action_id, from_status, "executing")
            await real(action_id, "executing", "done", result_json=json.dumps({"result": "other"}))
        await real(action_id, from_status, to_status, **kw)

    env.actions.transition = racing  # type: ignore[method-assign]
    out = await env.outbox.execute(res.action_id, GateContext())
    assert out.status == "done" and out.result == "other"


async def test_approval_expiry_is_set(env: Env, clock: FrozenClock) -> None:
    res = await env.outbox.submit("send_mail", SEND, TASK)
    action = await env.actions.get(res.action_id)
    assert action is not None and action.expires_at == clock.now_utc() + timedelta(hours=24)


async def test_held_then_resumed(env: Env) -> None:
    held = await env.outbox.submit(
        "move_mail", {"message_id": MSG, "folder": "Done"}, GateContext(paused=True)
    )
    assert held.status == "held"
    again = await env.outbox.submit("move_mail", {"message_id": MSG, "folder": "Done"}, GateContext())
    assert again.action_id == held.action_id and again.status == "done"
    held_send = await env.outbox.submit(
        "send_mail", SEND, GateContext(participants=frozenset({CALLER}), paused=True)
    )
    still = await env.outbox.submit(
        "send_mail", SEND, GateContext(participants=frozenset({CALLER}), paused=True)
    )
    assert still.status == "held"
    resumed = await env.outbox.submit("send_mail", SEND, TASK)
    assert resumed.action_id == held_send.action_id and resumed.status == "proposed"
    assert env.world.mail.sent == []
