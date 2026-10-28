"""RecordedModel: replays recorded model outputs (tests, the recipe harness, shadow evaluation).

Responses are queued per purpose ("summary", "triage" ...) or in one shared queue. A queued Exception
is raised instead of answering, to test outages. Every call is kept in `calls`, so tests can check
what the model would have seen. The privacy hook still runs when one is given.
"""

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from agent.core.errors import ModelUnavailable
from agent.model.gateway import DEFAULT_MAX_TOKENS, GatewayResult, Tier
from agent.privacy.hook import DataClass, PrivacyHook

Response = dict[str, Any] | BaseModel | Exception


class RecordedModel:
    def __init__(
        self,
        responses: Mapping[str, Sequence[Response]] | Sequence[Response] = (),
        privacy: PrivacyHook | None = None,
    ) -> None:
        self._by_purpose: dict[str, deque[Response]] = defaultdict(deque)
        self._shared: deque[Response] = deque()
        if isinstance(responses, Mapping):
            for purpose, items in responses.items():
                self._by_purpose[purpose].extend(items)
        else:
            self._shared.extend(responses)
        self._privacy = privacy
        self.calls: list[dict[str, Any]] = []

    def add(self, purpose: str, *responses: Response) -> None:
        self._by_purpose[purpose].extend(responses)

    async def run[T: BaseModel](
        self,
        purpose: str,
        messages: list[dict[str, Any]],
        schema: type[T],
        data_classes: set[DataClass],
        tier: Tier = "auto",
        task_id: str | None = None,
        recipe: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        instructions: str | None = None,
    ) -> GatewayResult[T]:
        if self._privacy is not None:
            messages = await self._privacy.outbound(messages, data_classes, task_id)
        self.calls.append(
            {
                "purpose": purpose,
                "messages": messages,
                "data_classes": set(data_classes),
                "task_id": task_id,
                "recipe": recipe,
                "instructions": instructions,
                "tier": tier,
            }
        )
        queue = self._by_purpose[purpose] if self._by_purpose[purpose] else self._shared
        if not queue:
            # Like an outage: steps take their code-only path (E07), tests see the purpose it wanted.
            raise ModelUnavailable(f"no recorded model response left for {purpose!r}")
        item = queue.popleft()
        if isinstance(item, Exception):
            raise item
        data = item.model_dump() if isinstance(item, BaseModel) else item
        return GatewayResult[schema](output=schema.model_validate(data), model="recorded", tier=str(tier))  # type: ignore[valid-type]
