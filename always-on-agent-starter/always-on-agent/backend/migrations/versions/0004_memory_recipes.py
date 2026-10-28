"""memory and recipes: richer task events, summary coverage, recipe load state, context log (F09, F10, F12)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import agent.db.schema

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("kind", sa.Text(), server_default="note", nullable=False))
        batch_op.add_column(sa.Column("headline", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("lang", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("text_translated", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("importance", sa.Integer(), server_default="0", nullable=False))
        batch_op.create_index(batch_op.f("ix_task_events_source_ref"), ["source_ref"], unique=False)

    with op.batch_alter_table("task_summaries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("covers_through", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("verified", sa.Boolean(), server_default="0", nullable=False))

    with op.batch_alter_table("recipes_state", schema=None) as batch_op:
        batch_op.add_column(sa.Column("error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("content_hash", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("loaded_at", agent.db.schema.UTCDateTime(), nullable=True))

    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_documents_source"), ["source"], unique=False)

    op.create_table(
        "context_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("at", agent.db.schema.UTCDateTime(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("recipe", sa.Text(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_log")),
    )
    with op.batch_alter_table("context_log", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_context_log_task_id"), ["task_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("context_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_context_log_task_id"))
    op.drop_table("context_log")
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_documents_source"))
    with op.batch_alter_table("recipes_state", schema=None) as batch_op:
        batch_op.drop_column("loaded_at")
        batch_op.drop_column("content_hash")
        batch_op.drop_column("error")
    with op.batch_alter_table("task_summaries", schema=None) as batch_op:
        batch_op.drop_column("verified")
        batch_op.drop_column("covers_through")
    with op.batch_alter_table("task_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_task_events_source_ref"))
        batch_op.drop_column("importance")
        batch_op.drop_column("text_translated")
        batch_op.drop_column("lang")
        batch_op.drop_column("headline")
        batch_op.drop_column("kind")
