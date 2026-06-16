from pathlib import Path
import subprocess

import pytest

from worker.runtime import command_runner
from worker.runtime.command_runner import _copy_supervisor_decision
from worker.runtime.command_runner import _current_turn_gate
from worker.runtime.command_runner import CommandRunner
from worker.project.git_submit import run_git_submit
from worker.trae.diagnose import detect_terminal_prompt
from worker.trae import trace_copy
from worker.trae.trace_copy import probe_trace, scroll_assistant_to_bottom
from worker.trae import window as trae_window
from worker.trae.window import TraeAutomationError
from worker.runtime import stop_cleanup


def test_runner_uses_configured_worker_id():
    result = CommandRunner(worker_id="worker-test").run({"command_id": "cmd-1", "type": "stop_current_task", "payload": {}})

    assert result["worker_id"] == "worker-test"
    assert result["status"] == "success"
    assert result["data"]["stopped"] is True


def test_stop_current_task_cleans_workspace_processes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    cleaned = {}

    def fake_cleanup(**kwargs):
        cleaned.update(kwargs)
        return {"status": "completed", "killed": [{"pid": 1234}], "errors": []}

    monkeypatch.setattr(command_runner.settings, "workspace_root", tmp_path)
    monkeypatch.setattr(command_runner, "cleanup_local_activity", fake_cleanup)
    monkeypatch.setattr(command_runner, "_try_click_trae_stop", lambda **_kwargs: {"status": "clicked"})
    monkeypatch.setattr(command_runner, "_stop_verification_snapshot", lambda _workspace: {"project": {"path": "a", "mtime": 1}})
    monkeypatch.setattr(command_runner.time, "sleep", lambda _seconds: None)

    runner = CommandRunner(worker_id="worker-test")
    result = runner.run(
        {
            "command_id": "cmd-stop",
            "type": "stop_current_task",
            "payload": {
                "workspace_path": "project",
                "project_name": "demo-project",
            },
        }
    )

    assert result["status"] == "success"
    assert result["data"]["stopped"] is True
    assert result["data"]["cleanup"]["killed"][0]["pid"] == 1234
    assert result["data"]["stop_report"]["trae_stop_clicked"] is True
    assert result["data"]["stop_report"]["requires_resume_prompt"] is True
    assert result["data"]["stop_report"]["sandbox_killed"] == 1
    assert result["data"]["stop_report"]["trae_ui_stopped_verified"] is True
    assert cleaned == {"workspace_path": workspace, "project_name": "demo-project", "kill_trae": False}


def test_stop_cleanup_matches_workspace_and_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace = tmp_path / "roles-dashboard"
    workspace.mkdir()
    killed = []
    monkeypatch.setattr(
        stop_cleanup,
        "_query_processes",
        lambda: [
            {
                "pid": 101,
                "name": "node.exe",
                "command_line": f"node server.js --cwd {workspace}",
            },
            {
                "pid": 102,
                "name": "node.exe",
                "command_line": "node unrelated.js",
            },
            {
                "pid": 103,
                "name": "trae-sandbox.exe",
                "command_line": "trae-sandbox.exe exec --command-line <by-env>",
            },
        ],
    )
    monkeypatch.setattr(stop_cleanup, "_kill_process_tree", lambda pid: killed.append(pid) or {"ok": True})

    result = stop_cleanup.cleanup_local_activity(workspace_path=workspace, project_name="roles-dashboard")

    assert result["status"] == "completed"
    assert result["matched_count"] == 2
    assert result["killed_count"] == 2
    assert killed == [101, 103]
    assert [item["pid"] for item in result["killed"]] == [101, 103]


def test_stop_cleanup_reports_no_matching_processes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace = tmp_path / "roles-dashboard"
    workspace.mkdir()
    monkeypatch.setattr(stop_cleanup, "_query_processes", lambda: [])

    result = stop_cleanup.cleanup_local_activity(workspace_path=workspace, project_name="roles-dashboard")

    assert result["status"] == "no_matching_processes"
    assert result["matched_count"] == 0
    assert result["killed_count"] == 0
    assert result["errors"] == []


