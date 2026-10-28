from typing import Any

import pytest

from agent.core.auth import GraphAuth, graph_scopes, powerbi_scopes
from agent.core.config import Config
from agent.core.errors import ConfigError, NeedsSignIn
from agent.core.logging import redact
from tests.core.conftest import with_overrides


class FakeMsal:
    def __init__(self, accounts: list[dict[str, str]], silent: dict[str, Any] | None) -> None:
        self.accounts = accounts
        self.silent = silent

    def get_accounts(self) -> list[dict[str, str]]:
        return self.accounts

    def acquire_token_silent(self, scopes: list[str], account: dict[str, str]) -> dict[str, Any] | None:
        return self.silent

    def initiate_device_flow(self, scopes: list[str]) -> dict[str, str]:
        return {"user_code": "ABCD", "message": "Go to https://microsoft.com/devicelogin and enter ABCD"}

    def acquire_token_by_device_flow(self, flow: dict[str, str]) -> dict[str, Any]:
        return {"access_token": "device-token-value-123"}


@pytest.fixture
def registered(config: Config) -> Config:
    return with_overrides(config, graph={"tenant_id": "tenant", "client_id": "client"})


def test_needs_app_registration(config: Config) -> None:
    with pytest.raises(ConfigError):
        GraphAuth(config)


def test_silent_token_and_redaction(registered: Config) -> None:
    auth = GraphAuth(
        registered, app=FakeMsal([{"username": "u"}], {"access_token": "silent-token-value-456"})
    )
    assert auth.get_token(graph_scopes(registered)) == "silent-token-value-456"
    assert "silent-token-value-456" not in redact("token silent-token-value-456")


@pytest.mark.parametrize(
    ("accounts", "silent"),
    [([], None), ([{"username": "u"}], None), ([{"username": "u"}], {"error": "invalid_grant"})],
)
def test_needs_sign_in(
    registered: Config, accounts: list[dict[str, str]], silent: dict[str, Any] | None
) -> None:
    auth = GraphAuth(registered, app=FakeMsal(accounts, silent))
    with pytest.raises(NeedsSignIn):
        auth.get_token(graph_scopes(registered))


def test_device_code_shows_message(registered: Config) -> None:
    shown: list[str] = []
    auth = GraphAuth(registered, app=FakeMsal([], None))
    assert auth.sign_in_device_code(["x"], show=shown.append) == "device-token-value-123"
    assert "ABCD" in shown[0]


def test_scopes_are_resource_qualified(registered: Config) -> None:
    assert "https://graph.microsoft.com/Mail.ReadWrite" in graph_scopes(registered)
    assert all(s.startswith("https://analysis.windows.net/powerbi/api/") for s in powerbi_scopes(registered))
