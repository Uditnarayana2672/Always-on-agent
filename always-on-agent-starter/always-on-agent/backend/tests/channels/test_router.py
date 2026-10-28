from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.adapters.base import HealthState, HealthStatus
from agent.channels.email_self import EmailSelfChannel
from agent.channels.notifier import Notification, Notifier
from agent.channels.router import NotDelivered, NotificationRouter
from agent.channels.teams_notify import TeamsSelfChannel
from agent.core.clock import FrozenClock
from agent.core.config import NotificationsConfig, QuietHours
from agent.core.errors import AgentError
from agent.db.repositories import KvRepo

CONSOLE = "http://127.0.0.1:8765"


class Channel:
    def __init__(self, name: str, status: HealthState = "ok", fails: bool = False) -> None:
        self.name = name
        self.status = status
        self.fails = fails
        self.sent: list[str] = []

    async def health(self) -> HealthStatus:
        return HealthStatus(connector=self.name, status=self.status)

    async def send(self, text: str) -> None:
        if self.fails:
            raise ConnectionError("socket closed")
        self.sent.append(text)


async def test_slack_first_when_healthy() -> None:
    slack, teams, mail = Channel("slack"), Channel("teams_self"), Channel("email_self")
    router = NotificationRouter([mail, teams, slack])
    await router.send("Brief ready")
    assert slack.sent == ["Brief ready"] and router.last_channel == "slack"


async def test_fallback_when_slack_is_red_says_so_once() -> None:
    slack, teams, mail = Channel("slack", "down"), Channel("teams_self"), Channel("email_self")
    router = NotificationRouter([slack, teams, mail])
    await router.send("one")
    await router.send("two")
    assert slack.sent == [] and teams.sent[0].startswith("(Slack is not reachable") and teams.sent[1] == "two"
    slack.status = "ok"
    await router.send("three")
    assert slack.sent == ["three"]
    slack.status = "down"
    await router.send("four")
    assert teams.sent[-1].startswith("(Slack is not reachable")  # noted again for the new outage


async def test_a_failing_send_tries_the_next_channel() -> None:
    slack, teams, mail = (
        Channel("slack", "degraded", fails=True),
        Channel("teams_self", "down"),
        Channel("email_self"),
    )
    router = NotificationRouter([slack, teams, mail])
    await router.send("hello")
    assert mail.sent and router.last_channel == "email_self"


async def test_nothing_delivered_raises() -> None:
    router = NotificationRouter([Channel("slack", "down"), Channel("email_self", fails=True)])
    with pytest.raises(NotDelivered, match="slack"):
        await router.send("lost?")


async def test_teams_self_is_skipped_until_q4_is_verified() -> None:
    teams = TeamsSelfChannel(client=None, verified=False)
    assert (await teams.health()).status == "down"
    with pytest.raises(AgentError):
        await teams.send("x")


async def test_email_self_only_ever_goes_to_the_users_own_address() -> None:
    sent: list[tuple[str, str, str]] = []

    async def send(to: str, subject: str, text: str) -> None:
        sent.append((to, subject, text))

    channel = EmailSelfChannel(["48:notes", "Test.User@corp.example"], send, CONSOLE)
    await channel.send("Slack is down. Approve INC30114872 <!channel> reply to boss@partner.example")
    assert sent[0][0] == "test.user@corp.example" and CONSOLE in sent[0][2]
    assert (await EmailSelfChannel([], send, CONSOLE).health()).status == "down"
    await EmailSelfChannel(["me@corp.example"], send, CONSOLE).send("x" * 10000)
    assert len(sent[-1][2]) < 4200  # long content is cut; the console link carries the rest


# ---------------------------------------------------------------- notifier + router


@pytest.fixture
def notifier_parts(engine: AsyncEngine, clock: FrozenClock) -> tuple[KvRepo, NotificationsConfig]:
    return KvRepo(engine, clock), NotificationsConfig(
        primary="slack", max_pushes_per_day=2, quiet_hours=QuietHours(start="21:30", end="08:30")
    )


async def test_push_cap_batches_into_the_next_check_in(
    notifier_parts: tuple[KvRepo, NotificationsConfig], clock: FrozenClock
) -> None:
    kv, config = notifier_parts
    slack = Channel("slack")
    notifier = Notifier(config, clock, kv, NotificationRouter([slack]))
    results = [await notifier.notify(Notification(kind="alert", text=f"alert {i}")) for i in range(4)]
    assert results == ["sent", "sent", "batched", "batched"] and len(slack.sent) == 2
    assert await notifier.notify(Notification(kind="alert", text="clock at 22 h", urgent=True)) == "sent"
    assert await notifier.flush() == 2 and "alert 2" in slack.sent[-1] and "alert 3" in slack.sent[-1]


async def test_quiet_hours_hold_until_morning_even_on_the_fallback(
    notifier_parts: tuple[KvRepo, NotificationsConfig], clock: FrozenClock
) -> None:
    kv, config = notifier_parts
    clock.set(datetime(2026, 3, 25, 17, 0, tzinfo=UTC))  # 22:30 IST
    slack, mail = Channel("slack", "down"), Channel("email_self")
    notifier = Notifier(config, clock, kv, NotificationRouter([slack, mail]))
    assert await notifier.notify(Notification(kind="digest", text="digest")) == "batched"
    assert await notifier.flush() == 0 and mail.sent == []
    clock.advance(timedelta(hours=10))  # 08:30 IST
    assert await notifier.flush() == 1 and mail.sent and "digest" in mail.sent[0]


async def test_undelivered_push_is_not_counted(
    notifier_parts: tuple[KvRepo, NotificationsConfig], clock: FrozenClock
) -> None:
    kv, config = notifier_parts
    notifier = Notifier(config, clock, kv, NotificationRouter([Channel("slack", "down")]))
    with pytest.raises(NotDelivered):
        await notifier.notify(Notification(kind="alert", text="x"))
    assert await notifier.pushes_today() == 0
