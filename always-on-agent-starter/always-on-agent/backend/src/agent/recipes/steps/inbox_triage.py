"""R02 Inbox triage: sender rules in code first, the model only labels what the rules leave.

Never deletes and never moves to Deleted Items. Actions (all Report, with undo): move to the
notifications or newsletters folder, flag, add a category. A mail that needs a reply gets a reply task
(RPL-####) for R03.

Rules, in order:
1. Not ours to touch: already filed by the user (not in Inbox), sent by the user, a draft, or gone.
2. The manager's mail always stays in Inbox and is flagged, whatever the label (architecture R02).
3. Automatic replies and bounces -> notifications folder; meeting invitations and responses are only
   categorised (moving them would hide the meeting request).
4. Configured newsletter / notification senders -> their folders.
5. Protected mail (labelled, encrypted) is never sent to the model: left in Inbox, noted.
6. The rest: the model labels needs_reply / fyi / notification with a confidence; below
   MIN_CONFIDENCE nothing happens.

Also:
- categories are added to the user's own, never replacing them;
- a thread where the user wrote last is not "owed" a reply;
- a mail that belongs to a task (an INC ticket, a reply task) gets only the code rules: the task's
  own recipe handles replies, so no second reply task;
- with the data rule below "text" the model is not asked; code rules still run (E12 reduced form).
"""

from typing import Literal

from pydantic import BaseModel, Field

from agent.adapters.base import MailMessage
from agent.core.errors import AgentError
from agent.model.prompts import render
from agent.recipes.runner import StepContext
from agent.recipes.views import MailRules

MIN_CONFIDENCE = 0.6
BODY_CHARS = 2000
_AUTO_REPLY = (
    "automatic reply",
    "auto reply",
    "autoreply",
    "out of office",
    "abwesenheitsnotiz",
    "réponse automatique",
)
_BOUNCE = ("mailer-daemon@", "postmaster@", "microsoftexchange")
_BOUNCE_SUBJECT = ("undeliverable", "delivery status notification", "nicht zustellbar", "non remis")
_INVITE = (
    "accepted:",
    "declined:",
    "tentative:",
    "invitation:",
    "updated invitation:",
    "canceled:",
    "cancelled:",
)


class Classification(BaseModel):
    label: Literal["needs_reply", "fyi", "notification"]
    confidence: float = Field(ge=0, le=1)


Decision = Literal["manager", "auto_reply", "bounce", "invite", "newsletter", "notification", "protected"]


def matches(address: str, patterns: list[str]) -> bool:
    """An address matches a full address, an @domain, or a prefix like 'noreply@'."""
    addr = address.lower()
    for raw in patterns:
        pattern = raw.lower().strip()
        if not pattern:
            continue
        if pattern.startswith("@") and addr.endswith(pattern):
            return True
        if pattern.endswith("@") and addr.startswith(pattern):
            return True
        if addr == pattern:
            return True
    return False


def rule_for(mail: MailMessage, rules: MailRules) -> Decision | None:
    sender = mail.sender.address.lower()
    subject = mail.subject.strip().lower()
    if matches(sender, rules.manager):
        return "manager"
    if subject.startswith(_AUTO_REPLY) or any(a in subject for a in _AUTO_REPLY[:4]):
        return "auto_reply"
    if sender.startswith(_BOUNCE) or subject.startswith(_BOUNCE_SUBJECT):
        return "bounce"
    if subject.startswith(_INVITE):
        return "invite"
    if matches(sender, rules.newsletter_senders):
        return "newsletter"
    if matches(sender, rules.notification_senders):
        return "notification"
    if mail.metadata_only:
        return "protected"
    return None


def skip_reason(mail: MailMessage, rules: MailRules) -> str | None:
    if mail.is_draft:
        return "a draft"
    if mail.folder.strip("/").lower() not in ("inbox", ""):
        return f"already in {mail.folder}"
    if mail.sender.address.lower() in rules.me:
        return "sent by you"
    return None


