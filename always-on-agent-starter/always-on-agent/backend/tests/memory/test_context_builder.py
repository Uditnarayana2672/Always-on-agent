from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.memory.context_builder import DEFAULT_BUDGETS, DEFAULT_NEEDS, ORDER, ContextBuilder, SearchMemory
from agent.memory.tokens import estimate_tokens, trim_to_tokens
from tests.memory.helpers import TASK, Stack

ALL = list(ORDER)


@pytest.fixture
async def stack(engine: AsyncEngine, clock: FrozenClock, tmp_path: Path) -> Stack:
    s = Stack(engine, clock, tmp_path)
    await s.task()
    return s


def test_trim_keeps_start_and_end() -> None:
    text = "Question: why does login fail? " + "filler " * 400 + "Thanks, CALLER"
    out, cut = trim_to_tokens(text, 50)
    assert cut and out.startswith("Question:") and out.endswith("CALLER") and estimate_tokens(out) <= 50


# ---------------------------------------------------------------- budgets


async def test_budgets_respected_for_a_task_with_200_events(stack: Stack) -> None:
    await stack.events_n(200)
    await stack.repo.put_summary(TASK, "CALLER cannot log in. " * 200, covers_through=195, verified=True)
    for i in range(30):
        await stack.repo.set_fact(TASK, f"fact{i:02d}", "value " * 20, source_ref="test")
    packet = await stack.builder.build(TASK, ALL)
    for section in packet.sections:
        assert section.tokens <= section.budget, section.name
    assert packet.tokens <= sum(DEFAULT_BUDGETS.values())
    last = packet.section("last_events")
    assert last is not None
    shown = [i.ref for i in last.items]
    assert shown[-1] == "te:200" and len(shown) <= 5  # the newest; never all 200
    older = packet.section("older_events")
    assert older is not None and older.note == f"{200 - len(shown)} older event(s) in total"
    card = packet.section("card")
    assert card is not None and card.items[0].text.startswith("id: INC30114872\nstate: new\npriority: normal")
    assert card.note and "left out" in card.note  # facts dropped, never id/state


async def test_unsummarised_events_come_before_the_last_five(stack: Stack) -> None:
    ids = await stack.events_n(9, words=5)
    await stack.repo.put_summary(TASK, "short", covers_through=ids[1], verified=True)  # 7 events behind
    packet = await stack.builder.build(TASK, ["last_events"])
    last = packet.section("last_events")
    assert last is not None and [i.ref for i in last.items] == [f"te:{i}" for i in ids[2:]]


async def test_one_huge_event_cannot_take_the_whole_budget(stack: Stack) -> None:
    await stack.events_n(3, words=10)
    big = await stack.memory.add_event(
        TASK, "x " * 30000, actor="CALLER", channel="mail", kind="message", pseudonymised=True
    )
    packet = await stack.builder.build(TASK, ["last_events"])
    last = packet.section("last_events")
    assert last is not None and f"te:{big}" in last.trimmed
    huge = next(i for i in last.items if i.ref == f"te:{big}")
    assert huge.tokens <= DEFAULT_BUDGETS["last_events"] // 3 + 5
    assert len(last.items) == 4  # the others still fit


async def test_trigger_event_is_always_in(stack: Stack) -> None:
    ids = await stack.events_n(12, words=200)  # each ~ 330 tokens: only a few fit
    packet = await stack.builder.build(TASK, ["last_events", "older_events"], trigger_ref=f"te:{ids[0]}")
    last = packet.section("last_events")
    assert last is not None and f"te:{ids[0]}" in [i.ref for i in last.items]
    assert last.omitted  # some newer ones did not fit ...
    older = packet.section("older_events")
    assert older is not None and any(last.omitted[0] in i.text for i in older.items)  # ... and are pointed to


# ---------------------------------------------------------------- data rule, translation, provenance


async def test_metadata_level_withholds_text_but_keeps_pointers(
    engine: AsyncEngine, clock: FrozenClock, tmp_path: Path
) -> None:
    stack = Stack(engine, clock, tmp_path, level="metadata")
    await stack.task()
    await stack.events_n(3, words=5)
    await stack.repo.put_summary(TASK, "CALLER cannot log in", verified=True)
    packet = await stack.builder.build(TASK, ["card", "summary", "last_events"])
    assert packet.data_classes() == {"metadata"} and not packet.fetched_content()
    text = "\n".join(m["content"] for m in packet.messages())
    assert "word0" not in text and "cannot log in" not in text and "Login fails" not in text
    assert "te:1" in text and "data rule" in text


