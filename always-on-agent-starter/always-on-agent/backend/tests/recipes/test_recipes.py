import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.core.config import CONFIG_DIR, load_config
from agent.core.errors import DataRuleViolation
from agent.db.repositories import EventRepo, RecipeStateRepo
from agent.graphs.runner import open_checkpointer
from agent.memory.context_builder import ContextBuilder
from agent.memory.service import MemoryService
from agent.memory.task_memory import TaskMemory
from agent.model.fake import RecordedModel
from agent.recipes.harness import HarnessError, run_recipe_on_fixture
from agent.recipes.loader import RecipeLoader
from agent.recipes.runner import RecipeRunner
from agent.recipes.schema import RecipeSpec, Trigger
from agent.recipes.triggers import RecipeCreateRule, RecipeMatcher, resolve_exclusive, schedule_specs
from agent.runtime.coordinator import Event
from tests.graphs.harness import TASK, Harness
from tests.policy import tools as t
from tests.recipes.steps import sample

STEPS = ("tests.recipes.steps.",)


def recipe(name: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "project": "doxis",
        "status": "live",
        "trigger": {"event": "mail", "match": "login"},
        "model": "none",
        "tools": ["update_ledger"],
        "steps": "tests.recipes.steps.sample:note_only",
    }
    return base | fields


def write(folder: Path, data: dict[str, Any] | str, file: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    name = file or (data["name"] if isinstance(data, dict) else "broken")
    path = folder / f"{name}.yaml"
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return tmp_path / "recipes"


def loader_for(folder: Path, clock: FrozenClock, state: RecipeStateRepo | None = None) -> RecipeLoader:
    return RecipeLoader(t.build(), clock, folder, state, step_prefixes=STEPS, poll_minutes={"teams": 5})


# ---------------------------------------------------------------- schema


def test_trigger_rules() -> None:
    assert Trigger(poll="teams_chats", every="5m", match="refresh").source == "teams"
    assert Trigger(schedule="09:00, 13:30").times == ["09:00", "13:30"]
    assert Trigger(schedule="brief").times == []
    assert Trigger(poll="mail", every="90s").every_minutes == 2
    for bad in [
        {},
        {"schedule": "9am"},
        {"event": "fax"},
        {"poll": "mail", "event": "mail"},
        {"event": "mail", "every": "5m"},
        {"event": "mail", "match": "("},
    ]:
        with pytest.raises(ValueError):
            Trigger.model_validate(bad)


def test_spec_rules() -> None:
    assert RecipeSpec.model_validate(recipe("ok")).stage == "live"
    for bad in [
        recipe("Bad Name"),
        recipe("x", exclusive_with=["x"]),
        recipe("x", tools=["a", "a"]),
        recipe("x", steps="not a ref"),
        recipe("x", data_classes=[]),
        recipe("x", trigger={"schedule": "09:00"}, creates_task={"id_prefix": "REF"}),
        recipe("x", unknown_field=1),
    ]:
        with pytest.raises(ValueError):
            RecipeSpec.model_validate(bad)


# ---------------------------------------------------------------- loader


async def test_invalid_recipe_is_turned_off_with_a_clear_error(
    folder: Path, engine: AsyncEngine, clock: FrozenClock
) -> None:
    state = RecipeStateRepo(engine, clock)
    write(folder, recipe("good"))
    write(folder, recipe("bad", trigger={"schedule": "9am"}))
    write(folder, "name: [unclosed", file="broken")
    report = await loader_for(folder, clock, state).load_all()
    assert report.loaded == ["good"]
    assert "trigger.schedule" in report.failed["bad"] and "HH:MM" in report.failed["bad"]
    assert report.failed["broken"].startswith("not valid YAML")
    row = await state.get("bad")
    assert row is not None and row["stage"] == "off" and not row["enabled"] and "HH:MM" in row["error"]
    good = await state.get("good")
    assert good is not None and good["enabled"] and good["error"] is None and good["content_hash"]


async def test_hand_off_tool_is_refused(folder: Path, clock: FrozenClock) -> None:
    write(folder, recipe("deleter", tools=["update_ledger", "delete_item"]))
    write(folder, recipe("sneaky", tools=["remove_user"]))  # Hand off by name even if never registered
    report = await loader_for(folder, clock).load_all()
    assert "delete_item is a Hand off action" in report.failed["deleter"]
    assert "remove_user is a Hand off action" in report.failed["sneaky"]


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"tools": ["no_such_tool"]}, "unknown tool"),
        ({"steps": "os:system"}, "outside tests.recipes.steps."),
        ({"steps": "agent.core.secrets:get_secret"}, "outside"),
        ({"steps": "tests.recipes.steps.sample:not_async"}, "async function"),
        ({"steps": "tests.recipes.steps.missing:fn"}, "cannot import"),
    ],
)
async def test_load_errors(folder: Path, clock: FrozenClock, fields: dict[str, Any], error: str) -> None:
    write(folder, recipe("r1", **fields))
    report = await loader_for(folder, clock).load_all()
    assert error in report.failed["r1"]


