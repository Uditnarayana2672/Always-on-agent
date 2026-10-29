"""Health monitor (F18): every 5 minutes, what is broken, said once.

Checks: each connector's own health call (written to connector_health), the last successful poll per
source, stalled tasks, jobs that gave up, schedules missed, ICA reachability and free disk space.

Problems are incidents with a stable key ("connector:teams", "poll:outlook", "stalled:INC30114872",
"disk"). An incident is announced once, in one DM per run that lists only the new ones; while it
lasts it is not repeated, and when it clears it is dropped (a later return is a new incident). Two
kinds never DM: failed jobs (the worker pool already said so when the job failed) and missed
schedules (the next run says what it missed). Everything open shows at the top of the brief and on
the console's Connectors page.

A check that itself fails becomes its own incident instead of stopping the others.
"""

import contextlib
import json
import logging
import shutil
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from agent.channels.notifier import Notification
from agent.core.clock import Clock
from agent.core.config import Config
from agent.runtime.pollers import LAST_OK

log = logging.getLogger(__name__)

INTERVAL = timedelta(minutes=5)
INCIDENTS_KEY = "health.incidents"
POLL_LATE_FACTOR = 3  # a poll is late after three missed intervals
POLL_LATE_MIN = timedelta(minutes=30)
MIN_FREE_BYTES = 1024**3  # 1 GB
MIN_FREE_SHARE = 0.05
LOOKBACK = timedelta(hours=24)
# Pollers: connector -> config.schedule.poll_minutes key
POLLED = {"outlook": "mail", "teams": "teams", "powerbi": "powerbi"}
LABEL = {
    "outlook": "Outlook mail",
    "calendar": "Outlook calendar",
    "teams": "Teams",
    "powerbi": "Power BI",
    "slack": "Slack",
    "ica": "ICA model gateway",
}

Severity = Literal["alert", "info"]
IcaCheck = Callable[[], Awaitable[str | None]]  # None when reachable, else the problem


class Incident(BaseModel):
    key: str
    severity: Severity
    text: str
    since: datetime | None = None


class HealthReport(BaseModel):
    at: datetime
    incidents: list[Incident]
    new: list[str]  # keys announced in this run
    cleared: list[str]


