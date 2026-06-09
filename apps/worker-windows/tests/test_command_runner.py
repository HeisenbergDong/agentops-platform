from pathlib import Path
import subprocess

import pytest

from worker.runtime import command_runner
from worker.runtime.command_runner import CommandRunner
from worker.project.git_submit import run_git_submit
from worker.trae.trace_copy import probe_trace
from worker.trae.window import TraeAutomationError


def test_runner_uses_configured_worker_id():
    runner = CommandRunner(worker_id="worker-test")

    result = runner.run({"command_id": "cmd-1", "type": "stop_current_task", "payload": {}})

    assert result["worker_id"] == "worker-test"
    assert result["status"] == "success"
    assert result["data"] == {"stopped": True}


def test_send_prompt_opens_workspace_and_sends(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    opened = []

    monkeypatch.setattr(command_runner.settings, "workspace_root", tmp_path)
    monkeypatch.setattr(command_runner.settings, "trae_exe_path", Path("C:/Trae/Trae.exe"))
    monkeypatch.setattr(
        command_runner,
        "open_trae",
        lambda exe, workspace_path: opened.append((exe, workspace_path))
        or {"status": "launched", "workspace_path": str(workspace_path)},
    )
    monkeypatch.setattr(
        command_runner,
        "send_prompt",
        lambda prompt, submit, submit_hotkey: {
            "status": "sent",
            "chars": len(prompt),
            "submitted": submit,
            "submit_hotkey": submit_hotkey,
        },
    )

    result = CommandRunner(worker_id="worker-test").run(
        {
            "command_id": "cmd-2",
            "type": "send_prompt",
            "payload": {
                "prompt": "Build the feature",
                "trae_workspace_path": "project",
                "submit_hotkey": "^{ENTER}",
            },
        }
    )

    assert result["status"] == "success"
    assert result["data"]["status"] == "sent"
    assert result["data"]["submit_hotkey"] == "^{ENTER}"
    assert opened == [(Path("C:/Trae/Trae.exe"), workspace)]


def test_send_prompt_gui_failure_requires_manual_intervention(monkeypatch: pytest.MonkeyPatch):
    def raise_no_window(*args, **kwargs):
        raise TraeAutomationError("no window")

    monkeypatch.setattr(command_runner, "send_prompt", raise_no_window)

    result = CommandRunner(worker_id="worker-test").run(
        {"command_id": "cmd-3", "type": "send_prompt", "payload": {"prompt": "hello"}}
    )

    assert result["status"] == "manual_required"
    assert result["error"] == "no window"


def test_workspace_path_rejects_outside_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runner = CommandRunner(worker_id="worker-test")
    monkeypatch.setattr(command_runner.settings, "workspace_root", tmp_path)

    result = runner.run(
        {
            "command_id": "cmd-4",
            "type": "send_prompt",
            "payload": {"prompt": "hello", "workspace_path": str(tmp_path.parent)},
        }
    )

    assert result["status"] == "failed"
    assert "outside allowed root" in result["error"]


def test_wait_completion_routes_payload(monkeypatch: pytest.MonkeyPatch):
    received = {}

    def fake_wait_completion(timeout_seconds: float, stable_seconds: float, poll_interval_seconds: float):
        received["timeout_seconds"] = timeout_seconds
        received["stable_seconds"] = stable_seconds
        received["poll_interval_seconds"] = poll_interval_seconds
        return {"status": "completed"}

    monkeypatch.setattr(command_runner, "wait_completion", fake_wait_completion)

    result = CommandRunner(worker_id="worker-test").run(
        {
            "command_id": "cmd-5",
            "type": "wait_completion",
            "payload": {"timeout_seconds": 30, "stable_seconds": 3, "poll_interval_seconds": 1},
        }
    )

    assert result["status"] == "success"
    assert received == {"timeout_seconds": 30.0, "stable_seconds": 3.0, "poll_interval_seconds": 1.0}


def test_copy_latest_reply_routes_payload(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        command_runner,
        "copy_latest_reply",
        lambda timeout_seconds: {"status": "copied", "raw_text": "trace", "timeout_seconds": timeout_seconds},
    )

    result = CommandRunner(worker_id="worker-test").run(
        {"command_id": "cmd-6", "type": "copy_latest_reply", "payload": {"timeout_seconds": 7}}
    )

    assert result["status"] == "success"
    assert result["data"]["raw_text"] == "trace"
    assert result["data"]["timeout_seconds"] == 7.0


def test_scan_project_uses_workspace_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "package.json").write_text('{"scripts":{"test":"vitest","build":"vite build"}}', encoding="utf-8")
    monkeypatch.setattr(command_runner.settings, "workspace_root", tmp_path)

    result = CommandRunner(worker_id="worker-test").run(
        {"command_id": "cmd-7", "type": "scan_project", "payload": {"workspace_path": "project"}}
    )

    assert result["status"] == "success"
    assert result["data"]["status"] == "scanned"
    assert result["data"]["root"] == str(workspace)
    assert result["data"]["recommended_commands"] == [["npm", "test"], ["npm", "run", "build"]]


