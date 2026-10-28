import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.core.config import RetentionConfig
from agent.core.errors import LeakDetected, ModelUnavailable
from agent.db.repositories import ContextLogRepo, RetentionRepo
from agent.memory.knowledge import keywords, shareable
from agent.memory.personal import PersonalMemory, clean_markdown
from agent.memory.retention import RetentionJob
from agent.memory.summary import CHECK_FACT, SummaryOut, mentioned_dates
from agent.memory.task_memory import detect_language, headline_of
from agent.model.fake import RecordedModel
from tests.memory.helpers import TASK, Stack


@pytest.fixture
async def stack(engine: AsyncEngine, clock: FrozenClock, tmp_path: Path) -> Stack:
    s = Stack(engine, clock, tmp_path)
    await s.task()
    return s


def ok(text: str, state: str = "new") -> dict[str, str]:
    return {"summary": text, "current_state": state}


# ---------------------------------------------------------------- summary


async def test_summary_written_with_coverage(stack: Stack) -> None:
    ids = await stack.events_n(3, words=5)
    stack.model.add("summary", ok("CALLER reported a login failure at SITE_1 for INC30114872 on 2026-03-25."))
    result = await stack.summaries.update(TASK)
    assert result.status == "updated" and result.version == 1
    view = await stack.memory.summary(TASK)
    assert view.covers_through == ids[-1] and view.verified
    assert (await stack.summaries.update(TASK)).status == "unchanged"  # nothing new: no model call
    assert len(stack.model.calls) == 1
    call = stack.model.calls[0]
    assert (
        call["purpose"] == "summary"
        and call["tier"] == "haiku"
        and call["data_classes"] == {"metadata", "text"}
    )


async def test_summary_mismatch_triggers_one_rewrite_with_the_problems(stack: Stack) -> None:
    await stack.events_n(2, words=5)
    stack.model.add(
        "summary",
        ok("INC30999999 for USER_9 was closed on 2026-04-30. The ticket is closed.", state="done"),
        ok("CALLER cannot log in; waiting for the logs."),
    )
    result = await stack.summaries.update(TASK)
    assert result.status == "updated"
    retry = stack.model.calls[1]["instructions"]
    for problem in ("INC30999999", "30.04", "USER_9", "current_state is 'done'", "says the task is closed"):
        assert problem in retry
    assert (await stack.memory.summary(TASK)).text == "CALLER cannot log in; waiting for the logs."


async def test_second_mismatch_keeps_the_previous_summary(stack: Stack) -> None:
    await stack.repo.put_summary(TASK, "Old good summary.", covers_through=None, verified=True)
    await stack.events_n(2, words=5)
    bad = ok("Fixed on 2026-05-01.")
    stack.model.add("summary", bad, bad)
    result = await stack.summaries.update(TASK)
    assert result.status == "kept_previous" and result.problems
    view = await stack.memory.summary(TASK)
    assert view.text == "Old good summary." and view.covers_through is None  # not advanced: retried next run
    facts = {f["key"]: f for f in await stack.repo.facts(TASK)}
    assert facts[CHECK_FACT]["status"] == "failed"
    card = await stack.memory.card(TASK)
    assert CHECK_FACT not in card.facts  # internal: never shown to the model


async def test_too_long_summary_is_rejected(stack: Stack) -> None:
    await stack.events_n(1, words=5)
    stack.model.add("summary", ok("word " * 130), ok("Short."))
    assert (await stack.summaries.update(TASK)).status == "updated"
    assert "130 words" in stack.model.calls[1]["instructions"]


async def test_model_outage_or_leak_skips_without_failing(stack: Stack) -> None:
    await stack.events_n(1, words=5)
    stack.model.add("summary", ModelUnavailable("down"), LeakDetected("1 known name"))
    assert (await stack.summaries.update(TASK)).reason == "ModelUnavailable"
    assert (await stack.summaries.update(TASK)).reason == "LeakDetected"
    assert (await stack.memory.summary(TASK)).text is None


