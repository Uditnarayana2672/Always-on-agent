from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.core.config import ClockConfig
from agent.core.errors import IllegalTransition, LinkConflict
from agent.db.repositories import ClockRepo, MemoryRepo, Task, TaskRepo
from agent.ledger import clocks
from agent.ledger.service import Ledger
from agent.ledger.states import TRANSITIONS, check_transition, path

STATES = ["new", "scheduled", "in_progress", "needs_you", "waiting", "done", "failed"]
LEGAL = {(a, b) for a, targets in TRANSITIONS.items() for b in targets}
EXPECTED_LEGAL = {
    ("new", "in_progress"),
    ("scheduled", "in_progress"),
    ("in_progress", "needs_you"),
    ("needs_you", "in_progress"),
    ("in_progress", "waiting"),
    ("waiting", "in_progress"),
    ("waiting", "needs_you"),
    ("in_progress", "done"),
    ("waiting", "done"),
    ("needs_you", "done"),
    ("done", "in_progress"),
    ("in_progress", "failed"),
    ("failed", "needs_you"),
}

# Friday 2026-03-20 18:00 IST, the weekend in between, Monday 2026-03-23 09:00 IST.
FRI_1800 = datetime(2026, 3, 20, 12, 30, tzinfo=UTC)
MON_0900 = datetime(2026, 3, 23, 3, 30, tzinfo=UTC)


def make(engine: AsyncEngine, clock: FrozenClock, **cfg: object) -> Ledger:
    config = ClockConfig(
        start=cfg.get("start", "opened"),  # type: ignore[arg-type]
        pause_on_hold=bool(cfg.get("pause_on_hold", False)),
        thresholds_hours=[12, 18, 22],
    )
    return Ledger(TaskRepo(engine, clock), MemoryRepo(engine, clock), ClockRepo(engine, clock), clock, config)


async def new_task(ledger: Ledger, task_id: str = "INC30114872", state: str = "new") -> Task:
    return await ledger.create(Task(id=task_id, project="doxis", kind="incident", title="t", state=state))


async def force_state(engine: AsyncEngine, clock: FrozenClock, task_id: str, state: str) -> None:
    await TaskRepo(engine, clock).update(task_id, state=state)


# ---------------------------------------------------------------- state machine (pure)


def test_transition_table_matches_architecture() -> None:
    assert LEGAL == EXPECTED_LEGAL


@pytest.mark.parametrize(("src", "dst"), sorted(product(STATES, STATES)))
def test_every_pair(src: str, dst: str) -> None:
    if (src, dst) not in LEGAL:
        with pytest.raises(IllegalTransition):
            check_transition(src, dst, "user")
        return
    if dst == "done":
        check_transition(src, dst, "user")
        for actor in ("agent", "worker", "scheduler", "source_system"):
            with pytest.raises(IllegalTransition, match="only the user"):
                check_transition(src, dst, actor)
        check_transition(src, dst, "source_system", source_state="Closed")
        with pytest.raises(IllegalTransition):
            check_transition(src, dst, "source_system", source_state="Resolved")
        with pytest.raises(IllegalTransition):
            check_transition(src, dst, "agent", source_state="Closed")  # only the source system
    else:
        for actor in ("user", "agent", "worker", "scheduler", "source_system"):
            check_transition(src, dst, actor)


def test_path_never_passes_through_done() -> None:
    assert path("new", "waiting") == ["in_progress", "waiting"]
    assert path("failed", "in_progress") == ["needs_you", "in_progress"]
    assert path("waiting", "waiting") == []
    assert path("done", "waiting") == ["in_progress", "waiting"]
    with pytest.raises(IllegalTransition):
        path("in_progress", "new")
    with pytest.raises(IllegalTransition):
        path("in_progress", "scheduled")


# ---------------------------------------------------------------- ledger transitions


async def test_ledger_transition_records_event(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock)
    await new_task(ledger)
    task = await ledger.transition("INC30114872", "in_progress", "worker", "first run")
    assert task.state == "in_progress"
    events = await MemoryRepo(engine, clock).task_events("INC30114872")
    assert events[-1]["text_pseudonymised"] == "state new -> in_progress: first run"
    assert events[-1]["actor"] == "worker" and events[-1]["channel"] == "ledger"


async def test_create_must_start_new_or_scheduled(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock)
    with pytest.raises(IllegalTransition):
        await new_task(ledger, state="in_progress")
    await new_task(ledger, "INC1", state="scheduled")


async def test_illegal_transition_leaves_state(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock)
    await new_task(ledger)
    with pytest.raises(IllegalTransition):
        await ledger.transition("INC30114872", "done", "user")
    assert (await ledger.get("INC30114872")).state == "new"
    with pytest.raises(KeyError):
        await ledger.get("missing")


async def test_agent_cannot_close(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock)
    await new_task(ledger)
    await ledger.transition("INC30114872", "in_progress", "worker")
    with pytest.raises(IllegalTransition, match="only the user"):
        await ledger.transition("INC30114872", "done", "agent")
    await ledger.transition("INC30114872", "done", "user")


