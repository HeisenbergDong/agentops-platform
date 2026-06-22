from datetime import datetime, timezone
from pathlib import Path
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Attachment, AutomationError, Job, Project, RuntimeLog, TaskRound, User, WorkerCommand
from app.db.repositories.jobs import add_log
from app.db.repositories.workers import create_worker_command
from app.services.feishu.writer import FeishuWriteError, write_feishu_record
from app.services.github.repository import ensure_github_repository
from app.services.orchestrator.dissatisfaction import (
    DissatisfactionEvidence,
    generate_dissatisfaction_reason,
)
from app.services.orchestrator.prompt_writer import (
    PromptGenerationError,
    generate_round_prompt,
    mark_prompt_generation_failed,
)
from app.services.orchestrator.states import JobState
from app.services.trace.validator import is_recoverable_trace_reason, validate_full_trace
from app.services.user_settings import load_user_settings, save_user_settings
from app.services.webhook_notifier import WebhookNotifyError, notify_manual_required, notify_text
from app.services.orchestrator.worker_dispatch import (
    WorkerDispatchError,
    dispatch_prompt_to_worker,
    mark_worker_dispatch_failed,
)
from app.worker_gateway.contracts import CreateWorkerCommandRequest, WorkerCommandType, WorkerResult

FEISHU_ATTACHMENT_FIELD = "截图（userprompt附件/产物/运行结果/对话）"
LOG_TRACE_FIELD_SOFT_LIMIT = 45000
LOG_TRACE_OVERFLOW_TEXT = "因日志超长已经保存txt文档，放在截图列。"
MAX_ROUNDS_PER_PROJECT = 5
MAX_SATISFIED_RATIO = 0.20
TERMINAL_JOB_STATES = {JobState.STOPPED, JobState.PROJECT_COMPLETED}
TERMINAL_ROUND_STATES = {JobState.STOPPED, JobState.ROUND_COMPLETED, "first_round_discarded"}
PAUSED_STATES = {JobState.PAUSED}
IGNORED_RESULT_COMMAND_STATES = {"cancelled"}
RECOVERABLE_COPY_GATE_REASONS = {
    "awaiting_continuation",
    "awaiting_current_continuation",
    "manual_stopped",
    "service_interrupted",
    "no_completed_turn_after_prompt_send",
}
MANUAL_STOP_REASONS = {"manual_stopped", "generation_interrupted"}
TRACE_COPY_RETRY_REASONS = {
    "empty_trace",
    "trace_too_short",
    "missing_tool_trace_markers",
    "partial_code_copy",
    "final_summary_only",
    "copy_command_failed",
}
DEFAULT_MAX_TRACE_COPY_ATTEMPTS = 5
DEFAULT_MAX_SEND_PROMPT_VISUAL_RECOVERY_ATTEMPTS = 3
DEFAULT_MAX_TRACE_RECOVERY_DIAGNOSIS_ATTEMPTS = 2
DEFAULT_MAX_WAIT_RECOVERY_DIAGNOSIS_ATTEMPTS = 2
FIRST_ROUND_INTERVENTION_IDLE_SECONDS = 30
FOLLOWUP_ROUND_INTERVENTION_IDLE_SECONDS = 30
DEFAULT_MAX_WAIT_OBSERVATION_ATTEMPTS = 10
DEFAULT_TRAE_SLOW_NOTIFY_SECONDS = 30 * 60
TRACE_UNAVAILABLE_AFTER_COMPLETION = "trace_unavailable_after_completed"
INVALID_BUSINESS_TRACE_PATTERNS = (
    re.compile(r"^\s*Trae raw execution trace for session\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Trae structured execution trace collected locally\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*TEST MODE TRACE EXCEPTION\b", re.IGNORECASE | re.MULTILINE),
)
TRAE_SESSION_DISPLAY_RE = re.compile(
    r"^\.[0-9A-Za-z]+:"
    r"[0-9a-f]{8,64}_[0-9a-f]{8,64}\.[0-9a-f]{8,64}\.[0-9a-f]{8,64}"
    r":Trae CN\.T\([^)]+\)$"
)


def handle_worker_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    if result.status == "cancelled" and _stop_report_from_result(result):
        _handle_stop_result(db, command, result)
        db.commit()
        return
    if _should_ignore_worker_result(db, command, result):
        db.commit()
        return
    if command.command_type == WorkerCommandType.SEND_PROMPT.value:
        _handle_send_prompt_result(db, command, result)
        db.commit()
        return
    if command.command_type == WorkerCommandType.WAIT_COMPLETION.value:
        _handle_wait_completion_result(db, command, result)
        db.commit()
        return
    if command.command_type == WorkerCommandType.DIAGNOSE_UI.value:
        _handle_diagnose_ui_result(db, command, result)
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
    _maybe_notify_slow_trae(db, job, round_, command, result)
    if result.status in {"ok", "success", "completed"}:
        unconfirmed_reason = _prompt_submission_unconfirmed_reason(result.data)
        if unconfirmed_reason:
            if _queue_send_prompt_visual_diagnosis(
                db,
                command,
                job,
                round_,
                "Worker clicked/pasted in Trae but did not confirm that Trae received a new prompt; asking Worker to screenshot and diagnose before deciding.",
                {**extra, "unconfirmed_reason": unconfirmed_reason},
            ):
                return
            _mark_manual_required(
                db,
                job,
                round_,
                "Worker clicked/pasted in Trae but did not confirm that Trae received a new prompt; retry or manual intervention is required.",
                "prompt_submission_unconfirmed",
                {**extra, "unconfirmed_reason": unconfirmed_reason},
            )
            return
        if result.data.get("sent_at_epoch") and "sent_at_epoch" not in command.payload:
            command.payload = {**command.payload, "sent_at_epoch": result.data.get("sent_at_epoch")}
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
            _wait_completion_payload(
                command,
                round_,
                {
                    "sent_at_epoch": result.data.get("sent_at_epoch"),
                    "sent_at": result.data.get("sent_at"),
                },
            ),
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

    if _queue_send_prompt_visual_diagnosis(
        db,
        command,
        job,
        round_,
        "Worker could not send the prompt automatically; asking Worker to screenshot Trae and diagnose the composer before deciding.",
        extra,
    ):
        return

    _mark_manual_required(
        db,
        job,
        round_,
        "Worker could not send the prompt automatically; manual intervention is required.",
        result.status,
        extra,
    )


def _prompt_submission_unconfirmed_reason(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    submission = data.get("submission") if isinstance(data.get("submission"), dict) else {}
    if submission.get("status") == "unconfirmed":
        return str(submission.get("error") or "submission_unconfirmed")
    automation = data.get("automation") if isinstance(data.get("automation"), dict) else {}
    if automation.get("submission_verified") is False:
        return "submission_not_verified"
    return ""


def _queue_send_prompt_visual_diagnosis(
    db: Session,
    source_command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    message: str,
    extra: dict,
) -> bool:
    attempts = int(source_command.payload.get("send_prompt_visual_recovery_attempts") or 0) + 1
    max_attempts = int(
        source_command.payload.get("max_send_prompt_visual_recovery_attempts")
        or DEFAULT_MAX_SEND_PROMPT_VISUAL_RECOVERY_ATTEMPTS
    )
    if attempts > max_attempts:
        return False

    job.status = JobState.SENDING_TO_WORKER
    if round_:
        round_.status = JobState.SENDING_TO_WORKER
    recovery_reason = _send_prompt_recovery_reason(extra)
    retry_payload = {
        **source_command.payload,
        "open_new_task": False,
        "open_new_task_suppressed_reason": "send_prompt_visual_recovery",
        "send_prompt_visual_recovery_attempts": attempts,
        "max_send_prompt_visual_recovery_attempts": max_attempts,
        "retry_of_command_id": source_command.id,
        "verify_submission": True,
        "strict_submission_verification": True,
        "verify_visual_submission": True,
        "submission_timeout_seconds": max(30, int(source_command.payload.get("submission_timeout_seconds") or 30)),
    }
    result_data = extra.get("data") if isinstance(extra.get("data"), dict) else {}
    if result_data.get("sent_at_epoch") and not retry_payload.get("sent_at_epoch"):
        retry_payload["sent_at_epoch"] = result_data.get("sent_at_epoch")
    if result_data.get("sent_at") and not retry_payload.get("sent_at"):
        retry_payload["sent_at"] = result_data.get("sent_at")
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.SENDING_TO_WORKER,
        message=message,
        level="info",
        extra={
            **extra,
            "send_prompt_visual_recovery_attempts": attempts,
            "max_send_prompt_visual_recovery_attempts": max_attempts,
            "recovery_reason": recovery_reason,
            "display_message": (
                f"Worker 发送提示词未确认成功，调度先让 Worker 截图交给视觉诊断，"
                f"再决定重试或继续观察（第 {attempts}/{max_attempts} 次）。"
            ),
        },
    )
    diagnose_command = _enqueue_worker_command(
        db,
        source_command,
        WorkerCommandType.DIAGNOSE_UI,
        {
            "task": "find_prompt_input_and_send_button",
            "timeout_seconds": source_command.payload.get("diagnose_timeout_seconds", 10),
            "scroll_bottom": False,
            "previous_command_type": WorkerCommandType.SEND_PROMPT.value,
            "resume_previous_payload": retry_payload,
            "retry_of_command_id": source_command.id,
            "send_prompt_visual_recovery_attempts": attempts,
            "max_send_prompt_visual_recovery_attempts": max_attempts,
            "recovery_reason": recovery_reason,
            "use_ai_ui_analyst": source_command.payload.get("use_ai_ui_analyst", True),
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.SENDING_TO_WORKER,
        message="diagnose_ui worker command queued for send_prompt visual recovery.",
        extra={
            "worker_id": diagnose_command.worker_id,
            "command_id": diagnose_command.id,
            "recovery_reason": recovery_reason,
            "display_message": "已安排 Worker 截图诊断 Trae 输入框/发送按钮和提示词是否已经发出。",
        },
    )
    return True


def _handle_wait_completion_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return

    extra = _result_extra(command, result)
    _maybe_notify_slow_trae(db, job, round_, command, result)
    if result.status in {"ok", "success", "completed"}:
        _queue_trace_collection_after_wait(db, command, job, round_, extra, result.data)
        return

    if _wait_failure_can_collect_trace(extra) or _continuation_after_continue_can_collect_trace(command, extra):
        _queue_trace_collection_after_wait(
            db,
            command,
            job,
            round_,
            extra,
            extra.get("data") if isinstance(extra.get("data"), dict) else {},
            message="Trae appears complete after wait timeout; collecting trace before attempting any recovery click.",
        )
        return

    if _should_observe_wait_failure_without_recovery(extra):
        if _queue_wait_observation_retry(
            db,
            command,
            job,
            round_,
            "Worker did not find a safe Trae recovery action; continuing observation without clicking.",
            extra,
        ):
            return

    if _continue_action_was_sent(command, extra):
        if _queue_wait_observation_retry(
            db,
            command,
            job,
            round_,
            "Worker already sent a Trae continue recovery; observing the continuation result without sending another continue.",
            {**extra, "continue_action_sent": True},
        ):
            return

    _queue_continue_recovery(
        db,
        command,
        job,
        round_,
        "Worker has not confirmed the current Trae reply is complete; recovery will continue before trace collection.",
        extra,
    )


def _handle_diagnose_ui_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return

    extra = _result_extra(command, result)
    if result.status not in {"ok", "success", "completed"}:
        _mark_manual_required(
            db,
            job,
            round_,
            "Worker could not diagnose Trae current state after continue; manual inspection is required.",
            result.status,
            extra,
        )
        return

    state = str(result.data.get("state") or "").strip()
    output_probe = result.data.get("output_probe") if isinstance(result.data.get("output_probe"), dict) else {}
    suggested = result.data.get("suggested_intervention") if isinstance(result.data.get("suggested_intervention"), dict) else {}
    diagnostic_extra = {
        **extra,
        "diagnosis_state": state,
        "diagnosis_reason": str(result.data.get("reason") or ""),
        "output_probe": output_probe,
        "suggested_intervention": suggested,
        "resume_strategy": command.payload.get("resume_strategy", ""),
        "display_message": _diagnose_resume_display_message(state, result.data),
    }
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage="resume_state_diagnosed",
        message="Worker diagnosed Trae state after continue; scheduler is choosing the next step.",
        extra=diagnostic_extra,
    )
    if str(suggested.get("mode") or "") == "manual-required" or str(suggested.get("risk") or "") == "blocked":
        _mark_manual_required(
            db,
            job,
            round_,
            str(suggested.get("manual_message") or "Trae is waiting for a manual decision after continue."),
            state or "manual_required",
            diagnostic_extra,
        )
        return
    previous_command_type = str(command.payload.get("previous_command_type") or "").strip()
    if previous_command_type == WorkerCommandType.SEND_PROMPT.value:
        _handle_send_prompt_visual_diagnosis(db, command, job, round_, result.data, diagnostic_extra)
        return
    if previous_command_type and previous_command_type not in {
        WorkerCommandType.WAIT_COMPLETION.value,
        WorkerCommandType.CLICK_CONTINUE.value,
        WorkerCommandType.COPY_LATEST_REPLY.value,
        WorkerCommandType.SEND_PROMPT.value,
    }:
        _queue_resume_previous_worker_step(db, command, job, round_, previous_command_type, diagnostic_extra)
        return
    if state == "completed":
        _queue_trace_collection_after_wait(
            db,
            command,
            job,
            round_,
            diagnostic_extra,
            {"supervisor_decision": {"action": "collect_trace", "reason": "resume_diagnosis_completed"}},
            message="Continue diagnosis says Trae is complete; collecting the full assistant trace.",
        )
        return
    if suggested and not _diagnosis_suggests_continue_recovery(suggested):
        _queue_wait_observation_retry(
            db,
            command,
            job,
            round_,
            "Continue diagnosis found a safe non-continue Trae action; scheduler will re-observe so Worker can apply it in the wait loop.",
            {**diagnostic_extra, "intervention_idle_seconds": 1},
        )
        return
    if state in {"needs_scroll_inner_panel", "service_interrupted", "awaiting_continuation", "awaiting_terminal_input"} or suggested:
        _queue_continue_recovery(
            db,
            command,
            job,
            round_,
            "Continue diagnosis found a recoverable Trae UI state; scheduler will ask Worker to recover safely.",
            diagnostic_extra,
        )
        return
    if _diagnose_output_probe_can_collect_trace(output_probe):
        _queue_trace_collection_after_wait(
            db,
            command,
            job,
            round_,
            diagnostic_extra,
            {"supervisor_decision": {"action": "collect_trace", "reason": "resume_diagnosis_trace_probe_ok"}},
            message="Continue diagnosis found a complete-looking Trae reply; collecting trace before more recovery.",
        )
        return
    _queue_wait_observation_retry(
        db,
        command,
        job,
        round_,
        "Continue diagnosis did not prove completion yet; scheduler will observe Trae before taking more action.",
        diagnostic_extra,
    )


