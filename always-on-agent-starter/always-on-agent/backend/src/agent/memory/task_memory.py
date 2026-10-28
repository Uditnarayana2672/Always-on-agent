"""Task memory (F09): the event log, facts with provenance, the task card and the running summary.

- The event log is append-only and pseudonymised. Each event keeps a source_ref ("event:<id>" for
  intake, "action:<id>", "ledger" ...), a one-line headline for the older-events index, a language and,
  for non-English text, a pseudonymised translation that the packet uses instead (F10).
- An intake event is recorded once per task however often its job runs (source_ref dedupe).
- Every fact needs a source_ref. A changed value leaves a "fact" event, so the history explains itself.
  Fact values are pseudonymised like event text. Keys starting with "_" and "participants" are
  internal and never shown to the model.
- The card is the authoritative structured view; the summary must agree with it (summary.py).
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel

from agent.core.clock import Clock
from agent.db.repositories import ActionRepo, EventRepo, MemoryRepo
from agent.ledger.service import Ledger
from agent.memory.tokens import trim_head
from agent.privacy.pseudonymiser import Pseudonymiser

log = logging.getLogger(__name__)

HEADLINE_TOKENS = 30
INTERNAL_FACTS = frozenset({"participants"})
PERSON_KINDS = frozenset({"message", "edit"})
_OPEN_APPROVALS = ("proposed", "edited")
# Link kinds the model may see; conversation and chat ids are opaque and stay out.
MODEL_LINK_KINDS = frozenset({"servicenow", "powerbi_dataset"})

_STOPWORDS = {
    "en": {"the", "and", "is", "are", "please", "with", "for", "not", "this", "can", "you", "have"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "bitte", "mit", "für", "ich", "sie", "kann", "wir"},
    "fr": {"le", "la", "les", "et", "est", "pas", "merci", "avec", "pour", "je", "vous", "nous", "une"},
    "it": {"il", "lo", "gli", "e", "non", "grazie", "con", "per", "sono", "che", "una", "della"},
}
_URGENT = re.compile(r"\b(urgent|asap|dringend|urgence|urgente|blocked|down|cannot|can't|kann nicht)\b", re.I)

Translator = Callable[[str, str, str], Awaitable[str | None]]  # (pseudonymised text, lang, task_id)


def detect_language(text: str) -> str | None:
    """en, de, fr or it by stopword hits; None when the text is too short to say."""
    words = re.findall(r"[a-zà-ÿß']+", text.lower())
    if len(words) < 4:
        return None
    hits = {lang: sum(1 for w in words if w in stop) for lang, stop in _STOPWORDS.items()}
    best = max(hits, key=lambda k: hits[k])
    return best if hits[best] >= 2 else None


def headline_of(text: str) -> str:
    for line in text.splitlines():
        line = " ".join(line.split())
        if line and not line.startswith(">"):
            return trim_head(line, HEADLINE_TOKENS)[0]
    return ""


def importance_of(kind: str, actor: str, text: str) -> int:
    """0 (agent notes) to 5. People's messages matter most; questions and urgency raise them."""
    if kind in PERSON_KINDS and actor not in ("agent", "worker", "system"):
        score = 2 + ("?" in text) + bool(_URGENT.search(text))
        return min(score, 5)
    return {"ledger": 1, "fact": 1}.get(kind, 0)


class FactView(BaseModel):
    value: str
    status: str
    source_ref: str | None
    updated_at: datetime


class TaskCard(BaseModel):
    """Authoritative fields of a task, as the model may see them."""

    id: str
    title: str
    project: str
    kind: str
    state: str
    priority: str
    site: str | None = None
    recipe: str | None = None
    next_action: str | None = None
    opened_at: datetime | None = None
    clock_started_at: datetime | None = None
    clock_estimated: bool = False
    clock_paused: bool = False
    age_hours: float | None = None
    links: list[str] = []
    open_approvals: int = 0
    facts: dict[str, FactView] = {}

    def lines(
        self, local: Callable[[datetime], datetime] | None = None, include_title: bool = True
    ) -> list[str]:
        """Card lines in priority order: what must survive trimming comes first."""

        def when(dt: datetime | None) -> str | None:
            if dt is None:
                return None
            shown = local(dt) if local else dt
            return shown.strftime("%Y-%m-%d %H:%M %Z").strip()

        out = [f"id: {self.id}", f"state: {self.state}", f"priority: {self.priority}"]
        if self.age_hours is not None:
            clock = f"{self.age_hours:.1f} h"
            clock += " (estimated start)" if self.clock_estimated else ""
            clock += " (paused)" if self.clock_paused else ""
            out.append(f"clock: {clock}")
        if include_title:
            out.append(f"title: {self.title}")
        out.append(f"project: {self.project} / {self.kind}")
        for label, value in (
            ("site", self.site),
            ("next action", self.next_action),
            ("opened", when(self.opened_at)),
            ("clock started", when(self.clock_started_at)),
            ("recipe", self.recipe),
        ):
            if value:
                out.append(f"{label}: {value}")
        if self.open_approvals:
            out.append(f"open approvals: {self.open_approvals}")
        if self.links:
            out.append("linked: " + ", ".join(self.links))
        for key, fact in sorted(self.facts.items()):
            flag = "" if fact.status == "known" else f" ({fact.status})"
            out.append(f"fact {key}: {fact.value}{flag} [{fact.source_ref}]")
        return out


class SummaryView(BaseModel):
    text: str | None
    version: int | None
    covers_through: int | None
    verified: bool = False


