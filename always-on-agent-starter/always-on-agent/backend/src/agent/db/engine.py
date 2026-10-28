"""Database engine creation (F08)."""

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent.core.config import get_config

BUSY_TIMEOUT_MS = 5000


def _on_connect(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    cursor.close()


def database_url() -> str:
    return get_config().database.url


def sync_url(url: str) -> str:
    """The same database with a synchronous driver, for Alembic."""
    parsed = make_url(url)
    driver = {"sqlite+aiosqlite": "sqlite", "postgresql+asyncpg": "postgresql+psycopg"}.get(
        parsed.drivername, parsed.drivername
    )
    return parsed.set(drivername=driver).render_as_string(hide_password=False)


def is_sqlite(url: str) -> bool:
    return make_url(url).get_backend_name() == "sqlite"


def create_engine_async(url: str | None = None) -> AsyncEngine:
    url = url or database_url()
    kwargs: dict[str, Any] = {}
    if is_sqlite(url):
        kwargs["connect_args"] = {"timeout": BUSY_TIMEOUT_MS / 1000}
    engine = create_async_engine(url, **kwargs)
    if is_sqlite(url):
        event.listen(engine.sync_engine, "connect", _on_connect)
    return engine


def create_engine_sync(url: str | None = None) -> Engine:
    url = sync_url(url or database_url())
    engine = create_engine(url)
    if is_sqlite(url):
        event.listen(engine, "connect", _on_connect)
    return engine
