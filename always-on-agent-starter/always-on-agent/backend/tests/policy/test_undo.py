from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.policy.gate import GateContext
from tests.policy.test_outbox import MSG, Env


async def test_undo_move_restores_folder_once(engine: AsyncEngine, clock: FrozenClock) -> None:
    env = Env(engine, clock)
    res = await env.outbox.submit(
        "move_mail", {"message_id": MSG, "folder": "Done"}, GateContext(task_id=None)
    )
    assert res.audit_id is not None
    first = await env.undo.undo(res.audit_id)
    assert first.status == "done" and env.world.mail.folders[MSG] == "Inbox"
    second = await env.undo.undo(res.audit_id)
    assert second.status == "already_undone"
    assert [w.args["folder"] for w in env.world.mail.writes_to("move")] == ["Done", "Inbox"]
    results = [r["result"] for r in await env.audit.recent()]
    assert "undo: done" in results
    # The inverse ran through the gate and outbox and registered no undo of its own.
    inverse_rows = [r for r in await env.audit.recent() if r["result"] == "done" and r["id"] != res.audit_id]
    assert inverse_rows
    for row in inverse_rows:
        assert await env.undo_repo.for_audit(row["id"]) is None


async def test_undo_refuses_when_item_changed(engine: AsyncEngine, clock: FrozenClock) -> None:
    env = Env(engine, clock)
    res = await env.outbox.submit("move_mail", {"message_id": MSG, "folder": "Done"}, GateContext())
    assert res.audit_id is not None
    env.world.mail.folders[MSG] = "Archive"  # the user moved it by hand
    out = await env.undo.undo(res.audit_id)
    assert out.status == "refused" and "Archive" in out.message
    assert env.world.mail.folders[MSG] == "Archive"
    op = await env.undo_repo.for_audit(res.audit_id)
    assert op is not None and op["status"] == "available"  # can try again once it is put back
    env.world.mail.folders[MSG] = "Done"
    assert (await env.undo.undo(res.audit_id)).status == "done"


async def test_undo_save_draft_discards_own_draft(engine: AsyncEngine, clock: FrozenClock) -> None:
    env = Env(engine, clock)
    res = await env.outbox.submit(
        "save_draft", {"to": ["sophie.laurent@corp.example"], "subject": "s", "body": "b"}, GateContext()
    )
    assert res.audit_id is not None and res.result in env.world.mail.drafts
    assert (await env.undo.undo(res.audit_id)).status == "done"
    assert res.result not in env.world.mail.drafts
    # Second time: already undone, not "draft is gone".
    assert (await env.undo.undo(res.audit_id)).status == "already_undone"


async def test_not_undoable(engine: AsyncEngine, clock: FrozenClock) -> None:
    env = Env(engine, clock)
    res = await env.outbox.submit("update_ledger", {"text": "no inverse"}, GateContext())
    assert res.audit_id is not None
    assert (await env.undo.undo(res.audit_id)).status == "not_undoable"
    assert (await env.undo.undo(999_999)).status == "not_undoable"


async def test_undo_while_paused_is_refused_and_retryable(engine: AsyncEngine, clock: FrozenClock) -> None:
    env = Env(engine, clock)
    res = await env.outbox.submit("move_mail", {"message_id": MSG, "folder": "Done"}, GateContext())
    assert res.audit_id is not None

    real_submit = env.outbox.submit

    async def paused_submit(tool, args, ctx, **kw):  # type: ignore[no-untyped-def]
        return await real_submit(tool, args, ctx.model_copy(update={"paused": True}), **kw)

    env.outbox.submit = paused_submit  # type: ignore[method-assign]
    out = await env.undo.undo(res.audit_id)
    assert out.status == "refused" and "paused" in out.message
    assert env.world.mail.folders[MSG] == "Done"
    env.outbox.submit = real_submit  # type: ignore[method-assign]
    assert (await env.undo.undo(res.audit_id)).status == "done"  # after resume
    assert env.world.mail.folders[MSG] == "Inbox"


async def test_undo_in_progress_is_not_run_twice(engine: AsyncEngine, clock: FrozenClock) -> None:
    env = Env(engine, clock)
    res = await env.outbox.submit("move_mail", {"message_id": MSG, "folder": "Done"}, GateContext())
    assert res.audit_id is not None
    op = await env.undo_repo.for_audit(res.audit_id)
    assert op is not None and await env.undo_repo.mark(op["id"], "available", "running")
    out = await env.undo.undo(res.audit_id)
    assert out.status == "refused" and "already running" in out.message
