from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Attachment, Job, RuntimeLog, TaskRound, WorkerCommand
from app.db.repositories.jobs import add_log
from app.db.repositories.workers import create_worker_command
from app.services.feishu.writer import FeishuWriteError, write_feishu_record
from app.services.orchestrator.dissatisfaction import (
    DissatisfactionEvidence,
    generate_dissatisfaction_reason,
)
from app.services.orchestrator.states import JobState
from app.services.trace.validator import is_recoverable_trace_reason, validate_full_trace
from app.services.user_settings import load_user_settings, save_user_settings
from app.worker_gateway.contracts import CreateWorkerCommandRequest, WorkerCommandType, WorkerResult


def handle_worker_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    if command.command_type == WorkerCommandType.SEND_PROMPT.value:
        _handle_send_prompt_result(db, command, result)
        db.commit()
        return
    if command.command_type == WorkerCommandType.WAIT_COMPLETION.value:
        _handle_wait_completion_result(db, command, result)
        db.commit()
        return
    if command.command_type == WorkerCommandType.COPY_LATEST_REPLY.value:
        _handle_copy_latest_reply_result(db, command, result)
        db.commit()
        return
    if command.command_type == WorkerCommandType.CAPTURE_SCREENSHOT.value:
        _handle_capture_screenshot_result(db, command, result)
        db.commit()
        return
    if command.command_type == WorkerCommandType.SCAN_PROJECT.value:
        _handle_scan_project_result(db, command, result)
        db.commit()
        return
    if command.command_type == WorkerCommandType.RUN_COMMAND.value:
        _handle_run_command_result(db, command, result)
        db.commit()
        return
    if command.command_type == WorkerCommandType.BROWSER_ACCEPTANCE.value:
        _handle_browser_acceptance_result(db, command, result)
        db.commit()
        return
    if command.command_type == WorkerCommandType.GIT_SUBMIT.value:
        _handle_git_submit_result(db, command, result)
        db.commit()
        return
    if command.command_type == WorkerCommandType.CLICK_CONTINUE.value:
        _handle_click_continue_result(db, command, result)
        db.commit()
        return
    if command.command_type == WorkerCommandType.STOP_CURRENT_TASK.value:
        _handle_stop_result(db, command, result)
        db.commit()


def _handle_send_prompt_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return

    extra = _result_extra(command, result)
    if result.status in {"ok", "success", "completed"}:
        job.status = JobState.WAITING_TRAE
        if round_:
            round_.status = JobState.WAITING_TRAE
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.PROMPT_SENT,
            message="Worker confirmed the prompt was sent to Trae.",
            extra=extra,
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.WAITING_TRAE,
            message="Waiting for Trae completion before trace collection.",
            extra={"worker_id": command.worker_id, "command_id": command.id},
        )
        wait_command = _enqueue_worker_command(
            db,
            command,
            WorkerCommandType.WAIT_COMPLETION,
            {
                "timeout_seconds": command.payload.get("wait_timeout_seconds", 900),
                "stable_seconds": command.payload.get("stable_seconds", 15),
                "poll_interval_seconds": command.payload.get("poll_interval_seconds", 2),
            },
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.WAITING_TRAE,
            message="wait_completion worker command queued.",
            extra={"worker_id": wait_command.worker_id, "command_id": wait_command.id},
        )
        return

    _mark_manual_required(
        db,
        job,
        round_,
        "Worker could not send the prompt automatically; manual intervention is required.",
        result.status,
        extra,
    )


def _handle_wait_completion_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return

    extra = _result_extra(command, result)
    if result.status in {"ok", "success", "completed"}:
        job.status = JobState.COLLECTING_TRACE
        if round_:
            round_.status = JobState.COLLECTING_TRACE
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.COLLECTING_TRACE,
            message="Trae output appears stable; collecting the full assistant trace.",
            extra=extra,
        )
        copy_command = _enqueue_worker_command(
            db,
            command,
            WorkerCommandType.COPY_LATEST_REPLY,
            {"timeout_seconds": command.payload.get("copy_timeout_seconds", 10)},
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.COLLECTING_TRACE,
            message="copy_latest_reply worker command queued.",
            extra={"worker_id": copy_command.worker_id, "command_id": copy_command.id},
        )
        return

    _queue_continue_recovery(
        db,
        command,
        job,
        round_,
        "Worker could not confirm Trae completion; asking Trae to continue before collecting trace again.",
        extra,
    )


