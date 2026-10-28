"""Teams chats through Microsoft Graph (A03).

Reading
- Only chats active in the last teams.active_days days are polled, plus chats linked to tasks
  (the chat list can grow to thousands). At most teams.max_chats_per_poll chats per poll.
- Each chat keeps its own cursor (the newest lastModifiedDateTime seen) in kv; the poller commits
  the cursors only after the messages were ingested.
- A chat whose newest message is older than its cursor is skipped, except on a sweep every
  SWEEP_EVERY polls: edits and deletions of older messages do not move the chat's preview, so the
  sweep is what finds them.
- Edited means lastEditedDateTime is set. Reactions also change lastModifiedDateTime, and must not
  look like edits. Deleted messages keep no text (E29). System messages (members added, calls) are
  skipped. Mentions come from the mentions array, never from the text.

Writing
- post() refuses when the chat holds anyone the approval did not list (a guest or someone added
  since), so the gate's outside-recipient check always sees the real audience.
- Each post carries a hidden tag (agent:<hash of the idempotency key>). find_tagged() looks for the
  tag, or for the same text from the user, in the chat's latest messages: after a crash the outbox
  checks there before posting again (E18).

Transcripts need the chat data-rule level and OnlineMeetingTranscript.Read.All; otherwise
transcript() says plainly why it is not available (E30).
"""

import hashlib
import html
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel

from agent.adapters.base import ChatMessage, HealthStatus, ToolSpec, TranscriptResult
from agent.adapters.graph_client import Forbidden, GraphClient, NotFound
from agent.adapters.html_text import html_to_text
from agent.adapters.outlook_graph import parse_graph_time
from agent.core.config import DataRuleLevel
from agent.core.errors import AdapterError, AgentError

log = logging.getLogger(__name__)

CURSOR_KEY = "teams.cursor.{chat_id}"
SWEEP_KEY = "teams.polls"
SWEEP_EVERY = 6  # every sixth poll (30 min at 5-minute polling) re-reads quiet chats
MAX_MESSAGES_PER_CHAT = 200
TAG_SCAN = 50
TRANSCRIPT_SCOPE = "OnlineMeetingTranscript.Read.All"
SELF_CHAT = "48:notes"
_DATA_RANK = {"metadata": 0, "text": 1, "chat": 2, "images": 3}
_TAG = re.compile(r"agent:([0-9a-f]{16})")
_VTT_TIMING = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} --> ")
_VTT_VOICE = re.compile(r"<v\s+([^>]+)>(.*?)(?:</v>)?$")


class Kv(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str) -> None: ...


LinkedChats = Callable[[], Awaitable[set[str]]]


class ChatChanges(BaseModel):
    messages: list[ChatMessage]
    cursors: dict[str, str]  # chat id -> newest lastModifiedDateTime, stored on commit
    swept: bool = False


def tag_marker(tag: str) -> str:
    return "agent:" + hashlib.sha256(tag.encode("utf-8")).hexdigest()[:16]


def vtt_to_text(vtt: str) -> str:
    """WebVTT cues to 'Speaker: words' lines; consecutive lines of one speaker are joined."""
    out: list[tuple[str, str]] = []
    for line in vtt.splitlines():
        line = line.strip()
        if (
            not line
            or line == "WEBVTT"
            or _VTT_TIMING.match(line)
            or line.isdigit()
            or line.startswith("NOTE")
        ):
            continue
        match = _VTT_VOICE.match(line)
        speaker, words = (match.group(1).strip(), match.group(2).strip()) if match else ("", line)
        if out and out[-1][0] == speaker:
            out[-1] = (speaker, f"{out[-1][1]} {words}")
        else:
            out.append((speaker, words))
    return "\n".join(f"{s}: {w}" if s else w for s, w in out)


