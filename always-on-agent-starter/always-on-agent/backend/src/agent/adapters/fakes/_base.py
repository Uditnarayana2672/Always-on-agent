"""Shared behaviour for every fake: fixtures, write recording and failure injection."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from agent.adapters.base import HealthStatus, ToolSpec
from agent.core.clock import Clock
from agent.core.errors import AdapterError

FIXTURES_DIR = Path(__file__).resolve().parents[5] / "fixtures"

FailureKind = Literal["429", "410", "timeout", "500"]


def parse_time(value: str) -> datetime:
    """Graph timestamps: '2026-03-25T03:00:00Z' or with fractional seconds."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class WriteCall(BaseModel):
    method: str
    args: dict[str, Any]
    at: datetime


class _Failure(BaseModel):
    kind: FailureKind
    remaining: int
    method: str | None


class FakeAdapter:
    """Base class. Subclasses call self._check(method) first in every public method."""

    name = "fake"

    def __init__(self, clock: Clock, fixtures: Path = FIXTURES_DIR) -> None:
        self.clock = clock
        self.fixtures = fixtures
        self.writes: list[WriteCall] = []
        self._failures: list[_Failure] = []

    # -------------------------------------------------- test controls

    def fail_next(self, kind: FailureKind, times: int = 1, method: str | None = None) -> None:
        """Make the next `times` calls (optionally only to `method`) fail with `kind`."""
        self._failures.append(_Failure(kind=kind, remaining=times, method=method))

    def writes_to(self, method: str) -> list[WriteCall]:
        return [w for w in self.writes if w.method == method]

    # -------------------------------------------------- helpers for subclasses

    def _load(self, relative: str) -> Any:
        return json.loads((self.fixtures / relative).read_text(encoding="utf-8"))

    def _check(self, method: str) -> None:
        for failure in self._failures:
            if failure.remaining > 0 and failure.method in (None, method):
                failure.remaining -= 1
                raise _error(self.name, method, failure.kind)
        self._failures = [f for f in self._failures if f.remaining > 0]

    def _record(self, method: str, **args: Any) -> None:
        self._check(method)
        self.writes.append(WriteCall(method=method, args=args, at=self.clock.now_utc()))

    async def health(self) -> HealthStatus:
        try:
            self._check("health")
        except AdapterError as exc:
            return HealthStatus(connector=self.name, status="down", detail=str(exc))
        return HealthStatus(connector=self.name, status="ok", detail="fake")

    def tools(self) -> list[ToolSpec]:
        return []


def _error(name: str, method: str, kind: FailureKind) -> AdapterError:
    if kind == "429":
        return AdapterError(f"{name}.{method}: throttled", retryable=True, status_code=429)
    if kind == "410":
        return AdapterError(f"{name}.{method}: delta token expired", retryable=False, status_code=410)
    if kind == "500":
        return AdapterError(f"{name}.{method}: server error", retryable=True, status_code=500)
    return AdapterError(f"{name}.{method}: timed out", retryable=True)
