import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.core.config import WorkersConfig
from agent.core.errors import ApprovalConflict
from agent.db.repositories import ActionRepo
from agent.graphs.runner import open_checkpointer
from agent.runtime.workers import WorkerPool
from tests.graphs.harness import TASK, Harness
from tests.policy.tools import CALLER, ME


@pytest.fixture
async def h(engine: AsyncEngine, clock: FrozenClock, tmp_path: Path) -> AsyncIterator[Harness]:
    async with open_checkpointer(tmp_path / "checkpoints.db") as saver:
        yield Harness(engine, clock, saver)


async def proposed(h: Harness) -> Any:
    """Event in, draft out: the graph is parked on an approval card."""
    await h.caller_mail("M1")
    assert await h.drain() == [("new_event", "parked")]
    action = await h.only_action()
    assert action.status == "proposed"
    return action


# ---------------------------------------------------------------- the full loop


async def test_full_loop_event_draft_card_approve_execute_once(h: Harness) -> None:
    action = await proposed(h)
    assert await h.state() == "needs_you"
    assert await h.runner.waiting_on(TASK) == {"kind": "approval", "action_id": action.id}
    card = h.slack.open_cards()[0]
    assert card.content == "Hello, the login works again. (v1)" and card.content_hash == action.content_hash
    assert len(h.console.open_cards()) == 1
    assert h.posts() == []  # nothing before the approval

    outcome = await h.approvals.approve(action.id, card.content_hash, actor="slack")
    assert outcome.changed and outcome.status == "approved"
    assert set(h.console.status.values()) == {"approved"}  # the other surface is disabled
    assert await h.drain() == [("resume", "done")]

    assert h.posts() == ["Hello, the login works again. (v1)"]
    assert (await h.actions.get(action.id)).status == "done"  # type: ignore[union-attr]
    assert await h.state() == "in_progress"
    assert await h.runner.waiting_on(TASK) is None
    results = [r["result"] for r in await h.audit.for_action(action.id)]
    assert results[0].startswith("decision:proposed") and "approved" in results and results[-1] == "done"
    task = await h.tasks.get(TASK)
    assert task is not None and task.next_action == "Done; see the activity log."


async def test_full_loop_with_the_worker_pool(h: Harness) -> None:
    pool = WorkerPool(
        h.jobs,
        h.handlers,
        h.clock,
        WorkersConfig(pool_size=2, max_steps_per_run=4, job_timeout_seconds=30, lock_ttl_minutes=10),
        poll_interval=0.01,
    )
    await pool.start()
    try:
        await h.caller_mail("M1")
        await _until(lambda: h.slack.open_cards())
        card = h.slack.open_cards()[0]
        await h.approvals.approve(card.action_id, card.content_hash, actor="slack")
        await _until(lambda: h.posts())
    finally:
        await pool.stop()
    assert h.posts() == ["Hello, the login works again. (v1)"]


async def _until(cond: Any, timeout: float = 5.0) -> None:
    async def poll() -> None:
        while not cond():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


# ---------------------------------------------------------------- edits


async def test_edit_then_approve_the_edited_hash(h: Harness) -> None:
    action = await proposed(h)
    edited = await h.approvals.edit(action.id, "Hello, fixed now. Please try again.", actor="console")
    assert edited.changed and edited.content_hash != action.content_hash
    assert [c.edited for c in h.slack.open_cards()] == [True]  # old card closed, new card open
    assert h.slack.status["slack:1"] == "edited"

    with pytest.raises(ApprovalConflict, match="changed"):
        await h.approvals.approve(action.id, action.content_hash)  # the old hash no longer approves
    assert await h.drain() == []

    await h.approvals.approve(action.id, edited.content_hash)
    await h.drain()
    assert h.posts() == ["Hello, fixed now. Please try again."]


async def test_whitespace_only_edit_is_no_change(h: Harness) -> None:
    action = await proposed(h)
    same = await h.approvals.edit(action.id, "  Hello, the login works again. (v1)  \r\n")
    assert not same.changed and same.content_hash == action.content_hash


async def test_edit_needs_an_editable_tool_and_valid_text(h: Harness) -> None:
    action = await proposed(h)
    with pytest.raises(ApprovalConflict, match="empty"):
        await h.approvals.edit(action.id, "   ")
    await h.approvals.reject(action.id, "no")
    with pytest.raises(ApprovalConflict, match="rejected"):
        await h.approvals.edit(action.id, "new text")


async def test_two_editors_racing(h: Harness) -> None:
    action = await proposed(h)
    await h.approvals.edit(action.id, "First edit", expected_hash=action.content_hash)
    with pytest.raises(ApprovalConflict, match="same time"):
        await h.approvals.edit(action.id, "Second edit", expected_hash=action.content_hash)


