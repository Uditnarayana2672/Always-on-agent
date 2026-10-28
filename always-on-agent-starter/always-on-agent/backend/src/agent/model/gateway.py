"""The model gateway: every model call goes through ModelGateway.run (F05).

Order: privacy hook, routing and budget, structured call, validation with one repair retry, usage row.
Messages use the OpenAI chat format that ICA serves.
"""

import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from agent.core.clock import Clock
from agent.core.config import Config
from agent.core.errors import ModelOutputError
from agent.db.repositories import UsageRepo
from agent.model.ica_client import IcaClient, IcaRequestError
from agent.model.prompts import render
from agent.privacy.hook import DataClass, PrivacyHook

log = logging.getLogger(__name__)

Tier = Literal["auto", "haiku", "sonnet", "opus"]

RESULT_TOOL = "submit_result"
# Some ICA models spend output tokens on hidden reasoning, so keep the default generous.
DEFAULT_MAX_TOKENS = 2048
REPAIR_MAX_TOKENS_FACTOR = 2


class GatewayResult[T: BaseModel](BaseModel):
    output: T
    model: str
    tier: str
    downgraded: bool = False
    repaired: bool = False
    input_tokens: int = 0
    output_tokens: int = 0


class ModelRunner(Protocol):
    """What memory and recipes need from the gateway (ModelGateway, or RecordedModel in tests)."""

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
    ) -> GatewayResult[T]: ...


def _tools_unsupported(exc: IcaRequestError) -> bool:
    return exc.status_code == 400 and bool(re.search(r"\b(tool|function)", exc.detail, re.IGNORECASE))


def _extract_json(text: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text.strip()


class ModelGateway:
    def __init__(
        self,
        client: IcaClient,
        config: Config,
        usage: UsageRepo,
        clock: Clock,
        privacy: PrivacyHook,
        *,
        prompt_caching: bool = True,
    ) -> None:
        self._client = client
        self._config = config
        self._usage = usage
        self._clock = clock
        self._privacy = privacy
        self._prompt_caching = prompt_caching
        self._tools_supported = True

    # ------------------------------------------------------------ routing

    async def _route(self, purpose: str, tier: Tier) -> tuple[str, bool]:
        """Return (tier, downgraded). Sonnet only for listed purposes and while budget is left."""
        models = self._config.models
        auto = "sonnet" if purpose in models.sonnet_for else models.default
        wanted: str = auto if tier == "auto" else tier
        if wanted != "sonnet":
            return wanted, False
        if purpose not in models.sonnet_for:
            return "haiku", True
        used = await self._usage.calls_since(self._client.model_id("sonnet"), self._local_day_start())
        if used >= models.sonnet_daily_limit:
            log.info("Sonnet budget used up, using Haiku", extra={"purpose": purpose, "used": used})
            return "haiku", True
        return "sonnet", False

    def _local_day_start(self) -> datetime:
        local = self._clock.now_local()
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    # ------------------------------------------------------------ public API

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
        messages = await self._privacy.outbound(messages, data_classes, task_id)
        chosen, downgraded = await self._route(purpose, tier)
        model = self._client.model_id(chosen)
        system = render("base_system", task_instructions=instructions)

        conversation = list(messages)
        totals = [0, 0]
        last_error = ""
        for attempt in range(2):
            response, used_tools = await self._call(
                model, system, conversation, schema, max_tokens, purpose, task_id, recipe, totals
            )
            choice = response["choices"][0]
            raw = self._read_output(choice, used_tools)
            try:
                output = schema.model_validate_json(raw)
            except ValidationError as exc:
                last_error = str(exc)
                if choice.get("finish_reason") == "length":
                    last_error = "The answer was cut off by the length limit. Be shorter.\n" + last_error
                    max_tokens *= REPAIR_MAX_TOKENS_FACTOR
                log.info("Model output failed validation", extra={"purpose": purpose, "attempt": attempt + 1})
                conversation = [
                    *messages,
                    {"role": "assistant", "content": raw or "(no output)"},
                    {
                        "role": "user",
                        "content": "Your previous answer did not match the required schema. "
                        f"Errors:\n{last_error}\nAnswer again with only a valid result.",
                    },
                ]
                continue
            return GatewayResult[schema](  # type: ignore[valid-type]
                output=output,
                model=model,
                tier=chosen,
                downgraded=downgraded,
                repaired=attempt > 0,
                input_tokens=totals[0],
                output_tokens=totals[1],
            )
        raise ModelOutputError(f"{purpose}: output still invalid after repair: {last_error[:500]}")

    # ------------------------------------------------------------ internals

    def _system_message(self, model: str, text: str) -> dict[str, Any]:
        # Prompt caching markers are only understood on Claude routes; other models get plain text.
        if self._prompt_caching and model.startswith("claude"):
            return {
                "role": "system",
                "content": [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}],
            }
        return {"role": "system", "content": text}

    async def _call(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        max_tokens: int,
        purpose: str,
        task_id: str | None,
        recipe: str | None,
        totals: list[int],
    ) -> tuple[dict[str, Any], bool]:
        use_tools = self._tools_supported
        payload: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "stream": False}
        if use_tools:
            payload["messages"] = [self._system_message(model, system), *messages]
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": RESULT_TOOL,
                        "description": "Submit the structured result.",
                        "parameters": schema.model_json_schema(),
                    },
                }
            ]
            payload["tool_choice"] = {"type": "function", "function": {"name": RESULT_TOOL}}
        else:
            payload["messages"] = [
                self._system_message(model, system + self._json_instruction(schema)),
                *messages,
            ]
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            response = await self._client.chat(payload)
        except IcaRequestError as exc:
            if use_tools and _tools_unsupported(exc):
                log.warning("Tool use rejected by ICA, falling back to JSON in text")
                self._tools_supported = False
                return await self._call(
                    model, system, messages, schema, max_tokens, purpose, task_id, recipe, totals
                )
            await self._record(model, purpose, task_id, recipe, 0, 0, started, ok=False)
            raise
        except Exception:
            await self._record(model, purpose, task_id, recipe, 0, 0, started, ok=False)
            raise
        usage = response.get("usage") or {}
        tokens_in, tokens_out = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        totals[0] += tokens_in
        totals[1] += tokens_out
        await self._record(model, purpose, task_id, recipe, tokens_in, tokens_out, started, ok=True)
        return response, use_tools

    @staticmethod
    def _json_instruction(schema: type[BaseModel]) -> str:
        return "\n\nReply with one JSON object only, no prose, matching this JSON schema:\n" + json.dumps(
            schema.model_json_schema()
        )

    @staticmethod
    def _read_output(choice: dict[str, Any], used_tools: bool) -> str:
        """The raw JSON text of the answer: tool-call arguments if present, else JSON found in the text."""
        message = choice.get("message") or {}
        if used_tools:
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") == RESULT_TOOL:
                    arguments = function.get("arguments", "")
                    return arguments if isinstance(arguments, str) else json.dumps(arguments)
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return _extract_json(str(content))

    async def _record(
        self,
        model: str,
        purpose: str,
        task_id: str | None,
        recipe: str | None,
        input_tokens: int,
        output_tokens: int,
        started: float,
        ok: bool,
    ) -> None:
        await self._usage.record(
            model=model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            ok=ok,
            latency_ms=int((time.perf_counter() - started) * 1000),
            task_id=task_id,
            recipe=recipe,
        )
