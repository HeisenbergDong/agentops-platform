import subprocess
import time
from pathlib import Path
from typing import Callable

from worker.project.dev_env import command_environment, resolve_tool
from worker.project.diagnostics import summarize_command_result
from worker.safety.command_guard import assert_allowed_command
from worker.safety.path_guard import assert_within_root


def run_project_command(
    root: Path,
    cwd: Path,
    command: list[str],
    timeout: int = 120,
    cancellation_check: Callable[[], None] | None = None,
) -> dict:
    assert_within_root(cwd, root)
    assert_allowed_command(command)
    resolved_command = _resolve_command(root, command)
    process = subprocess.Popen(
        resolved_command,
        cwd=cwd,
        env=command_environment(root),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = _communicate(process, timeout, cancellation_check)
    except Exception:
        _terminate_process(process)
        raise
    stdout = stdout or ""
    stderr = stderr or ""
    return {
        "returncode": process.returncode,
        "stdout": stdout[-8000:],
        "stderr": stderr[-8000:],
        "diagnostics": summarize_command_result(command, process.returncode, stdout, stderr),
    }


def _resolve_command(root: Path, command: list[str]) -> list[str]:
    if not command:
        return command
    executable = command[0].lower()
    if executable in {"npm", "python", "mvn", "go"}:
        return [resolve_tool(root, executable), *command[1:]]
    return command


def _communicate(
    process: subprocess.Popen[str],
    timeout: int,
    cancellation_check: Callable[[], None] | None,
) -> tuple[str, str]:
    deadline = time.monotonic() + max(1, timeout)
    while process.poll() is None:
        if cancellation_check:
            cancellation_check()
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(0.25)
    return process.communicate()


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
