from datetime import datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    generate_worker_registration_code,
    generate_worker_token,
    hash_worker_secret,
)
from app.db.models import Job, RuntimeLog, TaskRound, Worker, WorkerCommand, WorkerRegistrationCode
from app.db.models.base import new_id, now_utc
from app.services.orchestrator.events import build_display_message
from app.services.orchestrator.states import JobState
from app.worker_gateway.contracts import (
    CreateWorkerCommandRequest,
    WorkerCommandType,
    WorkerHeartbeat,
    WorkerRegisterRequest,
    WorkerResult,
)

LEASED_COMMAND_STATES = {"claimed", "running"}
ACTIVE_COMMAND_STATES = {"queued", "claimed", "running"}
TERMINAL_COMMAND_STATES = {"completed", "failed", "manual_required", "cancelled"}
DEFAULT_LOCAL_WORKER_ID = "local-windows-worker"
LATE_SEND_PROMPT_GRACE_SECONDS = 90


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

    requested_worker_id = payload.worker_id.strip()
    worker_id = "" if requested_worker_id == DEFAULT_LOCAL_WORKER_ID else requested_worker_id
    worker_id = worker_id or f"worker_{new_id()}"
    token = generate_worker_token()
    worker = db.scalar(select(Worker).where(Worker.worker_id == worker_id))
    if not worker:
        worker = Worker(worker_id=worker_id, machine_name=payload.machine_name)
        db.add(worker)
    elif not _registration_can_update_worker(worker, code.assigned_user_id):
        raise ValueError(
            "Worker ID is already bound to another user. Use a unique worker_id or ask an admin to rebind it."
        )
    worker.token_hash = hash_worker_secret(token)
    worker.display_name = payload.display_name or payload.machine_name
    worker.worker_type = payload.worker_type
    worker.machine_name = payload.machine_name
    worker.machine_fingerprint = payload.machine_fingerprint
    worker.version = payload.version
    worker.supported_apps = payload.supported_apps
    worker.capabilities = payload.capabilities
    worker.status = "offline"
    worker.busy = False
    worker.last_seen_at = None
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


def _registration_can_update_worker(worker: Worker, assigned_user_id: str | None) -> bool:
    if not worker.user_id:
        return True
    return bool(assigned_user_id and worker.user_id == assigned_user_id)


def _is_expired(expires_at) -> bool:
    current = now_utc()
    if expires_at.tzinfo is None:
        current = current.replace(tzinfo=None)
    return expires_at < current


def _effective_worker_status(worker: Worker) -> str:
    if worker.revoked_at:
        return "revoked"
    if not worker.last_seen_at:
        return "offline"
    current = now_utc()
    last_seen = worker.last_seen_at
    if last_seen.tzinfo is None:
        current = current.replace(tzinfo=None)
    if current - last_seen > timedelta(minutes=2):
        return "offline"
    return str(worker.status or "offline")


def update_worker_heartbeat(db: Session, worker: Worker, payload: WorkerHeartbeat) -> Worker:
    expire_worker_command_leases(db, worker_id=worker.worker_id)
    worker.display_name = payload.display_name or worker.display_name or payload.machine_name
    worker.worker_type = payload.worker_type or worker.worker_type
    worker.machine_name = payload.machine_name
    worker.machine_fingerprint = payload.machine_fingerprint or worker.machine_fingerprint
    worker.version = payload.version or worker.version
    worker.supported_apps = payload.supported_apps
    worker.capabilities = payload.capabilities or payload.supported_apps
    worker.current_stage = payload.current_stage
    worker.current_window_title = payload.current_window_title
    worker.runtime_status = payload.runtime_status or {}
    worker.busy = payload.busy
    worker.status = "busy" if payload.busy else "online"
    worker.last_seen_at = now_utc()
    db.commit()
    db.refresh(worker)
    return worker


def list_workers(db: Session, user_id: str | None = None, include_revoked: bool = False) -> list[Worker]:
    query = select(Worker).order_by(Worker.worker_id)
    if not include_revoked:
        query = query.where(Worker.revoked_at.is_(None))
    if user_id is not None:
        query = query.where(Worker.user_id == user_id)
    return list(db.scalars(query).all())


def get_worker_by_worker_id(db: Session, worker_id: str, include_revoked: bool = False) -> Worker | None:
    query = select(Worker).where(Worker.worker_id == worker_id)
    if not include_revoked:
        query = query.where(Worker.revoked_at.is_(None))
    return db.scalar(query)


def bind_worker(db: Session, worker_id: str, user_id: str | None) -> Worker | None:
    worker = get_worker_by_worker_id(db, worker_id)
    if not worker:
        return None
    worker.user_id = user_id
    db.commit()
    db.refresh(worker)
    return worker


