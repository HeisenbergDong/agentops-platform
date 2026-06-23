from __future__ import annotations

import ctypes
from datetime import datetime
import os
from pathlib import Path
import time
from typing import Any

from PIL import Image

from worker.trae.window import TraeAutomationError, focus_trae_workspace_or_any, wait_for_workspace_window_or_any


MIN_CAPTURE_WIDTH = 500
MIN_CAPTURE_HEIGHT = 360


def capture_screenshot(
    target: str = "trae_window",
    timeout_seconds: float = 10.0,
    quality_required: bool = True,
    workspace_path: str | Path | None = None,
) -> dict:
    out_dir = _screenshot_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"trae-{target}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    capture = _capture_target(path, target=target, timeout_seconds=timeout_seconds, workspace_path=workspace_path)
    quality = assess_screenshot_quality(path, capture)
    if quality_required and not quality["ok"]:
        raise TraeAutomationError(f"Screenshot quality check failed: {quality['reason']}")
    return {
        "path": str(path),
        "filename": path.name,
        "status": "captured",
        "content_type": "image/png",
        "size_bytes": path.stat().st_size,
        "target": target,
        "capture": capture,
        "quality": quality,
    }


def assess_screenshot_quality(path: str | Path, capture: dict | None = None) -> dict:
    capture = capture or {}
    try:
        image = Image.open(path).convert("RGB")
    except Exception as exc:
        return {"ok": False, "reason": "unreadable_image", "error": str(exc)}
    width, height = image.size
    if width < MIN_CAPTURE_WIDTH or height < MIN_CAPTURE_HEIGHT:
        return {"ok": False, "reason": "image_too_small", "width": width, "height": height}
    stats = _image_stats(image)
    if stats["dark_ratio"] >= 0.96:
        return {"ok": False, "reason": "mostly_black", "width": width, "height": height, **stats}
    if stats["bright_ratio"] >= 0.985:
        return {"ok": False, "reason": "mostly_blank", "width": width, "height": height, **stats}
    if stats["edge_ratio"] < 0.002:
        return {"ok": False, "reason": "low_detail", "width": width, "height": height, **stats}
    target = str(capture.get("target") or "")
    if target == "trae_window" and not _capture_looks_like_window(capture):
        return {"ok": False, "reason": "not_window_capture", "width": width, "height": height, **stats}
    return {"ok": True, "reason": "ok", "width": width, "height": height, **stats}


def _capture_target(
    path: Path,
    target: str,
    timeout_seconds: float,
    workspace_path: str | Path | None = None,
) -> dict:
    if target != "full_screen":
        try:
            return _capture_trae_window(path, timeout_seconds=timeout_seconds, workspace_path=workspace_path)
        except Exception as exc:
            if target == "trae_window":
                raise
            fallback = _capture_full_screen(path)
            fallback["window_error"] = str(exc)
            return fallback
    return _capture_full_screen(path)


def _capture_trae_window(path: Path, timeout_seconds: float, workspace_path: str | Path | None = None) -> dict:
    focus_result = focus_trae_workspace_or_any(
        timeout_seconds=timeout_seconds,
        workspace_path=workspace_path,
        prefer_workspace_match=bool(workspace_path),
    )
    window = wait_for_workspace_window_or_any(
        timeout_seconds=timeout_seconds,
        workspace_path=workspace_path,
        prefer_workspace_match=bool(workspace_path),
    )
    time.sleep(0.25)
    rect = _window_rect(int(window.hwnd))
    if not rect:
        raise TraeAutomationError("Could not read Trae window bounds for screenshot")
    left, top, right, bottom = rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    if width < MIN_CAPTURE_WIDTH or height < MIN_CAPTURE_HEIGHT:
        raise TraeAutomationError(f"Trae window bounds are too small for screenshot: {width}x{height}")
    _grab_region(path, left=left, top=top, width=width, height=height)
    return {
        "target": "trae_window",
        "window_title": window.window_text(),
        "bounds": {"left": left, "top": top, "right": right, "bottom": bottom, "width": width, "height": height},
        "focus": focus_result,
    }


def _capture_full_screen(path: Path) -> dict:
    import mss

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        image = sct.grab(monitor)
        img = Image.frombytes("RGB", image.size, image.rgb)
        img.save(path)
        return {
            "target": "full_screen",
            "bounds": {
                "left": int(monitor["left"]),
                "top": int(monitor["top"]),
                "width": int(monitor["width"]),
                "height": int(monitor["height"]),
                "right": int(monitor["left"]) + int(monitor["width"]),
                "bottom": int(monitor["top"]) + int(monitor["height"]),
            },
        }


def _grab_region(path: Path, *, left: int, top: int, width: int, height: int) -> None:
    import mss

    with mss.mss() as sct:
        image = sct.grab({"left": left, "top": top, "width": width, "height": height})
        img = Image.frombytes("RGB", image.size, image.rgb)
        img.save(path)


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


def _image_stats(image: Image.Image) -> dict[str, Any]:
    sample = image.resize((160, 90))
    pixels = list(sample.getdata())
    total = max(1, len(pixels))
    dark = 0
    bright = 0
    neutral_dark = 0
    edge = 0
    previous = None
    for red, green, blue in pixels:
        avg = (red + green + blue) / 3
        spread = max(red, green, blue) - min(red, green, blue)
        if avg < 28:
            dark += 1
        if avg > 245:
            bright += 1
        if avg < 80 and spread < 55:
            neutral_dark += 1
        if previous:
            prev_avg = sum(previous) / 3
            if abs(avg - prev_avg) > 35:
                edge += 1
        previous = (red, green, blue)
    return {
        "dark_ratio": round(dark / total, 4),
        "bright_ratio": round(bright / total, 4),
        "neutral_dark_ratio": round(neutral_dark / total, 4),
        "edge_ratio": round(edge / total, 4),
    }


def _capture_looks_like_window(capture: dict) -> bool:
    title = str(capture.get("window_title") or "")
    bounds = capture.get("bounds") if isinstance(capture.get("bounds"), dict) else {}
    width = int(bounds.get("width") or 0)
    height = int(bounds.get("height") or 0)
    return ("trae" in title.lower()) and width >= MIN_CAPTURE_WIDTH and height >= MIN_CAPTURE_HEIGHT


def _screenshot_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "AgentOps" / "screenshots"
    return Path.home() / ".agentops" / "screenshots"
