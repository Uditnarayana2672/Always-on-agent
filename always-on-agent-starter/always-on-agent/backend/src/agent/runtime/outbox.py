"""The outbox (F06, F07, F13): the only place adapter write methods are called.

submit() runs the gate, audits the decision and stores an action request. Silent and Report actions
run at once; Ask actions wait for an approval (1.5); Hand off, shadow, held and blocked ones never
run here.

execute() is exactly-once per idempotency key:
- the action is claimed by compare-and-set (ready/approved -> executing), so two callers cannot both
  run it;
- the gate is checked again, so a policy tightened or a pause set since the proposal wins;
- an action found still 'executing' means the process died mid-call. Before running it again the
  outbox asks the adapter whether a side effect with this key already exists (Sent Items header,
  chat tag). An Ask tool with no way to check is not re-sent: it fails as "check by hand".
- Report actions register their inverse for undo.
"""

import inspect
import json
import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from agent.core.clock import Clock
from agent.core.errors import ApprovalConflict, PolicyViolation
from agent.core.ids import new_ulid
from agent.db.repositories import ActionRepo, ActionRequest, AuditRepo, UndoRepo
from agent.policy.audit import Auditor
from agent.policy.gate import Decision, Gate, GateContext, content_hash
from agent.policy.modes import RANK
from agent.policy.registry import Registry, ToolDef

log = logging.getLogger(__name__)

APPROVAL_TTL = timedelta(hours=24)
REPROPOSABLE = ("failed", "expired", "invalidated")


class OutboxResult(BaseModel):
    action_id: str
    status: str  # ready | proposed | handed_off | shadow | held | blocked | done | failed
    decision: Decision | None = None
    result: Any = None
    error: str | None = None
    audit_id: int | None = None  # the execution's audit row (what undo refers to)


def _initial_status(decision: Decision) -> str:
    if decision.mode == "blocked":
        return "blocked"
    if decision.mode == "hand_off":
        return "handed_off"
    if decision.record_only:
        return "shadow"
    if decision.held:
        return "held"
    if decision.mode == "ask":
        return "proposed"
    return "ready"


