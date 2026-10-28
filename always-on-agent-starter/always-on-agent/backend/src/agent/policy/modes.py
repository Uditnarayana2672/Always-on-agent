"""Action modes and policy.yaml (architecture 3.3, F06)."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from agent.adapters.base import Mode
from agent.core.config import CONFIG_DIR
from agent.core.errors import ConfigError

RANK: dict[str, int] = {"silent": 0, "report": 1, "ask": 2, "hand_off": 3}


def stricter(a: Mode, b: Mode) -> Mode:
    return a if RANK[a] >= RANK[b] else b


class PolicyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Mode
    locked: bool = False

    @model_validator(mode="after")
    def _locked_is_hand_off(self) -> "PolicyEntry":
        if self.locked and self.mode != "hand_off":
            raise ValueError("only Hand off entries can be locked")
        return self


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: dict[str, PolicyEntry]

    def mode_of(self, tool: str) -> Mode | None:
        entry = self.tools.get(tool)
        return entry.mode if entry else None

    def locked(self, tool: str) -> bool:
        entry = self.tools.get(tool)
        return bool(entry and entry.locked)


def load_policy(path: Path | None = None) -> Policy:
    path = path or CONFIG_DIR / "policy.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Policy.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid policy in {path}: {exc}") from exc
