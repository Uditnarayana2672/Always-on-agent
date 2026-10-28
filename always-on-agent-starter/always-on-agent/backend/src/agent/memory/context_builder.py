"""Context builder (F10): code decides what each run sees.

A recipe declares the sections it needs; each section has a token budget and its own way of shrinking:

    section        budget  order  shrinks by
    personal          300    1    keeping the start and end of the text
    rules             150    2    dropping the oldest approved lessons
    style             300    3    dropping samples
    directory         300    4    dropping entries
    precedents        800    5    dropping the lowest-ranked records whole
    card              400    6    dropping low-priority lines (facts last), never id/state/clock
    summary           400    7    keeping start and end
    older_events      150    8    dropping the least important pointers
    last_events      1200    9    oldest first; one event never takes more than a third

Order is stable first, volatile last: what changes rarely (personal, rules) forms a prefix that prompt
caching can reuse, and the newest events sit next to the instructions where the model attends most.

Nothing is dropped silently. Every item has a ref (te:<id> for a task event, doc:<id> for a document).
Items trimmed or left out are listed in the section, and the older-events section is an index of
pointers; the model fetches any of them with search_memory, a Silent tool that goes through the gate.

Events the summary does not cover yet always come before the plain last five, so a summary that fell
behind never hides what happened. The data rule is applied per item: text above the level is replaced
by its pointer and a note (E12). Each packet's manifest (sections, refs, trims, tokens) is written to
context_log, so "why did the agent see this" has an answer.
"""

import logging
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.db.repositories import ContextLogRepo
from agent.memory.knowledge import Knowledge, keywords
from agent.memory.personal import PersonalMemory
from agent.memory.task_memory import PERSON_KINDS, TaskCard, TaskMemory
from agent.memory.tokens import estimate_tokens, trim_head, trim_to_tokens
from agent.model.prompts import wrap_data
from agent.privacy.data_rule import allowed
from agent.privacy.hook import LEVEL_ORDER, DataClass

log = logging.getLogger(__name__)

SectionName = Literal[
    "personal", "rules", "style", "directory", "precedents", "card", "summary", "older_events", "last_events"
]
ORDER: tuple[SectionName, ...] = (
    "personal",
    "rules",
    "style",
    "directory",
    "precedents",
    "card",
    "summary",
    "older_events",
    "last_events",
)
DEFAULT_BUDGETS: dict[SectionName, int] = {
    "personal": 300,
    "rules": 150,
    "style": 300,
    "directory": 300,
    "precedents": 800,
    "card": 400,
    "summary": 400,
    "older_events": 150,
    "last_events": 1200,
}
DEFAULT_NEEDS: tuple[SectionName, ...] = (
    "personal",
    "rules",
    "card",
    "summary",
    "older_events",
    "last_events",
)
LAST_N = 5
EVENT_SHARE = 3  # one event at most budget // EVENT_SHARE
# Sections that are the user's own words (instructions), not fetched content.
OWN_SECTIONS = frozenset({"personal", "rules", "style"})
SEARCH_RESULT_TOKENS = 800
NOTE_RESERVE = 25  # room kept for a section's note ("3 newer events did not fit ...")
_REF = re.compile(r"^(te|doc):(.+)$")


class PacketItem(BaseModel):
    ref: str | None = None
    text: str
    data_class: DataClass = "metadata"
    tokens: int = 0
    trimmed: bool = False


class Section(BaseModel):
    name: SectionName
    budget: int
    items: list[PacketItem] = Field(default_factory=list)
    trimmed: list[str] = Field(default_factory=list)  # refs shortened to fit
    omitted: list[str] = Field(default_factory=list)  # refs left out (fetch with search_memory)
    note: str | None = None

    @property
    def tokens(self) -> int:
        return sum(i.tokens for i in self.items) + estimate_tokens(self.note)

    def text(self) -> str:
        body = "\n".join(i.text for i in self.items)
        return f"{body}\n({self.note})" if self.note else body


