"""Pseudonymiser, leak check and restore (F11).

Text going to the model has every person replaced by a stable per-task token:

- known people (task participants, the directory, mail headers) -> CALLER, AFFECTED, KM_n, LBA_n, USER_n
  by their role on the task;
- e-mail addresses -> the owner's token when known, else a new USER_n;
- phone numbers -> PHONE_n; user ids (CORP\\jdoe and configured patterns) -> UID_n;
- names in greetings ("Hi Tomás,") and signature blocks -> USER_n; the rest of a signature (title,
  phone, address) is dropped.

Matching ignores case, accents and invisible characters and is whole-word only. A full name matches in
any case; a single first or last name matches only when written with a capital, so a first name that
is also a word ("Will", "Rose") is not replaced in "I will". Mappings live in TokenMapRepo, encrypted.

leak_check() fails if a known name, any e-mail address or a phone number is still in the text.
restore() puts real values back locally and refuses a draft holding a token the task does not have.
"""

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from agent.adapters.base import MailMessage
from agent.core.config import PrivacyConfig
from agent.core.errors import LeakDetected, UnknownToken
from agent.db.repositories import DirectoryRepo, MemoryRepo, TokenCipher, TokenMapRepo
from agent.privacy.text import Folded, fold, strip_invisible

# Tokens the pseudonymiser writes. CALLER and AFFECTED are upper case only: "the caller" is a word.
TOKEN_RE = re.compile(r"\b(?:CALLER|AFFECTED|(?:USER|KM|LBA|PHONE|UID)_\d+)\b")
# What restore() treats as a token, including mis-cased ones the model may write ("User_2").
_TOKEN_LIKE = re.compile(r"\b(?:CALLER|AFFECTED)\b|\b(?i:user|km|lba|phone|uid)_\d+\b")

EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+|00|\(?0)[\d\s().\-/]{6,22}\d(?![\w])")
_DATE_LIKE = re.compile(r"^\(?\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\)?$")
_DOMAIN_USER = re.compile(r"(?<![\w\\])[A-Za-z][\w-]{1,14}\\[A-Za-z][\w.\-]{1,30}(?![\w])")

ROLE_TOKENS = {"caller": "CALLER", "affected": "AFFECTED"}
ROLE_PREFIXES = {"km": "KM", "lba": "LBA"}
_PARTICLES = {"de", "da", "di", "del", "della", "der", "den", "van", "von", "le", "la", "du", "dos", "das"}

_GREETING = re.compile(
    r"^[ \t>]*(?i:hi|hello|hey|dear|hallo|hoi|gr[uü]ezi|guten\s+(?:morgen|tag)|bonjour|salut|ciao|"
    r"buongiorno|hola|good\s+(?:morning|afternoon))[ \t]+"
    r"(?:(?i:mr|mrs|ms|dr|herr|frau|mme|m)\.?[ \t]+)?"
    r"(?P<name>[^\W\d_][\w'\u2019.\-]*(?:[ \t]+[^\W\d_][\w'\u2019.\-]*){0,2})",
    re.MULTILINE,
)
_NOT_NAMES = {
    "all",
    "team",
    "everyone",
    "everybody",
    "colleagues",
    "both",
    "there",
    "folks",
    "guys",
    "sir",
    "madam",
    "zusammen",
    "tous",
    "together",
    "again",
    "support",
    "and",
    "to",
    "from",
    "the",
    "you",
}
_SIGN_OFF = re.compile(
    r"^[ \t]*(?i:best\s+regards|kind\s+regards|warm\s+regards|regards|best|thanks|thank\s+you|"
    r"many\s+thanks|cheers|br|mit\s+freundlichen\s+gr[uü](?:ß|ss)en|freundliche\s+gr[uü](?:ß|ss)e|"
    r"beste\s+gr[uü](?:ß|ss)e|liebe\s+gr[uü](?:ß|ss)e|gru(?:ß|ss)|cordialement|bien\s+[aà]\s+vous|"
    r"merci|saluti|cordiali\s+saluti)[ \t]*[,.!]?[ \t]*$"
)
_SIGNATURE_LINES = 6
_SIGNATURE_END = re.compile(r"^\s*(?:$|>|-{2,}|_{2,}|From:|Von:|De:|Sent:|On .+ wrote:)", re.IGNORECASE)
SIGNATURE_MARK = "[signature removed]"


class KnownPerson(BaseModel):
    name: str = ""
    email: str | None = None
    role: str | None = None  # caller | affected | km | lba | anything else -> USER_n


