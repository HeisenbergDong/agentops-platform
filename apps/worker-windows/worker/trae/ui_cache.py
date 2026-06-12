from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CACHE_VERSION = 1
MAX_FAILURES = 3


def default_cache_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "AgentOps" / "trae-ui-cache.json"
    return Path.home() / ".agentops" / "trae-ui-cache.json"


def load_cache(path: Path | str | None = None) -> dict[str, Any]:
    cache_path = Path(path).expanduser() if path else default_cache_path()
    if not cache_path.exists():
        return _empty_cache()
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_cache()
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return _empty_cache()
    data.setdefault("targets", {})
    return data


def save_cache(cache: dict[str, Any], path: Path | str | None = None) -> Path:
    cache_path = Path(path).expanduser() if path else default_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return cache_path


def candidate_targets(
    action: str,
    window_rect: tuple[int, int, int, int] | None,
    *,
    workspace_marker: str = "",
    path: Path | str | None = None,
) -> list[dict[str, Any]]:
    if not window_rect:
        return []
    cache = load_cache(path)
    entries = cache.get("targets", {}).get(action)
    if not isinstance(entries, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("disabled"):
            continue
        if int(entry.get("consecutive_failures") or 0) >= MAX_FAILURES:
            continue
        if not _workspace_matches(entry, workspace_marker):
            continue
        point = _point_from_entry(entry, window_rect)
        if not point:
            continue
        result.append(
            {
                **entry,
                "action": action,
                "center": {"x": point[0], "y": point[1]},
                "score": _entry_score(entry, window_rect),
                "method": f"cache:{entry.get('source') or entry.get('method') or 'unknown'}",
            }
        )
    return sorted(result, key=lambda item: item.get("score", 0), reverse=True)


def record_success(
    action: str,
    center: dict[str, Any] | tuple[int, int],
    window_rect: tuple[int, int, int, int] | None,
    *,
    source: str,
    method: str = "",
    confidence: float = 1.0,
    label: str = "",
    workspace_marker: str = "",
    path: Path | str | None = None,
) -> dict[str, Any]:
    if not window_rect:
        return {}
    x, y = _xy(center)
    left, top, right, bottom = window_rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    ratio = {"x": round((x - left) / width, 4), "y": round((y - top) / height, 4)}
    cache = load_cache(path)
    targets = cache.setdefault("targets", {})
    entries = targets.setdefault(action, [])
    now = _now()
    existing = _find_similar(entries, ratio, workspace_marker)
    if existing is None:
        existing = {
            "action": action,
            "ratio": ratio,
            "absolute": {"x": x, "y": y},
            "window_size": [width, height],
            "workspace_marker": workspace_marker,
            "source": source,
            "method": method or source,
            "label": label,
            "confidence": float(confidence),
            "success_count": 0,
            "failure_count": 0,
            "consecutive_failures": 0,
            "created_at": now,
        }
        entries.append(existing)
    existing.update(
        {
            "ratio": ratio,
            "absolute": {"x": x, "y": y},
            "window_size": [width, height],
            "workspace_marker": workspace_marker,
            "source": source,
            "method": method or source,
            "label": label or existing.get("label", ""),
            "confidence": max(float(confidence), float(existing.get("confidence") or 0)),
            "success_count": int(existing.get("success_count") or 0) + 1,
            "consecutive_failures": 0,
            "last_success_at": now,
            "disabled": False,
        }
    )
    save_cache(cache, path)
    return existing


def record_failure(
    action: str,
    target: dict[str, Any],
    *,
    reason: str = "",
    path: Path | str | None = None,
) -> dict[str, Any]:
    ratio = target.get("ratio") if isinstance(target.get("ratio"), dict) else target.get("click_ratio")
    if not isinstance(ratio, dict):
        return {}
    cache = load_cache(path)
    entries = cache.get("targets", {}).get(action)
    if not isinstance(entries, list):
        return {}
    existing = _find_similar(entries, ratio, str(target.get("workspace_marker") or ""))
    if existing is None:
        return {}
    existing["failure_count"] = int(existing.get("failure_count") or 0) + 1
    existing["consecutive_failures"] = int(existing.get("consecutive_failures") or 0) + 1
    existing["last_failure_at"] = _now()
    existing["last_failure_reason"] = reason[:300]
    if int(existing["consecutive_failures"]) >= MAX_FAILURES:
        existing["disabled"] = True
    save_cache(cache, path)
    return existing


def _empty_cache() -> dict[str, Any]:
    return {"version": CACHE_VERSION, "targets": {}}


def _point_from_entry(entry: dict[str, Any], window_rect: tuple[int, int, int, int]) -> tuple[int, int] | None:
    ratio = entry.get("ratio") if isinstance(entry.get("ratio"), dict) else {}
    try:
        rx = float(ratio.get("x"))
        ry = float(ratio.get("y"))
    except (TypeError, ValueError):
        return None
    if not (0 <= rx <= 1 and 0 <= ry <= 1):
        return None
    left, top, right, bottom = window_rect
    return int(left + max(1, right - left) * rx), int(top + max(1, bottom - top) * ry)


def _entry_score(entry: dict[str, Any], window_rect: tuple[int, int, int, int]) -> float:
    left, top, right, bottom = window_rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    stored = entry.get("window_size") if isinstance(entry.get("window_size"), list) else []
    size_score = 0.5
    if len(stored) == 2:
        try:
            size_delta = abs(width - int(stored[0])) / width + abs(height - int(stored[1])) / height
            size_score = max(0.0, 1.0 - size_delta)
        except Exception:
            size_score = 0.5
    return (
        int(entry.get("success_count") or 0) * 10
        + float(entry.get("confidence") or 0) * 5
        + size_score * 4
        - int(entry.get("failure_count") or 0) * 6
        - int(entry.get("consecutive_failures") or 0) * 10
    )


def _workspace_matches(entry: dict[str, Any], workspace_marker: str) -> bool:
    cached = str(entry.get("workspace_marker") or "").strip().lower()
    marker = str(workspace_marker or "").strip().lower()
    return not cached or not marker or cached == marker


def _find_similar(entries: list[Any], ratio: dict[str, Any], workspace_marker: str) -> dict[str, Any] | None:
    try:
        rx = float(ratio.get("x"))
        ry = float(ratio.get("y"))
    except (TypeError, ValueError):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or not _workspace_matches(entry, workspace_marker):
            continue
        entry_ratio = entry.get("ratio") if isinstance(entry.get("ratio"), dict) else {}
        try:
            ex = float(entry_ratio.get("x"))
            ey = float(entry_ratio.get("y"))
        except (TypeError, ValueError):
            continue
        if abs(rx - ex) <= 0.025 and abs(ry - ey) <= 0.025:
            return entry
    return None


def _xy(center: dict[str, Any] | tuple[int, int]) -> tuple[int, int]:
    if isinstance(center, tuple):
        return int(center[0]), int(center[1])
    return int(float(center.get("x"))), int(float(center.get("y")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
