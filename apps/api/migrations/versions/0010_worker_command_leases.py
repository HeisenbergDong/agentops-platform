"""Add worker command leases."""

from alembic import op
import sqlalchemy as sa


revision = "0010_worker_command_leases"
down_revision = "0009_task_round_trae_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_commands", sa.Column("lease_id", sa.String(length=64), nullable=False, server_default=""))
    op.add_column("worker_commands", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_worker_commands_lease_id", "worker_commands", ["lease_id"])


def downgrade() -> None:
    op.drop_index("ix_worker_commands_lease_id", table_name="worker_commands")
    op.drop_column("worker_commands", "lease_expires_at")
    op.drop_column("worker_commands", "lease_id")
