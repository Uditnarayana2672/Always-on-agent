"""Everything the task graph needs, wired to the fakes: coordinator -> jobs -> runner -> graph ->
approvals -> outbox -> FakeTeams. Jobs are run one at a time by drain(), like one worker would."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from agent.adapters.base import MailMessage, Person
from agent.adapters.fakes.world import FakeWorld
from agent.approvals.service import ApprovalService
from agent.channels.base import MemoryChannel
from agent.core.clock import FrozenClock
from agent.core.config import ClockConfig, PrivacyConfig
from agent.db.repositories import (
    ActionRepo,
    AuditRepo,
    ClockRepo,
    DirectoryRepo,
    EventRepo,
    JobRepo,
    MemoryRepo,
    TaskRepo,
    UndoRepo,
)
from agent.graphs.runner import TaskRunner
from agent.graphs.task_graph import (
    GraphDeps,
    GraphState,
    Proposal,
    StepResult,
    TaskGateContext,
    build_task_graph,
)
from agent.ledger.service import Ledger
from agent.policy.gate import Gate
from agent.privacy.outbound import OutboundChecker, OutboundDraft
from agent.privacy.pseudonymiser import KnownPerson, LedgerPeople
from agent.runtime.coordinator import Coordinator
from agent.runtime.outbox import Outbox
from agent.runtime.workers import HandlerRegistry, JobContext
from tests.policy import tools as t
from tests.policy.tools import CALLER, ME

TASK = "INC30114872"
CHAT = "19:incident-chat"
CONV = "CONV-INC30114872"


class Controls:
    def __init__(self) -> None:
        self.is_paused = False
        self.purposes: set[str] = set()

    async def paused(self) -> bool:
        return self.is_paused

    async def suppressed_purposes(self) -> set[str]:
        return set(self.purposes)


class DraftStep:
    """A recipe step that drafts one caller reply per run (v1, v2, ...)."""

    def __init__(self) -> None:
        self.runs = 0
        self.text = "Hello, the login works again."
        self.extra: dict[str, Any] = {}
        self.endless = False

    async def __call__(self, state: GraphState) -> StepResult:
        if self.endless:
            return StepResult(done=False)
        if state.get("steps", 0) > 0:
            return StepResult()
        self.runs += 1
        body = (
            f"{self.text} (v{self.runs})" if "{fixed}" not in self.text else self.text.replace("{fixed}", "")
        )
        to = self.extra.get("to", [CALLER])
        return StepResult(
            proposals=[
                Proposal(
                    tool="post_chat",
                    args={"chat_id": CHAT, "body": body, "recipients": to},
                    content=body,
                    purpose=self.extra.get("purpose", "caller_draft"),
                    draft=OutboundDraft(body=body, to=to),
                )
            ]
        )


class Harness:
    def __init__(self, engine: AsyncEngine, clock: FrozenClock, saver: Any) -> None:
        self.clock = clock
        self.world = FakeWorld(clock)
        self.registry = t.build()
        self.tasks = TaskRepo(engine, clock)
        self.memory = MemoryRepo(engine, clock)
        self.jobs = JobRepo(engine, clock)
        self.actions = ActionRepo(engine, clock)
        self.audit = AuditRepo(engine, clock)
        self.ledger = Ledger(
            self.tasks,
            self.memory,
            ClockRepo(engine, clock),
            clock,
            ClockConfig(start="opened", pause_on_hold=False, thresholds_hours=[12, 18, 22]),
        )
        self.people = LedgerPeople(self.memory, DirectoryRepo(engine, clock))
        self.gate = Gate(self.registry, t.COMPANY, t.GROUPS)
        self.outbox = Outbox(
            self.gate,
            self.registry,
            self.actions,
            self.audit,
            UndoRepo(engine, clock),
            {"outlook": self.world.mail, "teams": self.world.teams, "powerbi": self.world.powerbi},
            clock,
        )
        self.coordinator = Coordinator(
            self.tasks, EventRepo(engine, clock), self.jobs, self.memory, clock, create_rules=[], me=[ME]
        )
        self.slack = MemoryChannel("slack")
        self.console = MemoryChannel("console")
        self.approvals = ApprovalService(
            self.outbox,
            self.actions,
            self.audit,
            self.registry,
            clock,
            self.coordinator.enqueue_resume,
            channels=[self.slack, self.console],
            tasks=self.tasks,
        )
        self.coordinator._approvals = self.approvals
        self.controls = Controls()
        self.step = DraftStep()
        self.deps = GraphDeps(
            ledger=self.ledger,
            memory=self.memory,
            actions=self.actions,
            outbox=self.outbox,
            approvals=self.approvals,
            gate_context=TaskGateContext(self.ledger, self.people, self.controls, me=[ME]),
            step=self.step,
            controls=self.controls,
            outbound=OutboundChecker(PrivacyConfig(), t.COMPANY),
            max_steps=4,
        )
        self.graph = build_task_graph(self.deps, saver)
        self.runner = TaskRunner(
            self.graph,
            self.actions,
            self.tasks,
            max_steps=4,
            schedule_handlers={"expire_approvals": self._sweep},
        )
        self.handlers = HandlerRegistry()
        self.runner.register(self.handlers)

    async def _sweep(self, job: Any) -> None:
        await self.approvals.expire()

    # ------------------------------------------------------------ driving

    async def caller_mail(self, mid: str, subject: str = f"{TASK} login fails", sender: str = CALLER) -> None:
        mail = MailMessage(
            id=mid,
            conversation_id=CONV,
            subject=subject,
            sender=Person(name="Sophie Laurent", address=sender),
            received_at=self.clock.now_utc(),
            body="Please help.",
        )
        await self.coordinator.ingest(mail, "mail")
        if await self.tasks.get(TASK) is not None:
            await self.people.remember(
                TASK, [KnownPerson(name="Sophie Laurent", email=CALLER, role="caller")]
            )

    async def drain(self, limit: int = 30) -> list[tuple[str, str]]:
        """Run queued jobs one by one until none is left. A handler error re-queues the job at once."""
        ran: list[tuple[str, str]] = []
        for _ in range(limit):
            job = await self.jobs.claim("w1")
            if job is None:
                return ran
            handler = self.handlers.get(job.kind)
            assert handler is not None
            try:
                outcome = await handler(job, JobContext(job, "w1", asyncio.Event()))
            except Exception as exc:
                await self.jobs.fail(job.id, repr(exc), retry_at=self.clock.now_utc(), worker_id="w1")
                ran.append((job.kind, "error"))
                continue
            if outcome == "parked":
                await self.jobs.park(job.id, "w1")
            else:
                await self.jobs.complete(job.id, "w1")
            ran.append((job.kind, outcome))
        raise AssertionError(f"queue did not drain: {ran}")

    async def only_action(self) -> Any:
        rows = await self.actions.open_for_task(
            TASK,
            (
                "proposed",
                "edited",
                "approved",
                "held",
                "done",
                "invalidated",
                "expired",
                "rejected",
                "failed",
            ),
        )
        assert len(rows) >= 1
        return rows[-1]

    async def state(self) -> str:
        task = await self.tasks.get(TASK)
        assert task is not None
        return task.state

    def posts(self) -> list[str]:
        return [w.args["body"] for w in self.world.teams.writes_to("post")]


T0 = datetime(2026, 3, 25, 4, 30, tzinfo=UTC)


def checkpoints(tmp_path: Path) -> Path:
    return tmp_path / "checkpoints.db"
