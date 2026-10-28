import re
from collections.abc import AsyncIterator
from datetime import datetime

import httpx
import pytest
import respx

from agent.adapters.graph_client import GRAPH_BASE
from agent.adapters.teams import SWEEP_EVERY, GraphTeamsAdapter, tag_marker, vtt_to_text
from agent.core.config import DataRuleLevel
from agent.core.errors import AdapterError
from tests.adapters.graph_helpers import NOW, Kv, client
from tests.adapters.graph_sim import TeamsSim

ME_MAIL = "test.user@corp.example"
ANNA = "anna.keller@corp.example"
EARLIER = datetime(2026, 3, 25, 3, 0, tzinfo=NOW.tzinfo)


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def router() -> AsyncIterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def sim(router: respx.MockRouter) -> TeamsSim:
    sim = TeamsSim(router)
    sim.add_chat("chat-active", "2026-03-25T04:00:00Z", [ME_MAIL, ANNA], topic="INC30114872 login")
    sim.add_chat("chat-old", "2026-02-01T10:00:00Z", [ME_MAIL, "karim.haddad@corp.example"])
    sim.add_chat("chat-task", "2026-01-10T10:00:00Z", [ME_MAIL, ANNA])
    return sim


def teams(
    http: httpx.AsyncClient,
    kv: Kv | None = None,
    level: DataRuleLevel = "chat",
    scopes: list[str] | None = None,
    linked: set[str] | None = None,
) -> GraphTeamsAdapter:
    async def linked_chats() -> set[str]:
        return linked or set()

    return GraphTeamsAdapter(
        client(http, "teams"),
        kv or Kv(),
        now=lambda: NOW,
        data_level=level,
        scopes=scopes if scopes is not None else ["Chat.ReadWrite", "OnlineMeetingTranscript.Read.All"],
        linked_chats=linked_chats,
    )


async def test_only_active_and_linked_chats_are_polled(http: httpx.AsyncClient, sim: TeamsSim) -> None:
    chats = await teams(http, linked={"chat-task"}).chats_to_poll()
    assert [c["id"] for c in chats] == ["chat-active", "chat-task"]  # chat-old is 52 days quiet


async def test_edits_reactions_deletes_system_and_mentions(http: httpx.AsyncClient, sim: TeamsSim) -> None:
    sim.add_message("chat-active", "m1", "2026-03-25T03:10:00Z", "plain")
    # A reaction changes lastModified but is not an edit.
    sim.add_message(
        "chat-active", "m2", "2026-03-25T03:11:00Z", "reacted", lastModifiedDateTime="2026-03-25T03:20:00Z"
    )
    sim.add_message(
        "chat-active",
        "m3",
        "2026-03-25T03:12:00Z",
        "fixed typo",
        lastModifiedDateTime="2026-03-25T03:21:00Z",
        lastEditedDateTime="2026-03-25T03:21:00Z",
    )
    sim.add_message(
        "chat-active",
        "m4",
        "2026-03-25T03:13:00Z",
        "secret",
        deletedDateTime="2026-03-25T03:22:00Z",
        lastModifiedDateTime="2026-03-25T03:22:00Z",
    )
    sim.add_message(
        "chat-active", "m5", "2026-03-25T03:14:00Z", "Karim joined", messageType="systemEventMessage"
    )
    sim.add_message(
        "chat-active",
        "m6",
        "2026-03-25T03:15:00Z",
        '<at id="0">Test User</at> please look',
        mentions=[{"id": 0, "mentioned": {"user": {"id": TeamsSim.ME}}}],
    )
    sim.add_message("chat-active", "m7", "2026-03-25T03:16:00Z", "@Test User in text only")
    changes = await teams(http).changes(EARLIER)
    by_id = {m.id: m for m in changes.messages}
    assert set(by_id) == {"m1", "m2", "m3", "m4", "m6", "m7"}  # system message skipped
    assert by_id["m2"].edited_at is None
    assert by_id["m3"].edited_at is not None and by_id["m3"].text == "fixed typo"
    assert by_id["m4"].deleted and by_id["m4"].text == ""
    assert by_id["m6"].mentions_me and by_id["m6"].text == "Test User please look"
    assert not by_id["m7"].mentions_me  # mentions come from the array, never the text


