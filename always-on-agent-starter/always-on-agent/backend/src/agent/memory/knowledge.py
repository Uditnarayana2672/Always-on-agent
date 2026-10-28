"""Knowledge shared across tasks (F09): documents, precedents, the site directory and lessons.

search(query, kind, k) is plain FTS5 with any-word matching; every hit carries a source_ref
("doc:<id>").

Precedents are outcome records written when a task closes. Before they are shared, per-task pseudonym
tokens are replaced by role words ("the caller", "a user"): USER_3 in one task is a different person
from USER_3 in another, and a token copied from a precedent into a draft would otherwise be restored
to the wrong name. Retrieval ranks them like episodic memory in agent research (relevance, recency,
importance) with boosts for the same project, kind and site, drops near-duplicates and never returns
the task's own record. The store is capped per (project, kind); the oldest go first.

Directory entries older than 90 days are flagged stale (R10 asks for a fresh screenshot).
"""

import json
import math
import re
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel

from agent.core.clock import Clock
from agent.db.repositories import DirectoryRepo, LessonRepo
from agent.db.search import Fts5SearchIndex
from agent.memory.task_memory import TaskCard

PRECEDENT = "precedent"
PRECEDENT_CAP = 200  # per (project, kind)
RECENCY_HALF_LIFE_DAYS = 90.0
STALE_AFTER = timedelta(days=90)
QUERY_WORDS = 12

_ROLE_WORDS = [
    (re.compile(r"\bCALLER\b"), "the caller"),
    (re.compile(r"\bAFFECTED\b"), "the affected user"),
    (re.compile(r"\bKM_\d+\b"), "the KM"),
    (re.compile(r"\bLBA_\d+\b"), "the LBA"),
    (re.compile(r"\bUSER_\d+\b"), "a user"),
    (re.compile(r"\bPHONE_\d+\b"), "[phone]"),
    (re.compile(r"\bUID_\d+\b"), "[user id]"),
]
_STOP = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "has",
    "have",
    "not",
    "are",
    "was",
    "will",
    "re",
    "fw",
    "fwd",
    "aw",
    "please",
    "help",
    "issue",
    "problem",
    "der",
    "die",
    "das",
    "und",
    "nicht",
    "les",
    "pour",
    "avec",
    "une",
    "ticket",
    "incident",
    "task",
    "caller",
    "user",
}


def shareable(text: str) -> str:
    """Per-task tokens -> role words, so a record can be read in any task."""
    for pattern, word in _ROLE_WORDS:
        text = pattern.sub(word, text)
    return text


def keywords(*texts: str, limit: int = QUERY_WORDS) -> str:
    """Distinctive words for a search: no stopwords, no tokens, no bare numbers, first seen first."""
    seen: dict[str, None] = {}
    for text in texts:
        for word in re.findall(r"[^\W_][\w\-]+", shareable(text or "")):
            low = word.lower()
            if low in _STOP or low.isdigit() or len(low) < 3:
                continue
            seen.setdefault(low, None)
    return " ".join(list(seen)[:limit])


class KnowledgeHit(BaseModel):
    ref: str
    kind: str
    title: str
    snippet: str
    score: float
    meta: dict[str, Any] = {}


class DirectoryEntry(BaseModel):
    name: str
    email: str
    role: str | None
    site: str | None
    stale: bool
    updated_at: datetime


