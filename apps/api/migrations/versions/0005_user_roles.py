"""add user roles

Revision ID: 0005_user_roles
Revises: 0004_role_templates
Create Date: 2026-06-08
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_user_roles"
down_revision = "0004_role_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_roles",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("template_id", sa.String(length=32), sa.ForeignKey("role_templates.id"), nullable=True),
        sa.Column("role_key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False, server_default=""),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("model_config_key", sa.String(length=128), nullable=False, server_default="default"),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "role_key", name="uq_user_role_key"),
    )


def downgrade() -> None:
    op.drop_table("user_roles")
