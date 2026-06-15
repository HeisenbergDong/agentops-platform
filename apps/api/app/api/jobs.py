from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import Attachment, AutomationError, Job, Project, RuntimeLog, TaskRound, User, WorkerCommand
from app.db.models.base import now_utc
from app.db.repositories.jobs import (
    add_log,
    cancel_job_worker_commands,
    cleanup_user_runtime_state,
    create_job,
    current_active_job,
    current_job,
    latest_round,
    list_logs,
    reset_job_for_reopen,
)
from app.db.repositories.rules import active_rule_version
from app.db.repositories.workers import create_worker_command, expire_worker_command_leases, get_worker_by_worker_id
from app.db.session import SessionLocal, get_db
from app.services.orchestrator.states import JobState
from app.services.orchestrator.directions import DEFAULT_DAILY_TARGET, normalize_job_directions
from app.services.orchestrator.intent import resolve_job_intent
from app.services.orchestrator.prompt_writer import (
    PromptGenerationError,
    generate_round_prompt,
    mark_prompt_generation_failed,
)
from app.services.preflight import build_preflight
from app.services.user_settings import load_user_settings
from app.services.orchestrator.worker_dispatch import (
    WorkerDispatchError,
    dispatch_prompt_to_worker,
    ensure_round_project_context,
    mark_worker_dispatch_failed,
)
from app.worker_gateway.contracts import CreateWorkerCommandRequest, WorkerCommandType

router = APIRouter()

RETRYABLE_COMMAND_STATES = {"failed", "manual_required", "cancelled"}
ACTIVE_WORKER_COMMAND_STATES = {"queued", "claimed", "running"}
RETRY_STAGE_BY_COMMAND_TYPE = {
    WorkerCommandType.SEND_PROMPT.value: JobState.SENDING_TO_WORKER,
    WorkerCommandType.WAIT_COMPLETION.value: JobState.WAITING_TRAE,
    WorkerCommandType.CLICK_CONTINUE.value: JobState.AWAITING_CONTINUE,
    WorkerCommandType.COPY_LATEST_REPLY.value: JobState.COLLECTING_TRACE,
    WorkerCommandType.CAPTURE_SCREENSHOT.value: JobState.SCREENSHOT_CAPTURING,
    WorkerCommandType.SCAN_PROJECT.value: JobState.PRODUCT_REVIEWING,
    WorkerCommandType.RUN_COMMAND.value: JobState.PRODUCT_REVIEWING,
    WorkerCommandType.BROWSER_ACCEPTANCE.value: JobState.BROWSER_ACCEPTING,
    WorkerCommandType.GIT_SUBMIT.value: JobState.GITHUB_SUBMITTING,
}


class StartJobRequest(BaseModel):
    directions: list[str]
    run_mode: str = "normal"


@router.post("/start")
def start_job(payload: StartJobRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    raw_directions = [item.strip() for item in payload.directions if item.strip()]
    scope_text = "\n".join(raw_directions)
    directions = normalize_job_directions(raw_directions, daily_target=DEFAULT_DAILY_TARGET)
    if not directions:
        raise HTTPException(status_code=400, detail="At least one direction is required")
    preflight = build_preflight(db, user)
    if not preflight["ready"]:
        raise HTTPException(
            status_code=400,
            detail={"message": preflight["summary"], "preflight": preflight},
        )
    cleanup = cleanup_user_runtime_state(db, user.id)
    rule_version = active_rule_version(db)
    run_mode = "test" if payload.run_mode == "test" else "normal"
    intent = resolve_job_intent(db, user, scope_text=scope_text, directions=directions, run_mode=run_mode)
    job = create_job(
        db,
        user_id=user.id,
        directions=directions,
        rule_version_id=rule_version.id if rule_version else None,
        scope_text=scope_text,
        intent=intent,
    )
    round_ = latest_round(db, job.id)
    preflight_level = "warning" if preflight["warnings"] else "info"
    preflight_message = (
        "Preflight checks passed with warnings."
        if preflight["warnings"]
        else "Preflight checks passed for this job."
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.CLEANING_OLD_RUNTIME,
        message="Start requested; old runtime logs, attachments, errors, and pending worker commands were cleaned.",
        extra=cleanup,
    )
    add_log(
        db,
        job_id=job.id,
        stage="preflight",
        message=preflight_message,
        level=preflight_level,
        extra=preflight,
    )
    if directions != raw_directions:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage="direction_queue",
            message="Job directions were normalized and expanded for the 100-round target.",
            extra={
                "raw_count": len(raw_directions),
                "normalized_count": len(directions),
                "daily_target": DEFAULT_DAILY_TARGET,
                "directions": directions,
            },
        )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.LOADING_RULES,
        message="User roles and rule files are ready for orchestration.",
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.GENERATING_PROMPT,
        message="Prompt generation is the next scheduler step.",
    )
    job.status = JobState.GENERATING_PROMPT
    if round_:
        round_.status = JobState.GENERATING_PROMPT
        try:
            generate_round_prompt(db, user, job, round_)
        except PromptGenerationError as exc:
            mark_prompt_generation_failed(db, job, round_, str(exc))
        if job.status == JobState.PROMPT_READY:
            try:
                dispatch_prompt_to_worker(db, user, job, round_)
            except WorkerDispatchError as exc:
                mark_worker_dispatch_failed(db, job, round_, str(exc))
    db.commit()
    db.refresh(job)
    return serialize_job(db, job)


