from __future__ import annotations

import os
import re
import sqlite3
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


HEX = r"[0-9a-f]{8,64}"
TRACE_RE = re.compile(rf'trace_id="(?P<trace_id>{HEX})"')
SESSION_EQ_RE = re.compile(rf"\bsession_id=(?P<session_id>{HEX})")
SESSION_COLON_RE = re.compile(rf"\bsession_id: (?P<session_id>{HEX})")
NEW_USER_MESSAGE_RE = re.compile(rf"new_user_message_id: (?P<message_id>{HEX})")
CREATE_MESSAGE_RE = re.compile(
    rf"create message, chat_session_id: (?P<session_id>{HEX}), message_id: (?P<message_id>{HEX})"
)
USER_MESSAGE_ID_RE = re.compile(rf"\buser_message_id: (?P<message_id>{HEX})")
MESSAGE_ID_RE = re.compile(rf'\bmessage_id[:=]\s*"?(?P<message_id>{HEX})"?')
TASK_RE = re.compile(rf"\btask_id[:=] (?P<task_id>{HEX})|\btask_id=(?P<task_id2>{HEX})")
TIMESTAMP_RE = re.compile(r"^(?P<ts>\S+)\s+")
TOOL_RE = re.compile(
    rf'plan tool call finish cost: (?P<cost_ms>\d+)ms, '
    rf'toolcall_name: "(?P<name>[^"]+)", status: (?P<status>[^,]+), '
    rf"user_message_id: (?P<user_message_id>{HEX}), task_id: (?P<task_id>{HEX})"
)

COMPLETED_MARKERS = (
    "main_routine completed",
    "chat_turn_finish completed",
    "normal path task exiting",
)
INTERRUPTION_MARKERS = (
    "task_failed",
    "send_error",
    "ErrorResponse",
    "服务端异常",
    "异常打断",
)


@dataclass
class TraeTurn:
    session_id: str
    user_message_id: str
    start_time: str = ""
    end_time: str = ""
    status: str = "unknown"
    trace_ids: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    event_count: int = 0
    tool_call_count: int = 0
    log_file: str = ""
    last_line_number: int = 0
    prompt_guess: str = ""
    workspace_folder: str = ""
    workspace_storage_id: str = ""
    match_score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "found",
            "session_id": self.session_id,
            "user_message_id": self.user_message_id,
            "trace_id": self.trace_ids[-1] if self.trace_ids else "",
            "trace_ids": self.trace_ids,
            "task_id": self.task_ids[-1] if self.task_ids else "",
            "task_ids": self.task_ids,
            "turn_status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "event_count": self.event_count,
            "tool_call_count": self.tool_call_count,
            "log_file": self.log_file,
            "last_line_number": self.last_line_number,
            "prompt_guess": self.prompt_guess[:800],
            "workspace_folder": self.workspace_folder,
            "workspace_storage_id": self.workspace_storage_id,
            "match_score": self.match_score,
            "confidence": "latest_completed_trae_log_turn" if self.status == "completed" else "latest_trae_log_turn",
        }


