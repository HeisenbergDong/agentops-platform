from datetime import timedelta
import base64
import hashlib
import hmac
import json
from pathlib import Path
import re
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, current_worker
from app.core.config import settings
from app.db.models import Attachment, User, Worker, WorkerCommand
from app.db.models.base import now_utc
from app.db.repositories.jobs import add_log
from app.db.repositories.workers import (
    ack_worker_command,
    create_worker_command,
    finish_worker_command,
    get_worker_by_worker_id,
    list_workers,
    poll_worker_commands,
    read_worker_command_for_worker,
    register_worker,
    update_worker_heartbeat,
)
from app.db.session import get_db
from app.services.orchestrator.worker_results import handle_worker_result
from app.services.trae_ui_analyst import analyze_trae_ui
from app.services.user_settings import load_user_settings
from app.worker_gateway.contracts import (
    CreateWorkerCommandRequest,
    WorkerHeartbeat,
    WorkerLogEntry,
    WorkerRegisterRequest,
    WorkerResult,
)

router = APIRouter()

WORKER_PACKAGE_DOWNLOAD_TOKEN_TTL_SECONDS = 120
WORKER_PACKAGE_MISSING_DETAIL = (
    "Worker package is not available. Ask an administrator to upload or build agentops-worker-windows.zip."
)


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


@router.get("/package")
def download_worker_package(user: User = Depends(current_user)) -> FileResponse:
    return _download_worker_package_response()


@router.head("/package")
def head_worker_package(user: User = Depends(current_user)) -> Response:
    return _worker_package_head_response()


@router.post("/package-ticket")
def create_worker_package_ticket(user: User = Depends(current_user)) -> dict:
    package = _worker_package_path()
    if not package:
        raise HTTPException(status_code=404, detail=WORKER_PACKAGE_MISSING_DETAIL)
    return {
        "download_url": f"/api/workers/package-download/{_create_worker_package_download_token(user.id)}",
        "expires_in": WORKER_PACKAGE_DOWNLOAD_TOKEN_TTL_SECONDS,
        "filename": package.name,
    }


@router.get("/package-download/{download_token}")
def download_worker_package_by_ticket(download_token: str) -> FileResponse:
    _verify_worker_package_download_token(download_token)
    return _download_worker_package_response()


@router.head("/package-download/{download_token}")
def head_worker_package_by_ticket(download_token: str) -> Response:
    _verify_worker_package_download_token(download_token)
    return _worker_package_head_response()


def _download_worker_package_response() -> FileResponse:
    package = _worker_package_path()
    if not package:
        raise HTTPException(status_code=404, detail=WORKER_PACKAGE_MISSING_DETAIL)
    return FileResponse(
        package,
        media_type="application/zip",
        filename=package.name,
    )


def _worker_package_head_response() -> Response:
    package = _worker_package_path()
    if not package:
        raise HTTPException(status_code=404, detail=WORKER_PACKAGE_MISSING_DETAIL)
    return Response(
        headers={
            "content-disposition": f'attachment; filename="{package.name}"',
            "content-length": str(package.stat().st_size),
            "content-type": "application/zip",
        }
    )


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
    lease_id: str = "",
    worker: Worker = Depends(current_worker),
    db: Session = Depends(get_db),
) -> dict:
    if worker.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="Worker token does not match worker_id")
    command, status = ack_worker_command(db, worker_id, command_id, lease_id=lease_id)
    if not command:
        raise HTTPException(status_code=404, detail="Command not found")
    result = serialize_command(command)
    result["ack_status"] = status
    if status != "ok":
        result["status"] = status
    return result


@router.get("/{worker_id}/commands/{command_id}")
def get_command_status(
    worker_id: str,
    command_id: str,
    lease_id: str = "",
    worker: Worker = Depends(current_worker),
    db: Session = Depends(get_db),
) -> dict:
    if worker.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="Worker token does not match worker_id")
    command, status = read_worker_command_for_worker(
        db,
        worker_id,
        command_id,
        lease_id=lease_id,
        renew=True,
    )
    if not command:
        raise HTTPException(status_code=404, detail="Command not found")
    result = serialize_command(command)
    result["read_status"] = status
    if status != "ok":
        result["status"] = status
    return result


