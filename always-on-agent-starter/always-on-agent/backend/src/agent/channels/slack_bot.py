"""Slack channel (A04, F14): Socket Mode bot inside agentd.

What it does
- DMs the user cards (cards.py); approval cards are an ApprovalChannel for the approval service.
- One thread per task, titled "<task id> - <short title>" (en dash); the thread ts is stored in task_links
  (kind slack_thread), so replies in it route to the task (F03). Replies are stored as events and
  acknowledged in the thread; R17 acts on them later.
- Buttons call the approval service with the hash from the card. Double clicks and Slack's
  redelivery resolve once (the service compares and sets; repeats here are also dropped by id).
  Edit opens a box with the text; saving makes a new version that needs its own approval.
- /agent status | pause | resume | brief | tasks | undo <id> | help.

Who is listened to
- Only the configured user (slack.user_id). Anyone else's click, command or message is refused
  or ignored: a colleague in a shared channel cannot approve the user's emails.
- The bot's own messages, edits and system subtypes are ignored, so acknowledgements never loop.

Health
- Socket Mode reconnects by itself. The watchdog marks the connection degraded as soon as it drops
  and down after DOWN_AFTER (2 minutes); the router then falls back to Teams or email (E31).
"""

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from agent.adapters.base import HealthStatus, SlackEvent
from agent.channels import cards
from agent.channels.base import ApprovalCard
from agent.core.clock import Clock
from agent.core.errors import AgentError, ApprovalConflict
from agent.runtime.coordinator import IngestResult, Source

log = logging.getLogger(__name__)

DOWN_AFTER = timedelta(minutes=2)
WATCH_SECONDS = 15
SEEN_MAX = 500
LINK_KIND = "slack_thread"
HELP = (
    "Commands: `/agent status`, `/agent pause`, `/agent resume`, `/agent brief`, "
    "`/agent tasks`, `/agent undo <id>`"
)


class SlackWeb(Protocol):
    """The parts of slack_sdk's AsyncWebClient the bot uses."""

    async def chat_postMessage(self, **kwargs: Any) -> Any: ...
    async def chat_update(self, **kwargs: Any) -> Any: ...
    async def chat_postEphemeral(self, **kwargs: Any) -> Any: ...
    async def views_open(self, **kwargs: Any) -> Any: ...
    async def conversations_open(self, **kwargs: Any) -> Any: ...


class Approvals(Protocol):
    async def approve(self, action_id: str, expected_hash: str, actor: str = ...) -> Any: ...
    async def reject(self, action_id: str, reason: str = ..., actor: str = ...) -> Any: ...
    async def edit(
        self, action_id: str, new_content: str, actor: str = ..., expected_hash: str | None = ...
    ) -> Any: ...
    async def card(self, action_id: str) -> tuple[ApprovalCard, str]: ...


class Links(Protocol):
    """MemoryRepo's task link methods."""

    async def link(self, task_id: str, kind: str, external_id: str) -> None: ...
    async def links(self, task_id: str) -> list[dict[str, Any]]: ...


class Ingest(Protocol):
    async def __call__(self, raw: Any, source: Source) -> IngestResult: ...


class Commands(Protocol):
    async def status(self) -> str: ...
    async def pause(self) -> str: ...
    async def resume(self) -> str: ...
    async def brief(self) -> str: ...
    async def tasks(self) -> str: ...
    async def undo(self, audit_id: int) -> str: ...


class HealthSink(Protocol):
    async def report(self, connector: str, status: str, error: str | None = None) -> None: ...


Respond = Callable[[str], Awaitable[None]]


