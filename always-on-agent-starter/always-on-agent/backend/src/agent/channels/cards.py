"""Slack Block Kit cards (F14): brief, alert, approval, hand-off, digest, task thread root.

Rules every builder follows:
- Text from mail, chats or the model is escaped (&, <, >), so it cannot turn into a link, a user
  mention or an @channel / @here broadcast (golden rule 4). The agent never sets link_names.
- Slack limits are respected: 3000 characters per section, 150 per header, 75 per button label,
  2000 per button value, 50 blocks per message. Longer content links to the console instead.
- An approval card whose text had to be cut has no Approve button: nobody approves text they did
  not see (golden rule 2). It says "Open in console to review and approve".
- Edit is offered only when the text fits Slack's edit box (3000 characters).
- Button values carry the action id and the content hash the card shows; the handler passes the
  hash to the approval service, so a stale card can never approve newer content.

User-facing wording is plain: what happened, what is needed, one action.
"""

import json
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from agent.channels.base import ApprovalCard

SECTION_MAX = 3000
HEADER_MAX = 150
BUTTON_TEXT_MAX = 75
BUTTON_VALUE_MAX = 2000
MAX_BLOCKS = 50
EDIT_MAX = 3000  # plain_text_input max_length
FALLBACK_MAX = 3000
DIGEST_ITEMS_MAX = 15

APPROVE, EDIT, REJECT, UNDO = "approval_approve", "approval_edit", "approval_reject", "undo"
OPEN = "open_console"

Block = dict[str, Any]


