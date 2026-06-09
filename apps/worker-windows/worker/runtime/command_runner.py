from pathlib import Path
from typing import Any

from worker.config import settings
from worker.project.browser_acceptance import run_browser_acceptance
from worker.project.commands import run_project_command
from worker.project.git_submit import run_git_submit
from worker.project.scanner import scan_project
from worker.runtime.state import WorkerRuntimeState
from worker.safety.path_guard import assert_within_root
from worker.trae.diagnose import diagnose_ui
from worker.trae.intervene import click_confirm, click_continue
from worker.trae.prompt import PromptSendError, send_prompt
from worker.trae.screenshot import capture_screenshot
from worker.trae.trace_copy import copy_latest_reply
from worker.trae.wait import wait_completion
from worker.trae.window import TraeAutomationError, focus_trae, open_trae


class CommandRunner:
    def __init__(self, worker_id: str | None = None) -> None:
        self.worker_id = worker_id or settings.worker_id
        self.state = WorkerRuntimeState()

    def run(self, command: dict) -> dict:
        command_id = command.get("command_id", "")
        command_type = command.get("type", "")
        payload = command.get("payload") or {}
        try:
            self.state.busy = True
            self.state.stage = command_type or "unknown_command"
            if command_type == "capture_screenshot":
                data = capture_screenshot()
            elif command_type == "open_trae":
                data = self._open_trae(payload)
            elif command_type == "focus_trae":
                data = focus_trae()
            elif command_type == "send_prompt":
                data = self._send_prompt(payload)
                self.state.stage = "prompt_sent"
            elif command_type == "wait_completion":
                data = self._wait_completion(payload)
                self.state.stage = "trae_completed"
            elif command_type == "diagnose_ui":
                data = diagnose_ui()
            elif command_type == "click_continue":
                data = self._click_continue(payload)
            elif command_type == "click_confirm":
                data = click_confirm()
            elif command_type == "copy_latest_reply":
                data = self._copy_latest_reply(payload)
            elif command_type == "scan_project":
                data = self._scan_project(payload)
            elif command_type == "run_command":
                data = self._run_command(payload)
            elif command_type == "browser_acceptance":
                data = self._browser_acceptance(payload)
            elif command_type == "git_submit":
                data = self._git_submit(payload)
            elif command_type == "stop_current_task":
                self.state.stop_requested = True
                data = {"stopped": True}
            else:
                return self._failed(command_id, f"Unsupported command type: {command_type}")
            return self._success(command_id, data)
        except (TraeAutomationError, PromptSendError) as exc:
            return self._manual_required(command_id, str(exc))
        except Exception as exc:
            return self._failed(command_id, str(exc))
        finally:
            self.state.busy = False
            if self.state.stage != "prompt_sent":
                self.state.stage = "idle"

    def _open_trae(self, payload: dict[str, Any]) -> dict:
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        return open_trae(settings.trae_exe_path, workspace_path)

    def _send_prompt(self, payload: dict[str, Any]) -> dict:
        prompt = str(payload.get("prompt") or "")
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        open_result = None
        if workspace_path:
            open_result = open_trae(settings.trae_exe_path, workspace_path)
        send_result = send_prompt(
            prompt,
            submit=bool(payload.get("submit", True)),
            submit_hotkey=str(payload.get("submit_hotkey") or "{ENTER}"),
        )
        if open_result:
            send_result["open_trae"] = open_result
        return send_result

    def _wait_completion(self, payload: dict[str, Any]) -> dict:
        return wait_completion(
            timeout_seconds=float(payload.get("timeout_seconds", 900)),
            stable_seconds=float(payload.get("stable_seconds", 15)),
            poll_interval_seconds=float(payload.get("poll_interval_seconds", 2)),
        )

    def _copy_latest_reply(self, payload: dict[str, Any]) -> dict:
        return copy_latest_reply(timeout_seconds=float(payload.get("timeout_seconds", 10)))

    def _click_continue(self, payload: dict[str, Any]) -> dict:
        return click_continue(timeout_seconds=float(payload.get("timeout_seconds", 10)))

    def _scan_project(self, payload: dict[str, Any]) -> dict:
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        return scan_project(workspace_path or settings.workspace_root)

    def _run_command(self, payload: dict[str, Any]) -> dict:
        command = payload.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError("run_command payload.command must be a list of strings")
        cwd = self._workspace_path(payload.get("cwd") or payload.get("workspace_path")) or settings.workspace_root
        timeout = int(payload.get("timeout", 120))
        return run_project_command(settings.workspace_root, cwd, command, timeout=timeout)

    def _browser_acceptance(self, payload: dict[str, Any]) -> dict:
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        return run_browser_acceptance(
            str(workspace_path or settings.workspace_root),
            url=str(payload.get("url") or payload.get("browser_url") or payload.get("acceptance_url") or ""),
            timeout_seconds=float(payload.get("timeout_seconds", 10)),
        )

    def _git_submit(self, payload: dict[str, Any]) -> dict:
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        project_path = workspace_path or settings.workspace_root
        return run_git_submit(
            settings.workspace_root,
            project_path,
            commit_message=str(payload.get("commit_message") or ""),
            push=bool(payload.get("push", True)),
            remote=str(payload.get("remote") or "origin"),
            branch=str(payload.get("branch") or ""),
            timeout=int(payload.get("timeout", 120)),
        )

    def _workspace_path(self, raw_path: Any) -> Path | None:
        if not raw_path:
            return None
        workspace_path = Path(str(raw_path)).expanduser()
        if not workspace_path.is_absolute():
            workspace_path = settings.workspace_root / workspace_path
        assert_within_root(workspace_path, settings.workspace_root)
        return workspace_path

    def _success(self, command_id: str, data: dict) -> dict:
        return {
            "command_id": command_id,
            "worker_id": self.worker_id,
            "status": "success",
            "message": "Command processed",
            "data": data,
        }

    def _manual_required(self, command_id: str, error: str) -> dict:
        return {
            "command_id": command_id,
            "worker_id": self.worker_id,
            "status": "manual_required",
            "message": "Command requires manual worker intervention",
            "data": {},
            "error": error,
        }

    def _failed(self, command_id: str, error: str) -> dict:
        return {
            "command_id": command_id,
            "worker_id": self.worker_id,
            "status": "failed",
            "message": "Command failed",
            "data": {},
            "error": error,
        }