class ConnectionMonitor:
    """Tracks the Socket Mode connection: ok while connected, degraded when it drops, down after 2 min."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.connected = False
        self.since: datetime | None = None  # when the current state began

    def set(self, connected: bool) -> bool:
        """Record the state; True when it changed."""
        if connected == self.connected and self.since is not None:
            return False
        self.connected = connected
        self.since = self._clock.now_utc()
        return True

    def status(self) -> HealthStatus:
        if self.connected:
            return HealthStatus(connector="slack", status="ok", detail="connected")
        if self.since is None:
            return HealthStatus(connector="slack", status="down", detail="not connected yet")
        gone = self._clock.now_utc() - self.since
        if gone >= DOWN_AFTER:
            return HealthStatus(
                connector="slack",
                status="down",
                detail=f"disconnected for {int(gone.total_seconds() // 60)} min",
            )
        return HealthStatus(connector="slack", status="degraded", detail="reconnecting")


class SlackBot:
    name = "slack"

    def __init__(
        self,
        web: SlackWeb,
        user_id: str,
        clock: Clock,
        console_base: str,
        approvals: Approvals | None = None,
        ingest: Ingest | None = None,
        links: Links | None = None,
        commands: Commands | None = None,
        health: HealthSink | None = None,
        undo: Callable[[int, str], Awaitable[Any]] | None = None,
    ) -> None:
        if not user_id:
            raise AgentError("slack.user_id is not set; the bot needs to know whose DMs it serves")
        self._web = web
        self._user = user_id
        self._clock = clock
        self._console = console_base
        self._approvals = approvals
        self._ingest = ingest
        self._links = links
        self._commands = commands
        self._health = health
        self._undo = undo
        self.monitor = ConnectionMonitor(clock)
        self._dm: str | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._thread_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------ outgoing

    async def health(self) -> HealthStatus:
        return self.monitor.status()

    def healthy(self) -> bool:
        return self.monitor.status().status == "ok"

    async def dm_channel(self) -> str:
        if self._dm is None:
            opened = await self._web.conversations_open(users=self._user)
            self._dm = str(opened["channel"]["id"])
        return self._dm

    async def post(
        self, text: str, blocks: list[dict[str, Any]] | None = None, thread_ts: str | None = None
    ) -> str:
        kwargs: dict[str, Any] = {"channel": await self.dm_channel(), "text": text, "unfurl_links": False}
        if blocks is not None:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        sent = await self._web.chat_postMessage(**kwargs)
        return str(sent["ts"])

    async def send(self, text: str) -> None:
        """NotifySink: a plain DM. Text may hold task titles from mail, so it is escaped."""
        await self.post(cards.escape(text))

    async def send_dm(self, text: str, blocks: list[dict[str, Any]] | None = None) -> str:
        return await self.post(text, blocks)

    async def post_thread(self, thread_ts: str, text: str) -> str:
        return await self.post(cards.escape(text), thread_ts=thread_ts)

    async def ensure_thread(self, task_id: str, title: str) -> str:
        """The task's thread ts, posting the root message the first time (one root even when racing)."""
        lock = self._thread_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            if self._links is not None:
                for link in await self._links.links(task_id):
                    if link.get("kind") == LINK_KIND:
                        return str(link["external_id"])
            text, blocks = cards.thread_root_blocks(task_id, title, self._console)
            ts = await self.post(text, blocks)
            if self._links is not None:
                await self._links.link(task_id, LINK_KIND, ts)
            return ts

    async def post_in_task(self, task_id: str, title: str, text: str) -> str:
        return await self.post_thread(await self.ensure_thread(task_id, title), text)

    # ------------------------------------------------------------ ApprovalChannel

    async def post_card(self, card: ApprovalCard) -> str:
        text, blocks = cards.approval_blocks(card, self._console, self._clock.to_local)
        channel = await self.dm_channel()
        sent = await self._web.chat_postMessage(channel=channel, text=text, blocks=blocks, unfurl_links=False)
        return f"{channel}|{sent['ts']}"

    async def update_card(self, ref: str, card: ApprovalCard, status: str, note: str | None) -> None:
        channel, _, ts = ref.partition("|")
        text, blocks = cards.approval_outcome_blocks(card, status, note, self._console)
        await self._web.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)

    # ------------------------------------------------------------ incoming: buttons

    def _first_time(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > SEEN_MAX:
            self._seen.popitem(last=False)
        return True

    async def on_action(self, body: dict[str, Any], respond: Respond) -> None:
        user = str((body.get("user") or {}).get("id") or "")
        actions = body.get("actions") or []
        if not actions:
            return
        action = actions[0]
        action_id = str(action.get("action_id") or "")
        if action_id == cards.OPEN:
            return  # a link button; Slack opens the URL
        if user != self._user:
            await respond("Only the owner of this agent can use these buttons.")
            return
        key = f"{action_id}:{action.get('value')}:{action.get('action_ts') or body.get('trigger_id')}"
        if not self._first_time(key):
            return
        try:
            value = cards.parse_value(str(action.get("value") or ""))
        except ValueError:
            await respond("That button is from an old message; open the console instead.")
            return
        try:
            if action_id == cards.UNDO:
                await self._do_undo(int(value["u"]), respond)
                return
            if self._approvals is None:
                await respond("Approvals are not running right now; try the console.")
                return
            action_ref, hash_ = str(value["a"]), str(value["h"])
            if action_id == cards.APPROVE:
                outcome = await self._approvals.approve(action_ref, hash_, actor="slack")
                if not getattr(outcome, "changed", True):
                    await respond("Already approved.")
            elif action_id == cards.REJECT:
                await self._approvals.reject(action_ref, "rejected in Slack", actor="slack")
            elif action_id == cards.EDIT:
                await self._open_edit(body, action_ref, hash_, respond)
        except ApprovalConflict as exc:
            await respond(f"Nothing was done: {exc}.")
        except (KeyError, ValueError):
            await respond("That request no longer exists.")

    async def _open_edit(self, body: dict[str, Any], action_id: str, hash_: str, respond: Respond) -> None:
        assert self._approvals is not None
        card, status = await self._approvals.card(action_id)
        if status not in ("proposed", "edited"):
            await respond(f"This request is already {status}.")
            return
        if card.content_hash != hash_:
            await respond("The text changed since this card was shown; use the newest card.")
            return
        if len(card.content) > cards.EDIT_MAX:
            await respond("This text is too long to edit in Slack; edit it in the console.")
            return
        await self._web.views_open(trigger_id=body.get("trigger_id"), view=cards.edit_modal(card))

    async def on_view_submission(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """Returns a response for Slack: None closes the box, an errors dict keeps it open."""
        user = str((body.get("user") or {}).get("id") or "")
        view = body.get("view") or {}
        if user != self._user:
            return {"response_action": "errors", "errors": {"text": "Only the owner of this agent can edit."}}
        try:
            meta = cards.parse_value(str(view.get("private_metadata") or ""))
            text = str(view["state"]["values"]["text"]["value"]["value"] or "")
        except (KeyError, TypeError, ValueError):
            return {"response_action": "errors", "errors": {"text": "This box is out of date; reopen it."}}
        if not text.strip():
            return {"response_action": "errors", "errors": {"text": "The text is empty."}}
        if self._approvals is None:
            return {"response_action": "errors", "errors": {"text": "Approvals are not running right now."}}
        try:
            await self._approvals.edit(str(meta["a"]), text, actor="slack", expected_hash=str(meta["h"]))
        except ApprovalConflict as exc:
            return {"response_action": "errors", "errors": {"text": str(exc)[:150]}}
        return None

    async def _do_undo(self, audit_id: int, respond: Respond) -> None:
        if self._undo is None:
            await respond("Undo is not available right now; use the console.")
            return
        result = await self._undo(audit_id, "slack")
        await respond(str(getattr(result, "message", result)))

    # ------------------------------------------------------------ incoming: messages

    async def on_message(self, event: dict[str, Any]) -> IngestResult | None:
        """A DM or a reply in a task thread. Returns the ingest result (None when ignored)."""
        if event.get("bot_id") or event.get("subtype"):
            return None  # bot posts (our own acks), edits, deletes, joins: never routed
        if str(event.get("user") or "") != self._user:
            return None
        if event.get("channel_type") not in (None, "im"):
            return None
        ts = str(event.get("ts") or "")
        thread_ts = event.get("thread_ts")
        is_reply = bool(thread_ts) and thread_ts != ts
        slack_event = SlackEvent(
            kind="thread_reply" if is_reply else "dm",
            user=self._user,
            text=str(event.get("text") or ""),
            thread_ts=str(thread_ts) if is_reply else None,
            at=_ts_time(ts) if ts else self._clock.now_utc(),
        )
        if self._ingest is None:
            return None
        result = await self._ingest(slack_event, "slack")
        if result.duplicate:
            return result
        if is_reply:
            if result.task_ids:
                await self.post(f"Noted on {', '.join(result.task_ids)}.", thread_ts=str(thread_ts))
            else:
                await self.post(
                    "Noted. I could not tell which task this belongs to.", thread_ts=str(thread_ts)
                )
        return result

    # ------------------------------------------------------------ incoming: /agent

    async def on_command(self, body: dict[str, Any]) -> str:
        if str(body.get("user_id") or "") != self._user:
            return "Only the owner of this agent can use /agent."
        words = str(body.get("text") or "").split()
        if not words or self._commands is None:
            return HELP
        verb, rest = words[0].lower(), words[1:]
        try:
            if verb == "status":
                return await self._commands.status()
            if verb == "pause":
                return await self._commands.pause()
            if verb == "resume":
                return await self._commands.resume()
            if verb == "brief":
                return await self._commands.brief()
            if verb == "tasks":
                return await self._commands.tasks()
            if verb == "undo":
                if len(rest) != 1 or not rest[0].isdigit():
                    return "Use `/agent undo <id>` with the number from the digest."
                return await self._commands.undo(int(rest[0]))
        except AgentError as exc:
            return f"That did not work: {exc}"
        return HELP

    # ------------------------------------------------------------ connection

    async def mark(self, connected: bool) -> None:
        if self.monitor.set(connected) and self._health is not None:
            status = self.monitor.status()
            await self._health.report("slack", status.status, None if connected else status.detail)

    async def watch(self, is_connected: Callable[[], Awaitable[bool]], stop: asyncio.Event) -> None:
        """Poll the socket state; report changes and the move to down after two minutes."""
        reported_down = False
        while not stop.is_set():
            try:
                up = await is_connected()
            except Exception:
                up = False
            await self.mark(up)
            status = self.monitor.status()
            if status.status == "down" and not reported_down and self._health is not None:
                await self._health.report("slack", "down", status.detail)
                reported_down = True
            if up:
                reported_down = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=WATCH_SECONDS)


def _ts_time(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), UTC)


