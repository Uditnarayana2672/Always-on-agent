import re
from datetime import UTC, date, datetime, timedelta

import pytest

from agent.adapters.base import Attachment, MailMessage
from agent.adapters.fakes._base import FIXTURES_DIR
from agent.adapters.fakes.world import FIXTURE_NOW, FakeWorld
from agent.core.errors import AdapterError

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld.frozen()


# ---------------------------------------------------------------- each fake loads its fixtures


async def test_outlook_loads_all_mail_kinds(world: FakeWorld) -> None:
    mails = await world.mail.list_new(EPOCH)
    assert len(mails) == 18
    snow = [m for m in mails if m.sender.address == "servicenow@corp.example"]
    assert sum("has been assigned" in m.subject for m in snow) == 6
    assert sum("comment added" in m.subject for m in snow) == 2
    reports = [m for m in snow if m.has_attachments]
    names = {a.name for m in reports for a in await world.mail.get_attachments(m.id)}
    assert names == {"doxis_l2_open.csv", "doxis_l2_open.xlsx"}
    assert any(p.address.endswith("@partner.example") for m in mails for p in m.cc)  # outside cc
    assert any("Bonjour" in m.body for m in mails)  # French
    assert all(m.received_at.tzinfo is not None for m in mails)


async def test_outlook_since_filter_and_deliver(world: FakeWorld) -> None:
    assert await world.mail.list_new(FIXTURE_NOW) == []
    raw = {
        "id": "AAMkAGFAKE9999",
        "conversationId": "AAQkAGFAKECONV0012",
        "subject": "Re: Sales Overview numbers",
        "from": {"emailAddress": {"name": "Tomás Rivera", "address": "tomas.rivera@corp.example"}},
        "receivedDateTime": "2026-03-25T04:00:00Z",
        "body": {"contentType": "text", "content": "Any update?"},
    }
    world.mail.deliver(raw)
    new = await world.mail.list_new(FIXTURE_NOW)
    assert [m.id for m in new] == ["AAMkAGFAKE9999"]
    assert len(await world.mail.get_thread("AAQkAGFAKECONV0012")) == 2


async def test_calendar_zones_and_private(world: FakeWorld) -> None:
    start = datetime(2026, 3, 24, 18, 30, tzinfo=UTC)  # 00:00 IST on the 25th
    events = await world.calendar.events_between(start, start + timedelta(days=1))
    assert len(events) == 4
    cet = next(e for e in events if "Sales Overview review" in e.subject)
    assert cet.start.astimezone(UTC) == datetime(2026, 3, 25, 9, 0, tzinfo=UTC)  # 10:00 CET
    private = next(e for e in events if e.private)
    assert private.subject == "(private)"


async def test_teams_mentions_edits_deletes(world: FakeWorld) -> None:
    messages = await world.teams.list_chat_messages(EPOCH)
    assert len(messages) == 6
    mention = [m for m in messages if m.mentions_me]
    assert len(mention) == 1 and "INC30115230" in mention[0].text
    assert sum(m.edited_at is not None for m in messages) == 1
    assert sum(m.deleted for m in messages) == 1
    assert any("app.powerbi.com" in m.text for m in messages)


async def test_powerbi_history_kinds_and_link(world: FakeWorld) -> None:
    pbi = world.powerbi
    by_name = {d["name"]: d["id"] for d in pbi.data["datasets"]}
    assert (await pbi.refresh_history(by_name["Inventory Health"]))[0].status == "Failed"
    assert (await pbi.refresh_history(by_name["Ops KPI"]))[0].status == "Unknown"  # running
    slow = (await pbi.refresh_history(by_name["Finance Monthly"]))[0]
    assert slow.end is not None and slow.end - slow.start > timedelta(hours=1)
    flows = await pbi.upstream_dataflows(by_name["Inventory Health"])
    assert flows[0].last_status == "Failed"

    chat = next(m for m in await world.teams.list_chat_messages(EPOCH) if "app.powerbi.com" in m.text)
    url = re.search(r"https://app\.powerbi\.com\S+", chat.text)
    assert url is not None
    link = await pbi.resolve_link(url.group(0))
    assert link.dataset_id == by_name["Sales Overview"]
    assert await pbi.refreshes_today(by_name["Sales Overview"], date(2026, 3, 25)) == 1
    groups = await pbi.groups()
    assert {g.capacity_sku for g in groups} == {None, "P1"}


async def test_servicenow_assignment_and_reports(world: FakeWorld) -> None:
    mails = await world.mail.list_new(EPOCH)
    notices = [n for m in mails if (n := world.servicenow.parse_assignment(m))]
    assert {n.number for n in notices} == {
        "INC30108754",
        "INC30109981",
        "INC30114872",
        "INC30115230",
        "INC30116640",
        "INC30117002",
    }
    report_mails = [m for m in mails if m.has_attachments]
    parsed = [
        world.servicenow.parse_report(a) for m in report_mails for a in await world.mail.get_attachments(m.id)
    ]
    assert len(parsed) == 2 and parsed[0] == parsed[1]  # CSV and XLSX carry the same rows
    assert len(parsed[0]) == 6 and parsed[0][0].opened.tzinfo is not None
    comma = Attachment(
        name="r.csv",
        content_type="text/csv",
        data=b"Number,Short description,State,Priority,Opened,"
        b"Updated,Assigned to,Assignment group,Caller\r\nINC30117002,x,Assigned,4 - Low,2026-03-25 03:20:00,"
        b"2026-03-25 03:30:00,Test User,Doxis L2,Sophie Laurent\r\n",
    )
    assert world.servicenow.parse_report(comma)[0].number == "INC30117002"