async def test_metadata_level_summarises_from_notes_only(
    engine: AsyncEngine, clock: FrozenClock, tmp_path: Path
) -> None:
    stack = Stack(engine, clock, tmp_path, level="metadata")
    await stack.task()
    await stack.events_n(1, words=5)
    await stack.ledger.transition(TASK, "in_progress", "worker", "picked up")
    stack.model.add("summary", ok("Work started.", state="in_progress"))
    await stack.summaries.update(TASK)
    call = stack.model.calls[0]
    assert call["data_classes"] == {"metadata"}
    content = call["messages"][0]["content"]
    assert "word0" not in content and "text left out" in content and "Login fails" not in content


def test_dates_are_read_in_several_formats() -> None:
    assert mentioned_dates("on 2026-03-25, 26.03.2026, 27 Mar and April 2nd") == {
        (3, 25),
        (3, 26),
        (3, 27),
        (4, 2),
    }


async def test_concurrent_writer_wins(stack: Stack) -> None:
    await stack.events_n(1, words=5)

    class Racing(RecordedModel):
        async def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            await stack.repo.put_summary(TASK, "someone else", verified=True)
            return await super().run(*args, **kwargs)

    stack.summaries._model = Racing({"summary": [ok("Mine.")]})
    result = await stack.summaries.update(TASK)
    assert result.status == "skipped" and (await stack.memory.summary(TASK)).text == "someone else"


# ---------------------------------------------------------------- task memory


async def test_trigger_recorded_once_and_purged_payloads_ignored(stack: Stack) -> None:
    event_id = await stack.events.add(
        "mail",
        "mail",
        "m1",
        {
            "subject": "Login",
            "body": "Bitte helfen Sie mir, die Anmeldung geht nicht und ich kann nicht arbeiten",
            "sender": "CALLER",
        },
    )
    trigger = {"event_id": event_id, "source": "mail"}
    first = await stack.memory.record_trigger(TASK, trigger)
    assert first is not None and await stack.memory.record_trigger(TASK, trigger) is None
    row = (await stack.repo.task_events(TASK))[-1]
    assert row["kind"] == "message" and row["lang"] == "de" and row["source_ref"] == f"event:{event_id}"
    assert row["raw_expires_at"] == row["at"] + timedelta(days=14)
    assert row["importance"] >= 2
    assert await stack.memory.record_trigger(TASK, {"source": "schedule", "event_id": event_id}) is None


async def test_facts_need_a_source_and_changes_are_logged(stack: Stack) -> None:
    with pytest.raises(ValueError, match="source_ref"):
        await stack.memory.set_fact(TASK, "site", "SITE_1", source_ref=" ")
    assert await stack.memory.set_fact(TASK, "site", "SITE_1", source_ref="event:1")
    assert not await stack.memory.set_fact(TASK, "site", "SITE_1", source_ref="event:1")
    assert await stack.memory.set_fact(TASK, "site", "SITE_2", source_ref="event:2")
    log = [e["text_pseudonymised"] for e in await stack.repo.task_events(TASK) if e["kind"] == "fact"]
    assert log == ["fact site: SITE_1 -> SITE_2 (event:2)"]
    card = await stack.memory.card(TASK)
    assert card.facts["site"].value == "SITE_2" and card.facts["site"].source_ref == "event:2"


def test_language_and_headline() -> None:
    assert (
        detect_language("Bonjour, je ne peux pas me connecter avec mon compte, merci pour votre aide") == "fr"
    )
    assert detect_language("ok thanks") is None
    assert headline_of("\n> quoted\nFirst real line   with  spaces\nmore") == "First real line with spaces"


# ---------------------------------------------------------------- knowledge


