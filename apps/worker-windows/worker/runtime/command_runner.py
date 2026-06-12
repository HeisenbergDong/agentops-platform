from pathlib import Path
import time
from typing import Any, Callable

from worker.config import WorkerSettings, settings
from worker.project.browser_acceptance import run_browser_acceptance
from worker.project.commands import run_project_command
from worker.project.git_submit import run_git_submit
from worker.project.scanner import scan_project
from worker.project.workspace import ensure_project_workspace
from worker.runtime.cancellation import CancellationToken, CommandCancelled
from worker.runtime.state import WorkerRuntimeState
from worker.safety.path_guard import assert_within_root
from worker.trae.diagnose import diagnose_ui
from worker.trae.intervene import click_confirm, click_continue
from worker.trae.prompt import PromptSendError, send_prompt
from worker.trae.screenshot import capture_screenshot
from worker.trae.session_probe import probe_latest_trae_turn
from worker.trae.trace_copy import copy_latest_reply
from worker.trae.wait import wait_completion
from worker.trae.window import TraeAutomationError, ensure_trae_running, focus_trae, open_trae

RECOVERABLE_TRACE_PROBE_REASONS = {"awaiting_continuation", "service_interrupted"}
RECOVERABLE_TURN_GATE_REASONS = {
    "awaiting_continuation",
    "awaiting_current_continuation",
    "service_interrupted",
    "no_completed_turn_after_prompt_send",
    "trae_turn_not_completed",
}


