"""Recipe test harness: run_recipe_on_fixture(slug, fixture) -> proposals and outputs (F12, 2.16).

Runs one recipe on one fixture with fakes and a throwaway database, nothing real touched:

    result = await run_recipe_on_fixture(
        "pbi-refresh",
        "emails/messages.json#AAMkAGFAKE0011",
        model_responses={"pbi-refresh": [{"note": "...", "proposals": []}]},
        registry=my_registry,
    )
    result.proposals, result.outputs, result.notes

The fixture is "emails/messages.json#<message id>", a dict {"source": "mail", "message": {...Graph
mail...}}, a dict {"source": ..., "event": {kind, external_id, payload}}, or an Event. Event-triggered
recipes go through the real coordinator (task creation, links) and the real context builder; scheduled
recipes run without a task. Proposals are returned, not executed; Silent read tools do run, against the
fakes, through the gate.
"""

import json
import tempfile
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict

from agent.adapters.base import MailMessage
from agent.adapters.fakes._base import FIXTURES_DIR
from agent.adapters.fakes.outlook import to_mail
from agent.adapters.fakes.world import FIXTURE_NOW, FakeWorld
from agent.core.clock import FrozenClock
from agent.core.config import CONFIG_DIR, Config, load_config
from agent.core.errors import AgentError
from agent.db.engine import create_engine_async
from agent.db.migrate import upgrade_head
from agent.db.repositories import (
    ActionRepo,
    AuditRepo,
    ClockRepo,
    ContextLogRepo,
    DirectoryRepo,
    EventRepo,
    JobRepo,
    LessonRepo,
    MemoryRepo,
    TaskRepo,
    TokenCipher,
    TokenMapRepo,
    UndoRepo,
)
from agent.db.search import Fts5SearchIndex
from agent.graphs.task_graph import GraphState, Proposal, initial_state
from agent.ledger.service import Ledger
from agent.memory.context_builder import ContextBuilder
from agent.memory.knowledge import Knowledge
from agent.memory.service import MemoryService
from agent.memory.task_memory import TaskMemory
from agent.model.fake import RecordedModel, Response
from agent.policy.gate import Gate
from agent.policy.modes import load_policy
from agent.policy.registry import Registry
from agent.privacy.hook import DataClass
from agent.privacy.pseudonymiser import LedgerPeople, Pseudonymiser, people_from_mail
from agent.recipes.loader import ALLOWED_STEP_PREFIXES, RecipeLoader
from agent.recipes.runner import RecipeRunner
from agent.recipes.triggers import RecipeCreateRule, RecipeMatcher
from agent.runtime.coordinator import Coordinator, Event, Source
from agent.runtime.outbox import Outbox


class HarnessError(AgentError):
    """The recipe could not be run on this fixture."""


class HarnessResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    recipe: str
    task_id: str | None
    proposals: list[Proposal]
    outputs: list[Any]
    notes: list[str]
    manifests: list[dict[str, Any]]
    turns: int


def load_fixture(fixture: str | dict[str, Any] | Event) -> tuple[Any, Source]:
    """(raw item for Coordinator.ingest, source)."""
    if isinstance(fixture, Event):
        return fixture, fixture.source
    if isinstance(fixture, str):
        file, _, item_id = fixture.partition("#")
        data = json.loads((FIXTURES_DIR / file).read_text(encoding="utf-8"))
        if file.startswith("emails/"):
            for raw in data:
                if raw["id"] == item_id:
                    return to_mail(raw), "mail"
        raise HarnessError(f"no fixture {fixture!r}")
    source: Source = fixture["source"]
    if "message" in fixture:
        return to_mail(fixture["message"]), source
    if "event" in fixture:
        return {"received_at": FIXTURE_NOW, **fixture["event"]}, source
    raise HarnessError("a fixture dict needs 'message' or 'event'")


async def run_recipe_on_fixture(
    slug: str,
    fixture: str | dict[str, Any] | Event | None,
    *,
    model_responses: dict[str, list[Response]] | list[Response] | None = None,
    registry: Registry | None = None,
    recipes_dir: Path | str | None = None,
    step_prefixes: tuple[str, ...] = ALLOWED_STEP_PREFIXES,
    level: DataClass = "chat",
    config: Config | None = None,
    max_turns: int = 8,
) -> HarnessResult:
    config = config or load_config(CONFIG_DIR / "config.example.yaml")
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{(Path(tmp) / 'agent.db').as_posix()}"
        upgrade_head(url)
        engine = create_engine_async(url)
        try:
            return await _run(
                slug,
                fixture,
                model_responses,
                registry,
                recipes_dir,
                step_prefixes,
                level,
                config,
                max_turns,
                engine,
            )
        finally:
            await engine.dispose()