def probe_latest_trae_turn(
    max_log_files: int = 20,
    max_workspaces: int = 20,
    prompt: str = "",
    workspace_path: str = "",
    sent_after_epoch: float | None = None,
    sent_after: str = "",
) -> dict[str, Any]:
    try:
        root = trae_appdata_root()
    except RuntimeError as exc:
        return {"status": "missing", "reason": str(exc)}

    log_files = collect_log_files(root / "logs", max_log_files=max_log_files)
    if not log_files:
        return {"status": "missing", "reason": "no_trae_ai_agent_logs", "log_root": str(root / "logs")}

    workspaces = collect_workspaces(root / "User" / "workspaceStorage", max_workspaces=max_workspaces)
    session_ids = {
        session["session_id"]
        for workspace in workspaces
        for session in workspace.get("sessions", [])
        if session.get("session_id")
    }
    turns = parse_log_turns(log_files, session_ids=session_ids)
    probe_scope = "workspace_sessions"
    if not turns and session_ids:
        # Trae sometimes updates the ai-agent log before workspaceStorage catches up.
        # Fall back to scanning recent log identifiers directly so a real session id
        # is not lost just because the local state database is stale.
        turns = parse_log_turns(log_files, session_ids=set())
        probe_scope = "all_recent_logs"
    attach_prompts_to_turns(workspaces, turns)
    if not turns:
        return {
            "status": "missing",
            "reason": "no_trae_turn_identifiers",
            "log_files": [str(path) for path in log_files],
            "workspace_session_count": len(session_ids),
        }

    sent_after_ts = _sent_after_timestamp(sent_after_epoch, sent_after)
    scoped_turns = filter_turns_after(turns, sent_after_ts)
    if sent_after_ts and not scoped_turns:
        newest = select_best_turn(turns, prompt=prompt, workspace_path=workspace_path) if turns else None
        return {
            "status": "missing",
            "reason": "no_completed_turn_after_prompt_send",
            "candidate": newest.as_dict() if newest else {},
            "log_files_scanned": len(log_files),
            "workspace_count": len(workspaces),
            "probe_scope": probe_scope,
            "sent_after_epoch": sent_after_ts,
        }

    pending_turn = latest_pending_turn(scoped_turns or turns, prompt=prompt, workspace_path=workspace_path)
    if pending_turn:
        return {
            "status": "missing",
            "reason": "awaiting_current_continuation",
            "candidate": pending_turn.as_dict(),
            "log_files_scanned": len(log_files),
            "workspace_count": len(workspaces),
            "probe_scope": probe_scope,
        }

    selected = select_best_turn(scoped_turns or turns, prompt=prompt, workspace_path=workspace_path)
    validation_error = selected_turn_context_error(selected, prompt=prompt, workspace_path=workspace_path)
    if validation_error:
        return {
            "status": "missing",
            "reason": validation_error,
            "candidate": selected.as_dict(),
            "log_files_scanned": len(log_files),
            "workspace_count": len(workspaces),
            "probe_scope": probe_scope,
        }
    result = selected.as_dict()
    result["log_files_scanned"] = len(log_files)
    result["workspace_count"] = len(workspaces)
    result["probe_scope"] = probe_scope
    return result


def filter_turns_after(turns: list[TraeTurn], sent_after_epoch: float | None) -> list[TraeTurn]:
    if not sent_after_epoch:
        return turns
    result: list[TraeTurn] = []
    cutoff = sent_after_epoch - 5.0
    for turn in turns:
        turn_epoch = _turn_timestamp(turn)
        if turn_epoch is not None and turn_epoch >= cutoff:
            result.append(turn)
    return result


def trae_appdata_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set; cannot inspect Trae CN local logs")
    return Path(appdata) / "Trae CN"


def collect_log_files(log_root: Path, max_log_files: int = 8) -> list[Path]:
    if not log_root.exists():
        return []
    candidates: list[tuple[float, Path]] = []
    for dirpath, _dirnames, filenames in os.walk(log_root, onerror=lambda _exc: None):
        for filename in filenames:
            if not (filename.startswith("ai-agent_") and filename.endswith("_stdout.log")):
                continue
            path = Path(dirpath) / filename
            try:
                stat = path.stat()
            except OSError:
                continue
            candidates.append((stat.st_mtime, path))
    return [path for _mtime, path in sorted(candidates, key=lambda item: item[0], reverse=True)[:max_log_files]]


