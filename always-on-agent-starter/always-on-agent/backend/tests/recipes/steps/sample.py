"""Step functions used by the recipe tests (loaded with step_prefixes=("tests.recipes.steps.",))."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from agent.core.errors import PolicyViolation
from agent.privacy.outbound import OutboundDraft
from agent.recipes.runner import StepContext

CHAT = "19:incident-chat"
CALLER = "sophie.laurent@corp.example"
seen: dict[str, Any] = {}


class Draft(BaseModel):
    reply: str


async def draft_reply(ctx: StepContext) -> None:
    """Model writes the words; code decides the tool, the chat and the recipient."""
    draft = await ctx.model(Draft)
    ctx.propose(
        "post_chat",
        {"chat_id": CHAT, "body": draft.reply, "recipients": [CALLER]},
        content=draft.reply,
        purpose="caller_draft",
        draft=OutboundDraft(body=draft.reply, to=[CALLER]),
    )


async def note_only(ctx: StepContext) -> None:
    """No model: a deterministic Report action."""
    ctx.propose("update_ledger", {"text": f"seen by {ctx.recipe.name}"}, purpose="note")
    seen.setdefault("events", []).append((ctx.recipe.name, ctx.trigger.get("event_id")))


async def two_turns(ctx: StepContext) -> None:
    ctx.propose("update_ledger", {"text": f"turn {ctx.turn}"}, purpose="note")
    if ctx.turn == 0:
        ctx.again()


async def try_to_write(ctx: StepContext) -> None:
    """Everything a step must not be able to do, recorded for the test."""
    attempts: dict[str, str] = {}
    outlook = ctx.adapters.outlook
    for name, attempt in [
        ("send", lambda: outlook.send("D1", "k")),
        ("move", lambda: outlook.move("m", "Done")),
        ("drafts", lambda: outlook.drafts),
        ("setattr", lambda: setattr(outlook, "send", None)),
        ("outbox", lambda: ctx.outbox),  # type: ignore[attr-defined]
    ]:
        try:
            attempt()
            attempts[name] = "allowed"
        except (PolicyViolation, AttributeError) as exc:
            attempts[name] = type(exc).__name__
    attempts["reads"] = ",".join(dir(outlook))
    mails = await outlook.list_new(datetime(2026, 1, 1, tzinfo=UTC))
    attempts["read_count"] = str(len(mails))
    try:
        await ctx.read("update_ledger", {"text": "x"})
        attempts["read_report"] = "allowed"
    except PolicyViolation as exc:
        attempts["read_report"] = str(exc)
    try:
        ctx.propose("send_mail", {"draft_id": "D1", "to": [CALLER]})
        attempts["unlisted"] = "allowed"
    except PolicyViolation:
        attempts["unlisted"] = "PolicyViolation"
    seen["attempts"] = attempts


async def brief(ctx: StepContext) -> None:
    """Scheduled, no task: Report is fine, Ask is refused."""
    ctx.propose("update_ledger", {"text": "brief posted"}, purpose="note")
    try:
        ctx.propose("post_chat", {"chat_id": CHAT, "body": "hi", "recipients": [CALLER]})
    except PolicyViolation as exc:
        ctx.note(str(exc))


def not_async(ctx: StepContext) -> None:
    return None
