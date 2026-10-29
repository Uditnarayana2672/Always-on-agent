from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.channels.notifier import Notification, Notifier
from agent.core.clock import FrozenClock
from agent.core.config import CONFIG_DIR, load_config
from agent.core.errors import ControlError
from agent.db.repositories import ActionRepo, AuditRepo, KvRepo, MemoryRepo, Task, TaskRepo
from agent.policy.gate import Gate, GateContext
from agent.runtime.controls import Controls, Resetter
from agent.runtime.settings import OVERRIDES_KEY, POLICY_KEY
from tests.policy import tools as t

T0 = datetime(2026, 3, 25, 4, 30, tzinfo=UTC)  # Wed 10:00 IST


class Releaser:
    def __init__(self) -> None:
        self.calls = 0

    async def release_held(self) -> list[str]:
        self.calls += 1
        return ["A1"]


class Sink:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(load_config(CONFIG_DIR / "config.example.yaml"), T0)


# ---------------------------------------------------------------- pause, resume, leave


async def test_pause_blocks_everything_above_silent(engine: AsyncEngine, clock: FrozenClock) -> None:
    controls = Controls(KvRepo(engine, clock), clock, AuditRepo(engine, clock))
    await controls.pause()
    gate = Gate(t.build(), t.COMPANY, t.GROUPS)
    ctx = GateContext(paused=await controls.paused(), participants=frozenset({t.CALLER}))
    assert gate.check("search_mail", {"query": "x"}, ctx).executable_now  # Silent still runs
    for tool, args in [
        ("move_mail", {"message_id": "m", "folder": "Done"}),
        ("post_chat", {"chat_id": "c", "body": "hi", "recipients": [t.CALLER]}),
    ]:
        decision = gate.check(tool, args, ctx)
        assert decision.held and not decision.executable_now


async def test_state_survives_a_restart_and_is_audited(engine: AsyncEngine, clock: FrozenClock) -> None:
    kv, audit = KvRepo(engine, clock), AuditRepo(engine, clock)
    await Controls(kv, clock, audit).pause(actor="slack")
    again = Controls(kv, clock, audit)
    state = await again.state()
    assert state.paused and state.paused_at == T0 and state.changed_by == "slack"
    await again.pause()  # idempotent
    await again.leave_mode(True)
    released = Releaser()
    await Controls(kv, clock, audit, approvals=released).resume()
    assert released.calls == 1 and not await again.paused() and await again.on_leave()
    tools = [r["tool"] for r in reversed(await audit.recent())]
    assert tools == ["control:pause", "control:leave_on", "control:resume"]


async def test_resume_when_not_paused_does_nothing(engine: AsyncEngine, clock: FrozenClock) -> None:
    released = Releaser()
    result = await Controls(
        KvRepo(engine, clock), clock, AuditRepo(engine, clock), approvals=released
    ).resume()
    assert result.released == [] and released.calls == 0


# ---------------------------------------------------------------- reset


async def _seed(engine: AsyncEngine, clock: FrozenClock) -> ActionRepo:
    await TaskRepo(engine, clock).create(
        Task(id="INC30114872", project="doxis", kind="incident", title="t", state="new")
    )
    await MemoryRepo(engine, clock).set_fact("INC30114872", "participants", "[]")
    actions = ActionRepo(engine, clock)
    await actions.create("A1", "INC30114872", "post_chat", {}, "ask", "proposed", "h", "k1")
    await AuditRepo(engine, clock).append(
        "post_chat", "ask", "h", "decision:proposed", "agent", "INC30114872", action_id="A1"
    )
    await KvRepo(engine, clock).set("delta.mail", "token-1")
    await KvRepo(engine, clock).set("schedule.missed.brief", "2026-03-24")
    return actions


