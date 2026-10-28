"""Token estimates and trimming for context budgets (F10).

No tokenizer dependency: 3.5 characters per token is conservative for mixed English and German text
with ids and pseudonym tokens (plain English is closer to 4), so a packet estimated within budget is
within budget for the model.
"""

import math

CHARS_PER_TOKEN = 3.5
TRIM_MARKER = " [… trimmed …] "


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def chars_for(tokens: int) -> int:
    return max(0, int(tokens * CHARS_PER_TOKEN))


def trim_to_tokens(text: str, budget: int, head_share: float = 0.6) -> tuple[str, bool]:
    """Fit text in `budget` tokens, keeping its start and its end (a mail's question and its signature
    line both matter more than the middle). Cuts on word boundaries. Returns (text, trimmed)."""
    if estimate_tokens(text) <= budget:
        return text, False
    room = chars_for(budget) - len(TRIM_MARKER)
    if room < 8:
        return "", True
    head_chars = int(room * head_share)
    tail_chars = room - head_chars
    head = text[:head_chars]
    if " " in head[head_chars // 2 :]:
        head = head[: head.rfind(" ")]
    tail = text[-tail_chars:] if tail_chars > 0 else ""
    if " " in tail[: len(tail) // 2]:
        tail = tail[tail.find(" ") + 1 :]
    return head.rstrip() + TRIM_MARKER + tail.lstrip(), True


def trim_head(text: str, budget: int) -> tuple[str, bool]:
    """Keep the start only (titles, headlines)."""
    if estimate_tokens(text) <= budget:
        return text, False
    room = max(0, chars_for(budget) - 1)
    cut = text[:room]
    if " " in cut[room // 2 :]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() + "…", True
