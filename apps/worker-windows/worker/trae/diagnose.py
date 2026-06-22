from __future__ import annotations

import ctypes
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from worker.trae.screenshot import capture_screenshot
from worker.trae.trace_copy import probe_trace, scroll_assistant_to_bottom
from worker.trae.supervisor import has_ui_completion_text
from worker.trae.ui_locator import normalize_action, validate_target
from worker.trae.window import TraeAutomationError, find_trae_window, focus_trae, window_text_snapshot

ACTION_BUTTON_MARKERS = {
    "run_anyway": (
        "\u4ecd\u8981\u8fd0\u884c",
        "\u4ecd\u8981\u6267\u884c",
        "\u8fd8\u662f\u8fd0\u884c",
        "\u7ee7\u7eed\u8fd0\u884c",
        "\u4f9d\u7136\u8fd0\u884c",
        "\u6211\u8981\u8fd0\u884c",
        "run anyway",
        "continue anyway",
    ),
    "execute": ("\u6267\u884c", "\u786e\u8ba4\u6267\u884c", "\u7ee7\u7eed\u6267\u884c", "execute"),
    "continue": ("\u7ee7\u7eed", "\u7ee7\u7eed\u751f\u6210", "continue", "continue generating"),
    "confirm": ("\u786e\u8ba4", "\u662f", "confirm", "yes", "ok"),
    "run": ("\u8fd0\u884c", "run"),
    "keep": ("\u4fdd\u7559", "\u4fdd\u7559\u53d8\u66f4", "keep", "keep changes"),
    "save": ("\u4fdd\u5b58", "save"),
}
ACTION_PRIORITY = {
    "run_anyway": 110,
    "execute": 100,
    "continue": 90,
    "confirm": 80,
    "save": 70,
    "keep": 65,
    "run": 60,
}
UNSAFE_BUTTON_MARKERS = (
    "\u5220\u9664",
    "\u6e05\u7a7a",
    "\u91cd\u7f6e",
    "\u53d6\u6d88",
    "\u653e\u5f03",
    "\u4e22\u5f03",
    "delete",
    "remove",
    "reset",
    "cancel",
    "discard",
)
TERMINAL_PROMPT_MARKERS = (
    "Need to install",
    "Ok to proceed?",
    "Proceed?",
    "Package name:",
    "Select a framework",
    "Select a variant",
    "Overwrite",
)
TERMINAL_DEFAULT_INPUT = "y"
SERVICE_RECOVERY_REASONS = {"awaiting_continuation", "service_interrupted"}
DELETE_CONFIRMATION_MARKERS = (
    "\u786e\u8ba4\u5220\u9664",
    "\u5220\u9664\u540e\u6587\u4ef6\u65e0\u6cd5\u6062\u590d",
    "\u662f\u5426\u4ecd\u8981\u5220\u9664",
    "\u662f\u5426\u8981\u5220\u9664",
)
DESTRUCTIVE_CHOICE_MARKERS = (
    "\u5220\u9664",
    "\u79fb\u9664",
    "\u6e05\u7a7a",
    "\u91cd\u7f6e",
    "\u653e\u5f03",
    "\u4e22\u5f03",
    "delete",
    "remove",
    "reset",
    "discard",
)
DESTRUCTIVE_ACTIONS = {"delete_button", "remove_button", "discard_button", "reset_button", "cancel_button"}
DELETE_CONFIRM_ACTIONS = {"delete_button", "remove_button"}
DELETE_CHOICE_MARKERS = ("\u5220\u9664", "\u79fb\u9664", "delete", "remove")
WAITING_ACTION_MARKERS = (
    "\u6b63\u5728\u7b49\u5f85\u4f60\u7684\u64cd\u4f5c",
    "\u6b63\u5728\u7b49\u5f85\u60a8\u7684\u64cd\u4f5c",
    "\u7b49\u5f85\u4f60\u7684\u64cd\u4f5c",
    "\u7b49\u5f85\u60a8\u7684\u64cd\u4f5c",
    "\u7b49\u5f85\u64cd\u4f5c",
    "waiting for your operation",
    "waiting for your action",
)
CLICK_RECOMMENDATIONS = {
    "click_run_button",
    "click_confirm_button",
    "click_keep_button",
    "click_save_button",
    "click_continue_button",
    "click_delete_button",
    "click_discard_button",
    "click_cancel_button",
}
SCROLL_RECOMMENDATIONS = {
    "scroll_inner_panel",
}
RECOMMENDATION_ACTIONS = {
    "click_run_button": "run_button",
    "click_confirm_button": "confirm_button",
    "click_keep_button": "keep_button",
    "click_save_button": "save_button",
    "click_continue_button": "continue_button",
    "click_delete_button": "delete_button",
    "click_discard_button": "discard_button",
    "click_cancel_button": "cancel_button",
}