class MemoryPacket(BaseModel):
    task_id: str | None
    recipe: str | None
    level: DataClass
    sections: list[Section]
    notes: list[str] = Field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(s.tokens for s in self.sections)

    def section(self, name: SectionName) -> Section | None:
        return next((s for s in self.sections if s.name == name), None)

    def refs(self) -> set[str]:
        return {i.ref for s in self.sections for i in s.items if i.ref}

    def data_classes(self) -> set[DataClass]:
        out: set[DataClass] = {"metadata"}
        out.update(i.data_class for s in self.sections for i in s.items)
        return out

    def fetched_content(self) -> bool:
        """True when the packet holds text written by other people (golden rule 4 applies)."""
        return any(
            i.data_class != "metadata" for s in self.sections if s.name not in OWN_SECTIONS for i in s.items
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "tokens": self.tokens,
            "notes": self.notes,
            "sections": {
                s.name: {
                    "tokens": s.tokens,
                    "budget": s.budget,
                    "refs": [i.ref for i in s.items if i.ref],
                    "trimmed": s.trimmed,
                    "omitted": s.omitted,
                }
                for s in self.sections
            },
        }

    def messages(self) -> list[dict[str, Any]]:
        """Two user messages: the user's own notes first (stable, cacheable), then the task's data,
        each section wrapped as data. Personal text goes in messages, not the system prompt, so the
        gateway pseudonymises it."""
        own = [s for s in self.sections if s.name in OWN_SECTIONS and s.items]
        data = [s for s in self.sections if s.name not in OWN_SECTIONS and (s.items or s.note)]
        out: list[dict[str, Any]] = []
        if own:
            parts = [f"## {_TITLES[s.name]}\n{s.text()}" for s in own]
            out.append(
                {"role": "user", "content": "About me and my rules (follow these):\n\n" + "\n\n".join(parts)}
            )
        if data:
            blocks = [f"## {_TITLES[s.name]}\n{wrap_data(s.text(), s.name)}" for s in data]
            if self.notes:
                blocks.append("Notes: " + "; ".join(self.notes))
            out.append({"role": "user", "content": "\n\n".join(blocks)})
        return out


_TITLES: dict[SectionName, str] = {
    "personal": "About me",
    "rules": "Rules I approved",
    "style": "How I write",
    "directory": "Site directory",
    "precedents": "Similar closed tasks",
    "card": "Task card",
    "summary": "Summary so far",
    "older_events": "Older events (ask search_memory for any of these)",
    "last_events": "Latest events",
}


class SearchMemory(BaseModel):
    """Fetch more memory: one event, one document, more precedents, or task events by keyword."""

    task_id: str
    kind: Literal["event", "document", "precedents", "events", "facts"]
    ref: str | None = None
    query: str | None = None
    k: int = Field(default=3, ge=1, le=5)
    exclude: list[str] = Field(default_factory=list)


class SearchResult(BaseModel):
    items: list[PacketItem]
    note: str | None = None

    def text(self) -> str:
        body = "\n".join(i.text for i in self.items) or "(nothing found)"
        return f"{body}\n({self.note})" if self.note else body


