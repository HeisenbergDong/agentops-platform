from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, TaskRound, User, WorkerCommand
from app.db.repositories.jobs import add_log
from app.db.repositories.workers import create_worker_command, get_worker_by_worker_id
from app.services.orchestrator.states import JobState
from app.services.user_settings import load_user_settings
from app.worker_gateway.contracts import CreateWorkerCommandRequest, WorkerCommandType

ACTIVE_COMMAND_STATES = {"queued", "claimed", "running"}


class WorkerDispatchError(RuntimeError):
    pass


def dispatch_prompt_to_worker(db: Session, user: User, job: Job, round_: TaskRound) -> WorkerCommand:
    if not round_.prompt:
        raise WorkerDispatchError("Round prompt is empty")

    existing = find_active_send_prompt_command(db, job.id, round_.id)
    if existing:
        job.status = JobState.SENDING_TO_WORKER
        round_.status = JobState.SENDING_TO_WORKER
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage=JobState.SENDING_TO_WORKER,
            message="Existing send_prompt worker command is still active; dispatch not duplicated.",
            extra={"worker_id": existing.worker_id, "command_id": existing.id, "status": existing.status},
        )
        return existing

    settings = load_user_settings(db, user.id)
    worker_settings = settings.get("worker", {})
    worker_id = worker_settings.get("worker_id")
    if not worker_id:
        raise WorkerDispatchError("No worker is bound to current user")
    worker = get_worker_by_worker_id(db, worker_id)
    if not worker or worker.user_id != user.id:
        raise WorkerDispatchError("Configured worker is not available for current user")

    command = create_worker_command(
        db,
        worker_id=worker.worker_id,
        user_id=user.id,
        payload=CreateWorkerCommandRequest(
            type=WorkerCommandType.SEND_PROMPT,
            job_id=job.id,
            round_id=round_.id,
            payload={
                "prompt": round_.prompt,
                "trae_workspace_path": worker_settings.get("trae_workspace_path", ""),
                "job_id": job.id,
                "round_id": round_.id,
                "round_index": round_.round_index,
                "directions": job.directions,
            },
        ),
    )
    job.status = JobState.SENDING_TO_WORKER
    round_.status = JobState.SENDING_TO_WORKER
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.SENDING_TO_WORKER,
        message="send_prompt worker command queued.",
        extra={"worker_id": command.worker_id, "command_id": command.id, "prompt_chars": len(round_.prompt)},
    )
    return command


def mark_worker_dispatch_failed(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    message: str,
) -> None:
    job.status = JobState.MANUAL_REQUIRED
    if round_:
        round_.status = JobState.MANUAL_REQUIRED
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.MANUAL_REQUIRED,
        message=f"Worker dispatch requires manual action: {message}",
        level="warning",
    )


def find_active_send_prompt_command(
    db: Session,
    job_id: str,
    round_id: str,
) -> WorkerCommand | None:
    return db.scalar(
        select(WorkerCommand)
        .where(
            WorkerCommand.job_id == job_id,
            WorkerCommand.round_id == round_id,
            WorkerCommand.command_type == WorkerCommandType.SEND_PROMPT.value,
            WorkerCommand.status.in_(ACTIVE_COMMAND_STATES),
        )
        .order_by(WorkerCommand.created_at.desc())
        .limit(1)
    )
