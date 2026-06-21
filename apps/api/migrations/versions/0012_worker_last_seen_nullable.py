"""allow registered workers without heartbeat

Revision ID: 0012_worker_last_seen_nullable
Revises: 0011_job_scope_text
Create Date: 2026-06-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_worker_last_seen_nullable"
down_revision = "0011_job_scope_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "workers",
        "last_seen_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        server_default=None,
    )
    op.alter_column("workers", "status", existing_type=sa.String(length=32), server_default="offline")


def downgrade() -> None:
    op.execute("update workers set last_seen_at = now() where last_seen_at is null")
    op.alter_column(
        "workers",
        "last_seen_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    op.alter_column("workers", "status", existing_type=sa.String(length=32), server_default="online")