def test_stop_current_task_reports_structured_confirmation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace = tmp_path / "project"
    workspace.mkdir()

    monkeypatch.setattr(command_runner.settings, "workspace_root", tmp_path)
    monkeypatch.setattr(
        command_runner,
        "cleanup_local_activity",
        lambda **_kwargs: {
            "status": "no_matching_processes",
            "matched_count": 0,
            "killed_count": 0,
            "error_count": 0,
            "killed": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(command_runner, "_try_click_trae_stop", lambda **_kwargs: {"status": "not_clicked", "reason": "no_stop_button"})
    monkeypatch.setattr(command_runner, "_stop_verification_snapshot", lambda _workspace: {"project": {"path": "", "mtime": 0}})
    monkeypatch.setattr(command_runner.time, "sleep", lambda _seconds: None)

    result = CommandRunner(worker_id="worker-test").run(
        {"command_id": "cmd-stop", "type": "stop_current_task", "payload": {"workspace_path": "project"}}
    )

    assert result["status"] == "success"
    report = result["data"]["stop_report"]
    assert report["stop_confirmed"] is True
    assert report["cleanup_status"] == "no_matching_processes"
    assert report["local_processes_killed"] == 0
    assert result["data"]["message"] == "Worker stop completed."


def test_stop_verification_ignores_noisy_trae_log_when_project_is_quiet():
    before = {
        "trae_log": {"path": "C:/Trae/logs/main.log", "mtime": 10, "size": 100},
        "project": {"path": "D:/work/project/app.py", "mtime": 20, "size": 200},
    }
    after = {
        "trae_log": {"path": "C:/Trae/logs/main.log", "mtime": 11, "size": 120},
        "project": {"path": "D:/work/project/app.py", "mtime": 20, "size": 200},
    }

    result = command_runner._stop_verification_result(before, after)

    assert result["log_tail_changed"] is True
    assert result["project_write_changed"] is False
    assert result["log_change_ignored_for_generation"] is True
    assert result["still_generating_suspected"] is False
    assert result["trae_ui_stopped_verified"] is True


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
        lambda prompt, submit, submit_hotkey, **kwargs: {
            "status": "sent",
            "chars": len(prompt),
            "submitted": submit,
            "submit_hotkey": submit_hotkey,
            "verify_submission": kwargs.get("verify_submission"),
            "strict_submission_verification": kwargs.get("strict_submission_verification"),
            "workspace_path": str(kwargs.get("workspace_path") or ""),
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
    assert result["data"]["verify_submission"] is True
    assert result["data"]["strict_submission_verification"] is True
    assert result["data"]["workspace_path"] == str(workspace)
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
        lambda prompt, submit, submit_hotkey, **kwargs: {
            "status": "sent",
            "chars": len(prompt),
            "submitted": submit,
            "submit_hotkey": submit_hotkey,
            "verify_submission": kwargs.get("verify_submission"),
            "strict_submission_verification": kwargs.get("strict_submission_verification"),
            "workspace_path": str(kwargs.get("workspace_path") or ""),
        },
    )

    result = CommandRunner(worker_id="worker-test", runtime_settings=settings).run(
        {"command_id": "cmd-2b", "type": "send_prompt", "payload": {"prompt": "Build the feature"}}
    )

    assert result["status"] == "success"
    assert result["data"]["open_trae"]["status"] == "already_running"
    assert result["data"]["verify_submission"] is True
    assert result["data"]["strict_submission_verification"] is True
    assert result["data"]["workspace_path"] == str(tmp_path)
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


def test_send_prompt_manual_required_preserves_diagnostics(monkeypatch: pytest.MonkeyPatch):
    def raise_prompt_error(*args, **kwargs):
        raise command_runner.PromptSendError(
            "auto calibration failed",
            {"stage": "trae_ui_auto_calibration_failed", "screenshot": {"path": "screen.png"}},
        )

    monkeypatch.setattr(
        command_runner,
        "ensure_trae_running",
        lambda exe, workspace_path, launch_timeout_seconds, force_open_workspace=False: {"status": "already_running"},
    )
    monkeypatch.setattr(command_runner, "send_prompt", raise_prompt_error)

    result = CommandRunner(worker_id="worker-test").run(
        {"command_id": "cmd-diag", "type": "send_prompt", "payload": {"prompt": "hello"}}
    )

    assert result["status"] == "manual_required"
    assert result["data"]["stage"] == "trae_ui_auto_calibration_failed"
    assert result["data"]["screenshot"]["path"] == "screen.png"


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
    monkeypatch.setattr(trae_window, "_foreground_window", lambda: 202)
    monkeypatch.setattr(trae_window, "_window_process_id", lambda hwnd: {101: 1001, 202: 2002}.get(hwnd, 0))
    monkeypatch.setattr(
        trae_window,
        "_window_rect",
        lambda hwnd: {101: (0, 0, 900, 700), 202: (10, 20, 910, 720)}.get(hwnd),
    )

    result = trae_window.trae_window_diagnostics(selected_hwnd=202)

    assert result["count"] == 2
    assert result["selected_hwnd"] == 202
    assert result["foreground_hwnd"] == 202
    assert result["foreground_pid"] == 2002
    assert result["windows"] == [
        {
            "hwnd": 101,
            "title": "Trae CN - first",
            "selected": False,
            "pid": 1001,
            "foreground": False,
            "rect": {"left": 0, "top": 0, "right": 900, "bottom": 700, "width": 900, "height": 700},
        },
        {
            "hwnd": 202,
            "title": "Trae CN - second",
            "selected": True,
            "pid": 2002,
            "foreground": True,
            "rect": {"left": 10, "top": 20, "right": 910, "bottom": 720, "width": 900, "height": 700},
        },
    ]


def test_focus_window_maximizes_and_verifies_foreground(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, int, int | None]] = []
    window = trae_window.TraeWindow(777)
    monkeypatch.setattr(trae_window, "_window_title", lambda hwnd: "target-project - Trae CN")
    monkeypatch.setattr(trae_window, "_set_process_dpi_aware", lambda: calls.append(("dpi", 0, None)))
    monkeypatch.setattr(trae_window, "_show_window", lambda hwnd, command: calls.append(("show", hwnd, command)))
    monkeypatch.setattr(trae_window, "_app_activate_pid", lambda pid: calls.append(("appactivate", pid, None)) or True)
    monkeypatch.setattr(trae_window, "_tap_alt_for_foreground_unlock", lambda: calls.append(("alt", 0, None)))
    monkeypatch.setattr(trae_window, "_set_foreground_window", lambda hwnd, show_window=None: calls.append(("foreground", hwnd, show_window)))
    monkeypatch.setattr(trae_window, "_window_process_id", lambda hwnd: 4242)
    monkeypatch.setattr(trae_window, "_foreground_window", lambda: 777)
    monkeypatch.setattr(trae_window, "_foreground_process_id", lambda: 4242)
    monkeypatch.setattr(trae_window.time, "sleep", lambda seconds: None)

    title = trae_window._focus_window(window)

    assert title == "target-project - Trae CN"
    assert ("show", 777, trae_window.SW_MAXIMIZE) in calls
    assert ("appactivate", 4242, None) in calls
    assert ("foreground", 777, None) in calls


def test_wait_for_workspace_window_or_any_falls_back_when_title_missing(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def fake_wait(timeout_seconds, workspace_path=None, require_workspace_match=False, **_kwargs):
        calls.append(require_workspace_match)
        if require_workspace_match:
            raise TraeAutomationError("workspace window missing")
        return trae_window.TraeWindow(303)

    monkeypatch.setattr(trae_window, "wait_for_stable_trae_window", fake_wait)

    window = trae_window.wait_for_workspace_window_or_any(
        timeout_seconds=10,
        workspace_path="D:/work/target-project",
        prefer_workspace_match=True,
    )

    assert window.hwnd == 303
    assert calls == [True, False]


def test_find_trae_window_requires_workspace_match(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        trae_window,
        "_find_top_level_windows",
        lambda marker: [(101, "permission-system-old - Trae CN")],
    )
    monkeypatch.setattr(trae_window.time, "sleep", lambda seconds: None)

    with pytest.raises(TraeAutomationError) as exc:
        trae_window.find_trae_window(
            timeout_seconds=0.01,
            workspace_path="D:/code-space/coding-soler/permission-system-new",
            require_workspace_match=True,
        )

    assert "permission-system-new" in str(exc.value)


def test_ensure_trae_running_reuses_existing_window_for_target_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    calls = []

    monkeypatch.setattr(trae_window, "resolve_trae_executable", lambda path: Path("C:/Trae/Trae.exe"))
    monkeypatch.setattr(
        trae_window,
        "_find_top_level_windows",
        lambda marker: [(101, "old-project - Trae CN")]
        if len(calls) == 0
        else [(101, "target-project - Trae CN")],
    )
    monkeypatch.setattr(trae_window, "_focus_window", lambda window: "target-project - Trae CN")
    monkeypatch.setattr(trae_window.subprocess, "Popen", lambda args: calls.append(args))
    monkeypatch.setattr(trae_window.time, "sleep", lambda seconds: None)

    result = trae_window.ensure_trae_running(
        Path("C:/Trae/Trae.exe"),
        tmp_path / "target-project",
        launch_timeout_seconds=0.1,
    )

    assert calls == [[str(Path("C:/Trae/Trae.exe")), "--reuse-window", str(tmp_path / "target-project")]]
    assert result["reuse_window"] is True
    assert result["workspace_match"] is True


def test_ensure_trae_running_falls_back_for_existing_window_title_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    workspace = tmp_path / "target-project"
    fallback_window = trae_window.TraeWindow(202)
    wait_calls = []

    monkeypatch.setattr(trae_window, "_try_find_trae_window", lambda **_kwargs: fallback_window)
    monkeypatch.setattr(
        trae_window,
        "wait_for_workspace_window_or_any",
        lambda timeout_seconds, workspace_path=None, prefer_workspace_match=True: wait_calls.append(
            (timeout_seconds, workspace_path, prefer_workspace_match)
        )
        or fallback_window,
    )
    monkeypatch.setattr(trae_window, "_focus_window", lambda window: "Trae CN")
    monkeypatch.setattr(
        trae_window,
        "trae_window_diagnostics",
        lambda selected_hwnd=None, workspace_path=None: {"selected_hwnd": selected_hwnd, "matching_count": 0},
    )

    result = trae_window.ensure_trae_running(
        Path("C:/Trae/Trae.exe"),
        workspace,
        launch_timeout_seconds=10,
    )

    assert result["status"] == "already_running"
    assert result["window_title"] == "Trae CN"
    assert result["workspace_match"] is False
    assert wait_calls == [(6.0, workspace, True)]


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
        progress_callback=None,
        progress_interval_seconds: float = 10.0,
        prompt: str = "",
        workspace_path: str = "",
        sent_at_epoch: float | None = None,
        sent_at: str = "",
        ui_analyst=None,
    ):
        received["timeout_seconds"] = timeout_seconds
        received["stable_seconds"] = stable_seconds
        received["poll_interval_seconds"] = poll_interval_seconds
        received["intervention_idle_seconds"] = intervention_idle_seconds
        received["max_interventions"] = max_interventions
        received["cancellable"] = callable(cancellation_check)
        received["progress_callback"] = callable(progress_callback)
        received["progress_interval_seconds"] = progress_interval_seconds
        received["prompt"] = prompt
        received["workspace_path"] = workspace_path
        received["sent_at_epoch"] = sent_at_epoch
        received["sent_at"] = sent_at
        received["ui_analyst"] = callable(ui_analyst)
        return {"status": "completed"}

    monkeypatch.setattr(command_runner, "wait_completion", fake_wait_completion)
    monkeypatch.setattr(command_runner.settings, "workspace_root", Path("D:/work"))

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
                "prompt": "build feature",
                "workspace_path": "current",
                "sent_at_epoch": 123.5,
                "sent_at": "2026-06-13T00:00:00Z",
                "progress_interval_seconds": 7,
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
        "progress_callback": True,
        "progress_interval_seconds": 7.0,
        "prompt": "build feature",
        "workspace_path": str(Path("D:/work/current")),
        "sent_at_epoch": 123.5,
        "sent_at": "2026-06-13T00:00:00Z",
        "ui_analyst": True,
    }


