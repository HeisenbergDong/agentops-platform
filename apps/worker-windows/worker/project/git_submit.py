import re
import subprocess
from pathlib import Path
from typing import Any

from worker.safety.path_guard import assert_within_root


def run_git_submit(
    root: Path,
    project_path: Path,
    commit_message: str = "",
    push: bool = True,
    remote: str = "origin",
    branch: str = "",
    timeout: int = 120,
) -> dict:
    assert_within_root(project_path, root)
    message = commit_message.strip() or "AgentOps automated update"
    remote_name = (remote or "origin").strip()
    target_branch = branch.strip()

    inside = _git(project_path, ["rev-parse", "--is-inside-work-tree"], timeout)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {
            "status": "not_git_repository",
            "project_path": str(project_path),
            "message": "Project path is not a Git work tree.",
            "rev_parse": _run_result(inside),
        }

    current_branch = _git(project_path, ["rev-parse", "--abbrev-ref", "HEAD"], timeout)
    branch_name = current_branch.stdout.strip()
    remote_url = _git(project_path, ["remote", "get-url", remote_name], timeout)
    status_before = _git(project_path, ["status", "--porcelain=v1"], timeout)
    if status_before.returncode != 0:
        return _failed(
            "status_failed",
            project_path,
            "Could not read Git status.",
            status=status_before,
            branch=branch_name,
            remote=remote_name,
            remote_url=remote_url,
        )

    if not status_before.stdout.strip():
        head = _git(project_path, ["rev-parse", "HEAD"], timeout)
        return {
            "status": "nothing_to_commit",
            "project_path": str(project_path),
            "branch": branch_name,
            "remote": remote_name,
            "remote_url": _masked_stdout(remote_url),
            "commit_sha": head.stdout.strip() if head.returncode == 0 else "",
            "changed_files": 0,
            "push_requested": push,
            "message": "Git work tree has no changes to submit.",
        }

    add = _git(project_path, ["add", "-A"], timeout)
    if add.returncode != 0:
        return _failed(
            "add_failed",
            project_path,
            "Could not stage Git changes.",
            add=add,
            status=status_before,
            branch=branch_name,
            remote=remote_name,
            remote_url=remote_url,
        )

    staged = _git(project_path, ["diff", "--cached", "--name-only"], timeout)
    if staged.returncode != 0 or not staged.stdout.strip():
        return {
            "status": "nothing_to_commit",
            "project_path": str(project_path),
            "branch": branch_name,
            "remote": remote_name,
            "remote_url": _masked_stdout(remote_url),
            "changed_files": 0,
            "push_requested": push,
            "message": "No staged changes remained after Git add.",
        }

    commit = _git(
        project_path,
        [
            "-c",
            "user.name=AgentOps Worker",
            "-c",
            "user.email=agentops-worker@example.invalid",
            "commit",
            "-m",
            message,
        ],
        timeout,
    )
    if commit.returncode != 0:
        return _failed(
            "commit_failed",
            project_path,
            "Could not create Git commit.",
            commit=commit,
            staged=staged,
            branch=branch_name,
            remote=remote_name,
            remote_url=remote_url,
        )

    head = _git(project_path, ["rev-parse", "HEAD"], timeout)
    commit_sha = head.stdout.strip() if head.returncode == 0 else ""
    base = {
        "status": "committed",
        "project_path": str(project_path),
        "branch": branch_name,
        "remote": remote_name,
        "remote_url": _masked_stdout(remote_url),
        "commit_sha": commit_sha,
        "changed_files": len([line for line in staged.stdout.splitlines() if line.strip()]),
        "push_requested": push,
        "commit": _run_result(commit),
        "message": "Git commit created.",
    }
    if not push:
        return base

    push_args = ["push", remote_name]
    if target_branch:
        push_args.append(f"HEAD:{target_branch}")
    elif branch_name and branch_name != "HEAD":
        push_args.append(branch_name)
    push_result = _git(project_path, push_args, timeout)
    if push_result.returncode != 0:
        return {
            **base,
            "status": "push_failed",
            "push": _run_result(push_result),
            "message": "Git commit was created but push failed.",
        }
    return {
        **base,
        "status": "pushed",
        "pushed_branch": target_branch or branch_name,
        "push": _run_result(push_result),
        "message": "Git commit pushed.",
    }


def _git(cwd: Path, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _failed(failure_status: str, project_path: Path, message: str, **runs: Any) -> dict:
    return {
        "status": failure_status,
        "project_path": str(project_path),
        "message": message,
        **{key: _failure_value(key, value) for key, value in runs.items()},
    }


def _failure_value(key: str, value: Any) -> Any:
    if isinstance(value, subprocess.CompletedProcess):
        return _masked_stdout(value) if key == "remote_url" else _run_result(value)
    return value


def _run_result(result: subprocess.CompletedProcess[str]) -> dict:
    return {
        "returncode": result.returncode,
        "stdout": _trim(result.stdout),
        "stderr": _trim(result.stderr),
    }


def _trim(value: str, limit: int = 8000) -> str:
    return (value or "")[-limit:]


def _masked_stdout(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode != 0:
        return ""
    return _mask_remote_url(result.stdout.strip())


def _mask_remote_url(url: str) -> str:
    return re.sub(r"://[^/@]+@", "://***@", url)