class CommandRunner:
    def __init__(
        self,
        worker_id: str | None = None,
        runtime_settings: WorkerSettings | None = None,
        cancellation_checker: Callable[[str], bool] | None = None,
        worker_client: Any | None = None,
    ) -> None:
        self.settings = runtime_settings or settings
        self.worker_id = worker_id or self.settings.worker_id
        self.cancellation_checker = cancellation_checker
        self.worker_client = worker_client
        self.state = WorkerRuntimeState()

    def run(self, command: dict) -> dict:
        command_id = command.get("command_id", "")
        command_type = command.get("type", "")
        payload = command.get("payload") or {}
        lease_id = str(command.get("lease_id") or "")
        cancellation = CancellationToken(self.state, command_id, self.cancellation_checker)
        try:
            self.state.busy = True
            self.state.current_command_id = command_id
            self.state.current_lease_id = lease_id
            self.state.stage = command_type or "unknown_command"
            if command_type != "stop_current_task":
                self.state.stop_requested = False
                cancellation.raise_if_cancelled()
            if command_type == "capture_screenshot":
                data = self._capture_screenshot(payload)
            elif command_type == "open_trae":
                data = self._open_trae(payload)
            elif command_type == "focus_trae":
                data = self._focus_trae(payload)
            elif command_type == "send_prompt":
                data = self._send_prompt(payload)
                self.state.stage = "prompt_sent"
            elif command_type == "wait_completion":
                data = self._wait_completion(payload, cancellation)
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
                data = self._run_command(payload, cancellation)
            elif command_type == "browser_acceptance":
                data = self._browser_acceptance(payload, cancellation)
            elif command_type == "git_submit":
                data = self._git_submit(payload, cancellation)
            elif command_type == "stop_current_task":
                self.state.stop_requested = True
                data = {"stopped": True}
            else:
                return self._failed(command_id, f"Unsupported command type: {command_type}", lease_id=lease_id)
            return self._success(command_id, data, lease_id=lease_id)
        except CommandCancelled as exc:
            return self._cancelled(command_id, str(exc), lease_id=lease_id)
        except (TraeAutomationError, PromptSendError) as exc:
            return self._manual_required(command_id, exc, lease_id=lease_id)
        except Exception as exc:
            return self._failed(command_id, str(exc), lease_id=lease_id)
        finally:
            self.state.busy = False
            self.state.current_command_id = ""
            self.state.current_lease_id = ""
            if self.state.stage != "prompt_sent":
                self.state.stage = "idle"

    def _open_trae(self, payload: dict[str, Any]) -> dict:
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        return open_trae(self.settings.trae_exe_path, workspace_path)

    def ensure_trae_ready(
        self,
        workspace_path: Path | None = None,
        launch_timeout_seconds: float = 30.0,
        force_open_workspace: bool = False,
    ) -> dict:
        result = ensure_trae_running(
            self.settings.trae_exe_path,
            self._launch_workspace_path(workspace_path),
            launch_timeout_seconds=launch_timeout_seconds,
            force_open_workspace=force_open_workspace,
        )
        self.state.current_window_title = str(result.get("window_title") or "")
        return result

    def _focus_trae(self, payload: dict[str, Any]) -> dict:
        launch_if_missing = bool(payload.get("launch_if_missing", True))
        if not launch_if_missing:
            return focus_trae()
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        return self.ensure_trae_ready(
            workspace_path,
            launch_timeout_seconds=float(payload.get("launch_timeout_seconds", 30)),
            force_open_workspace=bool(payload.get("force_open_workspace", False)),
        )

    def _capture_screenshot(self, payload: dict[str, Any]) -> dict:
        return capture_screenshot(
            target=str(payload.get("target") or "trae_window"),
            timeout_seconds=float(payload.get("timeout_seconds", 10)),
            quality_required=bool(payload.get("quality_required", True)),
        )

    def _send_prompt(self, payload: dict[str, Any]) -> dict:
        prompt = str(payload.get("prompt") or "")
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        workspace_info = {}
        if workspace_path:
            workspace_info = ensure_project_workspace(self.settings.workspace_root, workspace_path, payload)
        open_result = self.ensure_trae_ready(
            workspace_path,
            launch_timeout_seconds=float(payload.get("launch_timeout_seconds", 30)),
            force_open_workspace=bool(payload.get("force_open_workspace", False)),
        )
        prompt_workspace_path = self._launch_workspace_path(workspace_path)
        sent_at_epoch = time.time()
        try:
            send_result = send_prompt(
                prompt,
                submit=bool(payload.get("submit", True)),
                submit_hotkey=str(payload.get("submit_hotkey") or "{ENTER}"),
                workspace_path=prompt_workspace_path,
                verify_submission=bool(payload.get("verify_submission", True)),
                sent_at_epoch=sent_at_epoch,
                submission_timeout_seconds=float(payload.get("submission_timeout_seconds", 15)),
                ui_analyst=self._analyze_trae_ui if bool(payload.get("use_ai_ui_analyst", True)) else None,
            )
        except PromptSendError as exc:
            details = dict(exc.details or {})
            details.setdefault("open_trae", open_result)
            details.setdefault("workspace", workspace_info)
            details.setdefault("sent_at_epoch", sent_at_epoch)
            try:
                details.setdefault(
                    "current_window",
                    focus_trae(
                        timeout_seconds=2.0,
                        workspace_path=prompt_workspace_path,
                        require_workspace_match=bool(prompt_workspace_path),
                    ),
                )
            except Exception as focus_exc:
                details.setdefault("current_window", {"status": "not_focused", "error": str(focus_exc)})
            raise PromptSendError(str(exc), details) from exc
        send_result["sent_at_epoch"] = sent_at_epoch
        send_result["open_trae"] = open_result
        send_result["workspace"] = workspace_info
        self.state.current_window_title = str(send_result.get("window_title") or self.state.current_window_title)
        return send_result

    def _wait_completion(self, payload: dict[str, Any], cancellation: CancellationToken) -> dict:
        return wait_completion(
            timeout_seconds=float(payload.get("timeout_seconds", 900)),
            stable_seconds=float(payload.get("stable_seconds", 15)),
            poll_interval_seconds=float(payload.get("poll_interval_seconds", 2)),
            intervention_idle_seconds=float(payload.get("intervention_idle_seconds", 60)),
            max_interventions=int(payload.get("max_interventions", 3)),
            cancellation_check=cancellation.raise_if_cancelled,
        )

    def _copy_latest_reply(self, payload: dict[str, Any]) -> dict:
        result = copy_latest_reply(timeout_seconds=float(payload.get("timeout_seconds", 10)))
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        result["trae_turn"] = probe_latest_trae_turn(
            prompt=str(payload.get("prompt") or ""),
            workspace_path=str(workspace_path or self.settings.workspace_root),
            sent_after_epoch=_float_or_none(payload.get("sent_at_epoch") or payload.get("prompt_sent_at_epoch")),
            sent_after=str(payload.get("sent_at") or payload.get("prompt_sent_at") or ""),
        )
        result["current_turn_gate"] = _current_turn_gate(result.get("trae_turn"), result.get("trace_probe"))
        return result

    def _click_continue(self, payload: dict[str, Any]) -> dict:
        return click_continue(
            timeout_seconds=float(payload.get("timeout_seconds", 10)),
            ui_analyst=self._analyze_trae_ui if bool(payload.get("use_ai_ui_analyst", True)) else None,
        )

    def _analyze_trae_ui(self, screenshot_path: str, context: dict[str, Any]) -> dict:
        if not self.worker_client:
            return {"status": "unavailable", "reason": "worker_client_not_configured"}
        return self.worker_client.analyze_trae_ui(
            self.worker_id,
            Path(screenshot_path),
            context=context,
            content_type="image/png",
        )

    def _scan_project(self, payload: dict[str, Any]) -> dict:
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        return scan_project(
            workspace_path or self.settings.workspace_root,
            prompt=str(payload.get("prompt") or ""),
            changed_file_list=payload.get("changed_files"),
        )

    def _run_command(self, payload: dict[str, Any], cancellation: CancellationToken) -> dict:
        command = payload.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError("run_command payload.command must be a list of strings")
        cwd = self._workspace_path(payload.get("cwd") or payload.get("workspace_path")) or self.settings.workspace_root
        timeout = int(payload.get("timeout", 120))
        return run_project_command(
            self.settings.workspace_root,
            cwd,
            command,
            timeout=timeout,
            cancellation_check=cancellation.raise_if_cancelled,
        )

    def _browser_acceptance(self, payload: dict[str, Any], cancellation: CancellationToken) -> dict:
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        result = run_browser_acceptance(
            str(workspace_path or self.settings.workspace_root),
            url=str(
                payload.get("url")
                or payload.get("browser_url")
                or payload.get("acceptance_url")
                or self.settings.browser_url
                or ""
            ),
            timeout_seconds=float(payload.get("timeout_seconds", 10)),
            cancellation_check=cancellation.raise_if_cancelled,
        )
        return self._restore_trae_foreground(result)

    def _git_submit(self, payload: dict[str, Any], cancellation: CancellationToken) -> dict:
        workspace_path = self._workspace_path(payload.get("trae_workspace_path") or payload.get("workspace_path"))
        project_path = workspace_path or self.settings.workspace_root
        return run_git_submit(
            self.settings.workspace_root,
            project_path,
            commit_message=str(payload.get("commit_message") or ""),
            push=bool(payload.get("push", True)),
            remote=str(payload.get("remote") or "origin"),
            branch=str(payload.get("branch") or ""),
            remote_url=str(payload.get("remote_url") or payload.get("github_remote_url") or ""),
            project_name=str(payload.get("project_name") or payload.get("github_repo_name") or ""),
            timeout=int(payload.get("timeout", 120)),
            cancellation_check=cancellation.raise_if_cancelled,
        )

    def _workspace_path(self, raw_path: Any) -> Path | None:
        if not raw_path:
            return None
        workspace_path = Path(str(raw_path)).expanduser()
        if not workspace_path.is_absolute():
            workspace_path = self.settings.workspace_root / workspace_path
        assert_within_root(workspace_path, self.settings.workspace_root)
        return workspace_path

    def _launch_workspace_path(self, workspace_path: Path | None) -> Path | None:
        candidate = workspace_path or self.settings.workspace_root
        return candidate if candidate and candidate.exists() else workspace_path

    def _restore_trae_foreground(self, data: dict) -> dict:
        if not self.settings.keep_trae_foreground:
            return data
        result = dict(data)
        try:
            focus_result = focus_trae(timeout_seconds=1.0)
            self.state.current_window_title = str(focus_result.get("window_title") or self.state.current_window_title)
            result["trae_foreground"] = focus_result
        except Exception as exc:
            result["trae_foreground"] = {"status": "not_restored", "error": str(exc)}
        return result

    def _success(self, command_id: str, data: dict, lease_id: str = "") -> dict:
        return {
            "command_id": command_id,
            "worker_id": self.worker_id,
            "lease_id": lease_id,
            "status": "success",
            "message": "Command processed",
            "data": data,
        }

    def _manual_required(self, command_id: str, error: Any, lease_id: str = "") -> dict:
        details = getattr(error, "details", None)
        return {
            "command_id": command_id,
            "worker_id": self.worker_id,
            "lease_id": lease_id,
            "status": "manual_required",
            "message": "Command requires manual worker intervention",
            "data": details if isinstance(details, dict) else {},
            "error": str(error),
        }

    def _cancelled(self, command_id: str, message: str, lease_id: str = "") -> dict:
        return {
            "command_id": command_id,
            "worker_id": self.worker_id,
            "lease_id": lease_id,
            "status": "cancelled",
            "message": message or "Command cancelled",
            "data": {},
            "error": "",
        }

    def _failed(self, command_id: str, error: str, lease_id: str = "") -> dict:
        return {
            "command_id": command_id,
            "worker_id": self.worker_id,
            "lease_id": lease_id,
            "status": "failed",
            "message": "Command failed",
            "data": {},
            "error": error,
        }