async def test_search_ranks_the_matching_precedent_first(stack: Stack) -> None:
    for task_id, title, site in [
        ("INC30000001", "Archive export fails with timeout", "SITE_9"),
        ("INC30000002", "Login fails at SITE_1 after password change", "SITE_1"),
        ("INC30000003", "Printer offline", "SITE_1"),
    ]:
        await stack.task(task_id, title=title, site=site)
        await stack.repo.put_summary(task_id, f"Resolved: {title}. CALLER confirmed with USER_2.")
        await stack.ledger.transition(task_id, "in_progress", "worker")
        await stack.ledger.transition(task_id, "done", "user")  # the listener writes the precedent
    await stack.tasks.update(TASK, site="SITE_1")
    card = await stack.memory.card(TASK)
    hits = await stack.knowledge.precedents(card, k=3)
    assert hits[0].ref == "doc:precedent:INC30000002"
    assert "CALLER" not in hits[0].snippet and "USER_2" not in hits[0].snippet  # shared as role words
    plain = await stack.knowledge.search("archive export", kind="precedent")
    assert plain[0].ref == "doc:precedent:INC30000001"


async def test_precedents_exclude_own_task_dedupe_and_cap(
    engine: AsyncEngine, clock: FrozenClock, tmp_path: Path
) -> None:
    stack = Stack(engine, clock, tmp_path)
    stack.knowledge._cap = 2
    for n in range(4):
        task_id = f"INC3000001{n}"
        await stack.task(task_id, title="Login fails")
        await stack.ledger.transition(task_id, "in_progress", "worker")
        await stack.ledger.transition(task_id, "done", "user")
        clock.advance(timedelta(minutes=1))
    stored = await stack.index.by_source("precedent")
    assert sorted(r["id"] for r in stored) == [
        "precedent:INC30000012",
        "precedent:INC30000013",
    ]  # oldest went
    card = await stack.memory.card("INC30000013")
    hits = await stack.knowledge.precedents(card, k=5)
    assert [h.ref for h in hits] == ["doc:precedent:INC30000012"]  # not itself; one per title


def test_shareable_and_keywords() -> None:
    assert (
        shareable("CALLER asked KM_1 and USER_3, call PHONE_1")
        == "the caller asked the KM and a user, call [phone]"
    )
    assert keywords("RE: Login fails at SITE_1 for CALLER", "incident") == "login fails site_1"


async def test_directory_staleness_and_lesson_conflicts(stack: Stack, clock: FrozenClock) -> None:
    await stack.directory.upsert("km.old@corp.example", "Karin Moser", role="KM", site="SITE_1")
    clock.advance(timedelta(days=91))
    await stack.directory.upsert("lba.new@corp.example", "Lukas Bauer", role="LBA", site="SITE_1")
    entries = {e.email: e.stale for e in await stack.knowledge.directory_for("SITE_1")}
    assert entries == {"km.old@corp.example": True, "lba.new@corp.example": False}
    await stack.lessons.add("L1", "global", "Greeting: use Dear")
    await stack.lessons.set_status("L1", "active")
    clock.advance(timedelta(minutes=5))
    await stack.lessons.add("L2", "doxis", "Greeting: use Hi")
    await stack.lessons.set_status("L2", "active")
    lessons = await stack.knowledge.lessons(["global", "doxis"])
    assert [row["id"] for row in lessons] == ["L2"]  # the newest on the same subject wins
    await stack.tasks.update(TASK, site="SITE_1")
    packet = await stack.builder.build(TASK, ["directory"])
    assert "stale: last confirmed" in packet.section("directory").text()  # type: ignore[union-attr]


# ---------------------------------------------------------------- personal