def _handle_send_prompt_visual_diagnosis(
    db: Session,
    command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    data: dict,
    diagnostic_extra: dict,
) -> None:
    if _visual_diagnosis_blocked(data):
        _mark_manual_required(
            db,
            job,
            round_,
            _visual_manual_message(data) or "Visual diagnosis found a blocked Trae state while sending the prompt.",
            "manual_required",
            diagnostic_extra,
        )
        return

    visual_state = _send_prompt_visual_state(data)
    if visual_state in {"completed"} or _diagnose_output_probe_can_collect_trace(diagnostic_extra.get("output_probe", {})):
        _queue_trace_collection_after_wait(
            db,
            command,
            job,
            round_,
            {**diagnostic_extra, "send_prompt_visual_state": visual_state},
            {"supervisor_decision": {"action": "collect_trace", "reason": "send_prompt_visual_diagnosis_completed"}},
            message="Visual diagnosis says Trae already processed the prompt; collecting the assistant trace.",
        )
        return
    if visual_state in {"prompt_submitted", "generating", "still_generating", "awaiting_action"}:
        job.status = JobState.WAITING_TRAE
        if round_:
            round_.status = JobState.WAITING_TRAE
        wait_extra = _send_prompt_wait_context_from_diagnosis(command)
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.WAITING_TRAE,
            message="Visual diagnosis indicates the prompt was submitted; scheduler will observe Trae instead of failing the run.",
            extra={
                **diagnostic_extra,
                "send_prompt_visual_state": visual_state,
                "display_message": "视觉诊断显示提示词已进入 Trae 或 Trae 正在处理，调度继续观察收口。",
            },
        )
        wait_command = _enqueue_worker_command(
            db,
            command,
            WorkerCommandType.WAIT_COMPLETION,
            _wait_completion_payload(command, round_, wait_extra),
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.WAITING_TRAE,
            message="wait_completion worker command queued after send_prompt visual diagnosis.",
            extra={"worker_id": wait_command.worker_id, "command_id": wait_command.id},
        )
        return

    resume_payload = command.payload.get("resume_previous_payload")
    if isinstance(resume_payload, dict) and resume_payload.get("prompt"):
        job.status = JobState.SENDING_TO_WORKER
        if round_:
            round_.status = JobState.SENDING_TO_WORKER
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.SENDING_TO_WORKER,
            message="Visual diagnosis did not confirm prompt submission; scheduler is retrying send_prompt with visual targeting enabled.",
            extra={
                **diagnostic_extra,
                "send_prompt_visual_state": visual_state,
                "display_message": "视觉诊断未确认提示词已发出，调度重试一次发送提示词并继续启用视觉定位。",
            },
        )
        next_command = _enqueue_worker_command(
            db,
            command,
            WorkerCommandType.SEND_PROMPT,
            _suppress_open_new_task_for_retry(resume_payload, "send_prompt_visual_recovery"),
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.SENDING_TO_WORKER,
            message="send_prompt worker command requeued after visual diagnosis.",
            extra={"worker_id": next_command.worker_id, "command_id": next_command.id},
        )
        return

    _mark_manual_required(
        db,
        job,
        round_,
        "Visual diagnosis could not decide how to recover prompt submission; manual inspection is required.",
        "manual_required",
        {**diagnostic_extra, "send_prompt_visual_state": visual_state},
    )


def _queue_resume_previous_worker_step(
    db: Session,
    source_command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    previous_command_type: str,
    extra: dict,
) -> None:
    try:
        command_type = WorkerCommandType(previous_command_type)
    except ValueError:
        _queue_wait_observation_retry(
            db,
            source_command,
            job,
            round_,
            "Continue diagnosis could not map the paused worker command; scheduler will observe Trae instead.",
            extra,
        )
        return
    payload = source_command.payload.get("resume_previous_payload")
    if not isinstance(payload, dict):
        payload = {}
    resume_payload = {
        **payload,
        "retry_of_command_id": source_command.payload.get("retry_of_command_id", ""),
        "resume_after_pause": True,
        "resume_diagnosis_state": extra.get("diagnosis_state", ""),
    }
    if command_type == WorkerCommandType.SEND_PROMPT:
        resume_payload = _suppress_open_new_task_for_retry(resume_payload, "resume_after_pause")
    stage = _resume_stage_for_worker_command(command_type)
    job.status = stage
    if round_:
        round_.status = stage
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=stage,
        message="Continue diagnosis completed; scheduler is resuming the paused worker step.",
        extra={
            **extra,
            "previous_command_type": previous_command_type,
            "display_message": "Trae 当前状态已诊断，调度会按暂停前的阶段继续执行下一步。",
        },
    )
    next_command = _enqueue_worker_command(db, source_command, command_type, resume_payload)
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=stage,
        message="Paused worker step requeued after continue diagnosis.",
        extra={
            "worker_id": next_command.worker_id,
            "command_id": next_command.id,
            "command_type": previous_command_type,
            "display_message": "已重新下发暂停前的 Worker 步骤。",
        },
    )


def _resume_stage_for_worker_command(command_type: WorkerCommandType) -> JobState:
    return {
        WorkerCommandType.CAPTURE_SCREENSHOT: JobState.SCREENSHOT_CAPTURING,
        WorkerCommandType.SCAN_PROJECT: JobState.PRODUCT_REVIEWING,
        WorkerCommandType.RUN_COMMAND: JobState.PRODUCT_REVIEWING,
        WorkerCommandType.BROWSER_ACCEPTANCE: JobState.BROWSER_ACCEPTING,
        WorkerCommandType.GIT_SUBMIT: JobState.GITHUB_SUBMITTING,
    }.get(command_type, JobState.WAITING_TRAE)


def _diagnose_output_probe_can_collect_trace(output_probe: dict) -> bool:
    if not isinstance(output_probe, dict):
        return False
    if output_probe.get("complete_like") is True:
        return True
    return str(output_probe.get("reason") or "") == "ok"


def _send_prompt_recovery_reason(extra: dict) -> str:
    data = extra.get("data") if isinstance(extra.get("data"), dict) else {}
    for source in (
        data,
        data.get("automation") if isinstance(data.get("automation"), dict) else {},
        data.get("submission") if isinstance(data.get("submission"), dict) else {},
        data.get("submission_probe") if isinstance(data.get("submission_probe"), dict) else {},
        extra,
    ):
        if isinstance(source, dict) and str(source.get("stage") or "").strip():
            return str(source.get("stage")).strip()
        if isinstance(source, dict) and str(source.get("reason") or "").strip():
            return str(source.get("reason")).strip()
        if isinstance(source, dict) and str(source.get("status") or "").strip() in {
            "unconfirmed",
            "failed",
            "manual_required",
        }:
            return str(source.get("status")).strip()
    error = str(extra.get("error") or "").strip()
    if "could not locate" in error.lower() or "prompt controls" in error.lower():
        return "prompt_controls_not_found"
    if error:
        return "send_prompt_failed"
    return "prompt_submission_unconfirmed"


def _suppress_open_new_task_for_retry(payload: dict, reason: str) -> dict:
    result = dict(payload)
    if result.get("open_new_task"):
        result["open_new_task_suppressed_reason"] = reason
    result["open_new_task"] = False
    return result


def _visual_diagnosis_blocked(data: dict) -> bool:
    suggested = data.get("suggested_intervention") if isinstance(data.get("suggested_intervention"), dict) else {}
    visual = data.get("visual") if isinstance(data.get("visual"), dict) else {}
    ai_analysis = visual.get("ai_analysis") if isinstance(visual.get("ai_analysis"), dict) else {}
    return (
        str(suggested.get("mode") or "") == "manual-required"
        or str(suggested.get("risk") or "") == "blocked"
        or str(ai_analysis.get("risk") or "") == "blocked"
        or (
            str(ai_analysis.get("recommended_action") or "") == "do_not_click"
            and str(ai_analysis.get("screen_state") or "") == "manual_required"
        )
    )


def _visual_manual_message(data: dict) -> str:
    suggested = data.get("suggested_intervention") if isinstance(data.get("suggested_intervention"), dict) else {}
    visual = data.get("visual") if isinstance(data.get("visual"), dict) else {}
    ai_analysis = visual.get("ai_analysis") if isinstance(visual.get("ai_analysis"), dict) else {}
    return str(
        suggested.get("manual_message")
        or ai_analysis.get("blocked_reason")
        or ai_analysis.get("reason")
        or ""
    )


def _send_prompt_visual_state(data: dict) -> str:
    state = str(data.get("state") or "").strip()
    if state == "completed":
        return "completed"
    visual = data.get("visual") if isinstance(data.get("visual"), dict) else {}
    status = str(visual.get("status") or "").strip()
    ai_analysis = visual.get("ai_analysis") if isinstance(visual.get("ai_analysis"), dict) else {}
    screen_state = str(ai_analysis.get("screen_state") or "").strip()
    recommended = str(ai_analysis.get("recommended_action") or "").strip()
    if status in {"completed", "prompt_submitted"}:
        return "prompt_submitted" if status == "prompt_submitted" else "completed"
    if screen_state in {"prompt_submitted", "generating", "still_generating"}:
        return screen_state
    if recommended in {"wait", "collect_trace_candidate"}:
        return "generating"
    if screen_state in {"prompt_still_in_composer", "prompt_not_submitted"}:
        return screen_state
    if status in {"prompt_ready", "found", "partial"}:
        return "prompt_ready"
    if state in {"service_interrupted", "awaiting_continuation", "awaiting_terminal_input", "needs_scroll_inner_panel"}:
        return "awaiting_action"
    return state or status or screen_state or "unknown"


def _send_prompt_wait_context_from_diagnosis(command: WorkerCommand) -> dict:
    resume_payload = command.payload.get("resume_previous_payload")
    source = resume_payload if isinstance(resume_payload, dict) else command.payload
    sent_at_epoch = source.get("sent_at_epoch") or source.get("prompt_sent_at_epoch")
    return {
        "sent_at_epoch": sent_at_epoch,
        "sent_at": source.get("sent_at") or source.get("prompt_sent_at") or "",
        "send_prompt_visual_recovery_attempts": source.get("send_prompt_visual_recovery_attempts", 0),
        "max_send_prompt_visual_recovery_attempts": source.get(
            "max_send_prompt_visual_recovery_attempts",
            DEFAULT_MAX_SEND_PROMPT_VISUAL_RECOVERY_ATTEMPTS,
        ),
    }


def _diagnose_resume_display_message(state: str, data: dict) -> str:
    if state == "completed":
        return "Worker 已截图并诊断 Trae 当前状态：看起来已经完成，调度会先获取回复轨迹。"
    suggested = data.get("suggested_intervention") if isinstance(data.get("suggested_intervention"), dict) else {}
    if str(suggested.get("mode") or "") == "manual-required" or str(suggested.get("risk") or "") == "blocked":
        return "Worker 已截图并诊断 Trae 当前状态：界面需要人工确认，调度已停止自动点击。"
    if suggested:
        return "Worker 已截图并诊断 Trae 当前状态：发现可恢复操作，调度会让 Worker 安全恢复后继续观察。"
    if state:
        return f"Worker 已截图并诊断 Trae 当前状态：{state}，调度会继续观察。"
    return "Worker 已截图并诊断 Trae 当前状态，调度会继续观察。"


def _queue_trace_collection_after_wait(
    db: Session,
    command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    extra: dict,
    data: dict | None,
    message: str = "Trae output appears stable; collecting the full assistant trace.",
) -> None:
    job.status = JobState.COLLECTING_TRACE
    if round_:
        round_.status = JobState.COLLECTING_TRACE
    wait_extra = _wait_completion_supervisor_extra(extra, data or {})
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.COLLECTING_TRACE,
        message=message,
        extra=wait_extra,
    )
    copy_command = _enqueue_worker_command(
        db,
        command,
        WorkerCommandType.COPY_LATEST_REPLY,
        {
            "timeout_seconds": command.payload.get("copy_timeout_seconds", 10),
            "prompt": round_.prompt if round_ and round_.prompt else command.payload.get("prompt", ""),
            "trace_copy_attempts": command.payload.get("trace_copy_attempts", 0),
            "max_trace_copy_attempts": command.payload.get("max_trace_copy_attempts", DEFAULT_MAX_TRACE_COPY_ATTEMPTS),
            "allow_local_trace_fallback": False,
            "completion_observation": wait_extra,
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.COLLECTING_TRACE,
        message="copy_latest_reply worker command queued.",
        extra={"worker_id": copy_command.worker_id, "command_id": copy_command.id},
    )


def _handle_copy_latest_reply_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return

    extra = _result_extra(command, result)
    if result.status not in {"ok", "success", "completed"}:
        if _queue_trace_recovery_diagnosis(
            db,
            command,
            job,
            round_,
            "Worker did not copy a complete Trae assistant trace; scheduler will screenshot and diagnose Trae before deciding recovery.",
            {**extra, "validation": {"valid": False, "reason": "copy_command_failed"}},
        ):
            return
        if _queue_trace_copy_retry(
            db,
            command,
            job,
            round_,
            "Worker did not copy a complete Trae assistant trace; retrying trace copy before recovery.",
            {**extra, "validation": {"valid": False, "reason": "copy_command_failed"}},
        ):
            return
        if _handle_completed_trace_unavailable(db, command, job, round_, {**extra, "validation": {"valid": False, "reason": "copy_command_failed"}}):
            return
        _queue_continue_recovery(
            db,
            command,
            job,
            round_,
            "Worker did not copy a complete Trae assistant trace; recovery will continue before retrying collection.",
            extra,
        )
        return

    raw_trace = str(result.data.get("raw_text") or "")
    worker_probe = result.data.get("trace_probe") if isinstance(result.data, dict) else None
    probe_reason = str(worker_probe.get("reason") or "") if isinstance(worker_probe, dict) else ""
    if probe_reason in MANUAL_STOP_REASONS:
        if round_:
            round_.trace_status = probe_reason
        _mark_manual_required(
            db,
            job,
            round_,
            "Trae generation appears to have been manually stopped; manual inspection is required before retrying this round.",
            result.status,
            {**extra, "trace_probe": worker_probe, "trace_chars": len(raw_trace), "recovery_reason": probe_reason},
        )
        return
    if probe_reason in RECOVERABLE_COPY_GATE_REASONS:
        if round_:
            round_.trace_status = probe_reason
        if _queue_trace_recovery_diagnosis(
            db,
            command,
            job,
            round_,
            f"Trae copied reply still needs recovery ({probe_reason}); scheduler will screenshot and diagnose Trae before acting.",
            {**extra, "trace_probe": worker_probe, "trace_chars": len(raw_trace)},
        ):
            return
        _queue_continue_recovery(
            db,
            command,
            job,
            round_,
            f"Trae copied reply still needs recovery ({probe_reason}); retrying recovery before trace collection.",
            {**extra, "trace_probe": worker_probe, "trace_chars": len(raw_trace)},
        )
        return

    validation = validate_full_trace(raw_trace)
    trace_extra = {
        **extra,
        "trace_chars": len(raw_trace),
        "validation": validation,
    }
    if round_:
        round_.trace_status = "valid" if validation["valid"] else validation["reason"]

    if validation["valid"] and _is_invalid_business_trace(raw_trace):
        validation = {"valid": False, "reason": "local_trace_not_business_trace"}
        trace_extra["validation"] = validation
        if round_:
            round_.trace_status = validation["reason"]
        _mark_trace_missing_abort(
            db,
            job,
            round_,
            "Copied trace was rejected because it is a local/test trace, not the real Trae assistant reply body.",
            trace_extra,
        )
        return

    if not validation["valid"] and is_recoverable_trace_reason(validation["reason"]):
        if validation["reason"] in MANUAL_STOP_REASONS:
            _mark_manual_required(
                db,
                job,
                round_,
                "Trae generation appears to have been manually stopped; manual inspection is required before retrying this round.",
                result.status,
                {**trace_extra, "recovery_reason": validation["reason"]},
            )
            return
        if validation["reason"] in TRACE_COPY_RETRY_REASONS and _queue_trace_recovery_diagnosis(
            db,
            command,
            job,
            round_,
            f"Trae copied reply is not a complete raw tool trace yet ({validation['reason']}); scheduler will screenshot and diagnose Trae before retrying.",
            trace_extra,
        ):
            return
        if validation["reason"] in TRACE_COPY_RETRY_REASONS and _queue_trace_copy_retry(
            db,
            command,
            job,
            round_,
            f"Trae copied reply is not a complete raw tool trace yet ({validation['reason']}); retrying trace copy.",
            trace_extra,
        ):
            return
        if _handle_completed_trace_unavailable(db, command, job, round_, trace_extra, raw_trace=raw_trace):
            return
        _queue_continue_recovery(
            db,
            command,
            job,
            round_,
            f"Trae trace is not complete yet ({validation['reason']}); retrying recovery before trace collection.",
            trace_extra,
        )
        return

    if validation["valid"]:
        gate = _copy_current_turn_gate(result.data)
        trace_extra["current_turn_gate"] = gate
        gate_overridden = False
        if not gate["passed"]:
            if _valid_copy_can_override_current_turn_gate(gate, result.data):
                gate_overridden = True
                trace_extra["current_turn_gate_overridden"] = True
                trace_extra["current_turn_gate_override_reason"] = "validated_complete_copy"
                if round_:
                    round_.trace_status = "valid"
                add_log(
                    db,
                    job_id=job.id,
                    round_id=round_.id if round_ else None,
                    stage=JobState.TRACE_VALIDATING,
                    message=(
                        "Worker copied a complete Trae trace; local turn-status probe was recoverable, "
                        "so the validated copy is accepted."
                    ),
                    level="warning",
                    extra=trace_extra,
                )
            else:
                if round_:
                    round_.trace_status = gate["reason"]
                if gate["recoverable"]:
                    if _handle_completed_trace_unavailable(db, command, job, round_, trace_extra, raw_trace=raw_trace):
                        return
                    if _queue_trace_recovery_diagnosis(
                        db,
                        command,
                        job,
                        round_,
                        f"Current Trae turn is not complete yet ({gate['reason']}); scheduler will screenshot and diagnose Trae before recovery.",
                        trace_extra,
                    ):
                        return
                    _queue_continue_recovery(
                        db,
                        command,
                        job,
                        round_,
                        f"Current Trae turn is not complete yet ({gate['reason']}); retrying recovery before trace collection.",
                        trace_extra,
                    )
                    return
                _mark_trace_missing_abort(
                    db,
                    job,
                    round_,
                    f"Copied Trae reply was rejected because it does not belong to the current completed turn ({gate['reason']}).",
                    trace_extra,
                )
                return
        _store_trae_turn_metadata(
            db,
            job,
            round_,
            _trae_turn_metadata_from_copy_result(result.data, allow_candidate=gate_overridden),
            allow_non_completed=gate_overridden,
        )
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
            {"target": "trae_window", "quality_required": True},
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
    quality = result.data.get("quality") if isinstance(result.data, dict) else {}
    quality_failed = isinstance(quality, dict) and quality.get("ok") is False
    if result.status not in {"ok", "success", "completed"} or data_status != "captured" or not screenshot_path or quality_failed:
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
        {
            "prompt": round_.prompt if round_ else "",
        },
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
        message="Worker completed a Trae recovery action; waiting for the assistant reply to finish.",
        extra={**extra, "display_message": _continue_action_display_message(result.data)},
    )
    wait_command = _enqueue_worker_command(
        db,
        command,
        WorkerCommandType.WAIT_COMPLETION,
        _wait_completion_payload(
            command,
            round_,
            {
                "continue_attempts": command.payload.get("continue_attempts", 0),
                "max_continue_attempts": command.payload.get("max_continue_attempts", 20),
                "continue_action_sent": _click_continue_action_was_sent(result.data),
                "continue_text_sent": _click_continue_action_was_sent(result.data),
                "continue_sent_at": datetime.now(timezone.utc).isoformat(),
            },
        ),
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.WAITING_TRAE,
        message="wait_completion worker command queued after continuing Trae.",
        extra={
            "worker_id": wait_command.worker_id,
            "command_id": wait_command.id,
            "display_message": "续写恢复动作已完成，Worker 重新观察 Trae CN 回复是否收口。",
        },
    )