async def test_name_must_match_file_and_be_unique(folder: Path, clock: FrozenClock) -> None:
    write(folder, recipe("alpha"), file="beta")
    report = await loader_for(folder, clock).load_all()
    assert "must match the file name" in report.failed["beta"]


async def test_off_recipe_loads_but_never_runs(folder: Path, clock: FrozenClock) -> None:
    write(folder, recipe("sleepy", status="off"))
    loader = loader_for(folder, clock)
    report = await loader.load_all()
    assert report.loaded == ["sleepy"] and loader.get("sleepy") is None and loader.all() == []


async def test_hot_reload_keeps_running_versions(folder: Path, clock: FrozenClock) -> None:
    path = write(folder, recipe("r1", instructions="v1"))
    loader = loader_for(folder, clock)
    await loader.load_all()
    first = loader.get("r1")
    assert first is not None and await loader.reload_if_changed() is None  # nothing changed
    path.write_text(yaml.safe_dump(recipe("r1", instructions="v2")), encoding="utf-8")
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10_000_000))
    report = await loader.reload_if_changed()
    assert report is not None and report.loaded == ["r1"]
    current = loader.get("r1")
    assert current is not None and current.spec.instructions == "v2"
    pinned = loader.version("r1", first.content_hash)
    assert pinned is not None and pinned.spec.instructions == "v1"  # a job that started on v1 keeps it
    path.unlink()
    await loader.reload_if_changed()
    assert loader.version("r1", first.content_hash) is None  # removed: nothing runs it any more


async def test_poll_frequency_warning(folder: Path, clock: FrozenClock) -> None:
    write(folder, recipe("fast", trigger={"poll": "teams_chats", "every": "1m", "match": "refresh"}))
    report = await loader_for(folder, clock).load_all()
    assert report.warnings == ["fast: wants every 1 min but teams is polled every 5 min"]


# ---------------------------------------------------------------- triggers


def _event(text: str, source: str = "mail") -> Event:
    return Event(
        source=source,
        kind="mail",
        external_id="e1",
        received_at=datetime(2026, 3, 25, tzinfo=UTC),
        payload={"subject": text},
    )  # type: ignore[arg-type]


async def test_two_matching_recipes_both_run_unless_exclusive(folder: Path, clock: FrozenClock) -> None:
    write(folder, recipe("r-a"))
    write(folder, recipe("r-b"))
    write(folder, recipe("r-teams", trigger={"event": "teams", "match": "login"}))
    loader = loader_for(folder, clock)
    await loader.load_all()
    matcher = RecipeMatcher(loader)
    assert matcher(_event("Login fails")) == ["r-a", "r-b"]
    assert matcher(_event("printer")) == []
    write(folder, recipe("r-b", exclusive_with=["r-a"]))
    await loader.load_all()
    assert matcher(_event("Login fails")) == ["r-b"]  # the one that declares it wins


def test_mutual_exclusion_is_deterministic(clock: FrozenClock) -> None:
    from agent.recipes.loader import LoadedRecipe

    def loaded(name: str, other: str) -> LoadedRecipe:
        return LoadedRecipe(
            RecipeSpec.model_validate(recipe(name, exclusive_with=[other])),
            None,
            Path(name),
            name,
            clock.now_utc(),
        )

    recipes = {"a": loaded("aa", "bb"), "b": loaded("bb", "aa")}
    recipes = {r.name: r for r in recipes.values()}
    assert resolve_exclusive(["bb", "aa"], recipes) == ["aa"]


