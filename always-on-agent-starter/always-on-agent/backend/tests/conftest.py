from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.clock import FrozenClock
from agent.core.config import CONFIG_DIR, load_config
from agent.db.engine import create_engine_async
from agent.db.migrate import upgrade_head


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'agent.db').as_posix()}"
    upgrade_head(url)
    return url


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine_async(db_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(
        load_config(CONFIG_DIR / "config.example.yaml"), datetime(2026, 3, 25, 4, 30, tzinfo=UTC)
    )