def people_from_mail(mail: MailMessage, roles: dict[str, str] | None = None) -> list[KnownPerson]:
    """Sender, To and Cc of a message, with task roles by address when known."""
    roles = {k.lower(): v for k, v in (roles or {}).items()}
    out = []
    for person in [mail.sender, *mail.to, *mail.cc]:
        address = person.address.lower()
        out.append(KnownPerson(name=person.name, email=address, role=roles.get(address)))
    return out


# ---------------------------------------------------------------- name variants


def name_parts(name: str) -> list[str]:
    """'Laurent, Sophie (Doxis)' -> ['Sophie', 'Laurent']."""
    clean = re.sub(r"\(.*?\)|<.*?>", " ", strip_invisible(name)).strip()
    if "," in clean:
        last, _, first = clean.partition(",")
        clean = f"{first.strip()} {last.strip()}"
    return [p.strip(".") for p in clean.split() if any(c.isalpha() for c in p) and len(p.strip(".")) >= 2]


def name_key(name: str) -> str:
    return fold(" ".join(name_parts(name)))


@dataclass
class _Variant:
    pattern: re.Pattern[str]
    needs_capital: bool
    length: int
    person: int  # index into the person list


def _variants(parts: list[str], person: int) -> list[_Variant]:
    out: list[_Variant] = []
    folded = [fold(p) for p in parts]

    def add(regex: str, needs_capital: bool) -> None:
        out.append(
            _Variant(re.compile(r"(?<![\w])" + regex + r"(?![\w])"), needs_capital, len(regex), person)
        )

    if len(parts) >= 2:
        add(r"\s+".join(re.escape(p) for p in folded), False)
        reverse = [folded[-1], *folded[:-1]]
        add(re.escape(reverse[0]) + r",?\s+" + r"\s+".join(re.escape(p) for p in reverse[1:]), False)
    for part in folded:
        if part not in _PARTICLES:
            add(re.escape(part), True)
    return out


# ---------------------------------------------------------------- greetings and signatures


def greeting_names(text: str) -> list[str]:
    """Names after a greeting word at the start of a line: 'Hi Tomás,' -> ['Tomás']."""
    found = []
    for match in _GREETING.finditer(text):
        words = []
        for word in match.group("name").split():
            word = word.strip(".")
            if not word[:1].isupper() or fold(word) in _NOT_NAMES:
                break
            words.append(word)
        if words:
            found.append(" ".join(words))
    return found


def _looks_like_name(line: str) -> bool:
    words = line.strip().rstrip(",").split()
    return (
        0 < len(words) <= 4
        and len(line.strip()) <= 40
        and all(w[:1].isupper() and not any(c.isdigit() for c in w) for w in words)
        and fold(words[0]) not in _NOT_NAMES
    )


def strip_signatures(text: str) -> tuple[str, list[str]]:
    """Find sign-off blocks. Returns the text with each block's lines after the name removed, and the
    names found. A block ends at a blank line, a quote marker or after six lines."""
    lines = text.split("\n")
    names: list[str] = []
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        if not _SIGN_OFF.match(line):
            continue
        j = i
        while j < len(lines) and not lines[j].strip() and j - i < 1:
            j += 1  # allow one blank line between the sign-off and the name
        if j >= len(lines) or not _looks_like_name(lines[j]):
            continue
        names.append(lines[j].strip().rstrip(","))
        out.extend(lines[i : j + 1])
        k = j + 1
        while k < len(lines) and k - j <= _SIGNATURE_LINES and not _SIGNATURE_END.match(lines[k]):
            k += 1
        if k > j + 1:
            out.append(SIGNATURE_MARK)
        i = k
    return "\n".join(out), names


# ---------------------------------------------------------------- people source


class PeopleSource(Protocol):
    async def people(self, task_id: str | None) -> list[KnownPerson]: ...


PARTICIPANTS_FACT = "participants"


ENCRYPTED_PREFIX = "enc:"


