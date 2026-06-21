from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from worker.trae.session_probe import (
    collect_log_files,
    parse_log_line,
    trae_appdata_root,
)

TRACE_MARKERS = ("toolName:", "status:", "filePath:", "command:", "Todos updated:")
CONTINUE_MARKERS = (
    "\u8f93\u51fa\u8fc7\u957f",
    "\u8bf7\u8f93\u5165\u201c\u7ee7\u7eed\u201d",
    "\u7ee7\u7eed\u751f\u6210",
    "exceeded output window",
    "input continue",
    "type continue",
    "continue generating",
)
SERVICE_INTERRUPTION_MARKERS = (
    "\u6a21\u578b\u8bf7\u6c42\u5931\u8d25",
    "\u670d\u52a1\u7aef\u5f02\u5e38",
    "\u670d\u52a1\u5f02\u5e38",
    "\u7f51\u7edc\u5f02\u5e38",
    "\u751f\u6210\u5931\u8d25",
    "(3003)",
    "3003",
    "ErrorResponse",
    "server error",
    "service error",
    "failed to generate",
)
MANUAL_STOP_MARKERS = (
    "\u624b\u52a8\u7ec8\u6b62\u8f93\u51fa",
    "\u5f53\u524d\u4efb\u52a1\u88ab\u624b\u52a8\u4e2d\u65ad",
    "\u624b\u52a8\u4e2d\u65ad",
    "manually stopped",
    "manual stop",
    "stopped manually",
)


def collect_local_trace(
    trae_turn: dict[str, Any] | None,
    *,
    prompt: str = "",
    workspace_path: str = "",
) -> dict[str, Any]:
    turn = normalize_turn_probe(trae_turn)
    db_trace = read_trace_from_db(turn, workspace_path=workspace_path)
    if _is_valid_local_trace(db_trace):
        return {
            "status": "collected",
            "raw_text": db_trace,
            "chars": len(db_trace),
            "trace_source": "trae_db_raw_trace",
            "trace_probe": probe_local_trace(db_trace),
        }

    log_trace = assemble_trace_from_logs(turn, prompt=prompt)
    if _is_valid_local_trace(log_trace):
        return {
            "status": "collected",
            "raw_text": log_trace,
            "chars": len(log_trace),
            "trace_source": "trae_local_raw_log_trace",
            "trace_probe": probe_local_trace(log_trace),
        }
    return {
        "status": "missing",
        "trace_source": "trae_local_raw_log_trace",
        "trace_probe": probe_local_trace(log_trace or db_trace),
        "chars": len(log_trace or db_trace),
    }


def normalize_turn_probe(trae_turn: dict[str, Any] | None) -> dict[str, Any]:
    turn = trae_turn if isinstance(trae_turn, dict) else {}
    if turn.get("session_id") or turn.get("user_message_id"):
        return turn
    candidate = turn.get("candidate") if isinstance(turn.get("candidate"), dict) else {}
    if candidate.get("session_id") or candidate.get("user_message_id"):
        return {**candidate, "probe_status": turn.get("status"), "probe_reason": turn.get("reason")}
    return turn


def read_trace_from_db(turn: dict[str, Any], *, workspace_path: str = "") -> str:
    session_id = str(turn.get("session_id") or "").strip()
    user_message_id = str(turn.get("user_message_id") or "").strip()
    if not session_id and not user_message_id:
        return ""
    try:
        root = trae_appdata_root() / "User" / "workspaceStorage"
    except RuntimeError:
        return ""
    if not root.exists():
        return ""

    requested_leaf = _path_leaf(workspace_path)
    db_paths = _recent_state_dbs(root, limit=12)
    for db_path in db_paths:
        if requested_leaf and not _workspace_db_matches(db_path, requested_leaf):
            continue
        try:
            rows = _item_rows(db_path)
        except Exception:
            continue
        candidates: list[str] = []
        for value in rows:
            identifiers = [item for item in (session_id, user_message_id) if item]
            if identifiers and not any(item in value for item in identifiers):
                continue
            try:
                parsed = json.loads(value)
            except Exception:
                continue
            _extract_text_candidates(parsed, candidates)
        for text in reversed(candidates):
            normalized = normalize_raw_trace(text)
            if _is_valid_local_trace(normalized):
                return normalized
    return ""


def assemble_trace_from_logs(turn: dict[str, Any], *, prompt: str = "") -> str:
    session_id = str(turn.get("session_id") or "").strip()
    user_message_id = str(turn.get("user_message_id") or "").strip()
    if not session_id or not user_message_id:
        return ""
    log_file = str(turn.get("log_file") or "").strip()
    paths: list[Path] = []
    if log_file:
        paths.append(Path(log_file))
    try:
        paths.extend(path for path in collect_log_files(trae_appdata_root() / "logs", max_log_files=8) if path not in paths)
    except RuntimeError:
        pass

    raw_lines: list[str] = []
    tool_lines: list[str] = []
    trace_ids = [str(item).strip() for item in (turn.get("trace_ids") or []) if str(item).strip()]
    trace_id = str(turn.get("trace_id") or (trace_ids[-1] if trace_ids else "")).strip()
    task_ids = [str(item).strip() for item in (turn.get("task_ids") or []) if str(item).strip()]
    task_id = str(turn.get("task_id") or (task_ids[-1] if task_ids else "")).strip()
    seen: set[str] = set()

    for path in paths:
        if not path.exists():
            continue
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not _line_matches_turn(line, session_id, user_message_id, task_id, trace_id):
                    continue
                raw = line.rstrip("\r\n")
                if not raw or raw in seen:
                    continue
                seen.add(raw)
                raw_lines.append(raw)
                event = parse_log_line(raw)
                if event.get("kind") == "tool_call_finish":
                    tool_lines.append(_tool_line_from_raw(raw, event, user_message_id))

    if not raw_lines and not tool_lines:
        return ""

    header = f"Trae raw execution trace for session {session_id}, user message {user_message_id}."
    if trace_id:
        header += f" trace_id={trace_id}."
    if prompt:
        header += f" prompt_sha={_prompt_sha(prompt)}."
    lines = [header]
    lines.extend(raw_lines)
    for line in tool_lines:
        if line and line not in seen:
            lines.append(line)
            seen.add(line)
    if str(turn.get("turn_status") or turn.get("status") or "").lower() == "completed":
        lines.append(f"Trae task completed. completed verified build run user_message_id: {user_message_id}")
    return normalize_raw_trace("\n".join(lines))


