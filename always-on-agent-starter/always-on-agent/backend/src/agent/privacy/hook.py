"""Privacy hook called by the model gateway before every model call (F11).

Only the data-rule check exists for now; pseudonymisation arrives in prompt 1.7.
"""

from typing import Any, Literal, Protocol

from agent.core.errors import DataRuleViolation

DataClass = Literal["metadata", "text", "chat", "images"]

# Each level allows itself and every level before it.
LEVEL_ORDER: tuple[DataClass, ...] = ("metadata", "text", "chat", "images")


class PrivacyHook(Protocol):
    def outbound(
        self, messages: list[dict[str, Any]], data_classes: set[DataClass], task_id: str | None
    ) -> list[dict[str, Any]]:
        """Check and transform messages going to the model. Raise DataRuleViolation to block."""
        ...


def check_data_classes(data_classes: set[DataClass], level: DataClass) -> None:
    allowed = set(LEVEL_ORDER[: LEVEL_ORDER.index(level) + 1])
    blocked = sorted(set(data_classes) - allowed, key=LEVEL_ORDER.index)
    if blocked:
        raise DataRuleViolation(f"data rule is {level!r}; not allowed: {', '.join(blocked)}")


class DataRuleOnlyHook:
    """Pass-through that only enforces the data-rule level."""

    def __init__(self, level: DataClass) -> None:
        self._level = level

    def outbound(
        self, messages: list[dict[str, Any]], data_classes: set[DataClass], task_id: str | None
    ) -> list[dict[str, Any]]:
        check_data_classes(data_classes, self._level)
        return messages
