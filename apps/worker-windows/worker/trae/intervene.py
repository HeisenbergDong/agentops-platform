from __future__ import annotations

import ctypes
from pathlib import Path
import time
from typing import Any, Callable

from worker.system.clipboard import ClipboardError, set_clipboard_text
from worker.trae import ui_cache
from worker.trae.diagnose import diagnose_ui
from worker.trae.prompt import send_prompt, _send_keys
from worker.trae.screenshot import capture_screenshot
from worker.trae.trace_copy import scroll_assistant_to_bottom, scroll_inner_reply_panel
from worker.trae.ui_locator import normalize_action, target_for_action, validate_target
from worker.trae.window import (
    TraeAutomationError,
    find_trae_window,
    focus_trae,
    focus_trae_workspace_or_any,
    wait_for_workspace_window_or_any,
)

CONTINUE_MARKERS = (
    "\u7ee7\u7eed",
    "\u7ee7\u7eed\u751f\u6210",
    "\u786e\u8ba4",
    "\u6267\u884c",
    "\u8fd0\u884c",
    "\u4ecd\u8981\u8fd0\u884c",
    "\u4fdd\u7559\u53d8\u66f4",
    "\u4fdd\u5b58",
    "Continue",
    "continue",
    "Confirm",
    "Run",
    "Run anyway",
    "Execute",
    "Keep",
    "Keep Changes",
    "Save",
)
STOP_GENERATION_MARKERS = (
    "\u505c\u6b62\u751f\u6210",
    "\u505c\u6b62\u56de\u7b54",
    "\u505c\u6b62\u5f53\u524d",
    "\u4e2d\u6b62\u751f\u6210",
    "Stop generating",
    "Stop generation",
    "Stop response",
    "Stop",
)
UNSAFE_MARKERS = (
    "\u5220\u9664",
    "\u6e05\u7a7a",
    "\u91cd\u7f6e",
    "\u53d6\u6d88",
    "\u653e\u5f03",
    "Delete",
    "Remove",
    "Reset",
    "Cancel",
    "Discard",
)
BLOCKED_CLICK_POINT_ACTIONS = {"stop_button", "cancel_generation", "cancel_generation_button"}


def click_continue(
    timeout_seconds: float = 10.0,
    recovery_reason: str = "",
    workspace_path: str | Path | None = None,
    ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    round_context: dict[str, Any] | None = None,
) -> dict:
    if workspace_path:
        focus_trae_workspace_or_any(
            timeout_seconds=timeout_seconds,
            workspace_path=workspace_path,
        )
    else:
        focus_trae(timeout_seconds=timeout_seconds)
    diagnosis = diagnose_ui(
        timeout_seconds=timeout_seconds,
        scroll_bottom=True,
        workspace_path=workspace_path,
        ui_analyst=ui_analyst,
        round_context=round_context or {},
    )
    suggested = diagnosis.get("suggested_intervention") if isinstance(diagnosis, dict) else {}
    if isinstance(suggested, dict) and suggested:
        if str(suggested.get("mode") or "") == "manual-required":
            raise TraeAutomationError(
                str(suggested.get("manual_message") or "Trae requires manual confirmation before continuing."),
                {"diagnosis": _compact_diagnosis(diagnosis), "suggested_intervention": suggested},
            )
        result = apply_intervention(suggested, timeout_seconds=timeout_seconds, workspace_path=workspace_path)
        action_taken = _action_taken_from_result(result)
        return {
            "status": "clicked" if result.get("status") == "applied" else result.get("status", "attempted"),
            "action_taken": action_taken,
            "intervention": result,
            "diagnosis": _compact_diagnosis(diagnosis),
        }

    if workspace_path:
        window = wait_for_workspace_window_or_any(
            timeout_seconds=timeout_seconds,
            workspace_path=workspace_path,
        )
    else:
        window = find_trae_window(timeout_seconds=timeout_seconds)
    candidates = _matching_buttons(window, CONTINUE_MARKERS)
    if candidates:
        button_text, button = candidates[0]
        button.click_input()
        return {
            "status": "clicked",
            "action_taken": "clicked_button",
            "button_text": button_text,
            "diagnosis": _compact_diagnosis(diagnosis),
        }

    if _should_type_continue(recovery_reason, diagnosis):
        result = apply_intervention(
            {"mode": "continue-text", "text": "\u7ee7\u7eed"},
            timeout_seconds=timeout_seconds,
            workspace_path=workspace_path,
        )
        return {
            "status": "clicked" if result.get("status") == "applied" else result.get("status", "attempted"),
            "action_taken": "typed_continue",
            "intervention": result,
            "diagnosis": _compact_diagnosis(diagnosis),
            "recovery_reason": recovery_reason,
        }

    state = str(diagnosis.get("state") or "") if isinstance(diagnosis, dict) else ""
    raise TraeAutomationError(
        f"No explicit Trae intervention target was found; diagnosis_state={state or 'unknown'}"
    )


