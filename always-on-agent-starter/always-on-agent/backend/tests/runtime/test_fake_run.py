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
        assert "inbox-triage would have acted" in digest  # triage ran, in shadow
        rows = await runtime.audit.recent(500)
        triage = {
            r["tool"]
            for r in rows
            if r["recipe"] == "inbox-triage" and r["result"].startswith("decision:shadow")
        }
        assert {"move_mail", "flag_mail"} <= triage  # newsletter moved, manager mail flagged
        assert not any(r["tool"] in ("move_mail", "flag_mail") and r["result"] == "done" for r in rows)
        assert {t.id for t in await runtime.tasks.list()} >= {"INC30114872", "ENH-014", "REF-0412"}
    finally:
        await runtime.stop()


def test_cli_fake_demo(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["run", "--fake", "--demo-only", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Morning brief" in result.output and "Today's digest" in result.output
