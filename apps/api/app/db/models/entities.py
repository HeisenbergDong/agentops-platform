from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import IdMixin, TimestampMixin, now_utc
from app.db.session import Base


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(32), default="user", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    auth_token_version: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    configs: Mapped[list["UserConfig"]] = relationship(back_populates="user")


class UserConfig(IdMixin, TimestampMixin, Base):
    __tablename__ = "user_configs"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON, default=dict)

    user: Mapped[User] = relationship(back_populates="configs")


class Worker(IdMixin, TimestampMixin, Base):
    __tablename__ = "workers"

    worker_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(128), default="", index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    worker_type: Mapped[str] = mapped_column(String(64), default="windows_trae")
    machine_name: Mapped[str] = mapped_column(String(255))
    machine_fingerprint: Mapped[str] = mapped_column(String(512), default="")
    version: Mapped[str] = mapped_column(String(64), default="")
    supported_apps: Mapped[list[str]] = mapped_column(JSON, default=list)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="online", index=True)
    current_stage: Mapped[str] = mapped_column(String(128), default="idle")
    current_window_title: Mapped[str] = mapped_column(String(512), default="")
    busy: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime] = mapped_column(default=now_utc)
    registered_at: Mapped[datetime] = mapped_column(default=now_utc)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerRegistrationCode(IdMixin, TimestampMixin, Base):
    __tablename__ = "worker_registration_codes"

    code_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assigned_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by_worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)


class WorkerCommand(IdMixin, TimestampMixin, Base):
    __tablename__ = "worker_commands"

    worker_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    round_id: Mapped[str | None] = mapped_column(ForeignKey("task_rounds.id"), nullable=True)
    command_type: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")


class RoleTemplate(IdMixin, TimestampMixin, Base):
    __tablename__ = "role_templates"

    role_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    purpose: Mapped[str] = mapped_column(Text, default="")
    rules: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    model_config_key: Mapped[str] = mapped_column(String(128), default="default")
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class UserRole(IdMixin, TimestampMixin, Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_key", name="uq_user_role_key"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    template_id: Mapped[str | None] = mapped_column(ForeignKey("role_templates.id"), nullable=True)
    role_key: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255))
    purpose: Mapped[str] = mapped_column(Text, default="")
    rules: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    model_config_key: Mapped[str] = mapped_column(String(128), default="default")
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class UserRuleFile(IdMixin, TimestampMixin, Base):
    __tablename__ = "user_rule_files"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_user_rule_file_name"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    source_name: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class RoleChatMessage(IdMixin, TimestampMixin, Base):
    __tablename__ = "role_chat_messages"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role_key: Mapped[str] = mapped_column(String(128), index=True)
    sender: Mapped[str] = mapped_column(String(32), default="user")
    message: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(64), default="record_only")
    target_rule: Mapped[str] = mapped_column(String(255), default="")
    action: Mapped[dict] = mapped_column(JSON, default=dict)


class RuleVersion(IdMixin, TimestampMixin, Base):
    __tablename__ = "rule_versions"

    version: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Job(IdMixin, TimestampMixin, Base):
    __tablename__ = "jobs"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    rule_version_id: Mapped[str | None] = mapped_column(ForeignKey("rule_versions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(128), index=True)
    directions: Mapped[list[str]] = mapped_column(JSON, default=list)
    daily_target: Mapped[int] = mapped_column(Integer, default=100)
    satisfied_count: Mapped[int] = mapped_column(Integer, default=0)
    submitted_count: Mapped[int] = mapped_column(Integer, default=0)

    rounds: Mapped[list["TaskRound"]] = relationship(back_populates="job")


class Project(IdMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(Text)
    workspace_path: Mapped[str] = mapped_column(String(1024), default="")
    status: Mapped[str] = mapped_column(String(128), default="active")


class TaskRound(IdMixin, TimestampMixin, Base):
    __tablename__ = "task_rounds"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    round_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(128), index=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    trae_session_id: Mapped[str] = mapped_column(String(128), default="")
    trae_user_message_id: Mapped[str] = mapped_column(String(128), default="")
    trae_task_id: Mapped[str] = mapped_column(String(128), default="")
    trae_trace_id: Mapped[str] = mapped_column(String(128), default="")
    trace_status: Mapped[str] = mapped_column(String(128), default="missing")
    github_status: Mapped[str] = mapped_column(String(128), default="pending")
    feishu_status: Mapped[str] = mapped_column(String(128), default="pending")

    job: Mapped[Job] = relationship(back_populates="rounds")


class RuntimeLog(IdMixin, TimestampMixin, Base):
    __tablename__ = "runtime_logs"

    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    round_id: Mapped[str | None] = mapped_column(ForeignKey("task_rounds.id"), nullable=True)
    level: Mapped[str] = mapped_column(String(32), default="info")
    stage: Mapped[str] = mapped_column(String(128), index=True)
    message: Mapped[str] = mapped_column(Text)
    display_message: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class Attachment(IdMixin, TimestampMixin, Base):
    __tablename__ = "attachments"

    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    round_id: Mapped[str | None] = mapped_column(ForeignKey("task_rounds.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(512))
    path: Mapped[str] = mapped_column(String(1024))
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)


class AutomationError(IdMixin, TimestampMixin, Base):
    __tablename__ = "automation_errors"

    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    round_id: Mapped[str | None] = mapped_column(ForeignKey("task_rounds.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(128))
    stage: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
