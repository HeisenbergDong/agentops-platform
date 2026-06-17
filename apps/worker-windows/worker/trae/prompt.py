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
) -> dict:
    prompt = prompt.strip()
    if not prompt:
        raise PromptSendError("Prompt is empty")

    try:
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
    if new_task_result.get("status") == "sent":
        window = wait_for_workspace_window_or_any(
            timeout_seconds=3.0,
            workspace_path=workspace_path,
            prefer_workspace_match=bool(workspace_path),
        )
    window_rect = _window_rect(int(getattr(window, "hwnd", 0) or 0))
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
    )
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
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    workspace_marker = _workspace_marker(workspace_path)
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
        )
        attempts.append(_compact_attempt(result))
        if result.get("status") == "sent":
            result.setdefault("automation", {})["attempts"] = attempts
            result["automation"]["new_task"] = new_task_result
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
        )
        attempts.append(_compact_attempt(local_result))
        if local_result.get("status") == "sent":
            local_result.setdefault("automation", {})["attempts"] = attempts
            local_result["automation"]["screenshot"] = screenshot_info
            local_result["automation"]["local_analysis"] = local_analysis
            local_result["automation"]["new_task"] = new_task_result
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
            )
            attempts.append(_compact_attempt(ai_result))
            if ai_result.get("status") == "sent":
                ai_result.setdefault("automation", {})["attempts"] = attempts
                ai_result["automation"]["screenshot"] = screenshot_info
                ai_result["automation"]["local_analysis"] = local_analysis
                ai_result["automation"]["ai_analysis"] = ai_analysis
                ai_result["automation"]["new_task"] = new_task_result
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
    input_result = _click_target(input_target, method=_operation_method(source, "prompt_input"))
    _send_keys("^a")
    _send_keys("{BACKSPACE}")
    _send_keys("^v")
    time.sleep(0.7)
    submit_result = {}
    if submit:
        submit_result = _click_target(send_target, method=_operation_method(source, "send_button"))
    verified = not (submit and verify_submission)
    try:
        submission = {}
        if submit and verify_submission:
            submission = _verify_prompt_submission(
                prompt=prompt,
                workspace_path=workspace_path,
                sent_at_epoch=sent_at_epoch,
                timeout_seconds=submission_timeout_seconds,
            )
            verified = True
    except PromptSendError as exc:
        if strict_submission_verification and source.startswith("cache"):
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
        "blocked_actions": ["delete", "discard", "remove", "reset", "cancel"],
        "failed_attempts": failed_attempts[-8:],
        "instructions": "Locate the Trae chat composer input and green send button. Return JSON only.",
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
