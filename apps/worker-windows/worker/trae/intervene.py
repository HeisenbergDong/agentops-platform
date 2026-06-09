from worker.trae.window import TraeAutomationError, find_trae_window, focus_trae

CONTINUE_MARKERS = (
    "\u7ee7\u7eed",
    "\u7ee7\u7eed\u751f\u6210",
    "\u786e\u8ba4",
    "\u6267\u884c",
    "\u8fd0\u884c",
    "\u4fdd\u7559\u53d8\u66f4",
    "\u4fdd\u5b58",
    "Continue",
    "continue",
    "Confirm",
    "Run",
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
    "Delete",
    "Remove",
    "Reset",
    "Cancel",
    "Discard",
)


def click_continue(timeout_seconds: float = 10.0) -> dict:
    focus_trae(timeout_seconds=timeout_seconds)
    window = find_trae_window(timeout_seconds=timeout_seconds)
    candidates = _matching_buttons(window, CONTINUE_MARKERS)
    if not candidates:
        raise TraeAutomationError("No safe Trae continue button was found")
    button_text, button = candidates[0]
    button.click_input()
    return {"status": "clicked", "button_text": button_text}


def click_confirm() -> dict:
    return click_continue()


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
            and any(marker in text for marker in markers)
            and not any(marker.lower() in text.lower() for marker in UNSAFE_MARKERS)
        ):
            matches.append((text, control))
    return sorted(matches, key=_button_sort_key)


def _button_sort_key(item: tuple[str, object]) -> tuple[int, int, int]:
    text, control = item
    lower = text.lower()
    priority = 1
    if "\u7ee7\u7eed" in text or "continue" in lower:
        priority = 0
    elif "\u786e\u8ba4" in text or "confirm" in lower:
        priority = 1
    elif any(marker in text for marker in ("\u6267\u884c", "\u8fd0\u884c", "\u4fdd\u7559\u53d8\u66f4", "\u4fdd\u5b58")) or any(
        marker in lower for marker in ("run", "execute", "keep", "save")
    ):
        priority = 2
    try:
        rect = control.rectangle()
        top = int(getattr(rect, "top", 0))
        left = int(getattr(rect, "left", 0))
    except Exception:
        top = 0
        left = 0
    return (priority, -top, -left)