def parse_log_turns(log_files: list[Path], session_ids: set[str] | None = None) -> list[TraeTurn]:
    turns: dict[str, TraeTurn] = {}
    turns_by_task: dict[str, set[str]] = {}
    session_ids = session_ids or set()

    for log_file in log_files:
        try:
            handle = log_file.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line_number, line in enumerate(handle, 1):
                if session_ids and not any(session_id in line for session_id in session_ids):
                    continue
                event = parse_log_line(line)
                event_session_ids = event.get("session_ids") or []
                if session_ids:
                    event_session_ids = [session_id for session_id in event_session_ids if session_id in session_ids]
                message_id = event.get("user_message_id") or ""
                task_id = event.get("task_id") or ""
                target_keys: list[str] = []
                if task_id and task_id in turns_by_task and not message_id:
                    target_keys = list(turns_by_task[task_id])
                elif message_id and event_session_ids:
                    target_keys = [f"{session_id}:{message_id}" for session_id in event_session_ids]
                if not target_keys:
                    continue

                for key in target_keys:
                    session_id, user_message_id = key.split(":", 1)
                    turn = turns.setdefault(key, TraeTurn(session_id=session_id, user_message_id=user_message_id))
                    turn.event_count += 1
                    turn.log_file = str(log_file)
                    turn.last_line_number = line_number
                    timestamp = str(event.get("timestamp") or "")
                    kind = str(event.get("kind") or "")
                    if kind in {"chat_start", "create_message"} and not turn.start_time:
                        turn.start_time = timestamp
                    if kind == "tool_call_finish":
                        turn.tool_call_count += 1
                    if event.get("trace_id") and event["trace_id"] not in turn.trace_ids:
                        turn.trace_ids.append(event["trace_id"])
                    if task_id and task_id not in turn.task_ids:
                        turn.task_ids.append(task_id)
                        turns_by_task.setdefault(task_id, set()).add(key)
                    if kind == "turn_interrupted":
                        turn.status = "interrupted"
                        turn.end_time = timestamp or turn.end_time
                    elif kind in {"main_routine_completed", "chat_turn_finish_completed", "task_exiting"}:
                        if turn.status != "interrupted":
                            turn.status = "completed"
                        turn.end_time = timestamp or turn.end_time
    return list(turns.values())


def collect_workspaces(workspace_root: Path, max_workspaces: int = 20) -> list[dict[str, Any]]:
    if not workspace_root.exists():
        return []
    candidates: list[tuple[float, Path]] = []
    for path in workspace_root.glob("*/state.vscdb"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    db_paths = [path for _mtime, path in sorted(candidates, key=lambda item: item[0], reverse=True)[:max_workspaces]]
    workspaces: list[dict[str, Any]] = []
    for db_path in db_paths:
        ws_dir = db_path.parent
        workspace = {
            "workspace_storage_id": ws_dir.name,
            "workspace_storage_path": str(ws_dir),
            "workspace": read_workspace_json(ws_dir),
            "sessions": [],
            "prompts": [],
            "errors": [],
        }
        try:
            values = read_item_table(db_path)
        except Exception as exc:
            workspace["errors"].append(str(exc))
            workspaces.append(workspace)
            continue
        keys = values.pop("__keys__", [])
        user_id = extract_user_id(keys)
        session_store = maybe_json(values.get("memento/icube-ai-agent-storage")) or {}
        sessions = session_store.get("list", []) if isinstance(session_store, dict) else []
        current_session_id = session_store.get("currentSessionId") if isinstance(session_store, dict) else None
        agent_map = maybe_json(values.get("icube_session_agent_map")) or {}
        agent_map = agent_map if isinstance(agent_map, dict) else {}
        if not sessions and agent_map:
            sessions = [{"sessionId": session_id} for session_id in agent_map.keys()]
        model_map = {}
        if user_id:
            model_map = maybe_json(values.get(f"{user_id}_ai-chat:sessionRelation:modelMap")) or {}
        for item in sessions:
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("sessionId") or "").strip()
            if not session_id:
                continue
            workspace["sessions"].append(
                {
                    "session_id": session_id,
                    "is_current": bool(item.get("isCurrent") or session_id == current_session_id),
                    "agent": agent_map.get(session_id),
                    "model": model_map.get(session_id) if isinstance(model_map, dict) else None,
                }
            )
        prompts = maybe_json(values.get("icube-ai-agent-storage-input-history")) or []
        if isinstance(prompts, list):
            for index, item in enumerate(prompts):
                if not isinstance(item, dict):
                    continue
                input_text = item.get("inputText")
                if isinstance(input_text, str) and input_text.strip():
                    workspace["prompts"].append({"index": index + 1, "input_text": input_text})
        workspaces.append(workspace)
    return workspaces


def read_item_table(db_path: Path) -> dict[str, Any]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        key_rows = conn.execute("SELECT key FROM ItemTable").fetchall()
        keys = [row["key"] for row in key_rows]
        user_id = extract_user_id(keys)
        wanted_keys = {
            "memento/icube-ai-agent-storage",
            "icube_session_agent_map",
            "icube-ai-agent-storage-input-history",
        }
        if user_id:
            wanted_keys.add(f"{user_id}_ai-chat:sessionRelation:modelMap")
        placeholders = ",".join("?" for _item in wanted_keys)
        rows = conn.execute(
            f"SELECT key, value FROM ItemTable WHERE key IN ({placeholders})",
            sorted(wanted_keys),
        ).fetchall()
    values: dict[str, Any] = {"__keys__": keys}
    for row in rows:
        value = row["value"]
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        values[row["key"]] = value
    return values


