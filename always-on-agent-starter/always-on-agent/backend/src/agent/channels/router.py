"""Notification router (F14): which channel a notification goes out on.

The Notifier (channels.notifier) decides when a push goes out (push cap, quiet hours, batching);
the router decides where. It is the Notifier's sink.

Chain: the configured primary first, then Slack -> Teams self chat (only once Q4 is verified) ->
email to self. A channel whose health is down is skipped; a channel that fails while sending is
marked and the next one is tried, so a dropped Slack socket costs one try, not the message (E31).
The first message after a fallback says so once, so the user knows why it arrived somewhere else.
If every channel fails, NotDelivered is raised and the job retries; nothing is silently dropped.
"""

import logging
from collections.abc import Sequence
from typing import Any, Literal, Protocol

from agent.adapters.base import HealthStatus
from agent.core.errors import AgentError

log = logging.getLogger(__name__)

ChannelName = Literal["slack", "teams_self", "email_self"]
CHAIN: tuple[ChannelName, ...] = ("slack", "teams_self", "email_self")
_PLACE = {"slack": "Slack", "teams_self": "Teams", "email_self": "email"}


class NotDelivered(AgentError):
    """No channel could deliver the notification."""


class NotifyChannel(Protocol):
    name: str

    async def health(self) -> HealthStatus: ...
    async def send(self, text: str) -> None: ...


class NotificationRouter:
    def __init__(self, channels: Sequence[NotifyChannel], primary: ChannelName = "slack") -> None:
        by_name = {c.name: c for c in channels}
        order = [primary, *[n for n in CHAIN if n != primary]]
        self._chain = [by_name[n] for n in order if n in by_name]
        self._primary = primary
        self.last_channel: str | None = None
        self._fallback_noted = False

    def add(self, channel: NotifyChannel) -> None:
        """A channel made after the router (it needs a client the adapters build); keeps the chain order."""
        by_name = {c.name: c for c in [*self._chain, channel]}
        order = [self._primary, *[n for n in CHAIN if n != self._primary]]
        self._chain = [by_name[n] for n in order if n in by_name]

    async def send_card(self, text: str, blocks: list[dict[str, Any]]) -> None:
        """Like send, with a card for channels that can show one; the others get the text."""
        await self.send(text, blocks)

    async def send(self, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        errors: list[str] = []
        for channel in self._chain:
            status = await channel.health()
            if status.status == "down":
                errors.append(f"{channel.name}: {status.detail}")
                continue
            message = text
            if channel.name != self._primary and not self._fallback_noted:
                place = _PLACE.get(self._primary, self._primary)
                message = f"({place} is not reachable, so this came here.)\n{text}"
            try:
                send_card = getattr(channel, "send_card", None)
                if blocks and send_card is not None and message == text:
                    await send_card(message, blocks)
                else:
                    await channel.send(message)
            except Exception as exc:
                log.warning(
                    "notification channel failed",
                    extra={"channel": channel.name, "error": type(exc).__name__},
                )
                errors.append(f"{channel.name}: {type(exc).__name__}")
                continue
            self._fallback_noted = channel.name != self._primary
            self.last_channel = channel.name
            return
        raise NotDelivered("no channel could deliver: " + "; ".join(errors))
