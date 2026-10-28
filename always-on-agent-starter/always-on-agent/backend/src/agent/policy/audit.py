"""Audit trail (F13): one row per gate decision, execution and undo, including blocked and shadow ones.

Rows hold the args digest, never the args: message bodies stay out of the audit log.
"""

from agent.db.repositories import AuditRepo
from agent.policy.gate import Decision, GateContext


class Auditor:
    def __init__(self, repo: AuditRepo) -> None:
        self._repo = repo

    async def decision(self, decision: Decision, ctx: GateContext, status: str, action_id: str | None) -> int:
        """status is the outbox status the decision led to: ready, proposed, handed_off, shadow, ..."""
        notes = list(decision.reasons)
        if decision.flagged:
            notes.append("FLAGGED")
        if decision.lowering_refused:
            notes.append("LOWERING_REFUSED")
        result = f"decision:{status}" + (f" ({'; '.join(notes)})" if notes else "")
        return await self._repo.append(
            tool=decision.tool,
            mode=decision.mode,
            args_digest=decision.args_digest,
            result=result,
            actor=ctx.actor,
            task_id=ctx.task_id,
            recipe=ctx.recipe,
            action_id=action_id,
        )

    async def execution(
        self,
        tool: str,
        mode: str,
        args_digest: str,
        result: str,
        ctx: GateContext,
        action_id: str,
    ) -> int:
        return await self._repo.append(
            tool=tool,
            mode=mode,
            args_digest=args_digest,
            result=result,
            actor=ctx.actor,
            task_id=ctx.task_id,
            recipe=ctx.recipe,
            action_id=action_id,
        )

    async def undo(self, original: dict[str, object], result: str, actor: str) -> int:
        return await self._repo.append(
            tool=f"undo:{original['tool']}",
            mode=str(original["mode"]),
            args_digest=str(original["args_digest"]),
            result=result,
            actor=actor,
            task_id=opt_str(original.get("task_id")),
            recipe=opt_str(original.get("recipe")),
            action_id=opt_str(original.get("action_id")),
        )


def opt_str(value: object) -> str | None:
    return None if value is None else str(value)
