"""The task graph (F07, architecture 4.1): one LangGraph thread per task, thread_id = task id.

    load_context -> run_recipe_step -> propose_action -> gate -+-> await_approval -> execute -+
          ^              ^   |                ^              |   (interrupt)      (outbox)    |
          |              |   +-- finished ----)--------------)--------------------+-> update_memory -> END
          |              +--------------------+-- next proposal / next step <---------------+

Rules the graph keeps:
- Recipes only propose; every proposal goes through the gate and the outbox (golden rule 1).
- An Ask action waits in await_approval on a LangGraph interrupt. The worker parks the job; a decision
  enqueues a resume job and the worker resumes with Command(resume=...). The action's status in the
  database decides what happens next, never the resume value.
- A node re-runs from its start when resumed, so everything before an interrupt is idempotent. After
  an interrupt returns, a node never calls interrupt again: it routes back to itself, so a new run of
  the node gets a fresh interrupt.
- Paused: actions come back 'held'; the graph waits in execute until controls release them.
- Leave mode drops proposals whose purpose it suppresses (chases, caller drafts).
- Caller-facing drafts pass the outbound checks before any card is shown.
- At most max_steps recipe steps per run.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer, interrupt
from pydantic import BaseModel, Field

from agent.adapters.base import Mode
from agent.approvals.service import OPEN, ApprovalService
from agent.core.errors import IllegalTransition
from agent.db.repositories import ActionRepo, MemoryRepo
from agent.ledger.service import Ledger
from agent.policy.gate import GateContext
from agent.privacy.outbound import OutboundChecker, OutboundDraft
from agent.privacy.pseudonymiser import LedgerPeople
from agent.runtime.outbox import Outbox

log = logging.getLogger(__name__)

WAITING = (*OPEN, "held")  # still waiting for the user (or for resume after a pause)
_TERMINAL = ("done", "failed", "blocked", "handed_off", "shadow", "rejected", "expired", "invalidated")


class Proposal(BaseModel):
    """A tool call a recipe step wants. Nothing runs until the gate and, for Ask, the user say so."""

    tool: str
    args: dict[str, Any]
    content: str | None = None  # what the approval card shows; the approval binds to it
    purpose: str = "action"  # caller_draft | chase | note ... (leave mode suppresses some)
    triggered_by_fetched_content: bool = False
    requested_mode: Mode | None = None
    idempotency_key: str | None = None
    draft: OutboundDraft | None = None  # set for outgoing messages: checked before the card
    recipe: str | None = None  # the recipe that proposed it (shadow mode and audit are per recipe)
    recipe_stage: Literal["live", "shadow"] | None = None
    # Facts the step's code looked up for the tool's mode_fn (owned dataset, last error code ...).
    # Set by our own step code from reads, never from fetched text.
    facts: dict[str, Any] = Field(default_factory=dict)


class StepResult(BaseModel):
    proposals: list[Proposal] = Field(default_factory=list)
    done: bool = True  # no more steps after these proposals
    note: str | None = None
    memo: dict[str, Any] | None = None  # the step's own bookkeeping, kept in the checkpoint between steps


class GraphState(TypedDict, total=False):
    task_id: str
    job_kind: str
    trigger: dict[str, Any]
    steps: int
    context: dict[str, Any]
    proposals: list[dict[str, Any]]
    current: dict[str, Any] | None
    action_id: str | None
    action_status: str | None
    outcomes: list[dict[str, Any]]
    finished: bool
    stop_reason: str | None
    recipe_memo: dict[str, Any] | None


RecipeStep = Callable[[GraphState], Awaitable[StepResult]]


async def idle_step(state: GraphState) -> StepResult:
    """Default until the recipe engine (1.9): nothing to propose."""
    return StepResult()


class Controls(Protocol):
    async def paused(self) -> bool: ...
    async def suppressed_purposes(self) -> set[str]: ...


class MemoryHooks(Protocol):
    """MemoryService: record the run's trigger in the event log; rewrite the summary after the run."""

    async def record_trigger(self, task_id: str, trigger: dict[str, Any]) -> int | None: ...
    async def after_run(self, task_id: str) -> Any: ...


