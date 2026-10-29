"""Secrets in Windows Credential Manager via keyring (F17)."""

import keyring
from keyring.errors import KeyringError

from agent.core.errors import ConfigError
from agent.core.logging import register_secret

SERVICE = "always-on-agent"


def get_secret(name: str) -> str:
    """Read a secret; raises ConfigError if it is not set or there is no credential store."""
    try:
        value = keyring.get_password(SERVICE, name)
    except KeyringError as exc:
        raise ConfigError(f"no credential store to read {name!r} from ({type(exc).__name__})") from exc
    if value is None:
        raise ConfigError(f"secret {name!r} is not set; run: agentd secret set {name}")
    register_secret(value)
    return value


def set_secret(name: str, value: str) -> None:
    if not value:
        raise ConfigError("secret value is empty")
    try:
        keyring.set_password(SERVICE, name, value)
    except KeyringError as exc:
        raise ConfigError(f"no credential store to save {name!r} in ({type(exc).__name__})") from exc
    register_secret(value)