def diagnose_ui(
    timeout_seconds: float = 10.0,
    scroll_bottom: bool = True,
    ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    task: str = "find_reply_action_button",
    workspace_path: str | Path | None = None,
) -> dict:
    if workspace_path:
        focus_trae(
            timeout_seconds=timeout_seconds,
            workspace_path=workspace_path,
            require_workspace_match=True,
        )
        window = find_trae_window(
            timeout_seconds=timeout_seconds,
            workspace_path=workspace_path,
            require_workspace_match=True,
        )
    else:
        focus_trae(timeout_seconds=timeout_seconds)
        window = find_trae_window(timeout_seconds=timeout_seconds)
    scroll_result = scroll_assistant_to_bottom(window) if scroll_bottom else {}
    text = window_text_snapshot(window, limit=500)
    buttons = _button_summaries(window)
    window_rect = _window_rect(window)
    matches = _action_matches(buttons, window_rect)
    diagnosis_attempts = [
        {"button_count": len(buttons), "match_count": len(matches), "scroll": scroll_result},
    ]
    if scroll_bottom and not matches:
        extra_scroll = scroll_assistant_to_bottom(window)
        text = window_text_snapshot(window, limit=500)
        buttons = _button_summaries(window)
        matches = _action_matches(buttons, window_rect)
        if isinstance(scroll_result, dict):
            scroll_result = {**scroll_result, "extra_attempt": extra_scroll}
        else:
            scroll_result = {"extra_attempt": extra_scroll}
        diagnosis_attempts.append(
            {"button_count": len(buttons), "match_count": len(matches), "scroll": extra_scroll},
        )
    visual = {}
    if window_rect and ui_analyst:
        visual = _diagnose_ai_visual(
            window_rect,
            ui_analyst=ui_analyst,
            task=task,
            window_title=window.window_text(),
            text_sample=text[-1600:],
            buttons=buttons[:40],
        )
    output_probe = probe_trace(text)
    terminal_prompt = detect_terminal_prompt(text)

    state = "idle_or_running"
    suggested = {}
    confidence = 0.0
    reason = ""
    visual_suggested = _visual_suggested_intervention(visual, window_rect)
    delete_confirmation = _delete_confirmation_intervention(text, matches)
    destructive_waiting = _destructive_waiting_intervention(text, buttons)
    safe_destructive_visual = _safe_destructive_visual_intervention(visual_suggested, text, visual, workspace_path)
    if safe_destructive_visual:
        state = str(safe_destructive_visual.get("state") or "awaiting_safe_delete_confirmation")
        confidence = float(safe_destructive_visual.get("confidence") or 0.86)
        suggested = safe_destructive_visual["suggested_intervention"]
        reason = str(safe_destructive_visual.get("reason") or "safe_destructive_visual_action")
    elif delete_confirmation:
        state = "awaiting_delete_confirmation"
        confidence = float(delete_confirmation.get("confidence") or 0.86)
        suggested = delete_confirmation["suggested_intervention"]
        reason = str(delete_confirmation.get("reason") or "local_delete_confirmation")
    elif destructive_waiting:
        state = str(destructive_waiting.get("state") or "awaiting_destructive_confirmation")
        confidence = float(destructive_waiting.get("confidence") or 0.9)
        suggested = destructive_waiting["suggested_intervention"]
        reason = str(destructive_waiting.get("reason") or "local_waiting_destructive_confirmation")
    elif _visual_completion_detected(visual):
        state = "completed"
        confidence = _completion_confidence(text, visual)
        reason = "visual_completion_detected"
    elif visual_suggested:
        state = str(visual_suggested.get("state") or "awaiting_visual_action")
        confidence = float(visual_suggested.get("confidence") or 0.0)
        suggested = visual_suggested["suggested_intervention"]
        reason = str(visual_suggested.get("reason") or "ai_visual_action_target")
    elif _needs_inner_panel_scroll(text, matches, terminal_prompt):
        state = "needs_scroll_inner_panel"
        confidence = 0.84
        suggested = {
            "mode": "scroll-inner-panel",
            "action": "scroll_inner_panel",
            "risk": "safe",
            "wheel_steps": 8,
            "recommended_action": "scroll_inner_panel",
        }
        reason = "waiting_action_inner_panel_hidden"
    elif has_ui_completion_text(text):
        state = "completed"
        confidence = _completion_confidence(text, visual)
        reason = "ui_completion_detected"
    elif output_probe.get("reason") == "service_interrupted":
        state = "service_interrupted"
        confidence = 0.9
        suggested = {"mode": "continue-text", "action": "continue", "text": "\u7ee7\u7eed"}
        reason = str(output_probe.get("reason") or "")
    elif matches and not ui_analyst:
        best = matches[0]
        state = f"awaiting_{best['action']}"
        confidence = best["confidence"]
        suggested = {
            "mode": "click-point",
            "action": best["action"],
            "x": best["button"].get("center_x"),
            "y": best["button"].get("center_y"),
            "button": best["button"].get("name") or "",
        }
    elif terminal_prompt:
        state = "awaiting_terminal_input"
        confidence = terminal_prompt["confidence"]
        suggested = {
            "mode": "terminal-input",
            "action": "terminal_input",
            "text": terminal_prompt["input"],
        }
        reason = terminal_prompt["reason"]
    elif output_probe.get("reason") in SERVICE_RECOVERY_REASONS:
        state = str(output_probe.get("reason"))
        confidence = 0.82
        suggested = {"mode": "continue-text", "action": "continue", "text": "\u7ee7\u7eed"}
        reason = str(output_probe.get("reason") or "")

    return {
        "ok": bool(suggested) or state == "completed",
        "state": state,
        "confidence": confidence,
        "time": datetime.now().isoformat(),
        "window_title": window.window_text(),
        "workspace_path": str(workspace_path or ""),
        "window_rect": window_rect,
        "text_chars": len(text),
        "text_sample": text[-1600:],
        "output_probe": output_probe,
        "button_count": len(buttons),
        "buttons": buttons[:40],
        "matches": matches[:8],
        "visual": visual,
        "diagnosis_attempts": diagnosis_attempts,
        "terminal_prompt": terminal_prompt,
        "scroll_bottom": scroll_result,
        "suggested_intervention": suggested,
        "reason": reason,
    }


