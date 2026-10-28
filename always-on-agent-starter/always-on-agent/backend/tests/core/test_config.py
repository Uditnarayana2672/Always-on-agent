from pathlib import Path
from typing import Any

import pytest
import yaml

from agent.core.config import CONFIG_DIR, Config, load_config
from agent.core.errors import ConfigError


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def raw() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((CONFIG_DIR / "config.example.yaml").read_text(encoding="utf-8"))
    return data


def test_example_is_valid(raw: dict[str, Any], tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, raw))
    assert isinstance(cfg, Config)
    assert cfg.console.port == 8765
    assert cfg.data_rule.level == "metadata"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("timezone",), "Asia/Nowhere"),
        (("caller_timezone",), "Mars/Base"),
        (("schedule", "brief"), "9:00"),
        (("notifications", "quiet_hours", "start"), "25:00"),
        (("work", "end"), "18:60"),
        (("data_rule", "level"), "everything"),
        (("work", "days"), ["monday"]),
        (("work", "holidays"), ["26/03/2026"]),
        (("console", "host"), "0.0.0.0"),
    ],
)
def test_invalid_values_rejected(
    raw: dict[str, Any], tmp_path: Path, path: tuple[str, ...], value: object
) -> None:
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, raw))


def test_unknown_key_rejected(raw: dict[str, Any], tmp_path: Path) -> None:
    raw["surprise"] = 1
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, raw))


def test_missing_section_rejected(raw: dict[str, Any], tmp_path: Path) -> None:
    del raw["workers"]
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, raw))


def test_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")
