"""Table definitions for agent.db (F08, F13).

SQLAlchemy Core only, so Phase 3 can move to Postgres. FTS5 lives in a migration and behind SearchIndex.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Dialect,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as UTC and always returns them aware (SQLite drops the zone)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given to the database")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def _task_fk(nullable: bool = False) -> Column[str]:
    return Column("task_id", Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=nullable)


tasks = Table(
    "tasks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("priority", Text, nullable=False, server_default="normal"),
    Column("caller_token", Text),
    Column("affected_token", Text),
    Column("site", Text),
    Column("opened_at", UTCDateTime),
    Column("clock_started_at", UTCDateTime),
    Column("clock_paused", Boolean, nullable=False, server_default="0"),
    Column("next_action", Text),
    Column("recipe", Text),
    Column("last_heartbeat_at", UTCDateTime),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Index(None, "state"),
)

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("external_id", Text, nullable=False),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="SET NULL")),
    Column("received_at", UTCDateTime, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="new"),
    UniqueConstraint("source", "external_id"),
    Index(None, "status"),
)

task_events = Table(
    "task_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    _task_fk(),
    Column("at", UTCDateTime, nullable=False),
    Column("actor", Text, nullable=False),
    Column("channel", Text, nullable=False),
    Column("text_pseudonymised", Text, nullable=False),
    Column("source_ref", Text),
    Column("raw_expires_at", UTCDateTime),
    Index(None, "task_id", "at"),
)

task_summaries = Table(
    "task_summaries",
    metadata,
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

task_facts = Table(
    "task_facts",
    metadata,
    _task_fk(),
    Column("key", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("value", Text, nullable=False),
    Column("source_ref", Text),
    Column("updated_at", UTCDateTime, nullable=False),
    PrimaryKeyConstraint("task_id", "key"),
)

task_links = Table(
    "task_links",
    metadata,
    _task_fk(),
    Column("kind", Text, nullable=False),
    Column("external_id", Text, nullable=False),
    PrimaryKeyConstraint("kind", "external_id"),
    Index(None, "task_id"),
)

action_requests = Table(
    "action_requests",
    metadata,
    Column("id", Text, primary_key=True),
    _task_fk(nullable=True),
    Column("tool", Text, nullable=False),
    Column("args_json", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime),
    Column("decided_at", UTCDateTime),
    Column("result_json", Text),
    Index(None, "status"),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", UTCDateTime, nullable=False),
    Column("task_id", Text),
    Column("action_id", Text, ForeignKey("action_requests.id")),
    Column("recipe", Text),
    Column("tool", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("args_digest", Text, nullable=False),
    Column("result", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Index(None, "task_id"),
)

undo_ops = Table(
    "undo_ops",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("audit_id", Integer, ForeignKey("audit_log.id"), nullable=False, unique=True),
    Column("inverse_tool", Text, nullable=False),
    Column("inverse_args_json", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="available"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    _task_fk(nullable=True),
    Column("kind", Text, nullable=False),
    Column("payload_json", Text, nullable=False, server_default="{}"),
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("available_at", UTCDateTime, nullable=False),
    Column("locked_by", Text),
    Column("locked_at", UTCDateTime),
    Column("last_error", Text),
    Index(None, "status", "priority", "available_at"),
)

token_map = Table(
    "token_map",
    metadata,
    _task_fk(nullable=True),
    Column("token", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("real_value_encrypted", LargeBinary, nullable=False),
    Column("value_digest", Text, nullable=False),
    PrimaryKeyConstraint("token"),
    UniqueConstraint("kind", "value_digest"),
)

documents = Table(
    "documents",
    metadata,
    Column("id", Text, primary_key=True),
    Column("source", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("meta_json", Text, nullable=False, server_default="{}"),
)

recipes_state = Table(
    "recipes_state",
    metadata,
    Column("recipe", Text, primary_key=True),
    Column("version", Integer, nullable=False),
    Column("stage", Text, nullable=False),  # shadow | live | off
    Column("enabled", Boolean, nullable=False, server_default="1"),
    Column("last_run_at", UTCDateTime),
    Column("stats_json", Text, nullable=False, server_default="{}"),
)

lessons = Table(
    "lessons",
    metadata,
    Column("id", Text, primary_key=True),
    Column("scope", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("source_ref", Text),
    Column("status", Text, nullable=False, server_default="proposed"),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

directory = Table(
    "directory",
    metadata,
    Column("email", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("role", Text),
    Column("site", Text),
    Column("team", Text),
    Column("updated_at", UTCDateTime, nullable=False),
)

model_usage = Table(
    "model_usage",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", UTCDateTime, nullable=False),
    Column("model", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("task_id", Text),
    Column("recipe", Text),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("latency_ms", Integer),
    Column("ok", Boolean, nullable=False),
    Index(None, "at"),
)

connector_health = Table(
    "connector_health",
    metadata,
    Column("connector", Text, primary_key=True),
    Column("status", Text, nullable=False),  # ok | degraded | down
    Column("last_ok_at", UTCDateTime),
    Column("last_error", Text),
    Column("checked_at", UTCDateTime, nullable=False),
)

kv = Table(
    "kv",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

schedule_runs = Table(
    "schedule_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("schedule", Text, nullable=False),
    Column("due_at", UTCDateTime, nullable=False),
    Column("ran_at", UTCDateTime),
    Column("status", Text, nullable=False),  # ran | missed | caught_up | skipped
    UniqueConstraint("schedule", "due_at"),
)