def _handle_copy_latest_reply_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return

    extra = _result_extra(command, result)
    if result.status not in {"ok", "success", "completed"}:
        _queue_continue_recovery(
            db,
            command,
            job,
            round_,
            "Worker could not copy a complete Trae assistant trace; asking Trae to continue before retrying.",
            extra,
        )
        return

    raw_trace = str(result.data.get("raw_text") or "")
    validation = validate_full_trace(raw_trace)
    trace_extra = {
        **extra,
        "trace_chars": len(raw_trace),
        "validation": validation,
    }
    if round_:
        round_.trace_status = "valid" if validation["valid"] else validation["reason"]

    if not validation["valid"] and is_recoverable_trace_reason(validation["reason"]):
        _queue_continue_recovery(
            db,
            command,
            job,
            round_,
            f"Trae trace is not complete yet ({validation['reason']}); continuing Trae before retrying trace collection.",
            trace_extra,
        )
        return

    if validation["valid"]:
        trace_attachment = _record_trace_attachment(db, command, raw_trace)
        job.status = JobState.SCREENSHOT_CAPTURING
        if round_:
            round_.status = JobState.SCREENSHOT_CAPTURING
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.TRACE_VALIDATING,
            message="Full Trae assistant trace validated.",
            extra={**trace_extra, "attachment_id": trace_attachment.id},
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.SCREENSHOT_CAPTURING,
            message="Trace gate passed; screenshot capture is the next scheduler step.",
            extra={"worker_id": command.worker_id, "command_id": command.id},
        )
        screenshot_command = _enqueue_worker_command(
            db,
            command,
            WorkerCommandType.CAPTURE_SCREENSHOT,
            {},
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.SCREENSHOT_CAPTURING,
            message="capture_screenshot worker command queued.",
            extra={"worker_id": screenshot_command.worker_id, "command_id": screenshot_command.id},
        )
        return

    job.status = JobState.TRACE_MISSING_ABORT
    if round_:
        round_.status = JobState.TRACE_MISSING_ABORT
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.TRACE_MISSING_ABORT,
        message=f"Trae trace validation failed: {validation['reason']}.",
        level="error",
        extra=trace_extra,
    )


def _handle_capture_screenshot_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return
    if not _ensure_trace_gate(db, job, round_, command):
        return

    extra = _result_extra(command, result)
    data_status = str(result.data.get("status") or "")
    screenshot_path = str(result.data.get("path") or "")
    if result.status not in {"ok", "success", "completed"} or data_status != "captured" or not screenshot_path:
        _mark_manual_required(
            db,
            job,
            round_,
            "Worker could not capture the Trae screenshot automatically; manual intervention is required.",
            result.status,
            extra,
        )
        return

    attachment = _record_screenshot_attachment(db, command, result)
    job.status = JobState.PRODUCT_REVIEWING
    if round_:
        round_.status = JobState.PRODUCT_REVIEWING
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.SCREENSHOT_CAPTURING,
        message="Worker screenshot captured and recorded as an attachment.",
        extra={**extra, "attachment_id": attachment.id},
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.PRODUCT_REVIEWING,
        message="Screenshot gate passed; product review is the next scheduler step.",
        extra={"attachment_id": attachment.id, "worker_id": command.worker_id, "command_id": command.id},
    )
    scan_command = _enqueue_worker_command(
        db,
        command,
        WorkerCommandType.SCAN_PROJECT,
        {},
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.PRODUCT_REVIEWING,
        message="scan_project worker command queued for product review evidence.",
        extra={"worker_id": scan_command.worker_id, "command_id": scan_command.id},
    )


