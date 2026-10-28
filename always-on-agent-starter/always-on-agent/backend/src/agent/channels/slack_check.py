"""`agentd run --fake --real-slack`: one test approval card in the user's real Slack DM (prompt 1.11).

Everything except Slack is fake: a temporary database, the fake Teams chat from the fixtures and
the fixture clock's tools. Approving runs the post through the real approval service and outbox
against the fake chat, so nothing real is sent anywhere. The run ends when the card is decided,
or after `timeout` seconds.
"""

import asyncio
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

from agent.adapters.fakes.world import FakeWorld
from agent.adapters.tools import register_all
from agent.approvals.service import ApprovalService
from agent.channels.slack_bot import SlackBot, SlackRunner, SlackWeb
from agent.core.clock import SystemClock
from agent.core.config import Config
from agent.db.engine import create_engine_async
from agent.db.migrate import upgrade_head
from agent.db.repositories import ActionRepo, AuditRepo, Task, TaskRepo, UndoRepo
from agent.policy.gate import Gate, GateContext
from agent.policy.modes import load_policy
from agent.policy.registry import Registry
from agent.runtime.outbox import Outbox

TEST_TASK = "INC30199999"
TEST_CHAT = "19:fake-doxis-l2@thread.v2"
TEST_TEXT = "Always-On Agent test approval. Approving posts to a fake chat; nothing real is sent."


async def run_slack_check(
    config: Config, bot_token: str, app_token: str, timeout: float = 600, echo: Callable[[str], None] = print
) -> str:
    from slack_sdk.web.async_client import AsyncWebClient

    clock = SystemClock(config)
    with tempfile.TemporaryDirectory(prefix="agent-slack-check-") as tmp:
        url = f"sqlite+aiosqlite:///{(Path(tmp) / 'check.db').as_posix()}"
        upgrade_head(url)
        engine = create_engine_async(url)
        try:
            world = FakeWorld(clock)
            adapters = {
                "outlook": world.mail,
                "calendar": world.calendar,
                "teams": world.teams,
                "powerbi": world.powerbi,
            }
            registry = register_all(Registry(load_policy()), adapters)
            actions, audit = ActionRepo(engine, clock), AuditRepo(engine, clock)
            outbox = Outbox(
                Gate(registry, ["corp.example"], {}),
                registry,
                actions,
                audit,
                UndoRepo(engine, clock),
                adapters,
                clock,
            )
            now = clock.now_utc()
            await TaskRepo(engine, clock).create(
                Task(
                    id=TEST_TASK,
                    project="doxis",
                    kind="incident",
                    title="Slack test",
                    state="needs_you",
                    created_at=now,
                    updated_at=now,
                )
            )
            members = [m for m in await world.teams.chat_members(TEST_CHAT) if m != world.teams.me_address]
            ctx = GateContext(task_id=TEST_TASK, participants=frozenset(members))
            decided = asyncio.Event()
            outcome: dict[str, str] = {}

            async def resume(task_id: str, action_id: str) -> int:
                action = await actions.get(action_id)
                status = action.status if action else "missing"
                if status == "approved":
                    status = (await outbox.execute(action_id, ctx)).status
                outcome["status"] = status
                decided.set()
                return 0

            bot = SlackBot(
                cast(SlackWeb, AsyncWebClient(token=bot_token)),
                config.slack.user_id,
                clock,
                f"http://{config.console.host}:{config.console.port}",
            )
            approvals = ApprovalService(outbox, actions, audit, registry, clock, resume, channels=[bot])
            bot._approvals = approvals
            runner = SlackRunner(bot, bot_token, app_token)
            await runner.start()
            try:
                result = await approvals.create(
                    TEST_TASK,
                    "post_chat",
                    {"chat_id": TEST_CHAT, "body": TEST_TEXT, "recipients": members},
                    TEST_TEXT,
                    ctx,
                )
                echo(
                    f"Test approval card sent to your Slack DM ({result.status}). "
                    "Press Approve or Reject there."
                )
                try:
                    await asyncio.wait_for(decided.wait(), timeout=timeout)
                except TimeoutError:
                    return "timed out: no decision in Slack"
                if outcome.get("status") == "done":
                    await bot.send(
                        "Test approval worked: the fake chat got the message. Nothing real was sent."
                    )
                    return "approved and run once in the fake chat"
                return outcome.get("status", "unknown")
            finally:
                await runner.stop()
        finally:
            await engine.dispose()
