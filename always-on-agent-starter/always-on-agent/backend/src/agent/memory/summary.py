"""The running summary: compaction by the model, verification by code (F09).

After each run with new events, Haiku rewrites the summary (at most 120 words) from the previous
summary plus the events since it, with the card as the authority. Code then checks the result:

- every task id it names appears in the card or the sources;
- every date it names appears in the card or the sources;
- current_state equals the card's state, and the text does not call the task closed when it is not;
- every pseudonym token it uses exists in the sources (none invented);
- it is at most max_words long.

On a mismatch the model gets one rewrite with the problems listed. If that fails too, the previous
summary stays and coverage does not advance, so the next run tries again with the same events; a
drifting summary is never stored. Model outages and privacy blocks skip the update; they never fail
the run.

Data rule: below `text`, people's messages are left out and the summary is built from the agent's own
notes and the card only (E12).
"""

import calendar
import logging
import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.core.errors import AgentError, ApprovalConflict
from agent.memory.task_memory import PERSON_KINDS, TaskCard, TaskMemory
from agent.memory.tokens import trim_to_tokens
from agent.model.gateway import ModelRunner
from agent.model.prompts import render, wrap_data
from agent.privacy.data_rule import allowed
from agent.privacy.hook import DataClass
from agent.privacy.pseudonymiser import TOKEN_RE
from agent.runtime.coordinator import find_task_ids

log = logging.getLogger(__name__)

MAX_WORDS = 120
MAX_NEW_EVENTS = 40  # beyond this, older new events go in as headlines only
EVENT_TOKENS = 300
CHECK_FACT = "_summary_check"

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b")
_DAY_MON = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?", re.I
)
_MON_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I
)
_CLOSED_CLAIM = re.compile(
    r"\b(?:task|ticket|incident|request|it)\s+(?:is\s+|was\s+|has\s+been\s+)?(?:now\s+)?(closed|done|resolved)\b",
    re.I,
)


class SummaryOut(BaseModel):
    summary: str = Field(description="The new running summary")
    current_state: str = Field(description="The card's state, copied exactly")


class SummaryResult(BaseModel):
    status: Literal["unchanged", "updated", "kept_previous", "skipped"]
    version: int | None = None
    problems: list[str] = []
    reason: str | None = None


# ---------------------------------------------------------------- verification


def mentioned_dates(text: str) -> set[tuple[int, int]]:
    """(month, day) pairs for every date written in the text; the year is ignored on purpose."""
    out: set[tuple[int, int]] = set()
    for _year, m, d in _ISO.findall(text):
        out.add((int(m), int(d)))
    for d, m, _ in _DMY.findall(text):
        if 1 <= int(m) <= 12:
            out.add((int(m), int(d)))
    for d, mon in _DAY_MON.findall(text):
        out.add((_MONTHS[mon[:3].lower()], int(d)))
    for mon, d in _MON_DAY.findall(text):
        out.add((_MONTHS[mon[:3].lower()], int(d)))
    return out


def _state(value: str) -> str:
    return re.sub(r"[\s-]+", "_", value.strip().lower())


def verify_summary(
    out: SummaryOut,
    card: TaskCard,
    sources: list[str],
    known_dates: set[date],
    max_words: int = MAX_WORDS,
) -> list[str]:
    """Problems in plain words; empty when the summary agrees with the record."""
    problems: list[str] = []
    text = out.summary
    words = len(text.split())
    if words > max_words:
        problems.append(f"it has {words} words; the limit is {max_words}")
    if not text.strip():
        problems.append("it is empty")
    joined = "\n".join(sources)

    allowed_ids = {card.id, *find_task_ids(joined)}
    for task_id in find_task_ids(text):
        if task_id not in allowed_ids:
            problems.append(f"it names {task_id}, which is not in the record")

    allowed_dates = mentioned_dates(joined) | {(d.month, d.day) for d in known_dates}
    for month, day in sorted(mentioned_dates(text) - allowed_dates):
        problems.append(f"it names the date {day:02d}.{month:02d}, which is not in the record")

    if _state(out.current_state) != card.state:
        problems.append(f"current_state is {out.current_state!r} but the card says {card.state!r}")
    if card.state != "done" and _CLOSED_CLAIM.search(text):
        problems.append(f"it says the task is closed, but the card says {card.state!r}")

    known_tokens = set(TOKEN_RE.findall(joined))
    for token in sorted(set(TOKEN_RE.findall(text)) - known_tokens):
        problems.append(f"it uses {token}, which does not appear in the record")
    return problems


# ---------------------------------------------------------------- the updater