# ---------------------------------------------------------------- invalidation


async def test_caller_reply_invalidates_and_a_new_draft_follows(h: Harness) -> None:
    first = await proposed(h)
    await h.caller_mail("M2", subject="RE: still broken?")  # same conversation: routed by link
    assert (await h.actions.get(first.id)).status == "invalidated"  # type: ignore[union-attr]
    assert h.slack.status["slack:1"] == "invalidated"

    ran = await h.drain()
    assert ran == [("resume", "done"), ("new_event", "parked")]  # old run closed, new draft parked
    second = await h.only_action()
    assert second.id != first.id and second.status == "proposed"
    with pytest.raises(ApprovalConflict, match="invalidated"):
        await h.approvals.approve(first.id, first.content_hash)
    await h.approvals.approve(second.id, second.content_hash)
    await h.drain()
    assert h.posts() == ["Hello, the login works again. (v2)"]


async def test_users_own_message_does_not_invalidate(h: Harness) -> None:
    action = await proposed(h)
    await h.caller_mail("M2", subject="RE: note to self", sender=ME)
    assert (await h.actions.get(action.id)).status == "proposed"  # type: ignore[union-attr]
    assert await h.drain() == [("new_event", "done")]  # recorded; the graph keeps waiting
    assert await h.runner.waiting_on(TASK) == {"kind": "approval", "action_id": action.id}
    await h.approvals.approve(action.id, action.content_hash)
    await h.drain()
    assert len(h.posts()) == 1


async def test_reply_after_approve_but_before_send_still_stops_it(h: Harness) -> None:
    action = await proposed(h)
    await h.approvals.approve(action.id, action.content_hash)
    await h.caller_mail("M2", subject="RE: never mind, it works")  # arrives before the resume job runs
    assert (await h.actions.get(action.id)).status == "invalidated"  # type: ignore[union-attr]
    await h.drain()
    assert h.posts() == []  # the stale reply was never sent


async def test_same_draft_after_invalidation_needs_a_fresh_approval(h: Harness) -> None:
    h.step.text = "Fixed.{fixed}"  # the redraft is word for word the same
    action = await proposed(h)
    await h.caller_mail("M2", subject="RE: thanks")
    await h.drain()
    again = await h.actions.get(action.id)
    assert again is not None and again.status == "proposed" and again.decided_by is None
    assert len(h.slack.open_cards()) == 1  # shown again
    await h.approvals.approve(action.id, action.content_hash)
    await h.drain()
    assert h.posts() == ["Fixed."]


# ---------------------------------------------------------------- double clicks and stale jobs


async def test_double_approve_resolves_once(h: Harness) -> None:
    action = await proposed(h)
    a, b = await asyncio.gather(
        h.approvals.approve(action.id, action.content_hash, actor="slack"),
        h.approvals.approve(action.id, action.content_hash, actor="console"),
    )
    assert sorted([a.changed, b.changed]) == [False, True]
    again = await h.approvals.approve(action.id, action.content_hash, actor="slack")
    assert not again.changed
    resumes = [j for j in await h.jobs.for_task(TASK) if j.kind == "resume"]
    assert len(resumes) == 1
    await h.drain()
    assert len(h.posts()) == 1


async def test_approve_and_reject_race(h: Harness) -> None:
    action = await proposed(h)
    await h.approvals.reject(action.id, "wrong tone", actor="console")
    with pytest.raises(ApprovalConflict, match="rejected"):
        await h.approvals.approve(action.id, action.content_hash, actor="slack")
    await h.drain()
    assert h.posts() == [] and await h.state() == "in_progress"
    task = await h.tasks.get(TASK)
    assert task is not None and task.next_action == "You rejected the proposed action."


async def test_stale_resume_job_does_nothing(h: Harness) -> None:
    action = await proposed(h)
    await h.coordinator.enqueue_resume(TASK, "SOME-OTHER-ACTION")
    assert await h.drain() == [("resume", "done")]
    assert await h.runner.waiting_on(TASK) == {"kind": "approval", "action_id": action.id}


# ---------------------------------------------------------------- crash recovery