async def steps(ctx: StepContext) -> None:
    message_id = str(ctx.trigger.get("external_id") or "")
    if ctx.trigger.get("source") != "mail" or ctx.trigger.get("kind") != "mail" or not message_id:
        return
    rules = MailRules.model_validate(await ctx.read("read_mail_rules", {}))
    try:
        mail: MailMessage = await ctx.adapters.outlook.get_message(message_id)
    except AgentError as exc:
        ctx.note(f"mail no longer there ({type(exc).__name__}); nothing to triage")
        return
    reason = skip_reason(mail, rules)
    if reason:
        ctx.note(f"left alone: {reason}")
        return

    decision = rule_for(mail, rules)
    label: str | None = None
    if decision in (None, "manager") and not mail.metadata_only and ctx.task is None:
        label = await classify(ctx, mail)
    elif decision is None and ctx.task is not None:
        ctx.note("mail belongs to a task; its recipe handles it")
        return

    def key(tool: str) -> str:
        return f"triage:{mail.id}:{tool}"

    def categorise(name: str) -> None:
        if name in mail.categories:
            return
        ctx.propose(
            "categorise",
            {"message_id": mail.id, "categories": [*mail.categories, name]},
            idempotency_key=key("categorise"),
        )

    def flag() -> None:
        if not mail.flagged:
            ctx.propose("flag_mail", {"message_id": mail.id}, idempotency_key=key("flag"))

    def move(folder: str) -> None:
        ctx.propose("move_mail", {"message_id": mail.id, "folder": folder}, idempotency_key=key("move"))

    if decision == "manager":
        flag()  # stays in Inbox, whatever the label
    elif decision in ("auto_reply", "bounce"):
        categorise("Automatic")
        move(rules.folder_notifications)
    elif decision == "invite":
        categorise("Meetings")
    elif decision == "newsletter":
        categorise("Newsletter")
        move(rules.folder_newsletters)
    elif decision == "notification":
        categorise("Notification")
        move(rules.folder_notifications)
    elif decision == "protected":
        ctx.note("protected mail: left in Inbox without reading it")
        return
    elif label == "notification":
        categorise("Notification")
        move(rules.folder_notifications)
    elif label == "fyi":
        categorise("FYI")
    elif label == "needs_reply":
        flag()

    if label == "needs_reply" and ctx.task is None and not await replied_last(ctx, mail, rules):
        ctx.propose(
            "open_reply_task",
            {
                "message_id": mail.id,
                "conversation_id": mail.conversation_id,
                "subject": mail.subject,
                "event_id": ctx.trigger.get("event_id"),
            },
            purpose="reply_task",
            idempotency_key=f"triage:{mail.conversation_id}:reply_task",
        )


async def classify(ctx: StepContext, mail: MailMessage) -> str | None:
    if ctx.recipe.model == "none":
        return None
    text = f"Subject: {mail.subject}\n\n{mail.body[:BODY_CHARS]}"
    try:
        result = await ctx.model(
            Classification,
            purpose="triage",
            extra=text,
            extra_class="text",
            instructions=render("triage_classify"),
        )
    except AgentError as exc:
        ctx.note(f"not classified ({type(exc).__name__}); only sender rules applied")
        return None
    if result.confidence < MIN_CONFIDENCE:
        ctx.note(f"unsure ({result.label}, {result.confidence:.2f}); left alone")
        return None
    return result.label


async def replied_last(ctx: StepContext, mail: MailMessage, rules: MailRules) -> bool:
    """True when the user wrote the latest message in the thread (nothing is owed)."""
    try:
        thread = await ctx.adapters.outlook.get_thread(mail.conversation_id)
    except AgentError:
        return False
    sent = [m for m in thread if not m.is_draft]
    return bool(sent) and sent[-1].sender.address.lower() in rules.me and sent[-1].id != mail.id