def _record_screenshot_attachment(db: Session, command: WorkerCommand, result: WorkerResult) -> Attachment:
    uploaded = result.data.get("server_attachment")
    if isinstance(uploaded, dict) and uploaded.get("id"):
        attachment = db.get(Attachment, str(uploaded.get("id")))
        if attachment and attachment.user_id == command.user_id:
            attachment.job_id = attachment.job_id or command.job_id
            attachment.round_id = attachment.round_id or command.round_id
            attachment.kind = "screenshot"
            db.flush()
            return attachment
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
    return _record_text_attachment(db, command, raw_trace, kind="trace", filename_prefix="trae-trace")


def _record_text_attachment(
    db: Session,
    command: WorkerCommand,
    text: str,
    *,
    kind: str,
    filename_prefix: str,
) -> Attachment:
    filename = f"{filename_prefix}-{command.job_id or 'job'}-{command.round_id or 'round'}.txt"
    out_dir = settings.attachment_root / f"{kind}s"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(text, encoding="utf-8")
    attachment = Attachment(
        user_id=command.user_id,
        job_id=command.job_id,
        round_id=command.round_id,
        kind=kind,
        filename=filename,
        path=str(path),
        content_type="text/plain; charset=utf-8",
        size_bytes=len(text.encode("utf-8")),
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

    product_review = _product_review_from_data(result.data)
    if product_review:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.PRODUCT_REVIEWING,
            message="Static product review evidence collected.",
            level="warning" if _product_review_has_blocking(product_review) else "info",
            extra={"product_review": product_review},
        )

    recommended_commands = [] if _test_mode_skips_trae_self_tests(job) else _recommended_commands(result.data)
    if _test_mode_skips_trae_self_tests(job):
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.PRODUCT_REVIEWING,
            message="Test mode skipped project self-test/build commands so the chain can validate GitHub and Feishu faster.",
            level="info",
            extra={
                "run_mode": "test",
                "skipped_recommended_commands": _recommended_commands(result.data),
                "policy": "skip_trae_self_tests",
            },
        )
    if recommended_commands:
        review_command = _enqueue_worker_command(
            db,
            command,
            WorkerCommandType.RUN_COMMAND,
            {
                "command": recommended_commands[0],
                "cwd": _recommended_command_cwd(command, result.data),
                "timeout": command.payload.get("review_timeout_seconds", 180),
                "purpose": "product_review",
                "remaining_commands": recommended_commands[1:],
                "product_review": product_review,
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

    if _product_review_has_blocking(product_review):
        _record_product_review_evidence(
            db,
            job,
            round_,
            command,
            "Static product review found blocking issues and no automated build/test command was available.",
            extra,
            product_review=product_review,
        )
        _advance_to_browser_accepting(
            db,
            command,
            job,
            round_,
            "Static product review found issues; browser acceptance will continue before generating the final dissatisfaction reason.",
            level="warning",
            extra=extra,
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
        remaining_commands = _remaining_commands(command.payload)
        if remaining_commands:
            next_command = _enqueue_worker_command(
                db,
                command,
                WorkerCommandType.RUN_COMMAND,
                {
                    "command": remaining_commands[0],
                    "cwd": command.payload.get("cwd") or command.payload.get("workspace_path"),
                    "timeout": command.payload.get("review_timeout_seconds", command.payload.get("timeout", 180)),
                    "purpose": "product_review",
                    "remaining_commands": remaining_commands[1:],
                    "product_review": command.payload.get("product_review") or {},
                },
            )
            add_log(
                db,
                job_id=job.id,
                round_id=round_.id if round_ else None,
                stage=JobState.PRODUCT_REVIEWING,
                message="Additional product review command queued.",
                extra={"worker_id": next_command.worker_id, "command_id": next_command.id, "command": remaining_commands[0]},
            )
            return

        product_review = command.payload.get("product_review") if isinstance(command.payload, dict) else {}
        if _product_review_has_blocking(product_review):
            _record_product_review_evidence(
                db,
                job,
                round_,
                command,
                "Product review build/test command passed, but static review still found blocking issues.",
                extra,
                product_review=product_review,
            )
            _advance_to_browser_accepting(
                db,
                command,
                job,
                round_,
                "Build/test command passed, but static review still found issues; browser acceptance will continue before the final dissatisfaction reason.",
                level="warning",
                extra=extra,
            )
            return

        _advance_to_browser_accepting(
            db,
            command,
            job,
            round_,
            "Product review build/test command passed.",
            extra=extra,
        )
        return

    _record_product_review_evidence(
        db,
        job,
        round_,
        command,
        "Product review build/test command failed; manual review or dissatisfaction reason generation is required.",
        extra,
        result=result,
    )
    _advance_to_browser_accepting(
        db,
        command,
        job,
        round_,
        "Product review build/test command failed; browser acceptance will continue before generating the final dissatisfaction reason.",
        level="warning",
        extra=extra,
    )


def _handle_browser_acceptance_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return
    if not _ensure_trace_gate(db, job, round_, command):
        return
    if not _ensure_trae_session_gate(db, job, round_, command):
        return

    extra = _result_extra(command, result)
    data_status = str(result.data.get("status") or "")
    if result.status in {"ok", "success", "completed"} and data_status == "passed":
        if _force_test_unsatisfied(job):
            _record_dissatisfaction(
                db,
                job,
                round_,
                command,
                result,
                JobState.BROWSER_ACCEPTING,
                "Browser acceptance passed, but this test-chain run is configured to force dissatisfaction so the GitHub and Feishu follow-up path can be verified.",
                extra,
            )
            _advance_to_github_submitting(
                db,
                command,
                job,
                round_,
                "Browser acceptance passed; test mode forced dissatisfaction and GitHub submission will continue for chain validation.",
                extra,
                level="warning",
            )
            return
        if _has_pending_product_review_evidence(db, job.id, round_.id if round_ else None):
            review_result, review_extra = _merge_product_review_evidence_for_final_reason(db, job, round_, command, result, extra)
            _record_dissatisfaction(
                db,
                job,
                round_,
                command,
                review_result,
                JobState.BROWSER_ACCEPTING,
                "Browser acceptance passed, but earlier product review evidence still found blocking issues.",
                review_extra,
            )
            _advance_to_github_submitting(
                db,
                command,
                job,
                round_,
                "Browser acceptance passed after product review issues; final dissatisfaction reason was generated and GitHub submission will continue.",
                extra,
                level="warning",
            )
            return
        if round_ and round_.round_index == 1 and not _has_dissatisfaction_reason(db, job.id, round_.id):
            _discard_first_round_satisfied(db, job, round_, extra)
            return
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage=JobState.BROWSER_ACCEPTING,
            message="Browser acceptance passed with local page evidence.",
            extra=extra,
        )
        _advance_to_github_submitting(
            db,
            command,
            job,
            round_,
            "Browser acceptance gate passed; git_submit worker command queued.",
            extra,
        )
        return

    message = _browser_acceptance_failure_message(result, data_status)
    _record_dissatisfaction(db, job, round_, command, result, JobState.BROWSER_ACCEPTING, message, extra)
    _advance_to_github_submitting(
        db,
        command,
        job,
        round_,
        "Browser acceptance did not pass; dissatisfaction reason was generated and GitHub submission will continue for the business record.",
        extra,
        level="warning",
    )


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

    command.status = "manual_required"
    command.error = "Git submission failed; stopping before Feishu write."
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


def _ensure_github_repo_for_job(db: Session, job: Job, command: WorkerCommand) -> dict:
    configs = load_user_settings(db, job.user_id)
    github_config = dict(configs.get("github", {}))
    if command.payload.get("github_remote_url") and not github_config.get("remote_url"):
        github_config["remote_url"] = command.payload["github_remote_url"]
    project_name = str(command.payload.get("project_name") or command.payload.get("github_repo_name") or "")
    return ensure_github_repository(github_config, project_name=project_name)


def _advance_to_github_submitting(
    db: Session,
    command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    message: str,
    extra: dict,
    level: str = "info",
) -> None:
    job.status = JobState.GITHUB_SUBMITTING
    if round_:
        round_.status = JobState.GITHUB_SUBMITTING
        round_.github_status = "submitting"
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.GITHUB_SUBMITTING,
        message=message,
        level=level,
        extra={"worker_id": command.worker_id, "command_id": command.id, **extra},
    )
    github_push = command.payload.get("github_push", True)
    github_repo = _ensure_github_repo_for_job(db, job, command) if github_push else {"ok": True, "skipped": True}
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.GITHUB_SUBMITTING,
        message="GitHub repository preflight completed before worker git_submit.",
        level="info" if github_repo.get("ok") else "warning",
        extra=github_repo,
    )
    git_command = _enqueue_worker_command(
        db,
        command,
        WorkerCommandType.GIT_SUBMIT,
        {
            "commit_message": _commit_message(job, round_),
            "push": github_push,
            "remote": command.payload.get("github_remote", "origin"),
            "remote_url": github_repo.get("remote_url") or command.payload.get("github_remote_url", ""),
            "branch": command.payload.get("github_branch", ""),
            "timeout": command.payload.get("github_timeout_seconds", 120),
            "github_repo": github_repo,
            "project_name": command.payload.get("project_name") or command.payload.get("github_repo_name") or "",
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
        _persist_feishu_token_cache(db, job.user_id, feishu_config, getattr(exc, "token_cache", None))
        _mark_feishu_failed(db, job, round_, str(exc), _feishu_failure_extra(fields, exc))
        return
    except Exception as exc:
        _mark_feishu_failed(db, job, round_, f"Feishu write failed: {exc}", {"field_names": list(fields.keys()), "field_count": len(fields)})
        return

    token_cache = write_result.pop("token_cache", None)
    _persist_feishu_token_cache(db, job.user_id, feishu_config, token_cache)

    satisfaction = _feishu_satisfaction(db, job.id, round_.id)
    satisfied = bool(satisfaction["satisfied"])
    round_.feishu_status = str(write_result.get("status") or "written")
    round_.status = JobState.ROUND_COMPLETED
    decision = _next_round_decision(job, round_, satisfied)
    if satisfied and decision.get("accepted_satisfied"):
        job.satisfied_count += 1
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.FEISHU_WRITING,
        message="Feishu business record written.",
        extra=write_result,
    )
    _advance_after_feishu_success(db, job, round_, satisfied, decision)


def _persist_feishu_token_cache(
    db: Session,
    user_id: str,
    feishu_config: dict,
    token_cache: dict | None,
) -> None:
    if not token_cache:
        return
    feishu_config["token_cache"] = token_cache
    save_user_settings(db, user_id, {"feishu": feishu_config}, allow_internal=True)


def _feishu_failure_extra(fields: dict[str, object], exc: FeishuWriteError) -> dict:
    extra = {"field_names": list(fields.keys()), "field_count": len(fields)}
    if exc.auth_mode:
        extra["auth_mode"] = exc.auth_mode
    if exc.status_code is not None:
        extra["http_status"] = exc.status_code
    if exc.code is not None:
        extra["feishu_code"] = exc.code
    if exc.operation:
        extra["operation"] = exc.operation
    if exc.token_cache:
        extra["token_cache_refreshed_before_failure"] = True
    return extra


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
    _notify_feishu_write_failed(db, job, round_, message, extra)


def _advance_after_feishu_success(
    db: Session,
    job: Job,
    round_: TaskRound,
    satisfied: bool,
    decision: dict[str, object] | None = None,
) -> None:
    decision = decision or _next_round_decision(job, round_, satisfied)
    if decision["action"] == "continue_project":
        next_round = TaskRound(
            job_id=job.id,
            project_id=round_.project_id,
            round_index=round_.round_index + 1,
            status=JobState.GENERATING_PROMPT,
        )
        db.add(next_round)
        db.flush()
        job.status = JobState.GENERATING_PROMPT
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage=JobState.ROUND_COMPLETED,
            message=_continue_project_message(decision),
            extra={**decision, "next_round_id": next_round.id, "next_round_index": next_round.round_index},
        )
        add_log(
            db,
            job_id=job.id,
            round_id=next_round.id,
            stage=JobState.GENERATING_PROMPT,
            message="Next round is ready for prompt generation.",
            extra={"previous_round_id": round_.id, "round_index": next_round.round_index},
        )
        _auto_dispatch_next_round(db, job, next_round)
        return

    if not str(decision.get("reason") or "").startswith("daily_target_reached") and _advance_to_next_direction(db, job, round_, decision):
        return

    job.status = JobState.PROJECT_COMPLETED
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.PROJECT_COMPLETED,
        message="Round completed and project marked completed.",
        extra={
            **decision,
            "round_index": round_.round_index,
            "feishu_status": round_.feishu_status,
            "submitted_count": job.submitted_count,
            "satisfied_count": job.satisfied_count,
        },
    )


def _next_round_decision(job: Job, round_: TaskRound, satisfied: bool) -> dict[str, object]:
    daily_target_reached = bool(job.daily_target and job.submitted_count >= job.daily_target)
    range_target = _current_range_target_rounds(job)
    if satisfied:
        if int(job.submitted_count or 0) <= 0:
            return {"action": "complete_project", "reason": "satisfied_without_submission", "accepted_satisfied": False}
        if _would_exceed_satisfied_ratio(job):
            if daily_target_reached:
                return {
                    "action": "complete_project",
                    "reason": "daily_target_reached_after_satisfied_ratio_cap",
                    "accepted_satisfied": False,
                    "satisfied_ratio_cap": MAX_SATISFIED_RATIO,
                }
            if round_.round_index >= range_target:
                return {
                    "action": "complete_project",
                    "reason": "max_round_reached_after_satisfied_ratio_cap",
                    "accepted_satisfied": False,
                    "satisfied_ratio_cap": MAX_SATISFIED_RATIO,
                }
            return {
                "action": "continue_project",
                "reason": "satisfied_ratio_cap",
                "accepted_satisfied": False,
                "satisfied_ratio_cap": MAX_SATISFIED_RATIO,
            }
        if daily_target_reached:
            return {"action": "complete_project", "reason": "daily_target_reached", "accepted_satisfied": True}
        if _should_continue_after_satisfied(job, round_):
            return {
                "action": "continue_project",
                "reason": "satisfied_expand_next_module",
                "accepted_satisfied": True,
                "range_target_rounds": range_target,
            }
        return {"action": "complete_project", "reason": "satisfied", "accepted_satisfied": True, "range_target_rounds": range_target}
    if daily_target_reached:
        return {"action": "complete_project", "reason": "daily_target_reached"}
    if round_.round_index >= range_target:
        return {"action": "complete_project", "reason": "range_target_reached", "range_target_rounds": range_target}
    return {"action": "continue_project", "reason": "dissatisfied_followup", "range_target_rounds": range_target}


