import importlib

import pytest
from typer.testing import CliRunner

from agent import __version__
from agent.cli import app

SUBPACKAGES = [
    "core",
    "db",
    "runtime",
    "graphs",
    "model",
    "policy",
    "approvals",
    "ledger",
    "memory",
    "privacy",
    "recipes",
    "adapters",
    "adapters.fakes",
    "channels",
    "api",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    module = importlib.import_module(f"agent.{name}")
    assert module.__doc__


def test_status_prints_version() -> None:
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert __version__ in result.output