async def test_create_rule_numbers_tasks(folder: Path, engine: AsyncEngine, clock: FrozenClock) -> None:
    from agent.db.repositories import Task, TaskRepo

    write(
        folder,
        recipe(
            "refresh",
            project="powerbi",
            trigger={"poll": "teams_chats", "match": "refresh"},
            creates_task={"id_prefix": "REF"},
        ),
    )
    loader = loader_for(folder, clock)
    await loader.load_all()
    tasks = TaskRepo(engine, clock)
    rule = RecipeCreateRule(loader, tasks)
    spec = await rule.match(_event("please refresh Sales", "teams"))
    assert spec is not None and (spec.id, spec.project, spec.recipe) == ("REF-0001", "powerbi", "refresh")
    await tasks.create(Task(id="REF-0412", project="powerbi", kind="r", title="t", state="new"))
    await tasks.create(Task(id="REF_X", project="powerbi", kind="r", title="t", state="new"))  # not a REF- id
    assert (await rule.match(_event("refresh again", "teams"))).id == "REF-0413"  # type: ignore[union-attr]
    assert await rule.match(_event("hello", "teams")) is None


async def test_schedule_specs(folder: Path, clock: FrozenClock) -> None:
    write(folder, recipe("noon", trigger={"schedule": "12:00,16:00"}))
    write(folder, recipe("morning", trigger={"schedule": "brief"}))
    loader = loader_for(folder, clock)
    await loader.load_all()
    specs = schedule_specs(loader.all(), load_config(CONFIG_DIR / "config.example.yaml"))
    assert [(s.name, s.times) for s in specs] == [("recipe:noon", ("12:00", "16:00"))]


# ---------------------------------------------------------------- inside the task graph


@pytest.fixture
async def h(engine: AsyncEngine, clock: FrozenClock, tmp_path: Path, folder: Path) -> AsyncIterator[Harness]:
    async with open_checkpointer(tmp_path / "checkpoints.db") as saver:
        harness = Harness(engine, clock, saver)
        memory = TaskMemory(harness.memory, harness.ledger, clock, EventRepo(engine, clock), harness.actions)
        builder = ContextBuilder(memory, None, None, "text")
        harness.recipe_state = RecipeStateRepo(engine, clock)  # type: ignore[attr-defined]
        harness.loader = RecipeLoader(
            harness.registry, clock, folder, harness.recipe_state, step_prefixes=STEPS
        )  # type: ignore[attr-defined]
        harness.model = RecordedModel()  # type: ignore[attr-defined]
        runner = RecipeRunner(
            harness.loader,
            harness.registry,
            memory,
            builder,
            harness.model,  # type: ignore[attr-defined]
            {"outlook": harness.world.mail, "teams": harness.world.teams},
            clock,
            outbox=harness.outbox,
            gate=harness.gate,
            state=harness.recipe_state,  # type: ignore[attr-defined]
        )
        harness.recipes = runner  # type: ignore[attr-defined]
        harness.deps.step = runner
        harness.deps.memory_hooks = MemoryService(memory, builder)
        harness.coordinator._match_recipes = RecipeMatcher(harness.loader)  # type: ignore[attr-defined]
        yield harness


async def test_live_recipe_drafts_through_the_gate_and_flags_fetched_content(
    h: Harness, folder: Path
) -> None:
    write(
        folder,
        recipe(
            "drafter",
            model="haiku",
            data_classes=["metadata", "text"],
            tools=["post_chat"],
            steps="tests.recipes.steps.sample:draft_reply",
        ),
    )
    await h.loader.load_all()  # type: ignore[attr-defined]
    h.model.add("drafter", {"reply": "Hello, please try again now."})  # type: ignore[attr-defined]
    await h.caller_mail("M1")
    assert await h.drain() == [("new_event", "parked")]
    action = await h.only_action()
    assert action.status == "proposed" and action.content == "Hello, please try again now."
    audit = await h.audit.for_action(action.id)
    assert "FLAGGED" in audit[0]["result"] and audit[0]["recipe"] == "drafter"
    call = h.model.calls[0]  # type: ignore[attr-defined]
    assert (
        call["recipe"] == "drafter" and "Please help." in call["messages"][-1]["content"]
    )  # the mail, as data
    assert len(h.slack.open_cards()) == 1


async def test_shadow_records_would_do_with_the_draft(h: Harness, folder: Path) -> None:
    write(
        folder,
        recipe(
            "drafter",
            status="shadow",
            model="haiku",
            data_classes=["metadata", "text"],
            tools=["post_chat"],
            steps="tests.recipes.steps.sample:draft_reply",
        ),
    )
    await h.loader.load_all()  # type: ignore[attr-defined]
    h.model.add("drafter", {"reply": "Shadow draft."})  # type: ignore[attr-defined]
    await h.caller_mail("M1")
    assert await h.drain() == [("new_event", "done")]
    [action] = await h.actions.open_for_task(TASK, ("shadow",))
    assert (action.status, action.content) == ("shadow", "Shadow draft.")
    assert h.slack.cards == {} and h.posts() == []
    stats = await h.recipe_state.get("drafter")  # type: ignore[attr-defined]
    assert stats is not None and '"would_do": 1' in stats["stats_json"] and '"runs": 1' in stats["stats_json"]


