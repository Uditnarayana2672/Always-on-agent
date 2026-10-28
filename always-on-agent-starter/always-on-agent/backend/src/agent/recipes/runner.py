"""Recipe runner (F12): runs recipe steps inside the task graph, and task-less recipes on schedules.

A step is an async function of one StepContext. Code first: the step reads through read-only adapters
and read tools, asks the model only for language (ctx.model, through the gateway), and proposes tool
calls with ctx.propose. It cannot write:

- ctx.adapters exposes only read methods (list_, get_, find_, read ...). Write methods the registry
  knows (send, post, move ...) raise PolicyViolation; nothing else of the adapter is reachable through
  it. This guards against mistakes in our own step modules; it is not a sandbox for foreign code (only
  modules under agent.recipes.steps load at all).
- ctx.read(tool, args) runs a Silent tool through the gate and the outbox (audited); anything above
  Silent is refused there.
- ctx.propose only accepts tools the recipe lists. The gate and, for Ask, the user decide the rest.
- After a model call that saw other people's text, every later proposal is flagged as triggered by
  fetched content, so it needs at least Ask (golden rule 4).
- A recipe without a task (a brief on a schedule) cannot propose Ask actions: there is no task to wait
  on.

Shadow recipes run the same way; their proposals carry stage "shadow" and the outbox records them as
would-do actions, with the drafted content, without running them.

Several recipes on one task run one after the other, one turn per graph step (each recipe's proposals
are gated before the next recipe runs). Each run pins the recipe versions it started with, so a hot
reload does not change a job half way.
"""

import inspect
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, Field

from agent.adapters.base import Mode
from agent.core.clock import Clock
from agent.core.errors import DataRuleViolation, PolicyViolation
from agent.db.repositories import EventRepo, Job, RecipeStateRepo
from agent.graphs.task_graph import GraphState, Proposal, StepResult
from agent.memory.context_builder import ContextBuilder, MemoryPacket
from agent.memory.task_memory import TaskCard, TaskMemory
from agent.model.gateway import ModelRunner
from agent.model.prompts import wrap_data
from agent.policy.gate import Gate, GateContext
from agent.policy.modes import RANK
from agent.policy.registry import Registry
from agent.privacy.hook import LEVEL_ORDER, DataClass
from agent.privacy.outbound import OutboundDraft
from agent.recipes.loader import LoadedRecipe, RecipeLoader
from agent.recipes.schema import RecipeSpec
from agent.runtime.outbox import Outbox

log = logging.getLogger(__name__)

EVENT_SCHEDULE = "event"  # schedule name of task-less jobs for intake events (see on_schedule)

READ_PREFIXES = (
    "list_",
    "get_",
    "find_",
    "read",
    "search",
    "resolve_",
    "refresh_history",
    "refreshes_today",
    "refresh_cap",
    "refresh_schedule",
    "running_refresh",
    "dataflow_transactions",
    "dataset_owner",
    "chat_members",
    "upstream_",
    "groups",
    "events_between",
    "parse_",
    "count",
    "max_date",
    "health",
    "next_events",
)


class ReadOnlyAdapter:
    """One adapter's read methods, wrapped. Write methods and private state are not reachable."""

    __slots__ = ("_methods", "_name", "_writes")

    def __init__(self, name: str, target: Any, writes: frozenset[str]) -> None:
        methods: dict[str, Callable[..., Any]] = {}
        for attr in dir(target):
            if attr.startswith("_") or attr in writes or not attr.startswith(READ_PREFIXES):
                continue
            value = getattr(target, attr)
            if callable(value):
                methods[attr] = _wrap(value)
        object.__setattr__(self, "_methods", methods)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_writes", writes)

    def __getattr__(self, attr: str) -> Callable[..., Any]:
        methods: dict[str, Callable[..., Any]] = object.__getattribute__(self, "_methods")
        if attr in methods:
            return methods[attr]
        name = object.__getattribute__(self, "_name")
        raise PolicyViolation(f"{name}.{attr} is not a read method; propose a tool call instead")

    def __setattr__(self, attr: str, value: Any) -> None:
        raise PolicyViolation("adapters are read-only in recipe steps")

    def __dir__(self) -> list[str]:
        return sorted(object.__getattribute__(self, "_methods"))