def delete_worker(db: Session, worker_id: str) -> Worker | None:
    worker = get_worker_by_worker_id(db, worker_id, include_revoked=True)
    if not worker:
        return None
    if _effective_worker_status(worker) in {"online", "busy"}:
        raise ValueError("Cannot delete an online Worker. Stop it first and wait for it to go offline.")

    now = now_utc()
    commands = list(
        db.scalars(
            select(WorkerCommand).where(
                WorkerCommand.worker_id == worker.worker_id,
                WorkerCommand.status.in_(ACTIVE_COMMAND_STATES),
            )
        ).all()
    )
    for command in commands:
        command.status = "cancelled"
        command.finished_at = command.finished_at or now
        command.lease_id = ""
        command.lease_expires_at = None
        command.error = "Worker was deleted by an administrator."
        command.message = command.error

    worker.user_id = None
    worker.token_hash = ""
    worker.status = "revoked"
    worker.busy = False
    worker.current_stage = "deleted"
    worker.current_window_title = ""
    worker.revoked_at = worker.revoked_at or now
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
        lease_id="",
        lease_expires_at=None,
    )
    db.add(command)
    db.commit()
    db.refresh(command)
    return command


def poll_worker_commands(db: Session, worker_id: str, limit: int = 5) -> list[WorkerCommand]:
    expire_worker_command_leases(db, worker_id=worker_id)
    rows = list(
        db.scalars(
            select(WorkerCommand)
            .where(WorkerCommand.worker_id == worker_id, WorkerCommand.status == "queued")
            .order_by(
                (WorkerCommand.command_type == WorkerCommandType.STOP_CURRENT_TASK.value).desc(),
                WorkerCommand.created_at,
            )
            .limit(limit)
        ).all()
    )
    claimed_at = now_utc()
    for row in rows:
        row.status = "claimed"
        row.claimed_at = claimed_at
        row.attempts += 1
        row.lease_id = new_id()
        row.lease_expires_at = claimed_at + timedelta(seconds=max(1, settings.worker_command_claim_lease_seconds))
    db.commit()
    return rows


def ack_worker_command(
    db: Session,
    worker_id: str,
    command_id: str,
    lease_id: str = "",
) -> tuple[WorkerCommand | None, str]:
    expire_worker_command_leases(db, worker_id=worker_id)
    command = db.scalar(
        select(WorkerCommand).where(WorkerCommand.id == command_id, WorkerCommand.worker_id == worker_id)
    )
    if not command:
        return None, "missing"
    if command.status in TERMINAL_COMMAND_STATES:
        db.commit()
        db.refresh(command)
        return command, "ok"
    if command.status != "claimed" or not _lease_matches(command, lease_id):
        db.commit()
        db.refresh(command)
        return command, "stale_lease"
    now = now_utc()
    command.status = "running"
    command.claimed_at = command.claimed_at or now
    command.lease_id = new_id()
    command.lease_expires_at = now + timedelta(seconds=max(1, settings.worker_command_run_lease_seconds))
    db.commit()
    db.refresh(command)
    return command, "ok"


def get_worker_command(db: Session, worker_id: str, command_id: str) -> WorkerCommand | None:
    return db.scalar(
        select(WorkerCommand).where(
            WorkerCommand.id == command_id,
            WorkerCommand.worker_id == worker_id,
        )
    )


def read_worker_command_for_worker(
    db: Session,
    worker_id: str,
    command_id: str,
    lease_id: str = "",
    *,
    renew: bool = False,
) -> tuple[WorkerCommand | None, str]:
    expire_worker_command_leases(db, worker_id=worker_id)
    command = get_worker_command(db, worker_id, command_id)
    if not command:
        return None, "missing"
    if command.status == "running":
        if not _lease_matches(command, lease_id):
            db.commit()
            db.refresh(command)
            return command, "stale_lease"
        if renew:
            command.lease_expires_at = now_utc() + timedelta(
                seconds=max(1, settings.worker_command_run_lease_seconds)
            )
            db.commit()
            db.refresh(command)
    return command, "ok"


def finish_worker_command(db: Session, worker_id: str, payload: WorkerResult) -> tuple[WorkerCommand | None, str]:
    command = get_worker_command(db, worker_id, payload.command_id)
    if not command:
        return None, "missing"
    if command.status == "cancelled":
        if payload.status != "cancelled":
            if _can_accept_late_send_prompt_success(command, payload):
                command.status = "completed"
                command.finished_at = now_utc()
                command.lease_id = ""
                command.lease_expires_at = None
                command.message = payload.message
                command.result = payload.data
                command.error = payload.error or ""
                db.commit()
                db.refresh(command)
                return command, "late_send_prompt_success"
            db.commit()
            db.refresh(command)
            return command, "stale_lease"
        data = payload.data if isinstance(payload.data, dict) else {}
        has_stop_report = isinstance(data.get("stop_report"), dict)
        if command.lease_id and not _lease_matches(command, payload.lease_id):
            if not has_stop_report:
                db.commit()
                db.refresh(command)
                return command, "stale_lease"
    elif command.status in TERMINAL_COMMAND_STATES:
        db.commit()
        db.refresh(command)
        return command, "stale_lease"
    elif command.lease_id and not _lease_matches(command, payload.lease_id):
        db.commit()
        db.refresh(command)
        return command, "stale_lease"
    if command.status == "cancelled":
        command.finished_at = command.finished_at or now_utc()
        data = payload.data if isinstance(payload.data, dict) else {}
        if isinstance(data.get("stop_report"), dict):
            command.status = "completed"
    elif payload.status in {"ok", "success", "completed"}:
        command.status = "completed"
    elif payload.status in {"failed", "manual_required", "cancelled"}:
        command.status = payload.status
    else:
        command.status = "failed"
    command.finished_at = now_utc()
    command.lease_id = ""
    command.lease_expires_at = None
    command.message = payload.message
    command.result = payload.data
    command.error = payload.error or ""
    db.commit()
    db.refresh(command)
    return command, "ok"


