import time

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


def copy_latest_reply(timeout_seconds: float = 10.0) -> dict:
    focus_trae(timeout_seconds=timeout_seconds)
    window = find_trae_window(timeout_seconds=timeout_seconds)
    sentinel = f"agentops-copy-sentinel-{time.time_ns()}"
    buttons = _copy_buttons(window)
    failures: list[dict] = []
    for button_text, button in buttons:
        try:
            before = sentinel if _set_clipboard_text(sentinel) else _read_clipboard_text()
            button.click_input()
            raw_text = _wait_for_clipboard_change(before, timeout_seconds=timeout_seconds)
            probe = probe_trace(raw_text)
        except Exception as exc:
            failures.append({"button_text": button_text, "error": str(exc)})
            continue
        if raw_text.strip():
            return {
                "status": "copied",
                "raw_text": raw_text,
                "chars": len(raw_text),
                "copy_method": "assistant_bottom_toolbar",
                "button_text": button_text,
                "trace_probe": probe,
            }

    raise TraeAutomationError(f"No Trae assistant reply copy button produced clipboard text. failures={failures[-3:]}")


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
    root = None
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        return root.clipboard_get()
    except Exception:
        return ""
    finally:
        if root:
            root.destroy()


def _set_clipboard_text(value: str) -> bool:
    root = None
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(value)
        root.update()
        return True
    except Exception:
        return False
    finally:
        if root:
            root.destroy()


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
    marker_count = sum(1 for marker in TRACE_MARKERS if marker in normalized)
    if marker_count == 0:
        return {"complete_like": False, "reason": "missing_tool_trace_markers", "chars": len(normalized)}
    if "toolName:" in normalized and "status:" not in normalized:
        return {"complete_like": False, "reason": "missing_status_marker", "chars": len(normalized)}
    return {"complete_like": len(normalized) >= 800, "reason": "ok", "chars": len(normalized), "marker_count": marker_count}
