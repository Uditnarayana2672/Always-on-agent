import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.adapters.fakes.world import FakeWorld
from agent.approvals.service import ApprovalService
from agent.channels import cards
from agent.channels.slack_bot import DOWN_AFTER, HELP, SlackBot
from agent.core.clock import FrozenClock
from agent.core.errors import AgentError
from agent.db.repositories import ActionRepo, AuditRepo, Task, TaskRepo, UndoRepo
from agent.policy.gate import Gate, GateContext
from agent.runtime.coordinator import IngestResult
from agent.runtime.outbox import Outbox
from tests.policy import tools as t

ME = "UME"
CONSOLE = "http://127.0.0.1:8765"
EN_DASH = chr(0x2013)


class FakeWeb:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.views: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> Any:
        self.posts.append(kwargs)
        return {"ts": f"1700000000.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> Any:
        self.updates.append(kwargs)
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs: Any) -> Any:
        return {"ok": True}

    async def views_open(self, **kwargs: Any) -> Any:
        self.views.append(kwargs)
        return {"ok": True}

    async def conversations_open(self, **kwargs: Any) -> Any:
        return {"channel": {"id": "D-ME"}}


class Links:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def link(self, task_id: str, kind: str, external_id: str) -> None:
        await asyncio.sleep(0)  # let a racing caller in
        self.rows.append({"task_id": task_id, "kind": kind, "external_id": external_id})

    async def links(self, task_id: str) -> list[dict[str, Any]]:
        await asyncio.sleep(0)
        return [r for r in self.rows if r["task_id"] == task_id]


class Env:
    def __init__(self, engine: AsyncEngine, clock: FrozenClock) -> None:
        self.clock = clock
        self.world = FakeWorld(clock)
        self.registry = t.build()
        self.actions = ActionRepo(engine, clock)
        audit = AuditRepo(engine, clock)
        self.outbox = Outbox(
            Gate(self.registry, t.COMPANY, t.GROUPS),
            self.registry,
            self.actions,
            audit,
            UndoRepo(engine, clock),
            {"outlook": self.world.mail, "teams": self.world.teams},
            clock,
        )
        self.resumed: list[str] = []
        self.web = FakeWeb()
        self.links = Links()
        self.ingested: list[Any] = []
        self.route_to: list[str] = ["INC30114872"]
        self.bot = SlackBot(
            self.web, ME, clock, CONSOLE, ingest=self._ingest, links=self.links, undo=self._undo
        )

        async def resume(task_id: str, action_id: str) -> int:
            self.resumed.append(action_id)
            return 1

        self.approvals = ApprovalService(
            self.outbox, self.actions, audit, self.registry, clock, resume, channels=[self.bot]
        )
        self.bot._approvals = self.approvals
        self.replies: list[str] = []

    async def _ingest(self, raw: Any, source: Any) -> IngestResult:
        duplicate = any(getattr(r, "at", None) == raw.at and r.text == raw.text for r in self.ingested)
        self.ingested.append(raw)
        return IngestResult(event_id=len(self.ingested), duplicate=duplicate, task_ids=list(self.route_to))

    async def _undo(self, audit_id: int, actor: str) -> Any:
        return type("R", (), {"message": f"Undone {audit_id} by {actor}."})()

    async def respond(self, text: str) -> None:
        self.replies.append(text)

    async def propose(self, body: str = "Fixed, please retry.") -> str:
        result = await self.approvals.create(
            "INC30114872",
            "post_chat",
            {"chat_id": "c1", "body": body, "recipients": []},
            body,
            GateContext(),
        )
        assert result.status == "proposed"
        return result.action_id

    def click(self, action_id: str, value: str, user: str = ME, ts: str = "1") -> dict[str, Any]:
        return {
            "user": {"id": user},
            "trigger_id": f"trig-{ts}",
            "actions": [{"action_id": action_id, "value": value, "action_ts": ts}],
        }


@pytest.fixture
async def env(engine: AsyncEngine, clock: FrozenClock) -> Env:
    now = clock.now_utc()
    await TaskRepo(engine, clock).create(
        Task(
            id="INC30114872",
            project="doxis",
            kind="incident",
            title="Login fails",
            state="needs_you",
            created_at=now,
            updated_at=now,
        )
    )
    return Env(engine, clock)


