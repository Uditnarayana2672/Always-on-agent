"""HTTP client shared by the Graph adapters (A01-A03) and Power BI (A05).

One place for the rules every Microsoft REST call follows:
- tokens come from GraphAuth silently; a token that cannot be refreshed raises NeedsSignIn and marks
  the connector down so the brief shows a "sign in again" hand-off (F17, E27);
- a 401 gets one retry with a fresh token (the cached one may have just expired);
- 429 and 503 wait for Retry-After (seconds or an HTTP date, capped), 5xx back off, at most
  MAX_ATTEMPTS calls, then AdapterError(retryable=True, status_code=429|5xx);
- 410 (delta token expired) and 403 (no permission) are typed errors the caller handles;
- every outcome is reported to the health sink: ok, degraded while throttled, down when signed out.

Response bodies are never logged; only method, path and status.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from agent.core.errors import AdapterError, NeedsSignIn

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
POWERBI_BASE = "https://api.powerbi.com/v1.0/myorg"
MAX_ATTEMPTS = 4
MAX_RETRY_AFTER = 120.0
BACKOFF = (2.0, 8.0, 30.0)
MAX_PAGES = 200  # a runaway nextLink loop stops here


class TokenProvider(Protocol):
    def get_token(self, scopes: list[str]) -> str: ...


class HealthSink(Protocol):
    """HealthRepo.report has this shape."""

    async def report(self, connector: str, status: str, error: str | None = None) -> None: ...


Sleep = Callable[[float], Awaitable[None]]


class Forbidden(AdapterError):
    """403: the signed-in user lacks permission. Callers turn it into a hand-off."""


class NotFound(AdapterError):
    """404: the item is gone (or was never visible to this user)."""


class DeltaExpired(AdapterError):
    """410 Gone or SyncStateNotFound: the stored delta token is no longer valid; resync."""


def retry_after_seconds(value: str | None, now: datetime, default: float) -> float:
    """Retry-After as seconds. Accepts a number or an HTTP date; never negative, capped."""
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return default
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now).total_seconds()
    return max(0.0, min(seconds, MAX_RETRY_AFTER))


class GraphClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenProvider,
        scopes: list[str],
        connector: str,
        base_url: str = GRAPH_BASE,
        health: HealthSink | None = None,
        sleep: Sleep = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http
        self._tokens = tokens
        self._scopes = scopes
        self.connector = connector
        self.base_url = base_url.rstrip("/")
        self._health = health
        self._sleep = sleep
        self._now = now
        self._token: str | None = None

    # ------------------------------------------------------------ public

    async def get(self, path: str, params: Mapping[str, str] | None = None, **kw: Any) -> dict[str, Any]:
        return await self.request("GET", path, params=params, **kw)

    async def post(self, path: str, json: Any = None, **kw: Any) -> dict[str, Any]:
        return await self.request("POST", path, json=json, **kw)

    async def patch(self, path: str, json: Any = None, **kw: Any) -> dict[str, Any]:
        return await self.request("PATCH", path, json=json, **kw)

    async def delete(self, path: str, **kw: Any) -> dict[str, Any]:
        return await self.request("DELETE", path, **kw)

    async def get_text(self, path: str, params: Mapping[str, str] | None = None) -> str:
        response = await self._send("GET", path, params=params)
        return response.text

    async def get_all(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        max_items: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Follow @odata.nextLink and return every item of 'value'."""
        out: list[dict[str, Any]] = []
        async for page in self.pages(path, params, headers):
            out.extend(page.get("value", []))
            if max_items is not None and len(out) >= max_items:
                return out[:max_items]
        return out

    async def pages(
        self, path: str, params: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over pages, following nextLink (absolute URLs carry their own query)."""
        url: str = path
        query: Mapping[str, str] | None = params
        for _ in range(MAX_PAGES):
            body = await self.get(url, params=query, headers=dict(headers or {}))
            yield body
            next_link = body.get("@odata.nextLink")
            if not next_link:
                return
            url, query = str(next_link), None
        raise AdapterError(f"{self.connector}: more than {MAX_PAGES} pages from {path.split('?')[0]}")

    async def request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        response = await self._send(method, path, **kw)
        if response.status_code == 204 or not response.content:
            return {"_headers": dict(response.headers)} if response.headers else {}
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            return {"_text": response.text}
        if isinstance(body, dict):
            body.setdefault("_headers", dict(response.headers))
            return body
        return {"value": body}

    # ------------------------------------------------------------ internals

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"

    def _get_token(self, fresh: bool = False) -> str:
        if self._token is None or fresh:
            try:
                self._token = self._tokens.get_token(self._scopes)
            except NeedsSignIn:
                self._token = None
                raise
        return self._token

    async def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        url = self._url(path)
        short = url.split("?")[0].replace(self.base_url, "")
        refreshed = False
        last: httpx.Response | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                token = self._get_token(fresh=refreshed)
            except NeedsSignIn as exc:
                await self._report("down", f"sign-in needed: {exc}")
                raise
            headers = {"Authorization": f"Bearer {token}", **kw.pop("headers", {})}
            try:
                response = await self._http.request(method, url, headers=headers, timeout=60, **kw)
            except httpx.TransportError as exc:
                if attempt + 1 >= MAX_ATTEMPTS:
                    await self._report("down", f"{type(exc).__name__}")
                    raise AdapterError(
                        f"{self.connector} {method} {short}: {type(exc).__name__}", retryable=True
                    ) from exc
                await self._sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
                continue
            last = response
            status = response.status_code
            kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
            if status < 400:
                await self._report("ok")
                return response
            if status == 401 and not refreshed:
                refreshed = True
                continue
            if status in (429, 503, 504) or status >= 500:
                if attempt + 1 >= MAX_ATTEMPTS:
                    break
                default = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                wait = retry_after_seconds(response.headers.get("Retry-After"), self._now(), default)
                await self._report("degraded", f"HTTP {status}; retrying in {wait:.0f} s")
                log.info("throttled", extra={"connector": self.connector, "path": short, "wait": wait})
                await self._sleep(wait)
                continue
            raise await self._error(method, short, response)
        assert last is not None
        await self._report("degraded", f"HTTP {last.status_code} after {MAX_ATTEMPTS} attempts")
        raise AdapterError(
            f"{self.connector} {method} {short}: HTTP {last.status_code} after {MAX_ATTEMPTS} attempts",
            retryable=True,
            status_code=last.status_code,
        )

    async def _error(self, method: str, short: str, response: httpx.Response) -> AdapterError:
        status = response.status_code
        code, message = _error_parts(response)
        text = f"{self.connector} {method} {short}: HTTP {status} {code}".strip()
        if status == 401:
            await self._report("down", "token rejected; sign in again")
            return AdapterError(text + " (sign in again)", status_code=401)
        if status == 410 or code in ("SyncStateNotFound", "resyncRequired", "SyncStateInvalid"):
            return DeltaExpired(text, status_code=410)
        if status == 403:
            return Forbidden(text + (f": {message}" if message else ""), status_code=403)
        if status == 404:
            return NotFound(text, status_code=404)
        return AdapterError(text + (f": {message}" if message else ""), status_code=status)

    async def _report(self, status: str, error: str | None = None) -> None:
        if self._health is None:
            return
        try:
            await self._health.report(self.connector, status, error)
        except Exception:  # health must never break the call it describes
            log.exception("could not record connector health", extra={"connector": self.connector})


def _error_parts(response: httpx.Response) -> tuple[str, str]:
    """(code, message) from a Graph or Power BI error body; message trimmed, never the full body."""
    try:
        body = response.json()
    except ValueError:
        return "", ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("code") or ""), str(error.get("message") or "")[:200]
    return "", ""
