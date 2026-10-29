import json
import logging
from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

from agent.core.errors import ConfigError
from agent.core.logging import MASK, JsonFormatter, RedactionFilter, redact, register_secret, setup_logging
from agent.core.secrets import SERVICE, get_secret, set_secret


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def", "eyJhbGciOiJIUzI1NiJ9"),
        ("slack bot xoxb-1234-5678-abcdefghijkl", "xoxb-1234"),
        ("slack app xapp-1-A0123-456-abcdef", "xapp-1"),
        ("key sk-ant-api03-ABCDEFGHIJKLMNOPqrstuv", "sk-ant-api03"),
        ("api_key=supersecretvalue123", "supersecretvalue123"),
        ('{"client_secret": "s3cr3t-val"}', "s3cr3t-val"),
        ("mail from anna.keller@example.com about INC30114872", "anna.keller@example.com"),
    ],
)
def test_redacts_secret_types(text: str, leak: str) -> None:
    out = redact(text)
    assert leak not in out
    assert MASK in out


def test_keeps_ordinary_text() -> None:
    assert redact("INC30114872 moved to On Hold") == "INC30114872 moved to On Hold"


def test_runtime_registered_secret() -> None:
    register_secret("plain-looking-value-42")
    assert "plain-looking-value-42" not in redact("value is plain-looking-value-42 here")


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)


def test_filter_and_formatter_emit_json() -> None:
    record = _record("token %s for %s", "xoxb-1-2-3", "bob@example.org")
    assert RedactionFilter().filter(record)
    record.task_id = "INC30114872"
    line = JsonFormatter().format(record)
    entry = json.loads(line)
    assert entry["level"] == "INFO"
    assert entry["task_id"] == "INC30114872"
    assert "xoxb" not in line and "bob@" not in line


def test_setup_logging_writes_file(tmp_path: Path) -> None:
    setup_logging(tmp_path)
    try:
        logging.getLogger("x").info("hello bob@example.org")
        for h in logging.getLogger().handlers:
            h.flush()
        content = (tmp_path / "agent.log").read_text(encoding="utf-8")
        assert "hello" in content and "bob@" not in content
        handler = next(h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler))
        assert getattr(handler, "backupCount", None) == 14
    finally:
        for h in list(logging.getLogger().handlers):
            h.close()
            logging.getLogger().removeHandler(h)


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def test_secret_roundtrip_registers_for_redaction(memory_keyring: MemoryKeyring) -> None:
    set_secret("ica", "ica-key-value-9876")
    assert memory_keyring.store[(SERVICE, "ica")] == "ica-key-value-9876"
    assert get_secret("ica") == "ica-key-value-9876"
    assert "ica-key-value-9876" not in redact("using ica-key-value-9876")


def test_missing_secret(memory_keyring: MemoryKeyring) -> None:
    with pytest.raises(ConfigError):
        get_secret("nope")


def test_no_credential_store_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux CI or a locked-down laptop without a keyring backend: a clear error, not a crash."""
    from keyring.errors import NoKeyringError

    def broken(*_: object) -> None:
        raise NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "get_password", broken)
    monkeypatch.setattr(keyring, "set_password", broken)
    with pytest.raises(ConfigError, match="no credential store"):
        get_secret("ica")
    with pytest.raises(ConfigError, match="no credential store"):
        set_secret("ica", "x")