async def test_both_matching_recipes_run_on_the_task(h: Harness, folder: Path) -> None:
    write(folder, recipe("r-a"))
    write(folder, recipe("r-b"))
    await h.loader.load_all()  # type: ignore[attr-defined]
    t.ledger_notes.clear()
    sample.seen["events"] = []
    await h.caller_mail("M1")
    await h.drain()
    assert t.ledger_notes == ["seen by r-a", "seen by r-b"]
    write(folder, recipe("r-a", exclusive_with=["r-b"]))
    await h.loader.load_all()  # type: ignore[attr-defined]
    sample.seen["events"] = []
    await h.caller_mail("M2", subject=f"RE: {TASK} login still fails")
    await h.drain()
    assert [name for name, _ in sample.seen["events"]] == ["r-a"]  # r-b did not run for this event


async def test_multi_turn_recipe(h: Harness, folder: Path) -> None:
    write(folder, recipe("turns", steps="tests.recipes.steps.sample:two_turns"))
    await h.loader.load_all()  # type: ignore[attr-defined]
    t.ledger_notes.clear()
    await h.caller_mail("M1")
    await h.drain()
    assert t.ledger_notes == ["turn 0", "turn 1"]


async def test_recipe_turned_off_mid_run_is_skipped(h: Harness, folder: Path) -> None:
    write(
        folder,
        recipe(
            "drafter",
            model="haiku",
            data_classes=["metadata", "text"],
            tools=["post_chat"],
            steps="tests.recipes.steps.sample:draft_reply",
        ),
    )
    write(folder, recipe("zz-after"))
    await h.loader.load_all()  # type: ignore[attr-defined]
    h.model.add("drafter", {"reply": "Hello."})  # type: ignore[attr-defined]
    t.ledger_notes.clear()
    await h.caller_mail("M1")
    await h.drain()  # parked on the drafter's approval; zz-after still queued
    write(folder, recipe("zz-after", status="off"))
    await h.loader.load_all()  # type: ignore[attr-defined]
    action = await h.only_action()
    await h.approvals.approve(action.id, action.content_hash)
    await h.drain()
    assert h.posts() == ["Hello."] and t.ledger_notes == []


async def test_steps_cannot_reach_write_methods(h: Harness, folder: Path) -> None:
    write(folder, recipe("prober", tools=["update_ledger"], steps="tests.recipes.steps.sample:try_to_write"))
    await h.loader.load_all()  # type: ignore[attr-defined]
    await h.caller_mail("M1")
    await h.drain()
    attempts = sample.seen["attempts"]
    assert attempts["send"] == attempts["move"] == attempts["drafts"] == "PolicyViolation"
    assert attempts["setattr"] == "PolicyViolation" and attempts["outbox"] == "AttributeError"
    assert "send" not in attempts["reads"] and "list_new" in attempts["reads"]
    assert int(attempts["read_count"]) > 0  # reads work
    assert "not a read" in attempts["read_report"] and attempts["unlisted"] == "PolicyViolation"
    assert h.world.mail.writes_to("send") == [] and h.world.mail.writes_to("move") == []


async def test_recipe_gets_a_packet_reduced_to_what_it_declared(h: Harness, folder: Path) -> None:
    write(
        folder,
        recipe(
            "meta-only",
            model="haiku",
            data_classes=["metadata"],
            tools=["post_chat"],
            steps="tests.recipes.steps.sample:draft_reply",
        ),
    )
    await h.loader.load_all()  # type: ignore[attr-defined]
    h.model.add("meta-only", {"reply": "Hello."})  # type: ignore[attr-defined]
    await h.caller_mail("M1")
    await h.drain()
    call = h.model.calls[0]  # type: ignore[attr-defined]
    assert call["data_classes"] == {"metadata"}
    assert "Please help." not in str(call["messages"]) and "data rule" in str(call["messages"])


async def test_undeclared_extra_data_is_refused(h: Harness, folder: Path) -> None:
    from agent.recipes.runner import ReadOnlyAdapters, StepContext

    spec = RecipeSpec.model_validate(recipe("meta-only", model="haiku", data_classes=["metadata"]))
    ctx = StepContext(
        spec, None, {}, None, ReadOnlyAdapters({}, h.registry), RecordedModel(), h.registry, None, 0
    )
    with pytest.raises(DataRuleViolation, match="did not declare text"):
        await ctx.model(sample.Draft, extra="a mail body read by the step")