def _handle_click_continue_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return

    extra = _result_extra(command, result)
    if result.status not in {"ok", "success", "completed"}:
        _mark_manual_required(
            db,
            job,
            round_,
            "Worker could not safely continue Trae automatically; manual intervention is required.",
            result.status,
            extra,
        )
        return

    job.status = JobState.WAITING_TRAE
    if round_:
        round_.status = JobState.WAITING_TRAE
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.AWAITING_CONTINUE,
        message="Worker clicked Trae continue; waiting for the assistant reply to finish.",
        extra=extra,
    )
    wait_command = _enqueue_worker_command(
        db,
        command,
        WorkerCommandType.WAIT_COMPLETION,
        {
            "timeout_seconds": command.payload.get("wait_timeout_seconds", 900),
            "stable_seconds": command.payload.get("stable_seconds", 15),
            "poll_interval_seconds": command.payload.get("poll_interval_seconds", 2),
            "continue_attempts": command.payload.get("continue_attempts", 0),
            "max_continue_attempts": command.payload.get("max_continue_attempts", 20),
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.WAITING_TRAE,
        message="wait_completion worker command queued after continuing Trae.",
        extra={"worker_id": wait_command.worker_id, "command_id": wait_command.id},
    )


def _record_screenshot_attachment(db: Session, command: WorkerCommand, result: WorkerResult) -> Attachment:
    path = str(result.data.get("path") or "")
    filename = str(result.data.get("filename") or Path(path).name or "screenshot.png")
    size_bytes = int(result.data.get("size_bytes") or 0)
    attachment = Attachment(
        user_id=command.user_id,
        job_id=command.job_id,
        round_id=command.round_id,
        kind="screenshot",
        filename=filename,
        path=path,
        content_type=str(result.data.get("content_type") or "image/png"),
        size_bytes=size_bytes,
    )
    db.add(attachment)
    db.flush()
    return attachment


def _record_trace_attachment(db: Session, command: WorkerCommand, raw_trace: str) -> Attachment:
    filename = f"trae-trace-{command.job_id or 'job'}-{command.round_id or 'round'}.txt"
    out_dir = settings.attachment_root / "traces"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(raw_trace, encoding="utf-8")
    attachment = Attachment(
        user_id=command.user_id,
        job_id=command.job_id,
        round_id=command.round_id,
        kind="trace",
        filename=filename,
        path=str(path),
        content_type="text/plain; charset=utf-8",
        size_bytes=len(raw_trace.encode("utf-8")),
    )
    db.add(attachment)
    db.flush()
    return attachment


def _handle_scan_project_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return
    if not _ensure_trace_gate(db, job, round_, command):
        return

    extra = _result_extra(command, result)
    if result.status not in {"ok", "success", "completed"} or result.data.get("status") != "scanned":
        _mark_manual_required(
            db,
            job,
            round_,
            "Worker could not scan the project for product review evidence; manual intervention is required.",
            result.status,
            extra,
        )
        return

    job.status = JobState.PRODUCT_REVIEWING
    if round_:
        round_.status = JobState.PRODUCT_REVIEWING
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.PRODUCT_REVIEWING,
        message="Project scan completed for product review.",
        extra=extra,
    )

    recommended_commands = _recommended_commands(result.data)
    if recommended_commands:
        review_command = _enqueue_worker_command(
            db,
            command,
            WorkerCommandType.RUN_COMMAND,
            {
                "command": recommended_commands[0],
                "cwd": _workspace_path(command, result.data),
                "timeout": command.payload.get("review_timeout_seconds", 180),
                "purpose": "product_review",
            },
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.PRODUCT_REVIEWING,
            message="run_command worker command queued for product review evidence.",
            extra={"worker_id": review_command.worker_id, "command_id": review_command.id, "command": recommended_commands[0]},
        )
        return

    _advance_to_browser_accepting(
        db,
        command,
        job,
        round_,
        "Product review found no automated build/test command; continuing with scan evidence only.",
        level="warning",
        extra=extra,
    )


