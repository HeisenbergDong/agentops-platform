from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import IdMixin, TimestampMixin, now_utc
from app.db.session import Base


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

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
    machine_name: Mapped[str] = mapped_column(String(255))
    supported_apps: Mapped[list[str]] = mapped_column(JSON, default=list)
    current_stage: Mapped[str] = mapped_column(String(128), default="idle")
    current_window_title: Mapped[str] = mapped_column(String(512), default="")
    busy: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime] = mapped_column(default=now_utc)


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
