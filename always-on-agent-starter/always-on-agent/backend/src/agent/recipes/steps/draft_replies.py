"""R03 Draft replies, on reply tasks (RPL-####) that inbox triage opens.

Turn 0: read the thread, ask the model for the words (Sonnet when allowed, Haiku otherwise), propose
save_draft (Report, undo discards the draft). Turn 1: with the saved draft's id, propose send_mail
(Ask): the approval card shows exactly the text that will be sent.

Code, not the model, picks the recipients: the sender of the last message from someone else, plus
everyone else on it except the user (reply-all). Anyone outside the company makes the send a Hand off
at the gate (E15). A draft made on Haiku says so on the card.

Edge cases:
- the user already answered (their message is the latest in the thread): nothing to draft;
- protected mail (metadata only): the agent cannot read it, so it asks the user to reply themselves;
- a system or no-reply sender: no reply is drafted;
- the model returns nothing usable: no draft, a note on the task;
- shadow: save_draft is recorded, not run, so send_mail is recorded against a placeholder draft;
- a new mail in the thread after the card went out invalidates it (coordinator, E16) and the next run
  drafts again from the new thread.
"""

from pydantic import BaseModel, Field

from agent.adapters.base import MailMessage
from agent.core.errors import AgentError
from agent.model.prompts import render
from agent.privacy.outbound import OutboundDraft
from agent.recipes.runner import StepContext
from agent.recipes.steps.inbox_triage import matches
from agent.recipes.views import MailRules, ReplyContext

MAX_WORDS = 180
THREAD_CHARS = 6000
SHADOW_DRAFT = "shadow-draft"
_SYSTEM = ["noreply@", "no-reply@", "donotreply@", "mailer-daemon@", "postmaster@", "servicenow@", "powerbi@"]


class Draft(BaseModel):
    reply: str = Field(default="", max_length=5000)


def thread_text(thread: list[MailMessage]) -> str:
    parts = []
    for mail in thread[-6:]:
        parts.append(f"From: {mail.sender.name}\nSubject: {mail.subject}\n\n{mail.body.strip()}")
    return "\n\n---\n\n".join(parts)[-THREAD_CHARS:]


def recipients(last: MailMessage, me: list[str]) -> tuple[list[str], list[str]]:
    to = [last.sender.address.lower()]
    cc: list[str] = []
    for person in [*last.to, *last.cc]:
        address = person.address.lower()
        if address and address not in me and address not in to and address not in cc:
            cc.append(address)
    return to, cc


async def steps(ctx: StepContext) -> None:
    if ctx.task is None or ctx.task.kind != "reply":
        return  # attached to reply tasks only
    if ctx.turn == 0:
        await draft(ctx)
    else:
        send(ctx)


async def draft(ctx: StepContext) -> None:
    assert ctx.task is not None
    rules = MailRules.model_validate(await ctx.read("read_mail_rules", {}))
    reply = ReplyContext.model_validate(await ctx.read("read_reply_context", {"task_id": ctx.task.id}))
    if not reply.conversation_id:
        ctx.note("no mail thread linked to this task")
        return
    try:
        thread: list[MailMessage] = await ctx.adapters.outlook.get_thread(reply.conversation_id)
    except AgentError as exc:
        ctx.note(f"could not read the thread ({type(exc).__name__})")
        return
    thread = [m for m in thread if not m.is_draft]
    if not thread:
        ctx.note("the thread is empty")
        return
    if thread[-1].sender.address.lower() in rules.me:
        ctx.note("you answered last; no reply owed")
        return
    last = thread[-1]
    if last.metadata_only:
        ctx.note("this mail is protected: the agent cannot read it, please reply yourself")
        return
    if matches(last.sender.address, _SYSTEM):
        ctx.note("sent by a system address; no reply drafted")
        return
    try:
        out = await ctx.model(
            Draft,
            purpose="caller_drafts",
            extra=thread_text(thread),
            extra_class="text",
            instructions=render("draft_reply", max_words=MAX_WORDS),
        )
    except AgentError as exc:
        ctx.note(f"no draft ({type(exc).__name__})")
        return
    body = out.reply.strip()
    if not body:
        ctx.note("the model wrote nothing usable; no draft")
        return
    to, cc = recipients(last, rules.me)
    tier = (ctx.last_tier or "").lower()
    ctx.memo.update(
        {
            "body": body,
            "to": to,
            "cc": cc,
            "reply_to": last.id,
            "drafted_on": "haiku" if "haiku" in tier else (tier or None),
        }
    )
    ctx.propose(
        "save_draft",
        {"to": to, "cc": cc, "subject": "", "body": body, "reply_to": last.id},
        purpose="caller_draft",
        idempotency_key=f"{ctx.task.id}:save_draft:{last.id}",
    )
    ctx.again()


def send(ctx: StepContext) -> None:
    memo = ctx.memo
    if not memo.get("body"):
        return
    saved = ctx.outcome_of("save_draft")
    status = (saved or {}).get("status")
    if status == "done":
        draft_id = str((saved or {}).get("result") or "")
    elif status == "shadow":
        draft_id = SHADOW_DRAFT
    else:
        ctx.note(f"draft not saved ({status or 'unknown'}); nothing to send")
        return
    if not draft_id:
        ctx.note("the saved draft has no id; nothing to send")
        return
    drafted_on = memo.get("drafted_on") if memo.get("drafted_on") in ("haiku", "sonnet", "opus") else None
    ctx.propose(
        "send_mail",
        {
            "draft_id": draft_id,
            "to": memo["to"],
            "cc": memo["cc"],
            "reply_all": False,
            "body": memo["body"],
            "drafted_on": drafted_on,
        },
        content=memo["body"],
        purpose="caller_draft",
        draft=OutboundDraft(body=memo["body"], to=memo["to"], cc=memo["cc"], caller_facing=False),
    )
