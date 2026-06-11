import subprocess
from pathlib import Path

from worker.project.dev_env import command_environment, resolve_tool
from worker.safety.command_guard import assert_allowed_command
from worker.safety.path_guard import assert_within_root


def run_project_command(root: Path, cwd: Path, command: list[str], timeout: int = 120) -> dict:
    assert_within_root(cwd, root)
    assert_allowed_command(command)
    resolved_command = _resolve_command(root, command)
    completed = subprocess.run(
        resolved_command,
        cwd=cwd,
        env=command_environment(root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "")[-8000:],
        "stderr": (completed.stderr or "")[-8000:],
    }


def _resolve_command(root: Path, command: list[str]) -> list[str]:
    if not command:
        return command
    executable = command[0].lower()
    if executable in {"npm", "python", "mvn", "go"}:
        return [resolve_tool(root, executable), *command[1:]]
    return command