def test_wait_completion_progress_posts_worker_log(monkeypatch: pytest.MonkeyPatch):
    class FakeClient:
        def __init__(self):
            self.logs = []

        def post_log(self, worker_id, payload):
            self.logs.append((worker_id, payload))

    client = FakeClient()

    def fake_wait_completion(
        timeout_seconds: float,
        stable_seconds: float,
        poll_interval_seconds: float,
        intervention_idle_seconds: float,
        max_interventions: int,
        cancellation_check=None,
        progress_callback=None,
        progress_interval_seconds: float = 10.0,
        **_kwargs,
    ):
        progress_callback(
            {
                "event": "trae_activity",
                "display_message": "Trae CN 正常工作中，检测到 agent_log 更新，继续等待。",
                "activity_source": "agent_log",
            }
        )
        return {"status": "completed"}

    monkeypatch.setattr(command_runner, "wait_completion", fake_wait_completion)
    monkeypatch.setattr(command_runner.settings, "workspace_root", Path("D:/work"))

    result = CommandRunner(worker_id="worker-test", worker_client=client).run(
        {
            "command_id": "cmd-progress",
            "type": "wait_completion",
            "payload": {
                "job_id": "job1",
                "round_id": "round1",
                "workspace_path": "current",
            },
        }
    )

    assert result["status"] == "success"
    assert client.logs == [
        (
            "worker-test",
            {
                "command_id": "cmd-progress",
                "job_id": "job1",
                "round_id": "round1",
                "stage": "waiting_trae",
                "level": "info",
                "message": "trae_activity",
                "display_message": "Trae CN 正常工作中，检测到 agent_log 更新，继续等待。",
                "extra": {"event": "trae_activity", "activity_source": "agent_log"},
            },
        )
    ]