async def test_concurrent_change_is_detected(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock)
    await new_task(ledger)
    repo = TaskRepo(engine, clock)
    assert await repo.transition("INC30114872", "new", "in_progress")
    assert not await repo.transition("INC30114872", "new", "in_progress")


async def test_reopen_moves_done_to_in_progress(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock)
    await new_task(ledger)
    await ledger.transition("INC30114872", "in_progress", "worker")
    await ledger.transition("INC30114872", "done", "user")
    task = await ledger.reopen("INC30114872")
    assert task.state == "in_progress"
    facts = {f["key"]: f["value"] for f in await MemoryRepo(engine, clock).facts("INC30114872")}
    assert facts["chasing"] == "active"
    with pytest.raises(IllegalTransition):
        await ledger.reopen("INC30114872")  # not done any more


async def test_close_from_source(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock)
    await new_task(ledger)
    task = await ledger.close_from_source("INC30114872", "Closed")
    assert task.state == "done"
    assert (await ledger.close_from_source("INC30114872", "Closed")).state == "done"  # idempotent
    await new_task(ledger, "INC2")
    await ledger.move_to("INC2", "waiting", "worker")
    with pytest.raises(IllegalTransition):
        await ledger.close_from_source("INC2", "Resolved")


async def test_reassigned_away(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock)
    await new_task(ledger)
    await ledger.move_to("INC30114872", "needs_you", "worker")
    task = await ledger.reassigned_away("INC30114872", "now with Doxis L3")
    assert task.state == "waiting" and task.next_action == "Reassigned away; no chasing"
    facts = {f["key"]: f["value"] for f in await MemoryRepo(engine, clock).facts("INC30114872")}
    assert facts == {"assigned_to_me": "false", "chasing": "stopped"}
    # A done task stays done.
    await new_task(ledger, "INC2")
    await ledger.move_to("INC2", "in_progress", "worker")
    await ledger.transition("INC2", "done", "user")
    assert (await ledger.reassigned_away("INC2", "x")).state == "done"


async def test_surface_failure(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock)
    await new_task(ledger)
    task = await ledger.surface_failure("INC30114872", "boom")
    assert task.state == "needs_you"
    notes = [e["text_pseudonymised"] for e in await MemoryRepo(engine, clock).task_events("INC30114872")]
    assert notes == [
        "state new -> in_progress: boom",
        "state in_progress -> failed: boom",
        "state failed -> needs_you: surfaced to user",
    ]


# ---------------------------------------------------------------- clocks (pure)


def test_choose_start() -> None:
    o, a, e = FRI_1800, FRI_1800 + timedelta(hours=1), FRI_1800 + timedelta(hours=2)
    assert clocks.choose_start("opened", o, a, e) == (o, False)
    assert clocks.choose_start("assigned", o, a, e) == (a, False)
    assert clocks.choose_start("email_received", o, a, e) == (e, False)
    assert clocks.choose_start("assigned", o, None, e) == (e, True)  # estimated from the email
    assert clocks.choose_start("opened", None, a, None) == (a, True)
    assert clocks.choose_start("opened", None, None, None) == (None, False)


def test_paused_time_merges_and_clips() -> None:
    s = FRI_1800
    pauses = [
        (s - timedelta(hours=5), s + timedelta(hours=1)),  # starts before the clock: clipped
        (s + timedelta(hours=2), s + timedelta(hours=4)),
        (s + timedelta(hours=3), s + timedelta(hours=5)),  # overlaps the previous one
        (s + timedelta(hours=8), None),  # still open: runs to now
        (s - timedelta(hours=9), s - timedelta(hours=8)),  # entirely before start
        (s + timedelta(hours=11), s + timedelta(hours=11)),  # zero length
    ]
    assert clocks.paused_time(s, s + timedelta(hours=10), pauses) == timedelta(hours=1 + 3 + 2)
    assert clocks.paused_time(s, s + timedelta(hours=1), []) == timedelta(0)


def test_age_never_negative() -> None:
    assert clocks.age(FRI_1800, FRI_1800 - timedelta(hours=1), [], False) == timedelta(0)


# ---------------------------------------------------------------- clocks through the ledger


@pytest.mark.parametrize(("pause_on_hold", "expected_hours"), [(False, 69.0), (True, 9.0)])
async def test_clock_across_weekend(
    engine: AsyncEngine, clock: FrozenClock, pause_on_hold: bool, expected_hours: float
) -> None:
    # Opened Friday 18:00 IST, On Hold Friday 21:00 to Monday 09:00 (60 h), read Monday 15:00.
    clock.set(MON_0900 + timedelta(hours=6))
    ledger = make(engine, clock, pause_on_hold=pause_on_hold)
    await new_task(ledger)
    await ledger.set_clock_times("INC30114872", opened_at=FRI_1800)
    await ledger.pause_clock("INC30114872", FRI_1800 + timedelta(hours=3))
    assert (await ledger.get("INC30114872")).clock_paused
    await ledger.resume_clock("INC30114872", MON_0900)
    assert not (await ledger.get("INC30114872")).clock_paused
    assert await ledger.age_hours("INC30114872") == expected_hours


