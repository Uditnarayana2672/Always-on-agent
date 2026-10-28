"""MemoryService: one place the task graph, recipes and runtime use memory through (F09, F10).

- record_trigger: the intake event of a run goes into the task's event log once.
- after_run: the running summary is rewritten and checked (never fails the run).
- on_state_change: a task reaching done writes its precedent; a reopened task's record is rewritten on
  its next close.
- register_tools: the Silent search_memory tool, so a model step can ask for more through the gate.
"""

import logging
from typing import Any

from pydantic import BaseModel

from agent.memory.context_builder import ContextBuilder, SearchMemory
from agent.memory.knowledge import Knowledge
from agent.memory.summary import SummaryResult, SummaryUpdater
from agent.memory.task_memory import TaskMemory
from agent.policy.registry import Registry

log = logging.getLogger(__name__)


class MemoryService:
    def __init__(
        self,
        memory: TaskMemory,
        builder: ContextBuilder,
        summaries: SummaryUpdater | None = None,
        knowledge: Knowledge | None = None,
    ) -> None:
        self.memory = memory
        self.builder = builder
        self._summaries = summaries
        self._knowledge = knowledge

    async def record_trigger(self, task_id: str, trigger: dict[str, Any]) -> int | None:
        try:
            return await self.memory.record_trigger(task_id, trigger)
        except Exception:
            log.exception("could not record the trigger event", extra={"task": task_id})
            return None

    async def after_run(self, task_id: str) -> SummaryResult | None:
        if self._summaries is None:
            return None
        try:
            return await self._summaries.update(task_id)
        except Exception:
            log.exception("summary update failed", extra={"task": task_id})
            return None

    async def on_state_change(self, task_id: str, from_state: str, to_state: str, actor: str) -> None:
        if to_state != "done" or self._knowledge is None:
            return
        card = await self.memory.card(task_id)
        summary = await self.memory.summary(task_id)
        outcome = "closed in ServiceNow" if actor == "source_system" else "closed"
        await self._knowledge.record_precedent(card, summary.text, outcome)

    def register_tools(self, registry: Registry) -> None:
        async def search(args: BaseModel, key: str) -> dict[str, Any]:
            assert isinstance(args, SearchMemory)
            return (await self.builder.search_memory(args)).model_dump()

        if registry.get("search_memory") is None:
            registry.tool("search_memory", mode="silent", handler=search)(SearchMemory)