class GraphTeamsAdapter:
    """Implements TeamsAdapter over Graph for the signed-in user's chats."""

    name = "teams"

    def __init__(
        self,
        client: GraphClient,
        kv: Kv,
        now: Callable[[], datetime],
        data_level: DataRuleLevel = "metadata",
        scopes: list[str] | None = None,
        linked_chats: LinkedChats | None = None,
        active_days: int = 14,
        max_chats: int = 50,
    ) -> None:
        self._g = client
        self._kv = kv
        self._now = now
        self._data_level = data_level
        self._scopes = [s.rsplit("/", 1)[-1] for s in scopes or []]
        self._linked = linked_chats
        self._active = timedelta(days=active_days)
        self._max_chats = max_chats
        self._me_id: str | None = None

    async def health(self) -> HealthStatus:
        try:
            await self.me_id()
            await self._g.get("me/chats", {"$top": "1"})
        except AgentError as exc:
            return HealthStatus(connector=self.name, status="down", detail=str(exc)[:200])
        return HealthStatus(connector=self.name, status="ok", detail="Graph chats reachable")

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="read_chat", mode="silent", description="Read chats"),
            ToolSpec(name="get_transcript", mode="silent", description="Read a meeting transcript"),
            ToolSpec(name="post_chat", mode="ask", description="Post in a chat"),
        ]

    async def me_id(self) -> str:
        if self._me_id is None:
            me = await self._g.get("me", {"$select": "id"})
            self._me_id = str(me["id"])
        return self._me_id

    # ------------------------------------------------------------ reading

    async def chats_to_poll(self) -> list[dict[str, Any]]:
        """Active chats (newest first) plus linked task chats, capped at max_chats."""
        cutoff = self._now() - self._active
        out: dict[str, dict[str, Any]] = {}
        async for page in self._g.pages(
            "me/chats",
            {
                "$expand": "lastMessagePreview",
                "$orderby": "lastMessagePreview/createdDateTime desc",
                "$top": "50",
            },
        ):
            stop = False
            for chat in page.get("value", []):
                last = _preview_time(chat)
                if last is not None and last < cutoff:
                    stop = True
                    break
                out[str(chat["id"])] = chat
                if len(out) >= self._max_chats:
                    stop = True
                    break
            if stop:
                break
        linked = await self._linked() if self._linked else set()
        for chat_id in sorted(linked - set(out)):
            try:
                out[chat_id] = await self._g.get(f"me/chats/{chat_id}", {"$expand": "lastMessagePreview"})
            except NotFound:
                log.info("linked chat no longer visible", extra={"chat": chat_id})
        return list(out.values())

    async def changes(self, since: datetime) -> ChatChanges:
        """Messages created, edited or deleted after each chat's cursor (or `since` for a new chat)."""
        me = await self.me_id()
        polls = int(await self._kv.get(SWEEP_KEY) or 0) + 1
        await self._kv.set(SWEEP_KEY, str(polls))
        sweep = polls % SWEEP_EVERY == 0
        messages: list[ChatMessage] = []
        cursors: dict[str, str] = {}
        for chat in await self.chats_to_poll():
            chat_id = str(chat["id"])
            stored = await self._kv.get(CURSOR_KEY.format(chat_id=chat_id))
            cursor = parse_graph_time(stored) if stored else since
            preview = _preview_time(chat)
            if not sweep and preview is not None and preview <= cursor:
                continue
            raw = await self._messages_after(chat_id, cursor)
            newest = cursor
            for item in raw:
                modified = parse_graph_time(str(item.get("lastModifiedDateTime") or item["createdDateTime"]))
                newest = max(newest, modified)
                message = to_chat_message(chat, item, me)
                if message is not None:
                    messages.append(message)
            if newest > cursor:
                cursors[chat_id] = newest.isoformat()
        messages.sort(key=lambda m: m.edited_at or m.sent_at)
        return ChatChanges(messages=messages, cursors=cursors, swept=sweep)

    async def commit(self, changes: ChatChanges) -> None:
        for chat_id, value in changes.cursors.items():
            await self._kv.set(CURSOR_KEY.format(chat_id=chat_id), value)

    async def list_chat_messages(self, since: datetime) -> list[ChatMessage]:
        """Protocol read: every change after `since` in the polled chats (no cursors used or stored)."""
        me = await self.me_id()
        out: list[ChatMessage] = []
        for chat in await self.chats_to_poll():
            for item in await self._messages_after(str(chat["id"]), since):
                message = to_chat_message(chat, item, me)
                if message is not None:
                    out.append(message)
        return sorted(out, key=lambda m: m.sent_at)

    async def _messages_after(self, chat_id: str, after: datetime) -> list[dict[str, Any]]:
        return await self._g.get_all(
            f"me/chats/{chat_id}/messages",
            {
                "$top": "50",
                "$orderby": "lastModifiedDateTime desc",
                "$filter": f"lastModifiedDateTime gt {_iso(after)}",
            },
            max_items=MAX_MESSAGES_PER_CHAT,
        )

    async def chat_members(self, chat_id: str) -> list[str]:
        """Members' addresses, the user included. A member without a visible address (federated
        or external) is returned as external:<id>, which the gate treats as outside the company."""
        out = []
        for member in await self._g.get_all(f"me/chats/{chat_id}/members"):
            email = str(member.get("email") or "").lower()
            out.append(email or f"external:{member.get('userId') or member.get('id')}")
        return out

    # ------------------------------------------------------------ writing (via the outbox only)

    async def post(self, chat_id: str, body: str, tag: str, recipients: list[str] | None = None) -> str:
        if chat_id == SELF_CHAT:
            raise AdapterError("the self chat is for notifications, not task messages")
        if recipients is not None:
            me = {a.lower() for a in await self._my_addresses()}
            members = {m for m in await self.chat_members(chat_id) if m not in me}
            uncovered = members - {r.lower() for r in recipients}
            if uncovered:
                raise AdapterError(
                    f"the chat has {len(uncovered)} member(s) this approval did not cover; review it again"
                )
        content = (
            html.escape(body).replace("\n", "<br>") + f'<span style="display:none">{tag_marker(tag)}</span>'
        )
        posted = await self._g.post(
            f"me/chats/{chat_id}/messages", {"body": {"contentType": "html", "content": content}}
        )
        return str(posted.get("id") or "")

    async def find_tagged(self, chat_id: str, tag: str, body: str | None = None) -> str | None:
        """Did a post with this tag (or, if Teams stripped it, this exact text from the user) go out?"""
        me = await self.me_id()
        marker = tag_marker(tag)
        wanted = " ".join(body.split()) if body else None
        recent = await self._g.get(f"me/chats/{chat_id}/messages", {"$top": str(TAG_SCAN)})
        for item in recent.get("value", []):
            content = str((item.get("body") or {}).get("content") or "")
            if marker in content:
                return str(item["id"])
            sender = ((item.get("from") or {}).get("user") or {}).get("id")
            if wanted and sender == me and " ".join(html_to_text(content).split()) == wanted:
                return str(item["id"])
        return None

    async def _my_addresses(self) -> list[str]:
        me = await self._g.get("me", {"$select": "mail,userPrincipalName"})
        return [str(v) for v in (me.get("mail"), me.get("userPrincipalName")) if v]

    # ------------------------------------------------------------ transcripts

    async def transcript(self, meeting_id: str) -> TranscriptResult:
        if _DATA_RANK[self._data_level] < _DATA_RANK["chat"]:
            return TranscriptResult(
                available=False, reason="transcripts need the chat data rule, not yet approved"
            )
        if TRANSCRIPT_SCOPE not in self._scopes:
            return TranscriptResult(
                available=False, reason="the transcript permission is not granted (needs admin consent)"
            )
        try:
            meeting = await self._meeting(meeting_id)
            if meeting is None:
                return TranscriptResult(available=False, reason="meeting not found or not organised by you")
            listing = await self._g.get(f"me/onlineMeetings/{meeting}/transcripts")
            items = listing.get("value") or []
            if not items:
                return TranscriptResult(
                    available=False, reason="no transcript: transcription was not turned on for this meeting"
                )
            latest = max(items, key=lambda t: str(t.get("createdDateTime") or ""))
            vtt = await self._g.get_text(
                f"me/onlineMeetings/{meeting}/transcripts/{latest['id']}/content", {"$format": "text/vtt"}
            )
        except Forbidden:
            return TranscriptResult(available=False, reason="no permission to read this meeting's transcript")
        except NotFound:
            return TranscriptResult(available=False, reason="meeting not found or not organised by you")
        return TranscriptResult(available=True, text=vtt_to_text(vtt))

    async def get_transcript(self, meeting_id: str) -> str | None:
        result = await self.transcript(meeting_id)
        return result.text if result.available else None

    async def _meeting(self, meeting_id: str) -> str | None:
        if not meeting_id.startswith("http"):
            return meeting_id
        found = await self._g.get(
            "me/onlineMeetings", {"$filter": f"JoinWebUrl eq '{meeting_id.replace(chr(39), chr(39) * 2)}'"}
        )
        items = found.get("value") or []
        return str(items[0]["id"]) if items else None