@router.post("/reopen")
def reopen_job(
    payload: StartJobRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    raw_directions = [item.strip() for item in payload.directions if item.strip()]
    scope_text = "\n".join(raw_directions)
    directions = normalize_job_directions(raw_directions, daily_target=DEFAULT_DAILY_TARGET)
    if not directions:
        raise HTTPException(status_code=400, detail="At least one direction is required")
    preflight = build_preflight(db, user)
    if not preflight["ready"]:
        raise HTTPException(
            status_code=400,
            detail={"message": preflight["summary"], "preflight": preflight},
        )
    expire_worker_command_leases(db)
    job = current_job(db, user.id)
    if not job:
        return {"status": "no_job", "message": "No existing job to reopen."}
    old_runtime_context = current_job_runtime_context(db, job)

    rule_version = active_rule_version(db)
    run_mode = "test" if payload.run_mode == "test" else "normal"
    intent = resolve_job_intent(db, user, scope_text=scope_text, directions=directions, run_mode=run_mode)
    round_, reset = reset_job_for_reopen(
        db,
        job,
        directions=directions,
        rule_version_id=rule_version.id if rule_version else None,
        scope_text=scope_text,
        intent=intent,
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.CLEANING_OLD_RUNTIME,
        message="Reopen requested; current job rounds, counters, runtime logs, attachments, errors, and queued commands were reset.",
        extra={**reset, "raw_directions": raw_directions},
    )
    if directions != raw_directions:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="direction_queue",
            message="Reopened job directions were normalized and expanded for the 100-round target.",
            extra={
                "raw_count": len(raw_directions),
                "normalized_count": len(directions),
                "daily_target": DEFAULT_DAILY_TARGET,
                "directions": directions,
            },
        )
    if reset["cancelled_active_commands"]:
        command = enqueue_stop_worker_command(
            db,
            user.id,
            job.id,
            None,
            reason="user_reopen",
            runtime_context=old_runtime_context,
        )
        if command:
            add_log(
                db,
                job_id=job.id,
                round_id=round_.id,
                stage="worker_stop_command",
                message="Stop command queued for the previous local Worker activity before reopen continues.",
                level="warning",
                extra={"worker_id": command.worker_id, "command_id": command.id},
            )
        else:
            add_log(
                db,
                job_id=job.id,
                round_id=round_.id,
                stage="worker_stop_command",
                message="Previous active worker commands were cancelled, but no bound online worker could receive an explicit stop command.",
                level="warning",
                extra={"cancelled_active_commands": reset["cancelled_active_commands"]},
            )
    preflight_level = "warning" if preflight["warnings"] else "info"
    preflight_message = (
        "Preflight checks passed with warnings."
        if preflight["warnings"]
        else "Preflight checks passed for reopened job."
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage="preflight",
        message=preflight_message,
        level=preflight_level,
        extra=preflight,
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.LOADING_RULES,
        message="User roles and rule files are ready for reopened orchestration.",
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.GENERATING_PROMPT,
        message="Prompt generation will continue in the background from round 1 with the latest directions.",
    )
    db.commit()
    db.refresh(job)
    if background_tasks is not None:
        background_tasks.add_task(generate_and_dispatch_reopened_round, user.id, job.id, round_.id)
        result = serialize_job(db, job)
        result["message"] = "Reopen reset complete; prompt generation is running in the background."
        return result

    generate_and_dispatch_reopened_round_in_session(db, user.id, job.id, round_.id)
    db.refresh(job)
    return serialize_job(db, job)