def test_personal_created_from_templates_and_reloaded(tmp_path: Path) -> None:
    personal = PersonalMemory(tmp_path / "memory")
    assert personal.load().text == ""  # untouched templates cost nothing
    assert sorted(p.name for p in (tmp_path / "memory").iterdir()) == ["preferences.md", "profile.md"]
    profile = tmp_path / "memory" / "profile.md"
    profile.write_text("# Profile\n- Role: L2 support\n<!-- a note -->", encoding="utf-8")
    assert personal.load().text == "# Profile\n- Role: L2 support"
    profile.write_text("# Profile\n- Role: Power BI owner\n" + "- detail line\n" * 300, encoding="utf-8")
    stat = profile.stat()
    os.utime(profile, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    loaded = personal.load(budget=50)
    assert "Power BI owner" in loaded.text and loaded.trimmed and loaded.tokens <= 50
    (tmp_path / "memory" / "preferences.md").unlink()
    personal.load()
    assert (tmp_path / "memory" / "preferences.md").exists()  # recreated


def test_clean_markdown_drops_empty_headings() -> None:
    assert clean_markdown("# A\n\n# B\n- x\n<!-- y -->\n\n\n\n# C") == "# B\n- x"


# ---------------------------------------------------------------- retention


async def test_retention_deletes_only_expired_rows(
    stack: Stack, engine: AsyncEngine, clock: FrozenClock, tmp_path: Path
) -> None:
    now = clock.now_utc()
    old = await stack.events.add(
        "mail",
        "mail",
        "old",
        {"subject": "S", "body": "secret body", "sender": "x@corp.example"},
        received_at=now - timedelta(days=15),
    )
    fresh = await stack.events.add(
        "mail",
        "mail",
        "new",
        {"subject": "S2", "body": "keep me", "sender": "y@corp.example"},
        received_at=now - timedelta(days=2),
    )
    short = await stack.events.add(
        "teams",
        "message",
        "short",
        {"text": "chat text", "chat_id": "c1"},
        received_at=now - timedelta(days=1),
    )
    await stack.repo.add_task_event(
        TASK,
        "CALLER",
        "teams",
        "pseudonymised",
        source_ref=f"event:{short}",
        raw_expires_at=now - timedelta(hours=1),
    )
    await stack.repo.add_task_event(
        TASK,
        "CALLER",
        "mail",
        "pseudonymised too",
        source_ref=f"event:{fresh}",
        raw_expires_at=now + timedelta(days=12),
    )
    screens = tmp_path / "screens"
    screens.mkdir()
    for name, age in [("old.png", 8), ("new.png", 1), ("notes.txt", 30)]:
        path = screens / name
        path.write_bytes(b"x")
        stamp = (now - timedelta(days=age)).timestamp()
        os.utime(path, (stamp, stamp))
    logs = ContextLogRepo(engine, clock)
    await logs.add(TASK, None, 10, {})

    job = RetentionJob(
        RetentionConfig(raw_text_days=14, screenshots_days=7, transcripts="delete_after_extract"),
        clock,
        RetentionRepo(engine, clock),
        logs,
        screens,
    )
    report = await job.run()
    assert (report.event_payloads, report.task_event_sources, report.screenshots, report.context_logs) == (
        1,
        1,
        1,
        0,
    )

    async def body(eid: int | None) -> dict[str, object]:
        row = await stack.events.get(int(eid or 0))
        assert row is not None
        return dict(json.loads(row["payload_json"]))

    assert await body(old) == {"sender": "x@corp.example", "_purged": True}
    assert (await body(fresh))["body"] == "keep me"
    assert "text" not in await body(short) and (await body(short))["chat_id"] == "c1"
    assert sorted(p.name for p in screens.iterdir()) == ["new.png", "notes.txt"]
    texts = [e["text_pseudonymised"] for e in await stack.repo.task_events(TASK)]
    assert texts == ["pseudonymised", "pseudonymised too"]  # memory itself is kept
    again = await job.run()
    assert (again.event_payloads, again.task_event_sources, again.screenshots) == (0, 0, 0)  # idempotent


async def test_old_context_logs_are_purged(engine: AsyncEngine, clock: FrozenClock) -> None:
    logs = ContextLogRepo(engine, clock)
    await logs.add(TASK, None, 1, {})
    clock.advance(timedelta(days=31))
    job = RetentionJob(
        RetentionConfig(raw_text_days=14, screenshots_days=7, transcripts="keep"),
        clock,
        RetentionRepo(engine, clock),
        logs,
    )
    assert (await job.run()).context_logs == 1


def test_summary_schema() -> None:
    assert SummaryOut(summary="x", current_state="new").current_state == "new"
    assert datetime(2026, 1, 1, tzinfo=UTC)