def detect_terminal_prompt(text: str) -> dict:
    tail = str(text or "")[-2400:]
    if not tail.strip():
        return {}
    lowered = tail.lower()
    matched = [marker for marker in TERMINAL_PROMPT_MARKERS if marker.lower() in lowered]
    if not matched:
        return {}
    input_text = TERMINAL_DEFAULT_INPUT
    if "select a framework" in lowered:
        input_text = "\n"
    elif "select a variant" in lowered:
        input_text = "\n"
    elif "package name" in lowered:
        input_text = "\n"
    elif "overwrite" in lowered:
        input_text = "y"
    return {
        "confidence": 0.86,
        "reason": "terminal_prompt:" + ",".join(matched[:3]),
        "input": input_text,
        "markers": matched[:8],
    }


def _needs_inner_panel_scroll(text: str, matches: list[dict], terminal_prompt: dict) -> bool:
    if matches or terminal_prompt:
        return False
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in WAITING_ACTION_MARKERS)


def _has_waiting_action_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in WAITING_ACTION_MARKERS)


def _has_destructive_choice_text(text: str) -> bool:
    normalized = _normalize(text)
    return any(_normalize(marker) in normalized for marker in DESTRUCTIVE_CHOICE_MARKERS)


def _button_summaries(window) -> list[dict[str, Any]]:
    try:
        controls = window.descendants(control_type="Button")
    except Exception as exc:
        raise TraeAutomationError(f"Could not inspect Trae buttons: {exc}") from exc
    buttons: list[dict[str, Any]] = []
    for control in controls[:400]:
        try:
            text = control.window_text().strip()
            rect = control.rectangle()
        except Exception:
            continue
        left = int(getattr(rect, "left", 0))
        top = int(getattr(rect, "top", 0))
        right = int(getattr(rect, "right", left))
        bottom = int(getattr(rect, "bottom", top))
        if right <= left or bottom <= top:
            continue
        buttons.append(
            {
                "name": text,
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
                "center_x": (left + right) // 2,
                "center_y": (top + bottom) // 2,
            }
        )
    return buttons


