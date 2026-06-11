import hashlib
import re
from pathlib import PurePosixPath, PureWindowsPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Job, Project, TaskRound, User, WorkerCommand
from app.db.repositories.jobs import add_log
from app.db.repositories.workers import create_worker_command, get_worker_by_worker_id
from app.services.github.repository import build_project_remote_url
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

    project_context = ensure_round_project_context(db, job, round_, worker_settings, settings.get("github", {}))
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
                "force_open_workspace": True,
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
        extra={
            "worker_id": command.worker_id,
            "command_id": command.id,
            "prompt_chars": len(round_.prompt),
            "project_name": project_context["project_name"],
            "workspace_path": project_context["workspace_path"],
        },
    )
    return command


def ensure_round_project_context(
    db: Session,
    job: Job,
    round_: TaskRound,
    worker_settings: dict,
    github_settings: dict | None = None,
) -> dict[str, str]:
    existing = db.get(Project, round_.project_id) if round_.project_id else None
    if not existing:
        existing = db.scalar(
            select(Project)
            .where(Project.job_id == job.id, Project.status == "active")
            .order_by(Project.created_at.desc())
            .limit(1)
        )
    if existing:
        round_.project_id = existing.id
        context = _project_context_from_record(existing, worker_settings, github_settings or {})
        if not existing.workspace_path and context["workspace_path"]:
            existing.workspace_path = context["workspace_path"]
        return context

    project_count = int(db.scalar(select(func.count(Project.id)).where(Project.job_id == job.id)) or 0)
    project_name = _project_name(job, project_count=project_count)
    workspace_root = str(worker_settings.get("trae_workspace_path") or worker_settings.get("workspace_root") or "").strip()
    workspace_path = _join_workspace_path(workspace_root, project_name) if workspace_root else project_name
    project = Project(
        job_id=job.id,
        name=project_name,
        direction=_first_direction(job),
        workspace_path=workspace_path,
        status="active",
    )
    db.add(project)
    db.flush()
    round_.project_id = project.id
    context = _project_context_from_record(project, worker_settings, github_settings or {})
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage="project_workspace_prepared",
        message="Per-job project workspace prepared before worker dispatch.",
        extra=context,
    )
    return context


def _project_context_from_record(project: Project, worker_settings: dict, github_settings: dict) -> dict[str, str]:
    workspace_root = str(worker_settings.get("trae_workspace_path") or worker_settings.get("workspace_root") or "").strip()
    workspace_path = str(project.workspace_path or "").strip()
    if not workspace_path:
        workspace_path = _join_workspace_path(workspace_root, project.name) if workspace_root else project.name
    return {
        "project_id": project.id,
        "project_name": project.name,
        "workspace_root": workspace_root,
        "workspace_path": workspace_path,
        "github_remote_url": build_project_remote_url(github_settings, project.name),
    }


def _project_name(job: Job, project_count: int = 0) -> str:
    direction = _first_direction(job)
    base = _slugify(direction)
    digest = hashlib.sha1(f"{job.id}:{direction}".encode("utf-8", errors="ignore")).hexdigest()[:8]
    if not base or base == "project":
        base = "agentops-project"
    suffix = "" if project_count <= 0 else f"-p{project_count + 1}"
    if base.endswith(digest):
        return f"{base[: max(1, 80 - len(suffix))]}{suffix}"
    return f"{base[: max(1, 70 - len(suffix))].strip('-')}-{digest}{suffix}"


def _slugify(value: str) -> str:
    text = str(value or "").strip().lower()
    replacements = {
        "agentops": "agentops",
        "自动作业": "auto-job",
        "自动化": "automation",
        "平台": "platform",
        "控制台": "console",
        "工作台": "workspace",
        "看板": "dashboard",
        "仓储": "warehouse",
        "物流": "logistics",
        "快递": "express",
        "物业": "property",
        "社区": "community",
        "监控": "monitor",
        "审批": "approval",
        "权限": "permission",
        "规则": "rules",
        "角色": "roles",
        "飞书": "feishu",
        "任务": "task",
        "项目": "project",
        "管理": "admin",
        "系统": "system",
    }
    pieces: list[str] = []
    for key, replacement in replacements.items():
        if key in text and replacement not in pieces:
            pieces.append(replacement)
    ascii_text = re.sub(r"[^a-z0-9._-]+", "-", text)
    ascii_text = re.sub(r"-{2,}", "-", ascii_text).strip(".-_")
    if ascii_text:
        pieces.insert(0, ascii_text)
    slug = "-".join(pieces)
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip(".-_")
    return slug[:80] or "project"


def _join_workspace_path(root: str, child: str) -> str:
    root = str(root or "").strip()
    child = str(child or "").strip().strip("/\\")
    if not root:
        return child
    if "\\" in root or re.match(r"^[A-Za-z]:", root):
        return str(PureWindowsPath(root) / child)
    return str(PurePosixPath(root) / child)


def _first_direction(job: Job) -> str:
    if isinstance(job.directions, list) and job.directions:
        return str(job.directions[0]).strip()
    return ""


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
