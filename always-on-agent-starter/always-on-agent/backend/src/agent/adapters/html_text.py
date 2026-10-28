"""HTML mail and chat bodies to plain text (A01, A03).

Images (inline cid: or remote) are dropped; scripts, styles and hidden elements never reach the text;
block elements become line breaks; links keep their visible text plus the URL when it differs (a
Power BI link in a chat must survive); Teams <at> mentions keep the name. Entities are decoded.
"""

import html
import re
from html.parser import HTMLParser

_BLOCK = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "table", "ul", "ol", "hr", "section", "article",
}  # fmt: skip
_SKIP = {"script", "style", "head", "title", "noscript", "template", "xml"}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all", re.I)
_SPACES = re.compile(r"[ \t\xa0]+")
_BLANK_LINES = re.compile(r"\n\s*\n\s*\n+")


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._stack: list[tuple[str, bool]] = []  # (tag, opened a skip)
        self._href: list[str | None] = []
        self._link_start: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        skip = tag in _SKIP or bool(_HIDDEN_STYLE.search(values.get("style", ""))) or "hidden" in values
        if tag in ("br", "img", "hr", "meta", "link", "input", "wbr"):  # void elements
            if tag in ("br", "hr") and not self._skip_depth:
                self.parts.append("\n")
            return
        self._stack.append((tag, skip))
        if skip:
            self._skip_depth += 1
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            self._href.append(values.get("href"))
            self._link_start.append(len(self.parts))

    def handle_endtag(self, tag: str) -> None:
        # Pop to the matching tag; tolerate the unclosed tags real mail is full of.
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                for _, skipped in self._stack[index:]:
                    if skipped:
                        self._skip_depth -= 1
                del self._stack[index:]
                break
        else:
            return
        if self._skip_depth:
            return
        if tag == "a" and self._href:
            href = self._href.pop()
            start = self._link_start.pop()
            visible = "".join(self.parts[start:]).strip()
            if href and href.startswith(("http://", "https://")) and href not in visible:
                self.parts.append(f" ({href})" if visible else href)
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(content: str) -> str:
    parser = _Text()
    parser.feed(content)
    parser.close()
    text = html.unescape("".join(parser.parts))
    lines = [_SPACES.sub(" ", line).strip() for line in text.replace("\r", "").split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def body_text(body: dict[str, str] | None) -> str:
    """A Graph itemBody ({contentType, content}) as plain text."""
    if not body:
        return ""
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        return html_to_text(content)
    return content.replace("\r\n", "\n").strip()