def _classify_button(button: dict[str, Any]) -> dict | None:
    name = str(button.get("name") or "").strip()
    normalized = _normalize(name)
    if not normalized:
        return None
    if any(marker in normalized for marker in (_normalize(item) for item in UNSAFE_BUTTON_MARKERS)):
        return None
    for action, markers in ACTION_BUTTON_MARKERS.items():
        normalized_markers = [_normalize(marker) for marker in markers]
        if normalized in normalized_markers or any(_contains_marker(normalized, marker) for marker in normalized_markers):
            return {
                "state": f"awaiting_{action}",
                "action": action,
                "confidence": 0.9 if normalized in normalized_markers else 0.74,
                "priority": ACTION_PRIORITY[action],
                "button": button,
            }
    return None


def _action_matches(buttons: list[dict[str, Any]], window_rect: dict | None) -> list[dict]:
    matches = []
    for button in buttons:
        match = _classify_button(button)
        if not match:
            continue
        if match["action"] in {"run", "run_anyway", "execute", "confirm", "continue"} and not _button_in_assistant_pane(
            button, window_rect
        ):
            continue
        matches.append(match)
    matches.sort(key=lambda item: (item["priority"], item["confidence"], int(item["button"].get("center_y") or 0)), reverse=True)
    return matches


def _delete_confirmation_intervention(text: str, matches: list[dict]) -> dict[str, Any]:
    normalized_text = _normalize(text)
    if not normalized_text or not any(marker in normalized_text for marker in DELETE_CONFIRMATION_MARKERS):
        return {}
    confirm = next((item for item in matches if item.get("action") == "confirm"), None)
    if not confirm:
        return {}
    button = confirm.get("button") if isinstance(confirm.get("button"), dict) else {}
    return {
        "confidence": min(0.92, max(0.86, float(confirm.get("confidence") or 0.86))),
        "reason": "local_delete_confirmation_allowed",
        "suggested_intervention": {
            "mode": "click-point",
            "action": "delete_button",
            "x": button.get("center_x"),
            "y": button.get("center_y"),
            "button": button.get("name") or "\u786e\u8ba4",
            "confidence": confirm.get("confidence") or 0.86,
            "source": "local_uia",
            "risk": "safe",
            "recommended_action": "click_delete_button",
            "manual_message": "Trae 正在等待删除确认，按策略可自动点击删除。",
        },
    }


def _destructive_waiting_intervention(text: str, buttons: list[dict[str, Any]]) -> dict[str, Any]:
    if not _has_waiting_action_text(text) or not _has_destructive_choice_text(text):
        return {}
    delete_button = _first_button_with_markers(buttons, DELETE_CHOICE_MARKERS)
    if delete_button:
        return {
            "state": "awaiting_delete_confirmation",
            "confidence": 0.92,
            "reason": "local_delete_confirmation_allowed",
            "suggested_intervention": {
                "mode": "click-point",
                "action": "delete_button",
                "x": delete_button.get("center_x"),
                "y": delete_button.get("center_y"),
                "button": delete_button.get("name") or "",
                "confidence": 0.92,
                "source": "local_uia",
                "risk": "safe",
                "recommended_action": "click_delete_button",
                "policy": "trae_delete_confirmation_allowed",
            },
        }
    button = _first_button_with_markers(buttons, DESTRUCTIVE_CHOICE_MARKERS)
    return {
        "state": "awaiting_destructive_confirmation",
        "confidence": 0.92,
        "reason": "local_waiting_destructive_confirmation",
        "suggested_intervention": {
            "mode": "manual-required",
            "action": "manual_required",
            "x": button.get("center_x") if button else None,
            "y": button.get("center_y") if button else None,
            "button": button.get("name") if button else "",
            "confidence": 0.92,
            "source": "local_uia",
            "risk": "blocked",
            "recommended_action": "do_not_click",
            "manual_message": (
                "Trae is waiting for a user decision on a destructive action "
                "(delete/remove/discard/reset). Do not mark the task complete or click it automatically."
            ),
        },
    }


