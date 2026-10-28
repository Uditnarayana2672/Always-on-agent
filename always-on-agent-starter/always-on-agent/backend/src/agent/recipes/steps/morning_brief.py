"""R01 Morning brief: code gathers and ranks, the model (optionally) rewords, publish_brief posts it.

Order, most urgent first: connection problems (the brief says up front when it may be incomplete),
24-hour clocks past a threshold (latest threshold first), what needs the user (oldest first), stalled
tasks, replies owed, Power BI failures, then today's meetings in time order.

Edge cases:
- nothing to report: a two-line brief ("Nothing needs you right now"), never a blank one;
- the agent was off on the previous work day: said at the top (from the ledger's last brief date or the
  scheduler's missed_since);
- a source that cannot be read (calendar, Power BI) is listed under "Could not check", the rest of the
  brief still goes out;
- the model is unavailable, not allowed this data, or changes an id, a number or the line count: the
  code-written lines are used as they are (E07, E09).
"""

import re

from pydantic import BaseModel

from agent.channels.cards import BriefSection
from agent.core.errors import AgentError
from agent.model.prompts import render
from agent.recipes.runner import StepContext
from agent.recipes.views import BriefData, ClockLine, TaskLine

DASH, DOT, ELLIPSIS = chr(0x2013), chr(0xB7), chr(0x2026)  # en dash, middle dot, ellipsis
MAX_LINE = 160
MAX_PER_SECTION = 8
_TOKENS = re.compile(r"\b(?:INC\d{8}|[A-Z]{2,6}-\d+|\d+(?:[.,:]\d+)?)\b")


class BriefWording(BaseModel):
    lines: list[str]


def _task(line: TaskLine, extra: str = "") -> str:
    parts = [f"{line.id} {DASH} {line.title}"]
    if line.next_action:
        parts.append(line.next_action)
    if line.age_hours is not None:
        parts.append(f"{line.age_hours:g} h")
    if extra:
        parts.append(extra)
    return f" {DOT} ".join(parts)[:MAX_LINE]


def _clock(line: ClockLine) -> str:
    passed = f"past {line.crossed[-1]} h" if line.crossed else "under the first mark"
    flags = [f for f, on in (("paused", line.paused), ("estimated start", line.estimated)) if on]
    text = f"{line.id} {DASH} {line.title} {DOT} {line.age_hours:g} h, {passed}"
    return (text + (f" ({', '.join(flags)})" if flags else ""))[:MAX_LINE]


def build_sections(data: BriefData) -> list[BriefSection]:
    """Deterministic ranking and wording. Every list is capped; the console has the rest."""

    def capped(lines: list[str]) -> list[str]:
        if len(lines) <= MAX_PER_SECTION:
            return lines
        return [*lines[:MAX_PER_SECTION], f"{ELLIPSIS}and {len(lines) - MAX_PER_SECTION} more in the console"]

    clocks = sorted(data.clocks, key=lambda c: (-(c.crossed[-1] if c.crossed else 0), -c.age_hours))
    urgent_ids = {c.id for c in clocks if c.crossed}
    needs_you = [t for t in data.needs_you if t.id not in urgent_ids]
    stalled_ids = {t.id for t in data.needs_you}
    sections = [
        BriefSection(
            heading="Connections",
            lines=[
                f"{c.connector}: {c.status}" + (f" ({c.detail[:80]})" if c.detail else "")
                for c in data.connectors
            ],
        ),
        BriefSection(heading="24-hour clocks", lines=capped([_clock(c) for c in clocks if c.crossed])),
        BriefSection(heading="Needs you", lines=capped([_task(t) for t in needs_you])),
        BriefSection(
            heading="Stalled", lines=capped([_task(t) for t in data.stalled if t.id not in stalled_ids])
        ),
        BriefSection(heading="Replies owed", lines=capped([_task(t) for t in data.replies_owed])),
        BriefSection(
            heading="Power BI",
            lines=capped(
                [
                    f"{r.dataset_id}: {r.status}" + (f" ({r.error_code})" if r.error_code else "")
                    for r in data.powerbi
                ]
            ),
        ),
        BriefSection(
            heading="Today's meetings",
            lines=capped([f"{m.start}{(DASH + m.end) if m.end else ''} {m.subject}" for m in data.meetings]),
        ),
        BriefSection(heading="Could not check", lines=data.unavailable),
    ]
    return [s for s in sections if s.lines]


def same_facts(original: str, reworded: str) -> bool:
    """The model may change words, never ids, numbers or times."""
    return (
        sorted(_TOKENS.findall(original)) == sorted(_TOKENS.findall(reworded))
        and 0 < len(reworded) <= MAX_LINE
    )


async def reword(ctx: StepContext, sections: list[BriefSection]) -> list[BriefSection]:
    lines = [line for s in sections for line in s.lines]
    if not lines or ctx.recipe.model == "none":
        return sections
    try:
        wording = await ctx.model(
            BriefWording,
            purpose="brief_lines",
            extra="\n".join(lines),
            extra_class="text",
            instructions=render("brief_lines", max_chars=MAX_LINE),
        )
    except AgentError as exc:  # no model, data rule, outage, bad output: keep the code's lines
        ctx.note(f"brief wording kept as written by code ({type(exc).__name__})")
        return sections
    if len(wording.lines) != len(lines):
        ctx.note("model changed the number of lines; kept the code's lines")
        return sections
    out, index = [], 0
    for section in sections:
        new_lines = []
        for line in section.lines:
            candidate = wording.lines[index].strip()
            new_lines.append(candidate if same_facts(line, candidate) else line)
            index += 1
        out.append(BriefSection(heading=section.heading, lines=new_lines))
    return out


async def steps(ctx: StepContext) -> None:
    data = BriefData.model_validate(await ctx.read("read_brief_data", {}))
    missed = data.missed
    if missed is None and ctx.trigger.get("missed_since"):
        missed = (
            f"Briefs were missed since {str(ctx.trigger['missed_since'])[:10]}; the agent was not running."
        )
    sections = await reword(ctx, build_sections(data))
    title = f"Morning brief {DASH} {data.day:%a %d %b}"
    ctx.propose(
        "publish_brief",
        {"title": title, "sections": [s.model_dump() for s in sections], "missed": missed},
        purpose="brief",
        # One brief per schedule slot: a live fire and its catch-up share due_at; "brief now" has its own.
        idempotency_key=f"brief:{ctx.trigger.get('due_at') or data.day.isoformat()}",
    )
