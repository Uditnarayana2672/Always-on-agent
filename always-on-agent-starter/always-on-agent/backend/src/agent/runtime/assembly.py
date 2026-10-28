"""Runtime assembly (F01): every part of agentd wired once, in the startup order of the F01 diagram.

    Runtime.build(...)  config + database (migrations) + repositories + ledger + registry (adapter,
                        memory and recipe-view tools) + gate + outbox + approvals + memory + recipes
                        + task graph + worker pool + scheduler + controls + pollers + notifications
    await rt.start()    recipes loaded, catch-up, scheduler, workers (the API and Slack are started
                        by their own runners on top of this)
    await rt.stop()     stop intake, drain the workers to a checkpoint, close the database

The same assembly runs real adapters (agentd run) and fakes (agentd run --fake, tests): only the
adapters, the model and the clock passed in differ.
"""

import asyncio
import contextlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.adapters.tools import register_all
from agent.approvals.service import ApprovalService
from agent.channels.base import ApprovalChannel, MemoryChannel
from agent.channels.notifier import Notification, Notifier
from agent.channels.router import NotificationRouter, NotifyChannel
from agent.core.clock import Clock
from agent.core.config import Config
from agent.db.engine import create_engine_async
from agent.db.migrate import upgrade_head
from agent.db.repositories import (
    ActionRepo,
    AuditRepo,
    ClockRepo,
    ContextLogRepo,
    DirectoryRepo,
    EventRepo,
    HealthRepo,
    JobRepo,
    KvRepo,
    LessonRepo,
    MemoryRepo,
    RecipeStateRepo,
    RetentionRepo,
    ScheduleRunRepo,
    TaskRepo,
    TokenCipher,
    TokenMapRepo,
    UndoRepo,
)
from agent.db.search import Fts5SearchIndex
from agent.graphs.runner import TaskRunner
from agent.graphs.task_graph import GraphDeps, TaskGateContext, build_task_graph
from agent.ledger.service import Ledger
from agent.memory.context_builder import ContextBuilder
from agent.memory.knowledge import Knowledge
from agent.memory.personal import PersonalMemory
from agent.memory.retention import RetentionJob
from agent.memory.service import MemoryService
from agent.memory.summary import SummaryUpdater
from agent.memory.task_memory import TaskMemory
from agent.model.gateway import ModelRunner
from agent.policy.gate import Gate
from agent.policy.modes import load_policy
from agent.policy.registry import Registry
from agent.policy.undo import UndoService
from agent.privacy.outbound import OutboundChecker
from agent.privacy.pseudonymiser import LedgerPeople, Pseudonymiser
from agent.recipes.loader import RecipeLoader
from agent.recipes.runner import RecipeRunner
from agent.recipes.triggers import RecipeCreateRule, RecipeMatcher, schedule_specs
from agent.recipes.views import AgentViews, ViewDeps, register_view_tools
from agent.runtime.controls import Controls, Resetter
from agent.runtime.coordinator import Coordinator
from agent.runtime.outbox import Outbox
from agent.runtime.pollers import Pollers
from agent.runtime.scheduler import Scheduler
from agent.runtime.settings import apply_overrides, load_overrides, policy_with
from agent.runtime.workers import HandlerRegistry, NotificationRequest, WorkerPool

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    config: Config
    clock: Clock
    engine: AsyncEngine
    adapters: dict[str, Any]
    registry: Registry
    gate: Gate
    outbox: Outbox
    approvals: ApprovalService
    console_channel: MemoryChannel
    coordinator: Coordinator
    ledger: Ledger
    tasks: TaskRepo
    memory: MemoryRepo
    task_memory: TaskMemory
    knowledge: Knowledge
    personal: PersonalMemory
    actions: ActionRepo
    audit: AuditRepo
    undo_repo: UndoRepo
    undo: UndoService
    events: EventRepo
    jobs: JobRepo
    kv: KvRepo
    health: HealthRepo
    lessons: LessonRepo
    directory: DirectoryRepo
    recipes_state: RecipeStateRepo
    loader: RecipeLoader
    recipe_runner: RecipeRunner
    task_runner: TaskRunner
    pool: WorkerPool
    scheduler: Scheduler
    controls: Controls
    pollers: Pollers
    notifier: Notifier
    router: NotificationRouter
    views: AgentViews
    retention: RetentionJob
    checkpointer: Any
    pseudo: Pseudonymiser
    people: LedgerPeople
    token_map: TokenMapRepo
    schedule_runs: ScheduleRunRepo
    _exit: contextlib.AsyncExitStack
    listeners: list[Any] = field(default_factory=list)  # SSE hub and friends: called on changes
    started: bool = False

    # ------------------------------------------------------------ build

    @classmethod
    async def build(
        cls,
        config: Config,
        clock: Clock,
        adapters: Mapping[str, Any],
        *,
        db_url: str | None = None,
        checkpoints: str | Path | None = None,
        model: ModelRunner | None = None,
        cipher: TokenCipher | None = None,
        notify_channels: Sequence[NotifyChannel] = (),
        approval_channels: Sequence[ApprovalChannel] = (),
        personal_dir: str | Path | None = None,
        recipes_dir: str | Path | None = None,
        console_url: str | None = None,
    ) -> "Runtime":
        url = db_url or config.database.url
        await asyncio.to_thread(upgrade_head, url)
        engine = create_engine_async(url)
        exit_stack = contextlib.AsyncExitStack()
        try:
            return await cls._build(
                config,
                clock,
                dict(adapters),
                engine,
                url,
                checkpoints or config.database.checkpoints_path,
                model,
                cipher or TokenCipher(Fernet.generate_key()),
                notify_channels,
                approval_channels,
                personal_dir,
                recipes_dir,
                console_url or f"http://{config.console.host}:{config.console.port}",
                exit_stack,
            )
        except BaseException:
            await exit_stack.aclose()
            await engine.dispose()
            raise

    @classmethod
    async def _build(
        cls,
        config: Config,
        clock: Clock,
        adapters: dict[str, Any],
        engine: AsyncEngine,
        url: str,
        checkpoints: str | Path,
        model: ModelRunner | None,
        cipher: TokenCipher,
        notify_channels: Sequence[NotifyChannel],
        approval_channels: Sequence[ApprovalChannel],
        personal_dir: str | Path | None,
        recipes_dir: str | Path | None,
        console_url: str,
        exit_stack: contextlib.AsyncExitStack,
    ) -> "Runtime":
        from agent.graphs.runner import open_checkpointer

        tasks, memory = TaskRepo(engine, clock), MemoryRepo(engine, clock)
        events, jobs = EventRepo(engine, clock), JobRepo(engine, clock, config.workers.lock_ttl_minutes)
        actions, audit, undo_repo = (
            ActionRepo(engine, clock),
            AuditRepo(engine, clock),
            UndoRepo(engine, clock),
        )
        kv, health = KvRepo(engine, clock), HealthRepo(engine, clock)
        directory, lessons = DirectoryRepo(engine, clock), LessonRepo(engine, clock)
        recipes_state = RecipeStateRepo(engine, clock)
        overrides, policy_modes = await load_overrides(kv)
        if overrides:
            config = apply_overrides(config, overrides)
            _reconfigure_clock(clock, config)
        ledger = Ledger(tasks, memory, ClockRepo(engine, clock), clock, config.clock)

        registry = Registry(policy_with(load_policy(), policy_modes))
        register_all(registry, adapters)
        level = config.data_rule.level
        gate = Gate(registry, config.company_domains, config.allowed_groups, level)
        outbox = Outbox(gate, registry, actions, audit, undo_repo, adapters, clock)
        undo = UndoService(registry, outbox, audit, undo_repo, adapters)

        people = LedgerPeople(memory, directory, cipher)
        token_map = TokenMapRepo(engine, clock, cipher)
        pseudo = Pseudonymiser(token_map, people, config.privacy)
        task_memory = TaskMemory(
            memory, ledger, clock, events, actions, pseudo, config.retention.raw_text_days
        )
        knowledge = Knowledge(Fts5SearchIndex(engine), clock, directory, lessons)
        personal = PersonalMemory(personal_dir or config.paths.memory_dir or None)
        context_log = ContextLogRepo(engine, clock)
        builder = ContextBuilder(task_memory, knowledge, personal, level, context_log)
        summaries = SummaryUpdater(model, task_memory, level) if model is not None else None
        memory_service = MemoryService(task_memory, builder, summaries, knowledge)
        memory_service.register_tools(registry)

        router = NotificationRouter(notify_channels, config.notifications.primary)
        notifier = Notifier(config.notifications, clock, kv, router)
        views = AgentViews(
            ViewDeps(
                config=config,
                clock=clock,
                tasks=tasks,
                ledger=ledger,
                memory=memory,
                jobs=jobs,
                actions=actions,
                audit=audit,
                undo=undo_repo,
                events=events,
                health=health,
                kv=kv,
                lessons=lessons,
                adapters=adapters,
                notifier=notifier,
                console_url=console_url,
                people=people,
            )
        )
        register_view_tools(registry, views)

        loader = RecipeLoader(
            registry,
            clock,
            recipes_dir or config.paths.recipes_dir or None,
            poll_minutes=config.schedule.poll_minutes.model_dump(),
            state=recipes_state,
        )
        console_channel = MemoryChannel("console")
        coordinator = Coordinator(
            tasks,
            events,
            jobs,
            memory,
            clock,
            create_rules=[RecipeCreateRule(loader, tasks)],
            me=config.me,
            recipe_matcher=RecipeMatcher(loader),
        )
        approvals = ApprovalService(
            outbox,
            actions,
            audit,
            registry,
            clock,
            coordinator.enqueue_resume,
            channels=[console_channel, *approval_channels],
            tasks=tasks,
        )
        coordinator._approvals = approvals

        schedule_runs = ScheduleRunRepo(engine, clock)
        scheduler = Scheduler(config, clock, coordinator.ingest, schedule_runs, kv, database_url=url)
        checkpointer = await exit_stack.enter_async_context(open_checkpointer(checkpoints))
        resetter = None
        controls = Controls(kv, clock, audit, scheduler, approvals, resetter)
        scheduler.attach_controls(controls)

        async def stage(recipe: str | None) -> Any:
            loaded = loader.get(recipe) if recipe else None
            return loaded.spec.stage if loaded is not None else "live"

        recipe_runner = RecipeRunner(
            loader,
            registry,
            task_memory,
            builder,
            model,
            adapters,
            clock,
            outbox=outbox,
            gate=gate,
            state=recipes_state,
            paused=controls.paused,
            events=events,
        )
        deps = GraphDeps(
            ledger=ledger,
            memory=memory,
            actions=actions,
            outbox=outbox,
            approvals=approvals,
            gate_context=TaskGateContext(ledger, people, controls, me=config.me, stage=stage),
            step=recipe_runner,
            controls=controls,
            outbound=OutboundChecker(config.privacy, config.company_domains),
            max_steps=config.workers.max_steps_per_run,
            memory_hooks=memory_service,
        )
        graph = build_task_graph(deps, checkpointer)
        retention = RetentionJob(config.retention, clock, RetentionRepo(engine, clock), context_log)
        pollers = Pollers(
            coordinator.ingest,
            kv,
            clock,
            mail=adapters.get("outlook"),
            teams=adapters.get("teams"),
            powerbi=adapters.get("powerbi"),
            health=health,
            watch_datasets=config.powerbi.watch.datasets,
        )

        async def sweep(job: Any) -> None:
            await approvals.expire()

        async def purge(job: Any) -> None:
            await retention.run()

        handlers_by_schedule = {"expire_approvals": sweep, "retention": purge, **pollers.handlers()}
        task_runner = TaskRunner(
            graph,
            actions,
            tasks,
            max_steps=config.workers.max_steps_per_run,
            schedule_handlers=handlers_by_schedule,
            schedule_listeners=[recipe_runner.on_schedule],
        )
        handlers = HandlerRegistry()
        task_runner.register(handlers)
        controls._resetter = Resetter(engine, actions, clock, forget_thread=task_runner.forget)

        async def worker_notify(request: NotificationRequest) -> None:
            await notifier.notify(
                Notification(kind="job_failed", text=request.text, task_id=request.task_id, urgent=True)
            )

        pool = WorkerPool(jobs, handlers, clock, config.workers, ledger=ledger, notify=worker_notify)
        return cls(
            config=config,
            clock=clock,
            engine=engine,
            adapters=adapters,
            registry=registry,
            gate=gate,
            outbox=outbox,
            approvals=approvals,
            console_channel=console_channel,
            coordinator=coordinator,
            ledger=ledger,
            tasks=tasks,
            memory=memory,
            task_memory=task_memory,
            knowledge=knowledge,
            personal=personal,
            actions=actions,
            audit=audit,
            undo_repo=undo_repo,
            undo=undo,
            events=events,
            jobs=jobs,
            kv=kv,
            health=health,
            lessons=lessons,
            directory=directory,
            recipes_state=recipes_state,
            loader=loader,
            recipe_runner=recipe_runner,
            task_runner=task_runner,
            pool=pool,
            scheduler=scheduler,
            controls=controls,
            pollers=pollers,
            notifier=notifier,
            router=router,
            views=views,
            retention=retention,
            checkpointer=checkpointer,
            pseudo=pseudo,
            people=people,
            token_map=token_map,
            schedule_runs=schedule_runs,
            _exit=exit_stack,
        )

    # ------------------------------------------------------------ lifecycle

    async def load_recipes(self) -> Any:
        report = await self.loader.load_all()
        self.scheduler.set_recipe_specs(schedule_specs(self.loader.all(), self.config))
        for name, error in report.failed.items():
            log.warning("recipe turned off", extra={"recipe": name, "error": error[:200]})
        return report

    async def start(self, *, scheduler: bool = True, workers: bool = True) -> None:
        """Recipes, then catch-up and the scheduler, then the workers (F01 order)."""
        await self.load_recipes()
        if scheduler:
            await self.scheduler.start()
        if workers:
            await self.pool.start()
        self.started = True

    async def stop(self) -> None:
        """Stop intake first, let running jobs reach a checkpoint, then close the database."""
        with contextlib.suppress(Exception):
            await self.scheduler.stop()
        with contextlib.suppress(Exception):
            await self.pool.stop()
        await self._exit.aclose()
        await self.engine.dispose()
        self.started = False

    async def drain(self, limit: int = 200, worker_id: str = "inline") -> int:
        """Run queued jobs inline until none is left (tests, the fake demo). Returns how many ran."""
        import asyncio as _asyncio

        from agent.runtime.workers import JobContext

        ran = 0
        for _ in range(limit):
            job = await self.jobs.claim(worker_id)
            if job is None:
                break
            handler = self.pool._handlers.get(job.kind)
            if handler is None:
                await self.jobs.fail(job.id, "no handler", retry_at=None, worker_id=worker_id)
                continue
            try:
                outcome = await handler(job, JobContext(job, worker_id, _asyncio.Event()))
            except Exception as exc:
                log.exception("inline job failed", extra={"job": job.id})
                await self.jobs.fail(job.id, repr(exc)[:500], retry_at=None, worker_id=worker_id)
                continue
            if outcome == "parked":
                await self.jobs.park(job.id, worker_id)
            else:
                await self.jobs.complete(job.id, worker_id)
            ran += 1
        return ran

    def reconfigure(self, config: Config) -> None:
        """Apply changed console settings to the running parts (schedules, hours, push cap, models)."""
        self.config = config
        _reconfigure_clock(self.clock, config)
        self.scheduler.reconfigure(config)
        self.notifier.set_cap(config.notifications.max_pushes_per_day)
        self.views.d.config = config
        model = self.recipe_runner._model
        if model is not None and hasattr(model, "_config"):
            model._config = config

    async def notify_listeners(self, kind: str, data: dict[str, Any]) -> None:
        for listener in list(self.listeners):
            try:
                await listener(kind, data)
            except Exception:
                log.exception("listener failed", extra={"kind": kind})


def _reconfigure_clock(clock: Clock, config: Config) -> None:
    reconfigure = getattr(clock, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(config)