def _safe_destructive_visual_intervention(
    visual_suggested: dict[str, Any],
    text: str,
    visual: dict[str, Any],
    workspace_path: str | Path | None,
) -> dict[str, Any]:
    if not isinstance(visual_suggested, dict) or not visual_suggested:
        return {}
    suggested = visual_suggested.get("suggested_intervention")
    if not isinstance(suggested, dict):
        return {}
    action = normalize_action(str(suggested.get("action") or ""))
    if action not in DELETE_CONFIRM_ACTIONS:
        return {}
    if str(suggested.get("risk") or "") != "safe":
        return {}
    analysis = {}
    if isinstance(visual, dict):
        analysis = visual.get("ai_analysis") if isinstance(visual.get("ai_analysis"), dict) else {}
    if not _is_delete_confirmation_context(text, analysis):
        return {}
    enriched = {
        **suggested,
        "risk": "safe",
        "policy": "trae_delete_confirmation_allowed",
        "recommended_action": "click_delete_button" if action in {"delete_button", "remove_button"} else suggested.get("recommended_action"),
    }
    return {
        **visual_suggested,
        "state": "awaiting_safe_delete_confirmation",
        "reason": visual_suggested.get("reason") or "trae_delete_confirmation_allowed",
        "suggested_intervention": enriched,
    }


def _is_delete_confirmation_context(text: str, analysis: dict[str, Any]) -> bool:
    blob = _delete_confirmation_text(text, analysis)
    normalized = _normalize(blob)
    return bool(
        any(_normalize(marker) in normalized for marker in DELETE_CHOICE_MARKERS)
        and (
            _has_waiting_action_text(blob)
            or any(_normalize(marker) in normalized for marker in DELETE_CONFIRMATION_MARKERS)
            or "delete" in normalized
            or "\u5220\u9664" in normalized
        )
    )


def _delete_confirmation_text(text: str, analysis: dict[str, Any]) -> str:
    parts = [str(text or "")]
    if isinstance(analysis, dict):
        for key in ("reason", "blocked_reason"):
            parts.append(str(analysis.get(key) or ""))
        evidence = analysis.get("evidence") if isinstance(analysis.get("evidence"), list) else []
        parts.extend(str(item) for item in evidence)
        target = analysis.get("target") if isinstance(analysis.get("target"), dict) else {}
        parts.extend(str(target.get(key) or "") for key in ("label", "reason"))
        targets = analysis.get("targets") if isinstance(analysis.get("targets"), list) else []
        for item in targets:
            if isinstance(item, dict):
                parts.extend(str(item.get(key) or "") for key in ("label", "reason"))
    return "\n".join(parts)


def _first_button_with_markers(buttons: list[dict[str, Any]], markers: tuple[str, ...]) -> dict[str, Any] | None:
    normalized_markers = [_normalize(marker) for marker in markers]
    for button in buttons:
        name = _normalize(str(button.get("name") or ""))
        if name and any(marker in name for marker in normalized_markers):
            return button
    return None