@router.post("/{worker_id}/results")
def post_result(
    worker_id: str,
    payload: WorkerResult,
    worker: Worker = Depends(current_worker),
    db: Session = Depends(get_db),
) -> dict:
    if worker.worker_id != worker_id or payload.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="Worker token does not match worker_id")
    command, status = finish_worker_command(db, worker_id, payload)
    if not command:
        raise HTTPException(status_code=404, detail="Command not found")
    if status == "stale_lease":
        return {"status": "ignored", "reason": "stale_lease", "worker_id": worker_id, "command": serialize_command(command)}
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
    command = None
    if payload.command_id:
        command = db.scalar(
            select(WorkerCommand).where(WorkerCommand.id == payload.command_id, WorkerCommand.worker_id == worker_id)
        )
        if command and command.job_id != payload.job_id:
            return {"status": "ignored", "reason": "stale_command_context"}
    log = add_log(
        db,
        job_id=payload.job_id,
        round_id=payload.round_id if not command else command.round_id,
        level=payload.level,
        stage=payload.stage,
        message=payload.message,
        extra={"worker_id": worker_id, "command_id": payload.command_id, **payload.extra},
        display_message=payload.display_message,
    )
    fallback_result = _result_from_finished_log(worker_id, command, payload)
    if fallback_result:
        finished_command, finish_status = finish_worker_command(db, worker_id, fallback_result)
        if finished_command and finish_status != "stale_lease":
            add_log(
                db,
                job_id=finished_command.job_id,
                round_id=finished_command.round_id,
                level="warning",
                stage="worker_result_recovered_from_log",
                message="Worker result was recovered from worker_command_finished log because the /results callback was not received first.",
                extra={
                    "worker_id": worker_id,
                    "command_id": finished_command.id,
                    "command_type": finished_command.command_type,
                    "finish_status": finish_status,
                    "source_log_id": log.id,
                    "result_status": fallback_result.status,
                },
            )
            handle_worker_result(db, finished_command, fallback_result)
    db.commit()
    return {"status": "received", "log_id": log.id}


def _result_from_finished_log(worker_id: str, command: WorkerCommand | None, payload: WorkerLogEntry) -> WorkerResult | None:
    if payload.stage != "worker_command_finished" or not command:
        return None
    if command.status not in {"claimed", "running"}:
        return None
    extra = payload.extra if isinstance(payload.extra, dict) else {}
    result_data = extra.get("result")
    if not isinstance(result_data, dict):
        return None
    result_status = str(extra.get("result_status") or "").strip()
    if not result_status:
        return None
    return WorkerResult(
        command_id=command.id,
        worker_id=worker_id,
        lease_id=str(command.lease_id or ""),
        status=result_status,
        message=str(extra.get("message") or payload.message or ""),
        error=str(extra.get("error") or "") or None,
        data=result_data,
    )


@router.post("/{worker_id}/attachments")
def upload_worker_attachment(
    worker_id: str,
    kind: str = Form(...),
    job_id: str | None = Form(None),
    round_id: str | None = Form(None),
    file: UploadFile = File(...),
    worker: Worker = Depends(current_worker),
    db: Session = Depends(get_db),
) -> dict:
    if worker.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="Worker token does not match worker_id")
    safe_kind = _safe_path_part(kind or "artifact")
    safe_filename = _safe_filename(file.filename or f"{safe_kind}.bin")
    out_dir = settings.attachment_root / "workers" / _safe_path_part(worker_id) / safe_kind
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _unique_path(out_dir / safe_filename)
    size_bytes = 0
    with path.open("wb") as target:
        while chunk := file.file.read(1024 * 1024):
            size_bytes += len(chunk)
            target.write(chunk)
    attachment = Attachment(
        user_id=worker.user_id,
        job_id=job_id,
        round_id=round_id,
        kind=safe_kind,
        filename=path.name,
        path=str(path),
        content_type=file.content_type or "application/octet-stream",
        size_bytes=size_bytes,
    )
    db.add(attachment)
    db.commit()
    db.refresh(attachment)
    return {"status": "uploaded", "attachment": serialize_attachment(attachment)}