async def test_reset_needs_token_and_pause_and_keeps_the_audit_log(
    engine: AsyncEngine, clock: FrozenClock
) -> None:
    actions = await _seed(engine, clock)
    kv, audit = KvRepo(engine, clock), AuditRepo(engine, clock)
    forgotten: list[str] = []

    async def forget(task_id: str) -> None:
        forgotten.append(task_id)

    controls = Controls(kv, clock, audit, resetter=Resetter(engine, actions, clock, forget))
    with pytest.raises(ControlError, match="first"):
        await controls.reset("anything")
    token = await controls.request_reset()
    with pytest.raises(ControlError, match="does not match"):
        await controls.reset("wrong")
    with pytest.raises(ControlError, match="pause"):
        await controls.reset(token)

    await controls.pause()
    await kv.set(OVERRIDES_KEY, '{"schedule": {"brief": "08:30"}}')
    await kv.set(POLICY_KEY, '{"save_draft": "report"}')
    result = await controls.reset(token)
    assert (result.tasks, result.actions_detached, forgotten) == (1, 1, ["INC30114872"])
    assert await kv.get(OVERRIDES_KEY) and await kv.get(POLICY_KEY)  # Settings page changes stay
    assert await TaskRepo(engine, clock).list() == []
    action = await actions.get("A1")
    assert action is not None and action.task_id is None and action.status == "invalidated"
    assert [r["tool"] for r in await audit.for_action("A1")] == ["post_chat"]  # kept
    assert await kv.get("delta.mail") == "token-1"  # intake resumes where it was
    assert await kv.get("schedule.missed.brief") is None
    assert await controls.paused()  # still paused after the reset
    with pytest.raises(ControlError, match="first"):
        await controls.reset(token)  # single use
    async with engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM audit_log WHERE tool = 'control:reset (1 tasks)'"))
        ).scalar_one() == 1


async def test_reset_token_expires(engine: AsyncEngine, clock: FrozenClock) -> None:
    actions = await _seed(engine, clock)
    controls = Controls(
        KvRepo(engine, clock), clock, AuditRepo(engine, clock), resetter=Resetter(engine, actions, clock)
    )
    token = await controls.request_reset()
    await controls.pause()
    clock.advance(timedelta(minutes=11))
    with pytest.raises(ControlError, match="expired"):
        await controls.reset(token)


# ---------------------------------------------------------------- notifications


def notifier(engine: AsyncEngine, clock: FrozenClock, sink: Sink) -> Notifier:
    cfg = load_config(CONFIG_DIR / "config.example.yaml")
    return Notifier(cfg.notifications, clock, KvRepo(engine, clock), sink)


async def test_quiet_hours_batch_until_the_next_checkin(engine: AsyncEngine, clock: FrozenClock) -> None:
    sink = Sink()
    n = notifier(engine, clock, sink)
    clock.set(datetime(2026, 3, 25, 17, 0, tzinfo=UTC))  # 22:30 IST: quiet
    assert await n.notify(Notification(kind="alert", text="Refresh failed", task_id="REF-0412")) == "batched"
    assert await n.notify(Notification(kind="info", text="Two new mails")) == "batched"
    assert await n.flush() == 0 and sink.sent == []  # a check-in in quiet hours waits too
    assert await n.notify(Notification(kind="job_failed", text="Job stopped", urgent=True)) == "sent"

    clock.set(datetime(2026, 3, 26, 3, 30, tzinfo=UTC))  # 09:00 IST check-in
    assert await n.flush() == 2
    assert sink.sent[-1].startswith("2 update(s)") and "REF-0412" in sink.sent[-1]
    assert await n.batched() == []


async def test_daily_push_cap(engine: AsyncEngine, clock: FrozenClock) -> None:
    sink = Sink()
    n = notifier(engine, clock, sink)
    results = [await n.notify(Notification(kind="alert", text=f"a{i}")) for i in range(8)]
    assert results == ["sent"] * 6 + ["batched"] * 2
    assert await n.notify(Notification(kind="alert", text="urgent", urgent=True)) == "sent"
    clock.advance(timedelta(days=1))  # a new local day resets the cap
    assert await n.notify(Notification(kind="alert", text="next day")) == "sent"
