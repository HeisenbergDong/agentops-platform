from pathlib import Path
import subprocess

import pytest

from worker.runtime import command_runner
from worker.runtime.command_runner import _current_turn_gate
from worker.runtime.command_runner import CommandRunner
from worker.project.git_submit import run_git_submit
from worker.trae.diagnose import detect_terminal_prompt
from worker.trae.trace_copy import probe_trace, scroll_assistant_to_bottom
from worker.trae import window as trae_window
from worker.trae.window import TraeAutomationError


def test_runner_uses_configured_worker_id():
    runner = CommandRunner(worker_id="worker-test")

    result = runner.run({"command_id": "cmd-1", "type": "stop_current_task", "payload": {}})

    assert result["worker_id"] == "worker-test"
    assert result["status"] == "success"
    assert result["data"] == {"stopped": True}


def test_send_prompt_uses_workspace_without_forcing_new_trae_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    ensured = []

    monkeypatch.setattr(command_runner.settings, "workspace_root", tmp_path)
    monkeypatch.setattr(command_runner.settings, "trae_exe_path", Path("C:/Trae/Trae.exe"))
    monkeypatch.setattr(
        command_runner,
        "ensure_trae_running",
        lambda exe, workspace_path, launch_timeout_seconds, force_open_workspace=False: ensured.append(
            (exe, workspace_path, launch_timeout_seconds, force_open_workspace)
        )
        or {"status": "launched", "workspace_path": str(workspace_path), "window_title": "Trae"},
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
    assert result["data"]["open_trae"]["status"] == "launched"
    assert ensured == [(Path("C:/Trae/Trae.exe"), workspace, 30.0, False)]


def test_send_prompt_auto_starts_trae_without_workspace_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    ensured = []
    settings = command_runner.WorkerSettings(
        workspace_root=tmp_path,
        trae_exe_path=Path("C:/Trae/Trae.exe"),
    )

    monkeypatch.setattr(
        command_runner,
        "ensure_trae_running",
        lambda exe, workspace_path, launch_timeout_seconds, force_open_workspace=False: ensured.append(
            (exe, workspace_path, launch_timeout_seconds, force_open_workspace)
        )
        or {"status": "already_running", "workspace_path": str(workspace_path), "window_title": "Trae"},
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

    result = CommandRunner(worker_id="worker-test", runtime_settings=settings).run(
        {"command_id": "cmd-2b", "type": "send_prompt", "payload": {"prompt": "Build the feature"}}
    )

    assert result["status"] == "success"
    assert result["data"]["open_trae"]["status"] == "already_running"
    assert ensured == [(Path("C:/Trae/Trae.exe"), tmp_path, 30.0, False)]


def test_send_prompt_gui_failure_requires_manual_intervention(monkeypatch: pytest.MonkeyPatch):
    def raise_no_window(*args, **kwargs):
        raise TraeAutomationError("no window")

    monkeypatch.setattr(
        command_runner,
        "ensure_trae_running",
        lambda exe, workspace_path, launch_timeout_seconds, force_open_workspace=False: {"status": "already_running"},
    )
    monkeypatch.setattr(command_runner, "send_prompt", raise_no_window)

    result = CommandRunner(worker_id="worker-test").run(
        {"command_id": "cmd-3", "type": "send_prompt", "payload": {"prompt": "hello"}}
    )

    assert result["status"] == "manual_required"
    assert result["error"] == "no window"


def test_focus_trae_launches_when_missing_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    ensured = []
    settings = command_runner.WorkerSettings(
        workspace_root=tmp_path,
        trae_exe_path=Path("C:/Trae/Trae.exe"),
    )

    monkeypatch.setattr(
        command_runner,
        "ensure_trae_running",
        lambda exe, workspace_path, launch_timeout_seconds, force_open_workspace=False: ensured.append(
            (exe, workspace_path, launch_timeout_seconds, force_open_workspace)
        )
        or {"status": "launched", "window_title": "Trae"},
    )

    result = CommandRunner(worker_id="worker-test", runtime_settings=settings).run(
        {"command_id": "cmd-3b", "type": "focus_trae", "payload": {}}
    )

    assert result["status"] == "success"
    assert result["data"]["status"] == "launched"
    assert ensured == [(Path("C:/Trae/Trae.exe"), tmp_path, 30.0, False)]


def test_trae_window_diagnostics_lists_multiple_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        trae_window,
        "_find_top_level_windows",
        lambda marker: [(101, "Trae CN - first"), (202, "Trae CN - second")],
    )

    result = trae_window.trae_window_diagnostics(selected_hwnd=202)

    assert result["count"] == 2
    assert result["selected_hwnd"] == 202
    assert result["windows"] == [
        {"hwnd": 101, "title": "Trae CN - first", "selected": False},
        {"hwnd": 202, "title": "Trae CN - second", "selected": True},
    ]


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

    def fake_wait_completion(
        timeout_seconds: float,
        stable_seconds: float,
        poll_interval_seconds: float,
        intervention_idle_seconds: float,
        max_interventions: int,
        cancellation_check=None,
    ):
        received["timeout_seconds"] = timeout_seconds
        received["stable_seconds"] = stable_seconds
        received["poll_interval_seconds"] = poll_interval_seconds
        received["intervention_idle_seconds"] = intervention_idle_seconds
        received["max_interventions"] = max_interventions
        received["cancellable"] = callable(cancellation_check)
        return {"status": "completed"}

    monkeypatch.setattr(command_runner, "wait_completion", fake_wait_completion)

    result = CommandRunner(worker_id="worker-test").run(
        {
            "command_id": "cmd-5",
            "type": "wait_completion",
            "payload": {
                "timeout_seconds": 30,
                "stable_seconds": 3,
                "poll_interval_seconds": 1,
                "intervention_idle_seconds": 11,
                "max_interventions": 5,
            },
        }
    )

    assert result["status"] == "success"
    assert received == {
        "timeout_seconds": 30.0,
        "stable_seconds": 3.0,
        "poll_interval_seconds": 1.0,
        "intervention_idle_seconds": 11.0,
        "max_interventions": 5,
        "cancellable": True,
    }


def test_wait_completion_cancelled_by_server(monkeypatch: pytest.MonkeyPatch):
    def fake_wait_completion(
        timeout_seconds: float,
        stable_seconds: float,
        poll_interval_seconds: float,
        intervention_idle_seconds: float,
        max_interventions: int,
        cancellation_check=None,
    ):
        assert cancellation_check is not None
        cancellation_check()
        return {"status": "completed"}

    monkeypatch.setattr(command_runner, "wait_completion", fake_wait_completion)
    runner = CommandRunner(worker_id="worker-test", cancellation_checker=lambda command_id: command_id == "cmd-cancel")

    result = runner.run(
        {
            "command_id": "cmd-cancel",
            "type": "wait_completion",
            "payload": {"timeout_seconds": 30, "stable_seconds": 3, "poll_interval_seconds": 1},
        }
    )

    assert result["status"] == "cancelled"
    assert "cancelled" in result["message"].lower()


def test_capture_screenshot_routes_quality_payload(monkeypatch: pytest.MonkeyPatch):
    received = {}

    def fake_capture_screenshot(target: str, timeout_seconds: float, quality_required: bool):
        received["target"] = target
        received["timeout_seconds"] = timeout_seconds
        received["quality_required"] = quality_required
        return {"status": "captured", "path": "screen.png"}

    monkeypatch.setattr(command_runner, "capture_screenshot", fake_capture_screenshot)

    result = CommandRunner(worker_id="worker-test").run(
        {
            "command_id": "cmd-shot",
            "type": "capture_screenshot",
            "payload": {"target": "full_screen", "timeout_seconds": 4, "quality_required": False},
        }
    )

    assert result["status"] == "success"
    assert received == {"target": "full_screen", "timeout_seconds": 4.0, "quality_required": False}


def test_copy_latest_reply_routes_payload(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        command_runner,
        "copy_latest_reply",
        lambda timeout_seconds: {"status": "copied", "raw_text": "trace", "timeout_seconds": timeout_seconds},
    )
    monkeypatch.setattr(
        command_runner,
        "probe_latest_trae_turn",
        lambda **kwargs: {"status": "found", "turn_status": "completed", "session_id": "sid", "user_message_id": "mid"},
    )

    result = CommandRunner(worker_id="worker-test").run(
        {"command_id": "cmd-6", "type": "copy_latest_reply", "payload": {"timeout_seconds": 7}}
    )

    assert result["status"] == "success"
    assert result["data"]["raw_text"] == "trace"
    assert result["data"]["timeout_seconds"] == 7.0
    assert result["data"]["current_turn_gate"]["passed"] is True


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
    monkeypatch.setattr(
        command_runner,
        "focus_trae",
        lambda timeout_seconds=1.0: {"status": "focused", "window_title": "Trae CN"},
    )

    def fake_browser_acceptance(project_path: str, url: str, timeout_seconds: float, cancellation_check=None):
        received["project_path"] = project_path
        received["url"] = url
        received["timeout_seconds"] = timeout_seconds
        received["cancellable"] = callable(cancellation_check)
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
    assert result["data"]["trae_foreground"]["status"] == "focused"
    assert received == {
        "project_path": str(workspace),
        "url": "localhost:5173",
        "timeout_seconds": 3.0,
        "cancellable": True,
    }


def test_browser_acceptance_uses_configured_url_when_payload_omits_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    received = {}
    settings = command_runner.WorkerSettings(
        workspace_root=tmp_path,
        browser_url="http://localhost:5173",
    )
    monkeypatch.setattr(
        command_runner,
        "focus_trae",
        lambda timeout_seconds=1.0: {"status": "focused", "window_title": "Trae CN"},
    )

    def fake_browser_acceptance(project_path: str, url: str, timeout_seconds: float, cancellation_check=None):
        received["project_path"] = project_path
        received["url"] = url
        received["timeout_seconds"] = timeout_seconds
        received["cancellable"] = callable(cancellation_check)
        return {"status": "passed", "url": url}

    monkeypatch.setattr(command_runner, "run_browser_acceptance", fake_browser_acceptance)

    result = CommandRunner(worker_id="worker-test", runtime_settings=settings).run(
        {
            "command_id": "cmd-8b",
            "type": "browser_acceptance",
            "payload": {"workspace_path": "project", "timeout_seconds": 3},
        }
    )

    assert result["status"] == "success"
    assert result["data"]["trae_foreground"]["status"] == "focused"
    assert received == {
        "project_path": str(workspace),
        "url": "http://localhost:5173",
        "timeout_seconds": 3.0,
        "cancellable": True,
    }


def test_git_submit_routes_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    received = {}
    monkeypatch.setattr(command_runner.settings, "workspace_root", tmp_path)

    def fake_git_submit(
        root,
        project_path,
        commit_message,
        push,
        remote,
        branch,
        remote_url,
        project_name,
        timeout,
        cancellation_check=None,
    ):
        received["root"] = root
        received["project_path"] = project_path
        received["commit_message"] = commit_message
        received["push"] = push
        received["remote"] = remote
        received["branch"] = branch
        received["remote_url"] = remote_url
        received["project_name"] = project_name
        received["timeout"] = timeout
        received["cancellable"] = callable(cancellation_check)
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
                "remote_url": "git@github.com:acme/project.git",
                "project_name": "project",
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
        "remote_url": "git@github.com:acme/project.git",
        "project_name": "project",
        "timeout": 5,
        "cancellable": True,
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


def test_run_git_submit_pushes_existing_clean_commit(tmp_path: Path):
    remote = tmp_path / "remote.git"
    project = tmp_path / "project"
    remote.mkdir()
    project.mkdir()
    _git(remote, "init", "--bare")
    _git(project, "init")
    _git(project, "config", "user.name", "Test User")
    _git(project, "config", "user.email", "test@example.invalid")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git(project, "add", "README.md")
    _git(project, "commit", "-m", "Initial commit")
    _git(project, "remote", "add", "origin", str(remote))

    result = run_git_submit(tmp_path, project, commit_message="AgentOps test", push=True)

    assert result["status"] == "pushed"
    assert result["changed_files"] == 0
    upstream = _git(project, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    assert upstream.returncode == 0


def test_run_git_submit_initializes_project_and_sets_remote(tmp_path: Path):
    remote = tmp_path / "remote.git"
    project = tmp_path / "project"
    remote.mkdir()
    project.mkdir()
    _git(remote, "init", "--bare")
    (project / "README.md").write_text("hello\n", encoding="utf-8")

    result = run_git_submit(
        tmp_path,
        project,
        commit_message="AgentOps test",
        push=True,
        remote_url=str(remote),
        project_name="project",
    )

    assert result["status"] == "pushed"
    assert result["repo_prepare"]["initialized"] is True
    assert result["project_name"] == "project"
    assert _git(project, "remote", "get-url", "origin").stdout.strip() == str(remote)
    gitignore = (project / ".gitignore").read_text(encoding="utf-8")
    assert "screenshots/" in gitignore
    assert "trae_reply_traces/" in gitignore


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


def test_probe_trace_reports_service_interruption():
    trace = "toolName: edit\nstatus: success\n" + ("trace detail line\n" * 80) + "\u670d\u52a1\u7aef\u5f02\u5e38\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5"

    result = probe_trace(trace)

    assert result["complete_like"] is False
    assert result["reason"] == "service_interrupted"


def test_detect_terminal_prompt_handles_npm_create_confirm():
    result = detect_terminal_prompt("Need to install the following packages: create-vite@latest\nOk to proceed? (y)")

    assert result["input"] == "y"
    assert result["reason"].startswith("terminal_prompt:")


def test_detect_terminal_prompt_handles_vite_select_defaults():
    result = detect_terminal_prompt("Select a framework: Vue React Svelte")

    assert result["input"] == "\n"
    assert result["confidence"] > 0.8


def test_current_turn_gate_blocks_old_or_missing_turn():
    gate = _current_turn_gate({"status": "missing", "reason": "workspace_mismatch"})

    assert gate["passed"] is False
    assert gate["recoverable"] is False
    assert gate["reason"] == "workspace_mismatch"


def test_current_turn_gate_recovers_unfinished_current_turn():
    gate = _current_turn_gate({"status": "found", "turn_status": "running", "session_id": "sid"})

    assert gate["passed"] is False
    assert gate["recoverable"] is True
    assert gate["reason"] == "trae_turn_not_completed:running"


def test_scroll_assistant_to_bottom_uses_scrollable_controls_without_hwnd():
    class Rect:
        left = 10
        top = 10
        right = 510
        bottom = 610

    class FakeControl:
        def __init__(self):
            self.focused = False
            self.wheels = []

        def set_focus(self):
            self.focused = True

        def wheel_mouse_input(self, wheel_dist):
            self.wheels.append(wheel_dist)

        def rectangle(self):
            return Rect()

        def window_text(self):
            return "assistant"

    class FakeWindow:
        hwnd = 0

        def __init__(self, control):
            self.control = control

        def descendants(self, control_type):
            return [self.control] if control_type == "Pane" else []

    control = FakeControl()

    result = scroll_assistant_to_bottom(FakeWindow(control), wheel_steps=2)

    assert result["status"] == "scrolled"
    assert control.focused is True
    assert control.wheels == [-5, -5]


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