def card_value(env: Env, index: int = -1) -> str:
    blocks = env.web.posts[index]["blocks"]
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    return str(next(a for a in actions if a["action_id"] == cards.APPROVE)["value"])


async def test_card_posts_and_approve_resolves_once(env: Env) -> None:
    action_id = await env.propose()
    assert len(env.web.posts) == 1 and env.web.posts[0]["channel"] == "D-ME"
    value = card_value(env)
    await env.bot.on_action(env.click(cards.APPROVE, value), env.respond)
    await env.bot.on_action(env.click(cards.APPROVE, value), env.respond)  # Slack redelivers the click
    await env.bot.on_action(env.click(cards.APPROVE, value, ts="2"), env.respond)  # a real double click
    action = await env.actions.get(action_id)
    assert action is not None and action.status == "approved" and action.decided_by == "slack"
    assert env.replies == ["Already approved."]
    assert env.resumed == [action_id]  # the graph resumes once
    assert len(env.web.updates) == 1 and "Approved" in json.dumps(env.web.updates[0]["blocks"])


async def test_stale_hash_and_strangers_change_nothing(env: Env) -> None:
    action_id = await env.propose()
    stale = json.dumps({"a": action_id, "h": "0" * 64})
    await env.bot.on_action(env.click(cards.APPROVE, stale), env.respond)
    await env.bot.on_action(env.click(cards.APPROVE, card_value(env), user="U-COLLEAGUE"), env.respond)
    action = await env.actions.get(action_id)
    assert action is not None and action.status == "proposed"
    assert env.replies[0].startswith("Nothing was done: the text changed")
    assert env.replies[1] == "Only the owner of this agent can use these buttons."


async def test_edit_opens_the_box_and_needs_a_new_approval(env: Env) -> None:
    action_id = await env.propose()
    value = card_value(env)
    await env.bot.on_action(env.click(cards.EDIT, value), env.respond)
    view = env.web.views[0]["view"]
    assert view["blocks"][0]["element"]["initial_value"] == "Fixed, please retry."
    submission = {
        "user": {"id": ME},
        "view": {
            "private_metadata": view["private_metadata"],
            "state": {"values": {"text": {"value": {"value": "Fixed now. Please retry."}}}},
        },
    }
    assert await env.bot.on_view_submission(submission) is None
    action = await env.actions.get(action_id)
    assert action is not None and action.status == "edited" and action.content == "Fixed now. Please retry."
    assert len(env.web.posts) == 2  # the edited version is a new card
    # The old card's Approve now carries an old hash: it cannot approve the edited text.
    await env.bot.on_action(env.click(cards.APPROVE, value, ts="9"), env.respond)
    assert (await env.actions.get(action_id)).status == "edited"  # type: ignore[union-attr]
    # The same box saved again is a no-op; an old box with other text is refused and stays open.
    assert await env.bot.on_view_submission(submission) is None
    stale = json.loads(json.dumps(submission))
    stale["view"]["state"]["values"]["text"]["value"]["value"] = "Something else"
    again = await env.bot.on_view_submission(stale)
    assert again is not None and again["response_action"] == "errors"
    await env.bot.on_action(env.click(cards.APPROVE, card_value(env), ts="10"), env.respond)
    assert (await env.actions.get(action_id)).status == "approved"  # type: ignore[union-attr]


async def test_empty_or_foreign_edits_are_refused(env: Env) -> None:
    await env.propose()
    meta = cards.approval_value(cards_card(env))
    empty = {
        "user": {"id": ME},
        "view": {"private_metadata": meta, "state": {"values": {"text": {"value": {"value": "  "}}}}},
    }
    assert (await env.bot.on_view_submission(empty))["errors"]["text"] == "The text is empty."  # type: ignore[index]
    foreign = {**empty, "user": {"id": "U-OTHER"}}
    assert "owner" in (await env.bot.on_view_submission(foreign))["errors"]["text"]  # type: ignore[index]


def cards_card(env: Env) -> Any:
    value = json.loads(card_value(env))
    return type("C", (), {"action_id": value["a"], "content_hash": value["h"]})()