class LedgerPeople:
    """Directory entries plus the task's participants (task fact 'participants', a JSON list).

    With a cipher (the token-map key) the participant list is stored encrypted: real names and
    addresses live only in encrypted form in memory (F09). Plain lists written before are still read.
    """

    def __init__(
        self, memory: MemoryRepo, directory: DirectoryRepo, cipher: TokenCipher | None = None
    ) -> None:
        self._memory = memory
        self._directory = directory
        self._cipher = cipher

    async def people(self, task_id: str | None) -> list[KnownPerson]:
        out = [
            KnownPerson(name=row["name"], email=row["email"], role=(row.get("role") or "").lower() or None)
            for row in await self._directory.all()
        ]
        if task_id is not None:
            out.extend(await self.participants(task_id))
        return out

    async def participants(self, task_id: str) -> list[KnownPerson]:
        for fact in await self._memory.facts(task_id):
            if fact["key"] == PARTICIPANTS_FACT:
                value = fact["value"]
                if value.startswith(ENCRYPTED_PREFIX):
                    if self._cipher is None:
                        raise ValueError(
                            "participants are encrypted; LedgerPeople needs the token-map cipher"
                        )
                    value = self._cipher.decrypt(value[len(ENCRYPTED_PREFIX) :].encode())
                return [KnownPerson.model_validate(p) for p in json.loads(value)]
        return []

    async def remember(self, task_id: str, people: Iterable[KnownPerson]) -> None:
        """Merge people into the task's participants (by address, else by name); new roles win."""
        merged: dict[str, KnownPerson] = {}
        for person in [*await self.participants(task_id), *people]:
            key = (person.email or "").lower() or name_key(person.name)
            if not key:
                continue
            old = merged.get(key)
            if old is not None:
                person = KnownPerson(
                    name=person.name or old.name,
                    email=person.email or old.email,
                    role=person.role or old.role,
                )
            merged[key] = person
        value = json.dumps([p.model_dump() for p in merged.values()], sort_keys=True)
        if self._cipher is not None:
            value = ENCRYPTED_PREFIX + self._cipher.encrypt(value).decode()
        await self._memory.set_fact(task_id, PARTICIPANTS_FACT, value, source_ref="privacy")


# ---------------------------------------------------------------- the pseudonymiser


@dataclass
class _Person:
    name: str
    email: str | None
    role: str | None
    token: str | None = None  # known already (from the token map)


@dataclass
class _Run:
    """State for one pseudonymise() call: tokens in use and tokens handed out."""

    task_id: str | None
    used: set[str] = field(default_factory=set)

    def next(self, prefix: str) -> str:
        n = 1 + max((int(t.rsplit("_", 1)[1]) for t in self.used if t.startswith(prefix + "_")), default=0)
        token = f"{prefix}_{n}"
        self.used.add(token)
        return token