def _wrap(method: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(method):

        async def call_async(*args: Any, **kwargs: Any) -> Any:
            return await method(*args, **kwargs)

        return call_async

    def call(*args: Any, **kwargs: Any) -> Any:
        return method(*args, **kwargs)

    return call


class ReadOnlyAdapters:
    __slots__ = ("_adapters",)

    def __init__(self, adapters: Mapping[str, Any], registry: Registry) -> None:
        writes: dict[str, set[str]] = {}
        for tool_name in registry.names():
            tool = registry.get(tool_name)
            if tool is not None and tool.adapter and tool.method and tool.mode != "silent":
                writes.setdefault(tool.adapter, set()).add(tool.method)
        wrapped = {
            name: ReadOnlyAdapter(name, a, frozenset(writes.get(name, ()))) for name, a in adapters.items()
        }
        object.__setattr__(self, "_adapters", wrapped)

    def __getattr__(self, name: str) -> ReadOnlyAdapter:
        adapters: dict[str, ReadOnlyAdapter] = object.__getattribute__(self, "_adapters")
        if name in adapters:
            return adapters[name]
        raise AttributeError(f"no adapter {name!r}")

    def __setattr__(self, attr: str, value: Any) -> None:
        raise PolicyViolation("adapters are read-only in recipe steps")


def closed_schema(schema: type[BaseModel]) -> bool:
    """True when every field is a Literal, bool, int or float: the answer cannot carry free text."""
    for field in schema.model_fields.values():
        annotation = field.annotation
        if annotation in (bool, int, float):
            continue
        if get_origin(annotation) is Literal and all(
            isinstance(v, (str, int, bool)) for v in get_args(annotation)
        ):
            continue
        return False
    return bool(schema.model_fields)


class ModelProposal(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    content: str | None = None


class DefaultOutput(BaseModel):
    """What a recipe without a steps module asks the model for."""

    note: str = ""
    proposals: list[ModelProposal] = Field(default_factory=list)


class StepContext:
    """Everything a step may use. No outbox, no write methods, no database."""

    def __init__(
        self,
        recipe: RecipeSpec,
        task: TaskCard | None,
        trigger: dict[str, Any],
        packet: MemoryPacket | None,
        adapters: ReadOnlyAdapters,
        model: ModelRunner | None,
        registry: Registry,
        reader: Callable[[str, dict[str, Any]], Any] | None,
        turn: int,
        outcomes: list[dict[str, Any]] | None = None,
    ) -> None:
        self.recipe = recipe
        self.task = task
        self.trigger = dict(trigger)
        self.packet = packet
        self.adapters = adapters
        self.turn = turn
        # What this run's earlier proposals came to: {tool, status, action_id, note, result}.
        self.outcomes: list[dict[str, Any]] = list(outcomes or [])
        self.memo: dict[str, Any] = {}  # the step's own notes between turns (kept in the checkpoint)
        self.last_tier: str | None = None  # the model tier the last answer came from (after fallback)
        self.last_downgraded = False
        self.proposals: list[Proposal] = []
        self.outputs: list[Any] = []
        self.notes: list[str] = []
        self._model = model
        self._registry = registry
        self._reader = reader
        self._fetched = False
        self._again = False

    @property
    def task_id(self) -> str | None:
        return self.task.id if self.task else None

    async def model[T: BaseModel](
        self,
        schema: type[T],
        *,
        purpose: str | None = None,
        extra: str | None = None,
        extra_class: DataClass = "text",
        instructions: str | None = None,
    ) -> T:
        """Ask the model (through the gateway) with the context packet plus optional extra data."""
        if self._model is None or self.recipe.model == "none":
            raise PolicyViolation(f"recipe {self.recipe.name} declares no model")
        messages = self.packet.messages() if self.packet else []
        classes: set[DataClass] = self.packet.data_classes() if self.packet else {"metadata"}
        if extra:
            messages = [*messages, {"role": "user", "content": wrap_data(extra, "step")}]
            classes = classes | {extra_class}
        undeclared = sorted(classes - set(self.recipe.data_classes) - {"metadata"})
        if undeclared:
            raise DataRuleViolation(f"recipe {self.recipe.name} did not declare {', '.join(undeclared)}")
        text = "\n\n".join(t for t in (self.recipe.instructions.strip(), instructions or "") if t)
        result = await self._model.run(
            purpose or self.recipe.name,
            messages,
            schema,
            classes,
            tier=self.recipe.model,
            task_id=self.task_id,
            recipe=self.recipe.name,
            instructions=text or None,
        )
        if (extra or (self.packet is not None and self.packet.fetched_content())) and not closed_schema(
            schema
        ):
            # Anything proposed from here on came after reading other people's text. A closed answer
            # (only fixed labels, numbers, yes/no) carries no words from that text into any argument,
            # so code that maps a label to an action keeps the action's own mode (R02 triage).
            self._fetched = True
        self.last_tier = result.tier
        self.last_downgraded = result.downgraded
        self.outputs.append(result.output)
        return result.output

    async def read(self, tool: str, args: dict[str, Any]) -> Any:
        """Run a Silent tool the recipe lists, through the gate and the outbox."""
        if tool not in self.recipe.tools:
            raise PolicyViolation(f"recipe {self.recipe.name} does not list {tool}")
        if self._reader is None:
            raise PolicyViolation("no tool reader in this context")
        result = self._reader(tool, args)
        return await result if inspect.isawaitable(result) else result

    def propose(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        content: str | None = None,
        purpose: str = "action",
        draft: OutboundDraft | None = None,
        idempotency_key: str | None = None,
        facts: dict[str, Any] | None = None,
    ) -> None:
        if tool not in self.recipe.tools:
            raise PolicyViolation(f"recipe {self.recipe.name} does not list {tool}")
        mode = self._mode(tool, args, facts or {})
        if self.task is None and RANK[mode] >= RANK["ask"]:
            raise PolicyViolation(
                f"{tool} needs an approval, and recipe {self.recipe.name} runs without a task"
            )
        self.proposals.append(
            Proposal(
                tool=tool,
                args=args,
                content=content,
                purpose=purpose,
                draft=draft,
                idempotency_key=idempotency_key,
                triggered_by_fetched_content=self._fetched,
                recipe=self.recipe.name,
                recipe_stage=self.recipe.stage,
                facts=dict(facts or {}),
            )
        )

    def _mode(self, tool: str, args: dict[str, Any], facts: dict[str, Any]) -> Mode:
        """The tool's mode for these args and facts (mode_fn included), before the gate's hard rules."""
        tool_def = self._registry.get(tool)
        if tool_def is None:
            return self._registry.policy.mode_of(tool) or "ask"
        try:
            model = tool_def.args_model.model_validate(args)
        except Exception:
            return self._registry.policy.mode_of(tool) or tool_def.mode
        mode, _ = self._registry.base_mode(tool_def, model, GateContext(facts=facts))
        return mode

    def outcome_of(self, tool: str) -> dict[str, Any] | None:
        """The latest outcome of a tool this run proposed earlier (None if it has not run)."""
        for item in reversed(self.outcomes):
            if item.get("tool") == tool:
                return dict(item)
        return None

    def again(self) -> None:
        """Run this recipe once more after its proposals are handled."""
        self._again = True

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def wants_again(self) -> bool:
        return self._again


async def default_step(ctx: StepContext) -> None:
    """Recipes without a steps module: one model call that may propose the recipe's tools."""
    if ctx.recipe.model == "none":
        ctx.note("no steps and no model: nothing to do")
        return
    out = await ctx.model(DefaultOutput)
    if out.note:
        ctx.note(out.note)
    for item in out.proposals:
        if item.tool not in ctx.recipe.tools:
            ctx.note(f"model proposed {item.tool}, which the recipe does not list; dropped")
            continue
        try:
            ctx.propose(item.tool, item.args, content=item.content)
        except PolicyViolation as exc:
            ctx.note(str(exc))


class RecipeRunner:
    """The graph's RecipeStep, plus task-less runs for scheduled recipes."""

    def __init__(
        self,
        loader: RecipeLoader,
        registry: Registry,
        memory: TaskMemory,
        builder: ContextBuilder,
        model: ModelRunner | None,
        adapters: Mapping[str, Any],
        clock: Clock,
        outbox: Outbox | None = None,
        gate: Gate | None = None,
        state: RecipeStateRepo | None = None,
        paused: Callable[[], Any] | None = None,
        events: EventRepo | None = None,
    ) -> None:
        self._loader = loader
        self._registry = registry
        self._memory = memory
        self._builder = builder
        self._model = model
        self._adapters = ReadOnlyAdapters(adapters, registry)
        self._clock = clock
        self._outbox = outbox
        self._gate = gate
        self._state = state
        self._paused = paused
        self._events = events
        self.last_context: StepContext | None = None  # the harness reads outputs and notes from here

    # ------------------------------------------------------------ inside the task graph

    async def __call__(self, state: GraphState) -> StepResult:
        task_id = state["task_id"]
        memo = dict(state.get("recipe_memo") or {})
        if "queue" not in memo:
            memo = {"queue": await self._initial(task_id, state.get("trigger") or {}), "pins": {}, "turn": 0}
        while memo["queue"]:
            name = memo["queue"][0]
            recipe = self._loader.version(name, memo["pins"].get(name))
            if recipe is None:
                log.info("recipe off or missing; skipped", extra={"recipe": name, "task": task_id})
                memo["queue"] = memo["queue"][1:]
                memo["turn"] = 0
                continue
            memo["pins"][name] = recipe.content_hash
            ctx = await self._context(recipe, task_id, state, memo["turn"])
            ctx.memo = dict((memo.get("data") or {}).get(name) or {})
            await self._run(recipe, ctx)
            memo.setdefault("data", {})[name] = ctx.memo
            if ctx.wants_again and memo["turn"] < 20:
                memo["turn"] += 1
            else:
                memo["queue"] = memo["queue"][1:]
                memo["turn"] = 0
            note = "; ".join(ctx.notes) or None
            return StepResult(proposals=ctx.proposals, done=not memo["queue"], note=note, memo=memo)
        return StepResult(done=True, memo=memo)

    async def _initial(self, task_id: str, trigger: dict[str, Any]) -> list[str]:
        names = list(trigger.get("recipes") or [])
        if not names:
            task = await self._memory.card(task_id)
            names = [task.recipe] if task.recipe else []
        return [n for n in dict.fromkeys(names) if n]

    async def _outcomes(self, state: GraphState) -> list[dict[str, Any]]:
        out = []
        for item in state.get("outcomes") or []:
            entry = dict(item)
            if self._outbox is not None and entry.get("action_id"):
                entry["status"], entry["result"] = await self._outbox.result_of(str(entry["action_id"]))
            out.append(entry)
        return out

    async def _context(self, recipe: LoadedRecipe, task_id: str, state: GraphState, turn: int) -> StepContext:
        trigger_ref = (state.get("context") or {}).get("trigger_ref")
        declared = max(recipe.spec.data_classes, key=LEVEL_ORDER.index)
        packet = await self._builder.build(
            task_id, recipe.spec.context, recipe=recipe.name, trigger_ref=trigger_ref, level=declared
        )
        card = await self._memory.card(task_id)
        return StepContext(
            recipe.spec,
            card,
            state.get("trigger") or {},
            packet,
            self._adapters,
            self._model,
            self._registry,
            self._reader(task_id, recipe),
            turn,
            await self._outcomes(state),
        )

    async def _run(self, recipe: LoadedRecipe, ctx: StepContext) -> None:
        self.last_context = ctx
        if self._state is not None:
            await self._state.bump(recipe.name, "runs")
        await (recipe.steps or default_step)(ctx)
        if self._state is not None and ctx.proposals:
            counter = "would_do" if recipe.spec.stage == "shadow" else "proposals"
            await self._state.bump(recipe.name, counter, len(ctx.proposals))

    def _reader(self, task_id: str | None, recipe: LoadedRecipe) -> Callable[[str, dict[str, Any]], Any]:
        async def read(tool: str, args: dict[str, Any]) -> Any:
            if self._outbox is None or self._gate is None:
                raise PolicyViolation("reading tools needs the gate and the outbox")
            ctx = GateContext(
                task_id=task_id, recipe=recipe.name, recipe_stage=recipe.spec.stage, actor=recipe.name
            )
            if self._gate.check(tool, args, ctx).mode != "silent":
                raise PolicyViolation(f"{tool} is not a read; propose it instead")
            result = await self._outbox.submit(tool, args, ctx, register_inverse=False)
            if result.status != "done":
                raise PolicyViolation(f"{tool} did not run: {result.status} {result.error or ''}".strip())
            return result.result

        return read

    # ------------------------------------------------------------ scheduled, without a task

    def recipes_for_schedule(self, schedule: str) -> list[LoadedRecipe]:
        """recipe:<name> fires that recipe; a named schedule (brief, checkin ...) fires recipes on it."""
        if schedule.startswith("recipe:"):
            recipe = self._loader.get(schedule.split(":", 1)[1])
            return [recipe] if recipe is not None else []
        return [r for r in self._loader.all() if r.spec.trigger.schedule == schedule]

    async def on_schedule(self, job: Job) -> None:
        """TaskRunner schedule listener: run the recipes this schedule fires.

        An intake event that belongs to no task but matches poll/event recipes (inbox triage, refresh
        watch) arrives here as schedule "event" with the recipe names and the event in the payload.
        """
        payload = json.loads(job.payload_json or "{}")
        schedule = str(payload.get("schedule") or "")
        if schedule == EVENT_SCHEDULE:
            trigger = {k: payload.get(k) for k in ("event_id", "source", "kind", "external_id")}
            row = (
                await self._events.get(int(payload["event_id"]))
                if self._events and payload.get("event_id")
                else None
            )
            trigger["event"] = json.loads(row["payload_json"]) if row else {}
            for name in payload.get("recipes") or []:
                recipe = self._loader.get(str(name))
                if recipe is None or recipe.spec.creates_task is not None:
                    continue
                try:
                    await self.run_taskless(recipe, trigger)
                except Exception:
                    log.exception("event recipe failed", extra={"recipe": recipe.name})
            return
        keep = ("schedule", "due_at", "late", "missed_since", "manual")
        trigger = {k: v for k, v in payload.items() if k in keep}
        for recipe in self.recipes_for_schedule(schedule):
            try:
                await self.run_taskless(recipe, trigger)
            except Exception:
                log.exception("scheduled recipe failed", extra={"recipe": recipe.name})

    async def run_taskless(self, recipe: LoadedRecipe, trigger: dict[str, Any]) -> list[Any]:
        """Steps without a task; Silent and Report proposals go straight through the gate and outbox."""
        ctx = StepContext(
            recipe.spec,
            None,
            trigger,
            None,
            self._adapters,
            self._model,
            self._registry,
            self._reader(None, recipe),
            0,
        )
        await self._run(recipe, ctx)
        results: list[Any] = []
        paused = bool(await self._paused()) if self._paused is not None else False
        for proposal in ctx.proposals:
            if self._outbox is None:
                results.append(proposal)
                continue
            gate_ctx = GateContext(
                recipe=recipe.name,
                recipe_stage=recipe.spec.stage,
                actor=recipe.name,
                paused=paused,
                triggered_by_fetched_content=proposal.triggered_by_fetched_content,
                facts=dict(proposal.facts),
            )
            results.append(
                await self._outbox.submit(
                    proposal.tool, proposal.args, gate_ctx, proposal.idempotency_key, content=proposal.content
                )
            )
        return results
