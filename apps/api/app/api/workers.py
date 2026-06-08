from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import require_worker_token
from app.db.repositories.workers import list_workers, upsert_worker_heartbeat
from app.db.session import get_db
from app.worker_gateway.contracts import WorkerHeartbeat, WorkerResult

router = APIRouter()


@router.post("/heartbeat", dependencies=[Depends(require_worker_token)])
def heartbeat(payload: WorkerHeartbeat, db: Session = Depends(get_db)) -> dict:
    worker = upsert_worker_heartbeat(db, payload)
    return {"status": "ok", "worker": serialize_worker(worker)}


@router.get("")
def get_workers(db: Session = Depends(get_db)) -> list[dict]:
    return [serialize_worker(item) for item in list_workers(db)]


@router.get("/{worker_id}/commands", dependencies=[Depends(require_worker_token)])
def poll_commands(worker_id: str) -> dict:
    return {"worker_id": worker_id, "commands": []}


@router.post("/{worker_id}/results", dependencies=[Depends(require_worker_token)])
def post_result(worker_id: str, payload: WorkerResult) -> dict:
    return {"status": "received", "worker_id": worker_id, "result": payload.model_dump()}


def serialize_worker(item) -> dict:
    return {
        "id": item.id,
        "worker_id": item.worker_id,
        "machine_name": item.machine_name,
        "supported_apps": item.supported_apps,
        "current_stage": item.current_stage,
        "current_window_title": item.current_window_title,
        "busy": item.busy,
        "last_seen_at": item.last_seen_at.isoformat(),
    }