def _handle_run_command_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return
    if not _ensure_trace_gate(db, job, round_, command):
        return

    extra = _result_extra(command, result)
    returncode = result.data.get("returncode")
    if result.status in {"ok", "success", "completed"} and returncode == 0:
        _advance_to_browser_accepting(
            db,
            command,
            job,
            round_,
            "Product review build/test command passed.",
            extra=extra,
        )
        return

    _record_dissatisfaction(
        db,
        job,
        round_,
        command,
        result,
        JobState.PRODUCT_REVIEWING,
        "Product review build/test command failed; manual review or dissatisfaction reason generation is required.",
        extra,
    )
    _mark_manual_required(
        db,
        job,
        round_,
        "Product review build/test command failed; dissatisfaction reason was generated from evidence.",
        result.status,
        extra,
    )


def _handle_browser_acceptance_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return
    if not _ensure_trace_gate(db, job, round_, command):
        return

    extra = _result_extra(command, result)
    data_status = str(result.data.get("status") or "")
    if result.status in {"ok", "success", "completed"} and data_status == "passed":
        job.status = JobState.GITHUB_SUBMITTING
        if round_:
            round_.status = JobState.GITHUB_SUBMITTING
            round_.github_status = "submitting"
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.BROWSER_ACCEPTING,
            message="Browser acceptance passed with local page evidence.",
            extra=extra,
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.GITHUB_SUBMITTING,
            message="Browser acceptance gate passed; git_submit worker command queued.",
            extra={"worker_id": command.worker_id, "command_id": command.id},
        )
        git_command = _enqueue_worker_command(
            db,
            command,
            WorkerCommandType.GIT_SUBMIT,
            {
                "commit_message": _commit_message(job, round_),
                "push": command.payload.get("github_push", True),
                "remote": command.payload.get("github_remote", "origin"),
                "branch": command.payload.get("github_branch", ""),
                "timeout": command.payload.get("github_timeout_seconds", 120),
            },
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.GITHUB_SUBMITTING,
            message="git_submit worker command queued.",
            extra={"worker_id": git_command.worker_id, "command_id": git_command.id},
        )
        return

    message = _browser_acceptance_failure_message(result, data_status)
    _record_dissatisfaction(db, job, round_, command, result, JobState.BROWSER_ACCEPTING, message, extra)
    _mark_manual_required(db, job, round_, message, result.status, extra)


def _handle_git_submit_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return
    if not _ensure_trace_gate(db, job, round_, command):
        return

    extra = _result_extra(command, result)
    data_status = str(result.data.get("status") or "")
    if result.status in {"ok", "success", "completed"} and data_status in {"committed", "pushed", "nothing_to_commit"}:
        job.status = JobState.FEISHU_PREPARING
        if round_:
            round_.status = JobState.FEISHU_PREPARING
            round_.github_status = data_status
        if data_status in {"committed", "pushed"}:
            job.submitted_count += 1
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.GITHUB_SUBMITTING,
            message="Git submission step completed.",
            extra=extra,
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.FEISHU_PREPARING,
            message="GitHub submission gate passed; Feishu preparation is the next scheduler step.",
            extra={"worker_id": command.worker_id, "command_id": command.id, "git_status": data_status},
        )
        _write_feishu_record(db, job, round_, command, result)
        return

    _record_dissatisfaction(
        db,
        job,
        round_,
        command,
        result,
        JobState.GITHUB_SUBMITTING,
        "Git submission failed; stopping before Feishu write.",
        extra,
    )
    job.status = JobState.GITHUB_FAILED_ABORT
    if round_:
        round_.status = JobState.GITHUB_FAILED_ABORT
        round_.github_status = data_status or result.status or "failed"
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.GITHUB_FAILED_ABORT,
        message="Git submission failed; stopping before Feishu write.",
        level="error",
        extra=extra,
    )


