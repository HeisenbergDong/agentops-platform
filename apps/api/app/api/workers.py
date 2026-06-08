from fastapi import APIRouter, Depends

from app.core.security import require_worker_token
from app.worker_gateway.contracts import WorkerHeartbeat, WorkerResult
from app.worker_gateway.registry import worker_registry

router = APIRouter()


@router.post("/heartbeat", dependencies=[Depends(require_worker_token)])
def heartbeat(payload: WorkerHeartbeat) -> dict:
    worker_registry.update(payload)
    return {"status": "ok", "worker": payload.model_dump()}


@router.get("")
def list_workers() -> list[dict]:
    return [item.model_dump() for item in worker_registry.list_workers()]


@router.get("/{worker_id}/commands", dependencies=[Depends(require_worker_token)])
def poll_commands(worker_id: str) -> dict:
    return {"worker_id": worker_id, "commands": []}


@router.post("/{worker_id}/results", dependencies=[Depends(require_worker_token)])
def post_result(worker_id: str, payload: WorkerResult) -> dict:
    return {"status": "received", "worker_id": worker_id, "result": payload.model_dump()}
