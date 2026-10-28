"""A memory stack on the test database: ledger, task memory, knowledge, builder, summaries."""

from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.core.config import ClockConfig
from agent.db.repositories import (
    ActionRepo,
    ClockRepo,
    ContextLogRepo,
    DirectoryRepo,
    EventRepo,
    LessonRepo,
    MemoryRepo,
    Task,
    TaskRepo,
)
from agent.db.search import Fts5SearchIndex
from agent.ledger.service import Ledger
from agent.memory.context_builder import ContextBuilder
from agent.memory.knowledge import Knowledge
from agent.memory.personal import PersonalMemory
from agent.memory.service import MemoryService
from agent.memory.summary import SummaryUpdater
from agent.memory.task_memory import TaskMemory
from agent.model.fake import RecordedModel
from agent.privacy.hook import DataClass

TASK = "INC30114872"


class Stack:
    def __init__(
        self,
        engine: AsyncEngine,
        clock: FrozenClock,
        tmp: Path,
        level: DataClass = "chat",
        model: RecordedModel | None = None,
    ) -> None:
        self.clock = clock
        self.tasks = TaskRepo(engine, clock)
        self.repo = MemoryRepo(engine, clock)
        self.events = EventRepo(engine, clock)
        self.ledger = Ledger(
            self.tasks,
            self.repo,
            ClockRepo(engine, clock),
            clock,
            ClockConfig(start="opened", pause_on_hold=False, thresholds_hours=[12, 18, 22]),
        )
        self.memory = TaskMemory(
            self.repo, self.ledger, clock, events=self.events, actions=ActionRepo(engine, clock)
        )
        self.index = Fts5SearchIndex(engine)
        self.directory = DirectoryRepo(engine, clock)
        self.lessons = LessonRepo(engine, clock)
        self.knowledge = Knowledge(self.index, clock, self.directory, self.lessons)
        self.personal = PersonalMemory(tmp / "memory")
        self.context_log = ContextLogRepo(engine, clock)
        self.builder = ContextBuilder(self.memory, self.knowledge, self.personal, level, self.context_log)
        self.model = model or RecordedModel()
        self.summaries = SummaryUpdater(self.model, self.memory, level)
        self.service = MemoryService(self.memory, self.builder, self.summaries, self.knowledge)
        self.ledger.add_listener(self.service.on_state_change)

    async def task(self, task_id: str = TASK, **fields: Any) -> None:
        values = {
            "project": "doxis",
            "kind": "incident",
            "title": "Login fails at SITE_1",
            "state": "new",
        } | fields
        await self.ledger.create(Task(id=task_id, **values))

    async def events_n(self, n: int, task_id: str = TASK, words: int = 60) -> list[int]:
        ids = []
        for i in range(n):
            text = f"Message {i}: " + " ".join(f"word{j}" for j in range(words))
            event_id = await self.memory.add_event(
                task_id,
                text,
                actor="CALLER",
                channel="mail",
                kind="message",
                pseudonymised=True,
                at=self.clock.now_utc() + timedelta(minutes=i),
            )
            assert event_id is not None
            ids.append(event_id)
        return ids
