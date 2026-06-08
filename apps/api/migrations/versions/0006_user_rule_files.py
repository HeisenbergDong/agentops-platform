"""add user rule files

Revision ID: 0006_user_rule_files
Revises: 0005_user_roles
Create Date: 2026-06-08
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_user_rule_files"
down_revision = "0005_user_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_rule_files",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "name", name="uq_user_rule_file_name"),
    )
    op.create_index("ix_user_rule_files_user_id", "user_rule_files", ["user_id"])
    op.create_index("ix_user_rule_files_name", "user_rule_files", ["name"])


def downgrade() -> None:
    op.drop_index("ix_user_rule_files_name", table_name="user_rule_files")
    op.drop_index("ix_user_rule_files_user_id", table_name="user_rule_files")
    op.drop_table("user_rule_files")
