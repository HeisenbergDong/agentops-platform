from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any


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
    kill_trae: bool = False,
) -> dict[str, Any]:
    markers = _cleanup_markers(workspace_path, project_name)
    if not markers and not kill_trae:
        return {"status": "skipped", "reason": "missing_workspace_marker", "killed": []}

    candidates = _matching_processes(markers=markers, kill_trae=kill_trae)
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


def _matching_processes(*, markers: list[str], kill_trae: bool) -> list[dict[str, Any]]:
    rows = _query_processes()
    matches: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "").lower()
        command_line = str(row.get("command_line") or "")
        lowered_command_line = command_line.lower()
        matched_marker = next((marker for marker in markers if marker in lowered_command_line), "")
        sandbox = name == "trae-sandbox.exe"
        trae_main = name == "trae cn.exe"
        if sandbox and (markers or kill_trae):
            matched_marker = matched_marker or "trae-sandbox.exe"
        elif trae_main and kill_trae:
            matched_marker = matched_marker or "Trae CN.exe"
        elif name not in PROCESS_NAMES or not matched_marker:
            continue
        matches.append(
            {
                "pid": int(row.get("pid") or 0),
                "name": row.get("name") or "",
                "matched_marker": matched_marker,
                "command_line": command_line[:500],
            }
        )
    return matches


def _query_processes() -> list[dict[str, Any]]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress -Depth 2"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
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
                "name": item.get("Name") or "",
                "command_line": item.get("CommandLine") or "",
            }
        )
    return rows


def _kill_process_tree(pid: int) -> dict[str, Any]:
    completed = subprocess.run(
        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout_tail": (completed.stdout or "")[-500:],
        "stderr_tail": (completed.stderr or "")[-500:],
    }
