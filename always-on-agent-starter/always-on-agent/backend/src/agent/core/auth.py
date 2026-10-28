"""Microsoft sign-in with MSAL and an encrypted token cache (F17).

Public client, delegated permissions. The cache is encrypted with DPAPI on Windows via msal-extensions.
Graph and Power BI are separate resources, so tokens are requested per resource.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import msal
from msal_extensions import PersistedTokenCache, build_encrypted_persistence

from agent.core.config import Config
from agent.core.errors import ConfigError, NeedsSignIn
from agent.core.logging import register_secret

log = logging.getLogger(__name__)

CACHE_PATH = Path.home() / ".always-on-agent" / "msal_token_cache.bin"
POWERBI_RESOURCE = "https://analysis.windows.net/powerbi/api"


def graph_scopes(config: Config) -> list[str]:
    return [s if "://" in s else f"https://graph.microsoft.com/{s}" for s in config.graph.scopes]


def powerbi_scopes(config: Config) -> list[str]:
    return [s if "://" in s else f"{POWERBI_RESOURCE}/{s}" for s in config.powerbi.scopes]


class GraphAuth:
    """get_token tries silent first and raises NeedsSignIn otherwise."""

    def __init__(self, config: Config, cache_path: Path = CACHE_PATH, app: Any | None = None) -> None:
        if not config.graph.tenant_id or not config.graph.client_id:
            raise ConfigError("graph.tenant_id and graph.client_id are not set (no app registration yet)")
        if app is None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache = PersistedTokenCache(build_encrypted_persistence(str(cache_path)))
            app = msal.PublicClientApplication(
                config.graph.client_id,
                authority=f"https://login.microsoftonline.com/{config.graph.tenant_id}",
                token_cache=cache,
            )
        self._app = app

    def get_token(self, scopes: list[str]) -> str:
        for account in self._app.get_accounts():
            result = self._app.acquire_token_silent(scopes, account=account)
            if result and "access_token" in result:
                return self._accept(result)
        raise NeedsSignIn(f"sign-in needed for {', '.join(scopes)}")

    def sign_in_device_code(self, scopes: list[str], show: Callable[[str], None] = print) -> str:
        flow = self._app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise NeedsSignIn(f"device code flow failed: {flow.get('error_description', flow.get('error'))}")
        show(flow["message"])
        return self._accept(self._app.acquire_token_by_device_flow(flow))

    def sign_in_interactive(self, scopes: list[str]) -> str:
        return self._accept(self._app.acquire_token_interactive(scopes=scopes, prompt="select_account"))

    @staticmethod
    def _accept(result: dict[str, Any]) -> str:
        if "access_token" not in result:
            raise NeedsSignIn(
                f"sign-in failed: {result.get('error_description', result.get('error', 'unknown'))}"
            )
        token: str = result["access_token"]
        register_secret(token)
        return token
