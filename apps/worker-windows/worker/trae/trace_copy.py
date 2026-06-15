import ctypes
import time
from typing import Callable

from worker.system.clipboard import ClipboardError, get_clipboard_text, set_clipboard_text
from worker.trae.local_trace import collect_local_trace
from worker.trae.window import TraeAutomationError, find_trae_window, focus_trae

COPY_BUTTON_MARKERS = ("\u590d\u5236", "Copy", "copy")
CODE_COPY_HINTS = ("\u590d\u5236\u4ee3\u7801", "Copy code", "copy code")
TRACE_MARKERS = ("toolName:", "status:", "filePath:", "command:", "Todos updated:")
CONTINUE_MARKERS = (
    "\u8f93\u51fa\u8fc7\u957f",
    "\u8bf7\u8f93\u5165\u201c\u7ee7\u7eed\u201d",
    "\u7ee7\u7eed\u751f\u6210",
    "\u66f4\u591a\u7ed3\u679c",
    "exceeded output window",
    "input continue",
    "type continue",
    "click continue",
    "continue generating",
    "more results",
)
CONTINUE_BARE_MARKERS = ("\u7ee7\u7eed", "continue")
SERVICE_INTERRUPTION_MARKERS = (
    "\u6a21\u578b\u8bf7\u6c42\u5931\u8d25",
    "\u670d\u52a1\u7aef\u5f02\u5e38",
    "\u670d\u52a1\u5f02\u5e38",
    "\u7f51\u7edc\u5f02\u5e38",
    "\u8bf7\u6c42\u5931\u8d25",
    "\u751f\u6210\u5931\u8d25",
    "\u4efb\u52a1\u4e2d\u65ad",
    "\u5df2\u4e2d\u65ad",
    "\u53d1\u751f\u9519\u8bef",
    "\u8bf7\u7a0d\u540e\u91cd\u8bd5",
    "(3003)",
    "3003",
    "ErrorResponse",
    "server error",
    "service error",
    "network error",
    "request failed",
    "failed to generate",
    "interrupted",
    "something went wrong",
)
SCROLL_CONTROL_TYPES = ("Document", "Pane", "List", "Group", "Custom")


def copy_latest_reply(
    timeout_seconds: float = 10.0,
    cancellation_check: Callable[[], None] | None = None,
    trae_turn: dict | None = None,
    prompt: str = "",
    workspace_path: str = "",
    allow_local_fallback: bool = True,
) -> dict:
    _raise_if_cancelled(cancellation_check)
    focus_trae(timeout_seconds=timeout_seconds)
    _raise_if_cancelled(cancellation_check)
    window = find_trae_window(timeout_seconds=timeout_seconds)
    scroll_result = scroll_assistant_to_bottom(window)
    sentinel = f"agentops-copy-sentinel-{time.time_ns()}"
    failures: list[dict] = []
    candidates: list[dict] = []
    try:
        buttons = _copy_buttons(window)
    except Exception as exc:
        buttons = []
        failures.append({"stage": "find_copy_buttons", "error": str(exc)})
    for button_text, button in buttons:
        _raise_if_cancelled(cancellation_check)
        try:
            before = sentinel if _set_clipboard_text(sentinel) else _read_clipboard_text()
            button.click_input()
            raw_text = _wait_for_clipboard_change(before, timeout_seconds=timeout_seconds)
            probe = probe_trace(raw_text)
        except Exception as exc:
            failures.append({"button_text": button_text, "error": str(exc)})
            continue
        if not raw_text.strip():
            continue
        candidate = {
            "raw_text": raw_text,
            "chars": len(raw_text),
            "button_text": button_text,
            "trace_probe": probe,
        }
        candidates.append(candidate)
        if _is_preferred_trace(probe):
            return {
                "status": "copied",
                "raw_text": raw_text,
                "chars": len(raw_text),
                "copy_method": "assistant_bottom_toolbar",
                "button_text": button_text,
                "trace_probe": probe,
                "scroll": scroll_result,
                "copy_candidates": _candidate_summaries(candidates),
            }

    best = _best_copy_candidate(candidates) if candidates else {}
    if allow_local_fallback:
        local = collect_local_trace(trae_turn, prompt=prompt, workspace_path=workspace_path)
        if _is_preferred_trace(local.get("trace_probe") if isinstance(local, dict) else {}):
            local_result = {
                "status": "copied",
                "raw_text": local["raw_text"],
                "chars": local["chars"],
                "copy_method": str(local.get("trace_source") or "trae_local_trace"),
                "trace_source": str(local.get("trace_source") or "trae_local_trace"),
                "trace_probe": local["trace_probe"],
                "scroll": scroll_result,
                "copy_candidates": _candidate_summaries(candidates),
                "copy_failures": failures[-5:],
            }
            if best:
                local_result["best_clipboard_candidate"] = _candidate_summary(best)
            return local_result
        if isinstance(local, dict):
            failures.append(
                {
                    "stage": "local_trace_fallback",
                    "trace_source": str(local.get("trace_source") or ""),
                    "reason": str((local.get("trace_probe") or {}).get("reason") or ""),
                    "chars": int(local.get("chars") or 0),
                }
            )

    if best:
        return {
            "status": "copied",
            "raw_text": best["raw_text"],
            "chars": best["chars"],
            "copy_method": "assistant_bottom_toolbar_best_effort",
            "button_text": best["button_text"],
            "trace_probe": best["trace_probe"],
            "scroll": scroll_result,
            "copy_candidates": _candidate_summaries(candidates),
            "copy_failures": failures[-5:],
        }

    raise TraeAutomationError(f"No Trae assistant reply copy button produced clipboard text. failures={failures[-3:]}")


