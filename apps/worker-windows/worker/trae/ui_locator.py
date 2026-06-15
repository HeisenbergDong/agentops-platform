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
    "stop_button",
    "run_button",
    "confirm_button",
    "keep_button",
    "save_button",
}
ACTION_ALIASES = {
    "continue": "continue_button",
    "continue-text": "continue_button",
    "stop": "stop_button",
    "cancel_generation": "stop_button",
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
        if action in {"run_button", "confirm_button", "continue_button", "stop_button"} and not (0.0 <= rx <= 0.55 and 0.08 <= ry <= 0.96):
            return False, "assistant_action_outside_expected_region"
    return True, "ok"


def locate_visible_action_targets(
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

    run_target = _find_run_confirmation_button(image, window_rect)
    if run_target:
        targets.append(run_target)
    status = "found" if targets else "not_found"
    return {
        "status": status,
        "method": "local_vision",
        "targets": targets,
        "reason": "ok" if targets else "no_action_targets_detected",
    }


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
    scan_left = int(width * 0.25)
    scan_right = int(width * 0.45)
    scan_top = int(height * 0.86)
    scan_bottom = int(height * 0.97)
    pixels = image.load()
    clusters: list[tuple[int, int, int]] = []
    step = 2
    for y in range(scan_top, min(scan_bottom, image.height), step):
        for x in range(scan_left, min(scan_right, image.width), step):
            red, green, blue = pixels[x, y]
            if green >= 60 and green > red * 1.2 and green > blue * 1.05:
                clusters.append((x, y, green - max(red, blue)))
    if len(clusters) < 20:
        return None
    total_weight = sum(max(1, item[2]) for item in clusters)
    cx = int(sum(item[0] * max(1, item[2]) for item in clusters) / total_weight)
    cy = int(sum(item[1] * max(1, item[2]) for item in clusters) / total_weight)
    spread_x = max(item[0] for item in clusters) - min(item[0] for item in clusters)
    spread_y = max(item[1] for item in clusters) - min(item[1] for item in clusters)
    if spread_x > width * 0.10 or spread_y > height * 0.08:
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


def _find_run_confirmation_button(image: Image.Image, window_rect: tuple[int, int, int, int]) -> dict[str, Any] | None:
    left, top, right, bottom = window_rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    chat_left = 0
    chat_right = min(image.width, int(width * 0.45))
    scan_top = int(height * 0.24)
    scan_bottom = min(image.height, int(height * 0.74))
    if chat_right - chat_left < 120 or scan_bottom - scan_top < 80:
        return None

    pixels = image.load()
    brown_pixels = 0
    button_pixels: list[tuple[int, int]] = []
    for y in range(scan_top, scan_bottom, 2):
        for x in range(chat_left, chat_right, 2):
            red, green, blue = pixels[x, y]
            if 55 <= red <= 115 and 38 <= green <= 85 and 20 <= blue <= 65 and red > blue + 15:
                brown_pixels += 1
            if _looks_like_light_button_pixel(red, green, blue):
                button_pixels.append((x, y))
    if brown_pixels < 30:
        return None

    clusters = _light_button_clusters(button_pixels, min_points=18)
    if not clusters:
        return None
    rightmost = max(clusters, key=lambda item: (item["cx"], item["points"]))
    if rightmost["max_x"] - rightmost["min_x"] > 90:
        right_edge = _cluster_summary(
            [(x, y) for x, y in button_pixels if rightmost["max_x"] - 58 <= x <= rightmost["max_x"]],
            min_points=12,
        )
        if right_edge:
            rightmost = right_edge
    cluster_width = rightmost["max_x"] - rightmost["min_x"]
    cluster_height = rightmost["max_y"] - rightmost["min_y"]
    if cluster_width < 24 or cluster_height < 16:
        return None
    x = left + int(rightmost["cx"])
    y = top + int(rightmost["cy"])
    confidence = 0.78
    if brown_pixels >= 90 and cluster_width >= 38:
        confidence = 0.86
    return _target(
        "run_button",
        x,
        y,
        window_rect,
        confidence,
        "local_vision",
        "high-risk command confirmation card with rightmost light action button",
    )


def _looks_like_light_button_pixel(red: int, green: int, blue: int) -> bool:
    avg = (red + green + blue) / 3
    spread = max(red, green, blue) - min(red, green, blue)
    return 198 <= avg <= 248 and spread <= 35


def _light_button_clusters(points: list[tuple[int, int]], min_points: int) -> list[dict[str, Any]]:
    if not points:
        return []
    clusters: list[dict[str, Any]] = []
    current: list[tuple[int, int]] = []
    last_x = -9999
    for x, y in sorted(points):
        if current and x - last_x > 8:
            cluster = _cluster_summary(current, min_points)
            if cluster:
                clusters.append(cluster)
            current = []
        current.append((x, y))
        last_x = x
    if current:
        cluster = _cluster_summary(current, min_points)
        if cluster:
            clusters.append(cluster)
    return clusters


def _cluster_summary(points: list[tuple[int, int]], min_points: int) -> dict[str, Any] | None:
    if len(points) < min_points:
        return None
    min_x = min(x for x, _y in points)
    max_x = max(x for x, _y in points)
    min_y = min(y for _x, y in points)
    max_y = max(y for _x, y in points)
    width = max_x - min_x
    height = max_y - min_y
    if width < 20 or height < 12:
        return None
    return {
        "min_x": min_x,
        "max_x": max_x,
        "min_y": min_y,
        "max_y": max_y,
        "cx": sum(x for x, _y in points) / len(points),
        "cy": sum(y for _x, y in points) / len(points),
        "points": len(points),
    }


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
