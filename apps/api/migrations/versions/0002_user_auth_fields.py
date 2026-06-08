"""add user auth fields

Revision ID: 0002_user_auth_fields
Revises: 0001_initial_schema
Create Date: 2026-06-08
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_user_auth_fields"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=False, server_default=""))
    op.add_column("users", sa.Column("role", sa.String(length=32), nullable=False, server_default="user"))
    op.add_column("users", sa.Column("auth_token_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "auth_token_version")
    op.drop_column("users", "role")
    op.drop_column("users", "password_hash")