def click_confirm(ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None) -> dict:
    return click_continue(ui_analyst=ui_analyst)


def click_stop_generation(
    timeout_seconds: float = 5.0,
    ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict:
    focus_trae(timeout_seconds=timeout_seconds)
    window = find_trae_window(timeout_seconds=timeout_seconds)
    candidates = _matching_buttons(window, STOP_GENERATION_MARKERS, allow_unsafe_stop=True)
    if candidates:
        button_text, button = candidates[0]
        button.click_input()
        return {"status": "clicked", "action_taken": "clicked_stop_button", "button_text": button_text}
    visual = click_visual_intervention("stop_button", timeout_seconds=timeout_seconds, ui_analyst=ui_analyst)
    if visual.get("status") == "clicked":
        return {**visual, "action_taken": "clicked_visual_stop_button"}
    return {"status": "not_clicked", "reason": visual.get("reason") or "no_stop_button", "visual": visual}


def apply_intervention(
    intervention: dict[str, Any],
    timeout_seconds: float = 10.0,
    workspace_path: str | Path | None = None,
) -> dict:
    mode = str(intervention.get("mode") or "")
    if mode == "click-point":
        action = normalize_action(str(intervention.get("action") or ""))
        if action in BLOCKED_CLICK_POINT_ACTIONS:
            raise TraeAutomationError(f"Refusing to click Trae generation stop target during recovery: {action}")
        if str(intervention.get("risk") or "safe") != "safe":
            raise TraeAutomationError(f"LLM marked Trae click target as unsafe: {action}")
        if workspace_path:
            focus_trae_workspace_or_any(
                timeout_seconds=timeout_seconds,
                workspace_path=workspace_path,
            )
        else:
            focus_trae(timeout_seconds=timeout_seconds)
        if _is_forbidden_generation_stop_point(intervention, workspace_path, timeout_seconds):
            raise TraeAutomationError("Refusing to click the Trae composer stop-button region during recovery")
        return click_screen_point(intervention.get("x"), intervention.get("y"))
    if mode == "expand-confirm-card":
        if str(intervention.get("risk") or "safe") != "safe":
            raise TraeAutomationError("LLM marked Trae confirm-card expansion target as unsafe")
        if workspace_path:
            focus_trae_workspace_or_any(
                timeout_seconds=timeout_seconds,
                workspace_path=workspace_path,
            )
        else:
            focus_trae(timeout_seconds=timeout_seconds)
        clicked = click_screen_point(intervention.get("x"), intervention.get("y"))
        time.sleep(0.25)
        return {**clicked, "mode": "expand-confirm-card", "action": "expand_confirm_card"}
    if mode == "terminal-input":
        return send_text_to_trae(str(intervention.get("text") or "y"), submit=True, workspace_path=workspace_path)
    if mode == "continue-text":
        text = str(intervention.get("text") or "\u7ee7\u7eed")
        if workspace_path:
            result = send_prompt(text, submit=True, workspace_path=workspace_path)
        else:
            result = send_prompt(text, submit=True)
        return {
            "status": "applied",
            "mode": "continue-text",
            "text": text,
            "input": result.get("input") or {},
        }
    if mode == "scroll-inner-panel":
        if workspace_path:
            window = wait_for_workspace_window_or_any(
                timeout_seconds=timeout_seconds,
                workspace_path=workspace_path,
            )
        else:
            window = find_trae_window(timeout_seconds=timeout_seconds)
        scroll = scroll_inner_reply_panel(window, wheel_steps=int(intervention.get("wheel_steps") or 8))
        return {
            "status": "applied" if scroll.get("status") == "scrolled" else "attempted",
            "mode": "scroll-inner-panel",
            "scroll": scroll,
        }
    if mode == "primary-fallback":
        return click_primary_fallback()
    raise TraeAutomationError(f"Unsupported Trae intervention mode: {mode}")


def _action_taken_from_result(result: dict[str, Any]) -> str:
    mode = str(result.get("mode") or "")
    if mode == "continue-text":
        return "typed_continue"
    if mode == "terminal-input":
        return "typed_terminal_input"
    if mode == "scroll-inner-panel":
        return "scrolled_inner_panel"
    if mode == "expand-confirm-card":
        return "expanded_confirm_card"
    if mode == "click-point":
        return "clicked_button"
    if mode == "primary-fallback":
        return "clicked_primary_fallback"
    if "visual-intervention" in mode:
        return "clicked_visual_target"
    return mode or "attempted"


def _is_forbidden_generation_stop_point(
    intervention: dict[str, Any],
    workspace_path: str | Path | None,
    timeout_seconds: float,
) -> bool:
    try:
        x = int(float(intervention.get("x")))
        y = int(float(intervention.get("y")))
    except (TypeError, ValueError):
        return False
    try:
        if workspace_path:
            window = wait_for_workspace_window_or_any(
                timeout_seconds=min(2.0, max(0.5, timeout_seconds)),
                workspace_path=workspace_path,
                prefer_workspace_match=True,
            )
        else:
            window = find_trae_window(timeout_seconds=min(2.0, max(0.5, timeout_seconds)))
        rect = _window_rect(int(getattr(window, "hwnd", 0) or 0))
    except Exception:
        rect = None
    if not rect:
        return False
    left, top, right, bottom = rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    rx = (x - left) / width
    ry = (y - top) / height
    action = normalize_action(str(intervention.get("action") or ""))
    if action == "prompt_input":
        return False
    return 0.30 <= rx <= 0.43 and 0.86 <= ry <= 0.99


def _should_type_continue(recovery_reason: str, diagnosis: dict[str, Any]) -> bool:
    reason = str(recovery_reason or "").strip()
    if reason in {
        "awaiting_continuation",
        "awaiting_current_continuation",
        "service_interrupted",
        "no_completed_turn_after_prompt_send",
    }:
        return True
    if reason.startswith("trae_turn_not_completed"):
        return True
    state = str(diagnosis.get("state") or "")
    output_probe = diagnosis.get("output_probe") if isinstance(diagnosis, dict) else {}
    output_reason = str(output_probe.get("reason") or "") if isinstance(output_probe, dict) else ""
    if state in {"awaiting_continuation", "service_interrupted"}:
        return True
    return output_reason in {"awaiting_continuation", "service_interrupted"}


def send_text_to_trae(text: str, submit: bool = True, workspace_path: str | Path | None = None) -> dict:
    if workspace_path:
        focus_trae_workspace_or_any(timeout_seconds=10.0, workspace_path=workspace_path)
    else:
        focus_trae(timeout_seconds=10.0)
    value = str(text or "")
    try:
        if value == "\n":
            if submit:
                _send_keys("{ENTER}")
            return {"status": "applied", "mode": "terminal-input", "text": "<enter>"}
        set_clipboard_text(value)
    except ClipboardError as exc:
        raise TraeAutomationError(str(exc)) from exc
    _send_keys("^v")
    if submit:
        _send_keys("{ENTER}")
    return {"status": "applied", "mode": "terminal-input", "text": value}


def click_screen_point(x: Any, y: Any) -> dict:
    try:
        click_x = int(float(x))
        click_y = int(float(y))
    except (TypeError, ValueError) as exc:
        raise TraeAutomationError(f"Invalid click coordinates: {x},{y}") from exc
    _mouse_click(click_x, click_y)
    return {"status": "applied", "mode": "click-point", "x": click_x, "y": click_y}


def click_visual_intervention(
    action: str = "continue_button",
    timeout_seconds: float = 10.0,
    ui_analyst: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict:
    window = find_trae_window(timeout_seconds=timeout_seconds)
    scroll_result = scroll_assistant_to_bottom(window)
    hwnd = int(getattr(window, "hwnd", 0) or 0)
    rect = _window_rect(hwnd)
    action = normalize_action(action or "continue_button")
    if not rect:
        return {"status": "not_clicked", "mode": "visual-intervention", "reason": "missing_window_rect", "scroll": scroll_result}

    cached = ui_cache.candidate_targets(action, rect)
    for target in cached[:2]:
        candidate = _target_from_cache(target, action)
        ok, reason = validate_target(candidate, action, rect, min_confidence=0.5)
        if not ok:
            continue
        clicked = _click_target(candidate, mode="cache-visual-intervention")
        ui_cache.record_success(action, candidate["center"], rect, source="cache", confidence=float(candidate.get("confidence") or 0.7))
        return {**clicked, "action": action, "source": "cache", "scroll": scroll_result}

    screenshot = _capture_for_visual_intervention()
    ai_analysis = {}
    ai_error = ""
    if ui_analyst and screenshot.get("path"):
        try:
            response = ui_analyst(
                str(screenshot["path"]),
                _visual_intervention_context(rect, window.window_text(), action),
            )
            ai_analysis = response.get("analysis") if isinstance(response, dict) else {}
            if not isinstance(ai_analysis, dict):
                ai_analysis = {}
        except Exception as exc:
            ai_error = str(exc)
    target = target_for_action(ai_analysis, action, min_confidence=0.75) if ai_analysis else None
    if target:
        ok, reason = validate_target(target, action, rect, min_confidence=0.75)
        if ok:
            clicked = _click_target(target, mode="ai-visual-intervention")
            ui_cache.record_success(
                action,
                target["center"],
                rect,
                source="ai_vision",
                method=str(target.get("method") or "ai_vision"),
                confidence=float(target.get("confidence") or 0.75),
                label=str(target.get("label") or ""),
            )
            return {
                **clicked,
                "action": action,
                "source": "ai_vision",
                "screenshot": screenshot,
                "ai_analysis": ai_analysis,
                "scroll": scroll_result,
            }
        return {
            "status": "not_clicked",
            "mode": "visual-intervention",
            "reason": reason,
            "action": action,
            "screenshot": screenshot,
            "ai_analysis": ai_analysis,
            "scroll": scroll_result,
        }
    return {
        "status": "not_clicked",
        "mode": "visual-intervention",
        "reason": "no_visual_target",
        "action": action,
        "screenshot": screenshot,
        "ai_analysis": ai_analysis,
        "ai_error": ai_error,
        "scroll": scroll_result,
    }


def click_primary_fallback() -> dict:
    window = find_trae_window(timeout_seconds=5.0)
    hwnd = int(getattr(window, "hwnd", 0) or 0)
    if hwnd <= 0:
        return {"status": "not_clicked", "reason": "missing_hwnd"}
    rect = _window_rect(hwnd)
    if not rect:
        return {"status": "not_clicked", "reason": "missing_window_rect"}
    left, top, right, bottom = rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    points = [
        ("risk-run", left + width * 0.255, top + height * 0.568),
        ("reply-card-primary", left + width * 0.355, top + height * 0.652),
        ("reply-card-execute", left + width * 0.350, top + height * 0.640),
        ("git-trust", left + width * 0.971, top + height * 0.918),
        ("keep-card", left + width * 0.319, top + height * 0.530),
        ("keep-left", left + width * 0.535, top + height * 0.150),
        ("keep-right", left + width * 0.783, top + height * 0.150),
        ("keep", left + width * 0.697, top + height * 0.150),
        ("primary", left + width * 0.355, top + height * 0.695),
    ]
    clicked = []
    for name, raw_x, raw_y in points:
        x = int(raw_x)
        y = int(raw_y)
        _mouse_click(x, y)
        clicked.append({"name": name, "x": x, "y": y})
        time.sleep(0.15)
    return {"status": "clicked", "mode": "primary-fallback", "points": clicked}


def _target_from_cache(target: dict[str, Any], action: str) -> dict[str, Any]:
    return {
        "action": action,
        "center": target.get("center") if isinstance(target.get("center"), dict) else {},
        "ratio": target.get("ratio") if isinstance(target.get("ratio"), dict) else {},
        "confidence": float(target.get("confidence") or 0.7),
        "risk": "safe",
        "method": str(target.get("method") or "cache"),
        "label": str(target.get("label") or ""),
    }


def _click_target(target: dict[str, Any], mode: str) -> dict:
    center = target.get("center") if isinstance(target.get("center"), dict) else {}
    x = int(float(center.get("x")))
    y = int(float(center.get("y")))
    _mouse_click(x, y)
    return {
        "status": "clicked",
        "mode": mode,
        "x": x,
        "y": y,
        "ratio": target.get("ratio") or {},
        "confidence": target.get("confidence"),
        "label": target.get("label") or "",
    }


def _capture_for_visual_intervention() -> dict[str, Any]:
    try:
        return capture_screenshot(target="trae_window", timeout_seconds=5.0, quality_required=False)
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _visual_intervention_context(
    rect: tuple[int, int, int, int],
    window_title: str,
    action: str,
) -> dict[str, Any]:
    left, top, right, bottom = rect
    return {
        "task": "find_reply_action_button",
        "requested_action": action,
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
        "allowed_actions": ["continue_button", "run_button", "confirm_button", "keep_button", "save_button", "stop_button"],
        "blocked_actions": ["delete_button", "discard_button", "remove_button", "reset_button", "cancel_button"],
        "instructions": "Find the safe visible button for the requested action in Trae's assistant reply area. Return JSON only.",
    }


def _action_from_diagnosis(diagnosis: dict) -> str:
    suggested = diagnosis.get("suggested_intervention") if isinstance(diagnosis, dict) else {}
    action = suggested.get("action") if isinstance(suggested, dict) else ""
    if action:
        return normalize_action(str(action))
    state = str(diagnosis.get("state") or "") if isinstance(diagnosis, dict) else ""
    if "continue" in state or "\u7ee7\u7eed" in state:
        return "continue_button"
    return "continue_button"


def _matching_buttons(window, markers: tuple[str, ...], allow_unsafe_stop: bool = False) -> list[tuple[str, object]]:
    matches: list[tuple[str, object]] = []
    try:
        controls = window.descendants(control_type="Button")
    except Exception as exc:
        raise TraeAutomationError(f"Could not inspect Trae buttons: {exc}") from exc
    for control in controls:
        try:
            text = control.window_text().strip()
        except Exception:
            continue
        if (
            text
            and any(marker.lower() in text.lower() for marker in markers)
            and (allow_unsafe_stop or not any(marker.lower() in text.lower() for marker in UNSAFE_MARKERS))
        ):
            matches.append((text, control))
    return sorted(matches, key=_button_sort_key)


def _button_sort_key(item: tuple[str, object]) -> tuple[int, int, int]:
    text, control = item
    lower = text.lower()
    priority = 3
    if "\u7ee7\u7eed" in text or "continue" in lower:
        priority = 0
    elif "\u4ecd\u8981\u8fd0\u884c" in text or "run anyway" in lower:
        priority = 1
    elif "\u786e\u8ba4" in text or "confirm" in lower:
        priority = 2
    elif any(marker in text for marker in ("\u6267\u884c", "\u8fd0\u884c", "\u4fdd\u7559\u53d8\u66f4", "\u4fdd\u5b58")) or any(
        marker in lower for marker in ("run", "execute", "keep", "save")
    ):
        priority = 3
    try:
        rect = control.rectangle()
        top = int(getattr(rect, "top", 0))
        left = int(getattr(rect, "left", 0))
    except Exception:
        top = 0
        left = 0
    return (priority, -top, -left)


def _compact_diagnosis(diagnosis: dict) -> dict:
    return {
        "ok": bool(diagnosis.get("ok")),
        "state": diagnosis.get("state") or "",
        "confidence": diagnosis.get("confidence") or 0.0,
        "reason": diagnosis.get("reason") or "",
        "terminal_prompt": diagnosis.get("terminal_prompt") or {},
        "suggested_intervention": diagnosis.get("suggested_intervention") or {},
        "button_count": diagnosis.get("button_count") or 0,
        "output_probe": diagnosis.get("output_probe") or {},
    }


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _mouse_click(x: int, y: int) -> None:
    user32 = ctypes.windll.user32
    user32.SetCursorPos(x, y)
    time.sleep(0.06)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.04)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