async def test_scheduled_recipe_runs_without_a_task(h: Harness, folder: Path) -> None:
    write(
        folder,
        recipe(
            "morning",
            trigger={"schedule": "brief"},
            tools=["update_ledger", "post_chat"],
            steps="tests.recipes.steps.sample:brief",
        ),
    )
    await h.loader.load_all()  # type: ignore[attr-defined]
    t.ledger_notes.clear()
    h.runner.add_schedule_listener(h.recipes.on_schedule)  # type: ignore[attr-defined]
    await h.coordinator.ingest(
        {
            "kind": "schedule",
            "external_id": "brief@1",
            "received_at": h.clock.now_utc(),
            "payload": {"schedule": "brief"},
        },
        "schedule",
    )
    await h.drain()
    assert t.ledger_notes == ["brief posted"]
    assert "runs without a task" in h.recipes.last_context.notes[0]  # type: ignore[attr-defined]


async def test_default_step_drops_unlisted_tools(h: Harness, folder: Path) -> None:
    write(
        folder,
        recipe(
            "plain", model="haiku", data_classes=["metadata", "text"], steps=None, tools=["update_ledger"]
        ),
    )
    await h.loader.load_all()  # type: ignore[attr-defined]
    h.model.add(
        "plain",
        {
            "note": "noted",
            "proposals": [
                {"tool": "send_mail", "args": {"draft_id": "D1", "to": []}},
                {"tool": "update_ledger", "args": {"text": "from model"}},
            ],
        },
    )  # type: ignore[attr-defined]
    t.ledger_notes.clear()
    await h.caller_mail("M1")
    await h.drain()
    notes = h.recipes.last_context.notes  # type: ignore[attr-defined]
    assert notes[0] == "noted" and "send_mail" in notes[1]
    # The model read the mail, then proposed a Report action: rule 4 raises it to Ask. Nothing ran.
    action = await h.only_action()
    assert action.tool == "update_ledger" and action.mode == "ask" and action.status == "proposed"
    assert t.ledger_notes == []


# ---------------------------------------------------------------- harness


async def test_run_recipe_on_fixture(folder: Path) -> None:
    write(
        folder,
        recipe(
            "numbers",
            project="powerbi",
            model="haiku",
            data_classes=["metadata", "text"],
            trigger={"event": "mail", "match": "numbers look off"},
            creates_task={"id_prefix": "ENH"},
            tools=["post_chat"],
            steps="tests.recipes.steps.sample:draft_reply",
        ),
    )
    result = await run_recipe_on_fixture(
        "numbers",
        "emails/messages.json#AAMkAGFAKE0012",
        recipes_dir=folder,
        registry=t.build(),
        step_prefixes=STEPS,
        model_responses={"numbers": [{"reply": "Thanks, I am checking the March numbers."}]},
    )
    assert result.task_id == "ENH-0001" and result.turns == 1
    assert [p.tool for p in result.proposals] == ["post_chat"]
    assert (
        result.proposals[0].content == "Thanks, I am checking the March numbers."
        and result.proposals[0].triggered_by_fetched_content
    )
    assert result.outputs[0].reply.startswith("Thanks")
    assert result.manifests and "last_events" in result.manifests[0]["sections"]


async def test_harness_errors(folder: Path) -> None:
    write(folder, recipe("numbers", trigger={"event": "mail", "match": "never matches"}))
    with pytest.raises(HarnessError, match="reached no task"):
        await run_recipe_on_fixture(
            "numbers",
            "emails/messages.json#AAMkAGFAKE0012",
            recipes_dir=folder,
            registry=t.build(),
            step_prefixes=STEPS,
        )
    with pytest.raises(HarnessError, match="unknown or off"):
        await run_recipe_on_fixture(
            "ghost", None, recipes_dir=folder, registry=t.build(), step_prefixes=STEPS
        )


def test_closed_schemas_carry_no_text() -> None:
    from typing import Literal

    from pydantic import BaseModel

    from agent.recipes.runner import closed_schema

    class Label(BaseModel):
        label: Literal["a", "b"]
        confidence: float
        urgent: bool

    class Words(BaseModel):
        label: Literal["a", "b"]
        reason: str

    assert closed_schema(Label) and not closed_schema(Words)
