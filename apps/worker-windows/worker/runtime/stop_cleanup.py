from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import urlparse


PROCESS_NAMES = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "node.exe",
    "npm.cmd",
    "npm.exe",
    "pnpm.cmd",
    "pnpm.exe",
    "yarn.cmd",
    "yarn.exe",
    "python.exe",
    "pythonw.exe",
    "go.exe",
    "mvn.cmd",
    "mvn.exe",
    "java.exe",
    "javac.exe",
    "trae-sandbox.exe",
}


def cleanup_local_activity(
    *,
    workspace_path: Path | None,
    project_name: str = "",
    browser_url: str = "",
    kill_trae: bool = False,
) -> dict[str, Any]:
    markers = _cleanup_markers(workspace_path, project_name)
    if not markers and not kill_trae:
        return {"status": "skipped", "reason": "missing_workspace_marker", "killed": []}

    candidates = _matching_processes(
        markers=markers,
        kill_trae=kill_trae,
        metadata_pids=_metadata_pids(workspace_path),
        port_pids=_port_listener_pids(browser_url),
    )
    killed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in candidates:
        pid = int(item.get("pid") or 0)
        if not pid or pid in seen:
            continue
        seen.add(pid)
        result = _kill_process_tree(pid)
        record = {**item, **result}
        if result.get("ok"):
            killed.append(record)
        else:
            errors.append(record)
    status = "completed"
    if candidates and errors and not killed:
        status = "failed"
    elif candidates and errors:
        status = "partial"
    elif not candidates:
        status = "no_matching_processes"
    return {
        "status": status,
        "markers": markers,
        "kill_trae": kill_trae,
        "matched_count": len(candidates),
        "killed_count": len(killed),
        "error_count": len(errors),
        "killed": killed,
        "errors": errors,
    }


def _cleanup_markers(workspace_path: Path | None, project_name: str) -> list[str]:
    markers: list[str] = []
    if workspace_path:
        markers.append(str(workspace_path))
        markers.append(str(workspace_path).replace("/", "\\"))
        markers.append(workspace_path.name)
    clean_project_name = str(project_name or "").strip()
    if clean_project_name:
        markers.append(clean_project_name)
    result: list[str] = []
    for marker in markers:
        text = str(marker or "").strip()
        if len(text) < 4:
            continue
        lowered = text.lower()
        if lowered not in result:
            result.append(lowered)
    return result


def _matching_processes(
    *,
    markers: list[str],
    kill_trae: bool,
    metadata_pids: set[int] | None = None,
    port_pids: set[int] | None = None,
) -> list[dict[str, Any]]:
    rows = _query_processes()
    rows_by_pid = {int(row.get("pid") or 0): row for row in rows if int(row.get("pid") or 0)}
    child_pids_by_parent: dict[int, list[int]] = {}
    for row in rows:
        pid = int(row.get("pid") or 0)
        parent_pid = int(row.get("parent_pid") or 0)
        if pid and parent_pid:
            child_pids_by_parent.setdefault(parent_pid, []).append(pid)

    matches_by_pid: dict[int, dict[str, Any]] = {}
    metadata_pids = metadata_pids or set()
    port_pids = port_pids or set()
    for row in rows:
        pid = int(row.get("pid") or 0)
        name = str(row.get("name") or "").lower()
        command_line = str(row.get("command_line") or "")
        lowered_command_line = command_line.lower()
        matched_marker = next((marker for marker in markers if marker in lowered_command_line), "")
        sandbox = name == "trae-sandbox.exe"
        trae_main = name == "trae cn.exe"
        if pid in metadata_pids and name in PROCESS_NAMES:
            matched_marker = "agentops-started-dev-server"
        elif pid in port_pids and name in PROCESS_NAMES:
            matched_marker = "browser-url-listener"
        elif sandbox and (markers or kill_trae):
            matched_marker = matched_marker or "trae-sandbox.exe"
        elif trae_main and kill_trae:
            matched_marker = matched_marker or "Trae CN.exe"
        elif name not in PROCESS_NAMES or not matched_marker:
            continue
        matches_by_pid[pid] = _process_record(row, matched_marker)

    for root_pid, root in list(matches_by_pid.items()):
        for child_pid in _descendant_pids(root_pid, child_pids_by_parent):
            if child_pid in matches_by_pid:
                continue
            child = rows_by_pid.get(child_pid)
            if not child:
                continue
            child_name = str(child.get("name") or "").lower()
            if child_name not in PROCESS_NAMES:
                continue
            matches_by_pid[child_pid] = _process_record(child, f"child_of:{root.get('name') or root_pid}")
    return list(matches_by_pid.values())