def _would_exceed_satisfied_ratio(job: Job) -> bool:
    submitted = int(job.submitted_count or 0)
    if submitted <= 0:
        return False
    next_satisfied = int(job.satisfied_count or 0) + 1
    return (next_satisfied / submitted) > MAX_SATISFIED_RATIO


def _current_range_target_rounds(job: Job) -> int:
    intent = job.intent if isinstance(job.intent, dict) else {}
    plan = intent.get("range_plan") if isinstance(intent.get("range_plan"), dict) else {}
    ranges = plan.get("ranges") if isinstance(plan.get("ranges"), list) else []
    current_direction = _direction_queue(job)[0] if _direction_queue(job) else ""
    for item in ranges:
        if not isinstance(item, dict):
            continue
        if str(item.get("source_text") or "").strip() == current_direction:
            try:
                return min(MAX_ROUNDS_PER_PROJECT, max(1, int(item.get("target_rounds") or MAX_ROUNDS_PER_PROJECT)))
            except (TypeError, ValueError):
                return MAX_ROUNDS_PER_PROJECT
    if ranges:
        try:
            return min(MAX_ROUNDS_PER_PROJECT, max(1, int(ranges[0].get("target_rounds") or MAX_ROUNDS_PER_PROJECT)))
        except (AttributeError, TypeError, ValueError):
            return MAX_ROUNDS_PER_PROJECT
    return MAX_ROUNDS_PER_PROJECT


def _should_continue_after_satisfied(job: Job, round_: TaskRound) -> bool:
    if round_.round_index >= _current_range_target_rounds(job):
        return False
    if _has_unstarted_direction(job) and round_.round_index >= max(3, _current_range_target_rounds(job) // 2):
        return False
    return True


def _has_unstarted_direction(job: Job) -> bool:
    return len(_direction_queue(job)) > 1


def _continue_project_message(decision: dict[str, object]) -> str:
    if decision.get("reason") == "satisfied_ratio_cap":
        return "Round completed; next project round prepared because the satisfied ratio cap would be exceeded."
    if decision.get("reason") == "satisfied_expand_next_module":
        return "Round completed satisfactorily; next range module round prepared by scheduler."
    return "Round completed; next project round prepared because the result is still dissatisfied."


def _advance_to_next_direction(db: Session, job: Job, round_: TaskRound, decision: dict[str, object]) -> bool:
    directions = _direction_queue(job)
    if len(directions) <= 1:
        if _maybe_append_synthetic_direction(job, round_, decision):
            directions = _direction_queue(job)
        else:
            return False
    if len(directions) <= 1:
        return False

    completed_direction = directions[0]
    remaining_directions = directions[1:]
    project = db.get(Project, round_.project_id) if round_.project_id else None
    if project:
        project.status = "completed"

    job.directions = remaining_directions
    next_round = TaskRound(
        job_id=job.id,
        round_index=1,
        status=JobState.GENERATING_PROMPT,
    )
    db.add(next_round)
    db.flush()
    job.status = JobState.GENERATING_PROMPT
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.ROUND_COMPLETED,
        message="Project direction completed; next queued direction prepared.",
        extra={
            **decision,
            "completed_direction": completed_direction,
            "next_direction": remaining_directions[0],
            "remaining_directions": remaining_directions,
            "next_round_id": next_round.id,
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=next_round.id,
        stage=JobState.GENERATING_PROMPT,
        message="Next queued direction is ready for prompt generation.",
        extra={"previous_round_id": round_.id, "direction": remaining_directions[0]},
    )
    _auto_dispatch_next_round(db, job, next_round)
    return True


def _maybe_append_synthetic_direction(job: Job, round_: TaskRound, decision: dict[str, object]) -> bool:
    if not job.daily_target or int(job.submitted_count or 0) >= int(job.daily_target or 0):
        return False
    reason = str(decision.get("reason") or "")
    if reason not in {"range_target_reached", "satisfied", "max_round_reached_after_satisfied_ratio_cap"}:
        return False
    intent = job.intent if isinstance(job.intent, dict) else {}
    plan = intent.get("range_plan") if isinstance(intent.get("range_plan"), dict) else {}
    policy = plan.get("synthetic_range_policy") if isinstance(plan.get("synthetic_range_policy"), dict) else {}
    if policy.get("allowed") is not True:
        return False
    examples = [str(item).strip() for item in policy.get("examples") or [] if str(item).strip()]
    if not examples:
        examples = ["运营管理后台", "业务审核工作台", "数据统计看板", "异常处理中心"]
    existing = set(_direction_queue(job))
    choice = next((item for item in examples if item not in existing), "")
    if not choice:
        return False
    synthetic = f"{choice}：作为前面范围的延展项目，继续做中等规模业务工作台，覆盖列表、详情、操作流、统计联动和异常反馈。"
    job.directions = [*(_direction_queue(job) or []), synthetic]
    plan_ranges = list(plan.get("ranges") if isinstance(plan.get("ranges"), list) else [])
    remaining = max(1, int(job.daily_target or 100) - int(job.submitted_count or 0))
    plan_ranges.append(
        {
            "range_id": f"synthetic_{len(plan_ranges) + 1}",
            "title": choice,
            "source_text": synthetic,
            "target_rounds": min(MAX_ROUNDS_PER_PROJECT, remaining),
            "completed_rounds": 0,
            "status": "synthetic_pending",
            "project_policy": "调度根据剩余轮次创建的新独立项目",
            "module_map": ["系统骨架", "主列表", "详情视图", "新增编辑", "状态流转", "搜索筛选", "统计联动", "异常状态", "运行构建"],
        }
    )
    job.intent = {**intent, "range_plan": {**plan, "ranges": plan_ranges}}
    return True


def _direction_queue(job: Job) -> list[str]:
    if not isinstance(job.directions, list):
        return []
    return [str(item).strip() for item in job.directions if str(item).strip()]


def _discard_first_round_satisfied(db: Session, job: Job, round_: TaskRound, extra: dict) -> None:
    round_.status = "first_round_discarded"
    project = db.get(Project, round_.project_id) if round_.project_id else None
    if project:
        project.status = "discarded_first_round_satisfied"
    next_round = TaskRound(
        job_id=job.id,
        round_index=1,
        status=JobState.GENERATING_PROMPT,
    )
    db.add(next_round)
    db.flush()
    job.status = JobState.GENERATING_PROMPT
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage="first_round_discarded",
        message="First round was satisfied, so it was discarded and will not be submitted to GitHub or Feishu.",
        level="warning",
        extra={**extra, "next_round_id": next_round.id},
    )
    add_log(
        db,
        job_id=job.id,
        round_id=next_round.id,
        stage=JobState.GENERATING_PROMPT,
        message="A new first round was prepared after discarding the satisfied first round.",
        extra={"discarded_round_id": round_.id},
    )
    _auto_dispatch_next_round(db, job, next_round)


def _has_dissatisfaction_reason(db: Session, job_id: str, round_id: str) -> bool:
    return bool(
        db.scalar(
            select(RuntimeLog.id)
            .where(
                RuntimeLog.job_id == job_id,
                RuntimeLog.round_id == round_id,
                RuntimeLog.stage == "dissatisfaction_reason",
            )
            .limit(1)
        )
    )


def _auto_dispatch_next_round(db: Session, job: Job, round_: TaskRound) -> None:
    user = db.get(User, job.user_id)
    if not user:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage=JobState.MANUAL_REQUIRED,
            message="Next round cannot continue automatically because the job user was not found.",
            level="warning",
        )
        return
    try:
        generate_round_prompt(db, user, job, round_)
    except PromptGenerationError as exc:
        mark_prompt_generation_failed(db, job, round_, str(exc))
        return
    if job.status != JobState.PROMPT_READY:
        return
    try:
        dispatch_prompt_to_worker(db, user, job, round_)
    except WorkerDispatchError as exc:
        mark_worker_dispatch_failed(db, job, round_, str(exc))


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
        orchestrator_intent=job.intent or {},
    )


def _add_dissatisfaction_log(
    db: Session,
    job: Job,
    round_: TaskRound,
    evidence: DissatisfactionEvidence,
) -> dict:
    generated = generate_dissatisfaction_reason(
        evidence,
        db=db,
        user=db.get(User, job.user_id),
        previous_reason=_previous_dissatisfaction_reason(db, job.id, round_.id),
    )
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


def _previous_dissatisfaction_reason(db: Session, job_id: str, round_id: str) -> str:
    item = db.scalar(
        select(RuntimeLog)
        .where(
            RuntimeLog.job_id == job_id,
            RuntimeLog.stage == "dissatisfaction_reason",
            RuntimeLog.round_id != round_id,
        )
        .order_by(RuntimeLog.created_at.desc())
        .limit(1)
    )
    if not item:
        return ""
    if isinstance(item.extra, dict):
        return str(item.extra.get("reason") or item.extra.get("product_reason") or item.extra.get("process_reason") or "")
    return str(item.message or "")


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
    recovery_reason = _recovery_reason(extra)
    recovery_extra = {
        **extra,
        "continue_attempts": continue_attempts,
        "max_continue_attempts": max_continue_attempts,
        "recovery_reason": recovery_reason,
        "display_message": _recovery_display_message(recovery_reason, continue_attempts, max_continue_attempts),
    }
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.AWAITING_CONTINUE,
        message=message,
        level="info",
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
            "recovery_reason": recovery_reason,
            "trae_workspace_path": source_command.payload.get("trae_workspace_path")
            or source_command.payload.get("workspace_path"),
            "workspace_path": source_command.payload.get("workspace_path")
            or source_command.payload.get("trae_workspace_path"),
            "workspace_root": source_command.payload.get("workspace_root", ""),
            "project_name": source_command.payload.get("project_name", ""),
            "project_slug": source_command.payload.get("project_slug", ""),
            "prompt": source_command.payload.get("prompt", ""),
            "sent_at_epoch": source_command.payload.get("sent_at_epoch")
            or source_command.payload.get("prompt_sent_at_epoch"),
            "sent_at": source_command.payload.get("sent_at") or source_command.payload.get("prompt_sent_at", ""),
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.AWAITING_CONTINUE,
        message="Trae recovery worker command queued for incomplete reply recovery.",
        extra={
            "worker_id": continue_command.worker_id,
            "command_id": continue_command.id,
            "recovery_reason": recovery_reason,
            "continue_attempts": continue_attempts,
            "max_continue_attempts": max_continue_attempts,
            "display_message": "已安排 Worker 进行一次续写恢复，完成后会重新等待 Trae CN 回复收口。",
        },
    )


