from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import Job, RuntimeLog, TaskRound
from app.services.orchestrator.states import JobState


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


def latest_round(db: Session, job_id: str) -> TaskRound | None:
    return db.scalar(
        select(TaskRound)
        .where(TaskRound.job_id == job_id)
        .order_by(desc(TaskRound.round_index))
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
) -> RuntimeLog:
    item = RuntimeLog(
        job_id=job_id,
        round_id=round_id,
        level=level,
        stage=str(stage),
        message=message,
        extra=extra or {},
    )
    db.add(item)
    db.flush()
    return item


def list_logs(db: Session, job_id: str | None = None, limit: int = 100) -> list[RuntimeLog]:
    query = select(RuntimeLog).order_by(desc(RuntimeLog.created_at)).limit(limit)
    if job_id:
        query = select(RuntimeLog).where(RuntimeLog.job_id == job_id).order_by(desc(RuntimeLog.created_at)).limit(limit)
    return list(reversed(db.scalars(query).all()))