async def test_reject_and_undo_buttons(env: Env) -> None:
    action_id = await env.propose()
    reject_value = card_value(env)
    await env.bot.on_action(env.click(cards.REJECT, reject_value), env.respond)
    assert (await env.actions.get(action_id)).status == "rejected"  # type: ignore[union-attr]
    await env.bot.on_action(env.click(cards.UNDO, json.dumps({"u": 41})), env.respond)
    assert env.replies[-1] == "Undone 41 by slack."
    await env.bot.on_action(env.click(cards.APPROVE, "not json"), env.respond)
    assert "old message" in env.replies[-1]


async def test_thread_replies_are_stored_and_acknowledged(env: Env) -> None:
    root = await env.bot.ensure_thread("INC30114872", "Login fails")
    reply = {
        "type": "message",
        "channel_type": "im",
        "user": ME,
        "text": "Caller says it works",
        "ts": "1700000100.000001",
        "thread_ts": root,
    }
    result = await env.bot.on_message(reply)
    assert (
        result is not None and env.ingested[-1].kind == "thread_reply" and env.ingested[-1].thread_ts == root
    )
    assert env.web.posts[-1]["thread_ts"] == root and env.web.posts[-1]["text"] == "Noted on INC30114872."
    posts = len(env.web.posts)
    await env.bot.on_message(reply)  # redelivered: no second acknowledgement
    assert len(env.web.posts) == posts
    env.route_to = []
    await env.bot.on_message({**reply, "ts": "1700000200.000001"})
    assert "could not tell" in env.web.posts[-1]["text"]


async def test_bot_posts_edits_and_other_people_are_ignored(env: Env) -> None:
    base = {"type": "message", "channel_type": "im", "user": ME, "text": "x", "ts": "1700000300.000001"}
    assert await env.bot.on_message({**base, "bot_id": "B1"}) is None
    assert await env.bot.on_message({**base, "subtype": "message_changed"}) is None
    assert await env.bot.on_message({**base, "user": "U-OTHER"}) is None
    assert await env.bot.on_message({**base, "channel_type": "channel"}) is None
    assert env.ingested == []
    dm = await env.bot.on_message(base)
    assert dm is not None and env.ingested[-1].kind == "dm"


async def test_one_thread_root_per_task_even_when_racing(env: Env) -> None:
    first, second = await asyncio.gather(
        env.bot.ensure_thread("INC30114872", "Login fails"),
        env.bot.ensure_thread("INC30114872", "Login fails"),
    )
    assert first == second and len(env.links.rows) == 1
    assert env.web.posts[0]["text"] == f"INC30114872 {EN_DASH} Login fails"


class Commands:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def status(self) -> str:
        return "Running"

    async def pause(self) -> str:
        self.calls.append("pause")
        return "Paused."

    async def resume(self) -> str:
        return "Resumed."

    async def brief(self) -> str:
        raise AgentError("the scheduler is not running")

    async def tasks(self) -> str:
        return "No open tasks."

    async def undo(self, audit_id: int) -> str:
        return f"undo {audit_id}"


async def test_agent_command(env: Env) -> None:
    commands = Commands()
    env.bot._commands = commands
    run = env.bot.on_command
    assert (
        await run({"user_id": "U-OTHER", "text": "pause"}) == "Only the owner of this agent can use /agent."
    )
    assert commands.calls == []
    assert await run({"user_id": ME, "text": "pause"}) == "Paused."
    assert await run({"user_id": ME, "text": "STATUS"}) == "Running"
    assert await run({"user_id": ME, "text": "undo 42"}) == "undo 42"
    assert "with the number" in await run({"user_id": ME, "text": "undo abc"})
    assert await run({"user_id": ME, "text": "brief"}) == "That did not work: the scheduler is not running"
    assert await run({"user_id": ME, "text": ""}) == HELP
    assert await run({"user_id": ME, "text": "dance"}) == HELP


async def test_connection_health_degrades_then_goes_down(env: Env, clock: FrozenClock) -> None:
    assert (await env.bot.health()).status == "down"  # never connected
    await env.bot.mark(True)
    assert (await env.bot.health()).status == "ok"
    await env.bot.mark(False)
    assert (await env.bot.health()).status == "degraded"
    clock.advance(DOWN_AFTER + timedelta(seconds=1))
    assert (await env.bot.health()).status == "down"


def test_bot_needs_a_user_id(clock: FrozenClock) -> None:
    with pytest.raises(AgentError, match="user_id"):
        SlackBot(FakeWeb(), "", clock, CONSOLE)
