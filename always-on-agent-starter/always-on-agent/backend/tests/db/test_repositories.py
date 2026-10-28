import asyncio
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.core.errors import ApprovalConflict
from agent.db.repositories import (
    ActionRepo,
    AuditRepo,
    EventRepo,
    JobRepo,
    KvRepo,
    MemoryRepo,
    Task,
    TaskRepo,
    TokenCipher,
    TokenMapRepo,
    UndoRepo,
)
from agent.db.search import Fts5SearchIndex


async def _task(engine: AsyncEngine, clock: FrozenClock, task_id: str) -> None:
    await TaskRepo(engine, clock).create(
        Task(id=task_id, project="doxis", kind="incident", title="t", state="open")
    )


async def test_pragmas(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1
        assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == 5000


async def test_task_roundtrip_keeps_utc(engine: AsyncEngine, clock: FrozenClock) -> None:
    await _task(engine, clock, "INC30114872")
    task = await TaskRepo(engine, clock).get("INC30114872")
    assert task is not None and task.created_at == clock.now_utc()


async def test_event_dedupe(engine: AsyncEngine, clock: FrozenClock) -> None:
    repo = EventRepo(engine, clock)
    first = await repo.add("mail", "message", "AAMk-1", {"subject": "x"})
    again = await repo.add("mail", "message", "AAMk-1", {"subject": "x"})
    other_source = await repo.add("teams", "message", "AAMk-1", {})
    assert first is not None and again is None and other_source is not None
    assert len(await repo.pending()) == 2


async def test_concurrent_claim_never_double_claims_a_task(engine: AsyncEngine, clock: FrozenClock) -> None:
    for task_id in ("INC30000001", "INC30000002"):
        await _task(engine, clock, task_id)
    jobs = JobRepo(engine, clock)
    for i in range(3):
        await jobs.enqueue("step", "INC30000001", priority=i)
        await jobs.enqueue("step", "INC30000002", priority=i)

    claimed = await asyncio.gather(*(JobRepo(engine, clock).claim(f"w{i}") for i in range(5)))
    got = [j for j in claimed if j is not None]
    assert sorted(j.task_id or "" for j in got) == ["INC30000001", "INC30000002"]
    assert all(j.priority == 2 for j in got)


async def test_claim_respects_priority_availability_and_ttl(engine: AsyncEngine, clock: FrozenClock) -> None:
    await _task(engine, clock, "INC30000003")
    jobs = JobRepo(engine, clock, lock_ttl_minutes=10)
    await jobs.enqueue("later", priority=9, available_at=clock.now_utc() + timedelta(hours=1))
    low = await jobs.enqueue("low", priority=1)
    high = await jobs.enqueue("high", "INC30000003", priority=5)
    await jobs.enqueue("second", "INC30000003", priority=4)

    a = await jobs.claim("w1")
    assert a is not None and a.id == high and a.locked_by == "w1" and a.attempts == 1
    b = await jobs.claim("w2")
    assert b is not None and b.id == low  # the task's second job waits
    assert await jobs.claim("w3") is None

    clock.advance(timedelta(minutes=11))  # every lock is now expired
    c = await jobs.claim("w3")
    assert c is not None and c.id == high and c.locked_by == "w3" and c.attempts == 2


async def test_job_complete_unblocks_task(engine: AsyncEngine, clock: FrozenClock) -> None:
    await _task(engine, clock, "INC30000004")
    jobs = JobRepo(engine, clock)
    first = await jobs.enqueue("a", "INC30000004", priority=2)
    second = await jobs.enqueue("b", "INC30000004", priority=1)
    claimed = await jobs.claim("w1")
    assert claimed is not None and claimed.id == first
    assert await jobs.claim("w2") is None
    await jobs.complete(first)
    nxt = await jobs.claim("w2")
    assert nxt is not None and nxt.id == second


async def test_action_compare_and_set(engine: AsyncEngine, clock: FrozenClock) -> None:
    repo = ActionRepo(engine, clock)
    await repo.create("A1", None, "send_mail", {"to": ["x"]}, "ask", "pending", "hash-1", "idem-1")

    with pytest.raises(ApprovalConflict):
        await repo.transition("A1", "pending", "approved", expected_hash="hash-2")
    with pytest.raises(ApprovalConflict):
        await repo.transition("A1", "approved", "executing")

    await repo.transition("A1", "pending", "approved", expected_hash="hash-1")
    action = await repo.get("A1")
    assert action is not None and action.status == "approved" and action.decided_at == clock.now_utc()

    with pytest.raises(ApprovalConflict):  # a second approver loses
        await repo.transition("A1", "pending", "approved", expected_hash="hash-1")


async def test_action_idempotency_key_unique(engine: AsyncEngine, clock: FrozenClock) -> None:
    repo = ActionRepo(engine, clock)
    await repo.create("A1", None, "flag_mail", {}, "report", "queued", "h", "idem")
    with pytest.raises(IntegrityError):
        await repo.create("A2", None, "flag_mail", {}, "report", "queued", "h", "idem")
    found = await repo.by_idempotency_key("idem")
    assert found is not None and found.id == "A1"


async def test_audit_and_undo(engine: AsyncEngine, clock: FrozenClock) -> None:
    audit_id = await AuditRepo(engine, clock).append("move_mail", "report", "d1", "ok", "agent")
    undo = UndoRepo(engine, clock)
    undo_id = await undo.add(audit_id, "move_mail", {"folder": "Inbox"})
    assert await undo.mark(undo_id, "available", "done")
    assert not await undo.mark(undo_id, "available", "done")


async def test_token_map_roundtrip(engine: AsyncEngine, clock: FrozenClock) -> None:
    repo = TokenMapRepo(engine, clock, TokenCipher(Fernet.generate_key()))
    assert await repo.put("PERSON_1", "person", "Anna Keller") == "PERSON_1"
    assert await repo.put("PERSON_9", "person", "Anna Keller") == "PERSON_1"
    assert await repo.real_value("PERSON_1") == "Anna Keller"
    assert await repo.token_for("person", "Anna Keller") == "PERSON_1"
    assert await repo.real_value("PERSON_404") is None

    async with engine.connect() as conn:
        blob = (await conn.execute(text("SELECT real_value_encrypted FROM token_map"))).scalar_one()
    assert b"Anna" not in blob


async def test_memory_summary_versions_and_links(engine: AsyncEngine, clock: FrozenClock) -> None:
    await _task(engine, clock, "INC30000005")
    mem = MemoryRepo(engine, clock)
    assert await mem.put_summary("INC30000005", "v1") == 1
    assert await mem.put_summary("INC30000005", "v2", expected_version=1) == 2
    with pytest.raises(ApprovalConflict):
        await mem.put_summary("INC30000005", "stale", expected_version=1)
    await mem.set_fact("INC30000005", "site", "SITE_A")
    await mem.set_fact("INC30000005", "site", "SITE_B")
    assert [f["value"] for f in await mem.facts("INC30000005")] == ["SITE_B"]
    await mem.link("INC30000005", "mail_conversation", "conv-1")
    assert await mem.task_for_link("mail_conversation", "conv-1") == "INC30000005"
    await mem.add_task_event("INC30000005", "PERSON_1", "mail", "Hello")
    assert len(await mem.task_events("INC30000005")) == 1


async def test_kv(engine: AsyncEngine, clock: FrozenClock) -> None:
    kv = KvRepo(engine, clock)
    await kv.set("delta:mail", "a")
    await kv.set("delta:mail", "b")
    assert await kv.get("delta:mail") == "b"


async def test_fts_search_ranked(engine: AsyncEngine) -> None:
    index = Fts5SearchIndex(engine)
    await index.upsert("kb1", "kb", "Printer setup", "How to add a network printer at the depot")
    await index.upsert("kb2", "kb", "Archive access", "Grant archive access through the rights matrix.")
    await index.upsert("kb3", "spec", "Sales report", "Monthly archive of sales")

    assert [h.id for h in await index.search("archive access")] == ["kb2"]
    hits = await index.search("archive")
    assert [h.id for h in hits] == ["kb2", "kb3"]  # title match weighs more
    assert hits[0].score > hits[1].score
    assert [h.id for h in await index.search("archive", source="spec")] == ["kb3"]

    await index.upsert("kb2", "kb", "Archive access", "replaced text")
    assert await index.search("rights") == []
    await index.remove("kb3")
    assert await index.search("sales") == []
    assert await index.search('"; DROP TABLE documents; --') == []