def _write_feishu_record(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    command: WorkerCommand,
    git_result: WorkerResult,
) -> None:
    if not round_:
        _mark_feishu_failed(db, job, round_, "Cannot write Feishu record without a task round.", {})
        return

    fields = _prepare_feishu_fields(db, job, round_, command, git_result)
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.FEISHU_PREPARING,
        message="Feishu record payload prepared.",
        extra={"field_names": list(fields.keys()), "field_count": len(fields)},
    )
    configs = load_user_settings(db, job.user_id)
    feishu_config = dict(configs.get("feishu", {}))
    if command.payload.get("feishu_app_token") and not feishu_config.get("app_token"):
        feishu_config["app_token"] = command.payload["feishu_app_token"]
    if command.payload.get("feishu_table_id") and not feishu_config.get("table_id"):
        feishu_config["table_id"] = command.payload["feishu_table_id"]
    if command.payload.get("feishu_view_id") and not feishu_config.get("view_id"):
        feishu_config["view_id"] = command.payload["feishu_view_id"]

    job.status = JobState.FEISHU_WRITING
    round_.status = JobState.FEISHU_WRITING
    round_.feishu_status = "writing"
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.FEISHU_WRITING,
        message="Writing Feishu business record.",
        extra={"app_token_configured": bool(feishu_config.get("app_token")), "table_id_configured": bool(feishu_config.get("table_id"))},
    )
    try:
        write_result = write_feishu_record(feishu_config, fields)
    except FeishuWriteError as exc:
        _mark_feishu_failed(db, job, round_, str(exc), {"fields": fields})
        return
    except Exception as exc:
        _mark_feishu_failed(db, job, round_, f"Feishu write failed: {exc}", {"fields": fields})
        return

    token_cache = write_result.pop("token_cache", None)
    if token_cache:
        feishu_config["token_cache"] = token_cache
        save_user_settings(db, job.user_id, {"feishu": feishu_config}, allow_internal=True)

    round_.feishu_status = "written"
    round_.status = JobState.ROUND_COMPLETED
    job.status = JobState.PROJECT_COMPLETED
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.FEISHU_WRITING,
        message="Feishu business record written.",
        extra=write_result,
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.PROJECT_COMPLETED,
        message="Round completed and project marked completed.",
        extra={"round_index": round_.round_index, "feishu_status": round_.feishu_status},
    )


def _mark_feishu_failed(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    message: str,
    extra: dict,
) -> None:
    _record_dissatisfaction_from_context(db, job, round_, JobState.FEISHU_FAILED_ABORT, message, extra)
    job.status = JobState.FEISHU_FAILED_ABORT
    if round_:
        round_.status = JobState.FEISHU_FAILED_ABORT
        round_.feishu_status = "failed"
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.FEISHU_FAILED_ABORT,
        message=message,
        level="error",
        extra=extra,
    )


def _record_dissatisfaction(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    command: WorkerCommand,
    result: WorkerResult,
    failure_stage: str,
    failure_message: str,
    extra: dict,
) -> dict:
    if not round_:
        return {}
    evidence = _dissatisfaction_evidence(
        db,
        job,
        round_,
        failure_stage=failure_stage,
        failure_message=failure_message,
        command_type=command.command_type,
        result_status=result.status,
        data=result.data or extra.get("data") or {},
    )
    return _add_dissatisfaction_log(db, job, round_, evidence)


def _record_dissatisfaction_from_context(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    failure_stage: str,
    failure_message: str,
    extra: dict,
) -> dict:
    if not round_:
        return {}
    data = extra.get("data") if isinstance(extra.get("data"), dict) else extra
    evidence = _dissatisfaction_evidence(
        db,
        job,
        round_,
        failure_stage=failure_stage,
        failure_message=failure_message,
        command_type=str(extra.get("command_type") or ""),
        result_status=str(extra.get("result_status") or ""),
        data=data,
    )
    return _add_dissatisfaction_log(db, job, round_, evidence)