async def test_open_pause_counts_until_now(engine: AsyncEngine, clock: FrozenClock) -> None:
    clock.set(FRI_1800 + timedelta(hours=10))
    ledger = make(engine, clock, pause_on_hold=True)
    await new_task(ledger)
    await ledger.set_clock_times("INC30114872", opened_at=FRI_1800)
    await ledger.pause_clock("INC30114872", FRI_1800 + timedelta(hours=4))
    await ledger.pause_clock("INC30114872", FRI_1800 + timedelta(hours=5))  # already paused: ignored
    assert await ledger.age_hours("INC30114872") == 4.0
    clock.advance(timedelta(hours=10))
    assert await ledger.age_hours("INC30114872") == 4.0  # still paused
    await ledger.resume_clock("INC30114872")
    await ledger.resume_clock("INC30114872")  # nothing open: no-op
    clock.advance(timedelta(hours=1))
    assert await ledger.age_hours("INC30114872") == 5.0


async def test_clock_start_rule_and_estimate(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock, start="assigned")
    await new_task(ledger)
    opened = FRI_1800
    received = FRI_1800 + timedelta(hours=5)
    task = await ledger.set_clock_times("INC30114872", opened_at=opened, email_received_at=received)
    assert task.clock_started_at == received and task.clock_estimated
    task = await ledger.set_clock_times("INC30114872", assigned_at=opened + timedelta(hours=1))
    assert task.clock_started_at == opened + timedelta(hours=1) and not task.clock_estimated
    assert task.opened_at == opened and task.email_received_at == received  # all kept
    await new_task(ledger, "INC2")
    assert await ledger.age("INC2") is None
    assert await ledger.due_thresholds("INC2") == []


async def test_thresholds_fire_once(engine: AsyncEngine, clock: FrozenClock) -> None:
    clock.set(FRI_1800 + timedelta(hours=11))
    ledger = make(engine, clock)
    await new_task(ledger)
    await ledger.set_clock_times("INC30114872", opened_at=FRI_1800)
    assert await ledger.due_thresholds("INC30114872") == []
    clock.advance(timedelta(hours=1))
    assert await ledger.due_thresholds("INC30114872") == [12]
    assert await ledger.due_thresholds("INC30114872") == []
    clock.advance(timedelta(hours=11))  # 23 h: 18 and 22 together, once
    assert await ledger.due_thresholds("INC30114872") == [18, 22]
    clock.advance(timedelta(hours=30))
    assert await ledger.due_thresholds("INC30114872") == []


async def test_thresholds_respect_pause_and_done(engine: AsyncEngine, clock: FrozenClock) -> None:
    clock.set(FRI_1800 + timedelta(hours=20))
    ledger = make(engine, clock, pause_on_hold=True)
    await new_task(ledger)
    await ledger.set_clock_times("INC30114872", opened_at=FRI_1800)
    await ledger.pause_clock("INC30114872", FRI_1800 + timedelta(hours=2))
    assert await ledger.due_thresholds("INC30114872") == []  # only 2 h counted
    await ledger.move_to("INC30114872", "in_progress", "worker")
    await ledger.transition("INC30114872", "done", "user")
    await ledger.resume_clock("INC30114872", FRI_1800 + timedelta(hours=3))
    assert await ledger.due_thresholds("INC30114872") == []  # done tasks never alert


# ---------------------------------------------------------------- heartbeats


async def test_stalled(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock)
    await new_task(ledger, "INC1")
    await new_task(ledger, "INC2")
    await new_task(ledger, "INC3")
    await ledger.move_to("INC3", "in_progress", "worker")
    await ledger.transition("INC3", "done", "user")
    clock.advance(timedelta(hours=20))
    await ledger.touch("INC2")
    clock.advance(timedelta(hours=5))
    stalled = await ledger.stalled()
    assert [t.id for t in stalled] == ["INC1"]  # INC2 touched 5 h ago, INC3 done
    clock.advance(timedelta(hours=20))
    assert [t.id for t in await ledger.stalled()] == ["INC1", "INC2"]


# ---------------------------------------------------------------- links


async def test_links_are_unique_per_kind_and_id(engine: AsyncEngine, clock: FrozenClock) -> None:
    ledger = make(engine, clock)
    await new_task(ledger, "INC1")
    await new_task(ledger, "INC2")
    await ledger.add_link("INC1", "mail_conversation", "C1")
    await ledger.add_link("INC1", "mail_conversation", "C1")  # idempotent
    await ledger.add_link("INC2", "teams_chat", "C1")  # same id, other kind: fine
    with pytest.raises(LinkConflict):
        await ledger.add_link("INC2", "mail_conversation", "C1")
    with pytest.raises(ValueError, match="unknown link kind"):
        await ledger.add_link("INC1", "fax", "1")
    with pytest.raises(ValueError, match="empty"):
        await ledger.add_link("INC1", "servicenow", "")
    links = {(r["kind"], r["external_id"]) for r in await MemoryRepo(engine, clock).links("INC1")}
    assert links == {("mail_conversation", "C1")}
