"""Fake Teams and Slack (A03, A04)."""

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.adapters.base import ChatMessage, SlackEvent, ToolSpec
from agent.adapters.fakes._base import FIXTURES_DIR, FakeAdapter, parse_time
from agent.core.clock import Clock
from agent.core.ids import new_ulid

_TAGS = re.compile(r"<[^>]+>")


class FakeTeams(FakeAdapter):
    name = "teams"

    def __init__(self, clock: Clock, fixtures: Path = FIXTURES_DIR) -> None:
        super().__init__(clock, fixtures)
        data = self._load("teams/chats.json")
        self.me_id: str = data["me"]["id"]
        self.chats: dict[str, dict[str, Any]] = {c["id"]: c for c in data["chats"]}
        self.posted: list[dict[str, str]] = []
        self.transcripts: dict[str, str] = {}

    def deliver(self, chat_id: str, raw: dict[str, Any]) -> ChatMessage:
        """Deliver a new or edited Graph-style chat message now (tests)."""
        messages = self.chats[chat_id]["messages"]
        messages[:] = [m for m in messages if m["id"] != raw["id"]] + [raw]
        return self._to_message(self.chats[chat_id], raw)

    async def list_chat_messages(self, since: datetime) -> list[ChatMessage]:
        """Messages created, edited or deleted after `since` (mirrors Graph lastModifiedDateTime)."""
        self._check("list_chat_messages")
        out = []
        for chat in self.chats.values():
            for raw in chat["messages"]:
                changed = parse_time(raw.get("deletedDateTime") or raw["lastModifiedDateTime"])
                if changed > since:
                    out.append(self._to_message(chat, raw))
        return sorted(out, key=lambda m: m.sent_at)

    async def post(self, chat_id: str, body: str, tag: str) -> str:
        self._record("post", chat_id=chat_id, body=body, tag=tag)
        self.posted.append({"chat_id": chat_id, "body": body, "tag": tag})
        return f"posted-{new_ulid()}"

    async def get_transcript(self, meeting_id: str) -> str | None:
        self._check("get_transcript")
        return self.transcripts.get(meeting_id)

    def _to_message(self, chat: dict[str, Any], raw: dict[str, Any]) -> ChatMessage:
        created, modified = raw["createdDateTime"], raw["lastModifiedDateTime"]
        mentioned = {
            m["mentioned"]["user"]["id"]
            for m in raw.get("mentions", [])
            if m.get("mentioned", {}).get("user")
        }
        return ChatMessage(
            id=raw["id"],
            chat_id=chat["id"],
            chat_topic=chat.get("topic"),
            sender=raw["from"]["user"]["displayName"],
            sent_at=parse_time(created),
            edited_at=parse_time(modified)
            if modified != created and not raw.get("deletedDateTime")
            else None,
            deleted=raw.get("deletedDateTime") is not None,
            text=_TAGS.sub("", raw["body"]["content"]).strip(),
            mentions_me=self.me_id in mentioned,  # from the mention array, never from text
        )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="read_chat", mode="silent", description="Read chats"),
            ToolSpec(name="get_transcript", mode="silent", description="Read a meeting transcript"),
            ToolSpec(name="post_chat", mode="ask", description="Post in a chat"),
        ]


class FakeSlack(FakeAdapter):
    name = "slack"

    def __init__(self, clock: Clock, fixtures: Path = FIXTURES_DIR) -> None:
        super().__init__(clock, fixtures)
        self.dms: list[dict[str, Any]] = []
        self.thread_posts: list[dict[str, str]] = []
        self._inbox: list[SlackEvent] = []

    def deliver(self, kind: str, text: str = "", **fields: Any) -> SlackEvent:
        """The user does something in Slack now (tests)."""
        event = SlackEvent.model_validate(
            {
                "kind": kind,
                "user": fields.pop("user", "U-FAKE-ME"),
                "text": text,
                "at": self.clock.now_utc(),
                **fields,
            }
        )
        self._inbox.append(event)
        return event

    async def next_events(self) -> list[SlackEvent]:
        self._check("next_events")
        events, self._inbox = self._inbox, []
        return events

    async def send_dm(self, text: str, blocks: list[dict[str, Any]] | None = None) -> str:
        self._record("send_dm", text=text, blocks=blocks)
        ts = f"{self.clock.now_utc().timestamp():.6f}"
        self.dms.append({"ts": ts, "text": text, "blocks": blocks})
        return ts

    async def post_thread(self, thread_ts: str, text: str) -> str:
        self._record("post_thread", thread_ts=thread_ts, text=text)
        self.thread_posts.append({"thread_ts": thread_ts, "text": text})
        return f"{self.clock.now_utc().timestamp():.6f}"
