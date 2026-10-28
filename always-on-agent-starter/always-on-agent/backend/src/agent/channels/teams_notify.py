"""Teams self-chat notifications (F14, A03): the first fallback when Slack is down.

Graph can post to the user's own "notes to self" chat (48:notes) on some tenants only. Phase 0 asks
(Q4); until teams.self_note_verified is true this channel says it is unavailable and the router
skips it. Only the user ever sees these messages, so they are notifications, not tool actions.
"""

import html
from typing import Any

from agent.adapters.base import HealthStatus
from agent.adapters.graph_client import GraphClient
from agent.core.errors import AgentError

SELF_CHAT = "48:notes"
MAX_CHARS = 20000


class TeamsSelfChannel:
    name = "teams_self"

    def __init__(self, client: GraphClient | None, verified: bool) -> None:
        self._g = client
        self._verified = verified
        self._last_error: str | None = None

    def available(self) -> bool:
        return self._verified and self._g is not None

    def healthy(self) -> bool:
        return self.available() and self._last_error is None

    async def health(self) -> HealthStatus:
        if not self.available():
            return HealthStatus(connector=self.name, status="down", detail="not verified (Q4)")
        if self._last_error:
            return HealthStatus(connector=self.name, status="degraded", detail=self._last_error)
        return HealthStatus(connector=self.name, status="ok", detail="self chat")

    async def send(self, text: str) -> None:
        if not self.available() or self._g is None:
            raise AgentError("Teams self chat is not available")
        body: dict[str, Any] = {
            "body": {"contentType": "html", "content": html.escape(text[:MAX_CHARS]).replace("\n", "<br>")}
        }
        try:
            await self._g.post(f"me/chats/{SELF_CHAT}/messages", body)
        except AgentError as exc:
            self._last_error = str(exc)[:200]
            raise
        self._last_error = None
