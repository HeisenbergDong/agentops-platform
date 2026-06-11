"""add task round trae metadata

Revision ID: 0009_task_round_trae_metadata
Revises: 0008_runtime_log_display_message
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_task_round_trae_metadata"
down_revision = "0008_runtime_log_display_message"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_rounds", sa.Column("trae_session_id", sa.String(length=128), nullable=False, server_default=""))
    op.add_column("task_rounds", sa.Column("trae_user_message_id", sa.String(length=128), nullable=False, server_default=""))
    op.add_column("task_rounds", sa.Column("trae_task_id", sa.String(length=128), nullable=False, server_default=""))
    op.add_column("task_rounds", sa.Column("trae_trace_id", sa.String(length=128), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("task_rounds", "trae_trace_id")
    op.drop_column("task_rounds", "trae_task_id")
    op.drop_column("task_rounds", "trae_user_message_id")
    op.drop_column("task_rounds", "trae_session_id")