async def test_cursors_commit_after_ingest_and_quiet_chats_wait_for_the_sweep(
    http: httpx.AsyncClient, sim: TeamsSim
) -> None:
    kv = Kv()
    adapter = teams(http, kv, linked={"chat-task"})
    sim.add_message("chat-active", "m1", "2026-03-25T03:10:00Z", "hello")
    first = await adapter.changes(EARLIER)
    assert [m.id for m in first.messages] == ["m1"] and kv.data.get("teams.cursor.chat-active") is None
    await adapter.commit(first)
    assert kv.data["teams.cursor.chat-active"].startswith("2026-03-25T03:10")
    sim.message_reads.clear()
    sim.chats["chat-active"]["lastMessagePreview"]["createdDateTime"] = "2026-03-25T03:10:00Z"
    kv.data["teams.cursor.chat-task"] = "2026-03-25T00:00:00+00:00"
    # Nothing new: neither chat's preview is past its cursor, so no messages are read ...
    again = await adapter.changes(EARLIER)
    assert again.messages == [] and sim.message_reads == []
    # ... until the sweep, which finds an edit to an old message that the preview never shows.
    sim.add_message(
        "chat-active",
        "m1",
        "2026-03-25T03:10:00Z",
        "hello again",
        lastModifiedDateTime="2026-03-25T04:00:00Z",
        lastEditedDateTime="2026-03-25T04:00:00Z",
    )
    kv.data["teams.polls"] = str(SWEEP_EVERY - 1)
    swept = await adapter.changes(EARLIER)
    assert swept.swept and [m.id for m in swept.messages] == ["m1"] and swept.messages[0].edited_at


async def test_post_refuses_members_the_approval_did_not_cover(
    http: httpx.AsyncClient, sim: TeamsSim
) -> None:
    adapter = teams(http)
    sim.members["chat-active"].append({"email": "", "userId": "guest-1"})  # a federated guest
    with pytest.raises(AdapterError, match="did not cover"):
        await adapter.post("chat-active", "Fixed now.", "tag-1", recipients=[ANNA])
    assert sim.posted == []
    sim.members["chat-active"].pop()
    posted = await adapter.post("chat-active", "Fixed now.\nPlease retry.", "tag-1", recipients=[ANNA])
    assert posted and tag_marker("tag-1") in sim.posted[0][1] and "<br>" in sim.posted[0][1]
    assert await adapter.find_tagged("chat-active", "tag-1") == posted
    assert await adapter.find_tagged("chat-active", "tag-2") is None
    # Teams stripped the hidden tag: the same text from the user still counts as already posted.
    sim.messages["chat-active"][-1]["body"]["content"] = "<p>Fixed now.<br>Please retry.</p>"
    assert await adapter.find_tagged("chat-active", "tag-1", body="Fixed now.\nPlease retry.") == posted
    with pytest.raises(AdapterError, match="self chat"):
        await adapter.post("48:notes", "x", "t", recipients=[])


async def test_chat_members_without_an_address_count_as_outside(
    http: httpx.AsyncClient, sim: TeamsSim
) -> None:
    sim.members["chat-old"].append({"email": None, "userId": "fed-9"})
    assert "external:fed-9" in await teams(http).chat_members("chat-old")


async def test_transcript_reasons_and_text(
    http: httpx.AsyncClient, router: respx.MockRouter, sim: TeamsSim
) -> None:
    assert "data rule" in (await teams(http, level="text").transcript("meet-1")).reason
    assert "admin consent" in (await teams(http, scopes=["Chat.ReadWrite"]).transcript("meet-1")).reason
    base = re.escape(GRAPH_BASE)
    router.get(url__regex=rf"{base}/me/onlineMeetings/meet-1/transcripts$").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    none = await teams(http).transcript("meet-1")
    assert not none.available and "not turned on" in none.reason
    router.get(url__regex=rf"{base}/me/onlineMeetings/meet-2/transcripts$").mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "t1", "createdDateTime": "2026-03-25T05:00:00Z"}]}
        )
    )
    router.get(url__regex=rf"{base}/me/onlineMeetings/meet-2/transcripts/t1/content").mock(
        return_value=httpx.Response(
            200, text="WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Anna Keller>The report is late.</v>\n"
        )
    )
    found = await teams(http).transcript("meet-2")
    assert found.available and found.text == "Anna Keller: The report is late."
    router.get(url__regex=rf"{base}/me/onlineMeetings/meet-3/transcripts$").mock(
        return_value=httpx.Response(403)
    )
    assert "permission" in (await teams(http).transcript("meet-3")).reason
    assert await teams(http).get_transcript("meet-1") is None


def test_vtt_joins_one_speakers_lines() -> None:
    vtt = (
        "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\n<v Anna>Hello</v>\n\n"
        "2\n00:00:02.000 --> 00:00:03.000\n<v Anna>again</v>\n\n"
        "00:00:03.000 --> 00:00:04.000\n<v Karim>Hi</v>\n"
    )
    assert vtt_to_text(vtt) == "Anna: Hello again\nKarim: Hi"
