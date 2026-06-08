"""add worker registration and command queue

Revision ID: 0003_worker_registration_commands
Revises: 0002_user_auth_fields
Create Date: 2026-06-08
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_worker_registration_commands"
down_revision = "0002_user_auth_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("token_hash", sa.String(length=128), nullable=False, server_default=""))
    op.add_column("workers", sa.Column("display_name", sa.String(length=255), nullable=False, server_default=""))
    op.add_column("workers", sa.Column("worker_type", sa.String(length=64), nullable=False, server_default="windows_trae"))
    op.add_column("workers", sa.Column("machine_fingerprint", sa.String(length=512), nullable=False, server_default=""))
    op.add_column("workers", sa.Column("version", sa.String(length=64), nullable=False, server_default=""))
    op.add_column("workers", sa.Column("capabilities", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("workers", sa.Column("status", sa.String(length=32), nullable=False, server_default="online"))
    op.add_column("workers", sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column("workers", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "worker_registration_codes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("code_hash", sa.String(length=128), nullable=False, unique=True),
        sa.Column("created_by", sa.String(length=32), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("assigned_user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by_worker_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "worker_commands",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("job_id", sa.String(length=32), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("round_id", sa.String(length=32), sa.ForeignKey("task_rounds.id"), nullable=True),
        sa.Column("command_type", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("worker_commands")
    op.drop_table("worker_registration_codes")
    op.drop_column("workers", "revoked_at")
    op.drop_column("workers", "registered_at")
    op.drop_column("workers", "status")
    op.drop_column("workers", "capabilities")
    op.drop_column("workers", "version")
    op.drop_column("workers", "machine_fingerprint")
    op.drop_column("workers", "worker_type")
    op.drop_column("workers", "display_name")
    op.drop_column("workers", "token_hash")