def read_workspace_json(ws_dir: Path) -> dict[str, Any]:
    path = ws_dir / "workspace.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc)}
    folder = data.get("folder")
    if folder:
        data["folder_path"] = file_uri_to_path(str(folder))
    return data


def maybe_json(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def file_uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    path = unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    return path.replace("/", "\\")


def extract_user_id(keys: list[str]) -> str:
    for key in keys:
        match = re.match(r"^(\d+)_ai-chat:sessionRelation:", str(key))
        if match:
            return match.group(1)
    for key in keys:
        match = re.match(r"^currentAgentData_(\d+)$", str(key))
        if match:
            return match.group(1)
    return ""


def attach_prompts_to_turns(workspaces: list[dict[str, Any]], turns: list[TraeTurn]) -> None:
    turns_by_session: dict[str, list[TraeTurn]] = {}
    for turn in turns:
        turns_by_session.setdefault(turn.session_id, []).append(turn)
    for session_turns in turns_by_session.values():
        session_turns.sort(key=lambda item: (item.start_time or "", item.last_line_number))

    for workspace in workspaces:
        prompts = workspace.get("prompts") or []
        if not prompts:
            continue
        ordered_turns: list[TraeTurn] = []
        for session in workspace.get("sessions") or []:
            ordered_turns.extend(turns_by_session.get(str(session.get("session_id") or ""), []))
        ordered_turns.sort(key=lambda item: (item.start_time or "", item.last_line_number))
        for index, turn in enumerate(ordered_turns):
            if index >= len(prompts):
                break
            turn.prompt_guess = str(prompts[index].get("input_text") or "")
            turn.workspace_folder = str((workspace.get("workspace") or {}).get("folder_path") or "")
            turn.workspace_storage_id = str(workspace.get("workspace_storage_id") or "")


def select_best_turn(turns: list[TraeTurn], prompt: str = "", workspace_path: str = "") -> TraeTurn:
    prompt_norm = _normalize_text(prompt)
    workspace_norm = _normalize_path(workspace_path)

    def score(turn: TraeTurn) -> tuple[float, str, int]:
        value = 0.0
        if turn.status == "completed":
            value += 10
        if workspace_norm and _normalize_path(turn.workspace_folder) == workspace_norm:
            value += 8
        elif workspace_norm and workspace_norm and _normalize_path(turn.workspace_folder).endswith(workspace_norm.split("/")[-1]):
            value += 3
        if prompt_norm and turn.prompt_guess:
            value += 20 * _similarity(prompt_norm, _normalize_text(turn.prompt_guess))
        return (value, turn.end_time or turn.start_time, turn.last_line_number)

    candidates = sorted(turns, key=score, reverse=True)
    selected = candidates[0]
    selected.match_score = score(selected)[0]
    return selected


def latest_pending_turn(turns: list[TraeTurn], prompt: str = "", workspace_path: str = "") -> TraeTurn | None:
    completed = [turn for turn in turns if turn.status == "completed"]
    pending = [turn for turn in turns if turn.status != "completed"]
    if not pending:
        return None
    latest_completed_epoch = max((_turn_timestamp(turn) or 0.0) for turn in completed) if completed else 0.0
    context_pending = [
        turn
        for turn in pending
        if _turn_matches_requested_context(turn, prompt=prompt, workspace_path=workspace_path)
        and (_turn_timestamp(turn) or 0.0) >= latest_completed_epoch
    ]
    if not context_pending:
        return None
    return sorted(context_pending, key=lambda item: (_turn_timestamp(item) or 0.0, item.last_line_number), reverse=True)[0]


def selected_turn_context_error(turn: TraeTurn, prompt: str = "", workspace_path: str = "") -> str:
    prompt_norm = _normalize_text(prompt)
    workspace_norm = _normalize_path(workspace_path)
    turn_workspace = _normalize_path(turn.workspace_folder)
    prompt_similarity = _similarity(prompt_norm, _normalize_text(turn.prompt_guess)) if prompt_norm and turn.prompt_guess else 0.0
    workspace_matches = bool(workspace_norm and turn_workspace and _workspace_matches(turn_workspace, workspace_norm))

    if turn.status != "completed":
        return f"trae_turn_not_completed:{turn.status or 'unknown'}"
    if workspace_norm and turn_workspace and not workspace_matches:
        return "workspace_mismatch"
    if prompt_norm and turn.prompt_guess and prompt_similarity < 0.10:
        return "prompt_mismatch"
    if workspace_norm and prompt_norm and not workspace_matches and prompt_similarity < 0.10:
        return "low_confidence_context_match"
    return ""


def _turn_matches_requested_context(turn: TraeTurn, prompt: str = "", workspace_path: str = "") -> bool:
    prompt_norm = _normalize_text(prompt)
    workspace_norm = _normalize_path(workspace_path)
    if workspace_norm:
        turn_workspace = _normalize_path(turn.workspace_folder)
        if turn_workspace and not _workspace_matches(turn_workspace, workspace_norm):
            return False
    if prompt_norm and turn.prompt_guess:
        return _similarity(prompt_norm, _normalize_text(turn.prompt_guess)) >= 0.10
    return True


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())[:4000]


