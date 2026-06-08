from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Worker
from app.db.models.base import now_utc
from app.worker_gateway.contracts import WorkerHeartbeat


def upsert_worker_heartbeat(db: Session, payload: WorkerHeartbeat) -> Worker:
    worker = db.scalar(select(Worker).where(Worker.worker_id == payload.worker_id))
    if not worker:
        worker = Worker(worker_id=payload.worker_id, machine_name=payload.machine_name)
        db.add(worker)
    worker.machine_name = payload.machine_name
    worker.supported_apps = payload.supported_apps
    worker.current_stage = payload.current_stage
    worker.current_window_title = payload.current_window_title
    worker.busy = payload.busy
    worker.last_seen_at = now_utc()
    db.commit()
    db.refresh(worker)
    return worker


def list_workers(db: Session, user_id: str | None = None) -> list[Worker]:
    query = select(Worker).order_by(Worker.worker_id)
    if user_id is not None:
        query = query.where(Worker.user_id == user_id)
    return list(db.scalars(query).all())