def _queue_trace_recovery_diagnosis(
    db: Session,
    source_command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    message: str,
    extra: dict,
) -> bool:
    attempts = int(source_command.payload.get("trace_recovery_diagnosis_attempts") or 0) + 1
    max_attempts = int(
        source_command.payload.get("max_trace_recovery_diagnosis_attempts")
        or DEFAULT_MAX_TRACE_RECOVERY_DIAGNOSIS_ATTEMPTS
    )
    if attempts > max_attempts:
        return False

    job.status = JobState.AWAITING_CONTINUE
    if round_:
        round_.status = JobState.AWAITING_CONTINUE
    recovery_reason = _trace_copy_retry_reason(extra)
    resume_payload = {
        "timeout_seconds": source_command.payload.get("copy_timeout_seconds", source_command.payload.get("timeout_seconds", 10)),
        "trace_copy_attempts": source_command.payload.get("trace_copy_attempts", 0),
        "max_trace_copy_attempts": source_command.payload.get("max_trace_copy_attempts", DEFAULT_MAX_TRACE_COPY_ATTEMPTS),
        "trace_recovery_diagnosis_attempts": attempts,
        "max_trace_recovery_diagnosis_attempts": max_attempts,
        "prompt": round_.prompt if round_ and round_.prompt else source_command.payload.get("prompt", ""),
        "allow_local_trace_fallback": False,
        "completion_observation": source_command.payload.get("completion_observation", {}),
    }
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.AWAITING_CONTINUE,
        message=message,
        level="info",
        extra={
            **extra,
            "trace_recovery_diagnosis_attempts": attempts,
            "max_trace_recovery_diagnosis_attempts": max_attempts,
            "recovery_reason": recovery_reason,
            "display_message": (
                f"复制轨迹不完整，调度先让 Worker 截图给视觉诊断，判断该继续、等待还是重试复制"
                f"（第 {attempts}/{max_attempts} 次）。"
            ),
        },
    )
    diagnose_command = _enqueue_worker_command(
        db,
        source_command,
        WorkerCommandType.DIAGNOSE_UI,
        {
            "task": "find_reply_action_button",
            "timeout_seconds": source_command.payload.get("diagnose_timeout_seconds", 10),
            "scroll_bottom": True,
            "previous_command_type": WorkerCommandType.COPY_LATEST_REPLY.value,
            "resume_previous_payload": resume_payload,
            "retry_of_command_id": source_command.id,
            "trace_copy_attempts": source_command.payload.get("trace_copy_attempts", 0),
            "max_trace_copy_attempts": source_command.payload.get("max_trace_copy_attempts", DEFAULT_MAX_TRACE_COPY_ATTEMPTS),
            "copy_timeout_seconds": source_command.payload.get("copy_timeout_seconds", source_command.payload.get("timeout_seconds", 10)),
            "completion_observation": source_command.payload.get("completion_observation", {}),
            "trace_recovery_diagnosis_attempts": attempts,
            "max_trace_recovery_diagnosis_attempts": max_attempts,
            "recovery_reason": recovery_reason,
            "use_ai_ui_analyst": source_command.payload.get("use_ai_ui_analyst", True),
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.AWAITING_CONTINUE,
        message="diagnose_ui worker command queued for trace recovery decision.",
        extra={
            "worker_id": diagnose_command.worker_id,
            "command_id": diagnose_command.id,
            "recovery_reason": recovery_reason,
            "display_message": "已安排 Worker 截图诊断 Trae 是否完成、是否需要续写或滚动。",
        },
    )
    return True


def _queue_wait_recovery_diagnosis(
    db: Session,
    source_command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    message: str,
    extra: dict,
) -> bool:
    attempts = int(source_command.payload.get("wait_recovery_diagnosis_attempts") or 0) + 1
    max_attempts = int(
        source_command.payload.get("max_wait_recovery_diagnosis_attempts")
        or DEFAULT_MAX_WAIT_RECOVERY_DIAGNOSIS_ATTEMPTS
    )
    if attempts > max_attempts:
        return False

    job.status = JobState.AWAITING_CONTINUE
    if round_:
        round_.status = JobState.AWAITING_CONTINUE
    recovery_reason = _recovery_reason(extra)
    resume_payload = _wait_completion_payload(
        source_command,
        round_,
        {
            "wait_observation_attempts": source_command.payload.get("wait_observation_attempts", 0),
            "max_wait_observation_attempts": source_command.payload.get(
                "max_wait_observation_attempts",
                DEFAULT_MAX_WAIT_OBSERVATION_ATTEMPTS,
            ),
            "wait_recovery_diagnosis_attempts": attempts,
            "max_wait_recovery_diagnosis_attempts": max_attempts,
            "continue_attempts": source_command.payload.get("continue_attempts", 0),
            "max_continue_attempts": source_command.payload.get("max_continue_attempts", 20),
            "continue_action_sent": bool(
                source_command.payload.get("continue_action_sent") or _wait_result_has_continue_action(extra)
            ),
            "continue_text_sent": bool(
                source_command.payload.get("continue_text_sent") or _wait_result_has_continue_action(extra)
            ),
            "continue_sent_at": source_command.payload.get("continue_sent_at") or extra.get("continue_sent_at", ""),
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.AWAITING_CONTINUE,
        message=message,
        level="info",
        extra={
            **extra,
            "wait_recovery_diagnosis_attempts": attempts,
            "max_wait_recovery_diagnosis_attempts": max_attempts,
            "recovery_reason": recovery_reason,
            "display_message": (
                f"等待 Trae 收口仍不明确，调度先让 Worker 截图给视觉诊断，判断该点击、继续等待还是采集轨迹"
                f"（第 {attempts}/{max_attempts} 次）。"
            ),
        },
    )
    diagnose_command = _enqueue_worker_command(
        db,
        source_command,
        WorkerCommandType.DIAGNOSE_UI,
        {
            "task": "wait_completion_state",
            "timeout_seconds": source_command.payload.get("diagnose_timeout_seconds", 10),
            "scroll_bottom": True,
            "previous_command_type": WorkerCommandType.WAIT_COMPLETION.value,
            "resume_previous_payload": resume_payload,
            "retry_of_command_id": source_command.id,
            "wait_recovery_diagnosis_attempts": attempts,
            "max_wait_recovery_diagnosis_attempts": max_attempts,
            "recovery_reason": recovery_reason,
            "use_ai_ui_analyst": source_command.payload.get("use_ai_ui_analyst", True),
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.AWAITING_CONTINUE,
        message="diagnose_ui worker command queued for wait completion recovery decision.",
        extra={
            "worker_id": diagnose_command.worker_id,
            "command_id": diagnose_command.id,
            "recovery_reason": recovery_reason,
            "display_message": "已安排 Worker 截图诊断 Trae 是否完成、是否需要点击确认或继续等待。",
        },
    )
    return True


def _queue_wait_observation_retry(
    db: Session,
    source_command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    message: str,
    extra: dict,
) -> bool:
    attempts = int(source_command.payload.get("wait_observation_attempts") or 0) + 1
    max_attempts = int(
        source_command.payload.get("max_wait_observation_attempts") or DEFAULT_MAX_WAIT_OBSERVATION_ATTEMPTS
    )
    if attempts > max_attempts:
        if _queue_wait_recovery_diagnosis(
            db,
            source_command,
            job,
            round_,
            "Worker repeatedly could not read a safe Trae completion signal; scheduler will request a visual diagnosis before stopping.",
            {**extra, "wait_observation_attempts": attempts, "max_wait_observation_attempts": max_attempts},
        ):
            return True
        _mark_manual_required(
            db,
            job,
            round_,
            "Worker repeatedly could not read a safe Trae completion signal; manual inspection is required.",
            "manual_required",
            {**extra, "wait_observation_attempts": attempts, "max_wait_observation_attempts": max_attempts},
        )
        return True

    job.status = JobState.WAITING_TRAE
    if round_:
        round_.status = JobState.WAITING_TRAE
    retry_extra = {
        **extra,
        "wait_observation_attempts": attempts,
        "max_wait_observation_attempts": max_attempts,
        "display_message": (
            f"Worker 暂时没有读到明确的 Trae CN 完成正文，也没有发现安全可点击的恢复操作，"
            f"继续观察（第 {attempts}/{max_attempts} 次）。"
        ),
    }
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.WAITING_TRAE,
        message=message,
        level="info",
        extra=retry_extra,
    )
    wait_command = _enqueue_worker_command(
        db,
        source_command,
        WorkerCommandType.WAIT_COMPLETION,
        _wait_completion_payload(
            source_command,
            round_,
            {
                "wait_observation_attempts": attempts,
                "max_wait_observation_attempts": max_attempts,
                "continue_attempts": source_command.payload.get("continue_attempts", 0),
                "max_continue_attempts": source_command.payload.get("max_continue_attempts", 20),
                "intervention_idle_seconds": extra.get(
                    "intervention_idle_seconds",
                    source_command.payload.get("intervention_idle_seconds", _default_wait_intervention_idle_seconds(round_)),
                ),
                "continue_action_sent": bool(
                    source_command.payload.get("continue_action_sent") or _wait_result_has_continue_action(extra)
                ),
                "continue_text_sent": bool(
                    source_command.payload.get("continue_text_sent") or _wait_result_has_continue_action(extra)
                ),
                "continue_sent_at": source_command.payload.get("continue_sent_at") or extra.get("continue_sent_at", ""),
            },
        ),
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.WAITING_TRAE,
        message="wait_completion worker command requeued for safe Trae observation.",
        extra={
            "worker_id": wait_command.worker_id,
            "command_id": wait_command.id,
            "wait_observation_attempts": attempts,
            "max_wait_observation_attempts": max_attempts,
            "display_message": "已重新安排 Worker 观察 Trae CN 当前状态，本次不会点击恢复按钮。",
        },
    )
    return True


def _queue_trace_copy_retry(
    db: Session,
    source_command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    message: str,
    extra: dict,
) -> bool:
    trace_copy_attempts = int(source_command.payload.get("trace_copy_attempts") or 0) + 1
    max_trace_copy_attempts = int(source_command.payload.get("max_trace_copy_attempts") or DEFAULT_MAX_TRACE_COPY_ATTEMPTS)
    if trace_copy_attempts > max_trace_copy_attempts:
        return False

    job.status = JobState.COLLECTING_TRACE
    if round_:
        round_.status = JobState.COLLECTING_TRACE
    retry_reason = _trace_copy_retry_reason(extra)
    retry_extra = {
        **extra,
        "trace_copy_attempts": trace_copy_attempts,
        "max_trace_copy_attempts": max_trace_copy_attempts,
        "trace_copy_retry_reason": retry_reason,
        "display_message": _trace_copy_retry_display_message(retry_reason, trace_copy_attempts, max_trace_copy_attempts),
    }
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.COLLECTING_TRACE,
        message=message,
        level="info",
        extra=retry_extra,
    )
    copy_command = _enqueue_worker_command(
        db,
        source_command,
        WorkerCommandType.COPY_LATEST_REPLY,
        {
            "timeout_seconds": source_command.payload.get("copy_timeout_seconds", source_command.payload.get("timeout_seconds", 10)),
            "trace_copy_attempts": trace_copy_attempts,
            "max_trace_copy_attempts": max_trace_copy_attempts,
            "prompt": round_.prompt if round_ and round_.prompt else source_command.payload.get("prompt", ""),
            "allow_local_trace_fallback": False,
            "completion_observation": source_command.payload.get("completion_observation", {}),
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.COLLECTING_TRACE,
        message="copy_latest_reply worker command requeued for trace collection retry.",
        extra={
            "worker_id": copy_command.worker_id,
            "command_id": copy_command.id,
            "trace_copy_attempts": trace_copy_attempts,
            "max_trace_copy_attempts": max_trace_copy_attempts,
            "trace_copy_retry_reason": retry_reason,
            "display_message": "已重新安排 Worker 复制 Trae CN 最新回复，先重试采集完整执行轨迹。",
        },
    )
    return True


def _wait_completion_payload(source_command: WorkerCommand, round_: TaskRound | None, extra: dict | None = None) -> dict:
    payload = {
        "timeout_seconds": source_command.payload.get("wait_timeout_seconds", 900),
        "stable_seconds": source_command.payload.get("stable_seconds", 15),
        "poll_interval_seconds": source_command.payload.get("poll_interval_seconds", 2),
        "intervention_idle_seconds": source_command.payload.get(
            "intervention_idle_seconds",
            _default_wait_intervention_idle_seconds(round_),
        ),
        "max_interventions": source_command.payload.get("max_interventions", 3),
    }
    if extra:
        payload.update(extra)
    return payload


def _wait_completion_supervisor_extra(extra: dict, data: dict) -> dict:
    if not isinstance(data, dict):
        return extra
    supervisor_decision = data.get("supervisor_decision")
    watcher_observation = data.get("watcher_observation")
    activity_summary = data.get("activity_summary")
    merged = dict(extra)
    if isinstance(watcher_observation, dict) and watcher_observation:
        merged["watcher_observation"] = watcher_observation
    if isinstance(activity_summary, dict) and activity_summary:
        merged["activity_summary"] = activity_summary
    if not isinstance(supervisor_decision, dict) or not supervisor_decision:
        return merged
    return {
        **merged,
        "supervisor_decision": supervisor_decision,
        "display_message": _wait_completion_supervisor_display_message(supervisor_decision),
    }


def _wait_completion_supervisor_display_message(supervisor_decision: dict) -> str:
    action = str(supervisor_decision.get("action") or "").strip()
    reason = str(supervisor_decision.get("reason") or "").strip()
    if action == "collect_trace":
        return "\u0053\u0075\u0070\u0065\u0072\u0076\u0069\u0073\u006f\u0072 \u5df2\u786e\u8ba4 \u0054\u0072\u0061\u0065 \u0043\u004e \u5f53\u524d\u56de\u5408\u5b8c\u6210\uff0c\u0057\u006f\u0072\u006b\u0065\u0072 \u5f00\u59cb\u83b7\u53d6\u56de\u590d\u5185\u5bb9\u548c\u6267\u884c\u8f68\u8ff9\u3002"
    if reason:
        return f"\u0053\u0075\u0070\u0065\u0072\u0076\u0069\u0073\u006f\u0072 \u5df2\u7ed9\u51fa \u0054\u0072\u0061\u0065 \u0043\u004e \u89c2\u5bdf\u7ed3\u8bba\uff08{reason}\uff09\uff0c\u0057\u006f\u0072\u006b\u0065\u0072 \u7ee7\u7eed\u6267\u884c\u8c03\u5ea6\u6d41\u7a0b\u3002"
    return "\u0053\u0075\u0070\u0065\u0072\u0076\u0069\u0073\u006f\u0072 \u5df2\u7ed9\u51fa \u0054\u0072\u0061\u0065 \u0043\u004e \u89c2\u5bdf\u7ed3\u8bba\uff0c\u0057\u006f\u0072\u006b\u0065\u0072 \u7ee7\u7eed\u6267\u884c\u8c03\u5ea6\u6d41\u7a0b\u3002"


def _default_wait_intervention_idle_seconds(round_: TaskRound | None) -> int:
    if round_ and int(round_.round_index or 0) == 1:
        return FIRST_ROUND_INTERVENTION_IDLE_SECONDS
    return FOLLOWUP_ROUND_INTERVENTION_IDLE_SECONDS


def _should_observe_wait_failure_without_recovery(extra: dict) -> bool:
    reason = _recovery_reason(extra)
    if reason == "wait_completion_timeout":
        return True
    if reason in {
        "awaiting_continuation",
        "awaiting_current_continuation",
        "service_interrupted",
        "no_completed_turn_after_prompt_send",
    } or reason.startswith("trae_turn_not_completed"):
        return False
    data = extra.get("data") if isinstance(extra.get("data"), dict) else {}
    supervisor = data.get("supervisor_decision") if isinstance(data.get("supervisor_decision"), dict) else {}
    supervisor_reason = str(supervisor.get("reason") or "").strip()
    error = str(extra.get("error") or "").strip()
    if supervisor_reason in {"window_chrome_only", "recent_trae_activity"}:
        return True
    if "only window chrome text was detected" in error:
        return True
    if "No explicit Trae intervention target was found" in error:
        return True
    if "No safe Trae intervention target was found" in error:
        return True
    if reason == "worker_command_error":
        return True
    return False


def _wait_failure_can_collect_trace(extra: dict) -> bool:
    if _recovery_reason(extra) != "wait_completion_timeout":
        return False
    data = extra.get("data") if isinstance(extra.get("data"), dict) else {}
    supervisor = data.get("supervisor_decision") if isinstance(data.get("supervisor_decision"), dict) else {}
    completion = supervisor.get("trae_turn_completion_decision") if isinstance(supervisor.get("trae_turn_completion_decision"), dict) else {}
    if completion.get("is_complete") is True and str(completion.get("next_action") or "") == "copy_trace":
        return True
    if str(supervisor.get("action") or "") == "collect_trace":
        return True
    if str(supervisor.get("reason") or "") in {
        "ui_completion_detected",
        "trae_turn_completed",
        "timeout_completion_detected",
        "completion_candidate_with_project_write",
        "completion_evidence_threshold",
    }:
        return True
    gate = data.get("completion_gate") if isinstance(data.get("completion_gate"), dict) else {}
    if gate.get("passed") is True:
        return True
    turn = data.get("trae_turn") if isinstance(data.get("trae_turn"), dict) else {}
    if turn.get("status") == "found" and str(turn.get("turn_status") or "") == "completed":
        return True
    if turn.get("status") == "found":
        tool_count = int(turn.get("tool_call_count") or 0)
        trace_id = str(turn.get("trace_id") or "")
        watcher = data.get("watcher_observation") if isinstance(data.get("watcher_observation"), dict) else {}
        activity = watcher.get("activity") if isinstance(watcher.get("activity"), dict) else {}
        recent = bool(activity.get("recent"))
        quiet = activity.get("quiet_seconds")
        try:
            quiet_value = float(quiet)
        except (TypeError, ValueError):
            quiet_value = 0.0
        if tool_count > 0 and trace_id and not recent and quiet_value >= 30.0:
            return True
    return False


def _wait_result_has_continue_action(extra: dict) -> bool:
    data = extra.get("data") if isinstance(extra.get("data"), dict) else {}
    if bool(extra.get("continue_action_sent") or extra.get("continue_text_sent")):
        return True
    if bool(data.get("continue_action_sent") or data.get("continue_text_sent")):
        return True
    interventions = data.get("interventions") if isinstance(data.get("interventions"), list) else []
    return any(_intervention_is_continue_action(item) for item in interventions)


def _continue_action_was_sent(command: WorkerCommand, extra: dict) -> bool:
    return bool(
        command.payload.get("continue_action_sent")
        or command.payload.get("continue_text_sent")
        or _wait_result_has_continue_action(extra)
    )


def _continuation_after_continue_can_collect_trace(command: WorkerCommand, extra: dict) -> bool:
    if not _continue_action_was_sent(command, extra):
        return False
    data = extra.get("data") if isinstance(extra.get("data"), dict) else {}
    supervisor = data.get("supervisor_decision") if isinstance(data.get("supervisor_decision"), dict) else {}
    if str(supervisor.get("action") or "") == "collect_trace":
        return True
    completion = supervisor.get("trae_turn_completion_decision") if isinstance(supervisor.get("trae_turn_completion_decision"), dict) else {}
    if completion.get("is_complete") is True and str(completion.get("next_action") or "") == "copy_trace":
        return True
    gate = data.get("completion_gate") if isinstance(data.get("completion_gate"), dict) else {}
    if gate.get("passed") is True:
        return True
    turn = data.get("trae_turn") if isinstance(data.get("trae_turn"), dict) else {}
    return turn.get("status") == "found" and str(turn.get("turn_status") or "") == "completed"


def _click_continue_action_was_sent(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    action_taken = str(data.get("action_taken") or "").strip()
    mode = str(data.get("mode") or "").strip()
    intervention = data.get("intervention") if isinstance(data.get("intervention"), dict) else {}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    return (
        action_taken in {"typed_continue", "clicked_button", "clicked_visual_target", "clicked_primary_fallback"}
        or mode in {"continue-text", "click-point", "primary-fallback"}
        or _intervention_is_continue_action(intervention)
        or _intervention_is_continue_action(result)
    )


def _intervention_is_continue_action(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    suggested = item.get("suggested_intervention") if isinstance(item.get("suggested_intervention"), dict) else {}
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    for candidate in (item, suggested, result):
        if not isinstance(candidate, dict):
            continue
        mode = str(candidate.get("mode") or "").strip()
        action = str(candidate.get("action") or "").strip()
        recommended = str(candidate.get("recommended_action") or "").strip()
        action_taken = str(candidate.get("action_taken") or "").strip()
        if mode == "continue-text":
            return True
        if action in {"continue", "continue_button"}:
            return True
        if recommended == "click_continue_button":
            return True
        if action_taken == "typed_continue":
            return True
    return False


def _diagnosis_suggests_continue_recovery(suggested: dict) -> bool:
    if not isinstance(suggested, dict):
        return False
    return _intervention_is_continue_action(suggested) or str(suggested.get("mode") or "") == "terminal-input"


def _recovery_reason(extra: dict) -> str:
    data = extra.get("data") if isinstance(extra.get("data"), dict) else {}
    for source in (
        extra.get("current_turn_gate"),
        data.get("current_turn_gate"),
        extra.get("validation"),
        data.get("validation"),
        extra.get("trace_probe"),
        data.get("trace_probe"),
        extra.get("output_probe"),
        data.get("output_probe"),
    ):
        if isinstance(source, dict) and str(source.get("reason") or "").strip():
            return str(source.get("reason")).strip()
    error = str(extra.get("error") or "").strip()
    if "did not become stable" in error:
        return "wait_completion_timeout"
    if error:
        return "worker_command_error"
    return "incomplete_trae_reply"


def _trace_copy_retry_reason(extra: dict) -> str:
    data = extra.get("data") if isinstance(extra.get("data"), dict) else {}
    for source in (
        extra.get("validation"),
        data.get("validation"),
        extra.get("trace_probe"),
        data.get("trace_probe"),
    ):
        if isinstance(source, dict) and str(source.get("reason") or "").strip():
            return str(source.get("reason")).strip()
    if str(extra.get("error") or "").strip():
        return "copy_command_failed"
    return "incomplete_trae_reply"


def _trace_copy_retry_display_message(reason: str, attempts: int, max_attempts: int) -> str:
    reason_text = _recovery_reason_text(reason)
    if max_attempts > 0:
        suffix = f"（第 {attempts}/{max_attempts} 次）"
    else:
        suffix = f"（第 {attempts} 次）"
    return f"复制到的 Trae CN 执行轨迹还不完整（{reason_text}），Worker 先重试滚底和复制{suffix}。"


def _recovery_display_message(reason: str, attempts: int, max_attempts: int) -> str:
    reason_text = _recovery_reason_text(reason)
    if max_attempts > 0:
        suffix = f"（第 {attempts}/{max_attempts} 次）"
    else:
        suffix = f"（第 {attempts} 次）"
    return f"当前回复还没有确认收口（{reason_text}），Worker 正在尝试续写恢复{suffix}。"


def _recovery_reason_text(reason: str) -> str:
    if reason.startswith("trae_turn_not_completed"):
        return "Trae 当前回合仍未完成"
    labels = {
        "awaiting_continuation": "回复提示需要继续",
        "awaiting_current_continuation": "当前回合需要继续",
        "service_interrupted": "Trae 回复出现中断信号",
        "no_completed_turn_after_prompt_send": "还没有找到本次提示词后的完成回合",
        "trace_too_short": "复制到的轨迹太短",
        "empty_trace": "没有复制到回复轨迹",
        "missing_tool_trace_markers": "回复轨迹缺少工具执行记录",
        "final_summary_only": "只复制到总结，没有完整执行过程",
        "partial_code_copy": "复制到的是局部代码片段",
        "pending_intervention_visible": "界面仍显示待确认操作",
        "turn_probe_unavailable": "无法读取 Trae 本地回合状态",
        "wait_completion_timeout": "等待完成超时",
        "worker_command_error": "Worker 命令返回异常",
        "incomplete_trae_reply": "回复或轨迹不完整",
    }
    return labels.get(reason, reason.replace("_", " ") if reason else "回复或轨迹不完整")


def _continue_action_display_message(data: dict) -> str:
    action_taken = str(data.get("action_taken") or "").strip()
    intervention = data.get("intervention") if isinstance(data.get("intervention"), dict) else {}
    mode = str(intervention.get("mode") or data.get("mode") or "")
    button_text = str(data.get("button_text") or "").strip()
    if button_text:
        return f"Worker 已点击 Trae CN 的「{button_text}」按钮，接下来重新等待回复收口。"
    if action_taken == "typed_continue" or mode == "continue-text":
        return "Worker 没有确认到可点击的继续按钮，已向 Trae CN 输入“继续”，接下来重新等待回复收口。"
    if action_taken == "clicked_button" or mode == "click-point":
        return "Worker 已点击诊断到的继续/确认操作，接下来重新等待回复收口。"
    if action_taken == "clicked_visual_target" or "visual-intervention" in mode:
        return "Worker 已按界面诊断结果点击可恢复操作，接下来重新等待回复收口。"
    if action_taken == "clicked_primary_fallback" or mode == "primary-fallback":
        return "Worker 未识别到明确按钮，已尝试安全的主操作位置，接下来重新观察 Trae CN。"
    return "Worker 已完成一次续写恢复尝试，接下来重新等待 Trae CN 回复收口。"


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
    if _test_chain_allowed(job):
        _apply_test_trace_exception(db, job, round_, command, trace_text)
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


def _ensure_trae_session_gate(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    command: WorkerCommand,
) -> bool:
    if round_ and _is_trusted_trae_session_display_id(round_.trae_session_id):
        return True
    if round_ and round_.trae_session_id:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="session_collected",
            message="Short Trae chat/session id is not trusted as the Feishu Trae Session ID.",
            level="warning",
            extra={"candidate": round_.trae_session_id, "reason": "not_canonical_display_id"},
        )
    if round_ and _hydrate_trae_session_from_trace(db, job, round_):
        return True
    session_probe = _latest_session_probe_context(db, job.id, round_.id if round_ else None)
    job.status = "session_missing_abort"
    if round_:
        round_.status = "session_missing_abort"
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage="session_missing_abort",
        message="Real Trae session id is missing; GitHub and Feishu submission were stopped.",
        level="error",
        extra={
            "command_id": command.id,
            "command_type": command.command_type,
            "session_probe": session_probe,
            "display_message": _session_missing_display_message(session_probe),
        },
    )
    return False


def _hydrate_trae_session_from_trace(db: Session, job: Job, round_: TaskRound) -> bool:
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage="session_collected",
        message="Trace text is not trusted as a Trae Session ID source; refusing session recovery from trace evidence.",
        level="warning",
        extra={"source": "trace_evidence_disabled"},
    )
    return False


def _copy_current_turn_gate(data: object) -> dict:
    if not isinstance(data, dict):
        return {"passed": False, "reason": "copy_result_missing", "recoverable": False}
    gate = data.get("current_turn_gate")
    if isinstance(gate, dict) and isinstance(gate.get("passed"), bool):
        reason = str(gate.get("reason") or ("ok" if gate.get("passed") else "current_turn_gate_failed"))
        return {
            **gate,
            "passed": bool(gate.get("passed")),
            "reason": reason,
            "recoverable": bool(gate.get("recoverable")),
        }

    turn = data.get("trae_turn")
    if not isinstance(turn, dict):
        return {"passed": False, "reason": "current_turn_probe_missing", "recoverable": False}
    if turn.get("status") != "found":
        reason = str(turn.get("reason") or "current_turn_missing")
        return {"passed": False, "reason": reason, "recoverable": _recoverable_copy_gate_reason(reason)}
    turn_status = str(turn.get("turn_status") or "")
    if turn_status != "completed":
        return {
            "passed": False,
            "reason": f"trae_turn_not_completed:{turn_status or 'unknown'}",
            "recoverable": True,
        }
    return {
        "passed": True,
        "reason": "ok",
        "recoverable": False,
        "session_id": str(turn.get("session_id") or ""),
        "user_message_id": str(turn.get("user_message_id") or ""),
    }


def _valid_copy_can_override_current_turn_gate(gate: dict, data: object) -> bool:
    if gate.get("passed") is True or not gate.get("recoverable"):
        return False
    reason = str(gate.get("reason") or "")
    if not reason.startswith("trae_turn_not_completed:"):
        return False
    if not isinstance(data, dict):
        return False
    probe = data.get("trace_probe") if isinstance(data.get("trace_probe"), dict) else {}
    if probe.get("complete_like") is not True or str(probe.get("reason") or "") != "ok":
        return False
    if str(data.get("raw_text") or "").strip():
        return True
    return False


def _recoverable_copy_gate_reason(reason: str) -> bool:
    if reason in RECOVERABLE_COPY_GATE_REASONS:
        return True
    return reason.startswith("trae_turn_not_completed:")


def _handle_completed_trace_unavailable(
    db: Session,
    command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    extra: dict,
    raw_trace: str = "",
) -> bool:
    if not _has_completed_observation(command, extra):
        return False

    trace_status = _completed_trace_unavailable_reason(extra)
    completed_extra = {
        **extra,
        "trace_status": trace_status,
        "completion_observation": _completion_observation(command, extra),
        "completed_without_full_trace": True,
    }
    if round_:
        round_.trace_status = trace_status

    if round_ and _test_chain_allowed(job):
        _apply_test_trace_exception(db, job, round_, command, raw_trace)
        job.status = JobState.SCREENSHOT_CAPTURING
        round_.status = JobState.SCREENSHOT_CAPTURING
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage=JobState.SCREENSHOT_CAPTURING,
            message=(
                "Trae was already completed but the full trace could not be copied; "
                "test-chain mode will continue with a labeled trace exception."
            ),
            level="warning",
            extra=completed_extra,
        )
        screenshot_command = _enqueue_worker_command(
            db,
            command,
            WorkerCommandType.CAPTURE_SCREENSHOT,
            {"target": "trae_window", "quality_required": True},
        )
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage=JobState.SCREENSHOT_CAPTURING,
            message="capture_screenshot worker command queued after test trace exception.",
            extra={"worker_id": screenshot_command.worker_id, "command_id": screenshot_command.id},
        )
        return True

    _mark_trace_missing_abort(
        db,
        job,
        round_,
        (
            "Trae was already completed, but Worker could not copy a complete assistant trace. "
            "Downstream review, GitHub, and Feishu writes were stopped instead of clicking continue again."
        ),
        completed_extra,
    )
    return True


