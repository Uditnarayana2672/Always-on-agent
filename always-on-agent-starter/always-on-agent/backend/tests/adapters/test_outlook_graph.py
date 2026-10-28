import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.adapters import outlook_graph as og
from agent.adapters.outlook_graph import OutlookGraphAdapter, label_names, parse_graph_time
from agent.core.clock import FrozenClock
from agent.core.errors import AdapterError
from agent.db.repositories import EventRepo, JobRepo, KvRepo, MemoryRepo, TaskRepo
from agent.runtime.coordinator import Coordinator
from agent.runtime.pollers import Pollers
from tests.adapters.graph_helpers import NOW, G, Kv, Sleeps, client
from tests.adapters.graph_sim import MailSim

DELTA = re.escape(f"{G}/me/mailFolders/inbox/messages/delta")
SENT_DELTA = re.escape(f"{G}/me/mailFolders/sentitems/messages/delta")
CALLER = "sophie.laurent@corp.example"


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def router() -> AsyncIterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def sim(router: respx.MockRouter) -> MailSim:
    return MailSim(router)


def adapter(
    http: httpx.AsyncClient, kv: Any | None = None, sleep: Sleeps | None = None
) -> OutlookGraphAdapter:
    return OutlookGraphAdapter(
        client(http, "outlook", sleep=sleep), kv or Kv(), me=["test.user@corp.example"], now=lambda: NOW
    )


def item(sim: MailSim, message_id: str, **fields: Any) -> dict[str, Any]:
    raw = sim.add(message_id, **fields)
    return {k: v for k, v in raw.items() if k != "props"}


# ---------------------------------------------------------------- helpers


def test_graph_time_and_labels() -> None:
    assert parse_graph_time("2026-03-25T09:30:00.1234567Z").microsecond == 123456
    assert parse_graph_time("2026-03-25T09:30:00").tzinfo is not None
    assert label_names(
        "MSIP_Label_a_Enabled=True; MSIP_Label_a_Name=Confidential;MSIP_Label_b_Name=General"
    ) == [
        "Confidential",
        "General",
    ]
    assert label_names(None) == []


# ---------------------------------------------------------------- delta


async def test_delta_pages_then_commits_the_delta_link(
    http: httpx.AsyncClient, router: respx.MockRouter, sim: MailSim
) -> None:
    first = item(sim, "m1")
    second = item(sim, "m2", receivedDateTime="2026-03-25T04:05:00Z")
    pages = [
        httpx.Response(
            200,
            json={
                "value": [second],
                "@odata.nextLink": f"{G}/me/mailFolders/inbox/messages/delta?$skiptoken=p2",
            },
        ),
        httpx.Response(
            200,
            json={
                "value": [first, {"id": "gone", "@removed": {"reason": "deleted"}}],
                "@odata.deltaLink": f"{G}/me/mailFolders/inbox/messages/delta?$deltatoken=t1",
            },
        ),
    ]
    route = router.get(url__regex=DELTA).mock(side_effect=pages)
    kv = Kv()
    mail = adapter(http, kv)
    changes = await mail.changes("inbox")
    assert [m.id for m in changes.messages] == ["m1", "m2"]  # oldest first, @removed skipped
    assert (
        route.calls[0].request.url.params["$filter"].startswith("receivedDateTime ge")
    )  # first sync: last 7 days only
    assert route.calls[0].request.headers["Prefer"] == 'IdType="ImmutableId"'
    assert kv.data == {}  # not stored until the poller commits
    await mail.commit_delta(changes)
    assert kv.data["delta.mail.inbox"].endswith("$deltatoken=t1")


