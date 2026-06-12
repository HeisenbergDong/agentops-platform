import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from worker.safety.path_guard import assert_within_root


def run_git_submit(
    root: Path,
    project_path: Path,
    commit_message: str = "",
    push: bool = True,
    remote: str = "origin",
    branch: str = "",
    remote_url: str = "",
    project_name: str = "",
    timeout: int = 120,
    cancellation_check: Callable[[], None] | None = None,
) -> dict:
    if cancellation_check:
        cancellation_check()
    assert_within_root(project_path, root)
    message = commit_message.strip() or "AgentOps automated update"
    remote_name = (remote or "origin").strip()
    target_branch = branch.strip() or "main"
    desired_remote_url = str(remote_url or "").strip()

    repo_prepare = _ensure_git_repository(project_path, target_branch, remote_name, desired_remote_url, timeout, cancellation_check)
    inside = _git(project_path, ["rev-parse", "--is-inside-work-tree"], timeout, cancellation_check)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {
            "status": "not_git_repository",
            "project_path": str(project_path),
            "message": "Project path is not a Git work tree.",
            "rev_parse": _run_result(inside),
            "repo_prepare": repo_prepare,
        }

    branch_name = _current_branch(project_path, timeout, cancellation_check)
    remote_url = _git(project_path, ["remote", "get-url", remote_name], timeout, cancellation_check)
    status_before = _git(project_path, ["status", "--porcelain=v1"], timeout, cancellation_check)
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

    if status_before.stdout.strip():
        _ensure_default_gitignore(project_path)
        status_before = _git(project_path, ["status", "--porcelain=v1"], timeout, cancellation_check)

    if not status_before.stdout.strip():
        head = _git(project_path, ["rev-parse", "HEAD"], timeout, cancellation_check)
        base = {
            "status": "nothing_to_commit",
            "project_path": str(project_path),
            "branch": branch_name,
            "remote": remote_name,
            "remote_url": _masked_stdout(remote_url),
            "project_name": project_name or project_path.name,
            "repo_prepare": repo_prepare,
            "commit_sha": head.stdout.strip() if head.returncode == 0 else "",
            "changed_files": 0,
            "push_requested": push,
            "message": "Git work tree has no changes to submit.",
        }
        if not push:
            return base
        return _push_existing_head(project_path, base, remote_name, target_branch, branch_name, timeout, cancellation_check)

    add = _git(project_path, ["add", "-A"], timeout, cancellation_check)
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

    staged = _git(project_path, ["diff", "--cached", "--name-only"], timeout, cancellation_check)
    if staged.returncode != 0 or not staged.stdout.strip():
        head = _git(project_path, ["rev-parse", "HEAD"], timeout, cancellation_check)
        base = {
            "status": "nothing_to_commit",
            "project_path": str(project_path),
            "branch": branch_name,
            "remote": remote_name,
            "remote_url": _masked_stdout(remote_url),
            "project_name": project_name or project_path.name,
            "repo_prepare": repo_prepare,
            "commit_sha": head.stdout.strip() if head.returncode == 0 else "",
            "changed_files": 0,
            "push_requested": push,
            "message": "No staged changes remained after Git add.",
        }
        if not push:
            return base
        return _push_existing_head(project_path, base, remote_name, target_branch, branch_name, timeout, cancellation_check)

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
        cancellation_check,
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

    head = _git(project_path, ["rev-parse", "HEAD"], timeout, cancellation_check)
    commit_sha = head.stdout.strip() if head.returncode == 0 else ""
    base = {
        "status": "committed",
        "project_path": str(project_path),
        "branch": branch_name,
        "remote": remote_name,
        "remote_url": _masked_stdout(remote_url),
        "project_name": project_name or project_path.name,
        "repo_prepare": repo_prepare,
        "commit_sha": commit_sha,
        "changed_files": _changed_file_count(staged.stdout),
        "push_requested": push,
        "commit": _run_result(commit),
        "message": "Git commit created.",
    }
    if not push:
        return base

    push_args = _push_args(project_path, remote_name, target_branch, branch_name, timeout, cancellation_check)
    push_result = _git(project_path, push_args, timeout, cancellation_check)
    if push_result.returncode != 0:
        return {
            **base,
            "status": "push_failed",
            "push": _run_result(push_result),
            "push_diagnostics": _push_diagnostics(push_result),
            "message": "Git commit was created but push failed.",
        }
    return {
        **base,
        "status": "pushed",
        "pushed_branch": target_branch or branch_name,
        "push": _run_result(push_result),
        "message": "Git commit pushed.",
    }


