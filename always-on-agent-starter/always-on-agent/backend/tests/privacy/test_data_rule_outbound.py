import pytest

from agent.core.config import PrivacyConfig
from agent.core.errors import OutboundBlocked
from agent.privacy.data_rule import ContextPacket, PacketItem, allowed, reduce
from agent.privacy.outbound import OutboundAttachment, OutboundChecker, OutboundDraft

PACKET = ContextPacket(
    task={"id": "INC30114872", "state": "in_progress", "title": "Login fails for CALLER", "age_hours": 5},
    items=[
        PacketItem(
            data_class="text",
            kind="mail",
            meta={"received_at": "2026-03-25T04:30Z", "subject": "Login", "attachments": 1},
            subject="Login fails",
            text="CALLER cannot log in",
        ),
        PacketItem(data_class="chat", kind="chat_message", meta={"chat_id": "19:abc"}, text="any news?"),
        PacketItem(
            data_class="images",
            kind="screenshot",
            meta={"file": "s1.png"},
            text="Error 403",
            image_refs=["s1.png"],
        ),
        PacketItem(data_class="metadata", kind="refresh", meta={"status": "Failed", "dataset": "Sales KPI"}),
    ],
)


def test_levels_are_cumulative() -> None:
    assert allowed("chat", "text") and allowed("images", "chat") and allowed("text", "metadata")
    assert not allowed("metadata", "text") and not allowed("text", "chat") and not allowed("chat", "images")


def test_reduce_metadata_keeps_subject_free_metadata_only() -> None:
    out = reduce(PACKET, "metadata")
    assert out.task == {"id": "INC30114872", "state": "in_progress", "age_hours": 5}
    mail, chat, shot, refresh = out.items
    assert (mail.subject, mail.text) == (None, None)
    assert mail.meta == {"received_at": "2026-03-25T04:30Z", "attachments": 1}
    assert chat.text is None and chat.meta == {"chat_id": "19:abc"}
    assert shot.text is None and shot.image_refs == []
    assert refresh.meta == {"status": "Failed", "dataset": "Sales KPI"}
    assert out.data_classes() == {"metadata"}
    assert any("title" in n for n in out.notes) and any("image" in n for n in out.notes)


def test_reduce_text_keeps_mail_and_ocr_but_not_chat_or_images() -> None:
    out = reduce(PACKET, "text")
    mail, chat, shot, _ = out.items
    assert mail.text == "CALLER cannot log in" and mail.subject == "Login fails"
    assert chat.text is None
    assert shot.text == "Error 403" and shot.image_refs == []
    assert out.task["title"] == "Login fails for CALLER"
    assert out.data_classes() == {"metadata", "text"}


def test_reduce_chat_and_images() -> None:
    chat_level = reduce(PACKET, "chat")
    assert chat_level.items[1].text == "any news?" and chat_level.items[2].image_refs == []
    assert chat_level.data_classes() == {"metadata", "text", "chat"}
    full = reduce(PACKET, "images")
    assert full.items == PACKET.items and full.notes == []
    assert full.data_classes() == {"metadata", "text", "chat", "images"}


def test_reduce_does_not_change_the_input() -> None:
    reduce(PACKET, "metadata")
    assert PACKET.items[0].text == "CALLER cannot log in"


# ---------------------------------------------------------------- outbound

CHECK = OutboundChecker(PrivacyConfig(), ["corp.example"])


def draft(body: str = "Hello, the login works again.", **kw: object) -> OutboundDraft:
    return OutboundDraft.model_validate({"body": body, "to": ["tomas.garcia@corp.example"], **kw})


def test_clean_draft_passes() -> None:
    assert CHECK.check(draft()).ok


@pytest.mark.parametrize(
    "body",
    [
        "We track this KPI daily.",
        "Our KPIs say otherwise.",
        "Within 24 hours we will reply.",
        "a 24-hour target",
        "fixed in 24h",
        "the metrics look fine",
        "the K​PI is green",
        "the kpi",
    ],
)
def test_forbidden_phrases_block_caller_drafts(body: str) -> None:
    report = CHECK.check(draft(body))
    assert not report.ok and report.problems[0].rule == "forbidden_phrase"
    with pytest.raises(OutboundBlocked):
        CHECK.enforce(draft(body))


def test_forbidden_phrases_also_in_subject_and_attachment_names() -> None:
    assert not CHECK.check(draft(subject="KPI breach")).ok
    assert not CHECK.check(draft(attachments=[OutboundAttachment(name="kpi-report.pdf")])).ok


def test_phrases_are_whole_words_and_only_for_callers() -> None:
    assert CHECK.check(draft("The metrical analysis and 124 hours.")).ok
    assert CHECK.check(draft("KPI is red", caller_facing=False)).ok


def test_outside_and_malformed_recipients() -> None:
    report = CHECK.check(draft(cc=["a@partner.example"], bcc=["not-an-address"]))
    assert report.hand_off and not report.ok
    assert {p.rule for p in report.problems} == {"outside_recipient", "bad_recipient"}
    assert CHECK.check(draft(to=["x@eu.corp.example"])).ok  # subdomain is inside
    assert not CHECK.check(draft(to=["x@corp.example.evil.example"])).ok


def test_no_company_domains_lets_nobody_through() -> None:
    closed = OutboundChecker(PrivacyConfig(), [])
    assert closed.check(draft()).hand_off


def test_attachments() -> None:
    bad = [
        OutboundAttachment(name="fix.ps1"),
        OutboundAttachment(name="invoice.pdf.html"),
        OutboundAttachment(name="dump.zip", size_bytes=50 * 1024 * 1024),
    ]
    report = CHECK.check(draft(attachments=bad))
    assert [p.rule for p in report.problems] == ["attachment"] * 3 and not report.ok
    shot = CHECK.check(draft(attachments=[OutboundAttachment(name="screen.png", content_type="image/png")]))
    assert shot.ok and shot.warnings  # warned, not blocked
    assert CHECK.check(draft(attachments=[OutboundAttachment(name="my.report.v2.pdf")])).ok


def test_leftover_tokens_block() -> None:
    report = CHECK.check(draft("Hi CALLER, USER_2 fixed it."))
    assert not report.ok and "CALLER" in report.summary()
