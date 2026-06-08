"""add role chat messages

Revision ID: 0007_role_chat_messages
Revises: 0006_user_rule_files
Create Date: 2026-06-08
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_role_chat_messages"
down_revision = "0006_user_rule_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "role_chat_messages",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role_key", sa.String(length=128), nullable=False),
        sa.Column("sender", sa.String(length=32), nullable=False, server_default="user"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("mode", sa.String(length=64), nullable=False, server_default="record_only"),
        sa.Column("target_rule", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("action", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_role_chat_messages_user_id", "role_chat_messages", ["user_id"])
    op.create_index("ix_role_chat_messages_role_key", "role_chat_messages", ["role_key"])


def downgrade() -> None:
    op.drop_index("ix_role_chat_messages_role_key", table_name="role_chat_messages")
    op.drop_index("ix_role_chat_messages_user_id", table_name="role_chat_messages")
    op.drop_table("role_chat_messages")