def generate_and_dispatch_reopened_round(user_id: str, job_id: str, round_id: str) -> None:
    with SessionLocal() as db:
        generate_and_dispatch_reopened_round_in_session(db, user_id, job_id, round_id)


def generate_and_dispatch_reopened_round_in_session(
    db: Session,
    user_id: str,
    job_id: str,
    round_id: str,
) -> None:
    user = db.get(User, user_id)
    job = db.get(Job, job_id)
    round_ = db.get(TaskRound, round_id)
    if not user or not job or not round_:
        return
    if job.status in {JobState.STOPPED, JobState.PROJECT_COMPLETED}:
        return
    try:
        generate_round_prompt(db, user, job, round_)
    except PromptGenerationError as exc:
        mark_prompt_generation_failed(db, job, round_, str(exc))
    if job.status == JobState.PROMPT_READY:
        try:
            dispatch_prompt_to_worker(db, user, job, round_)
        except WorkerDispatchError as exc:
            mark_worker_dispatch_failed(db, job, round_, str(exc))
    db.commit()


@router.post("/continue")
def continue_job(
    job_id: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    job = current_active_job(db, user.id)
    if not job:
        return {"status": "no_job", "message": "No existing job to continue."}
    round_ = latest_round(db, job.id)
    if job.status == JobState.STOPPED:
        return {"status": "no_job", "message": "Stopped jobs cannot be continued; start a new job."}
    if job.status == JobState.PAUSED:
        active_command = latest_active_worker_command(db, job.id, round_.id if round_ else None)
        if active_command:
            raise HTTPException(
                status_code=400,
                detail=f"Worker command is still active: {active_command.command_type} / {active_command.status}",
            )
        previous = latest_resumable_worker_command(db, job.id, round_.id if round_ else None)
        if previous:
            stop_command = latest_stop_worker_command(db, job.id, round_.id if round_ else None)
            if _stop_requires_resume_prompt(stop_command):
                _queue_resume_after_trae_stop(db, user, job, round_, previous, stop_command)
                db.commit()
                db.refresh(job)
                return serialize_job(db, job)
            _resume_worker_command(db, user, job, round_, previous)
            db.commit()
            db.refresh(job)
            return serialize_job(db, job)
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.LOADING_RULES,
        message="Continue requested; existing state preserved.",
    )
    job.status = JobState.LOADING_RULES
    if round_:
        round_.status = JobState.LOADING_RULES
        if round_.prompt:
            job.status = JobState.PROMPT_READY
            round_.status = JobState.PROMPT_READY
            add_log(
                db,
                job_id=job.id,
                round_id=round_.id,
                stage=JobState.PROMPT_READY,
                message="Existing prompt preserved and ready for worker dispatch.",
                extra={"prompt_chars": len(round_.prompt)},
            )
            try:
                dispatch_prompt_to_worker(db, user, job, round_)
            except WorkerDispatchError as exc:
                mark_worker_dispatch_failed(db, job, round_, str(exc))
        else:
            try:
                generate_round_prompt(db, user, job, round_)
            except PromptGenerationError as exc:
                mark_prompt_generation_failed(db, job, round_, str(exc))
            if job.status == JobState.PROMPT_READY:
                try:
                    dispatch_prompt_to_worker(db, user, job, round_)
                except WorkerDispatchError as exc:
                    mark_worker_dispatch_failed(db, job, round_, str(exc))
    db.commit()
    db.refresh(job)
    return serialize_job(db, job)


