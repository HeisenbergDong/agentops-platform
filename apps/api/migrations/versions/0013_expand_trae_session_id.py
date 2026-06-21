"""expand trae session id for Feishu display value

Revision ID: 0013_expand_trae_session_id
Revises: 0012_worker_last_seen_nullable
Create Date: 2026-06-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_expand_trae_session_id"
down_revision = "0012_worker_last_seen_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("task_rounds", "trae_session_id", existing_type=sa.String(length=128), type_=sa.String(length=512))


def downgrade() -> None:
    op.alter_column("task_rounds", "trae_session_id", existing_type=sa.String(length=512), type_=sa.String(length=128))
