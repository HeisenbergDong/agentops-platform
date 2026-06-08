from datetime import timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.security import (
    generate_worker_registration_code,
    generate_worker_token,
    hash_worker_secret,
)
from app.db.models import Worker, WorkerCommand, WorkerRegistrationCode
from app.db.models.base import new_id, now_utc
from app.worker_gateway.contracts import (
    CreateWorkerCommandRequest,
    WorkerHeartbeat,
    WorkerRegisterRequest,
    WorkerResult,
)


def create_registration_code(
    db: Session,
    created_by: str,
    assigned_user_id: str | None = None,
    expires_minutes: int = 60,
) -> tuple[str, WorkerRegistrationCode]:
    code = generate_worker_registration_code()
    row = WorkerRegistrationCode(
        code_hash=hash_worker_secret(code),
        created_by=created_by,
        assigned_user_id=assigned_user_id,
        expires_at=now_utc() + timedelta(minutes=expires_minutes),
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return code, row


def list_registration_codes(db: Session) -> list[WorkerRegistrationCode]:
    return list(
        db.scalars(
            select(WorkerRegistrationCode).order_by(desc(WorkerRegistrationCode.created_at)).limit(50)
        ).all()
    )


def register_worker(db: Session, payload: WorkerRegisterRequest) -> tuple[Worker, str]:
    code_hash = hash_worker_secret(payload.registration_code)
    code = db.scalar(select(WorkerRegistrationCode).where(WorkerRegistrationCode.code_hash == code_hash))
    if not code or code.status != "active" or code.used_at is not None:
        raise ValueError("Invalid or used worker registration code")
    if _is_expired(code.expires_at):
        code.status = "expired"
        db.commit()
        raise ValueError("Worker registration code expired")

    worker_id = payload.worker_id.strip() or f"worker_{new_id()}"
    token = generate_worker_token()
    worker = db.scalar(select(Worker).where(Worker.worker_id == worker_id))
    if not worker:
        worker = Worker(worker_id=worker_id, machine_name=payload.machine_name)
        db.add(worker)
    worker.token_hash = hash_worker_secret(token)
    worker.display_name = payload.display_name or payload.machine_name
    worker.worker_type = payload.worker_type
    worker.machine_name = payload.machine_name
    worker.machine_fingerprint = payload.machine_fingerprint
    worker.version = payload.version
    worker.supported_apps = payload.supported_apps
    worker.capabilities = payload.capabilities
    worker.status = "online"
    worker.busy = False
    worker.last_seen_at = now_utc()
    worker.registered_at = worker.registered_at or now_utc()
    worker.revoked_at = None
    if code.assigned_user_id:
        worker.user_id = code.assigned_user_id

    code.status = "used"
    code.used_at = now_utc()
    code.used_by_worker_id = worker_id
    db.commit()
    db.refresh(worker)
    return worker, token


def _is_expired(expires_at) -> bool:
    current = now_utc()
    if expires_at.tzinfo is None:
        current = current.replace(tzinfo=None)
    return expires_at < current


def update_worker_heartbeat(db: Session, worker: Worker, payload: WorkerHeartbeat) -> Worker:
    worker.display_name = payload.display_name or worker.display_name or payload.machine_name
    worker.worker_type = payload.worker_type or worker.worker_type
    worker.machine_name = payload.machine_name
    worker.machine_fingerprint = payload.machine_fingerprint or worker.machine_fingerprint
    worker.version = payload.version or worker.version
    worker.supported_apps = payload.supported_apps
    worker.capabilities = payload.capabilities or payload.supported_apps
    worker.current_stage = payload.current_stage
    worker.current_window_title = payload.current_window_title
    worker.busy = payload.busy
    worker.status = "busy" if payload.busy else "online"
    worker.last_seen_at = now_utc()
    db.commit()
    db.refresh(worker)
    return worker


def list_workers(db: Session, user_id: str | None = None) -> list[Worker]:
    query = select(Worker).order_by(Worker.worker_id)
    if user_id is not None:
        query = query.where(Worker.user_id == user_id)
    return list(db.scalars(query).all())


def get_worker_by_worker_id(db: Session, worker_id: str) -> Worker | None:
    return db.scalar(select(Worker).where(Worker.worker_id == worker_id))


def bind_worker(db: Session, worker_id: str, user_id: str | None) -> Worker | None:
    worker = get_worker_by_worker_id(db, worker_id)
    if not worker:
        return None
    worker.user_id = user_id
    db.commit()
    db.refresh(worker)
    return worker


def create_worker_command(
    db: Session,
    worker_id: str,
    user_id: str | None,
    payload: CreateWorkerCommandRequest,
) -> WorkerCommand:
    command = WorkerCommand(
        worker_id=worker_id,
        user_id=user_id,
        job_id=payload.job_id,
        round_id=payload.round_id,
        command_type=payload.type.value,
        payload=payload.payload,
        status="queued",
    )
    db.add(command)
    db.commit()
    db.refresh(command)
    return command


def poll_worker_commands(db: Session, worker_id: str, limit: int = 5) -> list[WorkerCommand]:
    rows = list(
        db.scalars(
            select(WorkerCommand)
            .where(WorkerCommand.worker_id == worker_id, WorkerCommand.status == "queued")
            .order_by(WorkerCommand.created_at)
            .limit(limit)
        ).all()
    )
    for row in rows:
        row.status = "claimed"
        row.claimed_at = now_utc()
        row.attempts += 1
    db.commit()
    return rows


def ack_worker_command(db: Session, worker_id: str, command_id: str) -> WorkerCommand | None:
    command = db.scalar(
        select(WorkerCommand).where(WorkerCommand.id == command_id, WorkerCommand.worker_id == worker_id)
    )
    if not command:
        return None
    command.status = "running"
    command.claimed_at = command.claimed_at or now_utc()
    db.commit()
    db.refresh(command)
    return command


def finish_worker_command(db: Session, worker_id: str, payload: WorkerResult) -> WorkerCommand | None:
    command = db.scalar(
        select(WorkerCommand).where(
            WorkerCommand.id == payload.command_id,
            WorkerCommand.worker_id == worker_id,
        )
    )
    if not command:
        return None
    command.status = "completed" if payload.status in {"ok", "success", "completed"} else "failed"
    command.finished_at = now_utc()
    command.message = payload.message
    command.result = payload.data
    command.error = payload.error or ""
    db.commit()
    db.refresh(command)
    return command
