"""Pollers: read new items from mail, Teams and Power BI and hand them to the coordinator (A01, A03, A05).

The scheduler fires poll:mail, poll:teams and poll:powerbi (F02); the task runner calls the matching
handler from handlers(). Every item goes to Coordinator.ingest, which dedupes on (source, id).

Exactly-once intake with at-least-once reads:
- Graph delta links and Teams chat cursors are committed only after every item was ingested; a
  crash or an ingest error mid-poll means the next poll reads the same items again and the
  coordinator drops the duplicates.
- Adapters without delta (Outlook COM) use a watermark: the newest received time seen, read back
  with OVERLAP so mail that syncs late (cached mode) is not skipped.
- Power BI: failed, cancelled and finished refreshes after the watermark become `refresh` events
  (R05 watches the failures); a refresh running longer than stuck_after_minutes becomes one
  `refresh_stuck` event; a failed upstream dataflow becomes a `dataflow_refresh` event.

A poll that fails records connector health and raises nothing: the next poll tries again.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from agent.core.clock import Clock
from agent.core.errors import AgentError, NeedsSignIn
from agent.runtime.coordinator import IngestResult, Source

log = logging.getLogger(__name__)

OVERLAP = timedelta(minutes=10)
FIRST_LOOKBACK = timedelta(days=1)
WATERMARK = "poll.{source}.since"
LAST_OK = "poll.{connector}.last_ok"  # when a poll last succeeded (health monitor, F18)


class Ingest(Protocol):
    async def __call__(self, raw: Any, source: Source) -> IngestResult: ...


class Kv(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str) -> None: ...


class HealthSink(Protocol):
    async def report(self, connector: str, status: str, error: str | None = None) -> None: ...


Handler = Callable[[Any], Awaitable[None]]


class Pollers:
    def __init__(
        self,
        ingest: Ingest,
        kv: Kv,
        clock: Clock,
        *,
        mail: Any | None = None,
        teams: Any | None = None,
        powerbi: Any | None = None,
        health: HealthSink | None = None,
        watch_datasets: Sequence[str] = (),
        stuck_after: timedelta = timedelta(minutes=120),
    ) -> None:
        self._ingest = ingest
        self._kv = kv
        self._clock = clock
        self._mail = mail
        self._teams = teams
        self._powerbi = powerbi
        self._health = health
        self._watch = list(watch_datasets)
        self._stuck_after = stuck_after

    def handlers(self) -> dict[str, Handler]:
        """Schedule handlers for the task runner, keyed by schedule name."""

        def wrap(poll: Callable[[], Awaitable[int]]) -> Handler:
            async def run(job: Any) -> None:
                await poll()

            return run

        out: dict[str, Handler] = {}
        if self._mail is not None:
            out["poll:mail"] = wrap(self.poll_mail)
        if self._teams is not None:
            out["poll:teams"] = wrap(self.poll_teams)
        if self._powerbi is not None:
            out["poll:powerbi"] = wrap(self.poll_powerbi)
        return out

    # ------------------------------------------------------------ mail

    async def poll_mail(self) -> int:
        return await self._guarded("outlook", self._poll_mail)

    async def _poll_mail(self) -> int:
        mail = self._mail
        assert mail is not None
        if hasattr(mail, "changes"):
            count = 0
            for folder in ("inbox", "sentitems"):
                changes = await mail.changes(folder)
                for message in changes.messages:
                    await self._ingest(message, "mail")
                    count += 1
                await mail.commit_delta(changes)
            return count
        since = await self._watermark("mail")
        messages = await mail.list_new(since - OVERLAP)
        for message in messages:
            await self._ingest(message, "mail")
        if messages:
            await self._advance("mail", max(m.received_at for m in messages))
        return len(messages)

    # ------------------------------------------------------------ Teams

    async def poll_teams(self) -> int:
        return await self._guarded("teams", self._poll_teams)

    async def _poll_teams(self) -> int:
        teams = self._teams
        assert teams is not None
        since = await self._watermark("teams")
        if hasattr(teams, "changes"):
            changes = await teams.changes(since)
            for message in changes.messages:
                await self._ingest(message, "teams")
            await teams.commit(changes)
            await self._advance("teams", self._clock.now_utc() - OVERLAP)
            return len(changes.messages)
        messages = await teams.list_chat_messages(since - OVERLAP)
        for message in messages:
            await self._ingest(message, "teams")
        if messages:
            await self._advance("teams", max(m.edited_at or m.sent_at for m in messages))
        return len(messages)

    # ------------------------------------------------------------ Power BI

    async def poll_powerbi(self) -> int:
        return await self._guarded("powerbi", self._poll_powerbi)

    async def _poll_powerbi(self) -> int:
        pbi = self._powerbi
        assert pbi is not None
        now = self._clock.now_utc()
        since = await self._watermark("powerbi")
        count = 0
        for dataset_id in self._watch:
            for refresh in await pbi.refresh_history(dataset_id, 10):
                event = self._refresh_event(dataset_id, refresh, since, now)
                if event is not None:
                    await self._ingest(event, "powerbi")
                    count += 1
            for flow in await pbi.upstream_dataflows(dataset_id):
                if (
                    (flow.last_status or "").lower() == "failed"
                    and flow.last_end
                    and flow.last_end > since - OVERLAP
                ):
                    await self._ingest(
                        {
                            "kind": "dataflow_refresh",
                            "external_id": f"dataflow:{flow.id}:{flow.last_end.isoformat()}:failed",
                            "received_at": flow.last_end,
                            "payload": {
                                "dataflow_id": flow.id,
                                "dataflow": flow.name,
                                "dataset_id": dataset_id,
                                "status": "Failed",
                            },
                        },
                        "powerbi",
                    )
                    count += 1
        await self._advance("powerbi", now)
        return count

    def _refresh_event(
        self, dataset_id: str, refresh: Any, since: datetime, now: datetime
    ) -> dict[str, Any] | None:
        payload = {
            "dataset_id": dataset_id,
            "request_id": refresh.request_id,
            "status": refresh.status,
            "error_code": refresh.error_code,
            "refresh_type": refresh.refresh_type,
            "start": refresh.start.isoformat(),
            "end": refresh.end.isoformat() if refresh.end else None,
        }
        if refresh.status == "Unknown":
            if now - refresh.start < self._stuck_after:
                return None
            return {
                "kind": "refresh_stuck",
                "external_id": f"refresh:{dataset_id}:{refresh.request_id}:stuck",
                "received_at": now,
                "payload": payload,
            }
        finished = refresh.end or refresh.start
        if finished <= since - OVERLAP:
            return None
        return {
            "kind": "refresh",
            "external_id": f"refresh:{dataset_id}:{refresh.request_id}:{refresh.status}",
            "received_at": finished,
            "payload": payload,
        }

    # ------------------------------------------------------------ helpers

    async def _guarded(self, connector: str, poll: Callable[[], Awaitable[int]]) -> int:
        try:
            count = await poll()
        except NeedsSignIn as exc:
            await self._report(connector, "down", f"sign in again: {exc}")
            return 0
        except AgentError as exc:
            log.warning("poll failed", extra={"connector": connector, "error": str(exc)[:200]})
            await self._report(connector, "degraded", str(exc)[:300])
            return 0
        await self._report(connector, "ok")
        await self._kv.set(LAST_OK.format(connector=connector), self._clock.now_utc().isoformat())
        return count

    async def _report(self, connector: str, status: str, error: str | None = None) -> None:
        if self._health is not None:
            try:
                await self._health.report(connector, status, error)
            except Exception:
                log.exception("could not record connector health", extra={"connector": connector})

    async def _watermark(self, source: str) -> datetime:
        raw = await self._kv.get(WATERMARK.format(source=source))
        if raw:
            return datetime.fromisoformat(raw)
        return self._clock.now_utc() - FIRST_LOOKBACK

    async def _advance(self, source: str, to: datetime) -> None:
        current = await self._kv.get(WATERMARK.format(source=source))
        if current is None or datetime.fromisoformat(current) < to:
            await self._kv.set(WATERMARK.format(source=source), to.isoformat())
