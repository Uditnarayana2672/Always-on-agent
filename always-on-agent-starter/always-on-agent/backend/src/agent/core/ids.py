"""ULIDs and task id helpers (F03, F08)."""

import os
import re
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_TASK_PATTERNS = (
    re.compile(r"^INC\d{8}$"),
    re.compile(r"^ENH-\d+$"),
    re.compile(r"^REF-\d+$"),
    re.compile(r"^PBI-[A-Z0-9]+$"),
)
_INC_IN_TEXT = re.compile(r"\bINC\d{8}\b", re.IGNORECASE)


def new_ulid(timestamp_ms: int | None = None) -> str:
    """A 26-character ULID: 48-bit millisecond time and 80 random bits, Crockford base32."""
    ms = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    if not 0 <= ms < 2**48:
        raise ValueError("timestamp out of range")
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def normalise_task_id(raw: str) -> str | None:
    """Return the canonical task id (upper case) or None if it is not a known form."""
    candidate = raw.strip().upper()
    for pattern in _TASK_PATTERNS:
        if pattern.match(candidate):
            return candidate
    return None


def is_task_id(raw: str) -> bool:
    return normalise_task_id(raw) is not None


def find_inc_numbers(text: str) -> list[str]:
    """INC numbers found in free text, upper-cased, in order, without duplicates."""
    seen: dict[str, None] = {}
    for match in _INC_IN_TEXT.findall(text):
        seen.setdefault(match.upper(), None)
    return list(seen)
