"""ledger clocks: start times, pauses and fired thresholds (F08)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import agent.db.schema

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("assigned_at", agent.db.schema.UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("email_received_at", agent.db.schema.UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("clock_estimated", sa.Boolean(), server_default="0", nullable=False))
    op.create_table(
        "clock_pauses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("started_at", agent.db.schema.UTCDateTime(), nullable=False),
        sa.Column("ended_at", agent.db.schema.UTCDateTime(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_clock_pauses_task_id_tasks"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clock_pauses")),
    )
    with op.batch_alter_table("clock_pauses", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_clock_pauses_task_id"), ["task_id"], unique=False)
    op.create_table(
        "clock_thresholds",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("hours", sa.Integer(), nullable=False),
        sa.Column("fired_at", agent.db.schema.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_clock_thresholds_task_id_tasks"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("task_id", "hours", name=op.f("pk_clock_thresholds")),
    )


def downgrade() -> None:
    op.drop_table("clock_thresholds")
    with op.batch_alter_table("clock_pauses", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_clock_pauses_task_id"))
    op.drop_table("clock_pauses")
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("clock_estimated")
        batch_op.drop_column("email_received_at")
        batch_op.drop_column("assigned_at")
