from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from app.db.models import Attachment, AutomationError, Job, RuntimeLog, TaskRound, WorkerCommand
from app.services.orchestrator.events import build_display_message
from app.services.orchestrator.states import JobState

TERMINAL_STATES = {JobState.STOPPED, JobState.PROJECT_COMPLETED}
ACTIVE_COMMAND_STATES = {"queued", "claimed", "running"}


def create_job(db: Session, user_id: str, directions: list[str], rule_version_id: str | None) -> Job:
    job = Job(
        user_id=user_id,
        rule_version_id=rule_version_id,
        status=JobState.JOB_STARTING,
        directions=directions,
    )
    db.add(job)
    db.flush()
    round_ = TaskRound(job_id=job.id, round_index=1, status=JobState.LOADING_RULES)
    db.add(round_)
    db.flush()
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.JOB_STARTING,
        message="Job created and initial round prepared.",
        extra={"directions": directions},
    )
    db.commit()
    db.refresh(job)
    return job


def current_job(db: Session, user_id: str) -> Job | None:
    return db.scalar(
        select(Job)
        .where(Job.user_id == user_id)
        .order_by(desc(Job.created_at))
        .limit(1)
    )


def current_active_job(db: Session, user_id: str) -> Job | None:
    return db.scalar(
        select(Job)
        .where(Job.user_id == user_id, Job.status.not_in([str(item) for item in TERMINAL_STATES]))
        .order_by(desc(Job.created_at))
        .limit(1)
    )


def cleanup_user_runtime_state(db: Session, user_id: str) -> dict:
    jobs = list(db.scalars(select(Job).where(Job.user_id == user_id)).all())
    job_ids = [job.id for job in jobs]
    stopped_jobs = 0
    for job in jobs:
        if job.status not in TERMINAL_STATES:
            job.status = JobState.STOPPED
            stopped_jobs += 1

    cancelled_commands = 0
    commands = list(
        db.scalars(
            select(WorkerCommand).where(
                WorkerCommand.user_id == user_id,
                WorkerCommand.status.in_(ACTIVE_COMMAND_STATES),
            )
        ).all()
    )
    for command in commands:
        command.status = "cancelled"
        command.message = "Cancelled by Start cleanup."
        cancelled_commands += 1

    deleted_logs = 0
    deleted_errors = 0
    deleted_attachments = 0
    if job_ids:
        deleted_logs = db.execute(delete(RuntimeLog).where(RuntimeLog.job_id.in_(job_ids))).rowcount or 0
        deleted_errors = db.execute(delete(AutomationError).where(AutomationError.job_id.in_(job_ids))).rowcount or 0
        deleted_attachments = (
            db.execute(
                delete(Attachment).where(
                    (Attachment.user_id == user_id) | (Attachment.job_id.in_(job_ids))
                )
            ).rowcount
            or 0
        )
    else:
        deleted_attachments = db.execute(delete(Attachment).where(Attachment.user_id == user_id)).rowcount or 0

    return {
        "stopped_jobs": stopped_jobs,
        "cancelled_commands": cancelled_commands,
        "deleted_logs": deleted_logs,
        "deleted_errors": deleted_errors,
        "deleted_attachments": deleted_attachments,
    }


def latest_round(db: Session, job_id: str) -> TaskRound | None:
    return db.scalar(
        select(TaskRound)
        .where(TaskRound.job_id == job_id)
        .order_by(desc(TaskRound.created_at), desc(TaskRound.round_index))
        .limit(1)
    )


def add_log(
    db: Session,
    stage: str,
    message: str,
    job_id: str | None = None,
    round_id: str | None = None,
    level: str = "info",
    extra: dict | None = None,
    display_message: str | None = None,
) -> RuntimeLog:
    extra = extra or {}
    item = RuntimeLog(
        job_id=job_id,
        round_id=round_id,
        level=level,
        stage=str(stage),
        message=message,
        display_message=display_message
        or build_display_message(str(stage), message, level=level, extra=extra),
        extra=extra,
    )
    db.add(item)
    db.flush()
    return item


def list_logs(db: Session, job_id: str | None = None, limit: int = 100) -> list[RuntimeLog]:
    query = select(RuntimeLog).order_by(desc(RuntimeLog.created_at)).limit(limit)
    if job_id:
        query = select(RuntimeLog).where(RuntimeLog.job_id == job_id).order_by(desc(RuntimeLog.created_at)).limit(limit)
    return list(reversed(db.scalars(query).all()))
