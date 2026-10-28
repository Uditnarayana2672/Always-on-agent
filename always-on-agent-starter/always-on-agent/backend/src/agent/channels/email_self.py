"""Email to self (F14): the last fallback. Always the user's own address, never anyone else.

The recipient is fixed at construction from config `me`; nothing in the text can change it. The
mail carries a console link instead of long content. Sent through Graph sendMail (not saved as a
draft first) or, without Graph, through Outlook desktop.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agent.adapters.base import HealthStatus
from agent.core.errors import AgentError

log = logging.getLogger(__name__)

SUBJECT = "Always-On Agent: update"
MAX_CHARS = 4000

SendMail = Callable[[str, str, str], Awaitable[None]]  # (to, subject, text)


def graph_sender(client: Any) -> SendMail:
    async def send(to: str, subject: str, text: str) -> None:
        await client.post(
            "me/sendMail",
            {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "text", "content": text},
                    "toRecipients": [{"emailAddress": {"address": to}}],
                },
                "saveToSentItems": False,
            },
        )

    return send


class EmailSelfChannel:
    name = "email_self"

    def __init__(self, me: list[str], send_mail: SendMail | None, console_url: str) -> None:
        addresses = [a.strip().lower() for a in me if "@" in a]
        self._to = addresses[0] if addresses else None
        self._send = send_mail
        self._console = console_url
        self._last_error: str | None = None

    def available(self) -> bool:
        return self._to is not None and self._send is not None

    def healthy(self) -> bool:
        return self.available() and self._last_error is None

    async def health(self) -> HealthStatus:
        if not self.available():
            return HealthStatus(connector=self.name, status="down", detail="no own address in config `me`")
        if self._last_error:
            return HealthStatus(connector=self.name, status="degraded", detail=self._last_error)
        return HealthStatus(connector=self.name, status="ok", detail="email to self")

    async def send(self, text: str) -> None:
        if self._to is None or self._send is None:
            raise AgentError("email to self is not set up (add your address to `me` in config.yaml)")
        body = text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"
        body += f"\n\nOpen the console: {self._console}"
        try:
            await self._send(self._to, SUBJECT, body)
        except AgentError as exc:
            self._last_error = str(exc)[:200]
            raise
        self._last_error = None