def _dissatisfaction_evidence(
    db: Session,
    job: Job,
    round_: TaskRound,
    failure_stage: str,
    failure_message: str,
    command_type: str,
    result_status: str,
    data: dict,
) -> DissatisfactionEvidence:
    screenshot = _latest_attachment(db, job.id, round_.id, "screenshot")
    return DissatisfactionEvidence(
        failure_stage=str(failure_stage),
        failure_message=failure_message,
        command_type=command_type,
        result_status=result_status,
        prompt=round_.prompt or "",
        trace_text=_latest_trace_text(db, job.id, round_.id),
        screenshot_path=screenshot.path if screenshot else "",
        runtime_log_text=_runtime_log_text(db, job.id, round_.id),
        data=data,
    )


def _add_dissatisfaction_log(
    db: Session,
    job: Job,
    round_: TaskRound,
    evidence: DissatisfactionEvidence,
) -> dict:
    generated = generate_dissatisfaction_reason(evidence)
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage="dissatisfaction_reason",
        message="Dissatisfaction reason generated from completed trace and failure evidence.",
        level="warning",
        extra=generated,
    )
    return generated


def _advance_to_browser_accepting(
    db: Session,
    source_command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    message: str,
    level: str = "info",
    extra: dict | None = None,
) -> None:
    job.status = JobState.BROWSER_ACCEPTING
    if round_:
        round_.status = JobState.BROWSER_ACCEPTING
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.PRODUCT_REVIEWING,
        message=message,
        level=level,
        extra=extra or {},
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.BROWSER_ACCEPTING,
        message="Product review gate passed; browser acceptance worker command queued.",
        extra=extra or {},
    )
    acceptance_command = _enqueue_worker_command(
        db,
        source_command,
        WorkerCommandType.BROWSER_ACCEPTANCE,
        {
            "url": _browser_acceptance_url(source_command, extra or {}),
            "timeout_seconds": source_command.payload.get("browser_acceptance_timeout_seconds", 10),
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.BROWSER_ACCEPTING,
        message="browser_acceptance worker command queued.",
        extra={"worker_id": acceptance_command.worker_id, "command_id": acceptance_command.id},
    )


def _queue_continue_recovery(
    db: Session,
    source_command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    message: str,
    extra: dict,
) -> None:
    continue_attempts = int(source_command.payload.get("continue_attempts") or 0) + 1
    max_continue_attempts = int(source_command.payload.get("max_continue_attempts") or 20)
    if continue_attempts > max_continue_attempts:
        _mark_trace_missing_abort(
            db,
            job,
            round_,
            "Trae did not produce a complete assistant trace after repeated continue attempts; downstream review, GitHub, and Feishu writes were stopped.",
            {**extra, "continue_attempts": continue_attempts, "max_continue_attempts": max_continue_attempts},
        )
        return

    job.status = JobState.AWAITING_CONTINUE
    if round_:
        round_.status = JobState.AWAITING_CONTINUE
    recovery_extra = {**extra, "continue_attempts": continue_attempts, "max_continue_attempts": max_continue_attempts}
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.AWAITING_CONTINUE,
        message=message,
        level="warning",
        extra=recovery_extra,
    )
    continue_command = _enqueue_worker_command(
        db,
        source_command,
        WorkerCommandType.CLICK_CONTINUE,
        {
            "timeout_seconds": source_command.payload.get("continue_timeout_seconds", 10),
            "continue_attempts": continue_attempts,
            "max_continue_attempts": max_continue_attempts,
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.AWAITING_CONTINUE,
        message="click_continue worker command queued for incomplete Trae reply recovery.",
        extra={"worker_id": continue_command.worker_id, "command_id": continue_command.id},
    )


def _ensure_trace_gate(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    command: WorkerCommand,
) -> bool:
    if not round_:
        _mark_trace_missing_abort(
            db,
            job,
            round_,
            "Cannot continue downstream automation without a task round and verified Trae trace.",
            {"command_id": command.id, "command_type": command.command_type},
        )
        return False
    trace_text = _latest_trace_text(db, job.id, round_.id)
    if round_.trace_status == "valid" and trace_text.strip():
        return True
    _mark_trace_missing_abort(
        db,
        job,
        round_,
        "Verified Trae assistant trace is missing; downstream review, GitHub, and Feishu writes were stopped.",
        {
            "command_id": command.id,
            "command_type": command.command_type,
            "trace_status": round_.trace_status,
            "trace_chars": len(trace_text),
        },
    )
    return False


def _mark_trace_missing_abort(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    message: str,
    extra: dict,
) -> None:
    job.status = JobState.TRACE_MISSING_ABORT
    if round_:
        round_.status = JobState.TRACE_MISSING_ABORT
        if round_.trace_status == "valid":
            round_.trace_status = "trace_attachment_missing"
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.TRACE_MISSING_ABORT,
        message=message,
        level="error",
        extra=extra,
    )


