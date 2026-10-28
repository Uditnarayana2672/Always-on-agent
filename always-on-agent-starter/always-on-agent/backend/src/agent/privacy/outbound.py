"""Outbound checks on every outgoing draft (F11), after restore and before the approval card.

- Forbidden phrases (config privacy.forbidden_phrases; default KPI, 24 hours, 24h, metric) in
  caller-facing drafts. Matching ignores case, accents and invisible characters, allows plurals and
  "24-hour" spacing, and also covers the subject and attachment names.
- Recipients: every To, Cc and Bcc must be inside company_domains (an empty list lets nobody through)
  and look like an address. Outside -> Hand off.
- Attachments: executables, double extensions and oversized files are blocked; images to a caller get
  a warning (a screenshot can show names).
- Tokens left in the draft mean restore() was skipped: blocked.
"""

import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

from agent.core.config import PrivacyConfig
from agent.core.errors import OutboundBlocked
from agent.privacy.pseudonymiser import TOKEN_RE
from agent.privacy.text import Folded, phrase_pattern

BLOCKED_EXTENSIONS = frozenset(
    {
        "exe",
        "bat",
        "cmd",
        "com",
        "ps1",
        "psm1",
        "vbs",
        "vbe",
        "js",
        "jse",
        "wsf",
        "msi",
        "dll",
        "scr",
        "hta",
        "jar",
        "lnk",
        "reg",
        "cpl",
    }
)
IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "heic"})
_DOCUMENT_EXTENSIONS = frozenset(
    {"pdf", "doc", "docx", "xls", "xlsx", "txt", "csv", "zip", *IMAGE_EXTENSIONS}
)
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

Rule = Literal["forbidden_phrase", "outside_recipient", "bad_recipient", "attachment", "token"]


class OutboundAttachment(BaseModel):
    name: str
    content_type: str = ""
    size_bytes: int = 0


class OutboundDraft(BaseModel):
    body: str
    subject: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    attachments: list[OutboundAttachment] = Field(default_factory=list)
    caller_facing: bool = True


class OutboundProblem(BaseModel):
    rule: Rule
    detail: str
    severity: Literal["block", "warn", "hand_off"] = "block"


class OutboundReport(BaseModel):
    problems: list[OutboundProblem] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(p.severity == "warn" for p in self.problems)

    @property
    def hand_off(self) -> bool:
        return any(p.severity == "hand_off" for p in self.problems)

    @property
    def warnings(self) -> list[str]:
        return [p.detail for p in self.problems if p.severity == "warn"]

    def summary(self) -> str:
        return "; ".join(p.detail for p in self.problems if p.severity != "warn")


class OutboundChecker:
    def __init__(self, config: PrivacyConfig, company_domains: Iterable[str]) -> None:
        self._phrases = [
            (phrase, phrase_pattern(re.split(r"[\s\-]+", phrase.strip()), plural=True))
            for phrase in config.forbidden_phrases
            if phrase.strip()
        ]
        self._domains = {d.strip().lower().lstrip("@") for d in company_domains if d.strip()}
        self._max_bytes = config.max_attachment_mb * 1024 * 1024

    def check(self, draft: OutboundDraft) -> OutboundReport:
        problems: list[OutboundProblem] = []
        if draft.caller_facing:
            problems += self._phrases_in(draft)
        problems += self._recipients(draft)
        problems += self._attachments(draft)
        leftover = sorted(set(TOKEN_RE.findall(f"{draft.subject}\n{draft.body}")))
        if leftover:
            problems.append(
                OutboundProblem(rule="token", detail=f"placeholders left in the draft: {', '.join(leftover)}")
            )
        return OutboundReport(problems=problems)

    def enforce(self, draft: OutboundDraft) -> OutboundReport:
        report = self.check(draft)
        if not report.ok:
            raise OutboundBlocked(report.summary())
        return report

    # ------------------------------------------------------------ rules

    def _phrases_in(self, draft: OutboundDraft) -> list[OutboundProblem]:
        found: list[str] = []
        texts = [draft.subject, draft.body, *(a.name for a in draft.attachments)]
        for phrase, pattern in self._phrases:
            if any(next(Folded(text).finditer(pattern), None) for text in texts if text):
                found.append(phrase)
        if not found:
            return []
        return [
            OutboundProblem(
                rule="forbidden_phrase",
                detail=f"wording not for callers: {', '.join(repr(p) for p in found)}",
            )
        ]

    def _recipients(self, draft: OutboundDraft) -> list[OutboundProblem]:
        problems = []
        for address in [*draft.to, *draft.cc, *draft.bcc]:
            clean = address.strip().lower()
            if not _ADDRESS.match(clean):
                problems.append(
                    OutboundProblem(rule="bad_recipient", detail=f"not an e-mail address: {address!r}")
                )
            elif not self._inside(clean):
                problems.append(
                    OutboundProblem(
                        rule="outside_recipient",
                        detail=f"{clean} is outside the company",
                        severity="hand_off",
                    )
                )
        return problems

    def _inside(self, address: str) -> bool:
        domain = address.rsplit("@", 1)[-1]
        return any(domain == d or domain.endswith("." + d) for d in self._domains)

    def _attachments(self, draft: OutboundDraft) -> list[OutboundProblem]:
        problems = []
        for attachment in draft.attachments:
            parts = attachment.name.lower().rsplit(".", 2)
            ext = parts[-1] if len(parts) > 1 else ""
            inner = parts[-2] if len(parts) > 2 else ""
            if ext in BLOCKED_EXTENSIONS:
                problems.append(
                    OutboundProblem(rule="attachment", detail=f"{attachment.name}: file type not allowed")
                )
            elif inner in _DOCUMENT_EXTENSIONS and ext not in _DOCUMENT_EXTENSIONS:
                problems.append(
                    OutboundProblem(rule="attachment", detail=f"{attachment.name}: double extension")
                )
            if attachment.size_bytes > self._max_bytes:
                problems.append(OutboundProblem(rule="attachment", detail=f"{attachment.name}: too large"))
            if draft.caller_facing and (
                ext in IMAGE_EXTENSIONS or attachment.content_type.startswith("image/")
            ):
                problems.append(
                    OutboundProblem(
                        rule="attachment",
                        detail=f"{attachment.name}: check the image shows no names",
                        severity="warn",
                    )
                )
        return problems