async def test_translation_is_used_and_marked(stack: Stack) -> None:
    async def translate(text: str, lang: str, task_id: str) -> str:
        return "Hello, the login does not work. Please help."

    stack.memory._translate = translate
    await stack.memory.add_event(
        TASK,
        "Hallo, die Anmeldung funktioniert nicht. Bitte helfen Sie mir, ich kann nicht arbeiten.",
        actor="CALLER",
        channel="mail",
        kind="message",
        pseudonymised=True,
    )
    packet = await stack.builder.build(TASK, ["last_events"])
    item = packet.section("last_events").items[0]  # type: ignore[union-attr]
    assert "(translated from de)" in item.text and "login does not work" in item.text


async def test_messages_order_and_own_sections(stack: Stack) -> None:
    stack.personal.ensure()
    (stack.personal.directory / "profile.md").write_text("# Profile\n- Role: L2 support", encoding="utf-8")
    await stack.lessons.add("L1", "global", "Greeting: use Hi, not Dear")
    await stack.lessons.set_status("L1", "active")
    await stack.events_n(1, words=5)
    packet = await stack.builder.build(TASK, DEFAULT_NEEDS)
    own, data = packet.messages()
    assert own["content"].startswith("About me and my rules") and "Role: L2 support" in own["content"]
    assert "Greeting: use Hi" in own["content"] and "<data" not in own["content"]
    assert data["content"].index("Task card") < data["content"].index("Latest events")  # newest last
    assert '<data source="last_events">' in data["content"]


async def test_manifest_is_logged(stack: Stack) -> None:
    await stack.events_n(2, words=5)
    packet = await stack.builder.build(TASK, ["card", "last_events"], recipe="r03")
    rows = await stack.context_log.for_task(TASK)
    assert rows and rows[0]["recipe"] == "r03" and rows[0]["tokens"] == packet.tokens


# ---------------------------------------------------------------- search_memory


async def test_search_memory_event_document_and_events(stack: Stack) -> None:
    ids = await stack.events_n(20, words=5)
    await stack.memory.add_event(
        TASK,
        "The archive export fails with error E-42",
        actor="CALLER",
        channel="mail",
        kind="message",
        pseudonymised=True,
    )
    one = await stack.builder.search_memory(SearchMemory(task_id=TASK, kind="event", ref=f"te:{ids[0]}"))
    assert one.items[0].ref == f"te:{ids[0]}" and "Message 0" in one.items[0].text
    found = await stack.builder.search_memory(
        SearchMemory(task_id=TASK, kind="events", query="archive export")
    )
    assert "E-42" in found.items[0].text
    await stack.index.upsert("kb1", "kb", "Archive export", "Restart the export service, then retry.")
    doc = await stack.builder.search_memory(SearchMemory(task_id=TASK, kind="document", ref="doc:kb1"))
    assert "Restart the export service" in doc.items[0].text


async def test_search_memory_cannot_read_another_task(stack: Stack) -> None:
    await stack.task("INC30000001")
    other = await stack.events_n(1, task_id="INC30000001", words=5)
    result = await stack.builder.search_memory(SearchMemory(task_id=TASK, kind="event", ref=f"te:{other[0]}"))
    assert result.items == [] and "no event" in (result.note or "")


async def test_search_memory_is_a_silent_tool_through_the_gate(
    stack: Stack, engine: AsyncEngine, clock: FrozenClock
) -> None:
    from agent.db.repositories import ActionRepo, AuditRepo, UndoRepo
    from agent.policy.gate import Gate, GateContext
    from agent.policy.modes import load_policy
    from agent.policy.registry import Registry
    from agent.runtime.outbox import Outbox

    registry = Registry(load_policy())
    stack.service.register_tools(registry)
    stack.service.register_tools(registry)  # idempotent
    ids = await stack.events_n(1, words=5)
    outbox = Outbox(
        Gate(registry, ["corp.example"]),
        registry,
        ActionRepo(engine, clock),
        AuditRepo(engine, clock),
        UndoRepo(engine, clock),
        {},
        clock,
    )
    result = await outbox.submit(
        "search_memory", {"task_id": TASK, "kind": "event", "ref": f"te:{ids[0]}"}, GateContext(task_id=TASK)
    )
    assert result.status == "done" and "Message 0" in result.result["items"][0]["text"]