class Knowledge:
    def __init__(
        self,
        index: Fts5SearchIndex,
        clock: Clock,
        directory: DirectoryRepo | None = None,
        lessons: LessonRepo | None = None,
        cap: int = PRECEDENT_CAP,
    ) -> None:
        self._index = index
        self._clock = clock
        self._directory = directory
        self._lessons = lessons
        self._cap = cap

    # ------------------------------------------------------------ search

    async def search(self, query: str, kind: str | None = None, k: int = 5) -> list[KnowledgeHit]:
        hits = await self._index.search(query, limit=k, source=kind, any_word=True)
        return [
            KnowledgeHit(
                ref=f"doc:{h.id}", kind=h.source, title=h.title, snippet=h.snippet, score=h.score, meta=h.meta
            )
            for h in hits
        ]

    async def document(self, doc_id: str) -> dict[str, Any] | None:
        return await self._index.get(doc_id)

    # ------------------------------------------------------------ precedents

    async def record_precedent(self, card: TaskCard, summary: str | None, outcome: str = "closed") -> str:
        """Write (or rewrite, after a reopen and close) the outcome record of a closed task."""
        now = self._clock.now_utc()
        facts = "; ".join(f"{k}={v.value}" for k, v in sorted(card.facts.items()))
        body = "\n".join(
            line
            for line in (
                f"Outcome: {outcome}",
                f"Summary: {summary}" if summary else "",
                f"Facts: {facts}" if facts else "",
                f"Site: {card.site}" if card.site else "",
            )
            if line
        )
        doc_id = f"{PRECEDENT}:{card.id}"
        meta = {
            "task_id": card.id,
            "project": card.project,
            "kind": card.kind,
            "site": card.site,
            "outcome": outcome,
            "closed_at": now.isoformat(),
        }
        await self._index.upsert(doc_id, PRECEDENT, shareable(card.title), shareable(body), json.dumps(meta))
        await self._enforce_cap(card.project, card.kind)
        return doc_id

    async def precedents(
        self,
        card: TaskCard,
        k: int = 5,
        exclude: set[str] | frozenset[str] = frozenset(),
        query: str | None = None,
    ) -> list[KnowledgeHit]:
        """Closed tasks most like this one, ranked by relevance, recency and similarity of shape."""
        text = query or keywords(card.title, card.kind, *(f.value for f in card.facts.values()))
        if not text:
            return []
        hits = await self._index.search(text, limit=k * 4, source=PRECEDENT, any_word=True)
        if not hits:
            return []
        top = max(h.score for h in hits) or 1.0
        now = self._clock.now_utc()
        ranked: list[tuple[float, Any]] = []
        for hit in hits:
            ref = f"doc:{hit.id}"
            if hit.meta.get("task_id") == card.id or ref in exclude:
                continue
            closed = hit.meta.get("closed_at")
            age_days = (now - datetime.fromisoformat(closed)).days if closed else 365
            score = 0.6 * max(hit.score, 0.0) / top + 0.2 * math.exp(
                -age_days * math.log(2) / RECENCY_HALF_LIFE_DAYS
            )
            score += 0.1 if hit.meta.get("project") == card.project else 0.0
            score += 0.05 if hit.meta.get("kind") == card.kind else 0.0
            score += 0.1 if card.site and hit.meta.get("site") == card.site else 0.0
            score += 0.05 if hit.meta.get("outcome") in ("resolved", "closed") else 0.0
            ranked.append((score, hit))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        out: list[KnowledgeHit] = []
        titles: set[str] = set()
        for score, hit in ranked:
            norm = " ".join(hit.title.lower().split())
            if norm in titles:
                continue  # the same problem again: one record is enough
            titles.add(norm)
            out.append(
                KnowledgeHit(
                    ref=f"doc:{hit.id}",
                    kind=PRECEDENT,
                    title=hit.title,
                    snippet=hit.snippet,
                    score=round(score, 4),
                    meta=hit.meta,
                )
            )
            if len(out) == k:
                break
        return out

    async def _enforce_cap(self, project: str, kind: str) -> None:
        rows = []
        for row in await self._index.by_source(PRECEDENT):
            meta = json.loads(row["meta_json"] or "{}")
            if meta.get("project") == project and meta.get("kind") == kind:
                rows.append((meta.get("closed_at") or "", row["id"]))
        rows.sort()
        for _, doc_id in rows[: max(0, len(rows) - self._cap)]:
            await self._index.remove(doc_id)

    # ------------------------------------------------------------ directory and lessons

    async def directory_for(self, site: str | None) -> list[DirectoryEntry]:
        if self._directory is None or not site:
            return []
        now = self._clock.now_utc()
        return [
            DirectoryEntry(
                name=row["name"],
                email=row["email"],
                role=row["role"],
                site=row["site"],
                stale=now - row["updated_at"] > STALE_AFTER,
                updated_at=row["updated_at"],
            )
            for row in await self._directory.by_site(site)
        ]

    async def lessons(self, scopes: list[str]) -> list[dict[str, Any]]:
        """Approved lessons for these scopes, newest first. Two lessons on the same subject (same first
        line): the newest wins (F09 conflicting lessons)."""
        if self._lessons is None:
            return []
        rows = [r for r in await self._lessons.active() if r["scope"] in scopes]
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        seen: set[str] = set()
        out = []
        for row in rows:
            subject = row["text"].strip().split("\n", 1)[0].lower().split(":", 1)[0]
            if subject in seen:
                continue
            seen.add(subject)
            out.append(row)
        return out