def _float_or_none(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _current_turn_gate(turn: object, trace_probe: object = None) -> dict:
    if isinstance(trace_probe, dict):
        probe_reason = str(trace_probe.get("reason") or "")
        if probe_reason in RECOVERABLE_TRACE_PROBE_REASONS:
            return {
                "passed": False,
                "reason": probe_reason,
                "recoverable": True,
                "source": "trace_probe",
            }
    if not isinstance(turn, dict):
        return {
            "passed": False,
            "reason": "current_turn_probe_missing",
            "recoverable": False,
            "source": "trae_turn",
        }
    if turn.get("status") != "found":
        reason = str(turn.get("reason") or "current_turn_missing")
        return {
            "passed": False,
            "reason": reason,
            "recoverable": _recoverable_turn_gate_reason(reason),
            "source": "trae_turn",
            "candidate": turn.get("candidate") if isinstance(turn.get("candidate"), dict) else None,
        }
    turn_status = str(turn.get("turn_status") or "")
    if turn_status != "completed":
        return {
            "passed": False,
            "reason": f"trae_turn_not_completed:{turn_status or 'unknown'}",
            "recoverable": True,
            "source": "trae_turn",
            "session_id": str(turn.get("session_id") or ""),
            "user_message_id": str(turn.get("user_message_id") or ""),
        }
    return {
        "passed": True,
        "reason": "ok",
        "recoverable": False,
        "source": "trae_turn",
        "session_id": str(turn.get("session_id") or ""),
        "user_message_id": str(turn.get("user_message_id") or ""),
    }


def _recoverable_turn_gate_reason(reason: str) -> bool:
    if reason in RECOVERABLE_TURN_GATE_REASONS:
        return True
    return reason.startswith("trae_turn_not_completed:")