class Pseudonymiser:
    def __init__(
        self, tokens: TokenMapRepo, people: PeopleSource, config: PrivacyConfig | None = None
    ) -> None:
        self._tokens = tokens
        self._people = people
        config = config or PrivacyConfig()
        self._user_ids = [re.compile(p) for p in config.user_id_patterns]

    # ------------------------------------------------------------ public API

    async def pseudonymise(
        self, text: str, task_id: str | None, extra_people: Sequence[KnownPerson] = ()
    ) -> str:
        if not text:
            return text
        run = _Run(task_id, set((await self._tokens.mapping(task_id)).keys()))
        # Text that looks like a token the task does not have would be "restored" into a name later:
        # neutralise it. The task's own tokens stay (stored task events are already pseudonymised).
        text = TOKEN_RE.sub(
            lambda m: m.group(0) if m.group(0) in run.used else m.group(0).lower().replace("_", "-"), text
        )
        text, signed = strip_signatures(text)
        greeted = greeting_names(text)

        people = await self._known(task_id, extra_people)
        people += [_Person(name, None, None) for name in [*signed, *greeted]]

        spans: list[tuple[int, int, str]] = []

        def free(start: int, end: int) -> bool:
            return all(end <= s or start >= e for s, e, _ in spans)

        by_email = {p.email.lower(): p for p in people if p.email}
        for match in EMAIL_RE.finditer(text):
            address = match.group(0).lower()
            person = by_email.get(address) or _Person("", address, None)
            spans.append((match.start(), match.end(), await self._token(person, run)))

        folded = Folded(text)
        for variant in self._all_variants(people):
            for start, end in folded.finditer(variant.pattern):
                if variant.needs_capital and not text[start].isupper():
                    continue
                if free(start, end):
                    spans.append((start, end, await self._token(people[variant.person], run)))

        for start, end, kind, value in self._patterns(text):
            if free(start, end):
                spans.append((start, end, await self._value_token(kind, value, run)))

        for start, end, token in sorted(spans, reverse=True):
            text = text[:start] + token + text[end:]
        return text

    async def leak_check(self, text: str, task_id: str | None) -> None:
        """Raise LeakDetected if a known name, an e-mail address or a phone number remains.

        The message counts what leaked and never repeats it (it ends up in logs and DMs)."""
        leaks = await self.find_leaks(text, task_id)
        if leaks:
            raise LeakDetected(f"pseudonymisation left {', '.join(leaks)} in the text")

    async def find_leaks(self, text: str, task_id: str | None) -> list[str]:
        masked = TOKEN_RE.sub(" ", text)
        names = 0
        folded = Folded(masked)
        people = await self._known(task_id, ())
        for variant in self._all_variants(people):
            for start, _ in folded.finditer(variant.pattern):
                if not variant.needs_capital or masked[start].isupper():
                    names += 1
        emails = len(EMAIL_RE.findall(masked))
        phones = sum(1 for kind, *_ in self._pattern_hits(masked) if kind == "phone")
        out = []
        if names:
            out.append(f"{names} known name(s)")
        if emails:
            out.append(f"{emails} e-mail address(es)")
        if phones:
            out.append(f"{phones} phone number(s)")
        return out

    async def restore(self, draft: str, task_id: str | None) -> str:
        """Replace tokens with real values. Refuses tokens the task does not have (invented by the model)."""
        mapping = await self._tokens.mapping(task_id)
        unknown = sorted({m.group(0).upper() for m in _TOKEN_LIKE.finditer(draft)} - set(mapping))
        if unknown:
            raise UnknownToken(f"draft uses {', '.join(unknown)}, which this task does not have")
        return _TOKEN_LIKE.sub(lambda m: mapping[m.group(0).upper()], draft)

    # ------------------------------------------------------------ people and tokens

    async def _known(self, task_id: str | None, extra: Sequence[KnownPerson]) -> list[_Person]:
        """People from the source and the call, plus names found earlier and kept in the token map."""
        people = [_Person(p.name, p.email, p.role) for p in [*await self._people.people(task_id), *extra]]
        for token, value in (await self._tokens.mapping(task_id)).items():
            if token.startswith(("PHONE_", "UID_")):
                continue
            if "@" in value:
                people.append(_Person("", value, None, token))
            else:
                people.append(_Person(value, None, None, token))
        return people

    @staticmethod
    def _all_variants(people: list[_Person]) -> list[_Variant]:
        variants = [v for i, p in enumerate(people) for v in _variants(name_parts(p.name), i)]
        return sorted(variants, key=lambda v: (-v.length, v.needs_capital))

    async def _token(self, person: _Person, run: _Run) -> str:
        """The person's token for this task: found by address or name, else a new one for their role."""
        if person.token is not None:
            return person.token
        key = name_key(person.name) if person.name else ""
        email = person.email.lower() if person.email else None
        token = None
        if email:
            token = await self._tokens.token_for("email", email, run.task_id)
        if token is None and key:
            token = await self._tokens.token_for("person", key, run.task_id)
        if token is None:
            token = self._allocate(person.role, run)
        if key:
            await self._tokens.put(token, "person", " ".join(name_parts(person.name)), run.task_id, True, key)
        if email:
            await self._tokens.put(token, "email", email, run.task_id, canonical=not key, key=email)
        person.token = token
        return token

    @staticmethod
    def _allocate(role: str | None, run: _Run) -> str:
        role = (role or "").lower()
        fixed = ROLE_TOKENS.get(role)
        if fixed and fixed not in run.used:
            run.used.add(fixed)
            return fixed
        return run.next(ROLE_PREFIXES.get(role, "USER"))

    async def _value_token(self, kind: str, value: str, run: _Run) -> str:
        key = re.sub(r"\D", "", value) if kind == "phone" else value.lower()
        token = await self._tokens.token_for(kind, key, run.task_id)
        if token is None:
            token = run.next("PHONE" if kind == "phone" else "UID")
            token = await self._tokens.put(token, kind, value, run.task_id, True, key)
        return token

    def _patterns(self, text: str) -> list[tuple[int, int, str, str]]:
        return [(start, end, kind, value) for kind, start, end, value in self._pattern_hits(text)]

    def _pattern_hits(self, text: str) -> list[tuple[str, int, int, str]]:
        """(kind, start, end, value) for phone numbers and user ids."""
        hits: list[tuple[str, int, int, str]] = []
        for match in _PHONE_RE.finditer(text):
            value = match.group(0).strip()
            digits = re.sub(r"\D", "", value)
            if 8 <= len(digits) <= 15 and not _DATE_LIKE.match(value):
                hits.append(("phone", match.start(), match.start() + len(value), value))
        for pattern in [_DOMAIN_USER, *self._user_ids]:
            for match in pattern.finditer(text):
                hits.append(("uid", match.start(), match.end(), match.group(0)))
        return hits
