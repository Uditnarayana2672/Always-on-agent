"""Secrets in Windows Credential Manager via keyring (F17)."""

import keyring

from agent.core.errors import ConfigError
from agent.core.logging import register_secret

SERVICE = "always-on-agent"


def get_secret(name: str) -> str:
    """Read a secret; raises ConfigError if it is not set."""
    value = keyring.get_password(SERVICE, name)
    if value is None:
        raise ConfigError(f"secret {name!r} is not set; run: agentd secret set {name}")
    register_secret(value)
    return value


def set_secret(name: str, value: str) -> None:
    if not value:
        raise ConfigError("secret value is empty")
    keyring.set_password(SERVICE, name, value)
    register_secret(value)
