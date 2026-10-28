"""approvals, per-task token map and schedule run details (F02, F07, F11)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("action_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("args_digest", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("content", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("edited", sa.Boolean(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("decided_by", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("surfaces_json", sa.Text(), server_default="{}", nullable=False))
        batch_op.create_index(batch_op.f("ix_action_requests_task_id"), ["task_id"], unique=False)
    op.execute("UPDATE action_requests SET args_digest = content_hash WHERE args_digest IS NULL")

    with op.batch_alter_table("schedule_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("detail", sa.Text(), nullable=True))

    # Tokens are per task: CALLER in one task is not CALLER in another. Several real values (full
    # name, first name, email) may share one token, so the key is the value, not the token.
    op.create_table(
        "token_map_new",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("real_value_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("value_digest", sa.Text(), nullable=False),
        sa.Column("canonical", sa.Boolean(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_token_map_task_id_tasks"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("scope", "kind", "value_digest", name=op.f("pk_token_map")),
    )
    op.execute(
        "INSERT INTO token_map_new"
        " (scope, task_id, token, kind, real_value_encrypted, value_digest, canonical) "
        "SELECT COALESCE(task_id, '_global'), task_id, token, kind, real_value_encrypted, value_digest, 1 "
        "FROM token_map"
    )
    op.drop_table("token_map")
    op.rename_table("token_map_new", "token_map")
    with op.batch_alter_table("token_map", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_token_map_scope_token"), ["scope", "token"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("token_map", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_token_map_scope_token"))
    op.create_table(
        "token_map_old",
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("real_value_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("value_digest", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_token_map_task_id_tasks", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("token", name="pk_token_map"),
        sa.UniqueConstraint("kind", "value_digest", name="uq_token_map_kind_value_digest"),
    )
    op.execute(
        "INSERT OR IGNORE INTO token_map_old (task_id, token, kind, real_value_encrypted, value_digest) "
        "SELECT task_id, token, kind, real_value_encrypted, value_digest FROM token_map WHERE canonical = 1"
    )
    op.drop_table("token_map")
    op.rename_table("token_map_old", "token_map")

    with op.batch_alter_table("schedule_runs", schema=None) as batch_op:
        batch_op.drop_column("detail")
    with op.batch_alter_table("action_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_action_requests_task_id"))
        batch_op.drop_column("surfaces_json")
        batch_op.drop_column("reason")
        batch_op.drop_column("decided_by")
        batch_op.drop_column("edited")
        batch_op.drop_column("content")
        batch_op.drop_column("args_digest")