def _normalize_path(value: str) -> str:
    text = str(value or "").replace("\\", "/").strip().rstrip("/")
    return text.lower()


def _workspace_matches(turn_workspace: str, requested_workspace: str) -> bool:
    if not turn_workspace or not requested_workspace:
        return False
    if turn_workspace == requested_workspace:
        return True
    return turn_workspace.endswith("/" + requested_workspace.split("/")[-1])


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    a_terms = set(re.findall(r"[a-z][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", a))
    b_terms = set(re.findall(r"[a-z][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", b))
    if not a_terms or not b_terms:
        return 0.0
    return len(a_terms & b_terms) / max(len(a_terms), len(b_terms))


def _sent_after_timestamp(sent_after_epoch: float | None, sent_after: str) -> float | None:
    if sent_after_epoch:
        try:
            return float(sent_after_epoch)
        except (TypeError, ValueError):
            pass
    return _parse_timestamp(sent_after)


def _turn_timestamp(turn: TraeTurn) -> float | None:
    return _parse_timestamp(turn.end_time or turn.start_time)


def _parse_timestamp(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def parse_log_line(line: str) -> dict[str, Any]:
    event: dict[str, Any] = {}
    timestamp = TIMESTAMP_RE.search(line)
    if timestamp:
        event["timestamp"] = timestamp.group("ts")
    trace = TRACE_RE.search(line)
    if trace:
        event["trace_id"] = trace.group("trace_id")
    session_ids = SESSION_EQ_RE.findall(line) + SESSION_COLON_RE.findall(line)
    if session_ids:
        event["session_ids"] = sorted(set(session_ids))
    task = TASK_RE.search(line)
    if task:
        event["task_id"] = task.group("task_id") or task.group("task_id2")
    new_message = NEW_USER_MESSAGE_RE.search(line)
    if new_message:
        event["user_message_id"] = new_message.group("message_id")
        event["kind"] = "chat_start"
    create_message = CREATE_MESSAGE_RE.search(line)
    if create_message:
        event["session_ids"] = sorted(set(event.get("session_ids", []) + [create_message.group("session_id")]))
        event["user_message_id"] = create_message.group("message_id")
        event["kind"] = "create_message"
    if "message_id" in line and not event.get("user_message_id"):
        message = USER_MESSAGE_ID_RE.search(line) or MESSAGE_ID_RE.search(line)
        if message:
            event["user_message_id"] = message.group("message_id")
    tool = TOOL_RE.search(line)
    if tool:
        event.update(
            {
                "kind": "tool_call_finish",
                "user_message_id": tool.group("user_message_id"),
                "task_id": tool.group("task_id"),
            }
        )
    if any(marker in line for marker in COMPLETED_MARKERS):
        if "main_routine completed" in line:
            event["kind"] = "main_routine_completed"
        elif "chat_turn_finish completed" in line:
            event["kind"] = "chat_turn_finish_completed"
        else:
            event["kind"] = "task_exiting"
    if any(marker in line for marker in INTERRUPTION_MARKERS):
        event["kind"] = "turn_interrupted"
    return event
