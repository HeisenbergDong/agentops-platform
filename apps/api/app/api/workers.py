from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, current_worker
from app.db.models import User, Worker, WorkerCommand
from app.db.models.base import now_utc
from app.db.repositories.jobs import add_log
from app.db.repositories.workers import (
    ack_worker_command,
    create_worker_command,
    finish_worker_command,
    get_worker_by_worker_id,
    list_workers,
    poll_worker_commands,
    register_worker,
    update_worker_heartbeat,
)
from app.db.session import get_db
from app.services.orchestrator.worker_results import handle_worker_result
from app.services.user_settings import load_user_settings
from app.worker_gateway.contracts import (
    CreateWorkerCommandRequest,
    WorkerHeartbeat,
    WorkerLogEntry,
    WorkerRegisterRequest,
    WorkerResult,
)

router = APIRouter()


@router.post("/register")
def register(payload: WorkerRegisterRequest, db: Session = Depends(get_db)) -> dict:
    try:
        worker, token = register_worker(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "registered",
        "worker": serialize_worker(worker),
        "worker_id": worker.worker_id,
        "worker_token": token,
    }


@router.post("/heartbeat")
def heartbeat(
    payload: WorkerHeartbeat,
    worker: Worker = Depends(current_worker),
    db: Session = Depends(get_db),
) -> dict:
    if payload.worker_id != worker.worker_id:
        raise HTTPException(status_code=403, detail="Worker token does not match worker_id")
    item = update_worker_heartbeat(db, worker, payload)
    return {"status": "ok", "worker": serialize_worker(item), "assigned_config": assigned_worker_config(db, item)}


@router.get("")
def get_workers(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    owner_id = None if user.role == "admin" else user.id
    return [serialize_worker(item) for item in list_workers(db, user_id=owner_id)]


@router.post("/{worker_id}/commands")
def create_command(
    worker_id: str,
    payload: CreateWorkerCommandRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    worker = get_worker_by_worker_id(db, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    if user.role != "admin" and worker.user_id != user.id:
        raise HTTPException(status_code=403, detail="Worker is not bound to current user")
    command = create_worker_command(db, worker_id=worker.worker_id, user_id=user.id, payload=payload)
    return serialize_command(command)


@router.get("/{worker_id}/recent-commands")
def recent_commands(
    worker_id: str,
    limit: int = 10,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    worker = get_worker_by_worker_id(db, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    if user.role != "admin" and worker.user_id != user.id:
        raise HTTPException(status_code=403, detail="Worker is not bound to current user")
    safe_limit = max(1, min(limit, 50))
    rows = list(
        db.scalars(
            select(WorkerCommand)
            .where(WorkerCommand.worker_id == worker.worker_id)
            .order_by(WorkerCommand.created_at.desc())
            .limit(safe_limit)
        ).all()
    )
    return {"worker_id": worker.worker_id, "commands": [serialize_command(item) for item in rows]}


@router.get("/{worker_id}/commands")
def poll_commands(
    worker_id: str,
    worker: Worker = Depends(current_worker),
    db: Session = Depends(get_db),
) -> dict:
    if worker.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="Worker token does not match worker_id")
    commands = poll_worker_commands(db, worker_id=worker_id)
    return {"worker_id": worker_id, "commands": [serialize_command(item) for item in commands]}


@router.post("/{worker_id}/commands/{command_id}/ack")
def ack_command(
    worker_id: str,
    command_id: str,
    worker: Worker = Depends(current_worker),
    db: Session = Depends(get_db),
) -> dict:
    if worker.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="Worker token does not match worker_id")
    command = ack_worker_command(db, worker_id, command_id)
    if not command:
        raise HTTPException(status_code=404, detail="Command not found")
    return serialize_command(command)


@router.post("/{worker_id}/results")
def post_result(
    worker_id: str,
    payload: WorkerResult,
    worker: Worker = Depends(current_worker),
    db: Session = Depends(get_db),
) -> dict:
    if worker.worker_id != worker_id or payload.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="Worker token does not match worker_id")
    command = finish_worker_command(db, worker_id, payload)
    if not command:
        raise HTTPException(status_code=404, detail="Command not found")
    handle_worker_result(db, command, payload)
    return {"status": "received", "worker_id": worker_id, "command": serialize_command(command)}


@router.post("/{worker_id}/logs")
def post_log(
    worker_id: str,
    payload: WorkerLogEntry,
    worker: Worker = Depends(current_worker),
    db: Session = Depends(get_db),
) -> dict:
    if worker.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="Worker token does not match worker_id")
    log = add_log(
        db,
        job_id=payload.job_id,
        round_id=payload.round_id,
        level=payload.level,
        stage=payload.stage,
        message=payload.message,
        extra={"worker_id": worker_id, "command_id": payload.command_id, **payload.extra},
        display_message=payload.display_message,
    )
    db.commit()
    return {"status": "received", "log_id": log.id}


def assigned_worker_config(db: Session, worker: Worker) -> dict:
    if not worker.user_id:
        return {}
    settings = load_user_settings(db, worker.user_id)
    worker_settings = settings.get("worker", {})
    if worker_settings.get("worker_id") != worker.worker_id:
        return {}
    return {
        "trae_workspace_path": worker_settings.get("trae_workspace_path", ""),
        "browser_url": worker_settings.get("browser_url", ""),
    }


def serialize_worker(item: Worker) -> dict:
    status = effective_worker_status(item)
    return {
        "id": item.id,
        "worker_id": item.worker_id,
        "user_id": item.user_id,
        "display_name": item.display_name,
        "worker_type": item.worker_type,
        "machine_name": item.machine_name,
        "machine_fingerprint": item.machine_fingerprint,
        "version": item.version,
        "supported_apps": item.supported_apps,
        "capabilities": item.capabilities,
        "status": status,
        "current_stage": item.current_stage,
        "current_window_title": item.current_window_title,
        "busy": item.busy,
        "last_seen_at": item.last_seen_at.isoformat(),
        "registered_at": item.registered_at.isoformat() if item.registered_at else None,
        "revoked": bool(item.revoked_at),
    }


def effective_worker_status(item: Worker) -> str:
    if item.revoked_at:
        return "revoked"
    if not item.last_seen_at:
        return "offline"
    current = now_utc()
    last_seen = item.last_seen_at
    if last_seen.tzinfo is None:
        current = current.replace(tzinfo=None)
    if current - last_seen > timedelta(minutes=2):
        return "offline"
    return item.status


def serialize_command(item) -> dict:
    return {
        "command_id": item.id,
        "worker_id": item.worker_id,
        "user_id": item.user_id,
        "job_id": item.job_id,
        "round_id": item.round_id,
        "type": item.command_type,
        "payload": item.payload,
        "status": item.status,
        "attempts": item.attempts,
        "message": item.message,
        "result": item.result,
        "error": item.error,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "claimed_at": item.claimed_at.isoformat() if item.claimed_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
    }