@router.post("/retry-worker-command")
def retry_worker_command(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    expire_worker_command_leases(db)
    job = current_active_job(db, user.id)
    if not job:
        return {"status": "no_job", "message": "No existing job to retry."}
    round_ = latest_round(db, job.id)
    round_id = round_.id if round_ else None
    active_command = latest_active_worker_command(db, job.id, round_id)
    if active_command:
        raise HTTPException(
            status_code=400,
            detail=f"Worker command is still active: {active_command.command_type} / {active_command.status}",
        )
    previous = latest_worker_command(db, job.id, round_id)
    if not previous:
        raise HTTPException(status_code=400, detail="No worker command is available to retry.")
    if previous.user_id != user.id:
        raise HTTPException(status_code=403, detail="Worker command does not belong to current user.")
    if previous.status not in RETRYABLE_COMMAND_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"Latest worker command is not retryable: {previous.command_type} / {previous.status}",
        )
    if previous.command_type not in RETRY_STAGE_BY_COMMAND_TYPE:
        raise HTTPException(status_code=400, detail=f"Worker command cannot be retried: {previous.command_type}")

    preflight = build_preflight(db, user)
    if not preflight["ready"]:
        raise HTTPException(
            status_code=400,
            detail={"message": preflight["summary"], "preflight": preflight},
        )

    settings = load_user_settings(db, user.id)
    worker_settings = settings.get("worker", {})
    worker_id = str(worker_settings.get("worker_id") or "").strip()
    worker = get_worker_by_worker_id(db, worker_id)
    if not worker or worker.user_id != user.id:
        raise HTTPException(status_code=400, detail="Configured worker is not available for retry.")

    retry_payload = refreshed_retry_payload(previous.payload, worker_settings, previous.id)
    command = create_worker_command(
        db,
        worker_id=worker.worker_id,
        user_id=user.id,
        payload=CreateWorkerCommandRequest(
            type=WorkerCommandType(previous.command_type),
            job_id=job.id,
            round_id=round_id,
            payload=retry_payload,
        ),
    )
    retry_stage = RETRY_STAGE_BY_COMMAND_TYPE[previous.command_type]
    job.status = retry_stage
    if round_:
        round_.status = retry_stage
        if previous.command_type == WorkerCommandType.GIT_SUBMIT.value:
            round_.github_status = "submitting"
    add_log(
        db,
        job_id=job.id,
        round_id=round_id,
        stage="worker_command_retry",
        message="Retry worker command queued for the current failed stage.",
        level="warning",
        extra={
            "previous_command_id": previous.id,
            "new_command_id": command.id,
            "command_type": previous.command_type,
            "worker_id": worker.worker_id,
        },
    )
    db.commit()
    db.refresh(job)
    return serialize_job(db, job)


@router.post("/stop")
def stop_job(
    job_id: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    job = current_active_job(db, user.id)
    if not job:
        return {"status": "no_job", "message": "No existing job to stop."}
    previous_job_status = str(job.status)
    job.status = JobState.PAUSED
    round_ = latest_round(db, job.id)
    previous_round_status = str(round_.status) if round_ else ""
    if round_:
        round_.status = JobState.PAUSED
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.PAUSED,
        message="Pause requested; scheduler and worker should stop current activity, and the job can be continued later.",
        extra={
            "previous_job_status": previous_job_status,
            "previous_round_status": previous_round_status,
        },
    )
    cancelled_commands = cancel_job_worker_commands(db, job.id)
    if cancelled_commands:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage="worker_commands_cancelled",
            message="Active worker commands for this job were cancelled before stop propagation.",
            level="warning",
            extra={"cancelled_commands": cancelled_commands},
        )
    command = enqueue_stop_worker_command(
        db,
        user.id,
        job.id,
        round_.id if round_ else None,
        runtime_context=current_job_runtime_context(db, job, round_),
    )
    if command:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage="worker_stop_command",
            message="Stop command queued for bound worker.",
            extra={"worker_id": command.worker_id, "command_id": command.id},
        )
    else:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage="worker_stop_command",
            message="No bound worker found; scheduler state stopped only.",
            level="warning",
        )
    db.commit()
    db.refresh(job)
    return serialize_job(db, job)


