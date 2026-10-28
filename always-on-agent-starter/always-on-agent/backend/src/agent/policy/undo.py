"""Undo for Report actions (F13).

undo(audit_id) runs the registered inverse once. It first checks the item is still as the agent left
it; if the user changed it by hand since, it refuses and says why. The inverse goes through the gate
and outbox like any other call, and must itself be Silent or Report. Undoing twice is a no-op.
"""

import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from agent.db.repositories import AuditRepo, UndoRepo
from agent.policy.audit import Auditor, opt_str
from agent.policy.gate import GateContext
from agent.policy.registry import Adapters, Registry

if TYPE_CHECKING:
    from agent.runtime.outbox import Outbox


class UndoResult(BaseModel):
    status: Literal["done", "already_undone", "refused", "not_undoable"]
    message: str


class UndoService:
    def __init__(
        self, registry: Registry, outbox: "Outbox", audit: AuditRepo, undo: UndoRepo, adapters: Adapters
    ) -> None:
        self._registry = registry
        self._outbox = outbox
        self._audit_repo = audit
        self._auditor = Auditor(audit)
        self._undo = undo
        self._adapters = adapters

    async def undo(self, audit_id: int, actor: str = "user") -> UndoResult:
        original = await self._audit_repo.get(audit_id)
        op = await self._undo.for_audit(audit_id)
        if original is None or op is None:
            return UndoResult(status="not_undoable", message="This action cannot be undone.")
        if op["status"] == "done":
            return UndoResult(status="already_undone", message="Already undone.")
        if not await self._undo.mark(op["id"], "available", "running"):
            latest = await self._undo.for_audit(audit_id)
            if latest is not None and latest["status"] == "done":
                return UndoResult(status="already_undone", message="Already undone.")
            return UndoResult(status="refused", message="An undo for this action is already running.")

        inverse_args: dict[str, Any] = json.loads(op["inverse_args_json"])
        tool_def = self._registry.get(str(original["tool"]))
        inverse = tool_def.inverse if tool_def else None

        reason = None
        if inverse is not None and inverse.check is not None:
            reason = await inverse.check(self._adapters, inverse_args)
        if reason is not None:
            return await self._refuse(op["id"], original, actor, reason)

        ctx = GateContext(
            task_id=opt_str(original.get("task_id")), recipe=opt_str(original.get("recipe")), actor=actor
        )
        outcome = await self._outbox.submit(
            str(op["inverse_tool"]),
            inverse_args,
            ctx,
            idempotency_key=f"undo:{audit_id}",
            register_inverse=False,
        )
        if outcome.status != "done":
            detail = (
                outcome.error or (outcome.decision and "; ".join(outcome.decision.reasons)) or outcome.status
            )
            return await self._refuse(op["id"], original, actor, f"the undo could not run ({detail})")

        await self._undo.mark(op["id"], "running", "done")
        await self._auditor.undo(original, "undo: done", actor)
        return UndoResult(status="done", message="Undone.")

    async def _refuse(self, undo_id: int, original: dict[str, Any], actor: str, reason: str) -> UndoResult:
        await self._undo.mark(undo_id, "running", "available")
        await self._auditor.undo(original, f"undo: refused: {reason}", actor)
        return UndoResult(status="refused", message=f"Not undone: {reason}.")