def scroll_assistant_to_bottom(window, wheel_steps: int = 14) -> dict:
    """Best-effort scroll of Trae's assistant reply pane before copying."""
    result = {
        "status": "not_scrolled",
        "attempted": True,
        "wheel_steps": wheel_steps,
        "method": "multi_strategy_reply_bottom",
        "methods": [],
        "errors": [],
    }
    coordinate_result = _scroll_window_reply_area(window, wheel_steps)
    result["methods"].append(coordinate_result)
    if coordinate_result["status"] != "scrolled":
        result["errors"].append(str(coordinate_result.get("error") or "coordinate_scroll_failed"))

    scrolled_controls = []
    for control in _scroll_candidates(window):
        try:
            control.set_focus()
        except Exception as exc:
            result["errors"].append(f"focus:{type(exc).__name__}")
        try:
            for _ in range(max(1, int(wheel_steps))):
                control.wheel_mouse_input(wheel_dist=-5)
                time.sleep(0.03)
            scrolled_controls.append(_control_summary(control))
            if len(scrolled_controls) >= 3:
                break
        except Exception as exc:
            result["errors"].append(f"wheel:{type(exc).__name__}:{exc}")
    if scrolled_controls:
        result["methods"].append({"status": "scrolled", "method": "uia_scrollable_controls", "controls": scrolled_controls})
    if any(item.get("status") == "scrolled" for item in result["methods"] if isinstance(item, dict)):
        result["status"] = "scrolled"
    return result


def _scroll_window_reply_area(window, wheel_steps: int) -> dict:
    hwnd = int(getattr(window, "hwnd", 0) or 0)
    if hwnd <= 0:
        return {"status": "not_scrolled", "attempted": True, "method": "win32_reply_area", "error": "missing_hwnd"}
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
    try:
        window.maximize()
        focus_trae(timeout_seconds=2.0)
    except Exception:
        try:
            window.maximize()
        except Exception:
            pass
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return {"status": "not_scrolled", "attempted": True, "method": "win32_reply_area", "error": "GetWindowRect failed"}

    width = max(1, int(rect.right - rect.left))
    height = max(1, int(rect.bottom - rect.top))
    # Same target family as the legacy D:\adbz automation: left conversation pane,
    # above the prompt input. Multiple hover points handle Trae layouts where the
    # actual scroll container is narrower than the visible assistant pane.
    points = [
        (0.32, 0.72),
        (0.265, 0.735),
        (0.38, 0.72),
        (0.22, 0.68),
    ]
    clicked_points = []
    try:
        for ratio_x, ratio_y in points:
            x = int(rect.left + width * ratio_x)
            y = int(rect.top + height * ratio_y)
            clicked_points.append({"x": x, "y": y, "ratio_x": ratio_x, "ratio_y": ratio_y})
            user32.SetCursorPos(x, y)
            time.sleep(0.06)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.04)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(0.08)
            for _ in range(max(1, int(wheel_steps))):
                user32.mouse_event(0x0800, 0, 0, ctypes.c_uint((-120 * 5) & 0xFFFFFFFF).value, 0)
                time.sleep(0.03)
            _press_key(user32, 0x22)  # PageDown
            time.sleep(0.04)
        _press_key(user32, 0x23)  # End
    except Exception as exc:
        return {
            "status": "not_scrolled",
            "attempted": True,
            "method": "win32_reply_area",
            "error": str(exc),
        }
    return {
        "status": "scrolled",
        "attempted": True,
        "method": "win32_reply_area",
        "points": clicked_points,
        "wheel_steps": wheel_steps,
    }