def _has_completed_observation(command: WorkerCommand, extra: dict) -> bool:
    observation = _completion_observation(command, extra)
    if not observation:
        return False
    supervisor = observation.get("supervisor_decision") if isinstance(observation.get("supervisor_decision"), dict) else {}
    completion = (
        supervisor.get("trae_turn_completion_decision")
        if isinstance(supervisor.get("trae_turn_completion_decision"), dict)
        else {}
    )
    if str(supervisor.get("action") or "") == "collect_trace":
        return True
    if completion.get("is_complete") is True and str(completion.get("next_action") or "") == "copy_trace":
        return True
    if str(supervisor.get("reason") or "") in {
        "ui_completion_detected",
        "trae_turn_completed",
        "timeout_completion_detected",
        "completion_candidate_with_project_write",
        "completion_evidence_threshold",
        "visual_completion_detected",
    }:
        return True
    return False


def _completion_observation(command: WorkerCommand, extra: dict) -> dict:
    payload_observation = command.payload.get("completion_observation")
    if isinstance(payload_observation, dict) and payload_observation:
        return payload_observation
    extra_observation = extra.get("completion_observation")
    if isinstance(extra_observation, dict) and extra_observation:
        return extra_observation
    return {}


def _completed_trace_unavailable_reason(extra: dict) -> str:
    reason = _trace_copy_retry_reason(extra)
    if reason in TRACE_COPY_RETRY_REASONS or reason == "copy_command_failed":
        return f"{TRACE_UNAVAILABLE_AFTER_COMPLETION}:{reason}"
    gate = extra.get("current_turn_gate")
    if isinstance(gate, dict) and str(gate.get("reason") or "").strip():
        return f"{TRACE_UNAVAILABLE_AFTER_COMPLETION}:{gate['reason']}"
    return TRACE_UNAVAILABLE_AFTER_COMPLETION


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


def _test_chain_allowed(job: Job) -> bool:
    intent = job.intent if isinstance(job.intent, dict) else {}
    return (
        intent.get("run_mode") == "test"
        and intent.get("downstream_policy") == "test_chain_allowed"
        and intent.get("trace_gate_policy") == "test_exception"
    )


def _force_test_unsatisfied(job: Job) -> bool:
    intent = job.intent if isinstance(job.intent, dict) else {}
    return intent.get("run_mode") == "test" and intent.get("dissatisfaction_policy") == "force_test_unsatisfied"


def _test_mode_skips_trae_self_tests(job: Job) -> bool:
    intent = job.intent if isinstance(job.intent, dict) else {}
    return intent.get("run_mode") == "test" and "skip_trae_self_tests" in set(intent.get("flags") or [])


def _apply_test_trace_exception(
    db: Session,
    job: Job,
    round_: TaskRound,
    command: WorkerCommand,
    trace_text: str,
) -> None:
    if round_.trace_status == "test_exception" and _latest_attachment(db, job.id, round_.id, "test_trace_exception"):
        return
    text = (
        "TEST MODE TRACE EXCEPTION\n"
        "This attachment is not a verified raw Trae assistant trace.\n"
        "The user requested a test-chain run to verify GitHub and Feishu automation even when Trae is abnormal.\n\n"
        f"Job: {job.id}\n"
        f"Round: {round_.id}\n"
        f"Blocked command: {command.command_type}\n"
        f"Previous trace status: {round_.trace_status}\n"
        f"Captured trace text:\n{trace_text or '[no raw trace text captured]'}\n\n"
        f"Runtime logs:\n{_runtime_log_text(db, job.id, round_.id)}\n"
    )
    _record_text_attachment(db, command, text, kind="test_trace_exception", filename_prefix="test-trace-exception")
    round_.trace_status = "test_exception"
    _record_dissatisfaction_from_context(
        db,
        job,
        round_,
        JobState.TRACE_MISSING_ABORT,
        "Trae trace was missing, but this run is marked as a test-chain exception.",
        {
            "command_type": command.command_type,
            "result_status": "test_exception",
            "trace_status": "test_exception",
            "data": {"test_mode": True, "intent": job.intent or {}, "trace_chars_before_exception": len(trace_text or "")},
        },
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage="test_chain_exception",
        message="Trae trace gate was bypassed only for a labeled test-chain run; downstream records must be treated as test data.",
        level="warning",
        extra={"command_id": command.id, "command_type": command.command_type, "intent": job.intent or {}},
    )
    _notify_test_chain_exception(db, job, round_, command)


def _notify_test_chain_exception(db: Session, job: Job, round_: TaskRound, command: WorkerCommand) -> None:
    config = load_user_settings(db, job.user_id).get("webhook", {})
    text = (
        "AgentOps 测试链路通知\n"
        f"Job: {job.id}\n"
        f"Round: {round_.id}\n"
        f"命令: {command.command_type}\n"
        "Trae 没有提供可验证的完整轨迹，但当前作业被识别为测试模式。\n"
        "系统将继续验证 GitHub 和飞书链路，后续记录会标记为测试例外，不作为正式验收结论。"
    )
    try:
        result = notify_text(config, text)
    except WebhookNotifyError as exc:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="test_chain_notification",
            message=str(exc),
            level="warning",
            extra={"status": "failed"},
        )
        return
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage="test_chain_notification",
        message="Test-chain exception notification sent.",
        level="warning",
        extra=result,
    )


def _maybe_notify_slow_trae(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    command: WorkerCommand,
    result: WorkerResult,
) -> None:
    if not round_:
        return
    threshold = int(command.payload.get("slow_notify_seconds") or DEFAULT_TRAE_SLOW_NOTIFY_SECONDS)
    elapsed = _wait_elapsed_seconds(command)
    if elapsed < threshold:
        return
    already_sent = db.scalar(
        select(RuntimeLog)
        .where(
            RuntimeLog.job_id == job.id,
            RuntimeLog.round_id == round_.id,
            RuntimeLog.stage == "trae_slow_notification",
        )
        .limit(1)
    )
    if already_sent:
        return
    config = load_user_settings(db, job.user_id).get("webhook", {})
    text = (
        "AgentOps 慢任务提醒\n"
        f"Job: {job.id}\n"
        f"Round: {round_.id}\n"
        f"已等待: {elapsed // 60} 分钟\n"
        f"当前状态: {job.status}\n"
        "Trae 超过 30 分钟还没有完成当前任务，人工可以进入控制台暂停本轮任务或接管处理。"
    )
    try:
        notify_result = notify_text(config, text)
    except WebhookNotifyError as exc:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="trae_slow_notification",
            message=str(exc),
            level="warning",
            extra={"status": "failed", "elapsed_seconds": elapsed, "threshold_seconds": threshold},
        )
        return
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage="trae_slow_notification",
        message="Trae has been running slowly; a Feishu webhook notification was sent.",
        level="warning",
        extra={"elapsed_seconds": elapsed, "threshold_seconds": threshold, **notify_result},
    )


def _wait_elapsed_seconds(command: WorkerCommand) -> int:
    start_epoch = command.payload.get("sent_at_epoch") or command.payload.get("prompt_sent_at_epoch")
    if start_epoch:
        try:
            return max(0, int(datetime.now(timezone.utc).timestamp() - float(start_epoch)))
        except (TypeError, ValueError):
            pass
    created = command.created_at
    if not created:
        return 0
    current = datetime.now(timezone.utc)
    if created.tzinfo is None:
        current = current.replace(tzinfo=None)
    return max(0, int((current - created).total_seconds()))


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
    db.add(
        AutomationError(
            job_id=job.id,
            round_id=round_.id if round_ else None,
            kind="manual_required",
            stage=JobState.MANUAL_REQUIRED,
            message=message,
            details=extra,
        )
    )
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.MANUAL_REQUIRED,
        message=message,
        level="warning" if result_status == "manual_required" else "error",
        extra=extra,
    )
    _notify_manual_required(db, job, round_, message, extra)


