"""Text folding for privacy matching (F11).

fold() lowercases, strips accents and drops invisible format characters (zero-width spaces, joiners,
bidi marks), keeping a map from every folded position back to the original text. Matching runs on the
folded text, so "Tomás", "TOMAS" and "To​mas" are the same word, and replacements land on the
original span.
"""

import re
import unicodedata
from collections.abc import Iterator

# A word character in folded text; names never start or end next to one (whole-word matching).
_LEFT = r"(?<![\w])"
_RIGHT = r"(?![\w])"


class Folded:
    def __init__(self, original: str) -> None:
        chars: list[str] = []
        index: list[int] = []
        for i, ch in enumerate(original):
            if unicodedata.category(ch) == "Cf":
                continue  # zero-width and bidi characters: invisible, used to split names
            for part in unicodedata.normalize("NFKD", ch):
                if unicodedata.combining(part):
                    continue
                for c in part.casefold():
                    chars.append(c)
                    index.append(i)
        self.original = original
        self.text = "".join(chars)
        self._index = index

    def span(self, start: int, end: int) -> tuple[int, int]:
        """Original (start, end) for a folded span."""
        return self._index[start], self._index[end - 1] + 1

    def finditer(self, pattern: re.Pattern[str]) -> Iterator[tuple[int, int]]:
        for match in pattern.finditer(self.text):
            if match.end() > match.start():
                yield self.span(match.start(), match.end())


def fold(text: str) -> str:
    return Folded(text).text


def strip_invisible(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def phrase_pattern(words: list[str], plural: bool = False) -> re.Pattern[str]:
    """Whole-word pattern over folded text. Words may be split by spaces, line breaks or hyphens.

    With plural, the last word matches with or without a trailing s ("24 hours" also finds "24-hour").
    """
    folded = [fold(w) for w in words if w]
    if plural and folded and len(folded[-1]) > 1 and folded[-1].endswith("s"):
        folded[-1] = folded[-1][:-1]
    body = r"[\s\-]*".join(re.escape(w) for w in folded)
    return re.compile(_LEFT + body + ("s?" if plural else "") + _RIGHT)


def name_pattern(parts: list[str]) -> re.Pattern[str]:
    """Whole-word pattern for a name; parts may be separated by any whitespace."""
    return re.compile(_LEFT + r"\s+".join(re.escape(fold(p)) for p in parts) + _RIGHT)