def _git(
    cwd: Path,
    args: list[str],
    timeout: int,
    cancellation_check: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    if cancellation_check:
        cancellation_check()
    process = subprocess.Popen(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = _communicate(process, timeout, cancellation_check)
    except Exception:
        _terminate_process(process)
        raise
    return subprocess.CompletedProcess(
        args=["git", *args],
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _ensure_git_repository(
    project_path: Path,
    branch_name: str,
    remote_name: str,
    remote_url: str,
    timeout: int,
    cancellation_check: Callable[[], None] | None = None,
) -> dict:
    project_path.mkdir(parents=True, exist_ok=True)
    initialized = False
    inside = _git(project_path, ["rev-parse", "--is-inside-work-tree"], timeout, cancellation_check)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        init = _git(project_path, ["init", "-b", branch_name], timeout, cancellation_check)
        if init.returncode != 0:
            init = _git(project_path, ["init"], timeout, cancellation_check)
            if init.returncode != 0:
                return {"ok": False, "stage": "init", "init": _run_result(init)}
            branch = _git(project_path, ["branch", "-M", branch_name], timeout, cancellation_check)
            if branch.returncode != 0:
                return {"ok": False, "stage": "branch", "branch": _run_result(branch)}
        initialized = True
    else:
        branch = _current_branch(project_path, timeout, cancellation_check)
        if branch and branch != branch_name:
            checkout = _git(project_path, ["checkout", branch_name], timeout, cancellation_check)
            if checkout.returncode != 0:
                checkout = _git(project_path, ["checkout", "-B", branch_name], timeout, cancellation_check)
            if checkout.returncode != 0:
                return {"ok": False, "stage": "checkout", "checkout": _run_result(checkout)}

    remote_changed = False
    if remote_url:
        existing_remote = _git(project_path, ["remote", "get-url", remote_name], timeout, cancellation_check)
        if existing_remote.returncode == 0 and existing_remote.stdout.strip():
            if existing_remote.stdout.strip() != remote_url:
                set_url = _git(project_path, ["remote", "set-url", remote_name, remote_url], timeout, cancellation_check)
                if set_url.returncode != 0:
                    return {"ok": False, "stage": "remote_set_url", "remote": _run_result(set_url)}
                remote_changed = True
        else:
            add = _git(project_path, ["remote", "add", remote_name, remote_url], timeout, cancellation_check)
            if add.returncode != 0:
                return {"ok": False, "stage": "remote_add", "remote": _run_result(add)}
            remote_changed = True
    return {
        "ok": True,
        "initialized": initialized,
        "remote_changed": remote_changed,
        "branch": branch_name,
        "remote": remote_name,
        "remote_url": _mask_remote_url(remote_url),
    }


DEFAULT_IGNORE_LINES = [
    "node_modules/",
    "dist/",
    "build/",
    "target/",
    ".next/",
    ".nuxt/",
    ".npm-cache/",
    "screenshots/",
    "trae_reply_traces/",
    "pending_*.json",
    "trae_collect_export.json",
    "trae_watch_status.json",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    "*.pyc",
]


def _ensure_default_gitignore(project_path: Path) -> None:
    path = project_path / ".gitignore"
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    existing_lines = {line.strip() for line in existing.splitlines()}
    additions = [line for line in DEFAULT_IGNORE_LINES if line not in existing_lines]
    if not additions:
        return
    text = existing.rstrip()
    if text:
        text += "\n"
    path.write_text(text + "\n".join(additions) + "\n", encoding="utf-8")


def _changed_file_count(value: str) -> int:
    files = []
    for line in str(value or "").splitlines():
        text = line.strip()
        if not text:
            continue
        path = text.split("->")[-1].strip()
        if path.replace("\\", "/") == ".gitignore":
            continue
        files.append(path)
    return len(files)


def _current_branch(
    project_path: Path,
    timeout: int,
    cancellation_check: Callable[[], None] | None = None,
) -> str:
    current = _git(project_path, ["branch", "--show-current"], timeout, cancellation_check)
    if current.returncode == 0 and current.stdout.strip():
        return current.stdout.strip()
    fallback = _git(project_path, ["rev-parse", "--abbrev-ref", "HEAD"], timeout, cancellation_check)
    return fallback.stdout.strip() if fallback.returncode == 0 else ""


def _push_args(
    project_path: Path,
    remote_name: str,
    target_branch: str,
    branch_name: str,
    timeout: int,
    cancellation_check: Callable[[], None] | None = None,
) -> list[str]:
    if target_branch and target_branch != branch_name:
        return ["push", remote_name, f"HEAD:{target_branch}"]
    if branch_name and branch_name != "HEAD":
        upstream = _git(
            project_path,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            timeout,
            cancellation_check,
        )
        if upstream.returncode != 0:
            return ["push", "--set-upstream", remote_name, branch_name]
        return ["push", remote_name, branch_name]
    return ["push", remote_name]


def _push_existing_head(
    project_path: Path,
    base: dict,
    remote_name: str,
    target_branch: str,
    branch_name: str,
    timeout: int,
    cancellation_check: Callable[[], None] | None = None,
) -> dict:
    push_args = _push_args(project_path, remote_name, target_branch, branch_name, timeout, cancellation_check)
    push_result = _git(project_path, push_args, timeout, cancellation_check)
    if push_result.returncode != 0:
        return {
            **base,
            "status": "push_failed",
            "push": _run_result(push_result),
            "push_diagnostics": _push_diagnostics(push_result),
            "message": "Git work tree had no new staged changes, but pushing the existing HEAD failed.",
        }
    return {
        **base,
        "status": "pushed",
        "pushed_branch": target_branch or branch_name,
        "push": _run_result(push_result),
        "message": "Git work tree had no new staged changes; existing HEAD was pushed.",
    }


def _push_diagnostics(result: subprocess.CompletedProcess[str]) -> dict:
    output = "\n".join(part for part in [result.stderr, result.stdout] if part)
    reason = _classify_push_failure(output)
    return {
        "reason": reason,
        "returncode": result.returncode,
        "message": _first_push_error_line(output),
        "credential_hint": _credential_hint(reason),
    }


def _classify_push_failure(output: str) -> str:
    text = str(output or "").lower()
    if any(token in text for token in ("permission denied (publickey)", "publickey", "could not read from remote repository")):
        return "ssh_key_or_deploy_key_failed"
    if any(token in text for token in ("authentication failed", "could not read username", "terminal prompts disabled", "repository not found")):
        return "https_token_or_credential_failed"
    if any(token in text for token in ("403", "write access", "permission denied", "protected branch")):
        return "remote_permission_denied"
    if any(token in text for token in ("failed to connect", "could not resolve host", "connection timed out", "network is unreachable")):
        return "network_failed"
    if any(token in text for token in ("non-fast-forward", "fetch first", "rejected")):
        return "non_fast_forward_rejected"
    return "push_failed"


def _first_push_error_line(output: str) -> str:
    for line in str(output or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.search(r"(fatal|error|denied|failed|rejected|403|permission|authentication)", stripped, re.IGNORECASE):
            return stripped[:240]
    return ""


def _credential_hint(reason: str) -> str:
    if reason == "ssh_key_or_deploy_key_failed":
        return "Verify the worker SSH key is loaded and the repository deploy key has write access."
    if reason == "https_token_or_credential_failed":
        return "Verify the HTTPS remote uses a valid GitHub token or configured credential helper."
    if reason == "remote_permission_denied":
        return "Verify GitHub token scopes, deploy key write access, and branch protection."
    return ""


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
