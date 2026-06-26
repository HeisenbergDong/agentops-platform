import ctypes
import time
from pathlib import Path
from typing import Any, Callable

from worker.system.clipboard import ClipboardError, set_clipboard_text
from worker.trae import ui_cache
from worker.trae.session_probe import probe_latest_trae_turn
from worker.trae.screenshot import capture_screenshot
from worker.trae.ui_locator import locate_prompt_targets, target_for_action, validate_target
from worker.trae.window import TraeAutomationError, focus_trae, wait_for_workspace_window_or_any

PROMPT_INPUT_X_RATIO = 0.26
PROMPT_INPUT_Y_RATIO = 0.88
PROMPT_SEND_X_RATIO = 0.364
PROMPT_SEND_Y_RATIO = 0.945
SOLO_INPUT_CENTER_X_RATIO = PROMPT_INPUT_X_RATIO
SOLO_INPUT_CENTER_Y_RATIO = PROMPT_INPUT_Y_RATIO
SOLO_INPUT_LEFT_MAX_RATIO = 0.40
SOLO_INPUT_TOP_MIN_RATIO = 0.70
SOLO_INPUT_BOTTOM_MAX_RATIO = 0.985
PROMPT_INPUT_MIN_WIDTH = 80
PROMPT_INPUT_MIN_HEIGHT = 12
PROMPT_INPUT_CANDIDATE_LIMIT = 2
SUBMISSION_PROBE_INTERVAL_SECONDS = 0.75
NEW_TASK_SETTLE_SECONDS = 0.8
COMPOSER_READY_TIMEOUT_SECONDS = 15.0
COMPOSER_READY_INTERVAL_SECONDS = 0.8
PROMPT_PROGRESS_INTERVAL_SECONDS = 5.0
VISUAL_SUBMISSION_SETTLE_SECONDS = 1.2
VISUAL_SUBMISSION_PASS_STATES = {
    "prompt_submitted",
    "generating",
    "still_generating",
    "awaiting_run_confirmation",
    "awaiting_confirm",
    "awaiting_keep_changes",
    "awaiting_save",
    "awaiting_continue",
    "service_interrupted",
    "model_error_3003",
    "terminal_prompt",
}
VISUAL_SUBMISSION_FAILURE_STATES = {"prompt_still_in_composer", "prompt_not_submitted"}
PROMPT_INPUT_NAME_MARKERS = (
    "ask",
    "chat",
    "message",
    "prompt",
    "trae",
    "input",
    "send",
    "\u8f93\u5165",
    "\u53d1\u9001",
    "\u63d0\u95ee",
)
STOP_SEND_BUTTON_MARKERS = (
    "stop",
    "stop generating",
    "cancel generation",
    "停止",
    "停止生成",
)