def normalize_raw_trace(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _is_valid_local_trace(text: str) -> bool:
    probe = probe_local_trace(text)
    return bool(probe.get("complete_like")) and str(probe.get("reason") or "") == "ok"


def probe_local_trace(text: str) -> dict[str, Any]:
    normalized = normalize_raw_trace(text)
    if not normalized:
        return {"complete_like": False, "reason": "empty_trace"}
    tail = normalized[-1600:].lower()
    if any(marker.lower() in tail for marker in CONTINUE_MARKERS):
        return {"complete_like": False, "reason": "awaiting_continuation", "chars": len(normalized)}
    if any(marker.lower() in normalized[-2400:].lower() for marker in MANUAL_STOP_MARKERS):
        return {"complete_like": False, "reason": "manual_stopped", "chars": len(normalized)}
    if any(marker.lower() in normalized[-2400:].lower() for marker in SERVICE_INTERRUPTION_MARKERS):
        return {"complete_like": False, "reason": "service_interrupted", "chars": len(normalized)}
    marker_count = sum(1 for marker in TRACE_MARKERS if marker in normalized)
    if marker_count == 0:
        return {"complete_like": False, "reason": "missing_tool_trace_markers", "chars": len(normalized)}
    if "toolName:" in normalized and "status:" not in normalized:
        return {"complete_like": False, "reason": "missing_status_marker", "chars": len(normalized)}
    return {"complete_like": len(normalized) >= 800, "reason": "ok", "chars": len(normalized), "marker_count": marker_count}


def _recent_state_dbs(root: Path, *, limit: int) -> list[Path]:
    candidates: list[tuple[float, Path]] = []
    for path in root.glob("*/state.vscdb"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return [path for _mtime, path in sorted(candidates, key=lambda item: item[0], reverse=True)[:limit]]


def _item_rows(db_path: Path) -> list[str]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT value FROM ItemTable").fetchall()
    values: list[str] = []
    for row in rows:
        value = row["value"]
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            values.append(value)
    return values


def _workspace_db_matches(db_path: Path, requested_leaf: str) -> bool:
    workspace_json = db_path.parent / "workspace.json"
    if not workspace_json.exists():
        return True
    try:
        data = json.loads(workspace_json.read_text(encoding="utf-8"))
    except Exception:
        return True
    folder = str(data.get("folder") or data.get("folder_path") or "").replace("/", "\\").lower()
    return not folder or requested_leaf.lower() in folder


def _extract_text_candidates(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        role = str(value.get("role") or value.get("sender") or value.get("author") or "").lower()
        if role in {"assistant", "ai", "agent"}:
            for key in ("content", "text", "message", "markdown", "answer"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    out.append(candidate)
        for item in value.values():
            _extract_text_candidates(item, out)
    elif isinstance(value, list):
        for item in value:
            _extract_text_candidates(item, out)


def _line_matches_turn(line: str, session_id: str, user_message_id: str, task_id: str, trace_id: str) -> bool:
    if user_message_id and user_message_id in line:
        return True
    if session_id and session_id in line and ("trace_id" in line or "message_id" in line or "task_id" in line):
        return True
    if task_id and task_id in line:
        return True
    if trace_id and trace_id in line:
        return True
    return False


def _tool_line_from_raw(raw: str, event: dict[str, Any], user_message_id: str) -> str:
    tool = "unknown"
    if 'toolcall_name: "' in raw:
        tool = raw.split('toolcall_name: "', 1)[1].split('"', 1)[0] or tool
    status = "unknown"
    if "status: " in raw:
        status = raw.split("status: ", 1)[1].split(",", 1)[0].strip() or status
    timestamp = str(event.get("timestamp") or "").strip()
    task_id = str(event.get("task_id") or "").strip()
    suffix = f", task_id: {task_id}" if task_id else ""
    return f"{timestamp} toolName: {tool}, status: {status}, command: local-trae-log, user_message_id: {user_message_id}{suffix}"


def _path_leaf(path: str) -> str:
    text = str(path or "").replace("/", "\\").rstrip("\\")
    return text.rsplit("\\", 1)[-1].strip()


def _prompt_sha(prompt: str) -> str:
    import hashlib

    return hashlib.sha256(str(prompt or "").encode("utf-8", errors="replace")).hexdigest()[:16]
