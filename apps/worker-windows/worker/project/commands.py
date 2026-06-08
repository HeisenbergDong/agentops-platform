import subprocess
from pathlib import Path

from worker.safety.command_guard import assert_allowed_command
from worker.safety.path_guard import assert_within_root


def run_project_command(root: Path, cwd: Path, command: list[str], timeout: int = 120) -> dict:
    assert_within_root(cwd, root)
    assert_allowed_command(command)
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-8000:],
        "stderr": completed.stderr[-8000:],
    }