def _process_record(row: dict[str, Any], matched_marker: str) -> dict[str, Any]:
    command_line = str(row.get("command_line") or "")
    return {
        "pid": int(row.get("pid") or 0),
        "parent_pid": int(row.get("parent_pid") or 0),
        "name": row.get("name") or "",
        "matched_marker": matched_marker,
        "command_line": command_line[:500],
    }


def _descendant_pids(root_pid: int, child_pids_by_parent: dict[int, list[int]]) -> list[int]:
    result: list[int] = []
    pending = list(child_pids_by_parent.get(root_pid, []))
    seen: set[int] = set()
    while pending:
        pid = pending.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
        pending.extend(child_pids_by_parent.get(pid, []))
    return result


def _metadata_pids(workspace_path: Path | None) -> set[int]:
    if not workspace_path:
        return set()
    metadata_path = workspace_path / ".agentops" / "browser-acceptance-server.json"
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    values = [data.get("pid")] if isinstance(data, dict) else []
    result: set[int] = set()
    for value in values:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            result.add(pid)
    return result


def _port_listener_pids(browser_url: str) -> set[int]:
    ports = _browser_ports(browser_url)
    if not ports:
        return set()
    script_ports = ",".join(str(port) for port in sorted(ports))
    script = (
        f"$ports=@({script_ports}); "
        "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
        "Where-Object { $ports -contains $_.LocalPort } | "
        "Select-Object OwningProcess,LocalPort | ConvertTo-Json -Compress -Depth 2"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if completed.returncode != 0:
        return set()
    return _parse_listener_pids(completed.stdout)


def _browser_ports(browser_url: str) -> set[int]:
    text = str(browser_url or "").strip()
    if not text:
        return set()
    if "://" not in text:
        text = f"http://{text}"
    try:
        parsed = urlparse(text)
    except ValueError:
        parsed = None
    port = parsed.port if parsed else None
    if not port:
        match = re.search(r":(?P<port>\d{2,5})(?:/|$)", text)
        port = int(match.group("port")) if match else 0
    if not port:
        return set()
    return {candidate for candidate in range(port, min(port + 8, 65536))}


def _parse_listener_pids(text: str) -> set[int]:
    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError:
        return set()
    if isinstance(data, dict):
        data = [data]
    result: set[int] = set()
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("OwningProcess") or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            result.add(pid)
    return result


def _query_processes() -> list[dict[str, Any]]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress -Depth 2"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return _parse_process_json(completed.stdout)


def _parse_process_json(text: str) -> list[dict[str, Any]]:
    import json

    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    rows: list[dict[str, Any]] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "pid": item.get("ProcessId"),
                "parent_pid": item.get("ParentProcessId"),
                "name": item.get("Name") or "",
                "command_line": item.get("CommandLine") or "",
            }
        )
    return rows


def _kill_process_tree(pid: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout_tail": (exc.stdout or "")[-500:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": f"taskkill timed out after {exc.timeout} seconds",
        }
    except OSError as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": str(exc)[-500:],
        }
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout_tail": (completed.stdout or "")[-500:],
        "stderr_tail": (completed.stderr or "")[-500:],
    }
