"""agentd run --fake: the fake day produces a brief, triage actions and a nightly digest (prompt 1.14)."""

from pathlib import Path

from typer.testing import CliRunner

from agent.cli import app
from agent.core.config import CONFIG_DIR, load_config
from agent.runtime.fake import build_fake_runtime, run_demo


async def test_fake_day_reaches_the_fake_slack_dm(tmp_path: Path) -> None:
    runtime, world = await build_fake_runtime(load_config(CONFIG_DIR / "config.example.yaml"), tmp_path)
    try:
        log = await run_demo(runtime, world)
        texts = [str(m["text"]) for m in log]
        assert texts[0].startswith("Morning brief")
        assert any("unanswered in Teams" in t for t in texts)
        digest = next(t for t in texts if t.startswith("Today's digest"))
        assert "Moved a mail to Newsletters (undo " in digest  # triage is Live (prompt 1.18)
        rows = await runtime.audit.recent(500)
        triage = {r["tool"] for r in rows if r["recipe"] == "inbox-triage" and r["result"] == "done"}
        assert {"move_mail", "flag_mail"} <= triage  # newsletter moved, manager mail flagged
        assert world.mail.folders["AAMkAGFAKE0013"] == "Newsletters"
        assert world.mail.folders["AAMkAGFAKE0011"] == "Inbox"
        assert not any(
            r["result"].startswith("decision:shadow") for r in rows if r["recipe"] == "inbox-triage"
        )
        assert {t.id for t in await runtime.tasks.list()} >= {"INC30114872", "ENH-014", "REF-0412"}
    finally:
        await runtime.stop()


def test_cli_fake_demo(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["run", "--fake", "--demo-only", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Morning brief" in result.output and "Today's digest" in result.output


async def test_live_recipes_draft_replies_that_need_approval(tmp_path: Path) -> None:
    """--live-recipes: recipes act on the fakes; replies are drafted and wait for two approvals."""
    runtime, world = await build_fake_runtime(
        load_config(CONFIG_DIR / "config.example.yaml"), tmp_path, live=True
    )
    try:
        await run_demo(runtime, world)
        assert all("status: live" in p.read_text() for p in (tmp_path / "recipes").glob("*.yaml"))
        pending = await runtime.approvals.pending()
        replies = [a for a in pending if a.task_id and a.task_id.startswith("RPL-")]
        assert replies and {a.tool for a in replies} == {"save_draft"}  # model text: asks first
        handed_off, sent = [], []
        for action in replies:
            await runtime.approvals.approve(action.id, action.content_hash)  # save the draft
            await runtime.drain()
            send = [a for a in await runtime.approvals.pending() if a.task_id == action.task_id]
            task = await runtime.tasks.get(str(action.task_id))
            assert task is not None
            if not send:
                # Reply-all would reach someone outside the company: never automated (golden rule 3).
                assert (
                    task.state == "needs_you"
                    and task.next_action == "Hand-off prepared: finish this step yourself."
                )
                handed_off.append(task.id)
                continue
            assert [a.tool for a in send] == ["send_mail"]
            before = len(world.mail.sent)
            await runtime.approvals.approve(send[0].id, send[0].content_hash)
            await runtime.drain()
            task = await runtime.tasks.get(str(action.task_id))
            assert task is not None and task.state == "waiting"
            assert len(world.mail.sent) == before + 1
            sent.append(task.id)
        assert handed_off and sent
    finally:
        await runtime.stop()