def _mark_manual_required(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    message: str,
    result_status: str,
    extra: dict,
) -> None:
    job.status = JobState.MANUAL_REQUIRED
    if round_:
        round_.status = JobState.MANUAL_REQUIRED
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.MANUAL_REQUIRED,
        message=message,
        level="warning" if result_status == "manual_required" else "error",
        extra=extra,
    )


def _handle_stop_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.STOPPED,
        message="Worker stop command finished.",
        level="info" if result.status in {"ok", "success", "completed"} else "warning",
        extra=_result_extra(command, result),
    )


def _load_job_round(db: Session, command: WorkerCommand) -> tuple[Job | None, TaskRound | None]:
    job = db.get(Job, command.job_id) if command.job_id else None
    round_ = db.get(TaskRound, command.round_id) if command.round_id else None
    return job, round_


def _enqueue_worker_command(
    db: Session,
    source_command: WorkerCommand,
    command_type: WorkerCommandType,
    payload: dict,
) -> WorkerCommand:
    payload = _merge_context(source_command, payload)
    return create_worker_command(
        db,
        worker_id=source_command.worker_id,
        user_id=source_command.user_id,
        payload=CreateWorkerCommandRequest(
            type=command_type,
            job_id=source_command.job_id,
            round_id=source_command.round_id,
            payload=payload,
        ),
    )


def _merge_context(source_command: WorkerCommand, payload: dict) -> dict:
    result = dict(payload)
    for key in (
        "trae_workspace_path",
        "workspace_path",
        "job_id",
        "round_id",
        "round_index",
        "directions",
        "url",
        "browser_url",
        "acceptance_url",
        "github_push",
        "github_remote",
        "github_branch",
    ):
        if key in source_command.payload and key not in result:
            result[key] = source_command.payload[key]
    for key in (
        "continue_attempts",
        "max_continue_attempts",
        "wait_timeout_seconds",
        "stable_seconds",
        "poll_interval_seconds",
        "copy_timeout_seconds",
        "continue_timeout_seconds",
        "browser_acceptance_timeout_seconds",
        "github_timeout_seconds",
    ):
        if key in source_command.payload and key not in result:
            result[key] = source_command.payload[key]
    return result


def _recommended_commands(data: dict) -> list[list[str]]:
    commands = data.get("recommended_commands")
    if not isinstance(commands, list):
        return []
    result: list[list[str]] = []
    for command in commands:
        if isinstance(command, list) and all(isinstance(item, str) for item in command):
            result.append(command)
    return result


def _browser_acceptance_url(command: WorkerCommand, extra: dict) -> str:
    for key in ("url", "browser_url", "acceptance_url"):
        value = command.payload.get(key)
        if value:
            return str(value)
    data = extra.get("data") if isinstance(extra, dict) else None
    if isinstance(data, dict):
        for key in ("url", "browser_url", "acceptance_url", "requested_url"):
            value = data.get(key)
            if value:
                return str(value)
    return ""


def _browser_acceptance_failure_message(result: WorkerResult, data_status: str) -> str:
    if result.status not in {"ok", "success", "completed"}:
        return "Browser acceptance worker command failed; manual review is required before submission."
    if data_status == "no_browser_evidence":
        return "No browser acceptance URL was available; manual review is required before submission."
    if data_status == "unsupported_url":
        return "Browser acceptance URL is unsupported for automated local verification; manual review is required."
    return "Browser acceptance did not pass with usable local page evidence; manual review is required before submission."


