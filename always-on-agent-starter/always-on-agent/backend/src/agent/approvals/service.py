"""Approval service (F07, architecture 4.3).

An approval request is an action_request in the outbox with status proposed. It is bound to
content_hash = sha256(normalised args + normalised shown content). Every decision is a compare-and-set
on status and hash, so a click in Slack and one in the console resolve once, and an approval for old
content never covers new content.

    proposed --approve(H)--> approved          (H must be the current hash; not after 24 h)
    proposed --edit--------> edited (new hash)  -> approved only with the new hash
    proposed/edited --reject--> rejected
    proposed/edited/approved --new event on the task, not the user's own--> invalidated
    proposed/edited --24 h--> expired           (task stays needs_you, draft kept)
    held (paused) --resume--> approved if it was approved before the pause, else proposed again

Every decision enqueues a resume job so the parked task graph moves on.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from agent.channels.base import ApprovalCard, ApprovalChannel
from agent.core.clock import Clock
from agent.core.errors import ApprovalConflict
from agent.db.repositories import ActionRepo, ActionRequest, AuditRepo, TaskRepo
from agent.policy.gate import GateContext, content_hash, digest_args, normalise_content
from agent.policy.registry import Registry
from agent.runtime.outbox import APPROVAL_TTL, Outbox, OutboxResult

log = logging.getLogger(__name__)

OPEN = ("proposed", "edited")
# Approved but not yet claimed by the outbox still counts as stale when the caller writes again (E16).
INVALIDATABLE = ("proposed", "edited", "approved")

Actor = str  # slack | console | system | user
EnqueueResume = Callable[[str, str], Awaitable[int]]  # (task_id, action_id) -> job id


class ApprovalOutcome(BaseModel):
    action_id: str
    status: str
    content_hash: str
    changed: bool  # False when this call found the decision already made (double click)
    note: str | None = None


class ApprovalService:
    def __init__(
        self,
        outbox: Outbox,
        actions: ActionRepo,
        audit: AuditRepo,
        registry: Registry,
        clock: Clock,
        enqueue_resume: EnqueueResume,
        channels: Sequence[ApprovalChannel] = (),
        tasks: TaskRepo | None = None,
    ) -> None:
        self._outbox = outbox
        self._actions = actions
        self._audit = audit
        self._registry = registry
        self._clock = clock
        self._resume = enqueue_resume
        self._channels = {c.name: c for c in channels}
        self._tasks = tasks

    # ------------------------------------------------------------ create

    async def create(
        self,
        task_id: str | None,
        tool: str,
        args: dict[str, Any] | BaseModel,
        content: str | None,
        ctx: GateContext,
        idempotency_key: str | None = None,
    ) -> OutboxResult:
        """Submit through the gate and outbox; post a card when the result needs an approval.

        Silent and Report actions run at once, Hand off, shadow and blocked ones are recorded; only
        proposed ones get a card. A re-run of the same proposal (same key) does not post a second card.
        """
        if ctx.task_id != task_id:
            ctx = ctx.model_copy(update={"task_id": task_id})
        result = await self._outbox.submit(tool, args, ctx, idempotency_key=idempotency_key, content=content)
        if result.status == "proposed":
            action = await self._get(result.action_id)
            if not action.surfaces:
                await self._post(action)
        return result

    # ------------------------------------------------------------ decisions

    async def approve(self, action_id: str, expected_hash: str, actor: Actor = "console") -> ApprovalOutcome:
        now = self._clock.now_utc()
        action = await self._get(action_id)
        if action.status == "approved" and action.content_hash == expected_hash:
            return ApprovalOutcome(
                action_id=action_id, status="approved", content_hash=expected_hash, changed=False
            )
        if await self._task_closed(action):
            await self._invalidate(action, "the task was closed")
            raise ApprovalConflict(f"{action.task_id} is closed; nothing was sent")
        if not await self._actions.decide(
            action_id, OPEN, "approved", expected_hash, unexpired_at=now, decided_at=now, decided_by=actor
        ):
            latest = await self._get(action_id)
            if latest.status == "approved" and latest.content_hash == expected_hash:
                return ApprovalOutcome(
                    action_id=action_id, status="approved", content_hash=expected_hash, changed=False
                )
            if latest.status in OPEN and latest.expires_at is not None and latest.expires_at <= now:
                await self._expire_one(latest)
                raise ApprovalConflict("this approval expired after 24 hours; nothing was sent")
            if latest.status in OPEN:
                raise ApprovalConflict("the text changed since this card was shown; approve the new version")
            raise ApprovalConflict(f"this request is already {latest.status}")
        return await self._decided(action, "approved", actor, None, expected_hash)

    async def reject(self, action_id: str, reason: str = "", actor: Actor = "console") -> ApprovalOutcome:
        action = await self._get(action_id)
        if action.status == "rejected":
            return ApprovalOutcome(
                action_id=action_id, status="rejected", content_hash=action.content_hash, changed=False
            )
        now = self._clock.now_utc()
        if not await self._actions.decide(
            action_id, OPEN, "rejected", decided_at=now, decided_by=actor, reason=reason or "rejected"
        ):
            latest = await self._get(action_id)
            raise ApprovalConflict(f"this request is already {latest.status}")
        return await self._decided(action, "rejected", actor, reason or None, action.content_hash)

    async def edit(
        self, action_id: str, new_content: str, actor: Actor = "console", expected_hash: str | None = None
    ) -> ApprovalOutcome:
        """Replace the shown content. Creates a new hash that needs its own approval (golden rule 2).

        expected_hash, when given, is the hash the editor saw: two editors racing cannot both win.
        """
        action = await self._get(action_id)
        now = self._clock.now_utc()
        if action.status not in OPEN:
            raise ApprovalConflict(f"this request is already {action.status}; it cannot be edited")
        if action.expires_at is not None and action.expires_at <= now:
            await self._expire_one(action)
            raise ApprovalConflict("this approval expired after 24 hours")
        tool_def = self._registry.get(action.tool)
        if tool_def is None or tool_def.content_field is None:
            raise ApprovalConflict("this action cannot be edited here; reject it and ask for a new draft")
        new_content = normalise_content(new_content)  # what is shown is exactly what is sent
        if not new_content:
            raise ApprovalConflict("the new text is empty")

        args = json.loads(action.args_json) | {tool_def.content_field: new_content}
        try:
            normalised = tool_def.args_model.model_validate(args).model_dump(mode="json")
        except ValidationError as exc:
            raise ApprovalConflict(
                f"the edited text is not valid for this action: {exc.errors()[0]['msg']}"
            ) from exc
        new_digest = digest_args(normalised)
        new_hash = content_hash(new_digest, new_content)
        if new_hash == action.content_hash:
            return ApprovalOutcome(
                action_id=action_id, status=action.status, content_hash=new_hash, changed=False
            )
        if not await self._actions.decide(
            action_id,
            OPEN,
            "edited",
            expected_hash or action.content_hash,
            unexpired_at=now,
            args_json=json.dumps(normalised, sort_keys=True),
            args_digest=new_digest,
            content=new_content,
            content_hash=new_hash,
            edited=True,
            expires_at=now + APPROVAL_TTL,
        ):
            raise ApprovalConflict("someone changed this request at the same time; reload it")
        await self._audit.append(
            tool=action.tool,
            mode=action.mode,
            args_digest=new_hash,
            result="edited",
            actor=actor,
            task_id=action.task_id,
            action_id=action_id,
        )
        await self._refresh_cards(action, "edited", "replaced by an edited version")
        await self._post(await self._get(action_id))
        return ApprovalOutcome(action_id=action_id, status="edited", content_hash=new_hash, changed=True)

    async def expire(self) -> list[str]:
        """Expire open requests older than 24 h (E19). The graph resumes and leaves the task as needs you."""
        expired = []
        for action in await self._actions.expired(OPEN, self._clock.now_utc()):
            if await self._expire_one(action):
                expired.append(action.id)
        return expired

    async def invalidate_for_task(
        self, task_id: str, actor: str, except_actor: str = "user", reason: str = "a new message arrived"
    ) -> list[str]:
        """A new event on the task changed the context: open requests can no longer be trusted (E16).

        The user's own events (their reply, their click) do not invalidate. Approved requests the
        outbox has not claimed yet are invalidated too; one already executing is past stopping.
        """
        if actor == except_actor:
            return []
        out = []
        for action in await self._actions.open_for_task(task_id, (*INVALIDATABLE, "held")):
            if action.status == "held" and action.mode != "ask":
                continue
            if await self._invalidate(action, reason):
                out.append(action.id)
        return out

    async def release_held(self) -> list[str]:
        """After a pause: approved requests go ahead, unapproved ones are shown again, own-space work runs."""
        released = []
        now = self._clock.now_utc()
        for action in await self._actions.with_statuses(("held",)):
            if action.mode == "ask" and action.decided_by:
                if await self._actions.decide(action.id, ("held",), "approved"):
                    await self._note(action, "approved while paused; sent after resume")
                    await self._enqueue(action)
                    released.append(action.id)
            elif action.mode == "ask":
                if await self._actions.decide(
                    action.id, ("held",), "proposed", expires_at=now + APPROVAL_TTL
                ):
                    await self._post(await self._get(action.id))
                    released.append(action.id)
            elif await self._actions.decide(action.id, ("held",), "ready"):
                await self._enqueue(action)
                released.append(action.id)
        return released

    async def pending(self) -> list[ActionRequest]:
        return await self._actions.with_statuses(OPEN)

    # ------------------------------------------------------------ internals

    async def _get(self, action_id: str) -> ActionRequest:
        action = await self._actions.get(action_id)
        if action is None:
            raise KeyError(action_id)
        return action

    async def _task_closed(self, action: ActionRequest) -> bool:
        if self._tasks is None or action.task_id is None:
            return False
        task = await self._tasks.get(action.task_id)
        return task is not None and task.state == "done"

    async def _decided(
        self,
        action: ActionRequest,
        status: Literal["approved", "rejected"],
        actor: str,
        note: str | None,
        hash_: str,
    ) -> ApprovalOutcome:
        await self._audit.append(
            tool=action.tool,
            mode=action.mode,
            args_digest=hash_,
            result=status,
            actor=actor,
            task_id=action.task_id,
            action_id=action.id,
        )
        await self._refresh_cards(action, status, note)
        await self._enqueue(action)
        return ApprovalOutcome(
            action_id=action.id, status=status, content_hash=hash_, changed=True, note=note
        )

    async def _invalidate(self, action: ActionRequest, reason: str) -> bool:
        if not await self._actions.decide(
            action.id,
            (action.status,),
            "invalidated",
            decided_at=self._clock.now_utc(),
            decided_by="system",
            reason=reason,
        ):
            return False
        await self._audit.append(
            tool=action.tool,
            mode=action.mode,
            args_digest=action.content_hash,
            result=f"invalidated: {reason}",
            actor="system",
            task_id=action.task_id,
            action_id=action.id,
        )
        await self._refresh_cards(
            action, "invalidated", f"No longer current: {reason}. A new draft will follow."
        )
        await self._enqueue(action)
        return True

    async def _expire_one(self, action: ActionRequest) -> bool:
        # Hash in the condition: an edit that lands meanwhile (new hash, new 24 h) is not expired.
        if not await self._actions.decide(
            action.id,
            OPEN,
            "expired",
            action.content_hash,
            decided_at=self._clock.now_utc(),
            decided_by="system",
            reason="no decision within 24 hours",
        ):
            return False
        await self._audit.append(
            tool=action.tool,
            mode=action.mode,
            args_digest=action.content_hash,
            result="expired",
            actor="system",
            task_id=action.task_id,
            action_id=action.id,
        )
        await self._refresh_cards(action, "expired", "Expired after 24 hours. The draft is kept on the task.")
        await self._enqueue(action)
        return True

    async def _enqueue(self, action: ActionRequest) -> None:
        if action.task_id is not None:
            await self._resume(action.task_id, action.id)

    def _card(self, action: ActionRequest, note: str | None = None) -> ApprovalCard:
        tool_def = self._registry.get(action.tool)
        label = (tool_def.description if tool_def and tool_def.description else action.tool).splitlines()[0]
        return ApprovalCard(
            action_id=action.id,
            task_id=action.task_id,
            title=f"{action.task_id or 'Request'}: {label}",
            content=action.content if action.content is not None else _args_text(action.args_json),
            content_hash=action.content_hash,
            expires_at=action.expires_at,
            edited=action.edited,
            note=note,
        )

    async def _post(self, action: ActionRequest) -> None:
        """Post to every channel. A channel that fails is logged; the console still lists the request."""
        card = self._card(action)
        for name, channel in self._channels.items():
            try:
                ref = await channel.post_card(card)
            except Exception:
                log.exception("could not post approval card", extra={"channel": name, "action": action.id})
                continue
            await self._actions.set_surface(action.id, name, ref)

    async def _refresh_cards(self, action: ActionRequest, status: str, note: str | None) -> None:
        card = self._card(action, note)
        for name, ref in action.surfaces.items():
            channel = self._channels.get(name)
            if channel is None:
                continue
            try:
                await channel.update_card(ref, card, status, note)
            except Exception:
                log.exception("could not update approval card", extra={"channel": name, "action": action.id})

    async def _note(self, action: ActionRequest, note: str) -> None:
        await self._refresh_cards(await self._get(action.id), "approved", note)


def _args_text(args_json: str) -> str:
    return "\n".join(f"{k}: {v}" for k, v in sorted(json.loads(args_json).items()))
