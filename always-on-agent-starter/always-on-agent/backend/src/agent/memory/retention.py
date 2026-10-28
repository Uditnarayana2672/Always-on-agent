"""Nightly retention (F09): raw text after retention.raw_text_days, screenshots after
retention.screenshots_days.

Raw text lives in intake payloads (events.payload_json: mail bodies, chat text, subjects). Pseudonymised
memory (task events, summaries, facts) is kept; it is what the agent remembers.

- Intake payloads received before the cutoff lose their free-text keys and are marked _purged; ids,
  senders and times stay so dedupe and routing still work.
- Task events whose raw_expires_at has passed get the payload they point to (event:<id>) purged too,
  even if that payload is younger (a per-event shorter retention).
- Screenshot files in paths.screenshots_inbox older than screenshots_days are deleted; anything that
  is not a regular image file, or a link, is left alone.
- Context manifests older than 30 days are deleted.

Runs are idempotent: purged rows are skipped next time.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from agent.core.clock import Clock
from agent.core.config import RetentionConfig
from agent.db.repositories import ContextLogRepo, RetentionRepo

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"})
CONTEXT_LOG_DAYS = 30


class RetentionReport(BaseModel):
    event_payloads: int = 0
    task_event_sources: int = 0
    screenshots: int = 0
    context_logs: int = 0


class RetentionJob:
    def __init__(
        self,
        config: RetentionConfig,
        clock: Clock,
        repo: RetentionRepo,
        context_log: ContextLogRepo | None = None,
        screenshots_dir: Path | str | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._repo = repo
        self._context_log = context_log
        self._screens = Path(screenshots_dir) if screenshots_dir else None

    async def run(self, now: datetime | None = None) -> RetentionReport:
        now = now or self._clock.now_utc()
        report = RetentionReport()
        report.event_payloads = await self._repo.purge_event_payloads(
            now - timedelta(days=self._config.raw_text_days)
        )
        for ref in await self._repo.expire_task_event_raw(now):
            kind, _, value = ref.partition(":")
            if kind == "event" and value.isdigit() and await self._repo.purge_event_payload(int(value)):
                report.task_event_sources += 1
        report.screenshots = self._purge_screenshots(now - timedelta(days=self._config.screenshots_days))
        if self._context_log is not None:
            report.context_logs = await self._context_log.purge_before(now - timedelta(days=CONTEXT_LOG_DAYS))
        log.info("retention run", extra=report.model_dump())
        return report

    def _purge_screenshots(self, cutoff: datetime) -> int:
        if self._screens is None or not self._screens.is_dir():
            return 0
        deleted = 0
        for path in self._screens.iterdir():
            try:
                if path.is_symlink() or not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                if path.stat().st_mtime < cutoff.timestamp():
                    path.unlink()
                    deleted += 1
            except OSError:
                log.warning("could not delete an old screenshot", extra={"file": path.name})
        return deleted
