from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

PROMPT_INPUT_X_RATIO = 0.26
PROMPT_INPUT_Y_RATIO = 0.88
PROMPT_SEND_X_RATIO = 0.364
PROMPT_SEND_Y_RATIO = 0.945


SAFE_ACTIONS = {
    "prompt_input",
    "send_button",
    "continue_button",
    "run_button",
    "confirm_button",
    "keep_button",
    "save_button",
}
ACTION_ALIASES = {
    "continue": "continue_button",
    "continue-text": "continue_button",
    "run": "run_button",
    "run_anyway": "run_button",
    "execute": "run_button",
    "confirm": "confirm_button",
    "keep": "keep_button",
    "save": "save_button",
}
BLOCKED_ACTIONS = {
    "delete_button",
    "discard_button",
    "remove_button",
    "reset_button",
    "cancel_button",
}


def default_targets(window_rect: tuple[int, int, int, int] | None) -> list[dict[str, Any]]:
    if not window_rect:
        return []
    return [
        _ratio_target("prompt_input", PROMPT_INPUT_X_RATIO, PROMPT_INPUT_Y_RATIO, window_rect, "adbz_ratio"),
        _ratio_target("send_button", PROMPT_SEND_X_RATIO, PROMPT_SEND_Y_RATIO, window_rect, "adbz_ratio"),
    ]


def locate_prompt_targets(
    screenshot_path: str | Path,
    window_rect: tuple[int, int, int, int] | None,
) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    if not window_rect:
        return {"status": "not_found", "method": "local_vision", "targets": targets, "reason": "missing_window_rect"}
    try:
        image = Image.open(screenshot_path).convert("RGB")
    except Exception as exc:
        return {"status": "not_found", "method": "local_vision", "targets": targets, "reason": f"unreadable_image:{exc}"}

    input_target = _find_prompt_input_area(image, window_rect)
    if input_target:
        targets.append(input_target)
    send_target = _find_green_send_button(image, window_rect)
    if send_target:
        targets.append(send_target)
    status = "found" if _has_actions(targets, {"prompt_input", "send_button"}) else "partial" if targets else "not_found"
    return {
        "status": status,
        "method": "local_vision",
        "targets": targets,
        "reason": "ok" if targets else "no_prompt_targets_detected",
    }


def normalize_action(action: str) -> str:
    text = str(action or "").strip()
    return ACTION_ALIASES.get(text, text)


def target_for_action(analysis: dict[str, Any], action: str, min_confidence: float = 0.6) -> dict[str, Any] | None:
    targets = analysis.get("targets") if isinstance(analysis.get("targets"), list) else []
    candidates = [
        item
        for item in targets
        if isinstance(item, dict)
        and item.get("action") == action
        and str(item.get("risk") or "safe") == "safe"
        and float(item.get("confidence") or 0) >= min_confidence
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: float(item.get("confidence") or 0), reverse=True)[0]


def validate_target(
    target: dict[str, Any],
    action: str,
    window_rect: tuple[int, int, int, int] | None,
    *,
    min_confidence: float = 0.75,
) -> tuple[bool, str]:
    if action in BLOCKED_ACTIONS:
        return False, "blocked_action"
    if action not in SAFE_ACTIONS:
        return False, "unknown_action"
    if target.get("action") != action:
        return False, "action_mismatch"
    if str(target.get("risk") or "safe") != "safe":
        return False, "target_not_safe"
    if float(target.get("confidence") or 0) < min_confidence:
        return False, "confidence_too_low"
    center = target.get("center") if isinstance(target.get("center"), dict) else {}
    if not center:
        return False, "missing_center"
    try:
        x = int(float(center.get("x")))
        y = int(float(center.get("y")))
    except (TypeError, ValueError):
        return False, "invalid_center"
    if window_rect:
        left, top, right, bottom = window_rect
        if not (left <= x <= right and top <= y <= bottom):
            return False, "center_outside_window"
        width = max(1, right - left)
        height = max(1, bottom - top)
        rx = (x - left) / width
        ry = (y - top) / height
        if action == "prompt_input" and not (0.0 <= rx <= 0.45 and 0.65 <= ry <= 0.98):
            return False, "prompt_input_outside_expected_region"
        if action == "send_button" and not (0.20 <= rx <= 0.55 and 0.75 <= ry <= 0.99):
            return False, "send_button_outside_expected_region"
    return True, "ok"


def _find_prompt_input_area(image: Image.Image, window_rect: tuple[int, int, int, int]) -> dict[str, Any]:
    left, top, right, bottom = window_rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    # The composer is dark and sits in the left-bottom pane; this is a conservative
    # local fallback, not a replacement for AI vision.
    x = int(left + width * PROMPT_INPUT_X_RATIO)
    y = int(top + height * PROMPT_INPUT_Y_RATIO)
    confidence = 0.62
    return _target("prompt_input", x, y, window_rect, confidence, "local_vision", "bottom-left composer estimate")


def _find_green_send_button(image: Image.Image, window_rect: tuple[int, int, int, int]) -> dict[str, Any] | None:
    left, top, right, bottom = window_rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    scan_left = int(width * 0.22)
    scan_right = int(width * 0.43)
    scan_top = int(height * 0.82)
    scan_bottom = int(height * 0.99)
    pixels = image.load()
    clusters: list[tuple[int, int, int]] = []
    step = 2
    for y in range(scan_top, min(scan_bottom, image.height), step):
        for x in range(scan_left, min(scan_right, image.width), step):
            red, green, blue = pixels[x, y]
            if green >= 95 and green > red * 1.25 and green > blue * 1.15:
                clusters.append((x, y, green - max(red, blue)))
    if len(clusters) < 8:
        return None
    total_weight = sum(max(1, item[2]) for item in clusters)
    cx = int(sum(item[0] * max(1, item[2]) for item in clusters) / total_weight)
    cy = int(sum(item[1] * max(1, item[2]) for item in clusters) / total_weight)
    spread_x = max(item[0] for item in clusters) - min(item[0] for item in clusters)
    spread_y = max(item[1] for item in clusters) - min(item[1] for item in clusters)
    if spread_x > width * 0.08 or spread_y > height * 0.08:
        confidence = 0.68
    else:
        confidence = 0.86
    return _target(
        "send_button",
        left + cx,
        top + cy,
        window_rect,
        confidence,
        "local_vision",
        "green send button cluster in composer area",
    )


def _ratio_target(
    action: str,
    rx: float,
    ry: float,
    window_rect: tuple[int, int, int, int],
    method: str,
) -> dict[str, Any]:
    left, top, right, bottom = window_rect
    x = int(left + max(1, right - left) * rx)
    y = int(top + max(1, bottom - top) * ry)
    return _target(action, x, y, window_rect, 0.8, method, f"default {method} target")


def _target(
    action: str,
    x: int,
    y: int,
    window_rect: tuple[int, int, int, int],
    confidence: float,
    method: str,
    reason: str,
) -> dict[str, Any]:
    left, top, right, bottom = window_rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    return {
        "action": action,
        "center": {"x": int(x), "y": int(y)},
        "ratio": {"x": round((x - left) / width, 4), "y": round((y - top) / height, 4)},
        "confidence": round(float(confidence), 3),
        "risk": "safe",
        "method": method,
        "reason": reason,
    }


def _has_actions(targets: list[dict[str, Any]], actions: set[str]) -> bool:
    found = {str(item.get("action") or "") for item in targets if isinstance(item, dict)}
    return actions.issubset(found)
