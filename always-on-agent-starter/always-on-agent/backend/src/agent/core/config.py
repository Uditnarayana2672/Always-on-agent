"""Settings loaded from config/config.yaml (F01)."""

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent.core.errors import ConfigError

log = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

_HH_MM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DataRuleLevel = Literal["metadata", "text", "chat", "images"]


def _check_hh_mm(value: str) -> str:
    if not _HH_MM.match(value):
        raise ValueError(f"time must be HH:MM, got {value!r}")
    return value


def _check_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {value!r}") from exc
    return value


HHMM = Annotated[str, AfterValidator(_check_hh_mm)]
Zone = Annotated[str, AfterValidator(_check_zone)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkConfig(_Model):
    days: list[Weekday]
    start: HHMM
    end: HHMM
    holidays: list[str] = Field(default_factory=list)

    @field_validator("holidays")
    @classmethod
    def _iso_dates(cls, value: list[str]) -> list[str]:
        for item in value:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", item):
                raise ValueError(f"holiday must be YYYY-MM-DD, got {item!r}")
        return value


class PollMinutes(_Model):
    mail: int = Field(gt=0)
    teams: int = Field(gt=0)
    powerbi: int = Field(gt=0)
    servicenow_report: int = Field(gt=0)


class ScheduleConfig(_Model):
    brief: HHMM
    checkin_every_minutes: int = Field(gt=0)
    nightly_digest: HHMM
    poll_minutes: PollMinutes
    chase_window_local_caller: HHMM


class QuietHours(_Model):
    start: HHMM
    end: HHMM


class NotificationsConfig(_Model):
    primary: Literal["slack", "teams_self", "email_self"]
    max_pushes_per_day: int = Field(ge=0)
    quiet_hours: QuietHours


class ModelIds(_Model):
    haiku: str
    sonnet: str
    opus: str


class ModelsConfig(_Model):
    default: Literal["haiku", "sonnet", "opus"]
    ids: ModelIds
    sonnet_daily_limit: int = Field(ge=0)
    sonnet_for: list[str]


class IcaConfig(_Model):
    base_url_env: str
    key_ref: str


class GraphConfig(_Model):
    tenant_id: str
    client_id: str
    scopes: list[str]


class PowerBIConfig(_Model):
    scopes: list[str]
    refresh_cap_per_day: int = Field(ge=0)


class DataRuleConfig(_Model):
    level: DataRuleLevel


class ClockConfig(_Model):
    start: Literal["opened", "assigned"]
    pause_on_hold: bool
    thresholds_hours: list[int]


class WorkersConfig(_Model):
    pool_size: int = Field(gt=0)
    max_steps_per_run: int = Field(gt=0)
    job_timeout_seconds: int = Field(gt=0)
    lock_ttl_minutes: int = Field(gt=0)


class RetentionConfig(_Model):
    raw_text_days: int = Field(ge=0)
    screenshots_days: int = Field(ge=0)
    transcripts: Literal["delete_after_extract", "keep"]


class PathsConfig(_Model):
    workbook: str
    rights_matrix: str
    specs: list[str]
    screenshots_inbox: str


class ConsoleConfig(_Model):
    host: Literal["127.0.0.1"]
    port: int = Field(gt=0, lt=65536)


class DatabaseConfig(_Model):
    url: str = "sqlite+aiosqlite:///agent.db"


class SlackConfig(_Model):
    user_id: str = ""  # the user's Slack member id (U...), for DMs


class Config(_Model):
    timezone: Zone
    caller_timezone: Zone
    work: WorkConfig
    schedule: ScheduleConfig
    notifications: NotificationsConfig
    models: ModelsConfig
    ica: IcaConfig
    graph: GraphConfig
    powerbi: PowerBIConfig
    data_rule: DataRuleConfig
    company_domains: list[str]
    allowed_groups: dict[str, str]
    clock: ClockConfig
    workers: WorkersConfig
    retention: RetentionConfig
    paths: PathsConfig
    console: ConsoleConfig
    database: DatabaseConfig = DatabaseConfig()
    slack: SlackConfig = SlackConfig()


def load_config(path: Path | None = None) -> Config:
    """Load and validate a config file. Without a path, use config.yaml or fall back to the example."""
    if path is None:
        path = CONFIG_DIR / "config.yaml"
        if not path.exists():
            log.warning("config/config.yaml not found, using config.example.yaml")
            path = CONFIG_DIR / "config.example.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}: {exc}") from exc


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE lines from backend/.env into the environment without overriding set variables.

    Only non-secret settings belong there; secrets live in Credential Manager.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Return the process-wide config, loaded once."""
    return load_config()
