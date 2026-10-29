"""Recipe YAML schema (F12).

    name: pbi-refresh            # must equal the file name (pbi-refresh.yaml)
    title: Refresh on request    # optional display name in the console
    project: powerbi             # doxis | powerbi | general
    status: shadow               # live | shadow | off
    trigger: {poll: teams_chats, every: 5m, match: "app\\.powerbi\\.com|\\brefresh\\b"}
    creates_task: {id_prefix: REF}
    model: haiku                 # haiku | sonnet | opus | none
    data_classes: [metadata, chat]
    tools: [read_chat, get_refresh_history, trigger_refresh, post_chat]
    steps: agent.recipes.steps.pbi_refresh:steps
    context: [personal, card, summary, last_events]
    instructions: >
      ...
    output: approval_card
    exclusive_with: []

Triggers: exactly one of schedule (HH:MM times, or a named schedule: brief, checkin, nightly_digest,
chase_window), poll (a source the pollers read; `every` is the wanted frequency) or event (a source).
match is a case-insensitive regex over subject, body and text.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.memory.context_builder import DEFAULT_NEEDS, SectionName
from agent.privacy.hook import DataClass

NAMED_SCHEDULES = frozenset({"brief", "checkin", "nightly_digest", "chase_window"})
# poll names in recipes -> the event source the pollers produce
POLL_SOURCES = {
    "mail": "mail",
    "inbox": "mail",
    "teams": "teams",
    "teams_chats": "teams",
    "slack": "slack",
    "powerbi": "powerbi",
    "refresh_history": "powerbi",
    "servicenow_report": "servicenow_report",
}
EVENT_SOURCES = frozenset({"mail", "teams", "slack", "powerbi", "servicenow_report", "api"})
_HH_MM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EVERY = re.compile(r"^(\d+)\s*(s|m|h)$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,48}$")
_STEPS = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
MAX_MATCH_LENGTH = 500


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Trigger(_Model):
    schedule: str | None = None
    poll: str | None = None
    event: str | None = None
    match: str | None = None
    every: str | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> "Trigger":
        kinds = [k for k in ("schedule", "poll", "event") if getattr(self, k)]
        if len(kinds) != 1:
            raise ValueError("a trigger needs exactly one of schedule, poll or event")
        if self.every and not self.poll:
            raise ValueError("every only goes with poll")
        if self.schedule and self.match:
            raise ValueError("match only goes with poll or event")
        return self

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, value: str | None) -> str | None:
        if value is None or value in NAMED_SCHEDULES:
            return value
        for part in value.split(","):
            if not _HH_MM.match(part.strip()):
                raise ValueError(
                    f"schedule must be HH:MM times or one of {sorted(NAMED_SCHEDULES)}, got {value!r}"
                )
        return value

    @field_validator("poll")
    @classmethod
    def _poll(cls, value: str | None) -> str | None:
        if value is not None and value not in POLL_SOURCES:
            raise ValueError(f"unknown poll source {value!r}; use one of {sorted(POLL_SOURCES)}")
        return value

    @field_validator("event")
    @classmethod
    def _event(cls, value: str | None) -> str | None:
        if value is not None and value not in EVENT_SOURCES:
            raise ValueError(f"unknown event source {value!r}; use one of {sorted(EVENT_SOURCES)}")
        return value

    @field_validator("match")
    @classmethod
    def _match(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if len(value) > MAX_MATCH_LENGTH:
            raise ValueError("match is too long")
        try:
            re.compile(value, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"match is not a valid regex: {exc}") from exc
        return value

    @field_validator("every")
    @classmethod
    def _every(cls, value: str | None) -> str | None:
        if value is not None and not _EVERY.match(value.strip()):
            raise ValueError(f"every must look like 30s, 5m or 1h, got {value!r}")
        return value

    @property
    def source(self) -> str | None:
        """The event source this trigger reacts to (None for schedules)."""
        if self.poll:
            return POLL_SOURCES[self.poll]
        return self.event

    @property
    def times(self) -> list[str]:
        if not self.schedule or self.schedule in NAMED_SCHEDULES:
            return []
        return [p.strip() for p in self.schedule.split(",")]

    @property
    def every_minutes(self) -> int | None:
        if not self.every:
            return None
        amount, unit = _EVERY.match(self.every.strip()).groups()  # type: ignore[union-attr]
        seconds = int(amount) * {"s": 1, "m": 60, "h": 3600}[unit]
        return max(1, round(seconds / 60))


class CreatesTask(_Model):
    id_prefix: str
    kind: str = "request"

    @field_validator("id_prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        if not re.match(r"^[A-Z]{2,6}$", value):
            raise ValueError("id_prefix must be 2-6 capital letters, like REF or ENH")
        return value


Output = Literal["approval_card", "digest", "dm", "report", "none"]


def _metadata_only() -> list[DataClass]:
    return ["metadata"]


class RecipeSpec(_Model):
    name: str
    title: str | None = Field(default=None, max_length=80)  # the console's display name
    version: int = Field(default=1, ge=1)
    project: Literal["doxis", "powerbi", "general"]
    status: Literal["live", "shadow", "off"]
    trigger: Trigger
    creates_task: CreatesTask | None = None
    model: Literal["haiku", "sonnet", "opus", "none"] = "haiku"
    data_classes: list[DataClass] = Field(default_factory=_metadata_only)
    tools: list[str] = Field(default_factory=list)
    steps: str | None = None
    context: list[SectionName] = Field(default_factory=lambda: list(DEFAULT_NEEDS))
    instructions: str = ""
    output: Output = "none"
    exclusive_with: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError("name must be lower case letters, digits, - or _")
        return value

    @field_validator("steps")
    @classmethod
    def _steps(cls, value: str | None) -> str | None:
        if value is not None and not _STEPS.match(value):
            raise ValueError("steps must look like package.module:function")
        return value

    @field_validator("tools", "exclusive_with")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("listed twice")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "RecipeSpec":
        if self.name in self.exclusive_with:
            raise ValueError("a recipe cannot be exclusive with itself")
        if not self.data_classes:
            raise ValueError("data_classes cannot be empty; use [metadata]")
        if self.creates_task is not None and self.trigger.schedule:
            raise ValueError("a scheduled recipe cannot create tasks; trigger it on events instead")
        return self

    @property
    def stage(self) -> Literal["live", "shadow"]:
        return "shadow" if self.status == "shadow" else "live"