async def test_budgets_can_be_overridden(stack: Stack) -> None:
    await stack.events_n(10, words=100)
    small = ContextBuilder(stack.memory, None, None, "chat", budgets={"last_events": 200})
    packet = await small.build(TASK, ["last_events"])
    assert packet.tokens <= 200


async def test_graph_records_the_trigger_updates_the_summary_and_writes_a_precedent(
    engine: AsyncEngine, clock: FrozenClock, tmp_path: Path
) -> None:
    from agent.db.repositories import EventRepo
    from agent.graphs.runner import open_checkpointer
    from agent.memory.service import MemoryService
    from agent.memory.summary import SummaryUpdater
    from agent.memory.task_memory import TaskMemory
    from agent.model.fake import RecordedModel
    from tests.graphs.harness import TASK as GRAPH_TASK
    from tests.graphs.harness import Harness

    async with open_checkpointer(tmp_path / "cp.db") as saver:
        h = Harness(engine, clock, saver)
        memory = TaskMemory(h.memory, h.ledger, clock, EventRepo(engine, clock), h.actions)
        model = RecordedModel(
            {
                "summary": [
                    {"summary": "Login fails; a reply is waiting for approval.", "current_state": "needs_you"}
                ]
            }
        )
        stack_knowledge = Stack(engine, clock, tmp_path).knowledge
        service = MemoryService(
            memory,
            ContextBuilder(memory, None, None, "text"),
            SummaryUpdater(model, memory, "text"),
            stack_knowledge,
        )
        h.deps.memory_hooks = service
        h.ledger.add_listener(service.on_state_change)
        await h.caller_mail("M1")
        await h.drain()
        events = await h.memory.task_events(GRAPH_TASK, limit=50)
        messages = [e for e in events if e["kind"] == "message"]
        assert len(messages) == 1 and "Please help." in messages[0]["text_pseudonymised"]
        assert messages[0]["source_ref"].startswith("event:")
        view = await memory.summary(GRAPH_TASK)
        assert view.verified and view.text == "Login fails; a reply is waiting for approval."
        assert "Please help." in model.calls[0]["messages"][0]["content"]

        await h.ledger.transition(GRAPH_TASK, "done", "user", "fixed")
        hits = await stack_knowledge.search("login", kind="precedent")
        assert [hit.ref for hit in hits] == [f"doc:precedent:{GRAPH_TASK}"]
        assert "CALLER" not in hits[0].snippet


async def test_style_samples_newest_first_within_budget(stack: Stack) -> None:
    style = stack.personal.directory / "style"
    style.mkdir(parents=True)
    (style / "old.md").write_text("Old sample " * 200, encoding="utf-8")  # too big for the budget
    (style / "short.md").write_text("Hi CALLER,\nshort and friendly.\nUdit", encoding="utf-8")
    packet = await stack.builder.build(TASK, ["style"])
    section = packet.section("style")
    assert section is not None and [i.text for i in section.items] == [
        "Hi CALLER,\nshort and friendly.\nUdit"
    ]
    assert "How I write" in packet.messages()[0]["content"]


async def test_search_memory_precedents_and_facts(stack: Stack) -> None:
    await stack.task("INC30000001", title="Login fails after password change")
    await stack.ledger.transition("INC30000001", "in_progress", "worker")
    await stack.ledger.transition("INC30000001", "done", "user")
    await stack.repo.set_fact(TASK, "site", "SITE_1", source_ref="event:1")
    more = await stack.builder.search_memory(
        SearchMemory(task_id=TASK, kind="precedents", query="password change")
    )
    assert [i.ref for i in more.items] == ["doc:precedent:INC30000001"]
    shown = await stack.builder.search_memory(
        SearchMemory(task_id=TASK, kind="precedents", exclude=["doc:precedent:INC30000001"])
    )
    assert shown.items == [] and shown.note == "no more similar tasks"
    facts = await stack.builder.search_memory(SearchMemory(task_id=TASK, kind="facts"))
    assert facts.items[0].text == "fact site: SITE_1 [event:1]"
    nothing = await stack.builder.search_memory(SearchMemory(task_id=TASK, kind="events", query=""))
    assert nothing.note == "give a query"
    missing = await stack.builder.search_memory(SearchMemory(task_id=TASK, kind="document", ref="doc:nope"))
    assert missing.note == "no document doc:nope"
