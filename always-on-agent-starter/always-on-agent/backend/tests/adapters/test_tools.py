from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.adapters.fakes.world import FakeWorld
from agent.adapters.tools import TRANSIENT_REFRESH_ERRORS, register_all
from agent.core.clock import FrozenClock
from agent.db.repositories import ActionRepo, AuditRepo, UndoRepo
from agent.policy.gate import Gate, GateContext
from agent.policy.modes import load_policy
from agent.policy.registry import Registry
from agent.policy.undo import UndoService
from agent.runtime.outbox import Outbox

COMPANY = ["corp.example"]
CALLER = "sophie.laurent@corp.example"
MSG = "AAMkAGFAKE0011"
ME = "test.user@corp.example"


class Env:
    def __init__(self, engine: AsyncEngine, clock: FrozenClock, data_level: Any = "metadata") -> None:
        self.world = FakeWorld(clock)
        self.adapters: dict[str, Any] = {
            "outlook": self.world.mail,
            "calendar": self.world.calendar,
            "teams": self.world.teams,
            "powerbi": self.world.powerbi,
        }
        self.registry = register_all(Registry(load_policy()), self.adapters)
        self.gate = Gate(self.registry, COMPANY, {}, data_level=data_level)
        self.audit = AuditRepo(engine, clock)
        self.undo_repo = UndoRepo(engine, clock)
        self.outbox = Outbox(
            self.gate,
            self.registry,
            ActionRepo(engine, clock),
            self.audit,
            self.undo_repo,
            self.adapters,
            clock,
        )
        self.undo = UndoService(self.registry, self.outbox, self.audit, self.undo_repo, self.adapters)


@pytest.fixture
def env(engine: AsyncEngine, clock: FrozenClock) -> Env:
    return Env(engine, clock)


CTX = GateContext(participants=frozenset({CALLER}), me=ME)


def test_every_tool_matches_policy_yaml(env: Env) -> None:
    policy = load_policy()
    for name in env.registry.names():
        tool = env.registry.get(name)
        assert tool is not None and policy.mode_of(name) in (None, tool.mode), name
    for name in (
        "send_mail",
        "post_chat",
        "trigger_refresh",
        "trigger_dataflow_refresh",
        "update_refresh_schedule",
    ):
        assert env.registry.get(name).mode == "ask"  # type: ignore[union-attr]


def test_gate_decisions(env: Env) -> None:
    send = {"draft_id": "D", "to": [CALLER]}
    assert env.gate.check("send_mail", send, CTX).mode == "ask"
    outside = env.gate.check("send_mail", {**send, "cc": ["marc.dubois@partner.example"]}, CTX)
    assert outside.mode == "hand_off"
    stranger = env.gate.check("send_mail", {"draft_id": "D", "to": ["someone@corp.example"]}, CTX)
    assert stranger.mode == "hand_off"
    trash = env.gate.check("move_mail", {"message_id": MSG, "folder": "Deleted Items"}, CTX)
    assert trash.mode == "blocked"
    guest = env.gate.check("post_chat", {"chat_id": "c", "body": "hi", "recipients": ["external:fed-1"]}, CTX)
    assert guest.mode == "hand_off"
    no_audience = env.gate.check("post_chat", {"chat_id": "c", "body": "hi", "recipients": []}, CTX)
    assert no_audience.mode == "blocked"
    # Transcripts need the chat data rule.
    assert env.gate.check("get_transcript", {"meeting_id": "m"}, CTX).mode == "blocked"
    chat_level = Gate(env.registry, COMPANY, {}, data_level="chat")
    assert chat_level.check("get_transcript", {"meeting_id": "m"}, CTX).mode == "silent"
    assert (
        env.gate.check(
            "add_hold", {"subject": "x", "start": "2026-03-25T10:00:00", "end": "2026-03-25T11:00:00Z"}, CTX
        ).mode
        == "blocked"
    )  # naive time


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"owned_datasets": ["ds"], "last_error_code": "DM_GWPipeline_Gateway_TimeoutError"}, "report"),
        (
            {
                "owned_datasets": ["ds"],
                "last_error_code": "DM_GWPipeline_Gateway_TimeoutError",
                "agent_retries_today": 1,
            },
            "ask",
        ),
        ({"owned_datasets": ["other"], "last_error_code": "DM_GWPipeline_Gateway_TimeoutError"}, "ask"),
        ({"owned_datasets": ["ds"], "last_error_code": "ModelRefresh_InvalidCredentials"}, "ask"),
        ({}, "ask"),
    ],
)
def test_refresh_retry_is_report_once_for_own_dataset_after_a_transient_error(
    env: Env, facts: dict[str, Any], expected: str
) -> None:
    decision = env.gate.check("trigger_refresh", {"dataset_id": "ds"}, GateContext(facts=facts))
    assert decision.mode == expected
    # Fetched content cannot make it Report.
    flagged = env.gate.check(
        "trigger_refresh", {"dataset_id": "ds"}, GateContext(facts=facts, triggered_by_fetched_content=True)
    )
    assert flagged.mode == "ask"
    assert "DM_GWPipeline_Gateway_TimeoutError" in TRANSIENT_REFRESH_ERRORS