class TaskMemory:
    def __init__(
        self,
        memory: MemoryRepo,
        ledger: Ledger,
        clock: Clock,
        events: EventRepo | None = None,
        actions: ActionRepo | None = None,
        pseudonymiser: Pseudonymiser | None = None,
        raw_text_days: int = 14,
        translator: Translator | None = None,
    ) -> None:
        self.repo = memory
        self._ledger = ledger
        self._clock = clock
        self._events = events
        self._actions = actions
        self._pseudo = pseudonymiser
        self._raw_days = raw_text_days
        self._translate = translator

    # ------------------------------------------------------------ events

    async def add_event(
        self,
        task_id: str,
        text: str,
        actor: str,
        channel: str,
        source_ref: str | None = None,
        kind: str = "note",
        raw_expires_at: datetime | None = None,
        at: datetime | None = None,
        pseudonymised: bool = False,
        importance: int | None = None,
    ) -> int | None:
        """Append to the log. Returns the event id, or None when this source_ref is already recorded."""
        if source_ref and await self.repo.has_source(task_id, source_ref):
            return None
        clean = (
            text if pseudonymised or self._pseudo is None else await self._pseudo.pseudonymise(text, task_id)
        )
        if (
            actor not in ("agent", "worker", "system", "user")
            and self._pseudo is not None
            and not pseudonymised
        ):
            actor = await self._pseudo.pseudonymise(actor, task_id)
        lang = detect_language(clean)
        translated = None
        if lang not in (None, "en") and kind in PERSON_KINDS and self._translate is not None:
            try:
                translated = await self._translate(clean, lang or "", task_id)
            except Exception:
                log.warning("translation failed; the original is kept", extra={"task": task_id})
        return await self.repo.add_task_event(
            task_id,
            actor=actor,
            channel=channel,
            text_pseudonymised=clean,
            source_ref=source_ref,
            raw_expires_at=raw_expires_at,
            at=at,
            kind=kind,
            headline=headline_of(translated or clean),
            lang=lang,
            text_translated=translated,
            importance=importance if importance is not None else importance_of(kind, actor, clean),
        )

    async def record_trigger(self, task_id: str, trigger: dict[str, Any]) -> int | None:
        """Record the intake event that started this run (once). Schedule fires carry no text."""
        event_id = trigger.get("event_id")
        if self._events is None or event_id is None or trigger.get("source") == "schedule":
            return None
        row = await self._events.get(int(event_id))
        if row is None:
            return None
        payload = json.loads(row["payload_json"] or "{}")
        if payload.get("_purged"):
            return None
        parts = [str(payload.get(k)) for k in ("subject", "body", "text") if payload.get(k)]
        if not parts:
            return None
        received: datetime = row["received_at"]
        return await self.add_event(
            task_id,
            "\n".join(parts),
            actor=str(payload.get("sender") or payload.get("user") or row["source"]),
            channel=str(row["source"]),
            source_ref=f"event:{event_id}",
            kind="edit" if row["kind"] == "message_edited" else "message",
            raw_expires_at=received + timedelta(days=self._raw_days),
            at=received,
        )

    # ------------------------------------------------------------ facts

    async def set_fact(
        self, task_id: str, key: str, value: str, source_ref: str, status: str = "known"
    ) -> bool:
        """Upsert a fact. Refuses one without a source. Returns True when the value changed."""
        if not source_ref or not source_ref.strip():
            raise ValueError(f"fact {key!r} needs a source_ref")
        clean = value if self._pseudo is None else await self._pseudo.pseudonymise(value, task_id)
        old = next((f for f in await self.repo.facts(task_id) if f["key"] == key), None)
        if old is not None and old["value"] == clean and old["status"] == status:
            return False
        await self.repo.set_fact(task_id, key, clean, status=status, source_ref=source_ref)
        if old is not None and not key.startswith("_"):
            await self.repo.add_task_event(
                task_id,
                actor="agent",
                channel="memory",
                kind="fact",
                text_pseudonymised=f"fact {key}: {old['value']} -> {clean} ({source_ref})",
                headline=f"fact {key} changed",
                importance=1,
            )
        return True

    # ------------------------------------------------------------ card and summary

    async def card(self, task_id: str) -> TaskCard:
        task = await self._ledger.get(task_id)
        facts = {
            f["key"]: FactView(
                value=f["value"], status=f["status"], source_ref=f["source_ref"], updated_at=f["updated_at"]
            )
            for f in await self.repo.facts(task_id)
            if f["key"] not in INTERNAL_FACTS and not f["key"].startswith("_")
        }
        open_approvals = (
            len(await self._actions.open_for_task(task_id, _OPEN_APPROVALS)) if self._actions else 0
        )
        return TaskCard(
            id=task.id,
            title=task.title,
            project=task.project,
            kind=task.kind,
            state=task.state,
            priority=task.priority,
            site=task.site,
            recipe=task.recipe,
            next_action=task.next_action,
            opened_at=task.opened_at,
            clock_started_at=task.clock_started_at,
            clock_estimated=task.clock_estimated,
            clock_paused=task.clock_paused,
            age_hours=await self._ledger.age_hours(task_id),
            links=[
                f"{link['kind']}:{link['external_id']}"
                for link in await self.repo.links(task_id)
                if link["kind"] in MODEL_LINK_KINDS
            ],
            open_approvals=open_approvals,
            facts=facts,
        )

    async def summary(self, task_id: str) -> SummaryView:
        row = await self.repo.get_summary(task_id)
        if row is None:
            return SummaryView(text=None, version=None, covers_through=None)
        return SummaryView(
            text=row["text"],
            version=row["version"],
            covers_through=row["covers_through"],
            verified=row["verified"],
        )

    def local(self, dt: datetime) -> datetime:
        return self._clock.to_local(dt)
