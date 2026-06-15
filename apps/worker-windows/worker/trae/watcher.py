from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any


IGNORED_DIR_NAMES = {
    ".git",
    ".npm-cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}
MEANINGFUL_LOG_MARKERS = (
    "writeSSEFile",
    "runCommand",
    "RunCommand",
    "toolcall_name",
    "chat_turn",
    "chat_turn_finish",
    "main_routine",
    "normal path task exiting",
    "new_user_message_id",
    "create message",
    "task_id",
    "trace_id",
    "OpenPreview",
    "Package name",
    "Scaffolding",
    "Done.",
    "ERROR",
    "ErrorResponse",
    "send_error",
    "task_failed",
    "failed",
    "error:",
    "3003",
)
NOISY_LOG_MARKERS = (
    "checkRunCommandStatus",
    "ckg",
    "getDynamicConfig",
    "getExperiment",
    "get_detail_param",
    "model info",
    "model_list",
    "model list",
    "model_list_by_function",
    "refresh_token",
    "successfully synced model info",
    "sync model",
    "syncwait",
    "telemetry",
)
TIMESTAMP_PATTERN = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")


def activity_snapshot(
    project_path: str | os.PathLike[str] | None,
    started_at_epoch: float | None,
    quiet_seconds: float,
) -> dict[str, Any]:
    project_mtime, project_file = newest_mtime_under(project_path)
    log_tail = filtered_agent_log_tail()
    return _activity_from_sources(
        project_mtime=project_mtime,
        project_file=project_file,
        log_mtime=float(log_tail.get("activity_mtime") or 0.0),
        log_file=str(log_tail.get("path") or ""),
        started_at_epoch=started_at_epoch,
        quiet_seconds=quiet_seconds,
    )


def _activity_from_sources(
    *,
    project_mtime: float,
    project_file: str,
    log_mtime: float,
    log_file: str,
    started_at_epoch: float | None,
    quiet_seconds: float,
) -> dict[str, Any]:
    sources: list[tuple[str, float, str]] = []
    if project_mtime:
        sources.append(("project", project_mtime, project_file))
    if log_mtime:
        sources.append(("agent_log", log_mtime, log_file))
    if not sources:
        return {"recent": False, "quiet_seconds": None, "source": "", "path": "", "last_write": ""}

    source, newest, path = max(sources, key=lambda item: item[1])
    now = time.time()
    quiet = max(0.0, now - newest)
    start = _float_or_none(started_at_epoch)
    threshold = max(0.0, float(quiet_seconds or 0.0))
    recent = bool(start and newest >= start - 60.0 and (threshold <= 0.0 or quiet <= threshold))
    return {
        "recent": recent,
        "quiet_seconds": round(quiet, 1),
        "source": source,
        "path": path,
        "last_write": _iso_from_epoch(newest),
    }


def latest_agent_log_path() -> Path | None:
    candidates: list[tuple[float, Path]] = []
    for root in _trae_log_roots():
        if not root.exists():
            continue
        for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _exc: None):
            for filename in filenames:
                if not (filename.startswith("ai-agent_") and filename.endswith("_stdout.log")):
                    continue
                path = Path(dirpath) / filename
                try:
                    stat = path.stat()
                except OSError:
                    continue
                candidates.append((stat.st_mtime, path))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def filtered_agent_log_tail(max_lines: int = 160) -> dict[str, Any]:
    path = latest_agent_log_path()
    if not path:
        return {"path": "", "mtime": 0.0, "tail_hash": "", "lines": []}
    try:
        stat = path.stat()
    except OSError:
        stat = None
    try:
        text = _read_tail_text(path)
    except OSError as exc:
        return {
            "path": str(path),
            "mtime": stat.st_mtime if stat else 0.0,
            "tail_hash": "",
            "lines": [],
            "error": str(exc),
        }

    raw_lines = text.splitlines()[-4000:]
    lines: list[str] = []
    latest_meaningful_epoch = 0.0
    latest_meaningful_index = -1
    for index, line in enumerate(raw_lines):
        if _is_noisy_log_line(line):
            continue
        if _is_meaningful_log_line(line):
            lines.append(line[-900:])
            latest_meaningful_index = index
            latest_meaningful_epoch = _timestamp_from_log_line(line) or latest_meaningful_epoch
    lines = lines[-max(1, int(max_lines or 1)) :]
    digest = hashlib.sha256("\n".join(lines).encode("utf-8", errors="replace")).hexdigest()[:16] if lines else ""
    file_mtime = stat.st_mtime if stat else 0.0
    activity_mtime = latest_meaningful_epoch
    if lines and not activity_mtime and latest_meaningful_index == len(raw_lines) - 1:
        activity_mtime = file_mtime
    return {
        "path": str(path),
        "mtime": file_mtime,
        "activity_mtime": activity_mtime,
        "tail_hash": digest,
        "lines": lines,
    }


