"""Async ICA client for the OpenAI-compatible chat-models API (F05).

ICA exposes GET /models and POST /chat/completions under ICA_BASE_URL with a Bearer key.
"""

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from agent.core.config import Config, load_env_file
from agent.core.errors import AgentError, ConfigError, ModelUnavailable
from agent.core.secrets import get_secret

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 5
BASE_DELAY = 1.0
MAX_DELAY = 30.0
MAX_RETRY_AFTER = 120.0


class IcaRequestError(AgentError):
    """ICA refused the request (4xx other than 429). Not retried."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"ICA HTTP {status_code}: {detail[:300]}")
        self.status_code = status_code
        self.detail = detail


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error") or body
        if isinstance(detail, dict):
            return str(detail.get("message", detail))
        return str(detail)
    return str(body)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(value)))
    except ValueError:
        return None


class IcaClient:
    """httpx client with exponential backoff on 429, 5xx and transport errors."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_ids: dict[str, str],
        *,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self.model_ids = model_ids
        self._max_attempts = max_attempts
        self._sleep = sleep

    @classmethod
    def from_config(cls, config: Config) -> "IcaClient":
        load_env_file()
        base_url = os.environ.get(config.ica.base_url_env)
        if not base_url:
            raise ConfigError(f"environment variable {config.ica.base_url_env} is not set")
        return cls(base_url, get_secret("ica"), config.models.ids.model_dump())

    def model_id(self, tier: str) -> str:
        model = self.model_ids.get(tier, "")
        if not model:
            raise ConfigError(f"no model id configured for tier {tier!r}")
        return model

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /chat/completions with retries. Returns the response JSON."""
        return await self._request("POST", "/chat/completions", payload)

    async def list_models(self) -> list[str]:
        body = await self._request("GET", "/models", None)
        return [m["id"] for m in body.get("data", [])]

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        url = self._base_url + path
        for attempt in range(1, self._max_attempts + 1):
            delay = min(MAX_DELAY, BASE_DELAY * 2 ** (attempt - 1)) * (0.5 + random.random() / 2)
            try:
                response = await self._http.request(method, url, headers=self._headers, json=payload)
            except httpx.TransportError as exc:  # connection errors and timeouts
                reason = type(exc).__name__
            else:
                if response.status_code < 400:
                    body: dict[str, Any] = response.json()
                    return body
                if response.status_code != 429 and response.status_code < 500:
                    raise IcaRequestError(response.status_code, _detail(response))
                reason = f"HTTP {response.status_code}"
                delay = _retry_after(response) or delay
            if attempt == self._max_attempts:
                raise ModelUnavailable(f"ICA failed after {attempt} attempts: {reason}")
            log.warning("ICA call failed, retrying", extra={"attempt": attempt, "reason": reason})
            await self._sleep(delay)
        raise AssertionError("unreachable")
