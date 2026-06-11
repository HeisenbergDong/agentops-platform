from __future__ import annotations

import ctypes
from datetime import datetime
from typing import Any

from worker.trae.trace_copy import probe_trace, scroll_assistant_to_bottom
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


def diagnose_ui(timeout_seconds: float = 10.0, scroll_bottom: bool = True) -> dict:
    focus_trae(timeout_seconds=timeout_seconds)
    window = find_trae_window(timeout_seconds=timeout_seconds)
    scroll_result = scroll_assistant_to_bottom(window) if scroll_bottom else {}
    text = window_text_snapshot(window, limit=500)
    buttons = _button_summaries(window)
    window_rect = _window_rect(window)
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
    output_probe = probe_trace(text)
    terminal_prompt = detect_terminal_prompt(text)

    state = "idle_or_running"
    suggested = {}
    confidence = 0.0
    reason = ""
    if matches:
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
        "ok": bool(suggested),
        "state": state,
        "confidence": confidence,
        "time": datetime.now().isoformat(),
        "window_title": window.window_text(),
        "window_rect": window_rect,
        "text_chars": len(text),
        "text_sample": text[-1600:],
        "output_probe": output_probe,
        "button_count": len(buttons),
        "buttons": buttons[:40],
        "matches": matches[:8],
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