class ContextBuilder:
    def __init__(
        self,
        memory: TaskMemory,
        knowledge: Knowledge | None,
        personal: PersonalMemory | None,
        level: DataClass,
        log_repo: ContextLogRepo | None = None,
        budgets: dict[SectionName, int] | None = None,
    ) -> None:
        self._memory = memory
        self._knowledge = knowledge
        self._personal = personal
        self._level = level
        self._log = log_repo
        self._budgets = {**DEFAULT_BUDGETS, **(budgets or {})}

    # ------------------------------------------------------------ build

    async def build(
        self,
        task_id: str,
        needs: Sequence[str] = DEFAULT_NEEDS,
        recipe: str | None = None,
        scopes: Sequence[str] = ("global",),
        trigger_ref: str | None = None,
        level: DataClass | None = None,
    ) -> MemoryPacket:
        """level can only make the packet stricter than the data rule (a recipe declaring metadata only
        gets a metadata packet, E12); it never loosens it."""
        if level is not None and LEVEL_ORDER.index(level) < LEVEL_ORDER.index(self._level):
            stricter = ContextBuilder(
                self._memory, self._knowledge, self._personal, level, self._log, self._budgets
            )
            return await stricter.build(task_id, needs, recipe, scopes, trigger_ref)
        wanted = [n for n in ORDER if n in set(needs)]
        card = await self._memory.card(task_id)
        sections: dict[SectionName, Section] = {}
        for name in wanted:
            sections[name] = Section(name=name, budget=self._budgets[name])

        shown_events: set[int] = set()
        if "last_events" in sections:
            shown_events = await self._last_events(sections["last_events"], task_id, trigger_ref)
        for name, section in sections.items():
            if name == "personal":
                self._fill_personal(section)
            elif name == "rules":
                await self._fill_rules(section, [*scopes, card.project, *([recipe] if recipe else [])])
            elif name == "style":
                self._fill_style(section)
            elif name == "directory":
                await self._fill_directory(section, card)
            elif name == "precedents":
                await self._fill_precedents(section, card)
            elif name == "card":
                self._fill_card(section, card)
            elif name == "summary":
                await self._fill_summary(section, task_id)
            elif name == "older_events":
                await self._fill_older(section, task_id, shown_events, sections.get("last_events"))

        packet = MemoryPacket(
            task_id=task_id, recipe=recipe, level=self._level, sections=[sections[n] for n in wanted]
        )
        packet.notes = self._notes(packet)
        if self._log is not None:
            await self._log.add(task_id, recipe, packet.tokens, packet.manifest())
        return packet

    def _notes(self, packet: MemoryPacket) -> list[str]:
        notes = []
        withheld = sum(1 for s in packet.sections for i in s.items if i.text.endswith(_WITHHELD))
        if withheld:
            notes.append(f"{withheld} item(s) shown without their text: data rule is {self._level}")
        return notes

    # ------------------------------------------------------------ sections

    def _fill_personal(self, section: Section) -> None:
        if self._personal is None:
            return
        loaded = self._personal.load(section.budget)
        if loaded.text:
            section.items.append(
                PacketItem(
                    ref="memory:personal", text=loaded.text, tokens=loaded.tokens, trimmed=loaded.trimmed
                )
            )
            if loaded.trimmed:
                section.trimmed.append("memory:personal")

    def _fill_style(self, section: Section) -> None:
        if self._personal is None:
            return
        for i, sample in enumerate(self._personal.style_samples(section.budget)):
            section.items.append(
                PacketItem(ref=f"memory:style:{i}", text=sample, tokens=estimate_tokens(sample))
            )

    async def _fill_rules(self, section: Section, scopes: list[str]) -> None:
        if self._knowledge is None:
            return
        for lesson in await self._knowledge.lessons(scopes):
            text = f"- {lesson['text'].strip()}"
            self._add_whole(
                section, PacketItem(ref=f"lesson:{lesson['id']}", text=text, tokens=estimate_tokens(text))
            )

    async def _fill_directory(self, section: Section, card: TaskCard) -> None:
        if self._knowledge is None:
            return
        for entry in await self._knowledge.directory_for(card.site):
            flag = f" (stale: last confirmed {entry.updated_at.date().isoformat()})" if entry.stale else ""
            text = f"- {entry.role or 'contact'} for {entry.site}: {entry.name} <{entry.email}>{flag}"
            self._add_whole(
                section, PacketItem(ref=f"directory:{entry.email}", text=text, tokens=estimate_tokens(text))
            )

    async def _fill_precedents(self, section: Section, card: TaskCard) -> None:
        if self._knowledge is None:
            return
        cap = section.budget // 2
        for hit in await self._knowledge.precedents(card, k=8):
            if not allowed(self._level, "text"):
                site = hit.meta.get("site") or "unknown site"
                closed = str(hit.meta.get("closed_at", ""))[:10]
                text = f"[{hit.ref}] {hit.meta.get('kind', '')} at {site}, closed {closed}{_WITHHELD}"
                item = PacketItem(ref=hit.ref, text=text, tokens=estimate_tokens(text))
            else:
                text, cut = trim_to_tokens(f"[{hit.ref}] {hit.title}: {hit.snippet}", cap)
                item = PacketItem(
                    ref=hit.ref, text=text, data_class="text", tokens=estimate_tokens(text), trimmed=cut
                )
            self._add_whole(section, item)

    def _fill_card(self, section: Section, card: TaskCard) -> None:
        lines = card.lines(self._memory.local, include_title=allowed(self._level, "text"))
        limit = section.budget - NOTE_RESERVE
        used = 0
        kept: list[str] = []
        for i, line in enumerate(lines):
            cost = estimate_tokens(line) + 1
            if used + cost > limit:
                if i < 4:  # id, state, priority, clock: always kept, shortened if they must be
                    line, _ = trim_head(line, max(8, limit - used))
                    kept.append(line)
                    used += estimate_tokens(line) + 1
                    continue
                section.note = f"{len(lines) - i} more card line(s) left out"
                section.trimmed.append(f"card:{card.id}")
                break
            kept.append(line)
            used += cost
        text = "\n".join(kept)
        section.items.append(PacketItem(ref=f"card:{card.id}", text=text, tokens=estimate_tokens(text)))

    async def _fill_summary(self, section: Section, task_id: str) -> None:
        view = await self._memory.summary(task_id)
        if not view.text:
            section.note = "no summary yet"
            return
        data_class: DataClass = "text"
        if not allowed(self._level, "text"):
            section.note = f"summary withheld: data rule is {self._level}"
            return
        text, cut = trim_to_tokens(view.text, section.budget - 12)
        coverage = f" (covers up to te:{view.covers_through})" if view.covers_through else ""
        unchecked = " (not yet checked against the card)" if not view.verified else ""
        text = f"{text}{coverage}{unchecked}"
        section.items.append(
            PacketItem(
                ref=f"summary:{task_id}",
                text=text,
                data_class=data_class,
                tokens=estimate_tokens(text),
                trimmed=cut,
            )
        )

    async def _last_events(self, section: Section, task_id: str, trigger_ref: str | None) -> set[int]:
        """Unsummarised events first, then the last five; newest first until the budget is spent,
        then shown oldest first. The trigger event is always in, trimmed if it must be."""
        view = await self._memory.summary(task_id)
        unsummarised = await self._memory.repo.task_events_after(task_id, view.covers_through, limit=200)
        recent = await self._memory.repo.task_events(task_id, limit=LAST_N)
        by_id = {e["id"]: e for e in [*recent, *unsummarised]}
        order = sorted(by_id, reverse=True)
        trigger_id = _te_id(trigger_ref)
        if trigger_id is not None and trigger_id in by_id:
            order.remove(trigger_id)
            order.insert(0, trigger_id)
        limit = section.budget - NOTE_RESERVE
        cap = max(60, section.budget // EVENT_SHARE)
        used = 0
        chosen: list[tuple[int, PacketItem]] = []
        for event_id in order:
            item = self._event_item(by_id[event_id], cap)
            if used + item.tokens > limit:
                room = limit - used
                if event_id == trigger_id and room > 40:
                    item = self._event_item(by_id[event_id], room)
                else:
                    section.omitted.append(f"te:{event_id}")
                    continue
            used += item.tokens
            chosen.append((event_id, item))
            if item.trimmed:
                section.trimmed.append(f"te:{event_id}")
        for _, item in sorted(chosen, key=lambda pair: pair[0]):
            section.items.append(item)
        if section.omitted:
            section.note = (
                f"{len(section.omitted)} newer event(s) did not fit; they are listed under older events"
            )
        return {event_id for event_id, _ in chosen}

    def _event_item(self, event: dict[str, Any], cap: int) -> PacketItem:
        data_class: DataClass = "metadata"
        if event["kind"] in PERSON_KINDS:
            data_class = "chat" if event["channel"] in ("teams", "slack") else "text"
        when = self._memory.local(event["at"]).strftime("%Y-%m-%d %H:%M")
        stamp = f"[te:{event['id']}] {when} · {event['channel']} · {event['actor']}"
        if not allowed(self._level, data_class):
            text = f"{stamp}: {event['kind']}{_WITHHELD}"
            return PacketItem(ref=f"te:{event['id']}", text=text, tokens=estimate_tokens(text))
        body = event["text_translated"] or event["text_pseudonymised"]
        if event["text_translated"]:
            stamp += f" (translated from {event['lang']})"
        body, cut = trim_to_tokens(body, max(20, cap - estimate_tokens(stamp) - 2))
        text = f"{stamp}: {body}"
        return PacketItem(
            ref=f"te:{event['id']}",
            text=text,
            data_class=data_class,
            tokens=estimate_tokens(text),
            trimmed=cut,
        )

    async def _fill_older(
        self, section: Section, task_id: str, shown: set[int], last: Section | None
    ) -> None:
        """Pointers to events not shown: important and recent first, listed oldest first."""
        rows = [r for r in await self._memory.repo.event_index(task_id) if r["id"] not in shown]
        if not rows:
            return
        must = {_te_id(ref) for ref in (last.omitted if last else [])}
        rows.sort(key=lambda r: (r["id"] in must, r["importance"], r["id"]), reverse=True)
        headline_ok = allowed(self._level, "text")
        chosen = []
        used = estimate_tokens(f"{len(rows)} older event(s) in total") + 2
        for row in rows:
            what = (
                (row["headline"] or row["kind"])
                if headline_ok or row["kind"] not in PERSON_KINDS
                else row["kind"]
            )
            line = f"te:{row['id']} {self._memory.local(row['at']).strftime('%m-%d')} {row['actor']}: {what}"
            cost = estimate_tokens(line) + 1
            if used + cost > section.budget:
                section.omitted.append(f"te:{row['id']}")
                continue
            used += cost
            chosen.append((row["id"], line))
        for _, line in sorted(chosen):
            section.items.append(
                PacketItem(
                    ref=None,
                    text=line,
                    data_class="text" if headline_ok else "metadata",
                    tokens=estimate_tokens(line) + 1,
                )
            )
        section.note = f"{len(rows)} older event(s) in total"

    @staticmethod
    def _add_whole(section: Section, item: PacketItem) -> None:
        """Whole items until the budget is spent; the rest are listed as omitted."""
        if section.tokens + item.tokens > section.budget:
            if item.ref:
                section.omitted.append(item.ref)
            return
        section.items.append(item)

    # ------------------------------------------------------------ search_memory

    async def search_memory(self, args: SearchMemory) -> SearchResult:
        """Fetch more on request. Only this task's events; everything within SEARCH_RESULT_TOKENS and the
        data rule."""
        budget = SEARCH_RESULT_TOKENS
        if args.kind == "event":
            event_id = _te_id(args.ref)
            event = (
                await self._memory.repo.task_event(args.task_id, event_id) if event_id is not None else None
            )
            if event is None:
                return SearchResult(items=[], note=f"no event {args.ref} on this task")
            return SearchResult(items=[self._event_item(event, budget)])
        if args.kind == "document":
            match = _REF.match(args.ref or "")
            doc_id = match.group(2) if match and match.group(1) == "doc" else (args.ref or "")
            doc = await self._knowledge.document(doc_id) if self._knowledge else None
            if doc is None:
                return SearchResult(items=[], note=f"no document {args.ref}")
            if not allowed(self._level, "text"):
                return SearchResult(items=[], note=f"document text withheld: data rule is {self._level}")
            text, cut = trim_to_tokens(f"[doc:{doc['id']}] {doc['title']}\n{doc['body']}", budget)
            return SearchResult(
                items=[
                    PacketItem(
                        ref=f"doc:{doc['id']}",
                        text=text,
                        data_class="text",
                        tokens=estimate_tokens(text),
                        trimmed=cut,
                    )
                ]
            )
        if args.kind == "precedents":
            card = await self._memory.card(args.task_id)
            section = Section(name="precedents", budget=budget)
            hits = (
                await self._knowledge.precedents(
                    card,
                    k=args.k,
                    exclude=set(args.exclude),
                    query=keywords(args.query) if args.query else None,
                )
                if self._knowledge
                else []
            )
            for hit in hits:
                text, cut = trim_to_tokens(f"[{hit.ref}] {hit.title}: {hit.snippet}", budget // 2)
                self._add_whole(
                    section,
                    PacketItem(
                        ref=hit.ref, text=text, data_class="text", tokens=estimate_tokens(text), trimmed=cut
                    ),
                )
            return SearchResult(items=section.items, note=None if section.items else "no more similar tasks")
        if args.kind == "facts":
            card = await self._memory.card(args.task_id)
            lines = [line for line in card.lines(self._memory.local) if line.startswith("fact ")]
            text, _ = trim_to_tokens("\n".join(lines), budget)
            return SearchResult(
                items=[PacketItem(ref=f"card:{card.id}", text=text, tokens=estimate_tokens(text))]
                if lines
                else [],
                note=None if lines else "no facts",
            )
        return await self._search_events(args, budget)

    async def _search_events(self, args: SearchMemory, budget: int) -> SearchResult:
        words = {w for w in re.findall(r"\w+", (args.query or "").lower()) if len(w) > 2}
        if not words:
            return SearchResult(items=[], note="give a query")
        scored = []
        for event in await self._memory.repo.task_events_after(args.task_id, None, limit=5000):
            body = (event["text_translated"] or event["text_pseudonymised"]).lower()
            hits = sum(1 for w in words if w in body)
            if hits and f"te:{event['id']}" not in args.exclude:
                scored.append((hits + 0.2 * event["importance"], event["id"], event))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        section = Section(name="last_events", budget=budget)
        for _, _, event in scored[: args.k]:
            self._add_whole(section, self._event_item(event, budget // 2))
        return SearchResult(items=section.items, note=None if section.items else "no matching events")


_WITHHELD = " (text withheld: data rule)"


def _te_id(ref: str | None) -> int | None:
    match = _REF.match(ref or "")
    if match and match.group(1) == "te" and match.group(2).isdigit():
        return int(match.group(2))
    return None
