"""Shared bits for the respx tests of the Graph and Power BI adapters."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from agent.adapters.graph_client import GRAPH_BASE, POWERBI_BASE, GraphClient

NOW = datetime(2026, 3, 25, 4, 30, tzinfo=UTC)  # 10:00 IST


class Tokens:
    def __init__(self) -> None:
        self.calls = 0

    def get_token(self, scopes: list[str]) -> str:
        self.calls += 1
        return f"token-{self.calls}"


class Health:
    def __init__(self) -> None:
        self.reports: list[tuple[str, str, str | None]] = []

    async def report(self, connector: str, status: str, error: str | None = None) -> None:
        self.reports.append((connector, status, error))

    def last(self) -> str:
        return self.reports[-1][1]


class Kv:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def client(
    http: httpx.AsyncClient,
    connector: str = "graph",
    base: str = GRAPH_BASE,
    tokens: Any = None,
    health: Health | None = None,
    sleep: Sleeps | None = None,
    now: Callable[[], datetime] = lambda: NOW,
) -> GraphClient:
    return GraphClient(
        http,
        tokens or Tokens(),
        ["https://graph.microsoft.com/.default"],
        connector,
        base_url=base,
        health=health,
        sleep=sleep or Sleeps(),
        now=now,
    )


G = GRAPH_BASE
P = POWERBI_BASE
