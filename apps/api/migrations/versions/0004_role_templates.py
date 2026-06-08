"""add role templates

Revision ID: 0004_role_templates
Revises: 0003_worker_registration_commands
Create Date: 2026-06-08
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_role_templates"
down_revision = "0003_worker_registration_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "role_templates",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("role_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False, server_default=""),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("model_config_key", sa.String(length=128), nullable=False, server_default="default"),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("role_templates")