# ---------------------------------------------------------------- undo through the outbox


async def test_move_undo_refuses_after_the_user_moved_it(env: Env) -> None:
    done = await env.outbox.submit("move_mail", {"message_id": MSG, "folder": "Done"}, CTX)
    assert done.status == "done" and env.world.mail.folders[MSG] == "Done"
    env.world.mail.folders[MSG] = "Archive"
    refused = await env.undo.undo(done.audit_id)  # type: ignore[arg-type]
    assert refused.status == "refused" and "Archive" in refused.message
    env.world.mail.folders[MSG] = "Done"
    assert (await env.undo.undo(done.audit_id)).status == "done"  # type: ignore[arg-type]
    assert env.world.mail.folders[MSG] == "Inbox"


async def test_flag_undo_restores_the_previous_state(env: Env) -> None:
    await env.world.mail.set_flag(MSG, True)  # flagged by the user before
    done = await env.outbox.submit("flag_mail", {"message_id": MSG}, CTX)
    assert (await env.undo.undo(done.audit_id)).status == "done"  # type: ignore[arg-type]
    assert await env.world.mail.is_flagged(MSG)  # still flagged: the undo does not clear the user's flag


async def test_categorise_undo_restores_old_categories_unless_changed(env: Env) -> None:
    before = await env.world.mail.get_categories(MSG)
    done = await env.outbox.submit("categorise", {"message_id": MSG, "categories": ["INC30114872"]}, CTX)
    await env.world.mail.categorise(MSG, ["Changed by hand"])
    assert (await env.undo.undo(done.audit_id)).status == "refused"  # type: ignore[arg-type]
    await env.world.mail.categorise(MSG, ["INC30114872"])
    assert (await env.undo.undo(done.audit_id)).status == "done"  # type: ignore[arg-type]
    assert await env.world.mail.get_categories(MSG) == before


async def test_save_draft_undo_discards_only_that_draft(env: Env) -> None:
    done = await env.outbox.submit("save_draft", {"to": [CALLER], "subject": "Re", "body": "Hello"}, CTX)
    assert done.result in env.world.mail.drafts
    assert (await env.undo.undo(done.audit_id)).status == "done"  # type: ignore[arg-type]
    assert done.result not in env.world.mail.drafts


async def test_hold_undo_refuses_once_someone_is_invited(env: Env, clock: FrozenClock) -> None:
    start = clock.now_utc() + timedelta(hours=1)
    args = {"subject": "Focus", "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat()}
    done = await env.outbox.submit("add_hold", args, CTX)
    assert done.status == "done"
    raw = next(r for r in env.world.calendar.raw if r["id"] == done.result)
    raw["attendees"] = [{"emailAddress": {"address": CALLER}}]
    assert (await env.undo.undo(done.audit_id)).status == "refused"  # type: ignore[arg-type]
    raw["attendees"] = []
    assert (await env.undo.undo(done.audit_id)).status == "done"  # type: ignore[arg-type]


async def test_read_calendar_skips_private_and_cancelled(env: Env, clock: FrozenClock) -> None:
    start = clock.now_utc() - timedelta(hours=12)
    result = await env.outbox.submit(
        "read_calendar", {"start": start.isoformat(), "end": (start + timedelta(days=1)).isoformat()}, CTX
    )
    assert result.status == "done" and result.result
    assert all(e["subject"] != "(private)" for e in result.result)