class GateContextFn(Protocol):
    async def __call__(self, task_id: str, proposal: Proposal) -> GateContext: ...


class TaskGateContext:
    """Builds the gate context for a task: its participants, the pause flag and the proposal's flags."""

    def __init__(
        self,
        ledger: Ledger,
        people: LedgerPeople | None = None,
        controls: Controls | None = None,
        me: Sequence[str] = (),
        stage: Callable[[str | None], Awaitable[Literal["live", "shadow"]]] | None = None,
    ) -> None:
        self._ledger = ledger
        self._people = people
        self._controls = controls
        self._me = [m.lower() for m in me]
        self._stage = stage

    async def __call__(self, task_id: str, proposal: Proposal) -> GateContext:
        task = await self._ledger.get(task_id)
        participants: frozenset[str] = frozenset()
        if self._people is not None:
            participants = frozenset(
                p.email.lower() for p in await self._people.participants(task_id) if p.email
            )
        recipe = proposal.recipe or task.recipe
        stage = proposal.recipe_stage or (await self._stage(recipe) if self._stage else "live")
        return GateContext(
            task_id=task_id,
            recipe=recipe,
            recipe_stage=stage,
            actor="agent",
            requested_mode=proposal.requested_mode,
            triggered_by_fetched_content=proposal.triggered_by_fetched_content,
            paused=await self._controls.paused() if self._controls else False,
            participants=participants,
            me=self._me[0] if self._me else None,
            facts=dict(proposal.facts),
        )


@dataclass
class GraphDeps:
    ledger: Ledger
    memory: MemoryRepo
    actions: ActionRepo
    outbox: Outbox
    approvals: ApprovalService
    gate_context: GateContextFn
    step: RecipeStep = idle_step
    controls: Controls | None = None
    outbound: OutboundChecker | None = None
    max_steps: int = 12
    memory_hooks: MemoryHooks | None = None


def initial_state(task_id: str, job_kind: str, trigger: dict[str, Any] | None = None) -> GraphState:
    """Input for a new run. Every key is set, so nothing leaks over from the thread's previous run."""
    return GraphState(
        task_id=task_id,
        job_kind=job_kind,
        trigger=trigger or {},
        steps=0,
        context={},
        proposals=[],
        current=None,
        action_id=None,
        action_status=None,
        outcomes=[],
        finished=False,
        stop_reason=None,
        recipe_memo=None,
    )