class Outbox:
    def __init__(
        self,
        gate: Gate,
        registry: Registry,
        actions: ActionRepo,
        audit: AuditRepo,
        undo: UndoRepo,
        adapters: Mapping[str, Any],
        clock: Clock,
    ) -> None:
        self._gate = gate
        self._registry = registry
        self._actions = actions
        self._auditor = Auditor(audit)
        self._undo = undo
        self._adapters = adapters
        self._clock = clock

    # ------------------------------------------------------------ submit

    async def submit(
        self,
        tool: str,
        args: Mapping[str, Any] | BaseModel,
        ctx: GateContext,
        idempotency_key: str | None = None,
        register_inverse: bool = True,
        content: str | None = None,
    ) -> OutboxResult:
        """content is what an approval card shows (a draft body); the approval binds to it and the args."""
        decision = self._gate.check(tool, args, ctx)
        bound = content_hash(decision.args_digest, content)
        key = idempotency_key or f"{ctx.task_id or '-'}:{tool}:{bound}"

        existing = await self._actions.by_idempotency_key(key)
        if existing is not None:
            return await self._resubmit(existing, decision, ctx, register_inverse)

        status = _initial_status(decision)
        action_id = new_ulid()
        await self._actions.create(
            action_id=action_id,
            task_id=ctx.task_id,
            tool=tool,
            args=decision.args,
            mode=decision.mode,
            status=status,
            content_hash=bound,
            idempotency_key=key,
            expires_at=self._clock.now_utc() + APPROVAL_TTL if status == "proposed" else None,
            args_digest=decision.args_digest,
            content=content,
        )
        await self._auditor.decision(decision, ctx, status, action_id)
        if decision.flagged:
            log.warning("tool call triggered by fetched content", extra={"tool": tool, "task": ctx.task_id})
        if status == "ready":
            return await self.execute(action_id, ctx, register_inverse=register_inverse)
        return OutboxResult(action_id=action_id, status=status, decision=decision)

    async def _resubmit(
        self, existing: ActionRequest, decision: Decision, ctx: GateContext, register_inverse: bool
    ) -> OutboxResult:
        """Same key again (graph re-run, duplicate event): never a second effect."""
        if existing.status == "failed" and existing.mode in ("silent", "report") and decision.executable_now:
            await self._actions.transition(existing.id, "failed", "ready")
            return await self.execute(existing.id, ctx, register_inverse=register_inverse)
        if existing.status == "held" and not decision.held:
            # Paused when first proposed; resumed since.
            if decision.executable_now and existing.mode in ("silent", "report"):
                await self._actions.transition(existing.id, "held", "ready")
                return await self.execute(existing.id, ctx, register_inverse=register_inverse)
            if decision.needs_approval:
                await self._actions.transition(existing.id, "held", "proposed")
                return OutboxResult(action_id=existing.id, status="proposed", decision=decision)
        # 4.3: a failed, expired or invalidated proposal comes back only with a fresh approval.
        if (
            existing.status in REPROPOSABLE
            and existing.mode == "ask"
            and decision.needs_approval
            and not decision.held
            and await self._actions.decide(
                existing.id,
                (existing.status,),
                "proposed",
                expires_at=self._clock.now_utc() + APPROVAL_TTL,
                decided_at=None,
                decided_by=None,
                reason=None,
                surfaces_json="{}",
            )
        ):
            return OutboxResult(action_id=existing.id, status="proposed", decision=decision)
        if existing.status in ("ready", "approved", "executing"):
            return await self.execute(existing.id, ctx, register_inverse=register_inverse)
        return OutboxResult(
            action_id=existing.id,
            status=existing.status,
            decision=decision,
            result=_stored_result(existing),
        )

    # ------------------------------------------------------------ execute

    async def execute(self, action_id: str, ctx: GateContext, register_inverse: bool = True) -> OutboxResult:
        action = await self._actions.get(action_id)
        if action is None:
            raise KeyError(action_id)
        if action.status == "done":
            return OutboxResult(action_id=action_id, status="done", result=_stored_result(action))
        tool_def = self._registry.get(action.tool)
        if tool_def is None:
            raise PolicyViolation(f"{action.tool} is not a registered tool")
        args = json.loads(action.args_json)

        recovering = action.status == "executing"
        if not recovering and action.status not in ("ready", "approved"):
            raise PolicyViolation(f"action {action_id} is {action.status}; it cannot run")
        if action.mode == "ask" and action.status == "ready":
            raise PolicyViolation(f"action {action_id} needs an approval")  # defensive; never created so

        # Policy edited or agent paused since the proposal: the stricter of then and now wins.
        decision = self._gate.check(action.tool, args, ctx)
        refusal = self._recheck(action, decision)
        if refusal is not None:
            if decision.held and refusal == "agent paused":
                # Not a refusal: runs on resume. A half-run action stays 'executing' for recovery.
                if not recovering:
                    await self._actions.transition(action_id, action.status, "held")
                status = "held"
            else:
                await self._actions.transition(action_id, action.status, "blocked", result_json=_err(refusal))
                status = "blocked"
            await self._auditor.execution(
                action.tool, action.mode, action.content_hash, status, ctx, action_id
            )
            return OutboxResult(action_id=action_id, status=status, decision=decision, error=refusal)

        model = tool_def.args_model.model_validate(args)
        if recovering:
            stored = json.loads(action.result_json or "{}")
            inverse_prep = stored.get("inverse_prep")
            found = await self._already_done(tool_def, args, action.idempotency_key)
            if found is not None:
                return await self._record_done(action, tool_def, ctx, found, inverse_prep, register_inverse)
            if tool_def.lookup is None and action.mode == "ask":
                msg = "outcome unknown after a crash; check by hand before sending again"
                await self._actions.transition(action_id, "executing", "failed", result_json=_err(msg))
                await self._auditor.execution(
                    action.tool, action.mode, action.content_hash, "failed: unknown outcome", ctx, action_id
                )
                return OutboxResult(action_id=action_id, status="failed", error=msg)
        else:
            inverse_prep = None
            if register_inverse and action.mode == "report" and tool_def.inverse is not None:
                inverse_prep = await tool_def.inverse.prepare(self._adapters, model)
            try:
                await self._actions.transition(
                    action_id,
                    action.status,
                    "executing",
                    result_json=json.dumps({"inverse_prep": inverse_prep}),
                )
            except ApprovalConflict:
                latest = await self._actions.get(action_id)
                if latest is not None and latest.status == "done":
                    return OutboxResult(action_id=action_id, status="done", result=_stored_result(latest))
                raise

        try:
            result = await self._call(tool_def, model, action.idempotency_key)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            await self._actions.transition(action_id, "executing", "failed", result_json=_err(msg))
            await self._auditor.execution(
                action.tool, action.mode, action.content_hash, f"failed: {type(exc).__name__}", ctx, action_id
            )
            return OutboxResult(action_id=action_id, status="failed", error=msg)
        return await self._record_done(action, tool_def, ctx, result, inverse_prep, register_inverse)

    def _recheck(self, action: ActionRequest, decision: Decision) -> str | None:
        if decision.args_digest != action.digest_of_args:
            return "content changed since it was proposed"
        if decision.mode == "blocked" or decision.mode == "hand_off":
            return f"now {decision.mode}: {'; '.join(decision.reasons)}"
        if RANK[decision.mode] > RANK[action.mode]:
            return f"policy now needs {decision.mode}, approved as {action.mode}"
        if decision.record_only:
            return "recipe is in shadow"
        if decision.held:
            return "agent paused"
        return None

    async def _record_done(
        self,
        action: ActionRequest,
        tool_def: ToolDef,
        ctx: GateContext,
        result: Any,
        inverse_prep: dict[str, Any] | None,
        register_inverse: bool,
    ) -> OutboxResult:
        payload = {"result": result, "inverse_prep": inverse_prep}
        await self._actions.transition(
            action.id, "executing", "done", result_json=json.dumps(payload, default=str)
        )
        audit_id = await self._auditor.execution(
            action.tool, action.mode, action.content_hash, "done", ctx, action.id
        )
        inverse = tool_def.inverse
        if register_inverse and inverse is not None and action.mode == "report" and inverse_prep is not None:
            inverse_args = inverse.finish(inverse_prep, result) if inverse.finish else inverse_prep
            await self._undo.add(audit_id, inverse.tool, inverse_args)
        return OutboxResult(action_id=action.id, status="done", result=result, audit_id=audit_id)

    # ------------------------------------------------------------ adapter calls

    async def _call(self, tool_def: ToolDef, model: BaseModel, key: str) -> Any:
        if tool_def.handler is not None:
            return await tool_def.handler(model, key)
        method = getattr(self._adapters[tool_def.adapter or ""], tool_def.method or "")
        return await method(**_kwargs_for(method, model.model_dump(), key))

    async def _already_done(self, tool_def: ToolDef, args: dict[str, Any], key: str) -> Any:
        if tool_def.lookup is None or tool_def.adapter is None:
            return None
        method = getattr(self._adapters[tool_def.adapter], tool_def.lookup)
        return await method(**_kwargs_for(method, args, key))


def _kwargs_for(method: Any, args: dict[str, Any], key: str) -> dict[str, Any]:
    """Pass the args the method accepts, plus the idempotency key when it takes one."""
    params = inspect.signature(method).parameters
    kwargs = {k: v for k, v in args.items() if k in params}
    if "idempotency_key" in params:
        kwargs["idempotency_key"] = key
    elif "tag" in params:
        kwargs["tag"] = key
    return kwargs


def _stored_result(action: ActionRequest) -> Any:
    if not action.result_json:
        return None
    return json.loads(action.result_json).get("result")


def _err(message: str) -> str:
    return json.dumps({"error": message})