async def test_sharepoint_sql_ocr(world: FakeWorld) -> None:
    list_id = next(iter(world.sharepoint.lists))
    assert len(await world.sharepoint.list_items(list_id)) == 4
    assert len(await world.sharepoint.list_items(list_id, since=datetime(2026, 3, 23, tzinfo=UTC))) == 2
    assert await world.sql.count("sales.fact_orders") == 182344
    assert await world.sql.max_date("inv.stock_snapshot") == date(2026, 3, 23)
    with pytest.raises(AdapterError):
        await world.sql.count("hr.salaries")
    image = (FIXTURES_DIR / "screenshots" / "doxis_site_page_km_lba.png").read_bytes()
    result = await world.ocr.read(image)
    assert "KM: Tomás Rivera" in result.text and "LBA: (empty)" in result.text
    assert all(line.box[2] > line.box[0] for line in result.lines)
    assert (await world.ocr.read(b"not a fixture")).lines == []


# ---------------------------------------------------------------- writes and failure injection


async def test_writes_are_recorded_and_send_is_idempotent(world: FakeWorld) -> None:
    draft = await world.mail.create_draft(["sophie.laurent@corp.example"], "Re: access", "Done.")
    await world.mail.send(draft, "idem-1")
    await world.mail.send(draft, "idem-1")
    assert world.mail.sent == [draft]
    await world.slack.send_dm("hello")
    assert world.all_writes() == [("outlook", "create_draft"), ("outlook", "send"), ("slack", "send_dm")]
    assert world.mail.writes[0].at == FIXTURE_NOW


@pytest.mark.parametrize(
    ("kind", "status", "retryable"), [("429", 429, True), ("410", 410, False), ("timeout", None, True)]
)
async def test_failure_injection(world: FakeWorld, kind: str, status: int | None, retryable: bool) -> None:
    world.mail.fail_next(kind, times=2, method="list_new")  # type: ignore[arg-type]
    await world.mail.get_thread("AAQkAGFAKECONV0001")  # other methods unaffected
    for _ in range(2):
        with pytest.raises(AdapterError) as info:
            await world.mail.list_new(EPOCH)
        assert info.value.status_code == status and info.value.retryable is retryable
    assert len(await world.mail.list_new(EPOCH)) == 18


async def test_failure_on_write_records_nothing(world: FakeWorld) -> None:
    world.teams.fail_next("500")
    with pytest.raises(AdapterError):
        await world.teams.post("19:fake-priya@unq.gbl.spaces", "hi", "test")
    assert world.teams.writes == [] and world.teams.posted == []


async def test_health_reflects_failure(world: FakeWorld) -> None:
    world.powerbi.fail_next("timeout", method="health")
    statuses = {s.connector: s.status for s in await world.health()}
    assert statuses["powerbi"] == "down" and statuses["outlook"] == "ok"


async def test_slack_events(world: FakeWorld) -> None:
    world.slack.deliver("button", action_id="approve", value="A1")
    events = await world.slack.next_events()
    assert [e.kind for e in events] == ["button"] and await world.slack.next_events() == []


def test_mail_model_round_trip(world: FakeWorld) -> None:
    raw = next(iter(world.mail.raw.values()))
    assert (
        MailMessage.model_validate_json(
            MailMessage.model_validate(
                {
                    "id": raw["id"],
                    "conversation_id": "c",
                    "subject": "s",
                    "sender": {"name": "n", "address": "a@corp.example"},
                    "received_at": FIXTURE_NOW,
                    "body": "",
                }
            ).model_dump_json()
        ).id
        == raw["id"]
    )


# ---------------------------------------------------------------- no real data


ALLOWED_DOMAINS = {
    "corp.example",
    "partner.example",
    "teams.example",
    "servicenow.corp.example",
    "app.powerbi.com",
}


def test_fixtures_contain_only_made_up_data() -> None:
    text = "\n".join(
        p.read_text(encoding="utf-8") for p in FIXTURES_DIR.rglob("*") if p.suffix in {".json", ".csv"}
    )
    # Teams chat ids (19:...@thread.v2) look like addresses; only match real-looking domains.
    emails = set(re.findall(r"[\w.+-]+@((?:[\w-]+\.)+(?:example|com|net|org|io|de|ch|fr|in|co))", text))
    assert emails <= {"corp.example", "partner.example"}, emails
    hosts = set(re.findall(r"https?://([\w.-]+)", text))
    assert hosts <= ALLOWED_DOMAINS, hosts
    for word in ("ibm", "udit", "gmail", "outlook.com", "microsoft.com"):
        assert word not in text.lower(), word