async def _run(
    slug: str,
    fixture: str | dict[str, Any] | Event | None,
    model_responses: dict[str, list[Response]] | list[Response] | None,
    registry: Registry | None,
    recipes_dir: Path | str | None,
    step_prefixes: tuple[str, ...],
    level: DataClass,
    config: Config,
    max_turns: int,
    engine: Any,
) -> HarnessResult:
    clock = FrozenClock(config, FIXTURE_NOW)
    world = FakeWorld(clock)
    registry = registry or Registry(load_policy())
    tasks, memory_repo = TaskRepo(engine, clock), MemoryRepo(engine, clock)
    ledger = Ledger(tasks, memory_repo, ClockRepo(engine, clock), clock, config.clock)
    actions = ActionRepo(engine, clock)
    cipher = TokenCipher(Fernet.generate_key())
    people = LedgerPeople(memory_repo, DirectoryRepo(engine, clock), cipher)
    pseudo = Pseudonymiser(TokenMapRepo(engine, clock, cipher), people, config.privacy)
    events = EventRepo(engine, clock)
    task_memory = TaskMemory(
        memory_repo, ledger, clock, events, actions, pseudo, config.retention.raw_text_days
    )
    knowledge = Knowledge(
        Fts5SearchIndex(engine), clock, DirectoryRepo(engine, clock), LessonRepo(engine, clock)
    )
    log_repo = ContextLogRepo(engine, clock)
    builder = ContextBuilder(task_memory, knowledge, None, level, log_repo)
    service = MemoryService(task_memory, builder, None, knowledge)
    service.register_tools(registry)

    loader = RecipeLoader(registry, clock, recipes_dir, step_prefixes=step_prefixes)
    report = await loader.load_all()
    if slug in report.failed:
        raise HarnessError(f"recipe {slug} does not load: {report.failed[slug]}")
    recipe = loader.get(slug)
    if recipe is None:
        raise HarnessError(f"recipe {slug} is unknown or off")

    adapters = {a.name: a for a in world.adapters()}
    gate = Gate(registry, config.company_domains or ["corp.example"], config.allowed_groups, level)
    outbox = Outbox(
        gate, registry, actions, AuditRepo(engine, clock), UndoRepo(engine, clock), adapters, clock
    )
    model = RecordedModel(model_responses or [])
    runner = RecipeRunner(
        loader, registry, task_memory, builder, model, adapters, clock, outbox=outbox, gate=gate
    )

    if recipe.spec.trigger.schedule or fixture is None:
        await runner.run_taskless(recipe, {"schedule": recipe.spec.trigger.schedule or "manual"})
        ctx = runner.last_context
        assert ctx is not None
        return HarnessResult(
            recipe=slug,
            task_id=None,
            proposals=ctx.proposals,
            outputs=ctx.outputs,
            notes=ctx.notes,
            manifests=[],
            turns=1,
        )

    raw, source = load_fixture(fixture)
    coordinator = Coordinator(
        tasks,
        events,
        JobRepo(engine, clock),
        memory_repo,
        clock,
        create_rules=[RecipeCreateRule(loader, tasks)],
        recipe_matcher=RecipeMatcher(loader),
    )
    ingested = await coordinator.ingest(raw, source)
    if not ingested.task_ids:
        raise HarnessError(f"the fixture reached no task ({ingested.status}); does the trigger match it?")
    task_id = ingested.task_ids[0]
    if isinstance(raw, MailMessage):
        await people.remember(task_id, people_from_mail(raw, {raw.sender.address.lower(): "caller"}))
    trigger = {"event_id": ingested.event_id, "source": source, "recipes": [slug]}
    recorded = await service.record_trigger(task_id, trigger)
    state: GraphState = initial_state(task_id, "new_event", trigger)
    state["context"] = {"trigger_ref": f"te:{recorded}" if recorded is not None else None}

    proposals: list[Proposal] = []
    outputs: list[Any] = []
    notes: list[str] = []
    turns = 0
    while turns < max_turns:
        turns += 1
        step = await runner(state)
        proposals += step.proposals
        if runner.last_context is not None:
            outputs += runner.last_context.outputs
            notes += runner.last_context.notes
            runner.last_context = None
        state["recipe_memo"] = step.memo
        if step.done:
            break
    manifests = [json.loads(r["manifest_json"]) for r in reversed(await log_repo.for_task(task_id))]
    return HarnessResult(
        recipe=slug,
        task_id=task_id,
        proposals=proposals,
        outputs=outputs,
        notes=notes,
        manifests=manifests,
        turns=turns,
    )