class SummaryUpdater:
    def __init__(
        self, model: ModelRunner, memory: TaskMemory, level: DataClass, max_words: int = MAX_WORDS
    ) -> None:
        self._model = model
        self._memory = memory
        self._level = level
        self._max_words = max_words

    async def update(self, task_id: str) -> SummaryResult:
        previous = await self._memory.summary(task_id)
        new = await self._memory.repo.task_events_after(task_id, previous.covers_through)
        if not new:
            return SummaryResult(status="unchanged", version=previous.version)
        card = await self._memory.card(task_id)
        messages, classes, sources = self._messages(card, previous.text, new)
        known_dates = self._card_dates(card) | {self._memory.local(e["at"]).date() for e in new}

        problems: list[str] = []
        for attempt in range(2):
            instructions = render("summary_update", max_words=self._max_words, problems=problems)
            try:
                result = await self._model.run(
                    "summary",
                    messages,
                    SummaryOut,
                    classes,
                    tier="haiku",
                    task_id=task_id,
                    instructions=instructions,
                )
            except AgentError as exc:
                log.warning("summary update skipped", extra={"task": task_id, "error": type(exc).__name__})
                return SummaryResult(status="skipped", version=previous.version, reason=type(exc).__name__)
            problems = verify_summary(result.output, card, sources, known_dates, self._max_words)
            if not problems:
                try:
                    version = await self._memory.repo.put_summary(
                        task_id,
                        result.output.summary.strip(),
                        expected_version=previous.version or 0,  # 0: no summary may exist yet
                        covers_through=new[-1]["id"],
                        verified=True,
                    )
                except ApprovalConflict:
                    return SummaryResult(
                        status="skipped", version=previous.version, reason="written concurrently"
                    )
                await self._memory.repo.delete_fact(task_id, CHECK_FACT)
                return SummaryResult(status="updated", version=version, problems=[])
            log.info("summary failed its check", extra={"task": task_id, "attempt": attempt + 1})

        await self._memory.repo.set_fact(
            task_id, CHECK_FACT, "; ".join(problems)[:500], status="failed", source_ref="memory:summary"
        )
        return SummaryResult(status="kept_previous", version=previous.version, problems=problems)

    # ------------------------------------------------------------ internals

    def _messages(
        self, card: TaskCard, previous: str | None, new: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], set[DataClass], list[str]]:
        classes: set[DataClass] = {"metadata"}
        include_title = allowed(self._level, "text")
        card_text = "\n".join(card.lines(self._memory.local, include_title=include_title))
        lines: list[str] = []
        sources = [card_text]
        if previous:
            sources.append(previous)
        older, recent = new[:-MAX_NEW_EVENTS], new[-MAX_NEW_EVENTS:]
        if older:
            lines.append(f"({len(older)} earlier events, headlines only)")
            for event in older:
                if self._visible(event):
                    line = f"- {self._when(event)} {event['actor']}: {event['headline'] or ''}"
                    lines.append(line)
                    sources.append(line)
        for event in recent:
            stamp = f"[te:{event['id']}] {self._when(event)} · {event['channel']} · {event['actor']}"
            if not self._visible(event):
                lines.append(f"{stamp}: (text left out: data rule {self._level})")
                continue
            body = event["text_translated"] or event["text_pseudonymised"]
            body, _ = trim_to_tokens(body, EVENT_TOKENS)
            lines.append(f"{stamp}: {body}")
            sources.append(f"{stamp}: {body}")
            if event["kind"] in PERSON_KINDS:
                classes.add("chat" if event["channel"] in ("teams", "slack") else "text")
        content = (
            "Task card:\n"
            + wrap_data(card_text, "card")
            + "\n\nPrevious summary:\n"
            + wrap_data(previous or "(none yet)", "summary")
            + "\n\nNew events:\n"
            + wrap_data("\n".join(lines), "events")
        )
        return [{"role": "user", "content": content}], classes, sources

    def _visible(self, event: dict[str, Any]) -> bool:
        if event["kind"] not in PERSON_KINDS:
            return True
        return allowed(self._level, "chat" if event["channel"] in ("teams", "slack") else "text")

    def _when(self, event: dict[str, Any]) -> str:
        at: datetime = event["at"]
        return self._memory.local(at).strftime("%Y-%m-%d %H:%M")

    def _card_dates(self, card: TaskCard) -> set[date]:
        out = set()
        for dt in (card.opened_at, card.clock_started_at, *(f.updated_at for f in card.facts.values())):
            if dt is not None:
                out |= {dt.date(), self._memory.local(dt).date()}
        return out
