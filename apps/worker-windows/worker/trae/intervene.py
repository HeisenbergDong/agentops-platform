from __future__ import annotations

import ctypes
import time
from typing import Any

from worker.system.clipboard import ClipboardError, set_clipboard_text
from worker.trae.diagnose import diagnose_ui
from worker.trae.prompt import _send_keys
from worker.trae.window import TraeAutomationError, find_trae_window, focus_trae

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


def click_continue(timeout_seconds: float = 10.0) -> dict:
    focus_trae(timeout_seconds=timeout_seconds)
    diagnosis = diagnose_ui(timeout_seconds=timeout_seconds, scroll_bottom=True)
    suggested = diagnosis.get("suggested_intervention") if isinstance(diagnosis, dict) else {}
    if isinstance(suggested, dict) and suggested:
        result = apply_intervention(suggested, timeout_seconds=timeout_seconds)
        return {
            "status": "clicked" if result.get("status") == "applied" else result.get("status", "attempted"),
            "intervention": result,
            "diagnosis": _compact_diagnosis(diagnosis),
        }

    window = find_trae_window(timeout_seconds=timeout_seconds)
    candidates = _matching_buttons(window, CONTINUE_MARKERS)
    if candidates:
        button_text, button = candidates[0]
        button.click_input()
        return {"status": "clicked", "button_text": button_text, "diagnosis": _compact_diagnosis(diagnosis)}

    fallback = click_primary_fallback()
    if fallback.get("status") == "clicked":
        return {"status": "clicked", "intervention": fallback, "diagnosis": _compact_diagnosis(diagnosis)}
    raise TraeAutomationError("No safe Trae intervention target was found")


def click_confirm() -> dict:
    return click_continue()


def apply_intervention(intervention: dict[str, Any], timeout_seconds: float = 10.0) -> dict:
    mode = str(intervention.get("mode") or "")
    if mode == "click-point":
        return click_screen_point(intervention.get("x"), intervention.get("y"))
    if mode == "terminal-input":
        return send_text_to_trae(str(intervention.get("text") or "y"), submit=True)
    if mode == "continue-text":
        return send_text_to_trae(str(intervention.get("text") or "\u7ee7\u7eed"), submit=True)
    if mode == "primary-fallback":
        return click_primary_fallback()
    raise TraeAutomationError(f"Unsupported Trae intervention mode: {mode}")


def send_text_to_trae(text: str, submit: bool = True) -> dict:
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


def _matching_buttons(window, markers: tuple[str, ...]) -> list[tuple[str, object]]:
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
            and not any(marker.lower() in text.lower() for marker in UNSAFE_MARKERS)
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
