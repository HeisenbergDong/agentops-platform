from fastapi import APIRouter, HTTPException
from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import Job, User
from app.db.repositories.jobs import add_log, create_job, current_job, latest_round, list_logs
from app.db.repositories.rules import active_rule_version
from app.db.session import get_db
from app.services.orchestrator.states import JobState
from app.services.user_settings import load_user_settings, readiness

router = APIRouter()


class StartJobRequest(BaseModel):
    directions: list[str]


@router.post("/start")
def start_job(payload: StartJobRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rule_version = active_rule_version(db)
    job = create_job(
        db,
        user_id=user.id,
        directions=payload.directions,
        rule_version_id=rule_version.id if rule_version else None,
    )
    settings_status = readiness(load_user_settings(db, user.id))
    message = "User settings loaded for this job."
    if not settings_status["complete"]:
        message = "User settings are incomplete; scheduler will continue with warnings."
    add_log(
        db,
        job_id=job.id,
        stage="user_settings",
        message=message,
        level="warning" if not settings_status["complete"] else "info",
        extra=settings_status,
    )
    db.commit()
    return serialize_job(db, job)


@router.post("/continue")
def continue_job(
    job_id: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    job = current_job(db, user.id)
    if not job:
        return {"status": "no_job", "message": "No existing job to continue."}
    round_ = latest_round(db, job.id)
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.LOADING_RULES,
        message="Continue requested; existing state preserved.",
    )
    db.commit()
    return serialize_job(db, job)


@router.post("/stop")
def stop_job(
    job_id: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    job = current_job(db, user.id)
    if not job:
        return {"status": "no_job", "message": "No existing job to stop."}
    job.status = JobState.STOPPED
    round_ = latest_round(db, job.id)
    if round_:
        round_.status = JobState.STOPPED
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.STOPPED,
        message="Stop requested; scheduler and worker should stop current activity.",
    )
    db.commit()
    db.refresh(job)
    return serialize_job(db, job)


@router.get("/current")
def get_current_job(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    job = current_job(db, user.id)
    if not job:
        return {"status": "idle", "job": None, "logs": []}
    return serialize_job(db, job)


@router.get("/{job_id}/logs")
def get_job_logs(
    job_id: str,
    limit: int = 100,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user.id))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return [serialize_log(item) for item in list_logs(db, job_id=job_id, limit=limit)]


def serialize_job(db: Session, job) -> dict:
    round_ = latest_round(db, job.id)
    logs = list_logs(db, job_id=job.id, limit=50)
    return {
        "status": job.status,
        "job": {
            "id": job.id,
            "status": job.status,
            "directions": job.directions,
            "daily_target": job.daily_target,
            "submitted_count": job.submitted_count,
            "satisfied_count": job.satisfied_count,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        },
        "round": {
            "id": round_.id,
            "round_index": round_.round_index,
            "status": round_.status,
            "trace_status": round_.trace_status,
            "github_status": round_.github_status,
            "feishu_status": round_.feishu_status,
        }
        if round_
        else None,
        "logs": [serialize_log(item) for item in logs],
    }


def serialize_log(item) -> dict:
    return {
        "id": item.id,
        "level": item.level,
        "stage": item.stage,
        "message": item.message,
        "extra": item.extra,
        "created_at": item.created_at.isoformat(),
    }