def test_wait_completion_cancelled_by_server(monkeypatch: pytest.MonkeyPatch):
    def fake_wait_completion(
        timeout_seconds: float,
        stable_seconds: float,
        poll_interval_seconds: float,
        intervention_idle_seconds: float,
        max_interventions: int,
        cancellation_check=None,
        progress_callback=None,
        progress_interval_seconds: float = 10.0,
        **_kwargs,
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
    trace = (
        "toolName: edit\nstatus: success\nfilePath: app.py\ncommand: pytest\nTodos updated: done\n"
        + ("trace detail line\n" * 80)
    )

    monkeypatch.setattr(
        command_runner,
        "copy_latest_reply",
        lambda timeout_seconds, cancellation_check=None, **kwargs: {
            "status": "copied",
            "raw_text": trace,
            "trace_probe": probe_trace(trace),
            "timeout_seconds": timeout_seconds,
            "cancellable": callable(cancellation_check),
            "kwargs": kwargs,
        },
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
    assert result["data"]["raw_text"] == trace
    assert result["data"]["timeout_seconds"] == 7.0
    assert result["data"]["cancellable"] is True
    assert result["data"]["kwargs"]["allow_local_fallback"] is True
    assert result["data"]["current_turn_gate"]["passed"] is True
    assert result["data"]["supervisor_decision"]["action"] == "collect_trace_candidate"


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


def test_copy_latest_reply_prefers_complete_raw_trace(monkeypatch: pytest.MonkeyPatch):
    class FakeButton:
        def __init__(self, text):
            self.text = text

        def click_input(self):
            return None

    class FakeWindow:
        pass

    values = iter(
        [
            "构建完成，测试通过。" * 80,
            "toolName: edit\nstatus: success\nfilePath: app.py\ncommand: pytest\nTodos updated: done\n"
            + ("trace detail line\n" * 80),
        ]
    )

    monkeypatch.setattr(trace_copy, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(trace_copy, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(trace_copy, "scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr(trace_copy, "_copy_buttons", lambda window: [("summary copy", FakeButton("summary")), ("trace copy", FakeButton("trace"))])
    monkeypatch.setattr(trace_copy, "_set_clipboard_text", lambda value: True)
    monkeypatch.setattr(trace_copy, "_wait_for_clipboard_change", lambda before, timeout_seconds: next(values))

    result = trace_copy.copy_latest_reply(timeout_seconds=1)

    assert result["button_text"] == "trace copy"
    assert result["trace_probe"]["reason"] == "ok"
    assert result["copy_candidates"][0]["reason"] == "missing_tool_trace_markers"
    assert result["copy_candidates"][1]["reason"] == "ok"


def test_copy_latest_reply_uses_local_trace_when_clipboard_is_incomplete(monkeypatch: pytest.MonkeyPatch):
    class FakeButton:
        def click_input(self):
            return None

    class FakeWindow:
        pass

    local_trace = (
        "Trae raw execution trace for session s1, user message u1.\n"
        "toolName: edit\nstatus: success\nfilePath: app.py\ncommand: pytest\nTodos updated: done\n"
        + ("trace detail line\n" * 80)
    )

    monkeypatch.setattr(trace_copy, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(trace_copy, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(trace_copy, "scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr(trace_copy, "_copy_buttons", lambda window: [("summary copy", FakeButton())])
    monkeypatch.setattr(trace_copy, "_set_clipboard_text", lambda value: True)
    monkeypatch.setattr(trace_copy, "_wait_for_clipboard_change", lambda before, timeout_seconds: "summary only")
    monkeypatch.setattr(
        trace_copy,
        "collect_local_trace",
        lambda trae_turn, prompt, workspace_path: {
            "status": "collected",
            "raw_text": local_trace,
            "chars": len(local_trace),
            "trace_source": "trae_local_raw_log_trace",
            "trace_probe": trace_copy.probe_trace(local_trace),
        },
    )

    result = trace_copy.copy_latest_reply(timeout_seconds=1, trae_turn={"session_id": "s1"}, prompt="demo", workspace_path="D:/work/demo")

    assert result["copy_method"] == "trae_local_raw_log_trace"
    assert result["trace_source"] == "trae_local_raw_log_trace"
    assert result["raw_text"] == local_trace
    assert result["copy_candidates"][0]["reason"] == "missing_tool_trace_markers"


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


def test_probe_trace_reports_model_request_3003_interruption():
    trace = "toolName: edit\nstatus: success\n" + ("trace detail line\n" * 80) + "模型请求失败，请稍后重试。(3003)"

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


def test_copy_supervisor_decision_recovers_gate_failure():
    decision = _copy_supervisor_decision(
        {"passed": False, "reason": "awaiting_current_continuation", "recoverable": True},
        {"reason": "ok"},
    )

    assert decision["action"] == "continue_output"
    assert decision["reason"] == "awaiting_current_continuation"
    assert decision["recoverable"] is True


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
