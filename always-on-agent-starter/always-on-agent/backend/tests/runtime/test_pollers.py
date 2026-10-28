from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.adapters.base import Dataflow, MailChanges, MailMessage, Person, Refresh
from agent.adapters.fakes.world import FakeWorld
from agent.core.clock import FrozenClock
from agent.core.errors import AdapterError, NeedsSignIn
from agent.db.repositories import EventRepo, JobRepo, KvRepo, MemoryRepo, TaskRepo
from agent.runtime.coordinator import Coordinator, IngestResult
from agent.runtime.pollers import OVERLAP, Pollers
from tests.adapters.graph_helpers import Health, Kv

NOW = datetime(2026, 3, 25, 4, 30, tzinfo=UTC)


def mail(mid: str, at: datetime = NOW) -> MailMessage:
    return MailMessage(
        id=mid,
        conversation_id=f"c-{mid}",
        subject=f"INC3011487{mid[-1]} help",
        sender=Person(name="S", address="sophie.laurent@corp.example"),
        received_at=at,
        body="",
    )


class Recorder:
    def __init__(self, fail_on: str | None = None) -> None:
        self.seen: list[str] = []
        self.fail_on = fail_on

    async def __call__(self, raw: Any, source: Any) -> IngestResult:
        key = getattr(raw, "id", None) or raw["external_id"]
        if key == self.fail_on:
            self.fail_on = None
            raise AdapterError("database busy")
        self.seen.append(str(key))
        return IngestResult(event_id=len(self.seen))


class DeltaMail:
    def __init__(self) -> None:
        self.pending = {"inbox": [mail("m1"), mail("m2")], "sentitems": []}
        self.committed: list[str] = []

    async def changes(self, folder: str) -> MailChanges:
        return MailChanges(folder=folder, messages=self.pending[folder], commit_token=f"{folder}-token")

    async def commit_delta(self, changes: MailChanges) -> None:
        self.committed.append(changes.folder)
        self.pending[changes.folder] = []


async def test_delta_is_committed_only_after_every_item_was_ingested(clock: FrozenClock) -> None:
    source, health = DeltaMail(), Health()
    ingest = Recorder(fail_on="m2")
    pollers = Pollers(ingest, Kv(), clock, mail=source, health=health)
    assert await pollers.poll_mail() == 0  # m2 failed: the poll gives up quietly ...
    assert source.committed == [] and health.last() == "degraded"  # ... and keeps the old token
    assert await pollers.poll_mail() == 2  # the next poll re-reads both
    assert source.committed == ["inbox", "sentitems"] and health.last() == "ok"
    assert ingest.seen == ["m1", "m1", "m2"]  # the coordinator drops the second m1


class WatermarkMail:
    def __init__(self, items: list[MailMessage]) -> None:
        self.items = items
        self.asked: list[datetime] = []

    async def list_new(self, since: datetime) -> list[MailMessage]:
        self.asked.append(since)
        return [m for m in self.items if m.received_at > since]


async def test_com_watermark_reads_back_with_overlap(clock: FrozenClock) -> None:
    kv = Kv()
    early = mail("m1", NOW - timedelta(minutes=30))
    source = WatermarkMail([early])
    pollers = Pollers(Recorder(), kv, clock, mail=source)
    await pollers.poll_mail()
    assert kv.data["poll.mail.since"] == early.received_at.isoformat()
    late = mail("m2", early.received_at - timedelta(minutes=5))  # synced late, received earlier
    source.items.append(late)
    ingest = Recorder()
    await Pollers(ingest, kv, clock, mail=source).poll_mail()
    assert source.asked[-1] == early.received_at - OVERLAP and "m2" in ingest.seen


async def test_signed_out_marks_down(clock: FrozenClock) -> None:
    class SignedOut:
        async def changes(self, folder: str) -> MailChanges:
            raise NeedsSignIn("token expired")

    health = Health()
    assert await Pollers(Recorder(), Kv(), clock, mail=SignedOut(), health=health).poll_mail() == 0
    assert health.reports[-1][1] == "down" and "sign in" in (health.reports[-1][2] or "")


async def test_teams_fake_changes_flow_through(clock: FrozenClock) -> None:
    world = FakeWorld(clock)
    ingest = Recorder()
    kv = Kv()
    kv.data["poll.teams.since"] = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    count = await Pollers(ingest, kv, clock, teams=world.teams).poll_teams()
    assert count == 6 and len(ingest.seen) == 6


class FakeRefreshes:
    def __init__(self, history: list[Refresh], flows: list[Dataflow] | None = None) -> None:
        self.history = history
        self.flows = flows or []

    async def refresh_history(self, dataset_id: str, top: int = 60) -> list[Refresh]:
        return self.history[:top]

    async def upstream_dataflows(self, dataset_id: str) -> list[Dataflow]:
        return self.flows


def refresh(
    rid: str, status: Any, start: datetime, end: datetime | None, error: str | None = None
) -> Refresh:
    return Refresh(
        request_id=rid, status=status, refresh_type="Scheduled", start=start, end=end, error_code=error
    )


async def test_powerbi_failures_stuck_runs_and_dataflows_become_events_once(
    clock: FrozenClock, engine: AsyncEngine
) -> None:
    history = [
        refresh("r3", "Unknown", NOW - timedelta(hours=3), None),  # running for 3 h: stuck
        refresh(
            "r2",
            "Failed",
            NOW - timedelta(minutes=40),
            NOW - timedelta(minutes=30),
            "DM_GWPipeline_Gateway_TimeoutError",
        ),
        refresh("r1", "Completed", NOW - timedelta(days=2), NOW - timedelta(days=2)),  # before the watermark
    ]
    flows = [Dataflow(id="f1", name="Staging", last_status="Failed", last_end=NOW - timedelta(minutes=20))]
    kv = KvRepo(engine, clock)
    coordinator = Coordinator(
        TaskRepo(engine, clock),
        EventRepo(engine, clock),
        JobRepo(engine, clock),
        MemoryRepo(engine, clock),
        clock,
        create_rules=[],
    )
    pollers = Pollers(
        coordinator.ingest, kv, clock, powerbi=FakeRefreshes(history, flows), watch_datasets=["ds"]
    )
    assert await pollers.poll_powerbi() == 3
    events = EventRepo(engine, clock)
    kinds = sorted(e["kind"] for e in await events.by_status("digest") + await events.by_status("routed"))
    assert kinds == ["dataflow_refresh", "refresh", "refresh_stuck"]
    await pollers.poll_powerbi()  # the same items again: all duplicates
    again = await events.by_status("digest") + await events.by_status("routed")
    assert len(again) == 3


async def test_handlers_are_named_like_the_schedules(clock: FrozenClock) -> None:
    world = FakeWorld(clock)
    pollers = Pollers(Recorder(), Kv(), clock, mail=world.mail, teams=world.teams, powerbi=world.powerbi)
    assert set(pollers.handlers()) == {"poll:mail", "poll:teams", "poll:powerbi"}


@pytest.mark.parametrize("missing", ["mail", "teams", "powerbi"])
def test_missing_sources_have_no_handler(clock: FrozenClock, missing: str) -> None:
    world = FakeWorld(clock)
    sources = {"mail": world.mail, "teams": world.teams, "powerbi": world.powerbi}
    sources.pop(missing)
    assert f"poll:{missing}" not in Pollers(Recorder(), Kv(), clock, **sources).handlers()  # type: ignore[arg-type]
