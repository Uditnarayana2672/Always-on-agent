"""Alembic environment. The URL comes from config (or -x url=...), with a sync driver."""

from alembic import context
from sqlalchemy import Connection

from agent.db.engine import create_engine_sync, database_url
from agent.db.schema import metadata

config = context.config


def _url() -> str:
    return context.get_x_argument(as_dictionary=True).get("url") or database_url()


def _include(name: str | None, type_: str, *_: object) -> bool:
    # documents_fts and its shadow tables are created by hand in a migration.
    return not (type_ == "table" and name is not None and name.startswith("documents_fts"))


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        render_as_batch=True,
        include_name=_include,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine_sync(_url())
    with engine.connect() as connection:
        _run(connection)
    engine.dispose()