async def test_expired_delta_resyncs_seven_days_and_the_coordinator_dedupes(
    http: httpx.AsyncClient, router: respx.MockRouter, sim: MailSim, engine: AsyncEngine, clock: FrozenClock
) -> None:
    raw = item(sim, "m1", subject="INC30114872 login fails")
    kv = KvRepo(engine, clock)
    await kv.set("delta.mail.inbox", f"{G}/me/mailFolders/inbox/messages/delta?$deltatoken=old")
    await kv.set("delta.mail.sentitems", f"{G}/me/mailFolders/sentitems/messages/delta?$deltatoken=s")

    def inbox(request: httpx.Request) -> httpx.Response:
        if "deltatoken=old" in str(request.url):
            return httpx.Response(410, json={"error": {"code": "SyncStateNotFound"}})
        return httpx.Response(
            200,
            json={
                "value": [raw],
                "@odata.deltaLink": f"{G}/me/mailFolders/inbox/messages/delta?$deltatoken=new",
            },
        )

    router.get(url__regex=DELTA).mock(side_effect=inbox)
    router.get(url__regex=SENT_DELTA).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [],
                "@odata.deltaLink": f"{G}/me/mailFolders/sentitems/messages/delta?$deltatoken=s",
            },
        )
    )
    coordinator = Coordinator(
        TaskRepo(engine, clock),
        EventRepo(engine, clock),
        JobRepo(engine, clock),
        MemoryRepo(engine, clock),
        clock,
        create_rules=[],
    )
    mail = adapter(http, kv)
    pollers = Pollers(coordinator.ingest, kv, clock, mail=mail)

    first = await mail.changes("inbox")
    assert first.resynced and [m.id for m in first.messages] == ["m1"]
    assert await coordinator.ingest(first.messages[0], "mail")  # seen once already
    assert await pollers.poll_mail() == 1  # the resync re-reads m1 ...
    duplicate = await coordinator.ingest(first.messages[0], "mail")
    assert duplicate.duplicate  # ... and the coordinator drops the copy
    assert (await kv.get("delta.mail.inbox") or "").endswith("deltatoken=new")


async def test_throttled_delta_waits_for_retry_after(
    http: httpx.AsyncClient, router: respx.MockRouter, sim: MailSim
) -> None:
    router.get(url__regex=DELTA).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "17"}),
            httpx.Response(200, json={"value": [], "@odata.deltaLink": f"{G}/d?$deltatoken=x"}),
        ]
    )
    sleeps = Sleeps()
    changes = await adapter(http, sleep=sleeps).changes("inbox")
    assert changes.messages == [] and sleeps.waits == [17]


# ---------------------------------------------------------------- protected mail


async def test_labelled_encrypted_and_confidential_mail_is_metadata_only(
    http: httpx.AsyncClient, router: respx.MockRouter, sim: MailSim
) -> None:
    sim.add("conf", sensitivity="confidential", body={"contentType": "text", "content": "secret plan"})
    sim.add("label", body={"contentType": "text", "content": "labelled text"})
    sim.messages["label"]["props"][og.LABEL_PROP] = "MSIP_Label_x_Enabled=True;MSIP_Label_x_Name=Confidential"
    sim.add("open", body={"contentType": "text", "content": "general text"})
    sim.messages["open"]["props"][og.LABEL_PROP] = "MSIP_Label_y_Name=General"
    sim.add("irm", hasAttachments=True, body={"contentType": "text", "content": "x"})
    sim.attachments["irm"] = [
        {"name": "message.rpmsg", "contentType": "application/x-microsoft-rpmsg-message", "size": 10}
    ]
    mail = adapter(http)
    assert (await mail.get_message("conf")).body == ""
    labelled = await mail.get_message("label")
    assert labelled.metadata_only and labelled.body == ""
    assert (await mail.get_message("open")).body == "general text"
    irm = await mail.get_message("irm")
    assert irm.metadata_only and irm.attachments[0].name == "message.rpmsg"
    with pytest.raises(AdapterError, match="protected"):
        await mail.get_attachments("conf")


async def test_label_check_failure_fails_closed(http: httpx.AsyncClient, router: respx.MockRouter) -> None:
    router.get(url__regex=re.escape(f"{G}/me/messages/x")).mock(return_value=httpx.Response(403))
    raw = {
        "id": "x",
        "subject": "s",
        "from": {"emailAddress": {"address": CALLER}},
        "receivedDateTime": "2026-03-25T04:00:00Z",
        "body": {"contentType": "text", "content": "body"},
    }
    message = await adapter(http)._to_mail(raw, "Inbox")
    assert message.metadata_only and message.body == ""


# ---------------------------------------------------------------- drafts and send