def _diagnose_ai_visual(
    window_rect: dict | None,
    *,
    ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    task: str = "find_reply_action_button",
    window_title: str = "",
    text_sample: str = "",
    buttons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not window_rect:
        return {"status": "not_found", "reason": "missing_window_rect"}
    tuple_rect = (
        int(window_rect.get("left") or 0),
        int(window_rect.get("top") or 0),
        int(window_rect.get("right") or 0),
        int(window_rect.get("bottom") or 0),
    )
    try:
        screenshot = capture_screenshot(target="trae_window", timeout_seconds=5.0, quality_required=False)
    except Exception as exc:
        return {"status": "not_found", "reason": "screenshot_failed", "error": str(exc)}
    ai_analysis = {}
    ai_error = ""
    if ui_analyst and screenshot.get("path"):
        try:
            response = ui_analyst(
                str(screenshot["path"]),
                _visual_diagnosis_context(
                    tuple_rect,
                    window_title=window_title,
                    task=task,
                    text_sample=text_sample,
                    buttons=buttons or [],
                ),
            )
            ai_analysis = response.get("analysis") if isinstance(response, dict) else response
            if not isinstance(ai_analysis, dict):
                ai_analysis = {}
        except Exception as exc:
            ai_error = str(exc)
    if _analysis_is_completed(ai_analysis):
        return {
            "status": "completed",
            "reason": "ai_visual_completion_detected",
            "screenshot": screenshot,
            "ai_analysis": ai_analysis,
            "ai_error": ai_error,
        }
    if task == "find_prompt_input_and_send_button" and ai_analysis:
        screen_state = str(ai_analysis.get("screen_state") or "")
        recommended = str(ai_analysis.get("recommended_action") or "")
        if screen_state in {"prompt_submitted", "generating", "still_generating"} or recommended in {
            "wait",
            "collect_trace_candidate",
        }:
            return {
                "status": "prompt_submitted",
                "reason": "ai_visual_prompt_submitted",
                "screenshot": screenshot,
                "ai_analysis": ai_analysis,
                "ai_error": ai_error,
            }
        if screen_state in {"prompt_still_in_composer", "prompt_not_submitted"} or str(ai_analysis.get("status") or "") in {
            "found",
            "partial",
        }:
            return {
                "status": "prompt_ready",
                "reason": "ai_visual_prompt_controls_or_unsent_prompt",
                "screenshot": screenshot,
                "ai_analysis": ai_analysis,
                "ai_error": ai_error,
            }
    return {
        "status": str(ai_analysis.get("status") or "not_found") if ai_analysis else "not_found",
        "reason": str(ai_analysis.get("reason") or ai_error or "ai_visual_no_action") if isinstance(ai_analysis, dict) else ai_error,
        "screenshot": screenshot,
        "ai_analysis": ai_analysis,
        "ai_error": ai_error,
    }


def _visual_diagnosis_context(
    rect: tuple[int, int, int, int],
    *,
    window_title: str,
    task: str,
    text_sample: str = "",
    buttons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    left, top, right, bottom = rect
    return {
        "task": task,
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
        "allowed_completion_evidence": [
            "left task card says task completed",
            "assistant reply footer indicates the current turn is done",
            "code changes tab shows a finished task with no generating indicator",
        ],
        "completion_blockers": [
            "Chinese text like \u6b63\u5728\u7b49\u5f85\u4f60\u7684\u64cd\u4f5c or \u7b49\u5f85\u60a8\u7684\u64cd\u4f5c",
            "English text like waiting for your operation/action",
            "a confirmation card with \u4fdd\u7559/\u5220\u9664, keep/delete, delete/remove/discard/cancel/reset",
            "terminal or UI confirmation asking whether to run, delete, discard, overwrite, or proceed",
        ],
        "manual_required_rules": [
            "Never return screen_state=completed or recommended_action=collect_trace_candidate when a waiting-for-user-action card is visible.",
            "If Trae asks whether it can delete/remove something, returning click_delete_button with risk=safe is allowed.",
            "Discard, reset, cancel, clear, or uncertain destructive prompts must stay manual_required/do_not_click.",
            "If only part of the action card is visible, return recommended_action=scroll_inner_panel instead of completed.",
            "Safe click actions are explicit run/continue/keep/save buttons and explicit Trae delete confirmations.",
        ],
        "required_json_shape": {
            "status": "found|partial|not_found",
            "screen_state": "completed|still_generating|prompt_submitted|prompt_still_in_composer|prompt_not_submitted|manual_required|awaiting_action|needs_scroll_inner_panel",
            "recommended_action": "wait|collect_trace_candidate|click_run_button|click_continue_button|click_keep_button|click_save_button|click_delete_button|scroll_inner_panel|do_not_click|type_continue|answer_terminal_prompt",
            "risk": "safe|blocked",
            "blocked_reason": "required when risk is blocked",
        },
        "visible_text_sample": text_sample,
        "uia_buttons": buttons or [],
        "allow_trae_delete_confirmations": True,
        "allowed_destructive_actions": ["delete_button", "remove_button"],
        "safe_destructive_action_policy": {
            "delete_button": "Allowed when Trae explicitly asks whether it can delete/remove something.",
            "manual_required": "Required for discard/reset/cancel/clear or uncertain destructive prompts.",
        },
        "instructions": _visual_task_instructions(task),
    }


def _visual_task_instructions(task: str) -> str:
    if task == "find_prompt_input_and_send_button":
        return (
            "Decide whether the latest prompt has actually left the Trae composer. "
            "If the prompt text is still in the composer or the active send/up-arrow button is visible beside a filled composer, "
            "return prompt_still_in_composer or prompt_not_submitted with do_not_click. "
            "If the prompt has been submitted and Trae is generating or responding, return prompt_submitted/still_generating with wait. "
            "If the composer and safe send button are visible for retry, identify that state without choosing microphone, voice, attachment, model selector, or toolbar icons as send. "
            "Return JSON only."
        )
    return (
        "Decide whether the current Trae task is completed, still generating, or waiting for a user action. "
        "Treat waiting-for-user-action cards as blockers, not completion evidence. "
        "If a visible confirmation card asks whether Trae can delete/remove something, return the delete button target with risk=safe. Keep discard/reset/cancel/clear prompts blocked unless explicitly safe. "
        "If a safe confirmation card asks to execute, continue, or keep/save changes, return the exact visible button target. "
        "Return JSON only."
    )


def _visual_suggested_intervention(visual: dict[str, Any], window_rect: dict | None) -> dict[str, Any]:
    if not isinstance(visual, dict):
        return {}
    analysis = visual.get("ai_analysis") if isinstance(visual.get("ai_analysis"), dict) else {}
    if not analysis:
        return {}
    recommended = str(analysis.get("recommended_action") or "")
    if recommended == "type_continue":
        return {
            "state": str(analysis.get("screen_state") or "awaiting_continue"),
            "confidence": analysis.get("confidence") or 0.82,
            "reason": str(analysis.get("reason") or "ai_visual_type_continue"),
            "suggested_intervention": {"mode": "continue-text", "action": "continue", "text": "\u7ee7\u7eed"},
        }
    if recommended == "answer_terminal_prompt":
        return {
            "state": "awaiting_terminal_input",
            "confidence": analysis.get("confidence") or 0.82,
            "reason": str(analysis.get("reason") or "ai_visual_terminal_prompt"),
            "suggested_intervention": {"mode": "terminal-input", "action": "terminal_input", "text": "y"},
        }
    if recommended in SCROLL_RECOMMENDATIONS:
        return {
            "state": str(analysis.get("screen_state") or "needs_scroll_inner_panel"),
            "confidence": analysis.get("confidence") or 0.82,
            "reason": str(analysis.get("reason") or "ai_visual_scroll_inner_panel"),
            "suggested_intervention": {
                "mode": "scroll-inner-panel",
                "action": "scroll_inner_panel",
                "risk": "safe",
                "wheel_steps": 8,
                "recommended_action": recommended,
            },
        }
    if recommended == "do_not_click" and str(analysis.get("risk") or "") == "blocked":
        return {
            "state": str(analysis.get("screen_state") or "manual_required"),
            "confidence": analysis.get("confidence") or 0.82,
            "reason": str(analysis.get("blocked_reason") or analysis.get("reason") or "ai_visual_manual_required"),
            "suggested_intervention": {
                "mode": "manual-required",
                "action": "manual_required",
                "risk": "blocked",
                "recommended_action": "do_not_click",
                "manual_message": str(analysis.get("blocked_reason") or analysis.get("reason") or "Trae 正在等待人工确认。"),
            },
        }
    if recommended in {"click_delete_button", "click_discard_button", "click_cancel_button"} and str(analysis.get("risk") or "") != "safe":
        return {
            "state": str(analysis.get("screen_state") or "manual_required"),
            "confidence": analysis.get("confidence") or 0.82,
            "reason": str(analysis.get("reason") or "ai_visual_destructive_confirmation"),
            "suggested_intervention": {
                "mode": "manual-required",
                "action": "manual_required",
                "risk": "blocked",
                "recommended_action": "do_not_click",
                "manual_message": "Trae 正在等待删除、丢弃或取消类确认，已暂停等待人工确认。",
            },
        }
    if recommended not in CLICK_RECOMMENDATIONS:
        return {}
    if str(analysis.get("risk") or "") != "safe":
        return {}
    target = analysis.get("target") if isinstance(analysis.get("target"), dict) else {}
    if not target:
        targets = analysis.get("targets") if isinstance(analysis.get("targets"), list) else []
        target = next((item for item in targets if isinstance(item, dict) and str(item.get("risk") or "") == "safe"), {})
    if not target:
        return {}
    action = normalize_action(str(target.get("action") or RECOMMENDATION_ACTIONS.get(recommended) or ""))
    expected = normalize_action(RECOMMENDATION_ACTIONS.get(recommended, action))
    if action != expected:
        action = expected
    tuple_rect = _tuple_window_rect(window_rect)
    ok, reason = validate_target(target, action, tuple_rect, min_confidence=0.6)
    if not ok:
        return {
            "state": str(analysis.get("screen_state") or "awaiting_visual_action"),
            "confidence": analysis.get("confidence") or 0.0,
            "reason": f"ai_visual_target_rejected:{reason}",
            "suggested_intervention": {},
        }
    center = target.get("center") if isinstance(target.get("center"), dict) else {}
    return {
        "state": str(analysis.get("screen_state") or f"awaiting_{action}"),
        "confidence": analysis.get("confidence") or target.get("confidence") or 0.0,
        "reason": str(analysis.get("reason") or target.get("reason") or "ai_visual_action_target"),
        "suggested_intervention": {
            "mode": "click-point",
            "action": action,
            "x": center.get("x"),
            "y": center.get("y"),
            "button": target.get("label") or target.get("action") or "",
            "confidence": target.get("confidence") or analysis.get("confidence"),
            "source": "ai_vision",
            "risk": target.get("risk") or analysis.get("risk"),
            "recommended_action": recommended,
        },
    }


def _tuple_window_rect(window_rect: dict | None) -> tuple[int, int, int, int] | None:
    if not window_rect:
        return None
    return (
        int(window_rect.get("left") or 0),
        int(window_rect.get("top") or 0),
        int(window_rect.get("right") or 0),
        int(window_rect.get("bottom") or 0),
    )


def _analysis_is_completed(analysis: dict[str, Any]) -> bool:
    if not isinstance(analysis, dict):
        return False
    state = str(analysis.get("screen_state") or analysis.get("state") or "").strip()
    action = str(analysis.get("recommended_action") or "").strip()
    try:
        confidence = float(analysis.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return state == "completed" and action == "collect_trace_candidate" and confidence >= 0.7


def _visual_completion_detected(visual: dict[str, Any]) -> bool:
    if not isinstance(visual, dict):
        return False
    if visual.get("status") == "completed":
        return True
    analysis = visual.get("ai_analysis") if isinstance(visual.get("ai_analysis"), dict) else {}
    return _analysis_is_completed(analysis)


def _completion_confidence(text: str, visual: dict[str, Any]) -> float:
    if has_ui_completion_text(text):
        return 0.88
    analysis = visual.get("ai_analysis") if isinstance(visual, dict) and isinstance(visual.get("ai_analysis"), dict) else {}
    try:
        return max(0.7, min(1.0, float(analysis.get("confidence") or 0.82)))
    except (TypeError, ValueError):
        return 0.82


def _contains_marker(normalized: str, marker: str) -> bool:
    if not marker:
        return False
    if len(marker) <= 3 and marker.isascii():
        return normalized == marker
    return marker in normalized


def _button_in_assistant_pane(button: dict[str, Any], window_rect: dict | None) -> bool:
    cx = button.get("center_x")
    if cx is None:
        return False
    if not window_rect:
        return True
    left = int(window_rect.get("left") or 0)
    width = int(window_rect.get("width") or 0)
    if width <= 0:
        return True
    return int(cx) <= left + int(width * 0.45)


def _window_rect(window) -> dict | None:
    hwnd = int(getattr(window, "hwnd", 0) or 0)
    if hwnd <= 0:
        return None
    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    left = int(rect.left)
    top = int(rect.top)
    right = int(rect.right)
    bottom = int(rect.bottom)
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": max(0, right - left),
        "height": max(0, bottom - top),
    }


def _normalize(value: str) -> str:
    return "".join(str(value or "").split()).lower()