class PromptSendError(RuntimeError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def send_prompt(
    prompt: str,
    submit: bool = True,
    submit_hotkey: str = "{ENTER}",
    workspace_path: str | Path | None = None,
    verify_submission: bool = False,
    strict_submission_verification: bool = True,
    sent_at_epoch: float | None = None,
    submission_timeout_seconds: float = 15.0,
    ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    open_new_task: bool = False,
    verify_visual_submission: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    prompt = prompt.strip()
    if not prompt:
        raise PromptSendError("Prompt is empty")

    try:
        _emit_prompt_progress(
            progress_callback,
            "focus_trae",
            "Worker is focusing Trae before sending the prompt.",
            workspace_path=str(workspace_path or ""),
        )
        focus_result = _focus_trae_for_prompt(workspace_path)
        window = wait_for_workspace_window_or_any(
            timeout_seconds=3.0,
            workspace_path=workspace_path,
            prefer_workspace_match=bool(workspace_path),
        )
    except TraeAutomationError as exc:
        raise PromptSendError(
            str(exc),
            {
                "stage": "focus_trae_failed",
                "workspace_path": str(workspace_path or ""),
            },
        ) from exc

    try:
        set_clipboard_text(prompt)
    except ClipboardError as exc:
        raise PromptSendError(str(exc)) from exc

    new_task_result = _open_new_task_composer() if open_new_task else {"status": "skipped"}
    composer_ready: dict[str, Any] = {"status": "skipped"}
    if new_task_result.get("status") == "sent":
        _emit_prompt_progress(
            progress_callback,
            "open_new_task",
            "Worker opened a new Trae task and is waiting for the composer.",
            method=str(new_task_result.get("method") or ""),
        )
        window = wait_for_workspace_window_or_any(
            timeout_seconds=3.0,
            workspace_path=workspace_path,
            prefer_workspace_match=bool(workspace_path),
        )
    window_rect = _window_rect(int(getattr(window, "hwnd", 0) or 0))
    if new_task_result.get("status") == "sent" and window_rect:
        composer_ready = _wait_for_composer_ready(
            window=window,
            window_rect=window_rect,
            workspace_path=workspace_path,
            submit=submit,
            ui_analyst=ui_analyst,
            window_title=str(focus_result.get("window_title") or ""),
            progress_callback=progress_callback,
        )
    if not window_rect:
        input_result = _focus_prompt_input(window)
        _send_keys("^a")
        _send_keys("{BACKSPACE}")
        _send_keys("^v")
        if submit:
            _send_keys(submit_hotkey)
        submission = {}
        if submit and verify_submission:
            try:
                submission = _verify_prompt_submission(
                    prompt=prompt,
                    workspace_path=workspace_path,
                    sent_at_epoch=sent_at_epoch,
                    timeout_seconds=submission_timeout_seconds,
                )
            except PromptSendError as exc:
                if strict_submission_verification:
                    raise
                submission = _unconfirmed_submission(exc)
        return {
            "status": "sent",
            "chars": len(prompt),
            "submitted": submit,
            "submit_hotkey": submit_hotkey if submit else "",
            "submit_method": submit_hotkey if submit else "",
            "submit": {"method": submit_hotkey} if submit else {},
            "window_title": focus_result.get("window_title", ""),
            "workspace_match": focus_result.get("workspace_match", False),
            "input": input_result,
            "submission": submission,
            "automation": {"strategy": "uia_no_window_rect", "new_task": new_task_result},
        }

    attempt_result = _send_prompt_with_adaptive_targets(
        prompt=prompt,
        submit=submit,
        verify_submission=verify_submission,
        workspace_path=workspace_path,
        sent_at_epoch=sent_at_epoch,
        submission_timeout_seconds=submission_timeout_seconds,
        strict_submission_verification=strict_submission_verification,
        window_rect=window_rect,
        window_title=str(focus_result.get("window_title") or ""),
        ui_analyst=ui_analyst,
        new_task_result=new_task_result,
        ready_target_set=composer_ready.get("target_set") if isinstance(composer_ready, dict) else None,
        verify_visual_submission=verify_visual_submission,
        progress_callback=progress_callback,
    )
    attempt_result.setdefault("automation", {})["composer_ready"] = composer_ready
    if attempt_result.get("status") == "failed":
        details = {
            "stage": str(attempt_result.get("reason") or "trae_prompt_send_failed"),
            "attempt": attempt_result,
        }
        raise PromptSendError(str(attempt_result.get("error") or attempt_result.get("reason") or "Trae prompt send failed"), details)
    return {
        "status": "sent",
        "chars": len(prompt),
        "submitted": submit,
        "submit_hotkey": submit_hotkey if submit else "",
        "submit_method": attempt_result.get("submit", {}).get("method", "") if submit else "",
        "submit": attempt_result.get("submit", {}),
        "window_title": focus_result.get("window_title", ""),
        "workspace_match": focus_result.get("workspace_match", False),
        "input": attempt_result.get("input", {}),
        "submission": attempt_result.get("submission", {}),
        "automation": attempt_result.get("automation", {}),
    }


def _focus_trae_for_prompt(workspace_path: str | Path | None) -> dict:
    try:
        return focus_trae(
            workspace_path=workspace_path,
            require_workspace_match=bool(workspace_path),
        )
    except TraeAutomationError as exc:
        if not workspace_path:
            raise
        fallback = focus_trae(
            workspace_path=workspace_path,
            require_workspace_match=False,
        )
        fallback["workspace_focus_fallback"] = {
            "requested_workspace_path": str(workspace_path),
            "reason": str(exc),
        }
        return fallback


def _verify_prompt_submission(
    prompt: str,
    workspace_path: str | Path | None,
    sent_at_epoch: float | None,
    timeout_seconds: float,
) -> dict:
    started = float(sent_at_epoch or time.time())
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    last_probe: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_probe = probe_latest_trae_turn(
            prompt=prompt,
            workspace_path=str(workspace_path or ""),
            sent_after_epoch=started,
        )
        if _submission_probe_passed(last_probe):
            return {
                "status": "confirmed",
                "probe": _compact_submission_probe(last_probe),
                "sent_after_epoch": started,
            }
        time.sleep(SUBMISSION_PROBE_INTERVAL_SECONDS)
    compact = _compact_submission_probe(last_probe)
    raise PromptSendError(
        "Prompt was pasted/submitted, but no new Trae user turn was detected. "
        f"submission_probe={compact}",
        {"submission_probe": compact},
    )


def _submission_probe_passed(probe: dict[str, Any]) -> bool:
    if probe.get("status") == "found":
        return True
    if probe.get("status") == "missing" and probe.get("reason") == "awaiting_current_continuation":
        return bool(probe.get("candidate"))
    return False


def _compact_submission_probe(probe: dict[str, Any]) -> dict:
    if not isinstance(probe, dict):
        return {}
    candidate = probe.get("candidate") if isinstance(probe.get("candidate"), dict) else {}
    result: dict[str, Any] = {
        "status": str(probe.get("status") or ""),
        "reason": str(probe.get("reason") or ""),
        "probe_scope": str(probe.get("probe_scope") or ""),
        "workspace_count": probe.get("workspace_count", 0),
        "log_files_scanned": probe.get("log_files_scanned", 0),
    }
    if candidate:
        result["candidate"] = {
            "session_id": str(candidate.get("session_id") or ""),
            "user_message_id": str(candidate.get("user_message_id") or ""),
            "turn_status": str(candidate.get("turn_status") or ""),
            "start_time": str(candidate.get("start_time") or ""),
            "end_time": str(candidate.get("end_time") or ""),
            "workspace_folder": str(candidate.get("workspace_folder") or ""),
            "match_score": candidate.get("match_score", 0),
        }
    return result


def _verify_prompt_submission_visually(
    *,
    prompt: str,
    workspace_path: str | Path | None,
    window_rect: tuple[int, int, int, int],
    window_title: str,
    ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None,
    probe_error: PromptSendError | None = None,
) -> dict[str, Any]:
    time.sleep(VISUAL_SUBMISSION_SETTLE_SECONDS)
    screenshot_info = _capture_ui_analysis_screenshot(workspace_path=workspace_path)
    analysis_rect = _screenshot_window_rect(screenshot_info) or window_rect
    local_analysis: dict[str, Any] = {}
    if screenshot_info.get("path"):
        local_analysis = locate_prompt_targets(screenshot_info["path"], analysis_rect)
    local_state = _local_visual_submission_state(local_analysis)
    if local_state["status"] == "failed":
        raise PromptSendError(
            "Prompt still appears to be in the Trae composer after clicking send.",
            {
                "stage": "visual_submission_failed",
                "reason": local_state["reason"],
                "submission_probe": _compact_submission_probe(probe_error.details.get("submission_probe", {}))
                if probe_error
                else {},
                "screenshot": screenshot_info,
                "local_analysis": local_analysis,
            },
        )

    ai_analysis: dict[str, Any] = {}
    ai_error = ""
    if ui_analyst and screenshot_info.get("path"):
        try:
            response = ui_analyst(
                str(screenshot_info["path"]),
                _submission_analysis_context(
                    prompt=prompt,
                    window_rect=analysis_rect,
                    window_title=window_title,
                    workspace_path=workspace_path,
                    local_analysis=local_analysis,
                    probe_error=probe_error,
                ),
            )
            ai_analysis = response.get("analysis") if isinstance(response, dict) else {}
            if not isinstance(ai_analysis, dict):
                ai_analysis = {}
        except Exception as exc:
            ai_error = str(exc)
        ai_state = _ai_visual_submission_state(ai_analysis)
        if ai_state["status"] == "confirmed":
            return {
                "status": "visually_confirmed",
                "source": "ai_vision",
                "screen_state": ai_state["screen_state"],
                "reason": ai_state["reason"],
                "screenshot": screenshot_info,
                "local_analysis": local_analysis,
                "ai_analysis": ai_analysis,
                "probe": _probe_error_summary(probe_error),
            }
        if ai_state["status"] == "failed":
            raise PromptSendError(
                "Visual analysis indicates the prompt was not submitted to Trae.",
                {
                    "stage": "visual_submission_failed",
                    "reason": ai_state["reason"],
                    "screen_state": ai_state["screen_state"],
                    "submission_probe": _probe_error_summary(probe_error),
                    "screenshot": screenshot_info,
                    "local_analysis": local_analysis,
                    "ai_analysis": ai_analysis,
                },
            )

    if local_state["status"] == "confirmed":
        return {
            "status": "visually_confirmed",
            "source": "local_vision",
            "reason": local_state["reason"],
            "screenshot": screenshot_info,
            "local_analysis": local_analysis,
            "ai_analysis": ai_analysis,
            "ai_error": ai_error,
            "probe": _probe_error_summary(probe_error),
        }

    if probe_error:
        raise PromptSendError(
            "Prompt was clicked in Trae, but neither Trae logs nor visual analysis confirmed submission.",
            {
                "stage": "visual_submission_unconfirmed",
                "submission_probe": _probe_error_summary(probe_error),
                "screenshot": screenshot_info,
                "local_analysis": local_analysis,
                "ai_analysis": ai_analysis,
                "ai_error": ai_error,
            },
        )

    return {
        "status": "visually_unconfirmed",
        "source": "visual_fallback",
        "reason": "no_failure_detected",
        "screenshot": screenshot_info,
        "local_analysis": local_analysis,
        "ai_analysis": ai_analysis,
        "ai_error": ai_error,
    }


def _local_visual_submission_state(local_analysis: dict[str, Any]) -> dict[str, str]:
    targets = local_analysis.get("targets") if isinstance(local_analysis.get("targets"), list) else []
    if not targets:
        return {"status": "unknown", "reason": "no_local_prompt_targets_detected"}
    send_target = target_for_action(local_analysis, "send_button", min_confidence=0.55)
    input_target = target_for_action(local_analysis, "prompt_input", min_confidence=0.55)
    if send_target and input_target:
        return {"status": "failed", "reason": "composer_still_has_active_send_button"}
    if not send_target:
        return {"status": "confirmed", "reason": "active_send_button_disappeared"}
    return {"status": "unknown", "reason": "send_button_visible_without_confirmed_input"}


def _ai_visual_submission_state(ai_analysis: dict[str, Any]) -> dict[str, str]:
    if not isinstance(ai_analysis, dict) or not ai_analysis:
        return {"status": "unknown", "screen_state": "", "reason": "missing_ai_analysis"}
    screen_state = str(ai_analysis.get("screen_state") or "")
    recommended_action = str(ai_analysis.get("recommended_action") or "")
    reason = str(ai_analysis.get("reason") or ai_analysis.get("blocked_reason") or "")
    if screen_state in VISUAL_SUBMISSION_PASS_STATES or recommended_action in {"wait", "click_run_button", "click_confirm_button"}:
        return {"status": "confirmed", "screen_state": screen_state, "reason": reason or "ai_visual_confirmed"}
    if screen_state in VISUAL_SUBMISSION_FAILURE_STATES:
        return {"status": "failed", "screen_state": screen_state, "reason": reason or screen_state}
    targets = ai_analysis.get("targets") if isinstance(ai_analysis.get("targets"), list) else []
    actions = {str(item.get("action") or "") for item in targets if isinstance(item, dict)}
    if {"prompt_input", "send_button"}.issubset(actions):
        return {"status": "failed", "screen_state": screen_state, "reason": "ai_detected_prompt_input_and_send_button"}
    return {"status": "unknown", "screen_state": screen_state, "reason": reason or "ai_visual_unknown"}


def _probe_error_summary(error: PromptSendError | None) -> dict[str, Any]:
    if not error:
        return {}
    details = error.details if isinstance(error.details, dict) else {}
    probe = details.get("submission_probe") if isinstance(details.get("submission_probe"), dict) else {}
    return _compact_submission_probe(probe) if probe else {"error": str(error)}


def _send_prompt_with_adaptive_targets(
    *,
    prompt: str,
    submit: bool,
    verify_submission: bool,
    workspace_path: str | Path | None,
    sent_at_epoch: float | None,
    submission_timeout_seconds: float,
    strict_submission_verification: bool,
    window_rect: tuple[int, int, int, int],
    window_title: str,
    ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None,
    new_task_result: dict[str, Any],
    ready_target_set: dict[str, Any] | None = None,
    verify_visual_submission: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    workspace_marker = _workspace_marker(workspace_path)
    if ready_target_set:
        ready_result = _try_candidate_set(
            ready_target_set,
            prompt=prompt,
            submit=submit,
            verify_submission=verify_submission,
            workspace_path=workspace_path,
            sent_at_epoch=sent_at_epoch,
            submission_timeout_seconds=submission_timeout_seconds,
            strict_submission_verification=strict_submission_verification,
            window_rect=window_rect,
            workspace_marker=workspace_marker,
            ui_analyst=ui_analyst,
            window_title=window_title,
            verify_visual_submission=verify_visual_submission,
            progress_callback=progress_callback,
        )
        attempts.append(_compact_attempt(ready_result))
        if ready_result.get("status") == "sent":
            ready_result.setdefault("automation", {})["attempts"] = attempts
            ready_result["automation"]["new_task"] = new_task_result
            return ready_result
        if _should_stop_after_send_verification_failure(ready_result):
            ready_result.setdefault("automation", {})["attempts"] = attempts
            ready_result["automation"]["new_task"] = new_task_result
            ready_result["reason"] = "send_clicked_but_unverified_no_retry"
            return ready_result

    for candidate_set in _candidate_target_sets(window_rect, workspace_marker):
        result = _try_candidate_set(
            candidate_set,
            prompt=prompt,
            submit=submit,
            verify_submission=verify_submission,
            workspace_path=workspace_path,
            sent_at_epoch=sent_at_epoch,
            submission_timeout_seconds=submission_timeout_seconds,
            strict_submission_verification=strict_submission_verification,
            window_rect=window_rect,
            workspace_marker=workspace_marker,
            ui_analyst=ui_analyst,
            window_title=window_title,
            verify_visual_submission=verify_visual_submission,
            progress_callback=progress_callback,
        )
        attempts.append(_compact_attempt(result))
        if result.get("status") == "sent":
            result.setdefault("automation", {})["attempts"] = attempts
            result["automation"]["new_task"] = new_task_result
            return result
        if _should_stop_after_send_verification_failure(result):
            result.setdefault("automation", {})["attempts"] = attempts
            result["automation"]["new_task"] = new_task_result
            result["reason"] = "send_clicked_but_unverified_no_retry"
            return result

    screenshot_info = _capture_ui_analysis_screenshot(workspace_path=workspace_path)
    local_analysis: dict[str, Any] = {}
    analysis_window_rect = _screenshot_window_rect(screenshot_info) or window_rect
    if screenshot_info.get("path"):
        local_analysis = locate_prompt_targets(screenshot_info["path"], analysis_window_rect)
        local_result = _try_analysis_targets(
            local_analysis,
            prompt=prompt,
            submit=submit,
            verify_submission=verify_submission,
            workspace_path=workspace_path,
            sent_at_epoch=sent_at_epoch,
            submission_timeout_seconds=submission_timeout_seconds,
            strict_submission_verification=strict_submission_verification,
            window_rect=analysis_window_rect,
            workspace_marker=workspace_marker,
            source="local_vision",
            ui_analyst=ui_analyst,
            window_title=window_title,
            verify_visual_submission=verify_visual_submission,
            progress_callback=progress_callback,
        )
        attempts.append(_compact_attempt(local_result))
        if local_result.get("status") == "sent":
            local_result.setdefault("automation", {})["attempts"] = attempts
            local_result["automation"]["screenshot"] = screenshot_info
            local_result["automation"]["local_analysis"] = local_analysis
            local_result["automation"]["new_task"] = new_task_result
            return local_result
        if _should_stop_after_send_verification_failure(local_result):
            local_result.setdefault("automation", {})["attempts"] = attempts
            local_result["automation"]["screenshot"] = screenshot_info
            local_result["automation"]["local_analysis"] = local_analysis
            local_result["automation"]["new_task"] = new_task_result
            local_result["reason"] = "send_clicked_but_unverified_no_retry"
            return local_result

    ai_analysis: dict[str, Any] = {}
    ai_error = ""
    if ui_analyst and screenshot_info.get("path"):
        context = _analysis_context(
            window_rect=analysis_window_rect,
            window_title=window_title,
            workspace_path=workspace_path,
            failed_attempts=attempts,
        )
        try:
            response = ui_analyst(str(screenshot_info["path"]), context)
            ai_analysis = response.get("analysis") if isinstance(response, dict) else {}
            if not isinstance(ai_analysis, dict):
                ai_analysis = {}
        except Exception as exc:
            ai_error = str(exc)
        if ai_analysis:
            ai_result = _try_analysis_targets(
                ai_analysis,
                prompt=prompt,
                submit=submit,
                verify_submission=verify_submission,
                workspace_path=workspace_path,
                sent_at_epoch=sent_at_epoch,
                submission_timeout_seconds=submission_timeout_seconds,
                strict_submission_verification=strict_submission_verification,
                window_rect=analysis_window_rect,
                workspace_marker=workspace_marker,
                source="ai_vision",
                ui_analyst=ui_analyst,
                window_title=window_title,
                verify_visual_submission=verify_visual_submission,
                progress_callback=progress_callback,
            )
            attempts.append(_compact_attempt(ai_result))
            if ai_result.get("status") == "sent":
                ai_result.setdefault("automation", {})["attempts"] = attempts
                ai_result["automation"]["screenshot"] = screenshot_info
                ai_result["automation"]["local_analysis"] = local_analysis
                ai_result["automation"]["ai_analysis"] = ai_analysis
                ai_result["automation"]["new_task"] = new_task_result
                return ai_result
            if _should_stop_after_send_verification_failure(ai_result):
                ai_result.setdefault("automation", {})["attempts"] = attempts
                ai_result["automation"]["screenshot"] = screenshot_info
                ai_result["automation"]["local_analysis"] = local_analysis
                ai_result["automation"]["ai_analysis"] = ai_analysis
                ai_result["automation"]["new_task"] = new_task_result
                ai_result["reason"] = "send_clicked_but_unverified_no_retry"
                return ai_result

    details = {
        "stage": "trae_ui_auto_calibration_failed",
        "window_title": window_title,
        "window_rect": _rect_dict(window_rect),
        "analysis_window_rect": _rect_dict(analysis_window_rect),
        "workspace_path": str(workspace_path or ""),
        "attempts": attempts,
        "screenshot": screenshot_info,
        "local_analysis": local_analysis,
        "ai_analysis": ai_analysis,
        "ai_error": ai_error,
        "new_task": new_task_result,
        "manual_hint": "Please inspect Trae and click the correct input/send controls manually.",
    }
    last_error = _last_attempt_error(attempts)
    message = "Worker could not locate and verify Trae prompt controls automatically."
    if last_error:
        message = f"{message} Last error: {last_error}"
    raise PromptSendError(message, details)


def _candidate_target_sets(window_rect: tuple[int, int, int, int], workspace_marker: str) -> list[dict[str, Any]]:
    sets: list[dict[str, Any]] = []
    cached_input = ui_cache.candidate_targets("prompt_input", window_rect, workspace_marker=workspace_marker)
    cached_send = ui_cache.candidate_targets("send_button", window_rect, workspace_marker=workspace_marker)
    default_send = _point_target("send_button", PROMPT_SEND_X_RATIO, PROMPT_SEND_Y_RATIO, window_rect, "adbz_ratio")
    for input_target in cached_input[:2]:
        for send_target in (cached_send[:2] or [default_send]):
            sets.append(
                {
                    "source": "cache",
                    "input": _target_from_cache(input_target, "prompt_input"),
                    "send": _target_from_cache(send_target, "send_button") if send_target else None,
                }
            )
    sets.append(
        {
            "source": "adbz_ratio",
            "input": _point_target("prompt_input", PROMPT_INPUT_X_RATIO, PROMPT_INPUT_Y_RATIO, window_rect, "adbz_ratio"),
            "send": _point_target("send_button", PROMPT_SEND_X_RATIO, PROMPT_SEND_Y_RATIO, window_rect, "adbz_ratio"),
        }
    )
    return sets


def _try_analysis_targets(
    analysis: dict[str, Any],
    *,
    prompt: str,
    submit: bool,
    verify_submission: bool,
    workspace_path: str | Path | None,
    sent_at_epoch: float | None,
    submission_timeout_seconds: float,
    strict_submission_verification: bool,
    window_rect: tuple[int, int, int, int],
    workspace_marker: str,
    source: str,
    ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    window_title: str = "",
    verify_visual_submission: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    input_target = target_for_action(analysis, "prompt_input", min_confidence=0.55 if source == "local_vision" else 0.75)
    send_target = target_for_action(analysis, "send_button", min_confidence=0.55 if source == "local_vision" else 0.75)
    return _try_candidate_set(
        {"source": source, "input": input_target or {}, "send": send_target or {}},
        prompt=prompt,
        submit=submit,
        verify_submission=verify_submission,
        workspace_path=workspace_path,
        sent_at_epoch=sent_at_epoch,
        submission_timeout_seconds=submission_timeout_seconds,
        strict_submission_verification=strict_submission_verification,
        window_rect=window_rect,
        workspace_marker=workspace_marker,
        ui_analyst=ui_analyst,
        window_title=window_title,
        verify_visual_submission=verify_visual_submission,
        progress_callback=progress_callback,
    )


def _try_candidate_set(
    candidate_set: dict[str, Any],
    *,
    prompt: str,
    submit: bool,
    verify_submission: bool,
    workspace_path: str | Path | None,
    sent_at_epoch: float | None,
    submission_timeout_seconds: float,
    strict_submission_verification: bool,
    window_rect: tuple[int, int, int, int],
    workspace_marker: str,
    ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    window_title: str = "",
    verify_visual_submission: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    source = str(candidate_set.get("source") or "unknown")
    input_target = candidate_set.get("input") if isinstance(candidate_set.get("input"), dict) else {}
    send_target = candidate_set.get("send") if isinstance(candidate_set.get("send"), dict) else {}
    ok, reason = validate_target(input_target, "prompt_input", window_rect, min_confidence=0.5 if source != "ai_vision" else 0.75)
    if not ok:
        return {"status": "failed", "source": source, "reason": reason, "input": input_target}
    if submit:
        ok, reason = validate_target(send_target, "send_button", window_rect, min_confidence=0.5 if source != "ai_vision" else 0.75)
        if not ok:
            return {"status": "failed", "source": source, "reason": reason, "input": input_target, "send": send_target}
    _emit_prompt_progress(
        progress_callback,
        "prompt_input_target_selected",
        "Worker selected the Trae prompt input target.",
        source=source,
        input=_compact_target_or_click(input_target),
        submit=submit,
    )
    input_result = _click_target(input_target, method=_operation_method(source, "prompt_input"))
    _send_keys("^a")
    _send_keys("{BACKSPACE}")
    _send_keys("^v")
    _emit_prompt_progress(
        progress_callback,
        "prompt_pasted",
        "Worker pasted the prompt into Trae.",
        source=source,
        input=_compact_target_or_click(input_result),
    )
    time.sleep(0.7)
    submit_result = {}
    if submit:
        send_guard = _verify_send_button_visual(send_target, window_rect, workspace_path)
        if send_guard.get("status") == "failed":
            ui_cache.record_failure("send_button", send_target, reason=str(send_guard.get("reason") or "send_visual_guard_failed"))
            return {
                "status": "failed",
                "source": source,
                "reason": str(send_guard.get("reason") or "send_visual_guard_failed"),
                "input": input_result,
                "send": send_target,
                "send_guard": send_guard,
            }
        submit_result = _click_target(send_target, method=_operation_method(source, "send_button"))
        _emit_prompt_progress(
            progress_callback,
            "prompt_send_clicked",
            "Worker clicked the Trae send button.",
            source=source,
            submit=_compact_target_or_click(submit_result),
        )
    verified = not (submit and verify_submission)
    try:
        submission = {}
        if submit and verify_submission:
            try:
                submission = _verify_prompt_submission(
                    prompt=prompt,
                    workspace_path=workspace_path,
                    sent_at_epoch=sent_at_epoch,
                    timeout_seconds=submission_timeout_seconds,
                )
            except PromptSendError as probe_error:
                if not verify_visual_submission:
                    raise
                submission = _verify_prompt_submission_visually(
                    prompt=prompt,
                    workspace_path=workspace_path,
                    window_rect=window_rect,
                    window_title=window_title,
                    ui_analyst=ui_analyst,
                    probe_error=probe_error,
                )
            verified = True
        elif submit and verify_visual_submission:
            submission = _verify_prompt_submission_visually(
                prompt=prompt,
                workspace_path=workspace_path,
                window_rect=window_rect,
                window_title=window_title,
                ui_analyst=ui_analyst,
            )
            verified = True
    except PromptSendError as exc:
        if strict_submission_verification:
            ui_cache.record_failure("prompt_input", input_target, reason=str(exc))
            if send_target:
                ui_cache.record_failure("send_button", send_target, reason=str(exc))
        if strict_submission_verification:
            return {
                "status": "failed",
                "source": source,
                "reason": "verification_failed",
                "error": str(exc),
                "details": exc.details,
                "input": input_result,
                "submit": submit_result,
            }
        submission = _unconfirmed_submission(exc)
    if verified:
        ui_cache.record_success(
            "prompt_input",
            input_target["center"],
            window_rect,
            source=source,
            method=str(input_target.get("method") or source),
            confidence=float(input_target.get("confidence") or 0.8),
            label=str(input_target.get("label") or ""),
            workspace_marker=workspace_marker,
        )
    if verified and submit and send_target:
        ui_cache.record_success(
            "send_button",
            send_target["center"],
            window_rect,
            source=source,
            method=str(send_target.get("method") or source),
            confidence=float(send_target.get("confidence") or 0.8),
            label=str(send_target.get("label") or ""),
            workspace_marker=workspace_marker,
        )
    return {
        "status": "sent",
        "input": input_result,
        "submit": submit_result,
        "submission": submission,
        "automation": {
            "strategy": source,
            "submission_verified": verified,
        },
    }


def _open_new_task_composer() -> dict[str, Any]:
    try:
        _send_keys("^%n")
        time.sleep(NEW_TASK_SETTLE_SECONDS)
        return {"status": "sent", "method": "ctrl_alt_n"}
    except PromptSendError as exc:
        return {"status": "failed", "method": "ctrl_alt_n", "error": str(exc)}


def _wait_for_composer_ready(
    *,
    window: Any,
    window_rect: tuple[int, int, int, int],
    workspace_path: str | Path | None,
    submit: bool,
    ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None,
    window_title: str,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + COMPOSER_READY_TIMEOUT_SECONDS
    observations: list[dict[str, Any]] = []
    last_screenshot: dict[str, Any] = {}
    last_local_analysis: dict[str, Any] = {}
    last_ai_analysis: dict[str, Any] = {}
    last_ai_error = ""

    while time.monotonic() < deadline:
        uia_candidates = _prompt_input_candidates(window, window_rect)
        screenshot_info = _capture_ui_analysis_screenshot(workspace_path=workspace_path)
        last_screenshot = screenshot_info
        analysis_rect = _screenshot_window_rect(screenshot_info) or window_rect
        local_analysis = {}
        if screenshot_info.get("path"):
            local_analysis = locate_prompt_targets(screenshot_info["path"], analysis_rect)
        last_local_analysis = local_analysis

        target_set = _ready_target_set(
            local_analysis,
            uia_candidates,
            window_rect=analysis_rect,
            submit=submit,
            source="composer_ready",
        )
        observation = _composer_ready_observation(local_analysis, uia_candidates, target_set)
        observations.append(observation)
        _emit_prompt_progress(
            progress_callback,
            "composer_ready_check",
            "Worker is waiting for the Trae prompt composer to become ready.",
            observation=observation,
            attempts=len(observations),
        )
        if target_set:
            return {
                "status": "ready",
                "source": "local_vision" if target_set.get("send") else "uia_candidate",
                "target_set": target_set,
                "attempts": len(observations),
                "observations": observations[-5:],
                "screenshot": screenshot_info,
                "local_analysis": local_analysis,
            }

        if ui_analyst and screenshot_info.get("path") and len(observations) >= 2:
            try:
                response = ui_analyst(
                    str(screenshot_info["path"]),
                    _analysis_context(
                        window_rect=analysis_rect,
                        window_title=window_title,
                        workspace_path=workspace_path,
                        failed_attempts=observations[-5:],
                    ),
                )
                ai_analysis = response.get("analysis") if isinstance(response, dict) else {}
                if not isinstance(ai_analysis, dict):
                    ai_analysis = {}
                last_ai_analysis = ai_analysis
                ai_target_set = _ready_target_set(
                    ai_analysis,
                    uia_candidates,
                    window_rect=analysis_rect,
                    submit=submit,
                    source="composer_ready_ai",
                )
                observations.append(_composer_ready_observation(ai_analysis, uia_candidates, ai_target_set))
                if ai_target_set:
                    return {
                        "status": "ready",
                        "source": "ai_vision",
                        "target_set": ai_target_set,
                        "attempts": len(observations),
                        "observations": observations[-5:],
                        "screenshot": screenshot_info,
                        "local_analysis": local_analysis,
                        "ai_analysis": ai_analysis,
                    }
            except Exception as exc:
                last_ai_error = str(exc)

        time.sleep(COMPOSER_READY_INTERVAL_SECONDS)

    raise PromptSendError(
        "Trae composer was not ready after opening a new task.",
        {
            "stage": "composer_not_ready",
            "timeout_seconds": COMPOSER_READY_TIMEOUT_SECONDS,
            "window_rect": _rect_dict(window_rect),
            "workspace_path": str(workspace_path or ""),
            "observations": observations[-8:],
            "screenshot": last_screenshot,
            "local_analysis": last_local_analysis,
            "ai_analysis": last_ai_analysis,
            "ai_error": last_ai_error,
        },
    )


def _ready_target_set(
    analysis: dict[str, Any],
    uia_candidates: list[dict[str, Any]],
    *,
    window_rect: tuple[int, int, int, int],
    submit: bool,
    source: str,
) -> dict[str, Any] | None:
    input_target = target_for_action(analysis, "prompt_input", min_confidence=0.55)
    send_target = target_for_action(analysis, "send_button", min_confidence=0.55)
    if submit:
        if not (input_target and send_target):
            return None
    elif not input_target and not uia_candidates:
        return None
    if not input_target and uia_candidates:
        input_target = _target_from_uia_candidate(uia_candidates[0], window_rect)
    result = {"source": source, "input": input_target or {}}
    if submit:
        result["send"] = send_target or {}
    return result


def _target_from_uia_candidate(candidate: dict[str, Any], window_rect: tuple[int, int, int, int]) -> dict[str, Any]:
    left, top, right, bottom = window_rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    x = int(candidate.get("center_x") or 0)
    y = int(candidate.get("center_y") or 0)
    return {
        "action": "prompt_input",
        "center": {"x": x, "y": y},
        "ratio": {"x": round((x - left) / width, 4), "y": round((y - top) / height, 4)},
        "confidence": 0.82,
        "risk": "safe",
        "method": "uia_candidate",
        "reason": "composer ready UIA candidate",
    }


def _composer_ready_observation(
    analysis: dict[str, Any],
    uia_candidates: list[dict[str, Any]],
    target_set: dict[str, Any] | None,
) -> dict[str, Any]:
    targets = analysis.get("targets") if isinstance(analysis.get("targets"), list) else []
    actions = [str(item.get("action") or "") for item in targets if isinstance(item, dict)]
    return {
        "status": "ready" if target_set else "waiting",
        "analysis_status": str(analysis.get("status") or ""),
        "analysis_reason": str(analysis.get("reason") or ""),
        "actions": actions,
        "uia_candidate_count": len(uia_candidates),
    }


def _verify_send_button_visual(
    target: dict[str, Any],
    window_rect: tuple[int, int, int, int],
    workspace_path: str | Path | None,
) -> dict[str, Any]:
    """Reject obvious microphone/toolbar mis-targets before clicking send."""
    label_text = _target_text(target)
    if any(marker in label_text for marker in STOP_SEND_BUTTON_MARKERS):
        return {"status": "failed", "reason": "send_target_is_stop_generation_button"}
    try:
        screenshot = capture_screenshot(
            target="trae_window",
            timeout_seconds=3.0,
            quality_required=False,
            workspace_path=workspace_path,
        )
    except Exception as exc:
        return {"status": "unknown", "reason": f"screenshot_failed:{exc}"}
    path = screenshot.get("path")
    if not path:
        return {"status": "unknown", "reason": "missing_screenshot_path"}
    capture = screenshot.get("capture") if isinstance(screenshot.get("capture"), dict) else {}
    bounds = capture.get("bounds") if isinstance(capture.get("bounds"), dict) else {}
    try:
        image_left = int(bounds.get("left"))
        image_top = int(bounds.get("top"))
    except (TypeError, ValueError):
        image_left, image_top = window_rect[0], window_rect[1]
    center = target.get("center") if isinstance(target.get("center"), dict) else {}
    try:
        cx = int(float(center.get("x"))) - image_left
        cy = int(float(center.get("y"))) - image_top
    except (TypeError, ValueError):
        return {"status": "failed", "reason": "send_visual_guard_missing_center"}
    try:
        from PIL import Image

        image = Image.open(path).convert("RGB")
    except Exception as exc:
        return {"status": "unknown", "reason": f"screenshot_unreadable:{exc}"}
    if not (0 <= cx < image.width and 0 <= cy < image.height):
        return {"status": "failed", "reason": "send_visual_guard_center_outside_screenshot"}
    radius = 18
    green_pixels = 0
    bright_pixels = 0
    dark_points: list[tuple[int, int]] = []
    total = 0
    for y in range(max(0, cy - radius), min(image.height, cy + radius + 1), 2):
        for x in range(max(0, cx - radius), min(image.width, cx + radius + 1), 2):
            red, green, blue = image.getpixel((x, y))
            total += 1
            if green >= 45 and green > red * 1.15 and green > blue * 1.05:
                green_pixels += 1
            if red >= 170 and green >= 170 and blue >= 170:
                bright_pixels += 1
            if abs(x - cx) <= 9 and abs(y - cy) <= 9 and red <= 70 and green <= 95 and blue <= 95:
                dark_points.append((x - cx, y - cy))
    green_ratio = green_pixels / max(1, total)
    if _looks_like_stop_generation_icon(dark_points):
        return {
            "status": "failed",
            "reason": "send_target_is_stop_generation_button",
            "green_pixels": green_pixels,
            "green_ratio": round(green_ratio, 4),
            "dark_pixels": len(dark_points),
        }
    # The valid Trae send control has a green square background. A microphone
    # button is mostly grey/white and should fail this guard.
    if green_pixels >= 10 and green_ratio >= 0.06:
        return {
            "status": "passed",
            "reason": "green_send_button_near_target",
            "green_pixels": green_pixels,
            "green_ratio": round(green_ratio, 4),
        }
    return {
        "status": "failed",
        "reason": "send_target_not_green_send_button",
        "green_pixels": green_pixels,
        "green_ratio": round(green_ratio, 4),
        "bright_pixels": bright_pixels,
    }


def _target_text(target: dict[str, Any]) -> str:
    return " ".join(
        str(target.get(key) or "").lower()
        for key in ("label", "name", "reason", "description", "aria_label", "text", "button")
    )


def _looks_like_stop_generation_icon(dark_points: list[tuple[int, int]]) -> bool:
    if not dark_points:
        return False
    total = 19 * 19
    if len(dark_points) / total < 0.18:
        return False
    row_counts: dict[int, int] = {}
    col_counts: dict[int, int] = {}
    for dx, dy in dark_points:
        row_counts[dy] = row_counts.get(dy, 0) + 1
        col_counts[dx] = col_counts.get(dx, 0) + 1
    dense_rows = sum(1 for count in row_counts.values() if count >= 3)
    dense_cols = sum(1 for count in col_counts.values() if count >= 3)
    return dense_rows >= 5 and dense_cols >= 5


def _unconfirmed_submission(error: PromptSendError) -> dict[str, Any]:
    return {
        "status": "unconfirmed",
        "error": str(error),
        "details": error.details,
    }


def _focus_prompt_input(window: Any) -> dict:
    window_rect = _window_rect(int(getattr(window, "hwnd", 0) or 0))
    solo_result = _click_solo_prompt_area(window_rect)
    if solo_result.get("status") == "clicked":
        return solo_result

    candidates = _prompt_input_candidates(window, window_rect)
    errors: list[str] = []
    for candidate in candidates[:PROMPT_INPUT_CANDIDATE_LIMIT]:
        control = candidate.pop("control")
        try:
            _activate_control(control)
            time.sleep(0.2)
            return {
                "method": "uia_candidate",
                "window_rect": _rect_dict(window_rect),
                "candidate": candidate,
                "candidate_count": len(candidates),
            }
        except Exception as exc:
            errors.append(f"{candidate.get('control_type', 'control')}: {exc}")

    result = _click_legacy_prompt_point(window_rect)
    result["candidate_count"] = len(candidates)
    if errors:
        result["uia_errors"] = errors
    return result


def _prompt_input_candidates(window: Any, window_rect: tuple[int, int, int, int] | None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for control_type in ("Edit", "Document"):
        try:
            controls = window.descendants(control_type)
        except Exception:
            continue
        for control in controls:
            summary = _control_summary(control, control_type)
            if summary is None:
                continue
            score = _candidate_score(summary, window_rect)
            if score <= 0:
                continue
            summary["score"] = score
            summary["control"] = control
            candidates.append(summary)
    candidates.sort(
        key=lambda item: (
            item.get("score", 0),
            item.get("y_ratio", 0),
            item.get("width", 0),
        ),
        reverse=True,
    )
    return candidates


def _control_summary(control: Any, control_type: str) -> dict[str, Any] | None:
    try:
        rect = control.rectangle()
    except Exception:
        return None
    left = int(getattr(rect, "left", 0) or 0)
    top = int(getattr(rect, "top", 0) or 0)
    right = int(getattr(rect, "right", 0) or 0)
    bottom = int(getattr(rect, "bottom", 0) or 0)
    width = right - left
    height = bottom - top
    if width < PROMPT_INPUT_MIN_WIDTH or height < PROMPT_INPUT_MIN_HEIGHT:
        return None
    try:
        name = str(control.window_text() or "")
    except Exception:
        name = ""
    return {
        "control_type": control_type,
        "name": name[:80],
        "rect": {"left": left, "top": top, "right": right, "bottom": bottom},
        "width": width,
        "height": height,
        "center_x": left + width // 2,
        "center_y": top + height // 2,
    }


def _candidate_score(summary: dict[str, Any], window_rect: tuple[int, int, int, int] | None) -> int:
    score = 1
    if not window_rect:
        return score

    left, top, right, bottom = window_rect
    window_width = max(right - left, 1)
    window_height = max(bottom - top, 1)
    x_ratio = (int(summary["center_x"]) - left) / window_width
    y_ratio = (int(summary["center_y"]) - top) / window_height
    summary["x_ratio"] = round(x_ratio, 3)
    summary["y_ratio"] = round(y_ratio, 3)

    if y_ratio < 0.5:
        return 0
    if y_ratio < SOLO_INPUT_TOP_MIN_RATIO or y_ratio > SOLO_INPUT_BOTTOM_MAX_RATIO:
        return 0
    if x_ratio > SOLO_INPUT_LEFT_MAX_RATIO:
        return 0
    if int(summary.get("center_x") or 0) > left + window_width * SOLO_INPUT_LEFT_MAX_RATIO:
        return 0
    if int(summary.get("height") or 0) > window_height * 0.25:
        return 0
    has_prompt_name = _has_prompt_name(summary)
    if summary.get("control_type") == "Document" and not has_prompt_name:
        return 0
    if int(summary.get("width") or 0) > window_width * 0.65 and not has_prompt_name:
        return 0
    score += 3
    if y_ratio >= 0.72:
        score += 2
    if has_prompt_name:
        score += 3
    if 0.02 <= x_ratio <= SOLO_INPUT_LEFT_MAX_RATIO:
        score += 3
    elif x_ratio <= 0.55:
        score += 1
    if int(summary.get("width") or 0) >= window_width * 0.15:
        score += 1
    return score


def _has_prompt_name(summary: dict[str, Any]) -> bool:
    name = str(summary.get("name") or "").lower()
    return any(marker in name for marker in PROMPT_INPUT_NAME_MARKERS)


def _activate_control(control: Any) -> None:
    try:
        control.set_focus()
    except Exception:
        pass
    if hasattr(control, "click_input"):
        control.click_input()
        return

    summary = _control_summary(control, "control")
    if summary is None:
        raise PromptSendError("UIA input candidate has no clickable bounds")
    _mouse_click(int(summary["center_x"]), int(summary["center_y"]))


def _click_legacy_prompt_point(window_rect: tuple[int, int, int, int] | None) -> dict:
    if not window_rect:
        raise PromptSendError("Could not locate Trae prompt input: no UIA candidate and no window bounds")
    left, top, right, bottom = window_rect
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        raise PromptSendError("Could not locate Trae prompt input: invalid Trae window bounds")
    x = int(left + width * PROMPT_INPUT_X_RATIO)
    y = int(top + height * PROMPT_INPUT_Y_RATIO)
    _mouse_click(x, y)
    time.sleep(0.2)
    return {
        "method": "coordinate_fallback",
        "window_rect": _rect_dict(window_rect),
        "click_x": x,
        "click_y": y,
        "click_ratio": {"x": PROMPT_INPUT_X_RATIO, "y": PROMPT_INPUT_Y_RATIO},
    }


def _click_solo_prompt_area(window_rect: tuple[int, int, int, int] | None) -> dict:
    if not window_rect:
        return {"status": "not_clicked", "method": "adbz_coordinate_primary", "reason": "missing_window_rect"}
    left, top, right, bottom = window_rect
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return {"status": "not_clicked", "method": "adbz_coordinate_primary", "reason": "invalid_window_rect"}
    x = int(left + width * SOLO_INPUT_CENTER_X_RATIO)
    y = int(top + height * SOLO_INPUT_CENTER_Y_RATIO)
    _mouse_click(x, y)
    time.sleep(0.2)
    return {
        "status": "clicked",
        "method": "adbz_coordinate_primary",
        "window_rect": _rect_dict(window_rect),
        "click_x": x,
        "click_y": y,
        "click_ratio": {"x": SOLO_INPUT_CENTER_X_RATIO, "y": SOLO_INPUT_CENTER_Y_RATIO},
        "target_region": {
            "left_max_ratio": SOLO_INPUT_LEFT_MAX_RATIO,
            "top_min_ratio": SOLO_INPUT_TOP_MIN_RATIO,
            "bottom_max_ratio": SOLO_INPUT_BOTTOM_MAX_RATIO,
        },
    }


def _click_send_button(window_rect: tuple[int, int, int, int] | None) -> dict:
    if not window_rect:
        raise PromptSendError("Could not click Trae send button: no window bounds")
    left, top, right, bottom = window_rect
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        raise PromptSendError("Could not click Trae send button: invalid Trae window bounds")
    x = int(left + width * PROMPT_SEND_X_RATIO)
    y = int(top + height * PROMPT_SEND_Y_RATIO)
    _mouse_click(x, y)
    time.sleep(0.2)
    return {
        "method": "adbz_send_button",
        "window_rect": _rect_dict(window_rect),
        "click_x": x,
        "click_y": y,
        "click_ratio": {"x": PROMPT_SEND_X_RATIO, "y": PROMPT_SEND_Y_RATIO},
    }


def _click_target(target: dict[str, Any], method: str) -> dict:
    center = target.get("center") if isinstance(target.get("center"), dict) else {}
    x = int(float(center.get("x")))
    y = int(float(center.get("y")))
    _mouse_click(x, y)
    time.sleep(0.2)
    return {
        "method": method,
        "click_x": x,
        "click_y": y,
        "click_ratio": target.get("ratio") or {},
        "confidence": target.get("confidence"),
        "source_method": target.get("method") or "",
        "reason": target.get("reason") or "",
    }


def _operation_method(source: str, action: str) -> str:
    if source == "adbz_ratio" and action == "prompt_input":
        return "adbz_coordinate_primary"
    if source == "adbz_ratio" and action == "send_button":
        return "adbz_send_button"
    return f"{source}_{action}"


def _point_target(
    action: str,
    rx: float,
    ry: float,
    window_rect: tuple[int, int, int, int],
    method: str,
) -> dict[str, Any]:
    left, top, right, bottom = window_rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    return {
        "action": action,
        "center": {"x": int(left + width * rx), "y": int(top + height * ry)},
        "ratio": {"x": round(rx, 4), "y": round(ry, 4)},
        "confidence": 0.8,
        "risk": "safe",
        "method": method,
    }


def _target_from_cache(target: dict[str, Any], action: str) -> dict[str, Any]:
    if not target:
        return {}
    center = target.get("center") if isinstance(target.get("center"), dict) else {}
    ratio = target.get("ratio") if isinstance(target.get("ratio"), dict) else {}
    return {
        "action": action,
        "center": center,
        "ratio": ratio,
        "confidence": float(target.get("confidence") or 0.75),
        "risk": "safe",
        "method": str(target.get("method") or "cache"),
        "label": str(target.get("label") or ""),
        "workspace_marker": str(target.get("workspace_marker") or ""),
    }


def _capture_ui_analysis_screenshot(workspace_path: str | Path | None = None) -> dict[str, Any]:
    try:
        return capture_screenshot(
            target="trae_window",
            timeout_seconds=5.0,
            quality_required=False,
            workspace_path=workspace_path,
        )
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _emit_prompt_progress(
    callback: Callable[[dict[str, Any]], None] | None,
    event: str,
    display_message: str,
    **extra: Any,
) -> None:
    if not callback:
        return
    try:
        callback(
            {
                "event": event,
                "display_message": display_message,
                **extra,
            }
        )
    except Exception:
        return


def _screenshot_window_rect(screenshot_info: dict[str, Any]) -> tuple[int, int, int, int] | None:
    capture = screenshot_info.get("capture") if isinstance(screenshot_info.get("capture"), dict) else {}
    bounds = capture.get("bounds") if isinstance(capture.get("bounds"), dict) else {}
    try:
        left = int(bounds.get("left"))
        top = int(bounds.get("top"))
        right = int(bounds.get("right"))
        bottom = int(bounds.get("bottom"))
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _analysis_context(
    *,
    window_rect: tuple[int, int, int, int],
    window_title: str,
    workspace_path: str | Path | None,
    failed_attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    left, top, right, bottom = window_rect
    return {
        "task": "find_prompt_input_and_send_button",
        "window": {
            "title": window_title,
            "bounds": {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "width": max(1, right - left),
                "height": max(1, bottom - top),
            },
        },
        "workspace_path": str(workspace_path or ""),
        "allowed_actions": ["prompt_input", "send_button"],
        "desired_action": "send_button",
        "blocked_actions": [
            "delete",
            "discard",
            "remove",
            "reset",
            "cancel",
            "voice",
            "microphone",
            "audio",
            "语音",
            "麦克风",
        ],
        "failed_attempts": failed_attempts[-8:],
        "instructions": (
            "Locate the Trae chat composer input and the active green send/up-arrow/paper-plane button. "
            "Do not choose microphone, voice input, audio, lightning, attachment, model selector, seed selector, "
            "or any other composer toolbar icon as send_button. If the candidate looks like microphone/voice, "
            "return not_found/do_not_click and explain it. Return JSON only."
        ),
    }


def _submission_analysis_context(
    *,
    prompt: str,
    window_rect: tuple[int, int, int, int],
    window_title: str,
    workspace_path: str | Path | None,
    local_analysis: dict[str, Any],
    probe_error: PromptSendError | None,
) -> dict[str, Any]:
    left, top, right, bottom = window_rect
    return {
        "task": "verify_prompt_submission",
        "window": {
            "title": window_title,
            "bounds": {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "width": max(1, right - left),
                "height": max(1, bottom - top),
            },
        },
        "workspace_path": str(workspace_path or ""),
        "desired_action": "verify_prompt_submission",
        "prompt_sample": prompt[:600],
        "local_analysis": local_analysis,
        "submission_probe_error": _probe_error_summary(probe_error),
        "instructions": (
            "Determine whether the prompt was actually submitted after the worker clicked send. "
            "Return screen_state=prompt_submitted, generating, awaiting_run_confirmation, awaiting_confirm, "
            "awaiting_continue, service_interrupted, model_error_3003, or terminal_prompt when the submitted prompt "
            "has left the composer and Trae is processing or waiting for a safe next step. "
            "Return screen_state=prompt_still_in_composer if the prompt text is still visible in the composer or "
            "the active green send button is still present next to a filled prompt input. "
            "If the green composer control shows a stop/square icon while Trae is working, that means the prompt was submitted; "
            "return generating or prompt_submitted and do not classify it as an active send button. "
            "Return screen_state=prompt_not_submitted if no submitted user prompt or generation is visible. "
            "Do not choose any click target for this task; classify only."
        ),
    }


def _compact_attempt(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "source": result.get("source") or result.get("automation", {}).get("strategy", ""),
        "reason": result.get("reason") or "",
        "error": result.get("error") or "",
        "input": _compact_target_or_click(result.get("input")),
        "submit": _compact_target_or_click(result.get("submit")),
    }


def _attempt_clicked_send_then_failed_verification(result: dict[str, Any]) -> bool:
    if str(result.get("status") or "") != "failed":
        return False
    if str(result.get("reason") or "") != "verification_failed":
        return False
    submit = result.get("submit") if isinstance(result.get("submit"), dict) else {}
    return submit.get("click_x") is not None and submit.get("click_y") is not None


def _should_stop_after_send_verification_failure(result: dict[str, Any]) -> bool:
    if not _attempt_clicked_send_then_failed_verification(result):
        return False
    return not _verification_failure_confirms_prompt_still_in_composer(result)


def _verification_failure_confirms_prompt_still_in_composer(result: dict[str, Any]) -> bool:
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    reason = str(details.get("reason") or "")
    screen_state = str(details.get("screen_state") or "")
    if reason in {"composer_still_has_active_send_button", "ai_detected_prompt_input_and_send_button"}:
        return True
    if screen_state == "prompt_still_in_composer":
        return True
    local_analysis = details.get("local_analysis") if isinstance(details.get("local_analysis"), dict) else {}
    if _local_visual_submission_state(local_analysis).get("status") == "failed":
        return True
    ai_analysis = details.get("ai_analysis") if isinstance(details.get("ai_analysis"), dict) else {}
    if _ai_visual_submission_state(ai_analysis).get("status") == "failed" and str(ai_analysis.get("screen_state") or "") == "prompt_still_in_composer":
        return True
    return False


def _last_attempt_error(attempts: list[dict[str, Any]]) -> str:
    for attempt in reversed(attempts):
        error = str(attempt.get("error") or "").strip()
        if error:
            return error
        reason = str(attempt.get("reason") or "").strip()
        if reason and reason not in {"verification_failed"}:
            return reason
    return ""


def _compact_target_or_click(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value.get(key)
        for key in ("action", "method", "click_x", "click_y", "click_ratio", "center", "ratio", "confidence", "reason")
        if key in value
    }


def _workspace_marker(workspace_path: str | Path | None) -> str:
    if not workspace_path:
        return ""
    text = str(workspace_path).strip().rstrip("\\/")
    return text.replace("\\", "/").rsplit("/", 1)[-1]


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if not hwnd:
        return None
    rect = _WinRect()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _rect_dict(rect: tuple[int, int, int, int] | None) -> dict:
    if not rect:
        return {}
    left, top, right, bottom = rect
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def _mouse_click(x: int, y: int) -> None:
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.05)


def _send_keys(keys: str) -> None:
    normalized = keys.strip()
    try:
        if normalized.lower() == "^v":
            _hotkey(0x11, 0x56)
        elif normalized.lower() == "^a":
            _hotkey(0x11, 0x41)
        elif normalized in {"{BACKSPACE}", "BACKSPACE", "Backspace"}:
            _press_key(0x08)
        elif normalized in {"{ENTER}", "ENTER", "Enter"}:
            _press_key(0x0D)
        elif normalized in {"^{ENTER}", "^ENTER", "^Enter", "CTRL+ENTER", "Ctrl+Enter"}:
            _hotkey(0x11, 0x0D)
        elif normalized in {"^%n", "^%N", "CTRL+ALT+N", "Ctrl+Alt+N"}:
            _hotkey_many([0x11, 0x12], 0x4E)
        else:
            raise PromptSendError(f"Unsupported key sequence: {keys}")
    except PromptSendError:
        raise
    except Exception as exc:
        raise PromptSendError(f"Could not send keys to Trae: {exc}") from exc
    time.sleep(0.05)


def _hotkey(modifier_vk: int, key_vk: int) -> None:
    _key_down(modifier_vk)
    try:
        _press_key(key_vk)
    finally:
        _key_up(modifier_vk)


def _hotkey_many(modifier_vks: list[int], key_vk: int) -> None:
    for modifier_vk in modifier_vks:
        _key_down(modifier_vk)
    try:
        _press_key(key_vk)
    finally:
        for modifier_vk in reversed(modifier_vks):
            _key_up(modifier_vk)


def _press_key(vk: int) -> None:
    _key_down(vk)
    _key_up(vk)


def _key_down(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)


def _key_up(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)


class _WinRect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]