def test_browser_acceptance_routes_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    received = {}
    monkeypatch.setattr(command_runner.settings, "workspace_root", tmp_path)

    def fake_browser_acceptance(project_path: str, url: str, timeout_seconds: float):
        received["project_path"] = project_path
        received["url"] = url
        received["timeout_seconds"] = timeout_seconds
        return {"status": "passed", "url": url}

    monkeypatch.setattr(command_runner, "run_browser_acceptance", fake_browser_acceptance)

    result = CommandRunner(worker_id="worker-test").run(
        {
            "command_id": "cmd-8",
            "type": "browser_acceptance",
            "payload": {"workspace_path": "project", "browser_url": "localhost:5173", "timeout_seconds": 3},
        }
    )

    assert result["status"] == "success"
    assert result["data"]["status"] == "passed"
    assert received == {
        "project_path": str(workspace),
        "url": "localhost:5173",
        "timeout_seconds": 3.0,
    }


def test_git_submit_routes_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    received = {}
    monkeypatch.setattr(command_runner.settings, "workspace_root", tmp_path)

    def fake_git_submit(root, project_path, commit_message, push, remote, branch, timeout):
        received["root"] = root
        received["project_path"] = project_path
        received["commit_message"] = commit_message
        received["push"] = push
        received["remote"] = remote
        received["branch"] = branch
        received["timeout"] = timeout
        return {"status": "committed", "commit_sha": "abc123"}

    monkeypatch.setattr(command_runner, "run_git_submit", fake_git_submit)

    result = CommandRunner(worker_id="worker-test").run(
        {
            "command_id": "cmd-9",
            "type": "git_submit",
            "payload": {
                "workspace_path": "project",
                "commit_message": "AgentOps test",
                "push": False,
                "remote": "upstream",
                "branch": "feature/test",
                "timeout": 5,
            },
        }
    )

    assert result["status"] == "success"
    assert result["data"]["status"] == "committed"
    assert received == {
        "root": tmp_path,
        "project_path": workspace,
        "commit_message": "AgentOps test",
        "push": False,
        "remote": "upstream",
        "branch": "feature/test",
        "timeout": 5,
    }


def test_run_git_submit_commits_local_changes(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.name", "Test User")
    _git(project, "config", "user.email", "test@example.invalid")
    (project / "README.md").write_text("hello\n", encoding="utf-8")

    result = run_git_submit(tmp_path, project, commit_message="AgentOps test", push=False)

    assert result["status"] == "committed"
    assert result["changed_files"] == 1
    assert result["commit_sha"]


def test_run_git_submit_reports_nothing_to_commit(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init")

    result = run_git_submit(tmp_path, project, commit_message="AgentOps test", push=False)

    assert result["status"] == "nothing_to_commit"


def test_probe_trace_reports_full_trace_shape():
    trace = (
        "toolName: edit\n"
        "status: success\n"
        "filePath: app.py\n"
        "command: pytest\n"
        "Todos updated: done\n"
        + ("trace detail line\n" * 80)
    )

    result = probe_trace(trace)

    assert result["complete_like"] is True
    assert result["reason"] == "ok"


def test_probe_trace_reports_awaiting_continuation():
    trace = "toolName: edit\nstatus: success\n" + ("trace detail line\n" * 80) + "输出过长，请输入“继续”"

    result = probe_trace(trace)

    assert result["complete_like"] is False
    assert result["reason"] == "awaiting_continuation"


def test_probe_trace_reports_missing_tool_trace_markers():
    result = probe_trace("构建完成，测试通过。" * 100)

    assert result["complete_like"] is False
    assert result["reason"] == "missing_tool_trace_markers"


def test_probe_trace_allows_body_mentions_of_continue():
    text = "toolName: edit\nstatus: success\nfilePath: app.py\n" + ("continue detail line but finished\n" * 80)

    result = probe_trace(text)

    assert result["complete_like"] is True
    assert result["reason"] == "ok"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
