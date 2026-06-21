"""add worker runtime status

Revision ID: 0014_worker_runtime_status
Revises: 0013_expand_trae_session_id
Create Date: 2026-06-21 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_worker_runtime_status"
down_revision = "0013_expand_trae_session_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column("runtime_status", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("workers", "runtime_status")
