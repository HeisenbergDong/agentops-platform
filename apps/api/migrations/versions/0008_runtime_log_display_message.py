"""add runtime log display message

Revision ID: 0008_runtime_log_display_message
Revises: 0007_role_chat_messages
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_runtime_log_display_message"
down_revision = "0007_role_chat_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtime_logs",
        sa.Column("display_message", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("runtime_logs", "display_message")
