"""agentd run --fake: the whole agent on the fixtures, nothing real touched.

Fake adapters (FakeWorld) on a clock that starts at the fixtures' Wednesday 09:15 IST and then runs in
real time, a database in a throwaway folder (or --data-dir), no model (recipes take their code-only
path), and the fake Slack DM as the notification channel.

run_demo() then does a short day: read the fixture mail and chats once (pollers), let the workers
triage them, make the morning brief, a check-in and the nightly digest, and returns what reached the
fake Slack DM. The console API is served on top of this runtime (agent.api).
"""

import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent.adapters.fakes.chat import FakeSlackChannel
from agent.adapters.fakes.world import FIXTURE_NOW, FakeWorld
from agent.core.clock import ShiftedClock
from agent.core.config import Config
from agent.runtime.assembly import Runtime
from agent.runtime.pollers import WATERMARK

FAKE_ME = "test.user@corp.example"
FAKE_MANAGER = "anna.keller@corp.example"


def fake_config(config: Config) -> Config:
    """The fixtures' own people and domain; everything else from the user's config."""
    mail = config.mail.model_copy(
        update={
            "manager": config.mail.manager or [FAKE_MANAGER],
            "newsletter_senders": config.mail.newsletter_senders or ["newsletter@corp.example"],
        }
    )
    return config.model_copy(
        update={"me": [FAKE_ME], "company_domains": ["corp.example"], "mail": mail, "slack": config.slack}
    )


async def build_fake_runtime(config: Config, data_dir: str | Path | None = None) -> tuple[Runtime, FakeWorld]:
    config = fake_config(config)
    folder = Path(data_dir) if data_dir else Path(tempfile.mkdtemp(prefix="agentd-fake-"))
    folder.mkdir(parents=True, exist_ok=True)
    clock = ShiftedClock(config, FIXTURE_NOW)
    world = FakeWorld(clock)
    adapters = {a.name: a for a in world.adapters()}
    runtime = await Runtime.build(
        config,
        clock,
        adapters,
        db_url=f"sqlite+aiosqlite:///{(folder / 'agent.db').as_posix()}",
        checkpoints=folder / "checkpoints.db",
        notify_channels=[FakeSlackChannel(world.slack)],
        personal_dir=folder / "memory",
    )
    return runtime, world


async def run_demo(runtime: Runtime, world: FakeWorld) -> list[dict[str, Any]]:
    """A short day on the fixtures. Returns the fake Slack DM log."""
    await runtime.load_recipes()
    week_ago = (runtime.clock.now_utc() - timedelta(days=7)).isoformat()
    for source in ("mail", "teams", "powerbi"):
        if await runtime.kv.get(WATERMARK.format(source=source)) is None:
            await runtime.kv.set(WATERMARK.format(source=source), week_ago)
    await runtime.pollers.poll_mail()
    await runtime.pollers.poll_teams()
    await runtime.drain()
    for schedule in ("brief", "checkin", "nightly_digest"):
        await runtime.scheduler.fire(schedule, manual=True)
        await runtime.drain()
    return list(world.slack.dms)