def _press_key(user32, virtual_key: int) -> None:
    user32.keybd_event(virtual_key, 0, 0, 0)
    time.sleep(0.025)
    user32.keybd_event(virtual_key, 0, 0x0002, 0)


def _scroll_candidates(window) -> list[object]:
    candidates: list[object] = []
    for control_type in SCROLL_CONTROL_TYPES:
        try:
            candidates.extend(window.descendants(control_type=control_type))
        except Exception:
            continue
    candidates.append(window)
    seen: set[int] = set()
    unique: list[object] = []
    for candidate in candidates:
        identity = id(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(candidate)
    window_rect = _control_rect_tuple(window)
    return sorted(unique, key=lambda control: _scroll_candidate_sort_key(control, window_rect))


def _control_rect_tuple(control) -> tuple[int, int, int, int] | None:
    try:
        rect = control.rectangle()
        left = int(getattr(rect, "left", 0))
        top = int(getattr(rect, "top", 0))
        right = int(getattr(rect, "right", left))
        bottom = int(getattr(rect, "bottom", top))
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _scroll_candidate_sort_key(control, window_rect: tuple[int, int, int, int] | None) -> tuple[int, float, int, int]:
    rect = _control_rect_tuple(control)
    if not rect:
        return (9, 9.0, 0, 0)
    left, top, right, bottom = rect
    width = max(0, right - left)
    height = max(0, bottom - top)
    area = width * height
    if not window_rect:
        return (0, 0.0, -area, left)
    window_left, window_top, window_right, window_bottom = window_rect
    window_width = max(1, window_right - window_left)
    window_height = max(1, window_bottom - window_top)
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    ratio_x = (center_x - window_left) / window_width
    ratio_y = (center_y - window_top) / window_height
    assistant_region = 0.10 <= ratio_x <= 0.45 and 0.12 <= ratio_y <= 0.86
    whole_window = width >= window_width * 0.86 and height >= window_height * 0.86
    composer_like = ratio_y >= 0.82 and height <= window_height * 0.28
    rank = 0 if assistant_region else 3
    if whole_window:
        rank += 2
    if composer_like:
        rank += 2
    return (rank, abs(ratio_x - 0.30), -height, -area)


def _control_summary(control) -> str:
    try:
        text = control.window_text().strip()
    except Exception:
        text = ""
    try:
        rect = control.rectangle()
        coords = ",".join(
            str(int(getattr(rect, attr, 0))) for attr in ("left", "top", "right", "bottom")
        )
    except Exception:
        coords = ""
    return f"{type(control).__name__}:{text[:60]}:{coords}"


def _copy_buttons(window) -> list[tuple[str, object]]:
    buttons: list[tuple[str, object]] = []
    try:
        controls = window.descendants(control_type="Button")
    except Exception as exc:
        raise TraeAutomationError(f"Could not inspect Trae buttons: {exc}") from exc
    for control in controls:
        try:
            text = control.window_text().strip()
        except Exception:
            continue
        if text and any(marker in text for marker in COPY_BUTTON_MARKERS):
            buttons.append((text, control))
    if not buttons:
        raise TraeAutomationError("No Trae assistant reply copy button was found")
    return sorted(buttons, key=_copy_button_sort_key)


def _copy_button_sort_key(item: tuple[str, object]) -> tuple[int, int, int]:
    text, control = item
    code_penalty = 1 if any(hint.lower() in text.lower() for hint in CODE_COPY_HINTS) else 0
    try:
        rect = control.rectangle()
        top = int(getattr(rect, "top", 0))
        left = int(getattr(rect, "left", 0))
    except Exception:
        top = 0
        left = 0
    # Reply toolbar buttons are usually lower in the conversation than code-block copy buttons.
    return (code_penalty, -top, -left)


def _wait_for_clipboard_change(before: str, timeout_seconds: float) -> str:
    deadline = time.monotonic() + timeout_seconds
    latest = before
    while time.monotonic() < deadline:
        latest = _read_clipboard_text()
        if latest.strip() and latest != before:
            return latest
        time.sleep(0.2)
    return "" if latest == before else latest


def _read_clipboard_text() -> str:
    try:
        return get_clipboard_text()
    except ClipboardError:
        return ""


def _set_clipboard_text(value: str) -> bool:
    try:
        set_clipboard_text(value)
        return True
    except ClipboardError:
        return False


def probe_trace(text: str) -> dict:
    normalized = str(text or "").strip()
    if not normalized:
        return {"complete_like": False, "reason": "empty_trace"}
    tail = normalized[-1600:].lower()
    if any(marker.lower() in tail for marker in CONTINUE_MARKERS):
        return {"complete_like": False, "reason": "awaiting_continuation", "chars": len(normalized)}
    tail_lines = [line.strip().lower() for line in normalized.splitlines()[-8:] if line.strip()]
    if any(line in CONTINUE_BARE_MARKERS for line in tail_lines):
        return {"complete_like": False, "reason": "awaiting_continuation", "chars": len(normalized)}
    service_marker = _first_marker(normalized[-2400:], SERVICE_INTERRUPTION_MARKERS)
    if service_marker:
        return {
            "complete_like": False,
            "reason": "service_interrupted",
            "chars": len(normalized),
            "marker": service_marker,
        }
    marker_count = sum(1 for marker in TRACE_MARKERS if marker in normalized)
    if marker_count == 0:
        return {"complete_like": False, "reason": "missing_tool_trace_markers", "chars": len(normalized)}
    if "toolName:" in normalized and "status:" not in normalized:
        return {"complete_like": False, "reason": "missing_status_marker", "chars": len(normalized)}
    return {"complete_like": len(normalized) >= 800, "reason": "ok", "chars": len(normalized), "marker_count": marker_count}


def _first_marker(text: str, markers: tuple[str, ...]) -> str:
    normalized = text.lower()
    for marker in markers:
        if marker.lower() in normalized:
            return marker
    return ""


def _is_preferred_trace(probe: dict) -> bool:
    return bool(probe.get("complete_like")) and str(probe.get("reason") or "") == "ok"


def _best_copy_candidate(candidates: list[dict]) -> dict:
    def score(candidate: dict) -> tuple[int, int, int]:
        probe = candidate.get("trace_probe") if isinstance(candidate.get("trace_probe"), dict) else {}
        reason = str(probe.get("reason") or "")
        preferred = 1 if _is_preferred_trace(probe) else 0
        marker_count = int(probe.get("marker_count") or 0)
        if reason in {"awaiting_continuation", "service_interrupted"}:
            marker_count += 10
        return (preferred, marker_count, int(candidate.get("chars") or 0))

    return sorted(candidates, key=score, reverse=True)[0]


def _candidate_summaries(candidates: list[dict]) -> list[dict]:
    result = []
    for candidate in candidates[:12]:
        result.append(_candidate_summary(candidate))
    return result


def _candidate_summary(candidate: dict) -> dict:
    probe = candidate.get("trace_probe") if isinstance(candidate.get("trace_probe"), dict) else {}
    return {
        "button_text": str(candidate.get("button_text") or ""),
        "chars": int(candidate.get("chars") or 0),
        "reason": str(probe.get("reason") or ""),
        "complete_like": bool(probe.get("complete_like")),
        "marker_count": int(probe.get("marker_count") or 0),
    }


def _raise_if_cancelled(cancellation_check: Callable[[], None] | None) -> None:
    if cancellation_check:
        cancellation_check()
