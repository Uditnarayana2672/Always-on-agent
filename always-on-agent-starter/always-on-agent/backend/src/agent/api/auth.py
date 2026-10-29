"""Console auth (F15, F17): one local user, one browser, nothing reachable from elsewhere.

- A random session token is made at start and kept in Credential Manager (console_session), so a
  restart of agentd keeps the browser signed in.
- `agentd console` prints a one-time link (/api/auth/login?code=...). The code works once, for ten
  minutes; the login sets the session cookie (HttpOnly, SameSite=Strict, path /) and redirects to /.
- Every /api route except the login needs the session: the cookie or an X-Agent-Token header.
  State-changing routes also need the X-Agent-Request header, which a cross-site form cannot set.
- Requests must name 127.0.0.1 or localhost as Host (no DNS rebinding) and, when the browser sends an
  Origin, it must be this console's own. CORS is never enabled.
"""

import contextlib
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from fastapi import HTTPException, Request

from agent.core.errors import AgentError

COOKIE = "agent_session"
TOKEN_HEADER = "X-Agent-Token"
REQUEST_HEADER = "X-Agent-Request"
SECRET_NAME = "console_session"
CODE_TTL = timedelta(minutes=10)
CODES_KEY = "console.login_codes"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class Kv(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str) -> None: ...


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ConsoleAuth:
    """Codes live in the agent's kv table: `agentd console` runs in its own process and writes one
    there; the running agentd redeems it. Only digests are stored."""

    def __init__(
        self,
        kv: Kv,
        now: Callable[[], datetime],
        token: str | None = None,
        port: int = 8765,
        load: Callable[[str], str] | None = None,
        store: Callable[[str, str], None] | None = None,
    ) -> None:
        self.port = port
        self._kv = kv
        self._now = now
        if token is None:
            token = _load_or_create(load, store)
        self.token = token

    # ------------------------------------------------------------ one-time links

    async def _codes(self) -> dict[str, str]:
        raw = await self._kv.get(CODES_KEY)
        codes: dict[str, str] = json.loads(raw) if raw else {}
        now = self._now()
        return {d: t for d, t in codes.items() if datetime.fromisoformat(t) > now}

    async def one_time_code(self) -> str:
        code = secrets.token_urlsafe(24)
        codes = await self._codes()
        codes[_digest(code)] = (self._now() + CODE_TTL).isoformat()
        await self._kv.set(CODES_KEY, json.dumps(codes))
        return code

    async def login_link(self, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}/api/auth/login?code={await self.one_time_code()}"

    async def redeem(self, code: str) -> bool:
        """True once per code, within its ten minutes."""
        codes = await self._codes()
        found = codes.pop(_digest(code), None) is not None
        await self._kv.set(CODES_KEY, json.dumps(codes))
        return found

    # ------------------------------------------------------------ checks

    def check_host(self, request: Request) -> None:
        host = (request.headers.get("host") or "").split(":")[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise HTTPException(status_code=403, detail="the console only answers on 127.0.0.1")
        origin = request.headers.get("origin")
        if origin:
            allowed = {
                f"http://127.0.0.1:{self.port}",
                f"http://localhost:{self.port}",
                "http://127.0.0.1:5173",  # npm run dev proxies /api from here
                "http://localhost:5173",
            }
            if origin.rstrip("/") not in allowed:
                raise HTTPException(status_code=403, detail="cross-site request refused")

    def authorised(self, request: Request) -> bool:
        supplied = request.headers.get(TOKEN_HEADER) or request.cookies.get(COOKIE) or ""
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    def require(self, request: Request) -> None:
        self.check_host(request)
        if not self.authorised(request):
            raise HTTPException(status_code=401, detail="open the console with the link from: agentd console")
        if request.method not in SAFE_METHODS and not request.headers.get(REQUEST_HEADER):
            raise HTTPException(status_code=403, detail="missing X-Agent-Request header")


def _load_or_create(load: Callable[[str], str] | None, store: Callable[[str, str], None] | None) -> str:
    if load is not None:
        try:
            return load(SECRET_NAME)
        except AgentError:
            pass
    token = secrets.token_urlsafe(32)
    if store is not None:
        # No Credential Manager (tests, CI): the token lives for this run only.
        with contextlib.suppress(AgentError):
            store(SECRET_NAME, token)
    return token