class HealthMonitor:
    def __init__(
        self,
        config: Config,
        clock: Clock,
        *,
        adapters: Mapping[str, Any],
        health: Any,  # HealthRepo
        kv: Any,  # KvRepo
        jobs: Any,  # JobRepo
        ledger: Any,  # Ledger
        schedule_runs: Any,  # ScheduleRunRepo
        notify: Callable[[Notification], Awaitable[Any]] | None = None,
        ica_check: IcaCheck | None = None,
        data_dir: Path | str | None = None,
        disk_usage: Callable[[str], Any] = shutil.disk_usage,
        setup_notes: list[str] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._adapters = adapters
        self._health = health
        self._kv = kv
        self._jobs = jobs
        self._ledger = ledger
        self._runs = schedule_runs
        self._notify = notify
        self._ica = ica_check
        self._dir = str(data_dir or ".")
        self._disk = disk_usage
        self.setup_notes = list(setup_notes or [])  # what is not set up yet (agentd run, real mode)

    # ------------------------------------------------------------ the run

    async def run_once(self) -> HealthReport:
        now = self._clock.now_utc()
        found: list[Incident] = []
        for name, check in (
            ("connectors", self._connectors),
            ("polls", self._polls),
            ("stalled", self._stalled),
            ("jobs", self._failed_jobs),
            ("schedules", self._missed),
            ("ica", self._model),
            ("disk", self._disk_space),
            ("setup", self._setup),
        ):
            try:
                found += await check(now)
            except Exception as exc:  # one broken check must not hide the others
                log.exception("health check failed", extra={"check": name})
                found.append(
                    Incident(key=f"check:{name}", severity="info", text=f"Health check {name} failed: {exc}")
                )

        previous = {i.key: i for i in await self.open_incidents()}
        current: dict[str, Incident] = {}
        for incident in found:
            if incident.key in current:
                continue
            earlier = previous.get(incident.key)
            since = earlier.since if earlier is not None else now
            current[incident.key] = incident.model_copy(update={"since": since})
        new = [k for k in current if k not in previous]
        cleared = [k for k in previous if k not in current]
        await self._kv.set(
            INCIDENTS_KEY, json.dumps([i.model_dump(mode="json") for i in current.values()], sort_keys=True)
        )
        announce = [current[k] for k in new if current[k].severity == "alert"]
        if announce and self._notify is not None:
            text = "Agent health: " + " ".join(i.text for i in announce)
            if len(announce) > 1:
                text = "Agent health:\n" + "\n".join(f"• {i.text}" for i in announce)
            with contextlib.suppress(Exception):
                await self._notify(Notification(kind="alert", text=text[:2900], urgent=False))
        if new or cleared:
            log.info("health changed", extra={"new": new, "cleared": cleared})
        return HealthReport(at=now, incidents=list(current.values()), new=new, cleared=cleared)

    async def open_incidents(self) -> list[Incident]:
        raw = await self._kv.get(INCIDENTS_KEY)
        if not raw:
            return []
        try:
            return [Incident.model_validate(i) for i in json.loads(raw)]
        except ValueError:
            return []

    async def summary(self) -> list[str]:
        """One line per open incident, alerts first (top of the brief, `agentd status`)."""
        incidents = await self.open_incidents()
        incidents.sort(key=lambda i: (i.severity != "alert", i.key))
        return [i.text for i in incidents]

    # ------------------------------------------------------------ checks

    async def _connectors(self, now: datetime) -> list[Incident]:
        out = []
        for name, adapter in self._adapters.items():
            check = getattr(adapter, "health", None)
            if check is None:
                continue
            try:
                status = await check()
                state, detail = str(status.status), str(status.detail or "")
            except Exception as exc:
                state, detail = "down", str(exc)[:200]
            await self._health.report(name, state, None if state == "ok" else detail[:300])
            if state != "ok":
                label = LABEL.get(name, name)
                out.append(
                    Incident(
                        key=f"connector:{name}",
                        severity="alert" if state == "down" else "info",
                        text=f"{label} is {'not working' if state == 'down' else 'partly working'}: "
                        f"{detail[:160]}",
                    )
                )
        return out

    async def _polls(self, now: datetime) -> list[Incident]:
        out = []
        minutes = self._config.schedule.poll_minutes.model_dump()
        for connector, source in POLLED.items():
            if connector not in self._adapters:
                continue
            raw = await self._kv.get(LAST_OK.format(connector=connector))
            if raw is None:
                continue  # never polled yet (just installed): the connector check speaks for it
            late_after = max(timedelta(minutes=minutes[source] * POLL_LATE_FACTOR), POLL_LATE_MIN)
            last = datetime.fromisoformat(raw)
            if now - last > late_after:
                ago = _ago(now - last)
                out.append(
                    Incident(
                        key=f"poll:{connector}",
                        severity="alert",
                        text=f"{LABEL.get(connector, connector)} has not been read for {ago}.",
                    )
                )
        return out

    async def _stalled(self, now: datetime) -> list[Incident]:
        return [
            Incident(
                key=f"stalled:{task.id}",
                severity="alert",
                text=f"{task.id} has not moved for a while: {task.title[:80]}.",
            )
            for task in await self._ledger.stalled(now)
        ]

    async def _failed_jobs(self, now: datetime) -> list[Incident]:
        failed = await self._jobs.failed_since(now - LOOKBACK)
        if not failed:
            return []
        kinds = sorted({j.kind for j in failed})
        return [
            Incident(
                key="jobs:failed",
                severity="info",  # the worker pool already sent a DM for each one
                text=f"{len(failed)} job{_s(len(failed))} failed in the last 24 h ({', '.join(kinds)}).",
            )
        ]

    async def _missed(self, now: datetime) -> list[Incident]:
        missed: dict[str, int] = {}
        for run in await self._runs.runs():
            due = run["due_at"]
            if run["status"] == "missed" and due >= now - LOOKBACK:
                missed[str(run["schedule"])] = missed.get(str(run["schedule"]), 0) + 1
        return [
            Incident(
                key=f"missed:{name}",
                severity="info",
                text=f"{count} {name.replace('_', ' ')} run{_s(count)} missed in the last 24 h "
                "(the agent was off).",
            )
            for name, count in sorted(missed.items())
        ]

    async def _model(self, now: datetime) -> list[Incident]:
        if self._ica is None:
            return []
        problem = await self._ica()
        await self._health.report("ica", "ok" if problem is None else "down", problem)
        if problem is None:
            return []
        return [
            Incident(
                key="connector:ica",
                severity="alert",
                text=f"The model gateway (ICA) is not reachable: {problem[:160]}. "
                "Recipes run their code-only steps.",
            )
        ]

    async def _setup(self, now: datetime) -> list[Incident]:
        return [
            Incident(key=f"setup:{i}", severity="info", text=f"Not set up: {note}")
            for i, note in enumerate(self.setup_notes)
        ]

    async def _disk_space(self, now: datetime) -> list[Incident]:
        usage = self._disk(self._dir)
        free, total = int(usage.free), int(usage.total) or 1
        if free >= MIN_FREE_BYTES and free / total >= MIN_FREE_SHARE:
            return []
        return [
            Incident(
                key="disk",
                severity="alert",
                text=f"Disk space is low: {free / 1024**3:.1f} GB free. "
                "The database needs room to keep working.",
            )
        ]


def _s(count: int) -> str:
    return "" if count == 1 else "s"


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"