@router.get("/current")
def get_current_job(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    expire_worker_command_leases(db)
    job = current_job(db, user.id)
    if not job:
        return {"status": "idle", "job": None, "logs": []}
    return serialize_job(db, job)


@router.get("")
def list_jobs(limit: int = 20, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(Job)
        .where(Job.user_id == user.id)
        .order_by(Job.created_at.desc())
        .limit(max(1, min(limit, 100)))
    ).all()
    return [serialize_job_summary(db, item) for item in rows]


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


def serialize_job_summary(db: Session, job: Job) -> dict:
    round_ = latest_round(db, job.id)
    latest_command = latest_worker_command(db, job.id, round_.id if round_ else None)
    error_count = db.scalar(select(func.count(AutomationError.id)).where(AutomationError.job_id == job.id))
    return {
        "id": job.id,
        "status": job.status,
        "scope_text": job.scope_text,
        "directions": job.directions,
        "daily_target": job.daily_target,
        "submitted_count": job.submitted_count,
        "satisfied_count": job.satisfied_count,
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
        "worker_command": serialize_worker_command(latest_command),
        "error_count": int(error_count or 0),
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def serialize_job(db: Session, job) -> dict:
    round_ = latest_round(db, job.id)
    logs = list_logs(db, job_id=job.id, limit=50)
    round_id = round_.id if round_ else None
    return {
        "status": job.status,
        "job": {
            "id": job.id,
            "status": job.status,
            "scope_text": job.scope_text,
            "directions": job.directions,
            "intent": job.intent or {},
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
            "prompt": round_.prompt,
            "trae_session_id": round_.trae_session_id,
            "trae_user_message_id": round_.trae_user_message_id,
            "trae_task_id": round_.trae_task_id,
            "trae_trace_id": round_.trae_trace_id,
            "trace_status": round_.trace_status,
            "github_status": round_.github_status,
            "feishu_status": round_.feishu_status,
        }
        if round_
        else None,
        "worker_command": serialize_worker_command(latest_worker_command(db, job.id, round_id)),
        "attachments": [serialize_attachment(item) for item in list_job_attachments(db, job.id, round_id)],
        "latest_dissatisfaction": serialize_optional_log(latest_dissatisfaction_reason(db, job.id, round_id)),
        "logs": [serialize_log(item) for item in logs],
    }


def serialize_log(item) -> dict:
    return {
        "id": item.id,
        "level": item.level,
        "stage": item.stage,
        "message": item.message,
        "display_message": item.display_message or item.message,
        "zh_message": item.display_message or item.message,
        "extra": item.extra,
        "created_at": item.created_at.isoformat(),
    }


def serialize_optional_log(item) -> dict | None:
    return serialize_log(item) if item else None


def serialize_attachment(item: Attachment) -> dict:
    return {
        "id": item.id,
        "kind": item.kind,
        "filename": item.filename,
        "path": item.path,
        "content_type": item.content_type,
        "size_bytes": item.size_bytes,
        "created_at": item.created_at.isoformat(),
    }


def list_job_attachments(db: Session, job_id: str, round_id: str | None) -> list[Attachment]:
    query = select(Attachment).where(Attachment.job_id == job_id)
    if round_id:
        query = query.where(Attachment.round_id == round_id)
    query = query.order_by(Attachment.created_at.desc()).limit(20)
    return list(db.scalars(query).all())


def latest_dissatisfaction_reason(db: Session, job_id: str, round_id: str | None) -> RuntimeLog | None:
    query = (
        select(RuntimeLog)
        .where(RuntimeLog.job_id == job_id, RuntimeLog.stage == "dissatisfaction_reason")
        .order_by(RuntimeLog.created_at.desc())
        .limit(1)
    )
    if round_id:
        query = query.where(RuntimeLog.round_id == round_id)
    return db.scalar(query)


def latest_worker_command(db: Session, job_id: str, round_id: str | None):
    query = select(WorkerCommand).where(WorkerCommand.job_id == job_id)
    if round_id:
        query = query.where(WorkerCommand.round_id == round_id)
    return db.scalar(query.order_by(WorkerCommand.created_at.desc()).limit(1))


def latest_active_worker_command(db: Session, job_id: str, round_id: str | None) -> WorkerCommand | None:
    query = select(WorkerCommand).where(
        WorkerCommand.job_id == job_id,
        WorkerCommand.status.in_(ACTIVE_WORKER_COMMAND_STATES),
    )
    if round_id:
        query = query.where(WorkerCommand.round_id == round_id)
    return db.scalar(query.order_by(WorkerCommand.created_at.desc()).limit(1))


def latest_resumable_worker_command(db: Session, job_id: str, round_id: str | None) -> WorkerCommand | None:
    query = select(WorkerCommand).where(
        WorkerCommand.job_id == job_id,
        WorkerCommand.status.in_(RETRYABLE_COMMAND_STATES),
        WorkerCommand.command_type.in_(set(RETRY_STAGE_BY_COMMAND_TYPE.keys())),
    )
    if round_id:
        query = query.where(WorkerCommand.round_id == round_id)
    return db.scalar(query.order_by(WorkerCommand.created_at.desc()).limit(1))


def latest_stop_worker_command(db: Session, job_id: str, round_id: str | None) -> WorkerCommand | None:
    query = select(WorkerCommand).where(
        WorkerCommand.job_id == job_id,
        WorkerCommand.command_type == WorkerCommandType.STOP_CURRENT_TASK.value,
    )
    if round_id:
        query = query.where(WorkerCommand.round_id == round_id)
    return db.scalar(query.order_by(WorkerCommand.created_at.desc()).limit(1))


def _stop_requires_resume_prompt(command: WorkerCommand | None) -> bool:
    if not command or command.status != "completed":
        return False
    result = command.result if isinstance(command.result, dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    report = data.get("stop_report") if isinstance(data.get("stop_report"), dict) else {}
    return bool(report.get("requires_resume_prompt") or report.get("trae_stop_clicked"))


def _queue_resume_after_trae_stop(
    db: Session,
    user: User,
    job: Job,
    round_: TaskRound | None,
    previous: WorkerCommand,
    stop_command: WorkerCommand | None,
) -> WorkerCommand:
    if not round_:
        raise HTTPException(status_code=400, detail="No round is available for continue.")
    settings = load_user_settings(db, user.id)
    worker_settings = settings.get("worker", {})
    worker_id = str(worker_settings.get("worker_id") or "").strip()
    worker = get_worker_by_worker_id(db, worker_id)
    if not worker or worker.user_id != user.id:
        raise HTTPException(status_code=400, detail="Configured worker is not available for continue.")
    project_context = ensure_round_project_context(db, job, round_, worker_settings, settings.get("github", {}))
    prompt = _resume_after_stop_prompt(job)
    command = create_worker_command(
        db,
        worker_id=worker.worker_id,
        user_id=user.id,
        payload=CreateWorkerCommandRequest(
            type=WorkerCommandType.SEND_PROMPT,
            job_id=job.id,
            round_id=round_.id,
            payload={
                "prompt": prompt,
                "trae_workspace_path": project_context["workspace_path"],
                "workspace_path": project_context["workspace_path"],
                "workspace_root": project_context["workspace_root"],
                "project_name": project_context["project_name"],
                "project_slug": project_context["project_name"],
                "browser_url": worker_settings.get("browser_url", ""),
                "job_id": job.id,
                "round_id": round_.id,
                "round_index": round_.round_index,
                "directions": job.directions,
                "github_remote_url": project_context.get("github_remote_url", ""),
                "github_repo_name": project_context["project_name"],
                "github_branch": str(worker_settings.get("github_branch") or "main"),
                "verify_submission": True,
                "strict_submission_verification": True,
                "submission_timeout_seconds": 30,
                "resume_after_stop": True,
                "retry_of_command_id": previous.id,
                "stop_command_id": stop_command.id if stop_command else "",
            },
        ),
    )
    job.status = JobState.SENDING_TO_WORKER
    round_.status = JobState.SENDING_TO_WORKER
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage="resume_after_trae_stop",
        message="Continue requested after Worker stopped Trae generation; a resume prompt was queued first.",
        level="warning",
        extra={
            "previous_command_id": previous.id,
            "stop_command_id": stop_command.id if stop_command else "",
            "new_command_id": command.id,
            "worker_id": worker.worker_id,
            "prompt_preview": prompt[:160],
        },
    )
    return command


def _resume_after_stop_prompt(job: Job) -> str:
    intent = job.intent if isinstance(job.intent, dict) else {}
    if intent.get("run_mode") == "test":
        return (
            "继续刚才被暂停的测试任务，从中断处往下完成。不要重建项目，保留已有文件和结构；"
            "本轮重点是快速完成可复查的最小结果，方便平台继续验证日志轨迹、GitHub 提交和飞书写入。"
            "不要主动执行耗时自测、长时间构建或完整浏览器验收，完成后简短说明即可。"
        )
    return (
        "继续刚才被暂停的任务，从中断处往下完成。不要重建项目，保留已有文件和结构；"
        "补完剩余工作后简短说明完成内容、关键文件和最小验证情况。"
    )


def _resume_worker_command(
    db: Session,
    user: User,
    job: Job,
    round_: TaskRound | None,
    previous: WorkerCommand,
) -> WorkerCommand:
    settings = load_user_settings(db, user.id)
    worker_settings = settings.get("worker", {})
    worker_id = str(worker_settings.get("worker_id") or "").strip()
    worker = get_worker_by_worker_id(db, worker_id)
    if not worker or worker.user_id != user.id:
        raise HTTPException(status_code=400, detail="Configured worker is not available for continue.")
    retry_payload = refreshed_retry_payload(previous.payload, worker_settings, previous.id)
    command = create_worker_command(
        db,
        worker_id=worker.worker_id,
        user_id=user.id,
        payload=CreateWorkerCommandRequest(
            type=WorkerCommandType(previous.command_type),
            job_id=job.id,
            round_id=round_.id if round_ else previous.round_id,
            payload=retry_payload,
        ),
    )
    retry_stage = RETRY_STAGE_BY_COMMAND_TYPE[previous.command_type]
    job.status = retry_stage
    if round_:
        round_.status = retry_stage
        if previous.command_type == WorkerCommandType.GIT_SUBMIT.value:
            round_.github_status = "submitting"
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else previous.round_id,
        stage="worker_command_retry",
        message="Continue requested after pause; worker command was requeued for the paused stage.",
        level="warning",
        extra={
            "previous_command_id": previous.id,
            "new_command_id": command.id,
            "command_type": previous.command_type,
            "worker_id": worker.worker_id,
        },
    )
    return command


def serialize_worker_command(item) -> dict | None:
    if not item:
        return None
    return {
        "command_id": item.id,
        "worker_id": item.worker_id,
        "type": item.command_type,
        "status": item.status,
        "attempts": item.attempts,
        "lease_id": item.lease_id,
        "lease_expires_at": item.lease_expires_at.isoformat() if item.lease_expires_at else None,
        "message": item.message,
        "error": item.error,
        "payload": _safe_command_dict(item.payload),
        "result": _safe_command_dict(item.result),
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "claimed_at": item.claimed_at.isoformat() if item.claimed_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
    }


def _safe_command_dict(value: dict | None) -> dict:
    if not isinstance(value, dict):
        return {}
    result: dict = {}
    for key, item in value.items():
        if key == "raw_text":
            result["raw_text_chars"] = len(str(item or ""))
        elif isinstance(item, str):
            result[key] = item[:2000]
        elif isinstance(item, dict):
            result[key] = _safe_command_dict(item)
        elif isinstance(item, list):
            result[key] = item[:50]
        else:
            result[key] = item
    return result


def refreshed_retry_payload(payload: dict | None, worker_settings: dict, previous_command_id: str) -> dict:
    result = dict(payload or {})
    result["retry_of_command_id"] = previous_command_id
    result["retry_requested_at"] = now_utc().isoformat()
    workspace_path = worker_settings.get("trae_workspace_path")
    if workspace_path:
        result["trae_workspace_path"] = workspace_path
        result["workspace_path"] = workspace_path
    browser_url = worker_settings.get("browser_url")
    if browser_url:
        result["browser_url"] = browser_url
        if "url" in result:
            result["url"] = browser_url
    return result


def enqueue_stop_worker_command(
    db: Session,
    user_id: str,
    job_id: str,
    round_id: str | None,
    reason: str = "user_stop",
    runtime_context: dict | None = None,
):
    settings = load_user_settings(db, user_id)
    worker_id = settings.get("worker", {}).get("worker_id")
    if not worker_id:
        return None
    worker = get_worker_by_worker_id(db, worker_id)
    if not worker or worker.user_id != user_id:
        return None
    if is_worker_offline(worker):
        return None
    return create_worker_command(
        db,
        worker_id=worker.worker_id,
        user_id=user_id,
        payload=CreateWorkerCommandRequest(
            type=WorkerCommandType.STOP_CURRENT_TASK,
            job_id=job_id,
            round_id=round_id,
            payload={"reason": reason, **(runtime_context or {})},
        ),
    )


def current_job_runtime_context(db: Session, job: Job, round_: TaskRound | None = None) -> dict[str, str]:
    active_round = round_ or latest_round(db, job.id)
    project = db.get(Project, active_round.project_id) if active_round and active_round.project_id else None
    if not project:
        project = db.scalar(
            select(Project)
            .where(Project.job_id == job.id)
            .order_by(Project.created_at.desc())
            .limit(1)
        )
    if not project:
        return {}
    result: dict[str, str] = {"project_name": project.name}
    if project.workspace_path:
        result["workspace_path"] = project.workspace_path
        result["trae_workspace_path"] = project.workspace_path
    return result


def is_worker_offline(worker) -> bool:
    if worker.revoked_at or not worker.last_seen_at:
        return True
    current = now_utc()
    last_seen = worker.last_seen_at
    if last_seen.tzinfo is None:
        current = current.replace(tzinfo=None)
    return current - last_seen > timedelta(minutes=2)
