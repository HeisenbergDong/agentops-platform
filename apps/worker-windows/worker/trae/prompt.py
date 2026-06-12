import ctypes
import time
from typing import Any

from worker.system.clipboard import ClipboardError, set_clipboard_text
from worker.trae.window import TraeAutomationError, find_trae_window, focus_trae

PROMPT_INPUT_X_RATIO = 0.26
PROMPT_INPUT_Y_RATIO = 0.88
PROMPT_INPUT_MIN_WIDTH = 80
PROMPT_INPUT_MIN_HEIGHT = 12
PROMPT_INPUT_CANDIDATE_LIMIT = 3
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
    pass


def send_prompt(prompt: str, submit: bool = True, submit_hotkey: str = "{ENTER}") -> dict:
    prompt = prompt.strip()
    if not prompt:
        raise PromptSendError("Prompt is empty")

    try:
        focus_result = focus_trae()
        window = find_trae_window(timeout_seconds=3.0)
    except TraeAutomationError as exc:
        raise PromptSendError(str(exc)) from exc

    input_result = _focus_prompt_input(window)
    try:
        set_clipboard_text(prompt)
    except ClipboardError as exc:
        raise PromptSendError(str(exc)) from exc
    _send_keys("^a")
    _send_keys("{BACKSPACE}")
    _send_keys("^v")
    if submit:
        _send_keys(submit_hotkey)
    return {
        "status": "sent",
        "chars": len(prompt),
        "submitted": submit,
        "submit_hotkey": submit_hotkey if submit else "",
        "window_title": focus_result.get("window_title", ""),
        "input": input_result,
    }


def _focus_prompt_input(window: Any) -> dict:
    window_rect = _window_rect(int(getattr(window, "hwnd", 0) or 0))
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
    if x_ratio > 0.72:
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
    if 0.02 <= x_ratio <= 0.55:
        score += 3
    elif x_ratio <= 0.72:
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
