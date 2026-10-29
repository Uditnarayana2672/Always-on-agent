"""Notifications to the user (F14, E50): quiet hours and the daily push cap, checked at send time.

Schedules fire whenever they are due; whether a push goes out now is decided here:
- urgent notifications (a clock at its last threshold, a failed job) always go out;
- otherwise, in quiet hours or once max_pushes_per_day is reached (per local day), the notification
  is batched and goes out with the next check-in as one message.

The batch lives in kv, so it survives a restart.
"""

import json
import logging
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from agent.core.clock import Clock
from agent.core.config import NotificationsConfig
from agent.db.repositories import KvRepo

log = logging.getLogger(__name__)

BATCH_KEY = "notify.batch"
PUSHES_KEY = "notify.pushes.{day}"

Kind = Literal["brief", "alert", "approval", "hand_off", "digest", "job_failed", "checkin", "info"]


class Notification(BaseModel):
    kind: Kind
    text: str  # plain and short: what happened, what is needed
    task_id: str | None = None
    urgent: bool = False
    blocks: list[dict[str, Any]] | None = None  # a richer card for channels that show one (Slack)


class NotifySink(Protocol):
    """The primary channel (Slack DM in Phase 1; fallbacks arrive with 1.11/1.12)."""

    async def send(self, text: str) -> None: ...


class Notifier:
    def __init__(self, config: NotificationsConfig, clock: Clock, kv: KvRepo, sink: NotifySink) -> None:
        self._cap = config.max_pushes_per_day
        self._clock = clock
        self._kv = kv
        self._sink = sink

    def set_cap(self, max_pushes_per_day: int) -> None:
        self._cap = max_pushes_per_day

    async def notify(self, note: Notification) -> Literal["sent", "batched"]:
        now = self._clock.now_utc()
        if not note.urgent:
            if self._clock.is_quiet(now):
                await self._batch(note)
                return "batched"
            if await self.pushes_today(now) >= self._cap:
                await self._batch(note)
                return "batched"
        await self._send(note.text, now, note.blocks)
        return "sent"

    async def flush(self, force: bool = False) -> int:
        """Send the batch as one message (the check-in). Waits out quiet hours unless forced."""
        now = self._clock.now_utc()
        batch = await self.batched()
        if not batch or (self._clock.is_quiet(now) and not force):
            return 0
        lines = [f"- {n.text}" + (f" ({n.task_id})" if n.task_id else "") for n in batch]
        # Batched items go out as one plain message; their cards are not repeated.
        await self._send(f"{len(batch)} update(s) since the last check-in:\n" + "\n".join(lines), now)
        await self._kv.delete(BATCH_KEY)
        return len(batch)

    async def batched(self) -> list[Notification]:
        raw = await self._kv.get(BATCH_KEY)
        return [Notification.model_validate(n) for n in json.loads(raw)] if raw else []

    async def pushes_today(self, now: datetime | None = None) -> int:
        raw = await self._kv.get(self._pushes_key(now or self._clock.now_utc()))
        return int(raw) if raw else 0

    # ------------------------------------------------------------ internals

    def _pushes_key(self, now: datetime) -> str:
        return PUSHES_KEY.format(day=self._clock.to_local(now).date().isoformat())

    async def _batch(self, note: Notification) -> None:
        batch = [*await self.batched(), note]
        await self._kv.set(BATCH_KEY, json.dumps([n.model_dump() for n in batch]))

    async def _send(self, text: str, now: datetime, blocks: list[dict[str, Any]] | None = None) -> None:
        send_card = getattr(self._sink, "send_card", None)
        if blocks and send_card is not None:
            await send_card(text, blocks)
        else:
            await self._sink.send(text)
        await self._kv.set(self._pushes_key(now), str(await self.pushes_today(now) + 1))