def to_chat_message(chat: dict[str, Any], raw: dict[str, Any], me_id: str) -> ChatMessage | None:
    """A Graph chatMessage as a ChatMessage; None for system messages."""
    if str(raw.get("messageType") or "message") != "message":
        return None
    sender = raw.get("from") or {}
    user = sender.get("user") or sender.get("application") or {}
    deleted = raw.get("deletedDateTime") is not None
    edited = raw.get("lastEditedDateTime")
    mentioned = {
        str(((m.get("mentioned") or {}).get("user") or {}).get("id") or "") for m in raw.get("mentions") or []
    }
    content = str((raw.get("body") or {}).get("content") or "")
    is_html = str((raw.get("body") or {}).get("contentType") or "html").lower() == "html"
    text = "" if deleted else (html_to_text(content) if is_html else content.strip())
    return ChatMessage(
        id=str(raw["id"]),
        chat_id=str(chat["id"]),
        chat_topic=chat.get("topic"),
        sender=str(user.get("displayName") or "(unknown)"),
        sender_id=str(user.get("id")) if user.get("id") else None,
        from_me=bool(user.get("id")) and str(user.get("id")) == me_id,
        sent_at=parse_graph_time(str(raw["createdDateTime"])),
        edited_at=parse_graph_time(str(edited)) if edited and not deleted else None,
        deleted=deleted,
        text=_TAG.sub("", text).strip(),
        mentions_me=me_id in mentioned,
    )


def _preview_time(chat: dict[str, Any]) -> datetime | None:
    preview = chat.get("lastMessagePreview") or {}
    value = preview.get("createdDateTime") or chat.get("lastUpdatedDateTime")
    return parse_graph_time(str(value)) if value else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
