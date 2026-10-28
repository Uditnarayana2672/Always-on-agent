"""Card builder snapshot tests. Regenerate with UPDATE_SNAPSHOTS=1 after a deliberate change."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent.channels import cards
from agent.channels.base import ApprovalCard
from agent.channels.cards import BriefSection, DigestItem

SNAPSHOTS = Path(__file__).parent / "snapshots"
CONSOLE = "http://127.0.0.1:8765"
EN_DASH = chr(0x2013)
EXPIRES = datetime(2026, 3, 26, 4, 30, tzinfo=UTC)


def snapshot(name: str, value: Any) -> None:
    path = SNAPSHOTS / f"{name}.json"
    text = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"missing snapshot {path.name}; run with UPDATE_SNAPSHOTS=1"
    assert text == path.read_text(encoding="utf-8")


def card(content: str = "Hello Sophie,\nthe login works again.", **extra: Any) -> ApprovalCard:
    return ApprovalCard(
        action_id="01HACTION",
        task_id="INC30114872",
        title="INC30114872: Post this in the Teams chat",
        content=content,
        content_hash="abc123",
        expires_at=EXPIRES,
        **extra,
    )


def test_approval_card_snapshot() -> None:
    text, blocks = cards.approval_blocks(card(), CONSOLE)
    snapshot("approval", {"text": text, "blocks": blocks})


def test_approval_outcome_snapshot() -> None:
    text, blocks = cards.approval_outcome_blocks(card(), "approved", None, CONSOLE)
    snapshot("approval_approved", {"text": text, "blocks": blocks})


def test_brief_digest_alert_hand_off_thread_snapshots() -> None:
    brief = cards.brief_blocks(
        "Wednesday 25 March",
        [
            BriefSection(
                heading="Needs you", lines=["INC30114872: reply to Sophie", "REF-0412: refresh approved?"]
            ),
            BriefSection(heading="Today", lines=["10:30 Sales Overview review"]),
            BriefSection(heading="Empty", lines=[]),
        ],
        CONSOLE,
    )
    digest = cards.digest_blocks(
        "Today's digest",
        [
            DigestItem(text="Moved 3 ServiceNow notices to Done", audit_id=41),
            DigestItem(text="Flagged a mail from your manager", audit_id=42, task_id="INC30114872"),
            DigestItem(text="Read the refresh history"),
        ],
        CONSOLE,
        open_approvals=2,
        hand_offs=1,
        shadow_counts={"inbox-triage": 5},
    )
    alert = cards.alert_blocks(
        "The 24-hour clock is at 22 hours", CONSOLE, task_id="INC30114872", urgent=True
    )
    hand_off = cards.hand_off_blocks(
        "REF-0412",
        "refresh Inventory Health",
        "you lack permission on this dataset",
        CONSOLE,
        who="priya.nair@corp.example",
    )
    thread = cards.thread_root_blocks("INC30114872", "Login fails\n on site   Basel", CONSOLE)
    snapshot("brief", {"text": brief[0], "blocks": brief[1]})
    snapshot("digest", {"text": digest[0], "blocks": digest[1]})
    snapshot("alert", {"text": alert[0], "blocks": alert[1]})
    snapshot("hand_off", {"text": hand_off[0], "blocks": hand_off[1]})
    snapshot("thread_root", {"text": thread[0], "blocks": thread[1]})


def test_empty_brief_and_digest_say_so() -> None:
    _, brief = cards.brief_blocks("Brief", [], CONSOLE)
    _, digest = cards.digest_blocks("Digest", [], CONSOLE)
    assert "Nothing needs you" in json.dumps(brief) and "No actions" in json.dumps(digest)


# ---------------------------------------------------------------- safety rules


def test_fetched_text_cannot_mention_or_link() -> None:
    hostile = "Hi <!channel> please see <https://evil.example|your bank> & <@U123>"
    _, blocks = cards.approval_blocks(card(content=hostile), CONSOLE)
    shown = json.dumps(blocks)
    assert "<!channel>" not in shown and "<https://evil" not in shown and "<@U123>" not in shown
    assert "&lt;!channel&gt;" in shown and "&amp;" in shown


def test_cut_text_has_no_approve_button() -> None:
    long_text = "word " * 1000
    _, blocks = cards.approval_blocks(card(content=long_text), CONSOLE)
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    ids = [a["action_id"] for a in actions]
    assert cards.APPROVE not in ids and cards.EDIT not in ids and cards.REJECT in ids and cards.OPEN in ids
    assert "Open in console to review and approve" in json.dumps(blocks)
    assert all(len(b.get("text", {}).get("text", "")) <= cards.SECTION_MAX for b in blocks)


def test_edit_is_offered_only_when_it_fits_the_box() -> None:
    text = "x" * 2700  # fits the card and Slack's edit box
    _, blocks = cards.approval_blocks(card(content=text), CONSOLE)
    ids = [a["action_id"] for a in next(b for b in blocks if b["type"] == "actions")["elements"]]
    assert cards.APPROVE in ids and cards.EDIT in ids


def test_button_values_carry_the_hash() -> None:
    _, blocks = cards.approval_blocks(card(), CONSOLE)
    approve = next(
        a
        for a in next(b for b in blocks if b["type"] == "actions")["elements"]
        if a["action_id"] == cards.APPROVE
    )
    assert cards.parse_value(approve["value"]) == {"a": "01HACTION", "h": "abc123"}


def test_limits() -> None:
    _, blocks = cards.digest_blocks(
        "D", [DigestItem(text=f"item {i}", audit_id=i) for i in range(80)], CONSOLE
    )
    assert len(blocks) <= cards.MAX_BLOCKS and "65 more in the console" in json.dumps(blocks)
    _, many = cards.brief_blocks(
        "B", [BriefSection(heading=f"h{i}", lines=["x"]) for i in range(80)], CONSOLE
    )
    assert len(many) == cards.MAX_BLOCKS
    header = cards.approval_blocks(card().model_copy(update={"title": "T" * 400}), CONSOLE)[1][0]
    assert len(header["text"]["text"]) <= cards.HEADER_MAX


@pytest.mark.parametrize(("text", "limit"), [("a &amp; b", 6), ("x &lt;y", 5)])
def test_clip_never_ends_inside_an_entity(text: str, limit: int) -> None:
    clipped, cut = cards.clip(text, limit)
    assert cut and not clipped.rstrip("…").endswith(("&", "&a", "&am", "&l", "&lt"))


def test_edit_modal_holds_the_text_and_hash() -> None:
    view = cards.edit_modal(card())
    assert view["blocks"][0]["element"]["initial_value"].startswith("Hello Sophie")
    assert cards.parse_value(view["private_metadata"])["h"] == "abc123"


def test_thread_title_is_one_line_with_an_en_dash() -> None:
    assert cards.thread_title("INC30114872", "Login\nfails") == f"INC30114872 {EN_DASH} Login fails"
    assert cards.thread_title("REF-0412", "") == "REF-0412"


def test_expiry_is_shown_in_the_users_zone() -> None:
    from zoneinfo import ZoneInfo

    _, blocks = cards.approval_blocks(card(), CONSOLE, lambda dt: dt.astimezone(ZoneInfo("Asia/Kolkata")))
    assert "Expires 26 Mar 10:00 IST" in json.dumps(blocks)