def _can_accept_late_send_prompt_success(command: WorkerCommand, payload: WorkerResult) -> bool:
    if command.command_type != WorkerCommandType.SEND_PROMPT.value:
        return False
    if payload.status not in {"ok", "success", "completed"}:
        return False
    if command.error != "Worker command run lease expired; worker likely crashed or lost contact.":
        return False
    if not command.finished_at:
        return False
    current = now_utc()
    finished_at = command.finished_at
    if finished_at.tzinfo is None and current.tzinfo is not None:
        current = current.replace(tzinfo=None)
    elif finished_at.tzinfo is not None and current.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=None)
    return current - finished_at <= timedelta(seconds=LATE_SEND_PROMPT_GRACE_SECONDS)


def expire_worker_command_leases(db: Session, worker_id: str | None = None) -> dict[str, int]:
    now = now_utc()
    query = select(WorkerCommand).where(
        WorkerCommand.status.in_(LEASED_COMMAND_STATES),
        WorkerCommand.lease_expires_at.is_not(None),
    )
    if worker_id:
        query = query.where(WorkerCommand.worker_id == worker_id)
    commands = [
        command
        for command in db.scalars(query).all()
        if command.lease_expires_at and _is_at_or_before(command.lease_expires_at, now)
    ]
    requeued = 0
    cancelled = 0
    failed = 0
    for command in commands:
        previous_status = command.status
        if command.status == "claimed":
            if command.attempts >= max(1, settings.worker_command_max_claim_attempts):
                command.status = "failed"
                command.finished_at = now
                command.error = "Worker command claim lease expired too many times."
                command.message = command.error
                command.lease_id = ""
                command.lease_expires_at = None
                failed += 1
                _mark_job_manual_required(db, command, now)
            else:
                command.status = "queued"
                command.claimed_at = None
                command.lease_id = ""
                command.lease_expires_at = None
                command.message = "Worker command claim lease expired; requeued."
                requeued += 1
        elif command.status == "running":
            command.status = "cancelled"
            command.finished_at = now
            command.error = "Worker command run lease expired; worker likely crashed or lost contact."
            command.message = command.error
            command.lease_id = ""
            command.lease_expires_at = None
            cancelled += 1
            _mark_job_manual_required(db, command, now)
        if command.status != previous_status:
            _add_lease_expired_log(db, command, previous_status, now)
    if commands:
        db.commit()
    return {"requeued": requeued, "cancelled": cancelled, "failed": failed}


def _lease_matches(command: WorkerCommand, lease_id: str | None) -> bool:
    expected = str(command.lease_id or "")
    provided = str(lease_id or "")
    return bool(expected) and expected == provided


def _is_at_or_before(value: datetime, reference: datetime) -> bool:
    if value.tzinfo is None and reference.tzinfo is not None:
        reference = reference.replace(tzinfo=None)
    elif value.tzinfo is not None and reference.tzinfo is None:
        value = value.replace(tzinfo=None)
    return value <= reference


def _mark_job_manual_required(db: Session, command: WorkerCommand, now: datetime) -> None:
    if not command.job_id:
        return
    job = db.get(Job, command.job_id)
    if job and job.status not in {JobState.STOPPED, JobState.PROJECT_COMPLETED}:
        job.status = JobState.MANUAL_REQUIRED
        job.updated_at = now
    if command.round_id:
        round_ = db.get(TaskRound, command.round_id)
        if round_ and round_.status not in {JobState.STOPPED, JobState.PROJECT_COMPLETED}:
            round_.status = JobState.MANUAL_REQUIRED
            round_.updated_at = now


def _add_lease_expired_log(
    db: Session,
    command: WorkerCommand,
    previous_status: str,
    now: datetime,
) -> None:
    if not command.job_id:
        return
    level = "warning" if command.status == "queued" else "error"
    message = (
        "Worker command claim lease expired and was requeued."
        if command.status == "queued"
        else "Worker command lease expired; command was stopped for crash recovery."
    )
    extra = {
        "command_id": command.id,
        "worker_id": command.worker_id,
        "command_type": command.command_type,
        "previous_status": previous_status,
        "status": command.status,
        "attempts": command.attempts,
    }
    log = RuntimeLog(
        job_id=command.job_id,
        round_id=command.round_id,
        level=level,
        stage="worker_command_lease_expired",
        message=message,
        display_message=build_display_message("worker_command_lease_expired", message, level=level, extra=extra),
        extra=extra,
        created_at=now,
        updated_at=now,
    )
    db.add(log)