async def test_crash_between_execute_and_record_does_not_double_send(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = await proposed(h)
    await h.approvals.approve(action.id, action.content_hash)
    real = ActionRepo.transition
    crashed = {"done": False}

    async def crash_before_recording(
        self: ActionRepo, action_id: str, frm: str, to: str, *a: Any, **kw: Any
    ) -> None:
        if to == "done" and not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("process died after the adapter call")
        await real(self, action_id, frm, to, *a, **kw)

    monkeypatch.setattr(ActionRepo, "transition", crash_before_recording)
    ran = await h.drain()
    assert ran[0] == ("resume", "error")  # the first run died after posting
    assert ran[-1][1] == "done"
    assert len(h.posts()) == 1  # found by its tag in the chat; not posted again
    assert (await h.actions.get(action.id)).status == "done"  # type: ignore[union-attr]
    assert await h.runner.waiting_on(TASK) is None


# ---------------------------------------------------------------- expiry, closing, pause


async def test_expiry_keeps_the_draft_and_needs_you(h: Harness) -> None:
    action = await proposed(h)
    h.clock.advance(timedelta(hours=24, minutes=1))
    assert await h.approvals.expire() == [action.id]
    with pytest.raises(ApprovalConflict, match="already expired"):
        await h.approvals.approve(action.id, action.content_hash)
    await h.drain()
    assert h.posts() == [] and await h.state() == "needs_you"
    task = await h.tasks.get(TASK)
    assert task is not None and task.next_action is not None and "expired" in task.next_action
    assert (await h.actions.get(action.id)).content == "Hello, the login works again. (v1)"  # type: ignore[union-attr]


async def test_approve_after_24h_without_the_sweep_is_refused(h: Harness) -> None:
    action = await proposed(h)
    h.clock.advance(timedelta(hours=25))
    with pytest.raises(ApprovalConflict, match="expired"):
        await h.approvals.approve(action.id, action.content_hash)
    assert (await h.actions.get(action.id)).status == "expired"  # type: ignore[union-attr]


async def test_task_closed_while_approval_pending(h: Harness) -> None:
    action = await proposed(h)
    await h.ledger.transition(TASK, "done", "user", "fixed by phone")
    with pytest.raises(ApprovalConflict, match="closed"):
        await h.approvals.approve(action.id, action.content_hash)
    await h.drain()
    assert h.posts() == [] and await h.state() == "done"


async def test_task_closed_after_approval_sends_nothing(h: Harness) -> None:
    action = await proposed(h)
    await h.approvals.approve(action.id, action.content_hash)
    await h.ledger.transition(TASK, "done", "user")
    await h.drain()
    assert h.posts() == []
    assert (await h.actions.get(action.id)).status == "invalidated"  # type: ignore[union-attr]


async def test_approved_during_pause_runs_after_resume(h: Harness) -> None:
    action = await proposed(h)
    h.controls.is_paused = True
    await h.approvals.approve(action.id, action.content_hash)  # stored
    assert await h.drain() == [("resume", "parked")]
    assert h.posts() == [] and (await h.actions.get(action.id)).status == "held"  # type: ignore[union-attr]
    assert await h.runner.waiting_on(TASK) == {"kind": "held", "action_id": action.id}

    h.controls.is_paused = False
    assert await h.approvals.release_held() == [action.id]
    assert h.slack.notes["slack:1"] == "approved while paused; sent after resume"
    await h.drain()
    assert len(h.posts()) == 1


async def test_proposed_while_paused_is_shown_after_resume(h: Harness) -> None:
    h.controls.is_paused = True
    await h.caller_mail("M1")
    assert await h.drain() == [("new_event", "parked")]
    action = await h.only_action()
    assert action.status == "held" and h.slack.open_cards() == []  # no card while paused
    h.controls.is_paused = False
    await h.approvals.release_held()
    assert len(h.slack.open_cards()) == 1
    await h.approvals.approve(action.id, action.content_hash)
    await h.drain()
    assert len(h.posts()) == 1


async def test_invalidated_while_held(h: Harness) -> None:
    action = await proposed(h)
    h.controls.is_paused = True
    await h.approvals.approve(action.id, action.content_hash)
    await h.drain()
    await h.caller_mail("M2", subject="RE: update")  # held send is now stale
    assert (await h.actions.get(action.id)).status == "invalidated"  # type: ignore[union-attr]
    h.controls.is_paused = False
    await h.drain()
    assert h.posts() == []


# ---------------------------------------------------------------- checks before the card


async def test_leave_mode_suppresses_caller_drafts(h: Harness) -> None:
    h.controls.purposes = {"caller_draft", "chase"}
    await h.caller_mail("M1")
    assert await h.drain() == [("new_event", "done")]
    assert h.slack.cards == {} and await h.actions.open_for_task(TASK, ("proposed",)) == []


async def test_forbidden_wording_never_reaches_a_card(h: Harness) -> None:
    h.step.text = "Your KPI ticket is fixed within 24 hours."
    await h.caller_mail("M1")
    await h.drain()
    assert h.slack.cards == {} and await h.state() == "needs_you"


async def test_outside_recipient_is_handed_off(h: Harness) -> None:
    h.step.extra["to"] = [CALLER, "someone@partner.example"]
    await h.caller_mail("M1")
    await h.drain()
    assert h.slack.cards == {} and await h.state() == "needs_you"
    task = await h.tasks.get(TASK)
    assert task is not None and task.next_action is not None and "Hand-off" in task.next_action


async def test_card_post_failure_on_one_channel(h: Harness) -> None:
    h.slack.fail_posts = 1
    action = await proposed(h)
    assert h.slack.cards == {} and len(h.console.open_cards()) == 1
    assert (await h.actions.get(action.id)).surfaces == {"console": "console:1"}  # type: ignore[union-attr]


async def test_step_limit(h: Harness) -> None:
    h.step.endless = True
    await h.caller_mail("M1")
    await h.drain()
    task = await h.tasks.get(TASK)
    assert task is not None and task.state == "needs_you" and "4 steps" in (task.next_action or "")


async def test_taskless_job_without_a_handler_is_done(h: Harness) -> None:
    await h.jobs.enqueue("schedule", task_id=None, payload={"schedule": "unknown"})
    assert await h.drain() == [("schedule", "done")]


async def test_expiry_sweep_runs_from_its_schedule(h: Harness) -> None:
    action = await proposed(h)
    h.clock.advance(timedelta(hours=25))
    sweep = {"kind": "schedule", "external_id": "expire_approvals@1", "received_at": h.clock.now_utc()}
    await h.coordinator.ingest(sweep | {"payload": {"schedule": "expire_approvals"}}, "schedule")
    assert await h.drain() == [("schedule", "done"), ("resume", "done")]
    assert (await h.actions.get(action.id)).status == "expired"  # type: ignore[union-attr]
    assert await h.state() == "needs_you" and await h.runner.waiting_on(TASK) is None


# ---------------------------------------------------------------- less common paths


async def test_new_event_settles_a_run_whose_resume_job_was_lost(h: Harness) -> None:
    first = await proposed(h)
    await h.caller_mail("M2", subject="RE: any news?")
    for job in await h.jobs.for_task(TASK):
        if job.kind == "resume":
            await h.jobs.complete(job.id)  # lost: never ran
    assert await h.drain() == [("new_event", "parked")]  # closed the old run itself, then drafted again
    assert (await h.only_action()).id != first.id
    assert len(h.slack.open_cards()) == 1


async def test_held_report_work_runs_after_resume(h: Harness) -> None:
    h.controls.is_paused = True

    async def note_step(state: Any) -> Any:
        from agent.graphs.task_graph import Proposal, StepResult

        if state.get("steps", 0) > 0:
            return StepResult()
        return StepResult(
            proposals=[Proposal(tool="update_ledger", args={"text": "triaged"}, purpose="note")]
        )

    h.deps.step = note_step
    await h.caller_mail("M1")
    assert await h.drain() == [("new_event", "parked")]
    assert await h.runner.waiting_on(TASK) == {"kind": "held", "action_id": (await h.only_action()).id}
    h.controls.is_paused = False
    assert len(await h.approvals.release_held()) == 1
    await h.drain()
    assert (await h.only_action()).status == "done" and await h.runner.waiting_on(TASK) is None


async def test_reject_twice_and_reject_after_approve(h: Harness) -> None:
    action = await proposed(h)
    await h.approvals.reject(action.id, "no")
    assert not (await h.approvals.reject(action.id, "no")).changed
    await h.drain()
    await h.caller_mail("M2", subject="RE: hello?")
    await h.drain()
    second = await h.only_action()
    await h.approvals.approve(second.id, second.content_hash)
    with pytest.raises(ApprovalConflict, match="approved"):
        await h.approvals.reject(second.id, "too late")


async def test_card_update_failure_does_not_block_the_decision(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = await proposed(h)

    async def broken(*args: Any) -> None:
        raise ConnectionError("slack down")

    monkeypatch.setattr(h.slack, "update_card", broken)
    assert (await h.approvals.approve(action.id, action.content_hash)).changed
    await h.drain()
    assert len(h.posts()) == 1


async def test_send_mail_is_not_editable_in_place(h: Harness) -> None:
    from agent.policy.gate import GateContext

    ctx = GateContext(task_id=TASK, participants=frozenset({CALLER}))
    await h.caller_mail("M0")
    result = await h.approvals.create(
        TASK, "send_mail", {"draft_id": "D1", "to": [CALLER]}, "Draft text", ctx
    )
    assert result.status == "proposed"
    with pytest.raises(ApprovalConflict, match="cannot be edited"):
        await h.approvals.edit(result.action_id, "New text")
    again = await h.approvals.create(TASK, "send_mail", {"draft_id": "D1", "to": [CALLER]}, "Draft text", ctx)
    assert again.action_id == result.action_id and len(h.slack.cards) == 1  # same proposal: one card
    assert [a.id for a in await h.approvals.pending()] == [result.action_id]


async def test_forget_drops_the_thread(h: Harness) -> None:
    await proposed(h)
    await h.runner.forget(TASK)
    assert await h.runner.waiting_on(TASK) is None