async def test_send_stamps_the_idempotency_header_and_find_sent_uses_it(
    http: httpx.AsyncClient, sim: MailSim, router: respx.MockRouter
) -> None:
    mail = adapter(http)
    draft = await mail.create_draft([CALLER], "Re: access", "Done, please try again.")
    assert sim.messages[draft]["props"][og.AGENT_DRAFT_PROP] == "1"
    assert await mail.find_sent("task:send_mail:abc") is None
    sent = await mail.send(draft, "task:send_mail:abc", to=[CALLER], cc=[])
    assert sent == draft and sim.sends == [draft]
    assert sim.messages[draft]["props"][og.IDEMPOTENCY_PROP] == "task:send_mail:abc"
    assert await mail.find_sent("task:send_mail:abc") == draft
    lookups = [c.request for c in router.calls if "sentitems/messages" in str(c.request.url)]
    assert "X-Agent-Idempotency" in str(lookups[-1].url)
    # A retry after the draft has gone finds it in Sent Items instead of sending again.
    sim.messages[draft]["isDraft"] = False
    assert await mail.send(draft, "task:send_mail:abc", to=[CALLER]) == draft
    assert sim.sends == [draft]


async def test_send_refuses_a_draft_changed_in_outlook(http: httpx.AsyncClient, sim: MailSim) -> None:
    mail = adapter(http)
    draft = await mail.create_draft([CALLER], "Re: access", "Done.")
    sim.messages[draft]["ccRecipients"] = [{"emailAddress": {"address": "boss@corp.example"}}]
    with pytest.raises(AdapterError, match="recipients changed"):
        await mail.send(draft, "k1", to=[CALLER], cc=[])
    sim.messages[draft]["ccRecipients"] = []
    sim.messages[draft]["body"] = {"contentType": "text", "content": "Done. Also, here is my password."}
    with pytest.raises(AdapterError, match="edited in Outlook"):
        await mail.send(draft, "k1", to=[CALLER], cc=[])
    assert sim.sends == []


async def test_text_edited_on_the_card_rebuilds_the_reply(http: httpx.AsyncClient, sim: MailSim) -> None:
    sim.add("orig")
    mail = adapter(http)
    draft = await mail.create_draft([CALLER], "", "First wording.", reply_to="orig")
    sent = await mail.send(draft, "k2", to=[CALLER], cc=[], body="Better wording.")
    assert sent != draft and draft not in sim.messages  # old agent draft discarded
    content = sim.messages[sent]["body"]["content"]
    assert "Better wording." in content and "Original: Hello" in content  # quote kept
    assert sim.sends == [sent]


async def test_send_of_a_missing_draft_is_not_reported_as_sent(http: httpx.AsyncClient, sim: MailSim) -> None:
    with pytest.raises(AdapterError, match="nothing was sent"):
        await adapter(http).send("never", "k3", to=[CALLER])


async def test_discard_only_touches_unsent_agent_drafts(http: httpx.AsyncClient, sim: MailSim) -> None:
    mail = adapter(http)
    sim.add("users-draft", "folder-drafts", isDraft=True)
    with pytest.raises(AdapterError, match="not created by the agent"):
        await mail.discard_draft("users-draft")
    sim.add("sent-one", "folder-sentitems")
    with pytest.raises(AdapterError, match="was sent"):
        await mail.discard_draft("sent-one")
    ours = await mail.create_draft([CALLER], "s", "b")
    await mail.discard_draft(ours)
    assert ours not in sim.messages
    await mail.discard_draft(ours)  # already gone: no error


async def test_move_resolves_paths_and_refuses_deleted_items(http: httpx.AsyncClient, sim: MailSim) -> None:
    sim.add("m1")
    mail = adapter(http)
    assert await mail.move("m1", "Inbox/Done") == "m1"  # immutable ids: same id after a move
    assert sim.messages["m1"]["parentFolderId"] == "folder-done"
    assert await mail.get_folder("m1") == "Done"
    with pytest.raises(AdapterError, match="hand-off"):
        await mail.move("m1", "Deleted Items")
    with pytest.raises(AdapterError, match="not found"):
        await mail.move("m1", "Inbox/Missing")
