"""Privacy hook called by the model gateway before every model call (F11).

PrivacyLayer is the real hook: data-rule check, pseudonymisation of every text part, then a leak check.
DataRuleOnlyHook only enforces the level (tests and diagnostics).
"""

from typing import TYPE_CHECKING, Any, Literal, Protocol

from agent.core.errors import DataRuleViolation

if TYPE_CHECKING:
    from agent.privacy.pseudonymiser import Pseudonymiser

DataClass = Literal["metadata", "text", "chat", "images"]

# Each level allows itself and every level before it.
LEVEL_ORDER: tuple[DataClass, ...] = ("metadata", "text", "chat", "images")

_IMAGE_PART_TYPES = {"image_url", "image", "input_image"}


class PrivacyHook(Protocol):
    async def outbound(
        self, messages: list[dict[str, Any]], data_classes: set[DataClass], task_id: str | None
    ) -> list[dict[str, Any]]:
        """Check and transform messages going to the model.

        Raise DataRuleViolation or LeakDetected to block the call.
        """
        ...


def check_data_classes(data_classes: set[DataClass], level: DataClass) -> None:
    allowed = set(LEVEL_ORDER[: LEVEL_ORDER.index(level) + 1])
    blocked = sorted(set(data_classes) - allowed, key=LEVEL_ORDER.index)
    if blocked:
        raise DataRuleViolation(f"data rule is {level!r}; not allowed: {', '.join(blocked)}")


def has_images(messages: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(part, dict) and part.get("type") in _IMAGE_PART_TYPES
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
    )


def _check(messages: list[dict[str, Any]], data_classes: set[DataClass], level: DataClass) -> None:
    check_data_classes(data_classes, level)
    if has_images(messages) and "images" not in data_classes:
        # A caller that forgets to declare images must not slip a screenshot past the level check.
        raise DataRuleViolation("messages hold images that were not declared as the images data class")


class DataRuleOnlyHook:
    """Pass-through that only enforces the data-rule level."""

    def __init__(self, level: DataClass) -> None:
        self._level = level

    async def outbound(
        self, messages: list[dict[str, Any]], data_classes: set[DataClass], task_id: str | None
    ) -> list[dict[str, Any]]:
        _check(messages, data_classes, self._level)
        return messages


class PrivacyLayer:
    """Data rule, pseudonymiser and leak check, in that order."""

    def __init__(self, level: DataClass, pseudonymiser: "Pseudonymiser") -> None:
        self._level = level
        self._pseudo = pseudonymiser

    async def outbound(
        self, messages: list[dict[str, Any]], data_classes: set[DataClass], task_id: str | None
    ) -> list[dict[str, Any]]:
        _check(messages, data_classes, self._level)
        out = []
        for message in messages:
            clean = dict(message)
            for key in ("content", "name"):
                value = clean.get(key)
                if isinstance(value, str):
                    clean[key] = await self._text(value, task_id)
                elif isinstance(value, list):
                    clean[key] = [await self._part(part, task_id) for part in value]
            out.append(clean)
        return out

    async def _part(self, part: Any, task_id: str | None) -> Any:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return part | {"text": await self._text(part["text"], task_id)}
        return part

    async def _text(self, text: str, task_id: str | None) -> str:
        result = await self._pseudo.pseudonymise(text, task_id)
        await self._pseudo.leak_check(result, task_id)
        return result