@router.post("/{worker_id}/trae-ui/analyze")
def analyze_worker_trae_ui(
    worker_id: str,
    context: str = Form("{}"),
    file: UploadFile = File(...),
    worker: Worker = Depends(current_worker),
    db: Session = Depends(get_db),
) -> dict:
    if worker.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="Worker token does not match worker_id")
    if not worker.user_id:
        raise HTTPException(status_code=400, detail="Worker is not bound to a user; model settings are unavailable")
    try:
        import json

        parsed_context = json.loads(context or "{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="context must be valid JSON") from exc
    if not isinstance(parsed_context, dict):
        raise HTTPException(status_code=400, detail="context must be a JSON object")
    image_bytes = file.file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="screenshot file is empty")
    configs = load_user_settings(db, worker.user_id)
    try:
        analysis = analyze_trae_ui(
            configs,
            image_bytes=image_bytes,
            mime_type=file.content_type or "image/png",
            context=parsed_context,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Trae UI Analyst failed: {exc}") from exc
    return {"status": "analyzed", "analysis": analysis}


def assigned_worker_config(db: Session, worker: Worker) -> dict:
    if not worker.user_id:
        return {}
    settings = load_user_settings(db, worker.user_id)
    worker_settings = settings.get("worker", {})
    if worker_settings.get("worker_id") != worker.worker_id:
        return {}
    return {
        "trae_workspace_path": worker_settings.get("trae_workspace_path", ""),
        "trae_exe_path": worker_settings.get("trae_exe_path", ""),
        "browser_url": worker_settings.get("browser_url", ""),
    }


def serialize_worker(item: Worker) -> dict:
    status = effective_worker_status(item)
    online = status in {"online", "busy"}
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
        "online": online,
        "registered": bool(item.registered_at),
        "current_stage": item.current_stage,
        "current_window_title": item.current_window_title,
        "runtime_status": item.runtime_status or {},
        "busy": bool(item.busy) if online else False,
        "last_seen_at": item.last_seen_at.isoformat() if item.last_seen_at else None,
        "registered_at": item.registered_at.isoformat() if item.registered_at else None,
        "revoked": bool(item.revoked_at),
    }


def serialize_attachment(item: Attachment) -> dict:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "job_id": item.job_id,
        "round_id": item.round_id,
        "kind": item.kind,
        "filename": item.filename,
        "path": item.path,
        "content_type": item.content_type,
        "size_bytes": item.size_bytes,
        "created_at": item.created_at.isoformat(),
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
        "lease_id": item.lease_id,
        "lease_expires_at": item.lease_expires_at.isoformat() if item.lease_expires_at else None,
        "message": item.message,
        "result": item.result,
        "error": item.error,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "claimed_at": item.claimed_at.isoformat() if item.claimed_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
    }


def _safe_path_part(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip())
    return text.strip(".-_")[:120] or "item"


def _safe_filename(value: str) -> str:
    name = Path(str(value or "attachment.bin")).name
    name = re.sub(r"[^a-zA-Z0-9._ -]+", "-", name).strip(". ")
    return name[:180] or "attachment.bin"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem or "attachment"
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}-{now_utc().strftime('%Y%m%d%H%M%S%f')}{suffix}")


def _create_worker_package_download_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "purpose": "worker_package_download",
        "exp": int(time.time()) + WORKER_PACKAGE_DOWNLOAD_TOKEN_TTL_SECONDS,
    }
    body = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign_worker_package_download_token(body)
    return f"v1.{body}.{signature}"


def _verify_worker_package_download_token(token: str) -> dict:
    try:
        version, body, signature = token.split(".", 2)
        if version != "v1" or not hmac.compare_digest(
            signature,
            _sign_worker_package_download_token(body),
        ):
            raise ValueError("Invalid token signature")
        payload = json.loads(_urlsafe_b64decode(body).decode("utf-8"))
        if payload.get("purpose") != "worker_package_download":
            raise ValueError("Invalid token purpose")
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("Token expired")
        if not str(payload.get("sub") or "").strip():
            raise ValueError("Missing token subject")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired download ticket") from exc


def _sign_worker_package_download_token(body: str) -> str:
    digest = hmac.new(settings.app_secret_key.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return _urlsafe_b64encode(digest)


def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _urlsafe_b64decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(f"{encoded}{padding}")


def _worker_package_path() -> Path | None:
    candidates: list[Path] = []
    if settings.worker_package_path:
        configured = Path(settings.worker_package_path)
        candidates.append(configured)
        if not configured.is_absolute():
            candidates.append(settings.repo_root / configured)
    attachment_root = Path(settings.attachment_root)
    candidates.extend(
        [
            attachment_root / "worker-packages" / "agentops-worker-windows.zip",
            settings.repo_root / attachment_root / "worker-packages" / "agentops-worker-windows.zip",
            settings.repo_root / "storage" / "worker-packages" / "agentops-worker-windows.zip",
            settings.repo_root / "apps" / "worker-windows" / "dist" / "agentops-worker-windows.zip",
            settings.repo_root / "apps" / "worker-windows" / "dist" / "agentops-worker-windows" / "agentops-worker-windows.zip",
        ]
    )
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path.is_file():
            return path
    return None
