"""Personal memory: profile.md and preferences.md, always loaded (F09).

The files live in memory/ at the repository root (git-ignored), or config paths.memory_dir. They are
created from the templates on first run. The user edits them by hand or in the console; they are
re-read when their modification time or size changes. Template guidance inside <!-- --> is dropped,
so an untouched template costs no tokens. Optional writing-style samples live in memory/style/*.md
and are loaded only by drafting recipes.

The text may hold the user's own name: it reaches the model only through the gateway, which
pseudonymises it like everything else.
"""

import logging
import re
import shutil
from pathlib import Path

from pydantic import BaseModel

from agent.core.config import CONFIG_DIR
from agent.memory.tokens import estimate_tokens, trim_to_tokens

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
DEFAULT_DIR = CONFIG_DIR.parent / "memory"
FILES = ("profile.md", "preferences.md")
STYLE_DIR = "style"
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLANKS = re.compile(r"\n{3,}")


class PersonalSection(BaseModel):
    text: str
    tokens: int
    trimmed: bool = False
    files: list[str]


def clean_markdown(text: str) -> str:
    """Drop comments and empty headings; collapse blank lines."""
    text = _COMMENT.sub("", text.replace("\r\n", "\n"))
    lines = [line.rstrip() for line in text.split("\n")]
    kept: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("#"):
            rest = [x for x in lines[i + 1 :] if x.strip()]
            if not rest or rest[0].startswith("#"):
                continue  # a heading with nothing under it
        kept.append(line)
    return _BLANKS.sub("\n\n", "\n".join(kept)).strip()


class PersonalMemory:
    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory else DEFAULT_DIR
        self._cache: dict[Path, tuple[int, int, str]] = {}

    def ensure(self) -> list[Path]:
        """Create the directory and any missing file from its template. Returns the files created."""
        created = []
        self.directory.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            path = self.directory / name
            if not path.exists():
                shutil.copyfile(TEMPLATES / name, path)
                created.append(path)
        return created

    def load(self, budget: int = 300) -> PersonalSection:
        try:
            self.ensure()
        except OSError:
            log.exception("cannot create personal memory files", extra={"dir": str(self.directory)})
        parts = []
        files = []
        for name in FILES:
            text = self._read(self.directory / name)
            if text:
                parts.append(text)
                files.append(name)
        combined = "\n\n".join(parts)
        text, trimmed = trim_to_tokens(combined, budget, head_share=0.8)
        return PersonalSection(text=text, tokens=estimate_tokens(text), trimmed=trimmed, files=files)

    def style_samples(self, budget: int = 300) -> list[str]:
        """Writing samples for drafts, newest file first, within budget."""
        folder = self.directory / STYLE_DIR
        if not folder.is_dir():
            return []
        out, used = [], 0
        for path in sorted(folder.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            text = self._read(path)
            cost = estimate_tokens(text)
            if not text or used + cost > budget:
                continue
            out.append(text)
            used += cost
        return out

    def _read(self, path: Path) -> str:
        """Cached by (mtime, size): edits in an editor are picked up on the next run."""
        try:
            stat = path.stat()
        except OSError:
            return ""
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(path)
        if cached is not None and cached[:2] == key:
            return cached[2]
        try:
            text = clean_markdown(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            log.exception("cannot read personal memory", extra={"file": path.name})
            return ""
        self._cache[path] = (*key, text)
        return text
