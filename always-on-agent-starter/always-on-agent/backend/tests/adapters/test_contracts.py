"""Contract tests: each fake and its real adapter must behave the same where the agent depends on it.

Every test runs twice, once against the fake (adapters/fakes) and once against the real adapter on
the respx simulators. A recipe that passes against the fakes then means the same against Graph.
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from agent.adapters.calendar import GraphCalendarAdapter
from agent.adapters.fakes.world import FakeWorld
from agent.adapters.graph_client import POWERBI_BASE
from agent.adapters.outlook_graph import OutlookGraphAdapter
from agent.adapters.powerbi import PowerBIRestAdapter, RefreshRefused
from agent.adapters.teams import GraphTeamsAdapter
from agent.core.errors import AdapterError
from tests.adapters.graph_helpers import Kv, client
from tests.adapters.graph_sim import CalendarSim, MailSim, PowerBISim, TeamsSim

CALLER = "sophie.laurent@corp.example"
ANNA = "anna.keller@corp.example"


@dataclass
class Setup:
    adapter: Any
    # backend-specific test controls
    foreign_draft: Callable[[], str]
    edit_draft_cc: Callable[[str, list[str]], None]
    invite: Callable[[str], None]
    chat: str
    add_guest: Callable[[], None]
    dataset: str
    owner: str
    start_running: Callable[[], None]
    fill_cap: Callable[[], None]
    forbid: Callable[[], None]


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


def _fake(kind: str) -> Setup:
    world = FakeWorld.frozen()
    now = world.clock.now_utc()
    pbi = world.powerbi
    dataset = next(d["id"] for d in pbi.data["datasets"] if d["name"] == "Sales Overview")
    chat = "19:fake-doxis-l2@thread.v2"

    def foreign() -> str:
        world.mail.drafts["USER-DRAFT"] = {"to": [], "cc": [], "body": "", "digest": "", "agent": False}
        return "USER-DRAFT"

    def edit_cc(draft: str, cc: list[str]) -> None:
        world.mail.edit_draft(draft, cc=cc)

    def invite(event_id: str) -> None:
        next(r for r in world.calendar.raw if r["id"] == event_id)["attendees"] = [
            {"emailAddress": {"address": ANNA}}
        ]

    def guest() -> None:
        world.teams.chats[chat]["members"].append(
            {"email": "guest@partner.example", "user": {"displayName": "Guest"}}
        )

    def running() -> None:
        stamp = now.isoformat().replace("+00:00", "Z")
        pbi.deliver_refresh(
            dataset,
            {
                "requestId": "run-1",
                "refreshType": "OnDemand",
                "status": "Unknown",
                "startTime": stamp,
                "endTime": None,
            },
        )

    def fill() -> None:
        for i in range(8):
            stamp = (now - timedelta(minutes=10 + i)).isoformat().replace("+00:00", "Z")
            pbi.deliver_refresh(
                dataset,
                {
                    "requestId": f"cap-{i}",
                    "refreshType": "OnDemand",
                    "status": "Completed",
                    "startTime": stamp,
                    "endTime": stamp,
                },
            )

    adapters = {"mail": world.mail, "calendar": world.calendar, "teams": world.teams, "powerbi": pbi}
    return Setup(
        adapter=adapters[kind],
        foreign_draft=foreign,
        edit_draft_cc=edit_cc,
        invite=invite,
        chat=chat,
        add_guest=guest,
        dataset=dataset,
        owner="test.user@corp.example",
        start_running=running,
        fill_cap=fill,
        forbid=lambda: pbi.forbidden.add(dataset),
    )


def _real(kind: str, http: httpx.AsyncClient, router: respx.MockRouter) -> Setup:
    now = datetime(2026, 3, 25, 3, 45, tzinfo=UTC)
    mail_sim = MailSim(router)
    cal_sim = CalendarSim(router)
    teams_sim = TeamsSim(router)
    teams_sim.add_chat("chat-1", "2026-03-25T03:00:00Z", ["test.user@corp.example", ANNA])
    pbi_sim = PowerBISim(router)
    pbi_sim.add_dataset("ds-1", owner="priya.nair@corp.example")

    def foreign() -> str:
        mail_sim.add("users-draft", "folder-drafts", isDraft=True)
        return "users-draft"

    def edit_cc(draft: str, cc: list[str]) -> None:
        mail_sim.messages[draft]["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]

    def invite(event_id: str) -> None:
        cal_sim.events[event_id]["attendees"] = [{"emailAddress": {"address": ANNA}}]

    def running() -> None:
        pbi_sim.add_refresh("ds-1", "2026-03-25T03:40:00Z", status="Unknown", request_id="run-1")

    def fill() -> None:
        for i in range(8):
            pbi_sim.add_refresh(
                "ds-1", f"2026-03-25T0{i // 4}:{(i % 4) * 10:02d}:00Z", end="2026-03-25T03:00:00Z"
            )

    adapters = {
        "mail": lambda: OutlookGraphAdapter(
            client(http, "outlook"), Kv(), ["test.user@corp.example"], lambda: now
        ),
        "calendar": lambda: GraphCalendarAdapter(client(http, "calendar"), "Asia/Kolkata", lambda: now),
        "teams": lambda: GraphTeamsAdapter(client(http, "teams"), Kv(), lambda: now),
        "powerbi": lambda: PowerBIRestAdapter(client(http, "powerbi", base=POWERBI_BASE), 8, lambda: now),
    }
    return Setup(
        adapter=adapters[kind](),
        foreign_draft=foreign,
        edit_draft_cc=edit_cc,
        invite=invite,
        chat="chat-1",
        add_guest=lambda: teams_sim.members["chat-1"].append(
            {"email": "guest@partner.example", "userId": "g"}
        ),
        dataset="ds-1",
        owner="priya.nair@corp.example",
        start_running=running,
        fill_cap=fill,
        forbid=lambda: pbi_sim.forbidden.add("ds-1"),
    )


@pytest.fixture(params=["fake", "real"])
def backend(request: pytest.FixtureRequest, http: httpx.AsyncClient) -> AsyncIterator[Callable[[str], Setup]]:
    if request.param == "fake":
        yield _fake
        return
    with respx.mock(assert_all_called=False) as router:
        yield lambda kind: _real(kind, http, router)


# ---------------------------------------------------------------- mail


async def test_mail_send_once_per_key_and_found_after(backend: Callable[[str], Setup]) -> None:
    s = backend("mail")
    draft = await s.adapter.create_draft([CALLER], "Re: access", "It works again.")
    assert await s.adapter.find_sent("key-1") is None
    first = await s.adapter.send(draft, "key-1", to=[CALLER], cc=[])
    assert await s.adapter.find_sent("key-1") == first
    assert await s.adapter.send(draft, "key-1", to=[CALLER], cc=[]) == first  # retry: no second send


async def test_mail_send_refuses_changed_recipients(backend: Callable[[str], Setup]) -> None:
    s = backend("mail")
    draft = await s.adapter.create_draft([CALLER], "Re: access", "It works again.")
    s.edit_draft_cc(draft, ["marc.dubois@partner.example"])
    with pytest.raises(AdapterError, match="recipients changed"):
        await s.adapter.send(draft, "key-2", to=[CALLER], cc=[])
    assert await s.adapter.find_sent("key-2") is None


async def test_mail_discard_refuses_drafts_the_agent_did_not_make(backend: Callable[[str], Setup]) -> None:
    s = backend("mail")
    with pytest.raises(AdapterError, match="not created by the agent"):
        await s.adapter.discard_draft(s.foreign_draft())
    ours = await s.adapter.create_draft([CALLER], "s", "b")
    await s.adapter.discard_draft(ours)
    await s.adapter.discard_draft(ours)  # gone already: fine


async def test_mail_move_to_deleted_items_is_refused(backend: Callable[[str], Setup]) -> None:
    s = backend("mail")
    with pytest.raises(AdapterError, match="hand-off"):
        await s.adapter.move("anything", "Deleted Items")


# ---------------------------------------------------------------- calendar


async def test_hold_is_idempotent_and_undo_refuses_once_people_are_invited(
    backend: Callable[[str], Setup],
) -> None:
    s = backend("calendar")
    start = datetime(2026, 3, 25, 9, 0, tzinfo=UTC)
    hold = await s.adapter.add_hold("Focus", start, start + timedelta(hours=1), idempotency_key="h1")
    assert await s.adapter.add_hold("Focus", start, start + timedelta(hours=1), idempotency_key="h1") == hold
    s.invite(hold)
    with pytest.raises(AdapterError, match="invited"):
        await s.adapter.discard_hold(hold)
    with pytest.raises(AdapterError):
        await s.adapter.add_hold("Backwards", start, start - timedelta(minutes=5))


# ---------------------------------------------------------------- Teams


async def test_post_covers_every_member_and_is_found_by_its_tag(backend: Callable[[str], Setup]) -> None:
    s = backend("teams")
    members = [m for m in await s.adapter.chat_members(s.chat) if m != "test.user@corp.example"]
    posted = await s.adapter.post(s.chat, "Fixed, please retry.", "tag-9", recipients=members)
    assert await s.adapter.find_tagged(s.chat, "tag-9") is not None and posted
    s.add_guest()
    with pytest.raises(AdapterError, match="did not cover"):
        await s.adapter.post(s.chat, "Again", "tag-10", recipients=members)
    assert await s.adapter.find_tagged(s.chat, "tag-10") is None


# ---------------------------------------------------------------- Power BI


async def test_refresh_running_is_not_doubled(backend: Callable[[str], Setup]) -> None:
    s = backend("powerbi")
    s.start_running()
    result = await s.adapter.trigger_refresh(s.dataset)
    assert result.status == "already_running" and result.request_id == "run-1"


async def test_refresh_at_cap_is_refused(backend: Callable[[str], Setup]) -> None:
    s = backend("powerbi")
    s.fill_cap()
    with pytest.raises(RefreshRefused) as err:
        await s.adapter.trigger_refresh(s.dataset)
    assert err.value.kind == "cap"


async def test_refresh_without_permission_hands_off_with_the_owner(backend: Callable[[str], Setup]) -> None:
    s = backend("powerbi")
    s.forbid()
    with pytest.raises(RefreshRefused) as err:
        await s.adapter.trigger_refresh(s.dataset)
    assert err.value.kind == "forbidden" and err.value.owner == s.owner