# ---------------------------------------------------------------- Socket Mode wiring


class SlackRunner:
    """Runs the bot in Socket Mode: Bolt app, handlers, auto-reconnect and the health watchdog."""

    def __init__(self, bot: SlackBot, bot_token: str, app_token: str) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp

        self.bot = bot
        self.app = AsyncApp(token=bot_token)
        self._handler = AsyncSocketModeHandler(self.app, app_token)
        self._stop = asyncio.Event()
        self._watch: asyncio.Task[None] | None = None
        self._register()

    def _register(self) -> None:
        import re

        app, bot = self.app, self.bot

        @app.action(re.compile(r"^(approval_approve|approval_edit|approval_reject|undo|open_console)$"))
        async def _action(ack: Any, body: dict[str, Any], respond: Any) -> None:
            await ack()

            async def say(text: str) -> None:
                await respond(text=text, response_type="ephemeral", replace_original=False)

            await bot.on_action(body, say)

        @app.view("approval_edit_submit")
        async def _view(ack: Any, body: dict[str, Any]) -> None:
            response = await bot.on_view_submission(body)
            await ack(**(response or {}))

        @app.command("/agent")
        async def _command(ack: Any, body: dict[str, Any]) -> None:
            await ack()
            text = await bot.on_command(body)
            await bot.send(text)

        @app.event("message")
        async def _message(event: dict[str, Any]) -> None:
            await bot.on_message(event)

    async def start(self) -> None:
        await self._handler.connect_async()  # type: ignore[no-untyped-call]
        await self.bot.mark(True)

        async def connected() -> bool:
            client = self._handler.client
            return bool(await client.is_connected())

        self._watch = asyncio.create_task(self.bot.watch(connected, self._stop))

    async def stop(self) -> None:
        self._stop.set()
        if self._watch is not None:
            await self._watch
        await self._handler.close_async()  # type: ignore[no-untyped-call]
        await self.bot.mark(False)