def _notify_manual_required(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    message: str,
    extra: dict,
) -> None:
    configs = load_user_settings(db, job.user_id)
    webhook_config = dict(configs.get("webhook", {}))
    if not webhook_config.get("url"):
        return
    try:
        result = notify_manual_required(
            webhook_config,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            message=message,
            details=extra,
        )
    except WebhookNotifyError as exc:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage="manual_required_notification",
            message="Manual-required webhook notification failed.",
            level="warning",
            extra={"error": str(exc)},
        )
        return
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage="manual_required_notification",
        message="Manual-required webhook notification sent.",
        extra=result,
    )


def _store_trae_turn_metadata(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    turn: object,
    *,
    allow_non_completed: bool = False,
) -> None:
    if not round_ or not isinstance(turn, dict):
        return
    if turn.get("status") != "found":
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="session_collected",
            message="Worker did not find a real Trae session id in local logs.",
            level="warning",
            extra={"probe": turn},
        )
        return
    turn_status = str(turn.get("turn_status") or "")
    if turn_status != "completed" and not allow_non_completed:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="session_collected",
            message="Worker found a Trae turn, but it is not a completed current turn.",
            level="warning",
            extra={"probe": turn},
        )
        return
    chat_session_id = str(turn.get("chat_session_id") or turn.get("session_id") or "").strip()
    display_session_id = str(
        turn.get("display_session_id")
        or turn.get("trae_session_display_id")
        or _build_trae_session_display_id(turn)
    ).strip()
    if not chat_session_id:
        return
    if not _is_trusted_trae_session_display_id(display_session_id):
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="session_collected",
            message="Worker found Trae turn metadata, but did not provide the canonical Feishu Trae Session ID.",
            level="warning",
            extra={
                "candidate": display_session_id or chat_session_id,
                "chat_session_id": chat_session_id,
                "user_message_id": str(turn.get("user_message_id") or "").strip(),
                "task_id": str(turn.get("task_id") or "").strip(),
                "trace_id": str(turn.get("trace_id") or "").strip(),
                "reason": "missing_canonical_display_id",
            },
        )
        return
    round_.trae_session_id = display_session_id
    round_.trae_user_message_id = str(turn.get("user_message_id") or "").strip()
    round_.trae_task_id = str(turn.get("task_id") or "").strip()
    round_.trae_trace_id = str(turn.get("trace_id") or "").strip()
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage="session_collected",
        message="Real Trae session metadata collected from worker local logs.",
        extra={
            "session_id": round_.trae_session_id,
            "display_session_id": display_session_id,
            "chat_session_id": chat_session_id,
            "user_message_id": round_.trae_user_message_id,
            "task_id": round_.trae_task_id,
            "trace_id": round_.trae_trace_id,
            "turn_status": turn_status,
            "confidence": turn.get("confidence"),
            "accepted_non_completed_turn": bool(allow_non_completed and turn_status != "completed"),
        },
    )


def _trae_turn_metadata_from_copy_result(data: object, *, allow_candidate: bool = False) -> object:
    if not isinstance(data, dict):
        return {}
    turn = data.get("trae_turn")
    if isinstance(turn, dict) and turn.get("status") == "found":
        return turn
    gate = data.get("current_turn_gate") if isinstance(data.get("current_turn_gate"), dict) else {}
    candidate = gate.get("candidate") if isinstance(gate.get("candidate"), dict) else {}
    if gate.get("passed") is True and str(gate.get("reason") or "") == "completed_turn_candidate" and candidate:
        return {**candidate, "status": "found", "turn_status": candidate.get("turn_status") or "completed"}
    if allow_candidate and candidate:
        return {**candidate, "status": "found", "turn_status": candidate.get("turn_status") or "candidate_only"}
    turn_candidate = turn.get("candidate") if isinstance(turn, dict) and isinstance(turn.get("candidate"), dict) else {}
    if allow_candidate and turn_candidate:
        return {
            **turn_candidate,
            "status": "found",
            "turn_status": turn_candidate.get("turn_status") or turn.get("reason") or "candidate_only",
        }
    if candidate:
        return {"status": "missing", "reason": gate.get("reason") or "candidate_only", "candidate": candidate}
    return turn or {}


def _build_trae_session_display_id(turn: dict[str, object]) -> str:
    trace_ids = turn.get("trace_ids") if isinstance(turn.get("trace_ids"), list) else []
    task_ids = turn.get("task_ids") if isinstance(turn.get("task_ids"), list) else []
    trace_id = str(turn.get("trace_id") or (trace_ids[0] if trace_ids else "") or "").strip()
    session_id = str(turn.get("chat_session_id") or turn.get("session_id") or "").strip()
    task_id = str(turn.get("task_id") or (task_ids[0] if task_ids else "") or "").strip()
    message_id = str(turn.get("user_message_id") or "").strip()
    end_time = _format_trae_session_time(str(turn.get("end_time") or turn.get("start_time") or "").strip())
    if not all([trace_id, session_id, task_id, message_id, end_time]):
        return ""
    return f".696467687743947:{trace_id}_{session_id}.{task_id}.{message_id}:Trae CN.T({end_time})"


def _format_trae_session_time(value: str) -> str:
    if not value:
        return ""
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return value
    return f"{parsed.year}/{parsed.month}/{parsed.day} {parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}"


def _is_trusted_trae_session_display_id(value: str) -> bool:
    return bool(TRAE_SESSION_DISPLAY_RE.match(str(value or "").strip()))


def _latest_session_probe_context(db: Session, job_id: str, round_id: str | None) -> dict:
    query = select(RuntimeLog).where(
        RuntimeLog.job_id == job_id,
        RuntimeLog.stage.in_(["session_collected", "trace_validating", "collecting_trace"]),
    )
    if round_id:
        query = query.where(RuntimeLog.round_id == round_id)
    logs = db.scalars(query.order_by(RuntimeLog.created_at.desc()).limit(12)).all()
    for log in logs:
        extra = log.extra if isinstance(log.extra, dict) else {}
        probe = extra.get("probe") if isinstance(extra.get("probe"), dict) else {}
        if probe:
            return _session_probe_summary(probe)
        data = extra.get("data") if isinstance(extra.get("data"), dict) else {}
        for source in (
            data.get("trae_turn"),
            data.get("current_turn_gate"),
            extra.get("current_turn_gate"),
            data.get("trace_probe"),
            extra.get("trace_probe"),
        ):
            summary = _session_probe_summary(source)
            if summary:
                return summary
    return {}


def _session_probe_summary(source: object) -> dict:
    if not isinstance(source, dict):
        return {}
    candidate = source.get("candidate") if isinstance(source.get("candidate"), dict) else {}
    probe = candidate or source
    keys = [
        "status",
        "reason",
        "session_id",
        "user_message_id",
        "task_id",
        "trace_id",
        "turn_status",
        "workspace_folder",
        "workspace_storage_id",
        "match_score",
        "confidence",
        "probe_scope",
        "log_files_scanned",
        "workspace_count",
        "sent_after_epoch",
    ]
    summary = {key: probe.get(key) for key in keys if probe.get(key) not in (None, "")}
    if candidate:
        summary["candidate_from"] = str(source.get("reason") or source.get("source") or "candidate")
    return summary


def _session_missing_display_message(session_probe: dict) -> str:
    if not session_probe:
        return "没有获取到真实 Trae Session ID，本轮不能提交 GitHub 或写入飞书；请检查 Worker 是否能读取 Trae 本地日志。"
    reason = str(session_probe.get("reason") or "").strip()
    session_id = str(session_probe.get("session_id") or "").strip()
    workspace = str(session_probe.get("workspace_folder") or "").strip()
    score = session_probe.get("match_score")
    pieces = ["没有获取到可信的真实 Trae Session ID，本轮不能提交 GitHub 或写入飞书。"]
    details: list[str] = []
    if reason:
        details.append(f"原因：{reason}")
    if session_id:
        details.append(f"候选：{session_id}")
    if workspace:
        details.append(f"工作区：{workspace}")
    if score not in (None, ""):
        details.append(f"匹配分：{score}")
    if details:
        pieces.append("；".join(details))
    return "".join(pieces)


def _handle_stop_result(db: Session, command: WorkerCommand, result: WorkerResult) -> None:
    job, round_ = _load_job_round(db, command)
    if not job:
        return
    extra = _result_extra(command, result)
    stop_report = _stop_report_from_result(result)
    if stop_report and command.status == "cancelled":
        command.status = "completed"
        command.finished_at = command.finished_at or datetime.now(timezone.utc)
        command.result = result.data
        command.message = result.message or command.message
    if str(job.status) == str(JobState.PAUSED):
        if round_:
            round_.status = JobState.PAUSED
        job.status = JobState.PAUSED
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.PAUSED if str(job.status) == str(JobState.PAUSED) else JobState.STOPPED,
        message=_stop_result_message(stop_report),
        level="info" if result.status in {"ok", "success", "completed"} else "warning",
        extra={**extra, "stop_report_summary": _stop_report_summary(stop_report)},
    )
    if str(job.status) == str(JobState.PAUSED):
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage="pause_confirmed",
            message="User pause completed; scheduler will not issue more work until continue is requested.",
            level="info",
            extra={
                **extra,
                "stop_report_summary": _stop_report_summary(stop_report),
                "display_message": _pause_confirmed_display_message(stop_report),
            },
        )


def _stop_report_from_result(result: WorkerResult) -> dict:
    data = result.data if isinstance(result.data, dict) else {}
    report = data.get("stop_report") if isinstance(data.get("stop_report"), dict) else {}
    return report


def _stop_report_summary(report: dict) -> dict:
    if not isinstance(report, dict):
        return {}
    return {
        "stop_confirmed": bool(report.get("stop_confirmed") or report.get("worker_command_cancelled")),
        "trae_stop_clicked": bool(report.get("trae_stop_clicked")),
        "trae_ui_stopped_verified": bool(report.get("trae_ui_stopped_verified")),
        "still_generating_suspected": bool(report.get("still_generating_suspected")),
        "requires_resume_prompt": bool(report.get("requires_resume_prompt")),
        "local_processes_matched": int(report.get("local_processes_matched") or 0),
        "local_processes_killed": int(report.get("local_processes_killed") or report.get("sandbox_killed") or 0),
        "local_process_kill_errors": int(report.get("local_process_kill_errors") or 0),
        "sandbox_killed": int(report.get("sandbox_killed") or 0),
        "cleanup_status": str(report.get("cleanup_status") or ""),
    }


def _stop_result_message(report: dict) -> str:
    summary = _stop_report_summary(report)
    if summary.get("local_process_kill_errors"):
        return "Worker 已收到暂停请求，但本地进程清理有告警，请检查是否仍有残留执行。"
    if summary.get("trae_stop_clicked") and summary.get("trae_ui_stopped_verified"):
        return "Worker 已停止本机动作，并确认 Trae 生成已停止。"
    if summary.get("trae_stop_clicked"):
        return "Worker 已点击 Trae 停止并清理本机动作，继续时会先发送恢复提示词。"
    if summary.get("local_processes_killed"):
        return "Worker 已停止本地项目或沙箱进程，但未点击到 Trae 的停止按钮。"
    if summary.get("stop_confirmed"):
        return "Worker 已确认停止：没有发现仍需清理的本地动作。"
    return "Worker 停止命令已结束，但没有拿到明确停止确认。"


def _pause_confirmed_display_message(report: dict) -> str:
    summary = _stop_report_summary(report)
    if summary.get("local_process_kill_errors"):
        return "暂停已生效：调度已停止继续下发任务，但本地清理有告警，请检查是否还有残留进程。"
    if summary.get("trae_stop_clicked") and summary.get("trae_ui_stopped_verified"):
        return "已经停了：调度已暂停，Worker 已确认 Trae 生成和本地进程都停止。"
    if summary.get("trae_stop_clicked"):
        return "已经停了：调度已暂停，Worker 已点击 Trae 停止并完成本地清理；继续时会先诊断当前状态。"
    if summary.get("local_processes_killed"):
        return "已经停了：调度已暂停，Worker 已停止相关本地项目进程；继续时会先诊断当前状态。"
    if summary.get("stop_confirmed"):
        return "已经停了：调度已暂停，Worker 没有发现仍需清理的本地动作。"
    return "暂停已生效：调度不会继续下发任务，但 Worker 没有拿到完整停止确认。"


def _should_ignore_worker_result(db: Session, command: WorkerCommand, result: WorkerResult) -> bool:
    if command.command_type == WorkerCommandType.STOP_CURRENT_TASK.value:
        return False
    job, round_ = _load_job_round(db, command)
    if not job:
        return False
    if str(command.status) in IGNORED_RESULT_COMMAND_STATES:
        _record_stale_worker_result(
            db,
            command,
            result,
            job,
            round_,
            "Cancelled worker command returned a late result; scheduler state was preserved.",
        )
        return True
    if str(job.status) in {str(item) for item in TERMINAL_JOB_STATES}:
        _record_stale_worker_result(
            db,
            command,
            result,
            job,
            round_,
            "Worker result arrived after the job was already terminal; no follow-up command was queued.",
        )
        return True
    if str(job.status) in {str(item) for item in PAUSED_STATES}:
        _record_stale_worker_result(
            db,
            command,
            result,
            job,
            round_,
            "Worker result arrived after the job was paused; scheduler state was preserved.",
        )
        return True
    if round_ and str(round_.status) in {str(item) for item in TERMINAL_ROUND_STATES}:
        _record_stale_worker_result(
            db,
            command,
            result,
            job,
            round_,
            "Worker result arrived after the round was already terminal; no follow-up command was queued.",
        )
        return True
    return False


def _record_stale_worker_result(
    db: Session,
    command: WorkerCommand,
    result: WorkerResult,
    job: Job,
    round_: TaskRound | None,
    message: str,
) -> None:
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else command.round_id,
        stage="stale_worker_result_ignored",
        message=message,
        level="warning",
        extra={
            **_result_extra(command, result),
            "job_status": str(job.status),
            "round_status": str(round_.status) if round_ else "",
        },
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
    payload = _merge_round_project_context(db, source_command, payload)
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
        "prompt",
        "trae_workspace_path",
        "workspace_path",
        "workspace_root",
        "project_name",
        "project_slug",
        "job_id",
        "round_id",
        "round_index",
        "directions",
        "url",
        "browser_url",
        "acceptance_url",
        "github_push",
        "github_remote",
        "github_remote_url",
        "github_repo_name",
        "github_branch",
        "sent_at_epoch",
        "sent_at",
        "prompt_sent_at_epoch",
        "prompt_sent_at",
    ):
        if key in source_command.payload and key not in result:
            result[key] = source_command.payload[key]
    for key in (
        "continue_attempts",
        "max_continue_attempts",
        "wait_observation_attempts",
        "max_wait_observation_attempts",
        "wait_timeout_seconds",
        "stable_seconds",
        "poll_interval_seconds",
        "intervention_idle_seconds",
        "max_interventions",
        "copy_timeout_seconds",
        "continue_timeout_seconds",
        "browser_acceptance_timeout_seconds",
        "github_timeout_seconds",
    ):
        if key in source_command.payload and key not in result:
            result[key] = source_command.payload[key]
    _normalize_workspace_context(result)
    return result


def _merge_round_project_context(db: Session, source_command: WorkerCommand, payload: dict) -> dict:
    result = dict(payload)
    if result.get("trae_workspace_path") or result.get("workspace_path"):
        _normalize_workspace_context(result)
        return result

    round_id = source_command.round_id or result.get("round_id")
    if not round_id:
        return result
    round_ = db.get(TaskRound, str(round_id))
    if not round_ or not round_.project_id:
        return result
    project = db.get(Project, round_.project_id)
    workspace_path = str(project.workspace_path or "").strip() if project else ""
    if not workspace_path:
        return result
    result.setdefault("workspace_path", workspace_path)
    result.setdefault("trae_workspace_path", workspace_path)
    if project:
        result.setdefault("project_name", project.name)
    result.setdefault("round_index", round_.round_index)
    _normalize_workspace_context(result)
    return result


def _normalize_workspace_context(payload: dict) -> None:
    workspace_path = str(payload.get("trae_workspace_path") or payload.get("workspace_path") or "").strip()
    if not workspace_path:
        return
    payload.setdefault("trae_workspace_path", workspace_path)
    payload.setdefault("workspace_path", workspace_path)


def _recommended_commands(data: dict) -> list[list[str]]:
    commands = data.get("recommended_commands")
    if not isinstance(commands, list):
        return []
    result: list[list[str]] = []
    for command in commands:
        if isinstance(command, list) and all(isinstance(item, str) for item in command):
            result.append(command)
    return result


