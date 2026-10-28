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
    watch: "PowerBIWatch" = Field(default_factory=lambda: PowerBIWatch())
    owned_datasets: list[str] = Field(default_factory=list)  # yours: a single transient retry may be Report
    retry_own_transient: bool = True  # False: that one retry asks first too (console Settings)


class DataRuleConfig(_Model):
    level: DataRuleLevel


class ClockConfig(_Model):
    start: Literal["opened", "assigned", "email_received"]
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
    memory_dir: str = ""  # profile.md and preferences.md; empty -> <repo>/memory (git-ignored)
    recipes_dir: str = ""  # recipe YAML files; empty -> config/recipes


class ConsoleConfig(_Model):
    host: Literal["127.0.0.1"]
    port: int = Field(gt=0, lt=65536)


class DatabaseConfig(_Model):
    url: str = "sqlite+aiosqlite:///agent.db"
    checkpoints_path: str = "checkpoints.db"  # LangGraph checkpoints, a separate SQLite file


DEFAULT_FORBIDDEN_PHRASES = ["KPI", "24 hours", "24h", "metric"]


class PrivacyConfig(_Model):
    # Never in caller-facing drafts. Matching ignores case, accents, plurals and "24-hour" spacing.
    forbidden_phrases: list[str] = Field(default_factory=lambda: list(DEFAULT_FORBIDDEN_PHRASES))
    # Extra regexes for user ids (Windows logins like CORP\jdoe are always caught).
    user_id_patterns: list[str] = Field(default_factory=lambda: [r"\b[a-zA-Z]\d{6}\b"])
    max_attachment_mb: int = Field(default=10, gt=0)

    @field_validator("user_id_patterns")
    @classmethod
    def _compiles(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"bad user id pattern {pattern!r}: {exc}") from exc
        return value


class MailConfig(_Model):
    backend: Literal["auto", "graph", "com"] = "auto"  # auto: Graph when an app registration is set
    resync_days: int = Field(default=7, gt=0, le=30)  # full resync window after an expired delta token
    # Sensitivity labels that still allow reading the body; any other label means metadata only.
    open_labels: list[str] = Field(default_factory=lambda: ["Public", "General"])
    # Inbox triage (R02) sender rules; addresses or @domain suffixes, case-insensitive.
    manager: list[str] = Field(default_factory=list)  # always stays in Inbox, flagged
    newsletter_senders: list[str] = Field(default_factory=list)
    notification_senders: list[str] = Field(
        default_factory=lambda: ["noreply@", "no-reply@", "donotreply@", "powerbi@", "notifications@"]
    )
    folder_notifications: str = "Notifications"
    folder_newsletters: str = "Newsletters"


class TeamsConfig(_Model):
    active_days: int = Field(default=14, gt=0)  # chats with activity in this window are polled
    max_chats_per_poll: int = Field(default=50, gt=0)
    self_note_verified: bool = False  # Q4: Graph can post to the user's own chat (48:notes)


class PowerBIWatch(_Model):
    datasets: list[str] = Field(default_factory=list)  # dataset ids polled for failures (R05)
    stuck_after_minutes: int = Field(default=120, gt=0)


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
    privacy: PrivacyConfig = PrivacyConfig()
    me: list[str] = Field(default_factory=list)  # the user's own addresses and chat ids
    mail: MailConfig = MailConfig()
    teams: TeamsConfig = TeamsConfig()


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