def escape(text: str) -> str:
    """Slack mrkdwn control characters; everything else is shown as written."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clip(text: str, limit: int, marker: str = "…") -> tuple[str, bool]:
    """Cut to at most `limit` characters (marker included). Returns (text, was_cut)."""
    if len(text) <= limit:
        return text, False
    cut = text[: max(0, limit - len(marker))]
    cut = re.sub(r"&[a-z]*$", "", cut)  # never end inside an escaped &amp; / &lt;
    return cut.rstrip() + marker, True


def _section(text: str) -> Block:
    clipped, _ = clip(text, SECTION_MAX)
    return {"type": "section", "text": {"type": "mrkdwn", "text": clipped}}


def _header(text: str) -> Block:
    clipped, _ = clip(text, HEADER_MAX)
    return {"type": "header", "text": {"type": "plain_text", "text": clipped, "emoji": False}}


def _context(text: str) -> Block:
    clipped, _ = clip(text, SECTION_MAX)
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": clipped}]}


def _button(
    label: str, action_id: str, value: str = "", style: str | None = None, url: str | None = None
) -> Block:
    text, _ = clip(label, BUTTON_TEXT_MAX)
    button: Block = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id}
    if value:
        if len(value) > BUTTON_VALUE_MAX:
            raise ValueError("button value too long")
        button["value"] = value
    if style:
        button["style"] = style
    if url:
        button["url"] = url
    return button


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in escape(text).splitlines() or [""])


def _finish(blocks: list[Block], fallback: str, console_url: str | None) -> tuple[str, list[Block]]:
    """Cap at MAX_BLOCKS: the overflow becomes one 'more in the console' line."""
    if len(blocks) > MAX_BLOCKS:
        blocks = [
            *blocks[: MAX_BLOCKS - 1],
            _context(f"More in the console{f': {console_url}' if console_url else ''}."),
        ]
    text, _ = clip(fallback, FALLBACK_MAX)
    return text, blocks


def console_link(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/#/{path.lstrip('/')}"


# ---------------------------------------------------------------- approval


def approval_value(card: ApprovalCard) -> str:
    return json.dumps({"a": card.action_id, "h": card.content_hash}, separators=(",", ":"))


def parse_value(value: str) -> dict[str, Any]:
    data = json.loads(value or "{}")
    if not isinstance(data, dict):
        raise ValueError("bad button value")
    return data


def approval_blocks(
    card: ApprovalCard, console_base: str, to_local: Callable[[datetime], datetime] | None = None
) -> tuple[str, list[Block]]:
    open_url = console_link(console_base, f"approvals/{card.action_id}")
    quoted = _quote(card.content)
    shown, cut = clip(quoted, SECTION_MAX - 200)
    blocks: list[Block] = [_header(card.title), _section(shown)]
    details = []
    if card.edited:
        details.append("Edited version")
    if card.expires_at is not None:
        when = to_local(card.expires_at) if to_local else card.expires_at
        details.append(f"Expires {when:%d %b %H:%M} {when.tzname() or 'UTC'}")
    if card.note:
        details.append(escape(card.note))
    if details:
        blocks.append(_context(" · ".join(details)))
    buttons: list[Block] = []
    if cut:
        blocks.append(_context("This is too long to show here. Open in console to review and approve."))
    else:
        if "Approve" in card.buttons:
            buttons.append(_button("Approve", APPROVE, approval_value(card), style="primary"))
        if "Edit" in card.buttons and len(card.content) <= EDIT_MAX:
            buttons.append(_button("Edit", EDIT, approval_value(card)))
    if "Reject" in card.buttons:
        buttons.append(_button("Reject", REJECT, approval_value(card), style="danger"))
    buttons.append(_button("Open in console", OPEN, url=open_url))
    blocks.append({"type": "actions", "block_id": f"approval:{card.action_id}", "elements": buttons})
    return _finish(blocks, f"Approval needed: {card.title}", open_url)


_OUTCOME = {
    "approved": "Approved",
    "rejected": "Rejected",
    "edited": "Replaced by an edited version",
    "expired": "Expired",
    "invalidated": "No longer current",
}


def approval_outcome_blocks(
    card: ApprovalCard, status: str, note: str | None, console_base: str
) -> tuple[str, list[Block]]:
    """The card after a decision: same text, no buttons, the outcome stated."""
    shown, _ = clip(_quote(card.content), SECTION_MAX - 200)
    outcome = _OUTCOME.get(status, status.capitalize())
    line = f"*{outcome}*" + (f": {escape(note)}" if note else "")
    blocks = [_header(card.title), _section(shown), _context(line)]
    return _finish(
        blocks, f"{outcome}: {card.title}", console_link(console_base, f"approvals/{card.action_id}")
    )


def edit_modal(card: ApprovalCard, callback_id: str = "approval_edit_submit") -> dict[str, Any]:
    title, _ = clip("Edit before approving", 24)
    return {
        "type": "modal",
        "callback_id": callback_id,
        "private_metadata": approval_value(card),
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "text",
                "label": {"type": "plain_text", "text": "Text"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "multiline": True,
                    "max_length": EDIT_MAX,
                    "initial_value": card.content[:EDIT_MAX],
                },
            },
            _context("Saving makes a new version. It still needs your approval."),
        ],
    }


# ---------------------------------------------------------------- brief, alert, hand-off


class BriefSection(BaseModel):
    heading: str
    lines: list[str] = Field(default_factory=list)


def brief_blocks(title: str, sections: Sequence[BriefSection], console_base: str) -> tuple[str, list[Block]]:
    blocks: list[Block] = [_header(title)]
    if not any(s.lines for s in sections):
        blocks.append(_section("Nothing needs you right now."))
    for section in sections:
        if not section.lines:
            continue
        body = "\n".join(f"• {escape(line)}" for line in section.lines)
        blocks.append(_section(f"*{escape(section.heading)}*\n{body}"))
    blocks.append(
        {"type": "actions", "elements": [_button("Open console", OPEN, url=console_link(console_base, ""))]}
    )
    return _finish(blocks, title, console_base)


def alert_blocks(
    text: str, console_base: str, task_id: str | None = None, urgent: bool = False
) -> tuple[str, list[Block]]:
    prefix = "Urgent: " if urgent else ""
    path = f"tasks/{task_id}" if task_id else ""
    lead = f"*{escape(task_id)}* " if task_id else ""
    blocks = [
        _section(f"{prefix}{lead}{escape(text)}"),
        {
            "type": "actions",
            "elements": [_button("Open in console", OPEN, url=console_link(console_base, path))],
        },
    ]
    return _finish(blocks, f"{prefix}{task_id + ': ' if task_id else ''}{text}", console_base)


def hand_off_blocks(
    task_id: str | None, what: str, why: str, console_base: str, who: str | None = None
) -> tuple[str, list[Block]]:
    """Something only the user can do (sign in, grant access, change a ticket state, delete)."""
    lines = [f"*Needs you:* {escape(what)}", f"Why: {escape(why)}"]
    if who:
        lines.append(f"Who can help: {escape(who)}")
    path = f"tasks/{task_id}" if task_id else ""
    blocks: list[Block] = []
    if task_id:
        blocks.append(_header(f"{task_id}: hand-off"))
    blocks.append(_section("\n".join(lines)))
    blocks.append(
        {
            "type": "actions",
            "elements": [_button("Open in console", OPEN, url=console_link(console_base, path))],
        }
    )
    return _finish(blocks, f"Needs you: {what}", console_base)


# ---------------------------------------------------------------- digest


class DigestItem(BaseModel):
    text: str
    audit_id: int | None = None  # set when the action can be undone
    task_id: str | None = None


def digest_blocks(
    title: str,
    done: Sequence[DigestItem],
    console_base: str,
    open_approvals: int = 0,
    hand_offs: int = 0,
    shadow_counts: dict[str, int] | None = None,
) -> tuple[str, list[Block]]:
    blocks: list[Block] = [_header(title)]
    if not done:
        blocks.append(_section("No actions on my own today."))
    for item in done[:DIGEST_ITEMS_MAX]:
        lead = f"*{escape(item.task_id)}* " if item.task_id else ""
        block = _section(f"{lead}{escape(item.text)}")
        if item.audit_id is not None:
            block["accessory"] = _button("Undo", UNDO, json.dumps({"u": item.audit_id}))
        blocks.append(block)
    if len(done) > DIGEST_ITEMS_MAX:
        blocks.append(_context(f"{len(done) - DIGEST_ITEMS_MAX} more in the console."))
    summary = []
    if open_approvals:
        summary.append(f"{open_approvals} waiting for your approval")
    if hand_offs:
        summary.append(f"{hand_offs} only you can do")
    for recipe, count in sorted((shadow_counts or {}).items()):
        summary.append(f"{escape(recipe)} would have acted {count} time(s) (trial mode)")
    if summary:
        blocks.append(_context(" · ".join(summary)))
    blocks.append(
        {
            "type": "actions",
            "elements": [_button("Open console", OPEN, url=console_link(console_base, "activity"))],
        }
    )
    return _finish(blocks, title, console_base)


# ---------------------------------------------------------------- task threads


def thread_title(task_id: str, title: str) -> str:
    """'INC30114872 <en dash> short description', one line, plain (architecture F14)."""
    one_line = " ".join(title.split())
    text, _ = clip(f"{task_id} \u2013 {one_line}" if one_line else task_id, HEADER_MAX)
    return text


def thread_root_blocks(task_id: str, title: str, console_base: str) -> tuple[str, list[Block]]:
    text = thread_title(task_id, title)
    blocks = [
        _section(f"*{escape(text)}*"),
        _context(f"Replies in this thread go to {escape(task_id)}."),
        {
            "type": "actions",
            "elements": [
                _button("Open in console", OPEN, url=console_link(console_base, f"tasks/{task_id}"))
            ],
        },
    ]
    return text, blocks


def status_text(
    paused: bool, leave: bool, pending: int, health: dict[str, str], since: datetime | None = None
) -> str:
    state = "Paused" if paused else "Running"
    if paused and since is not None:
        state += f" since {since:%d %b %H:%M} UTC"
    lines = [f"*{state}*" + (" · on leave" if leave else ""), f"{pending} waiting for your approval"]
    bad = sorted(f"{name} ({status})" for name, status in health.items() if status != "ok")
    lines.append("Connections: " + (", ".join(bad) if bad else "all fine"))
    return "\n".join(lines)