def _remaining_commands(payload: dict) -> list[list[str]]:
    commands = payload.get("remaining_commands") if isinstance(payload, dict) else []
    if not isinstance(commands, list):
        return []
    result: list[list[str]] = []
    for command in commands:
        if isinstance(command, list) and all(isinstance(item, str) for item in command):
            result.append(command)
    return result


def _product_review_from_data(data: dict) -> dict:
    review = data.get("product_review") if isinstance(data, dict) else {}
    return review if isinstance(review, dict) else {}


def _product_review_has_blocking(product_review: dict | None) -> bool:
    if not isinstance(product_review, dict):
        return False
    issues = product_review.get("issues")
    return isinstance(issues, list) and bool(issues)


def _record_product_review_evidence(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    command: WorkerCommand,
    message: str,
    extra: dict,
    *,
    product_review: dict | None = None,
    result: WorkerResult | None = None,
) -> None:
    data = dict(result.data) if result and isinstance(result.data, dict) else {}
    if product_review:
        data["product_review"] = product_review
    review_data = {
        "status": "static_review_failed",
        "message": message,
        "product_review": product_review or data.get("product_review") or {},
        "issues": (product_review or {}).get("issues") if isinstance(product_review, dict) else [],
        "warnings": (product_review or {}).get("warnings") if isinstance(product_review, dict) else [],
        "evidence": (product_review or {}).get("evidence") if isinstance(product_review, dict) else [],
        "data": data,
    }
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage="product_review_issue_evidence",
        message=message,
        level="warning",
        extra={**extra, "command_type": command.command_type, "result_status": "static_review_failed", "data": review_data},
    )


def _has_pending_product_review_evidence(db: Session, job_id: str, round_id: str | None) -> bool:
    if not round_id:
        return False
    return bool(
        db.scalar(
            select(RuntimeLog.id)
            .where(
                RuntimeLog.job_id == job_id,
                RuntimeLog.round_id == round_id,
                RuntimeLog.stage == "product_review_issue_evidence",
            )
            .limit(1)
        )
    )


def _latest_product_review_evidence(db: Session, job_id: str, round_id: str | None) -> RuntimeLog | None:
    if not round_id:
        return None
    return db.scalar(
        select(RuntimeLog)
        .where(
            RuntimeLog.job_id == job_id,
            RuntimeLog.round_id == round_id,
            RuntimeLog.stage == "product_review_issue_evidence",
        )
        .order_by(RuntimeLog.created_at.desc())
        .limit(1)
    )


def _merge_product_review_evidence_for_final_reason(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    command: WorkerCommand,
    result: WorkerResult,
    extra: dict,
) -> tuple[WorkerResult, dict]:
    evidence = _latest_product_review_evidence(db, job.id, round_.id if round_ else None)
    if not evidence or not isinstance(evidence.extra, dict):
        return result, extra
    evidence_data = evidence.extra.get("data") if isinstance(evidence.extra.get("data"), dict) else {}
    review_data = evidence_data.get("data") if isinstance(evidence_data.get("data"), dict) else {}
    merged_data = dict(review_data)
    merged_data["browser_acceptance"] = dict(result.data or {})
    if evidence_data.get("product_review") and "product_review" not in merged_data:
        merged_data["product_review"] = evidence_data["product_review"]
    merged_result = WorkerResult(
        command_id=result.command_id,
        worker_id=result.worker_id,
        lease_id=result.lease_id,
        status="failed",
        message=result.message,
        error=result.error,
        data=merged_data,
    )
    merged_extra = {
        **extra,
        "product_review_issue_evidence_log_id": evidence.id,
        "data": merged_data,
    }
    return merged_result, merged_extra


def _notify_feishu_write_failed(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    message: str,
    extra: dict,
) -> None:
    webhook_config = dict(load_user_settings(db, job.user_id).get("webhook", {}))
    if not webhook_config.get("url"):
        return
    if round_ and db.scalar(
        select(RuntimeLog.id)
        .where(
            RuntimeLog.job_id == job.id,
            RuntimeLog.round_id == round_.id,
            RuntimeLog.stage == "feishu_failure_notification",
        )
        .limit(1)
    ):
        return
    text = _feishu_failure_notification_text(job, round_, message, extra)
    try:
        result = notify_text(webhook_config, text)
    except WebhookNotifyError as exc:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id if round_ else None,
            stage="feishu_failure_notification",
            message="Feishu failure webhook notification failed.",
            level="warning",
            extra={"error": str(exc)},
        )
        return
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage="feishu_failure_notification",
        message="Feishu failure webhook notification sent.",
        level="warning",
        extra=result,
    )


def _feishu_failure_notification_text(job: Job, round_: TaskRound | None, message: str, extra: dict) -> str:
    prompt = str(round_.prompt or "").strip() if round_ else ""
    operation = str(extra.get("operation") or "")
    status = str(extra.get("http_status") or "")
    code = str(extra.get("feishu_code") or "")
    lines = [
        "AgentOps 飞书写入失败，需要处理",
        f"Job: {job.id}",
        f"Round: {round_.id if round_ else '-'}",
        f"原因: {message}",
    ]
    if operation:
        lines.append(f"飞书接口步骤: {operation}")
    if status or code:
        lines.append(f"状态码: HTTP {status or '-'} / code {code or '-'}")
    if prompt:
        lines.append(f"需求: {prompt[:500]}")
    lines.append("处理建议: 检查当前用户飞书 App ID/Secret、OAuth token、Base/Table 协作者和可编辑权限后重试。")
    return "\n".join(lines)


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
) -> dict[str, object]:
    if not round_.trae_session_id:
        raise FeishuWriteError("Real Trae Session ID is missing; refusing to write Feishu business record.")
    screenshot = _latest_attachment(db, job.id, round_.id, "screenshot")
    log_trace, attachment_paths = _feishu_trace_field_and_attachments(db, job, round_)
    if not log_trace.strip():
        raise FeishuWriteError("Verified Trae assistant trace is missing; refusing to write Feishu business record.")
    git_data = git_result.data or {}
    github_url = _github_url(git_data)
    commit_sha = str(git_data.get("commit_sha") or "")
    branch_or_files = str(git_data.get("pushed_branch") or git_data.get("branch") or commit_sha or "")
    if screenshot:
        attachment_paths.append(screenshot.path)
    satisfaction = _feishu_satisfaction(db, job.id, round_.id)
    fields = {
        "Trae Session ID": _session_id(job, round_),
        "轮次": _round_label(round_.round_index),
        "User Prompt": round_.prompt or "",
        "任务类型": _infer_feishu_task_type(job, round_),
        "业务领域": _infer_feishu_business_domain(job, round_),
        "修改范围": _infer_feishu_change_scope(git_data),
        "任务是否完成": "完成了任务" if satisfaction["satisfied"] else "未完成任务",
        "产物及过程是否满意": "满意" if satisfaction["satisfied"] else "不满意",
        "不满意原因": satisfaction["reason"],
        "github地址": github_url,
        "commit id": commit_sha,
        "分支/文件夹": branch_or_files,
        "日志轨迹": log_trace,
    }
    intent = job.intent if isinstance(job.intent, dict) else {}
    if intent.get("run_mode") == "test":
        fields["不满意原因"] = (
            f"{fields['不满意原因']}\n"
            "测试说明：本条记录来自测试模式，用于验证 AgentOps 的 GitHub/飞书链路，不作为正式业务验收结论。"
        ).strip()
    if attachment_paths:
        fields[FEISHU_ATTACHMENT_FIELD] = attachment_paths
    return fields


def _feishu_trace_field_and_attachments(db: Session, job: Job, round_: TaskRound) -> tuple[str, list[str]]:
    attachment = _latest_attachment(db, job.id, round_.id, "trace")
    is_test_exception = False
    if not attachment and _test_chain_allowed(job) and round_.trace_status == "test_exception":
        attachment = _latest_attachment(db, job.id, round_.id, "test_trace_exception")
        is_test_exception = attachment is not None
    if not attachment:
        return "", []
    path = Path(attachment.path)
    if not path.exists():
        return "", []
    text = path.read_text(encoding="utf-8")
    if is_test_exception:
        text = (
            "TEST MODE TRACE EXCEPTION - not a formal business acceptance trace.\n"
            "This row was written only to validate AgentOps GitHub/Feishu automation.\n\n"
            f"{text}"
        )
    if _is_invalid_business_trace(text):
        return "", []
    if len(text) > LOG_TRACE_FIELD_SOFT_LIMIT:
        return LOG_TRACE_OVERFLOW_TEXT, [str(path)]
    return text, []


def _is_invalid_business_trace(text: str) -> bool:
    return any(pattern.search(str(text or "")) for pattern in INVALID_BUSINESS_TRACE_PATTERNS)


def _latest_trace_text(db: Session, job_id: str, round_id: str, max_chars: int = 18000) -> str:
    attachment = _latest_attachment(db, job_id, round_id, "trace")
    if not attachment:
        return ""
    return _attachment_text(attachment, max_chars=max_chars)


def _attachment_text(attachment: Attachment, max_chars: int = 18000) -> str:
    path = Path(attachment.path)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if max_chars and max_chars > 0:
        return text[-max_chars:]
    return text


def _latest_attachment(db: Session, job_id: str, round_id: str, kind: str) -> Attachment | None:
    attachments = list(
        db.scalars(
            select(Attachment)
            .where(Attachment.job_id == job_id, Attachment.round_id == round_id, Attachment.kind == kind)
            .order_by(Attachment.created_at.desc())
        ).all()
    )
    if not attachments:
        return None
    return max(attachments, key=_attachment_freshness_key)


def _attachment_freshness_key(attachment: Attachment) -> tuple[float, float, str]:
    created = attachment.created_at.timestamp() if attachment.created_at else 0.0
    try:
        file_mtime = Path(str(attachment.path or "")).stat().st_mtime
    except OSError:
        file_mtime = 0.0
    return (created, file_mtime, str(attachment.id or ""))


def _runtime_log_text(db: Session, job_id: str, round_id: str) -> str:
    rows = list(
        db.scalars(
            select(RuntimeLog)
            .where(RuntimeLog.job_id == job_id, RuntimeLog.round_id == round_id)
            .order_by(RuntimeLog.created_at)
            .limit(120)
        ).all()
    )
    return "\n".join(f"[{item.stage}] {item.display_message or item.message}" for item in rows)[-18000:]


def _github_url(git_data: dict) -> str:
    for key in ("remote_url", "github_url", "repository_url"):
        normalized = _normalize_github_clone_url(str(git_data.get(key) or ""))
        if normalized:
            return normalized
    return str(git_data.get("commit_sha") or "")


def _normalize_github_clone_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("git@github.com:"):
        path = text.removeprefix("git@github.com:").strip("/")
        return f"https://github.com/{path.removesuffix('.git')}.git" if path else ""
    marker = "github.com/"
    if marker not in text:
        return text
    path = text.split(marker, 1)[1]
    path = path.split("/commit/", 1)[0]
    path = path.split("/tree/", 1)[0]
    path = path.split("/pull/", 1)[0]
    path = path.strip("/")
    if not path:
        return ""
    return f"https://github.com/{path.removesuffix('.git')}.git"


def _session_id(job: Job, round_: TaskRound) -> str:
    session_id = round_.trae_session_id or ""
    if not _is_trusted_trae_session_display_id(session_id):
        raise FeishuWriteError("Canonical Trae Session ID is missing; refusing to write Feishu business record.")
    return session_id


def _first_direction(job: Job) -> str:
    if isinstance(job.directions, list) and job.directions:
        return str(job.directions[0])
    return ""


def _round_label(round_index: int) -> str:
    labels = ["第一轮", "第二轮", "第三轮", "第四轮", "第五轮"]
    if 1 <= round_index <= len(labels):
        return labels[round_index - 1]
    return f"第{round_index}轮"


def _infer_feishu_task_type(job: Job, round_: TaskRound) -> str:
    intent = job.intent if isinstance(job.intent, dict) else {}
    if round_.round_index <= 1 and intent.get("start_mode") == "fresh_start":
        return "0-1代码生成"
    text = f"{_first_direction(job)} {round_.prompt or ''}".lower()
    if any(item in text for item in ["bug", "修复", "报错", "异常"]):
        return "Bug修复"
    if any(item in text for item in ["重构", "refactor"]):
        return "代码重构"
    if any(item in text for item in ["测试", "test"]):
        return "代码测试"
    if round_.round_index > 1:
        return "Feature迭代"
    if any(item in text for item in ["新增", "增加", "添加", "feature", "迭代", "优化"]):
        return "Feature迭代"
    if any(item in text for item in ["脚本", "自动化", "worker", "部署", "打包", "命令"]):
        return "工程化"
    return "0-1代码生成"


def _infer_feishu_business_domain(job: Job, round_: TaskRound) -> str:
    text = f"{_first_direction(job)} {round_.prompt or ''}".lower()
    if _looks_like_agentops_fullstack(text):
        return "全栈Web应用"
    front = any(item in text for item in ["前端", "页面", "ui", "vue", "react", "vite", "浏览器", "控制台", "看板"])
    back = any(item in text for item in ["api", "后端", "接口", "数据库", "服务端", "server", "postgres", "redis"])
    if front and back:
        return "全栈Web应用"
    if any(item in text for item in ["前端", "页面", "ui", "vue", "react", "vite"]):
        return "Web前端"
    if any(item in text for item in ["api", "后端", "接口", "数据库"]):
        return "纯后端API服务"
    if any(item in text for item in ["游戏", "game"]):
        return "游戏开发"
    if any(item in text for item in ["3d", "three", "可视化"]):
        return "3D/交互可视化"
    if any(item in text for item in ["脚本", "自动化", "worker", "打包", "部署", "命令"]):
        return "自动化与工具脚本"
    return "全栈Web应用"


def _looks_like_agentops_fullstack(text: str) -> bool:
    terms = (
        "agentops",
        "自动作业平台",
        "多角色",
        "角色工作台",
        "任务看板",
        "作业控制台",
        "trae控制",
        "trae 控制",
        "worker",
        "飞书",
        "github",
    )
    return any(term in text for term in terms) and any(
        term in text for term in ("平台", "控制台", "看板", "配置", "api", "worker", "飞书", "github")
    )


def _infer_feishu_change_scope(git_data: dict) -> str:
    files = git_data.get("files") or git_data.get("changed_files") or []
    if isinstance(files, int):
        if files <= 0:
            return "模块内多文件"
        if files == 1:
            return "单文件"
        if files <= 5:
            return "模块内多文件"
        return "跨模块多文件"
    if isinstance(files, list):
        count = len(files)
        if count <= 0:
            return "模块内多文件"
        if count == 1:
            return "单文件"
        if count <= 5:
            return "模块内多文件"
        return "跨模块多文件"
    if git_data.get("commit_sha"):
        return "模块内多文件"
    return "模块内多文件"


def _feishu_satisfaction(db: Session, job_id: str, round_id: str) -> dict[str, str | bool]:
    reason = db.scalar(
        select(RuntimeLog)
        .where(RuntimeLog.job_id == job_id, RuntimeLog.round_id == round_id, RuntimeLog.stage == "dissatisfaction_reason")
        .order_by(RuntimeLog.created_at.desc())
        .limit(1)
    )
    if not reason:
        return {"satisfied": True, "reason": ""}
    text = str(reason.message or "")
    extra_reason = ""
    if isinstance(reason.extra, dict):
        extra_reason = str(
            reason.extra.get("reason")
            or reason.extra.get("product_reason")
            or reason.extra.get("process_reason")
            or ""
        )
    return {"satisfied": False, "reason": (extra_reason or text).strip()}


def _commit_message(job: Job, round_: TaskRound | None) -> str:
    direction = ""
    if isinstance(job.directions, list) and job.directions:
        direction = str(job.directions[0]).strip()
    round_label = f"round {round_.round_index}" if round_ else "round"
    prefix = "TEST AgentOps: " if isinstance(job.intent, dict) and job.intent.get("run_mode") == "test" else "AgentOps: "
    if direction:
        return f"{prefix}{direction[:80]} ({round_label})"
    return f"{prefix}automated update ({round_label})"


def _workspace_path(command: WorkerCommand, data: dict) -> str:
    return str(
        command.payload.get("trae_workspace_path")
        or command.payload.get("workspace_path")
        or data.get("root")
        or ""
    )


def _recommended_command_cwd(command: WorkerCommand, data: dict) -> str:
    return str(data.get("recommended_command_cwd") or data.get("project_root") or _workspace_path(command, data))


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
