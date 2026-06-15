from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from app.db.models import Attachment, AutomationError, Job, Project, RuntimeLog, TaskRound, WorkerCommand
from app.services.orchestrator.events import build_display_message
from app.services.orchestrator.states import JobState

TERMINAL_STATES = {JobState.STOPPED, JobState.PROJECT_COMPLETED}
ACTIVE_COMMAND_STATES = {"queued", "claimed", "running"}


def create_job(
    db: Session,
    user_id: str,
    directions: list[str],
    rule_version_id: str | None,
    scope_text: str = "",
    intent: dict | None = None,
) -> Job:
    job = Job(
        user_id=user_id,
        rule_version_id=rule_version_id,
        status=JobState.JOB_STARTING,
        scope_text=scope_text,
        directions=directions,
        intent=intent or {},
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


def reset_job_for_reopen(
    db: Session,
    job: Job,
    directions: list[str],
    rule_version_id: str | None,
    scope_text: str = "",
    intent: dict | None = None,
) -> tuple[TaskRound, dict]:
    old_round_ids = list(db.scalars(select(TaskRound.id).where(TaskRound.job_id == job.id)).all())
    old_project_ids = list(db.scalars(select(Project.id).where(Project.job_id == job.id)).all())
    old_command_ids = list(db.scalars(select(WorkerCommand.id).where(WorkerCommand.job_id == job.id)).all())
    active_commands = list(
        db.scalars(
            select(WorkerCommand).where(
                WorkerCommand.job_id == job.id,
                WorkerCommand.status.in_({"claimed", "running"}),
            )
        ).all()
    )
    active_command_ids = {command.id for command in active_commands}
    for command in active_commands:
        command.status = "cancelled"
        command.message = "Cancelled by Reopen reset."
        command.job_id = None
        command.round_id = None
        command.lease_id = ""
        command.lease_expires_at = None
    db.flush()

    command_delete_query = delete(WorkerCommand).where(WorkerCommand.job_id == job.id)
    if active_command_ids:
        command_delete_query = command_delete_query.where(WorkerCommand.id.not_in(active_command_ids))
    deleted_commands = db.execute(command_delete_query).rowcount or 0
    deleted_attachments = db.execute(delete(Attachment).where(Attachment.job_id == job.id)).rowcount or 0
    deleted_logs = db.execute(delete(RuntimeLog).where(RuntimeLog.job_id == job.id)).rowcount or 0
    deleted_errors = db.execute(delete(AutomationError).where(AutomationError.job_id == job.id)).rowcount or 0
    deleted_rounds = db.execute(delete(TaskRound).where(TaskRound.job_id == job.id)).rowcount or 0
    deleted_projects = db.execute(delete(Project).where(Project.job_id == job.id)).rowcount or 0

    job.rule_version_id = rule_version_id
    job.scope_text = scope_text
    job.directions = directions
    job.intent = intent or {}
    job.submitted_count = 0
    job.satisfied_count = 0
    job.status = JobState.GENERATING_PROMPT

    round_ = TaskRound(job_id=job.id, round_index=1, status=JobState.GENERATING_PROMPT)
    db.add(round_)
    db.flush()
    return round_, {
        "old_rounds": len(old_round_ids),
        "old_projects": len(old_project_ids),
        "old_commands": len(old_command_ids),
        "cancelled_active_commands": len(active_commands),
        "deleted_commands": deleted_commands,
        "deleted_attachments": deleted_attachments,
        "deleted_logs": deleted_logs,
        "deleted_errors": deleted_errors,
        "deleted_rounds": deleted_rounds,
        "deleted_projects": deleted_projects,
        "directions": directions,
        "scope_text": scope_text,
        "intent": intent or {},
    }


def cancel_job_worker_commands(
    db: Session,
    job_id: str,
    *,
    reason: str = "Cancelled by user stop.",
    exclude_command_types: set[str] | None = None,
) -> int:
    exclude_command_types = exclude_command_types or set()
    commands = list(
        db.scalars(
            select(WorkerCommand).where(
                WorkerCommand.job_id == job_id,
                WorkerCommand.status.in_(ACTIVE_COMMAND_STATES),
            )
        ).all()
    )
    cancelled = 0
    for command in commands:
        if command.command_type in exclude_command_types:
            continue
        command.status = "cancelled"
        command.message = reason
        cancelled += 1
    return cancelled


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