def build_task_graph(deps: GraphDeps, checkpointer: Checkpointer) -> CompiledStateGraph[Any, Any, Any, Any]:
    async def move(task_id: str, to_state: str, reason: str) -> None:
        """Best-effort ledger move. Never moves a done task; an impossible move is logged, not fatal."""
        task = await deps.ledger.get(task_id)
        if task.state in ("done", to_state):
            return
        try:
            await deps.ledger.move_to(task_id, to_state, "worker", reason)
        except IllegalTransition:
            log.warning("ledger move skipped", extra={"task": task_id, "from": task.state, "to": to_state})

    def outcome(state: GraphState, tool: str, status: str, note: str | None = None) -> list[dict[str, Any]]:
        return [
            *state.get("outcomes", []),
            {"tool": tool, "status": status, "action_id": state.get("action_id"), "note": note},
        ]

    def after_action(state: GraphState) -> str:
        if state.get("proposals"):
            return "propose_action"
        if state.get("finished"):
            return "update_memory"
        return "run_recipe_step"

    # ------------------------------------------------------------ nodes

    async def load_context(state: GraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        task = await deps.ledger.get(task_id)
        if task.state in ("new", "scheduled") or (
            task.state == "waiting" and state.get("job_kind") == "new_event"
        ):
            await move(task_id, "in_progress", "worker picked it up")
            task = await deps.ledger.get(task_id)
        await deps.ledger.touch(task_id)
        trigger_ref = None
        if deps.memory_hooks is not None:
            recorded = await deps.memory_hooks.record_trigger(task_id, state.get("trigger") or {})
            trigger_ref = f"te:{recorded}" if recorded is not None else None
        # Only light, card-level context lives in the checkpoint; recipes build the full packet per step.
        return {
            "context": {
                "task": task.model_dump(mode="json"),
                "trigger_ref": trigger_ref,
            }
        }

    async def run_recipe_step(state: GraphState) -> dict[str, Any]:
        steps = state.get("steps", 0)
        if steps >= deps.max_steps:
            return {"finished": True, "stop_reason": f"stopped after {deps.max_steps} steps"}
        result = await deps.step(state)
        update: dict[str, Any] = {
            "steps": steps + 1,
            "proposals": [p.model_dump(mode="json") for p in result.proposals],
            "finished": result.done,
        }
        if result.memo is not None:
            update["recipe_memo"] = result.memo
        return update

    def after_step(state: GraphState) -> str:
        if state.get("proposals"):
            return "propose_action"
        if state.get("finished"):
            return "update_memory"
        return "run_recipe_step"

    async def propose_action(state: GraphState) -> dict[str, Any]:
        queue = list(state.get("proposals", []))
        raw = queue.pop(0)
        proposal = Proposal.model_validate(raw)
        update: dict[str, Any] = {
            "proposals": queue,
            "current": None,
            "action_id": None,
            "action_status": None,
        }
        if deps.controls is not None and proposal.purpose in await deps.controls.suppressed_purposes():
            update["outcomes"] = outcome(
                state, proposal.tool, "suppressed", f"{proposal.purpose}: leave mode"
            )
            return update
        if proposal.draft is not None and deps.outbound is not None:
            report = deps.outbound.check(proposal.draft)
            if not report.ok:
                status = "handed_off" if report.hand_off else "blocked"
                update["outcomes"] = outcome(state, proposal.tool, status, report.summary())
                return update
        update["current"] = raw
        return update

    def after_propose(state: GraphState) -> str:
        return "gate" if state.get("current") else after_action(state)

    async def gate(state: GraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        proposal = Proposal.model_validate(state["current"])
        ctx = await deps.gate_context(task_id, proposal)
        result = await deps.approvals.create(
            task_id, proposal.tool, proposal.args, proposal.content, ctx, proposal.idempotency_key
        )
        status = result.status
        if status == "held" and result.decision is not None and result.decision.mode == "ask":
            status = "held_ask"  # paused when proposed: it still needs an approval after resume
        update: dict[str, Any] = {"action_id": result.action_id, "action_status": status}
        if status in _TERMINAL:
            note = result.error or ("; ".join(result.decision.reasons) if result.decision else None)
            update["outcomes"] = outcome(
                {**state, "action_id": result.action_id}, proposal.tool, result.status, note
            )
            update["current"] = None
        return update

    def after_gate(state: GraphState) -> str:
        status = state.get("action_status")
        if status in (*OPEN, "held_ask"):
            return "await_approval"
        if status in ("held", "approved", "ready", "executing"):
            return "execute"
        return after_action(state)

    async def await_approval(state: GraphState) -> dict[str, Any]:
        task_id, action_id = state["task_id"], state["action_id"]
        assert action_id is not None
        await move(task_id, "needs_you", "approval pending")
        if deps.memory_hooks is not None:
            # A task can wait days for a decision: bring the summary up to date before parking. On
            # resume this re-runs and finds nothing new, so the model is not asked twice.
            await deps.memory_hooks.after_run(task_id)
        interrupt({"kind": "approval", "action_id": action_id})
        action = await deps.actions.get(action_id)
        status = action.status if action else "invalidated"
        update: dict[str, Any] = {"action_status": status}
        if status == "approved":
            await move(task_id, "in_progress", "approved")
        elif status not in WAITING:
            tool = action.tool if action else "?"
            update["outcomes"] = outcome(state, tool, status, action.reason if action else None)
            update["current"] = None
            update["proposals"] = []  # context changed or the user said no: re-plan on the next run
            update["finished"] = True
            if status in ("rejected", "invalidated"):
                await move(task_id, "in_progress", status)
        return update

    def after_await(state: GraphState) -> str:
        status = state.get("action_status")
        if status == "approved":
            return "execute"
        if status in WAITING:
            return "await_approval"  # spurious resume, or edited: wait again with a fresh interrupt
        return "update_memory"

    async def execute(state: GraphState) -> dict[str, Any]:
        task_id, action_id = state["task_id"], state["action_id"]
        assert action_id is not None
        action = await deps.actions.get(action_id)
        if action is None:
            return {"action_status": "failed", "current": None}
        if action.status == "held":
            interrupt({"kind": "held", "action_id": action_id})
            return {"action_status": "held_released"}
        if action.status not in ("approved", "ready", "executing", "done"):
            # Invalidated or expired while paused: record it, run nothing.
            return {
                "action_status": action.status,
                "current": None,
                "outcomes": outcome(state, action.tool, action.status, action.reason),
            }
        task = await deps.ledger.get(task_id)
        if task.state == "done" and action.status == "approved":
            # Closed by the user after approving: nothing goes out for a closed task.
            await deps.approvals.invalidate_for_task(task_id, actor="system", reason="the task was closed")
            return {
                "action_status": "invalidated",
                "current": None,
                "outcomes": outcome(state, action.tool, "invalidated", "task closed"),
            }
        proposal = Proposal.model_validate(state["current"]) if state.get("current") else None
        ctx = await deps.gate_context(task_id, proposal or Proposal(tool=action.tool, args={}))
        result = await deps.outbox.execute(action_id, ctx)
        await deps.ledger.touch(task_id)
        if result.status == "held":
            return {"action_status": "held"}
        note = result.error
        if result.status == "failed":
            await move(task_id, "needs_you", "action failed")
        return {
            "action_status": result.status,
            "current": None,
            "outcomes": outcome(state, action.tool, result.status, note),
        }

    def after_execute(state: GraphState) -> str:
        if state.get("action_status") in ("held", "held_released"):
            return "execute"
        return after_action(state)

    async def update_memory(state: GraphState) -> dict[str, Any]:
        task_id = state["task_id"]
        outcomes = state.get("outcomes", [])
        lines = [
            f"{o['tool']}: {o['status']}" + (f" ({o['note']})" if o.get("note") else "") for o in outcomes
        ]
        if state.get("stop_reason"):
            lines.append(str(state["stop_reason"]))
        if lines:
            await deps.memory.add_task_event(
                task_id, actor="agent", channel="graph", text_pseudonymised="; ".join(lines)
            )
        next_action, needs_you = _next_action(outcomes, state.get("stop_reason"))
        if next_action:
            await deps.ledger.set_next_action(task_id, next_action)
        if needs_you:
            await move(task_id, "needs_you", next_action or "needs you")
        if deps.memory_hooks is not None:
            await deps.memory_hooks.after_run(task_id)
        return {"current": None, "action_id": None}

    graph: StateGraph[GraphState, Any, GraphState, GraphState] = StateGraph(GraphState)
    graph.add_node("load_context", load_context)
    graph.add_node("run_recipe_step", run_recipe_step)
    graph.add_node("propose_action", propose_action)
    graph.add_node("gate", gate)
    graph.add_node("await_approval", await_approval)
    graph.add_node("execute", execute)
    graph.add_node("update_memory", update_memory)
    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "run_recipe_step")
    graph.add_conditional_edges("run_recipe_step", after_step)
    graph.add_conditional_edges("propose_action", after_propose)
    graph.add_conditional_edges("gate", after_gate)
    graph.add_conditional_edges("await_approval", after_await)
    graph.add_conditional_edges("execute", after_execute)
    graph.add_edge("update_memory", END)
    return graph.compile(checkpointer=checkpointer)


def _next_action(outcomes: list[dict[str, Any]], stop_reason: str | None) -> tuple[str | None, bool]:
    """(next action text for the task card, whether the task needs the user)."""
    statuses = {o["status"] for o in outcomes}
    if "expired" in statuses:
        return "Approval expired after 24 hours; the draft is kept. Ask for it again if still needed.", True
    if "handed_off" in statuses:
        return "Hand-off prepared: finish this step yourself.", True
    if "failed" in statuses:
        return "An action failed; check the activity log.", True
    if "blocked" in statuses:
        return "A proposed action was blocked; check the activity log.", True
    if stop_reason:
        return f"Run {stop_reason}; check the task.", True
    if "rejected" in statuses:
        return "You rejected the proposed action.", False
    if "done" in statuses:
        return "Done; see the activity log.", False
    return None, False