def _prepare_feishu_fields(
    db: Session,
    job: Job,
    round_: TaskRound,
    command: WorkerCommand,
    git_result: WorkerResult,
) -> dict[str, str]:
    trace_text = _latest_trace_text(db, job.id, round_.id)
    screenshot = _latest_attachment(db, job.id, round_.id, "screenshot")
    git_data = git_result.data or {}
    github_url = _github_url(git_data)
    branch_or_files = str(git_data.get("pushed_branch") or git_data.get("branch") or git_data.get("commit_sha") or "")
    log_trace = trace_text or _runtime_log_text(db, job.id, round_.id)
    if screenshot:
        branch_or_files = f"{branch_or_files}\nScreenshot: {screenshot.path}".strip()
    return {
        "Trae Session ID": _session_id(job, round_),
        "轮次": str(round_.round_index),
        "User Prompt": round_.prompt or "",
        "任务类型": "implementation",
        "业务领域": _first_direction(job),
        "修改范围": branch_or_files,
        "任务是否完成": "已完成任务",
        "产物及过程是否满意": "满意",
        "不满意原因": "",
        "github地址": github_url,
        "分支/文件夹": branch_or_files,
        "日志轨迹": log_trace,
    }


def _latest_trace_text(db: Session, job_id: str, round_id: str) -> str:
    attachment = _latest_attachment(db, job_id, round_id, "trace")
    if not attachment:
        return ""
    path = Path(attachment.path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")[-18000:]


def _latest_attachment(db: Session, job_id: str, round_id: str, kind: str) -> Attachment | None:
    return db.scalar(
        select(Attachment)
        .where(Attachment.job_id == job_id, Attachment.round_id == round_id, Attachment.kind == kind)
        .order_by(Attachment.created_at.desc())
        .limit(1)
    )


def _runtime_log_text(db: Session, job_id: str, round_id: str) -> str:
    rows = list(
        db.scalars(
            select(RuntimeLog)
            .where(RuntimeLog.job_id == job_id, RuntimeLog.round_id == round_id)
            .order_by(RuntimeLog.created_at)
            .limit(120)
        ).all()
    )
    return "\n".join(f"[{item.stage}] {item.message}" for item in rows)[-18000:]


def _github_url(git_data: dict) -> str:
    remote_url = str(git_data.get("remote_url") or "")
    commit_sha = str(git_data.get("commit_sha") or "")
    if remote_url and commit_sha and "://" in remote_url:
        cleaned = remote_url.removesuffix(".git")
        return f"{cleaned}/commit/{commit_sha}"
    return remote_url or commit_sha


def _session_id(job: Job, round_: TaskRound) -> str:
    return f"{job.id}-{round_.round_index}"


def _first_direction(job: Job) -> str:
    if isinstance(job.directions, list) and job.directions:
        return str(job.directions[0])
    return ""


def _commit_message(job: Job, round_: TaskRound | None) -> str:
    direction = ""
    if isinstance(job.directions, list) and job.directions:
        direction = str(job.directions[0]).strip()
    round_label = f"round {round_.round_index}" if round_ else "round"
    if direction:
        return f"AgentOps: {direction[:80]} ({round_label})"
    return f"AgentOps automated update ({round_label})"


def _workspace_path(command: WorkerCommand, data: dict) -> str:
    return str(
        command.payload.get("trae_workspace_path")
        or command.payload.get("workspace_path")
        or data.get("root")
        or ""
    )


def _result_extra(command: WorkerCommand, result: WorkerResult) -> dict:
    data = result.data
    if "raw_text" in data:
        data = {key: value for key, value in data.items() if key != "raw_text"}
        data["raw_text_chars"] = len(str(result.data.get("raw_text") or ""))
    return {
        "worker_id": command.worker_id,
        "command_id": command.id,
        "command_type": command.command_type,
        "command_status": command.status,
        "result_status": result.status,
        "message": result.message,
        "error": result.error or "",
        "data": data,
    }
