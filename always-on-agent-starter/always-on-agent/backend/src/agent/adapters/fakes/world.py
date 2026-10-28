"""FakeWorld: every fake adapter wired to one clock (tests and agentd run --fake)."""

from datetime import UTC, datetime
from pathlib import Path

from agent.adapters.base import HealthStatus
from agent.adapters.fakes._base import FIXTURES_DIR, FakeAdapter
from agent.adapters.fakes.chat import FakeSlack, FakeTeams
from agent.adapters.fakes.outlook import FakeCalendar, FakeOutlook
from agent.adapters.fakes.powerbi import FakePowerBI
from agent.adapters.fakes.sources import FakeOcr, FakeServiceNow, FakeSharePoint, FakeSql
from agent.core.clock import Clock, FrozenClock
from agent.core.config import Config, load_config

# The fixtures describe Wednesday 2026-03-25; 09:15 IST is just after the morning brief.
FIXTURE_NOW = datetime(2026, 3, 25, 3, 45, tzinfo=UTC)


class FakeWorld:
    def __init__(self, clock: Clock, fixtures: Path = FIXTURES_DIR) -> None:
        self.clock = clock
        self.mail = FakeOutlook(clock, fixtures)
        self.calendar = FakeCalendar(clock, fixtures)
        self.teams = FakeTeams(clock, fixtures)
        self.slack = FakeSlack(clock, fixtures)
        self.powerbi = FakePowerBI(clock, fixtures)
        self.sharepoint = FakeSharePoint(clock, fixtures)
        self.servicenow = FakeServiceNow(clock, fixtures)
        self.sql = FakeSql(clock, fixtures)
        self.ocr = FakeOcr(clock, fixtures)

    @classmethod
    def frozen(cls, config: Config | None = None, at: datetime = FIXTURE_NOW) -> "FakeWorld":
        """A world with a FrozenClock at the fixtures' 'now'."""
        return cls(FrozenClock(config or load_config(), at))

    def adapters(self) -> list[FakeAdapter]:
        return [
            self.mail,
            self.calendar,
            self.teams,
            self.slack,
            self.powerbi,
            self.sharepoint,
            self.servicenow,
            self.sql,
            self.ocr,
        ]

    def all_writes(self) -> list[tuple[str, str]]:
        return [(a.name, w.method) for a in self.adapters() for w in a.writes]

    async def health(self) -> list[HealthStatus]:
        return [await a.health() for a in self.adapters()]
