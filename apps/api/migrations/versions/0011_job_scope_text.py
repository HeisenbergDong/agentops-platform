"""add job scope text

Revision ID: 0011_job_scope_text
Revises: 0010_worker_command_leases
Create Date: 2026-06-15 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_job_scope_text"
down_revision = "0010_worker_command_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("scope_text", sa.Text(), nullable=False, server_default=""))
    op.alter_column("jobs", "scope_text", server_default=None)
    op.add_column("jobs", sa.Column("intent", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.alter_column("jobs", "intent", server_default=None)


def downgrade() -> None:
    op.drop_column("jobs", "intent")
    op.drop_column("jobs", "scope_text")