def build_trae_observation(
    *,
    project_path: str | os.PathLike[str] | None = None,
    started_at_epoch: float | None = None,
    quiet_seconds: float = 300.0,
    latest_text: str = "",
    turn_probe: dict[str, Any] | None = None,
    output_probe: dict[str, Any] | None = None,
    idle_seconds: float | None = None,
) -> dict[str, Any]:
    project_mtime, project_file = newest_mtime_under(project_path)
    log_tail = filtered_agent_log_tail()
    activity = _activity_from_sources(
        project_mtime=project_mtime,
        project_file=project_file,
        log_mtime=float(log_tail.get("activity_mtime") or 0.0),
        log_file=str(log_tail.get("path") or ""),
        started_at_epoch=started_at_epoch,
        quiet_seconds=quiet_seconds,
    )
    return {
        "turn_probe": turn_probe or {},
        "output_probe": output_probe or {},
        "activity": activity,
        "project_write": {
            "mtime": project_mtime,
            "path": project_file,
            "last_write": _iso_from_epoch(project_mtime),
        },
        "log": {key: value for key, value in log_tail.items() if key != "lines"},
        "log_sample": (log_tail.get("lines") or [])[-12:],
        "latest_text_hash": _text_hash(latest_text),
        "idle_seconds": round(float(idle_seconds or 0.0), 3),
    }


def newest_mtime_under(project_path: str | os.PathLike[str] | None) -> tuple[float, str]:
    if not project_path:
        return 0.0, ""
    root = Path(project_path)
    if not root.exists():
        return 0.0, ""
    newest = 0.0
    newest_path = ""
    if root.is_file():
        try:
            stat = root.stat()
        except OSError:
            return 0.0, ""
        return stat.st_mtime, str(root)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _exc: None):
        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIR_NAMES]
        for filename in filenames:
            path = Path(dirpath) / filename
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime > newest:
                newest = stat.st_mtime
                newest_path = str(path)
    return newest, newest_path


def newest_agent_log_mtime() -> tuple[float, str]:
    path = latest_agent_log_path()
    if not path:
        return 0.0, ""
    try:
        stat = path.stat()
    except OSError:
        return 0.0, str(path)
    return stat.st_mtime, str(path)


def _trae_log_roots() -> list[Path]:
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata) / "Trae CN"
        roots.extend([base / "User" / "logs", base / "logs"])
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _read_tail_text(path: Path, max_bytes: int = 1_200_000) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes), os.SEEK_SET)
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _is_noisy_log_line(line: str) -> bool:
    lowered = str(line or "").lower()
    return any(marker.lower() in lowered for marker in NOISY_LOG_MARKERS)


def _is_meaningful_log_line(line: str) -> bool:
    lowered = str(line or "").lower()
    return any(marker.lower() in lowered for marker in MEANINGFUL_LOG_MARKERS)


def _timestamp_from_log_line(line: str) -> float:
    match = TIMESTAMP_PATTERN.search(str(line or ""))
    if not match:
        return 0.0
    raw = match.group("ts").replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", raw):
        raw = f"{raw[:-2]}:{raw[-2:]}"
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return 0.0


def _text_hash(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(str(text)[-4000:].encode("utf-8", errors="replace")).hexdigest()[:16]


def _iso_from_epoch(value: float | int | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(float(value)).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _float_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None
