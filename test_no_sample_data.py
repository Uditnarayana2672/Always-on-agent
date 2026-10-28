"""Fails if sample data from the prototype or docs appears in application code.

Sample values are listed in docs/prototype/sample-data-denylist.txt. They are allowed only in
fixtures/, backend/tests/, console/src/mocks/, test files and docs/. See CLAUDE.md rule 11.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DENYLIST = REPO / "docs" / "prototype" / "sample-data-denylist.txt"

# Application code and anything that ships with it.
SCAN_ROOTS = [
    REPO / "backend" / "src",
    REPO / "console" / "src",
    REPO / "console" / "index.html",
    REPO / "config",
    REPO / "scripts",
]
SCAN_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".json", ".yaml", ".yml", ".sql", ".ps1", ".csv", ".md", ".txt"}

# Places where sample data is expected.
ALLOWED_PARTS = {"mocks", "__mocks__", "fixtures", "tests", "__tests__"}
ALLOWED_NAME = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx)$")


def _fold(text: str) -> str:
    """Lower-case and strip accents so 'Tomás' and 'Tomas' match."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def _load_denylist() -> list[str]:
    values = []
    for line in DENYLIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            values.append(line)
    return values


def _patterns(values: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [(v, re.compile(r"(?<![\w-])" + re.escape(_fold(v)) + r"(?![\w-])")) for v in values]


def _files() -> list[Path]:
    out: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_file():
            out.append(root)
            continue
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
                continue
            rel_parts = set(path.relative_to(REPO).parts)
            if rel_parts & ALLOWED_PARTS or ALLOWED_NAME.search(path.name):
                continue
            if "node_modules" in rel_parts or "dist" in rel_parts:
                continue
            out.append(path)
    return out


def test_denylist_exists() -> None:
    assert DENYLIST.exists(), "docs/prototype/sample-data-denylist.txt is missing"
    assert _load_denylist(), "the denylist is empty"


def test_no_sample_data_in_application_code() -> None:
    patterns = _patterns(_load_denylist())
    hits: list[str] = []
    for path in _files():
        try:
            text = _fold(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            continue
        for value, pattern in patterns:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(REPO)}:{line_no}: {value}")
    assert not hits, (
        "Sample data from the prototype or docs found in application code. "
        "Load real data from the API or database and show an empty state instead "
        "(CLAUDE.md rule 11):\n" + "\n".join(hits)
    )
