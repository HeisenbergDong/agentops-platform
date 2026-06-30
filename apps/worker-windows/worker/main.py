from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker.capabilities import CAPABILITIES, SUPPORTED_APPS, WORKER_RUNTIME_VERSION
from worker.config import WorkerSettings, apply_assigned_config, default_config_path, load_worker_settings
from worker.connection.client import WorkerClient
from worker.connection.uploader import AttachmentUploader
from worker.registration import RegistrationOptions, is_registered, machine_fingerprint, register_worker
from worker.runtime.supervisor import SupervisorOptions, run_supervisor
from worker.runtime.windows_service import run_windows_service
from worker.system.console import disable_quick_edit_mode
from worker.trae.window import candidate_trae_paths

ACTIVE_COMMAND_STATUSES = {"queued", "claimed", "running"}
STALE_LEASE_STATUSES = {"stale_lease", "expired_lease"}
MAX_COMMAND_STATUS_FAILURES = 3
LEASE_RENEW_INTERVAL_SECONDS = 2.0
COMMAND_HEARTBEAT_INTERVAL_SECONDS = 15.0


def run_once(
    client: WorkerClient | None = None,
    runner: Any | None = None,
    worker_settings: WorkerSettings | None = None,
) -> int:
    worker_settings = worker_settings or load_worker_settings()
    client = client or WorkerClient(worker_settings.server_url, worker_settings.token)
    runner = runner or create_command_runner(worker_settings)
    if getattr(runner, "cancellation_checker", None) is None:
        attach_cancellation_checker(runner, client, worker_settings)
    heartbeat = build_heartbeat_payload(worker_settings, runner)
    heartbeat_result = client.heartbeat(heartbeat)
    sync_runtime_config(worker_settings, runner, heartbeat_result)
    commands = client.poll_commands(worker_settings.worker_id)
    processed = 0
    for command in commands:
        if is_cancelled_command(command):
            result = cancelled_result(worker_settings.worker_id, command, "Command was cancelled before ack.")
            client.post_result(worker_settings.worker_id, result)
            processed += 1
            continue
        acked = client.ack_command(
            worker_settings.worker_id,
            command["command_id"],
            lease_id=str(command.get("lease_id") or ""),
        )
        if is_stale_lease_response(acked):
            log(f"Skipped stale worker command lease: {command.get('command_id')}")
            processed += 1
            continue
        if is_cancelled_command(acked):
            result = cancelled_result(worker_settings.worker_id, command, "Command was cancelled before worker execution.")
            client.post_result(worker_settings.worker_id, result)
            processed += 1
            continue
        command = {**command, "lease_id": str(acked.get("lease_id") or command.get("lease_id") or "")}
        renewer = CommandLeaseRenewer(client, worker_settings.worker_id, command, runner, worker_settings)
        renewer.start()
        try:
            post_worker_event(client, worker_settings.worker_id, command, "worker_command_started")
            result = runner.run(command)
            refresh_cancelled_state_after_run(client, worker_settings.worker_id, command, runner)
            if should_convert_to_cancelled_stop(command, runner, result):
                result = cancelled_stop_result(
                    worker_settings.worker_id,
                    command,
                    runner,
                    "Command was cancelled by server stop request; local stop cleanup was executed.",
                )
            if command.get("lease_id") and "lease_id" not in result:
                result["lease_id"] = command["lease_id"]
            result = attach_worker_uploads(client, worker_settings.worker_id, command, result)
            post_worker_event(
                client,
                worker_settings.worker_id,
                command,
                "worker_command_finished",
                level=worker_command_finished_level(str(command.get("type") or ""), str(result.get("status") or "")),
                extra={
                    "result_status": result.get("status"),
                    "error": result.get("error") or "",
                    "result": result.get("data") if isinstance(result.get("data"), dict) else {},
                },
            )
            client.post_result(worker_settings.worker_id, result)
        finally:
            renewer.stop()
        processed += 1
    return processed


def build_heartbeat_payload(
    worker_settings: WorkerSettings,
    runner: Any,
    *,
    active_command: dict | None = None,
) -> dict[str, Any]:
    runtime_status = worker_runtime_status(worker_settings)
    if active_command:
        runtime_status["active_command"] = {
            "command_id": str(active_command.get("command_id") or ""),
            "lease_id": str(active_command.get("lease_id") or ""),
            "type": str(active_command.get("type") or ""),
            "status": str(active_command.get("status") or "running"),
            "job_id": str(active_command.get("job_id") or ""),
            "round_id": str(active_command.get("round_id") or ""),
            "stage": str(getattr(getattr(runner, "state", None), "stage", "") or ""),
        }
    state = getattr(runner, "state", None)
    return {
        "worker_id": worker_settings.worker_id,
        "machine_name": socket.gethostname(),
        "display_name": worker_settings.display_name,
        "worker_type": worker_settings.worker_type,
        "machine_fingerprint": machine_fingerprint(),
        "version": WORKER_RUNTIME_VERSION,
        "config_version": worker_settings.version,
        "supported_apps": SUPPORTED_APPS,
        "capabilities": CAPABILITIES,
        "current_stage": str(getattr(state, "stage", "idle") or "idle"),
        "current_window_title": str(getattr(state, "current_window_title", "") or ""),
        "runtime_status": runtime_status,
        "busy": bool(getattr(state, "busy", False)),
    }


class CommandLeaseRenewer:
    def __init__(
        self,
        client: WorkerClient,
        worker_id: str,
        command: dict,
        runner: Any,
        worker_settings: WorkerSettings | None = None,
    ) -> None:
        self.client = client
        self.worker_id = worker_id
        self.command = dict(command or {})
        self.command_id = str(command.get("command_id") or "")
        self.lease_id = str(command.get("lease_id") or "")
        self.runner = runner
        self.worker_settings = worker_settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._last_heartbeat_at = 0.0

    def start(self) -> None:
        if not self.command_id or not self.lease_id:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"agentops-lease-renewer-{self.command_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(LEASE_RENEW_INTERVAL_SECONDS):
            try:
                command = self.client.get_command(self.worker_id, self.command_id, lease_id=self.lease_id)
            except Exception as exc:
                self._failures += 1
                log(
                    f"Could not renew command lease for {self.command_id}: "
                    f"{exc} ({self._failures}/{MAX_COMMAND_STATUS_FAILURES})."
                )
                if self._failures >= MAX_COMMAND_STATUS_FAILURES:
                    self._request_stop()
                    return
                continue
            self._failures = 0
            if is_stale_lease_response(command):
                log(f"Command lease became stale for {self.command_id}; stopping local execution.")
                self._request_stop()
                return
            if is_cancelled_command(command):
                log(f"Command was cancelled on server for {self.command_id}; stopping local execution.")
                self._request_stop()
                return
            self._maybe_send_heartbeat(command)

    def _request_stop(self) -> None:
        state = getattr(self.runner, "state", None)
        if state is not None:
            state.stop_requested = True
            if not isinstance(getattr(state, "stop_cleanup_result", None), dict):
                state.stop_cleanup_result = self._run_stop_cleanup()

    def _run_stop_cleanup(self) -> dict:
        if not hasattr(self.runner, "_cancelled_stop_data"):
            return {}
        payload = self._current_command_payload()
        try:
            return self.runner._cancelled_stop_data(payload)
        except Exception as exc:
            return {
                "stopped": False,
                "message": "Worker stop cleanup failed after server cancellation.",
                "stop_reason": "server_cancelled_current_command",
                "stop_report": {
                    "worker_command_cancelled": True,
                    "stop_confirmed": False,
                    "cleanup_status": "failed",
                    "cleanup_error": str(exc),
                    "trae_stop_clicked": False,
                    "local_processes_matched": 0,
                    "local_processes_killed": 0,
                    "local_process_kill_errors": 1,
                },
            }

    def _current_command_payload(self) -> dict:
        try:
            command = self.client.get_command(self.worker_id, self.command_id, lease_id=self.lease_id)
        except Exception:
            return {}
        payload = command.get("payload") if isinstance(command, dict) else {}
        return payload if isinstance(payload, dict) else {}

    def _maybe_send_heartbeat(self, command: dict | None = None) -> None:
        if not self.worker_settings:
            return
        now = time.monotonic()
        if self._last_heartbeat_at and now - self._last_heartbeat_at < COMMAND_HEARTBEAT_INTERVAL_SECONDS:
            return
        active_command = {**self.command, **(command if isinstance(command, dict) else {})}
        active_command.setdefault("status", "running")
        try:
            self.client.heartbeat(
                build_heartbeat_payload(
                    self.worker_settings,
                    self.runner,
                    active_command=active_command,
                )
            )
            self._last_heartbeat_at = now
        except Exception as exc:
            log(f"Could not send active command heartbeat for {self.command_id}: {exc}.")


def run_forever(worker_settings: WorkerSettings | None = None) -> None:
    worker_settings = worker_settings or load_worker_settings()
    print_runtime_summary(worker_settings)
    client = WorkerClient(worker_settings.server_url, worker_settings.token)
    runner = create_command_runner(worker_settings)
    attach_cancellation_checker(runner, client, worker_settings)
    if worker_settings.auto_launch_trae_on_startup:
        log("Trae startup auto-launch is disabled; Trae will open only when a job command arrives.")
    last_idle_log_at = 0.0
    while True:
        try:
            processed = run_once(client, runner, worker_settings)
            now = time.time()
            if processed:
                log(f"Processed {processed} command(s).")
            elif now - last_idle_log_at >= 30:
                log("Heartbeat OK; no queued commands.")
                last_idle_log_at = now
        except KeyboardInterrupt:
            log("Worker stopped by user.")
            return
        except Exception as exc:
            retry_seconds = max(worker_settings.poll_interval_seconds, 5.0)
            log(f"Worker loop error: {exc}. Retrying in {retry_seconds:g}s.")
            time.sleep(retry_seconds)
            continue
        time.sleep(worker_settings.poll_interval_seconds)


def main() -> None:
    disable_quick_edit_mode()
    try:
        _main()
    except KeyboardInterrupt:
        log("Worker stopped by user.")
    except Exception as exc:
        log(f"Startup error: {exc}")
        pause_before_exit()
        raise SystemExit(1) from exc


def _main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config_path = Path(args.config).expanduser() if getattr(args, "config", None) else default_config_path()
    if args.command not in {"supervise", "service-run"}:
        print_banner(config_path)
    if args.command == "register":
        worker_settings = register_from_args(args, config_path)
        if args.start:
            run_forever(worker_settings)
        return
    if args.command == "supervise":
        code = run_supervisor(
            SupervisorOptions(
                config_path=config_path,
                log_dir=args.log_dir,
                restart_delay_seconds=args.restart_delay_seconds,
                max_restart_attempts=args.max_restart_attempts,
                log_max_bytes=int(args.log_max_mb * 1024 * 1024),
                log_backups=args.log_backups,
                pid_file=args.pid_file,
            )
        )
        raise SystemExit(code)
    if args.command == "service-run":
        code = run_windows_service(
            args.service_name,
            SupervisorOptions(
                config_path=config_path,
                log_dir=args.log_dir,
                restart_delay_seconds=args.restart_delay_seconds,
                max_restart_attempts=args.max_restart_attempts,
                log_max_bytes=int(args.log_max_mb * 1024 * 1024),
                log_backups=args.log_backups,
                pid_file=args.pid_file,
            ),
            console_fallback=args.console_fallback,
        )
        raise SystemExit(code)

    worker_settings = load_worker_settings(config_path)
    if not is_registered(worker_settings):
        worker_settings = interactive_register(config_path)
    else:
        print_registered_status(worker_settings)

    once = bool(getattr(args, "once", False))
    if once:
        processed = run_once(worker_settings=worker_settings)
        log(f"One poll cycle complete. Processed {processed} command(s).")
    else:
        run_forever(worker_settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentops-worker",
        description="AgentOps Windows Worker registration and runtime",
    )
    parser.add_argument("--config", help="Path to worker JSON config file")
    parser.add_argument("--once", action="store_true", help="Run one heartbeat/poll cycle")
    subparsers = parser.add_subparsers(dest="command")

    register_parser = subparsers.add_parser("register", help="Register this worker with AgentOps")
    register_parser.add_argument("--config", help="Path to worker JSON config file")
    register_parser.add_argument("--server-url", help="AgentOps server URL, for example http://115.190.113.8")
    register_parser.add_argument("--registration-code", help="One-time worker registration code from the admin page")
    register_parser.add_argument("--worker-id", default="", help="Optional stable worker id")
    register_parser.add_argument("--display-name", default="", help="Human-readable worker name")
    register_parser.add_argument("--trae-exe-path", type=Path, help="Path to Trae CN executable")
    register_parser.add_argument("--workspace-root", type=Path, help="Allowed local workspace root")
    register_parser.add_argument("--poll-interval-seconds", type=float, help="Polling interval")
    register_parser.add_argument("--start", action="store_true", help="Start the worker after successful registration")

    run_parser = subparsers.add_parser("run", help="Run the registered worker")
    run_parser.add_argument("--config", help="Path to worker JSON config file")
    run_parser.add_argument("--once", action="store_true", help="Run one heartbeat/poll cycle")

    supervise_parser = subparsers.add_parser("supervise", help="Run the worker under a local restart supervisor")
    supervise_parser.add_argument("--config", help="Path to worker JSON config file")
    supervise_parser.add_argument("--log-dir", type=Path, help="Directory for supervised worker logs")
    supervise_parser.add_argument("--restart-delay-seconds", type=float, default=5.0, help="Delay before restarting after a crash")
    supervise_parser.add_argument(
        "--max-restart-attempts",
        type=int,
        default=0,
        help="Maximum crash restarts before exiting; 0 means unlimited",
    )
    supervise_parser.add_argument("--log-max-mb", type=float, default=10.0, help="Rotate worker log after this many MiB")
    supervise_parser.add_argument("--log-backups", type=int, default=5, help="Number of rotated worker logs to keep")
    supervise_parser.add_argument("--pid-file", type=Path, help="Path to the supervisor pid file")

    service_parser = subparsers.add_parser("service-run", help="Internal entrypoint used by Windows Service")
    service_parser.add_argument("--config", help="Path to worker JSON config file")
    service_parser.add_argument("--service-name", default="AgentOpsWorker", help="Windows service name")
    service_parser.add_argument("--log-dir", type=Path, help="Directory for supervised worker logs")
    service_parser.add_argument("--restart-delay-seconds", type=float, default=5.0, help="Delay before restarting after a crash")
    service_parser.add_argument(
        "--max-restart-attempts",
        type=int,
        default=0,
        help="Maximum crash restarts before exiting; 0 means unlimited",
    )
    service_parser.add_argument("--log-max-mb", type=float, default=10.0, help="Rotate worker log after this many MiB")
    service_parser.add_argument("--log-backups", type=int, default=5, help="Number of rotated worker logs to keep")
    service_parser.add_argument("--pid-file", type=Path, help="Path to the supervisor pid file")
    service_parser.add_argument(
        "--console-fallback",
        action="store_true",
        help="Run as a console supervisor if not launched by Service Control Manager",
    )
    return parser


def register_from_args(args: argparse.Namespace, config_path: Path) -> WorkerSettings:
    server_url = args.server_url or _prompt("AgentOps server URL", "http://115.190.113.8")
    registration_code = args.registration_code or _prompt("Worker registration code")
    options = RegistrationOptions(
        server_url=server_url,
        registration_code=registration_code,
        worker_id=args.worker_id or "",
        display_name=args.display_name or "",
        config_path=config_path,
        trae_exe_path=args.trae_exe_path,
        workspace_root=args.workspace_root,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    log("Registering worker...")
    worker_settings, saved_path, _response = register_worker(options)
    log(f"Worker registered: {worker_settings.worker_id}")
    log(f"Config saved: {saved_path}")
    return worker_settings


def interactive_register(config_path: Path) -> WorkerSettings:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Worker is not registered. Run `agentops-worker register --server-url ... "
            "--registration-code ...` first."
        )
    print("Worker is not registered yet.")
    print("Create a registration code in AgentOps Web > Worker, then enter it here.")
    print("You only need to do this once on this machine.")
    args = argparse.Namespace(
        server_url=_prompt("AgentOps server URL", "http://115.190.113.8"),
        registration_code=_prompt("Worker registration code"),
        worker_id="",
        display_name="",
        trae_exe_path=None,
        workspace_root=None,
        poll_interval_seconds=None,
    )
    return register_from_args(args, config_path)


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def create_command_runner(worker_settings: WorkerSettings) -> Any:
    from worker.runtime.command_runner import CommandRunner

    client = WorkerClient(worker_settings.server_url, worker_settings.token)
    return CommandRunner(worker_settings.worker_id, runtime_settings=worker_settings, worker_client=client)


def attach_cancellation_checker(
    runner: Any,
    client: WorkerClient,
    worker_settings: WorkerSettings,
) -> None:
    failures_by_command: dict[str, int] = {}

    def checker(command_id: str) -> bool:
        try:
            command = client.get_command(
                worker_settings.worker_id,
                command_id,
                lease_id=getattr(runner.state, "current_lease_id", ""),
            )
        except Exception as exc:
            failures = failures_by_command.get(command_id, 0) + 1
            failures_by_command[command_id] = failures
            log(f"Could not read command status for {command_id}: {exc} ({failures}/{MAX_COMMAND_STATUS_FAILURES}).")
            return failures >= MAX_COMMAND_STATUS_FAILURES
        failures_by_command[command_id] = 0
        if is_stale_lease_response(command):
            log(f"Command lease is no longer active for {command_id}; stopping local execution.")
            return True
        return is_cancelled_command(command)

    runner.cancellation_checker = checker


def sync_runtime_config(worker_settings: WorkerSettings, runner: Any, heartbeat_result: dict | None) -> dict[str, str]:
    assigned_config = heartbeat_result.get("assigned_config") if isinstance(heartbeat_result, dict) else None
    changes = apply_assigned_config(worker_settings, assigned_config)
    if not changes:
        return {}
    if getattr(runner, "settings", None) is not worker_settings:
        runner.settings = worker_settings
    log(f"Applied server worker config: {format_runtime_config_changes(changes)}")
    return changes


def worker_runtime_status(worker_settings: WorkerSettings) -> dict[str, Any]:
    trae_path = Path(worker_settings.trae_exe_path).expanduser()
    workspace_root = Path(worker_settings.workspace_root).expanduser()
    candidates = candidate_trae_paths(trae_path)
    resolved = next((candidate for candidate in candidates if candidate.exists() and candidate.is_file()), None)
    return {
        "trae_exe_path": str(trae_path),
        "trae_exe_exists": bool(resolved),
        "trae_exe_resolved_path": str(resolved or ""),
        "trae_exe_candidates": [str(candidate) for candidate in candidates[:8]],
        "workspace_root": str(workspace_root),
        "workspace_root_exists": workspace_root.exists(),
        "browser_url": worker_settings.browser_url,
        "keep_trae_foreground": worker_settings.keep_trae_foreground,
        "config_version": worker_settings.version,
    }


def format_runtime_config_changes(changes: dict[str, str]) -> str:
    ordered = []
    if "workspace_root" in changes:
        ordered.append(f"workspace_root={changes['workspace_root']}")
    if "browser_url" in changes:
        ordered.append(f"browser_url={changes['browser_url'] or '-'}")
    for key, value in changes.items():
        if key not in {"workspace_root", "browser_url"}:
            ordered.append(f"{key}={value}")
    return ", ".join(ordered)


def attach_worker_uploads(
    client: WorkerClient,
    worker_id: str,
    command: dict,
    result: dict,
) -> dict:
    command_type = str(command.get("type") or "")
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return result
    if command_type == "wait_completion":
        return attach_wait_diagnostic_upload(client, worker_id, command, result, data)
    if command_type != "capture_screenshot":
        return result
    path = Path(str(data.get("path") or ""))
    if not path:
        return result
    try:
        attachment = AttachmentUploader(client, worker_id).upload(
            path,
            "screenshot",
            job_id=str(command.get("job_id") or ""),
            round_id=str(command.get("round_id") or ""),
            content_type=str(data.get("content_type") or "image/png"),
        )
    except Exception as exc:
        result["data"] = {**data, "upload_status": "failed", "upload_error": str(exc)}
        return result
    result["data"] = {**data, "upload_status": "uploaded", "server_attachment": attachment}
    return result


def attach_wait_diagnostic_upload(
    client: WorkerClient,
    worker_id: str,
    command: dict,
    result: dict,
    data: dict,
) -> dict:
    screenshot = diagnostic_screenshot_from_wait_data(data)
    path_text = str(screenshot.get("path") or "").strip()
    if not path_text:
        return result
    path = Path(path_text)
    try:
        attachment = AttachmentUploader(client, worker_id).upload(
            path,
            "diagnostic_screenshot",
            job_id=str(command.get("job_id") or ""),
            round_id=str(command.get("round_id") or ""),
            content_type=str(screenshot.get("content_type") or "image/png"),
        )
    except Exception as exc:
        result["data"] = {**data, "diagnostic_upload_status": "failed", "diagnostic_upload_error": str(exc)}
        return result
    result["data"] = _set_diagnostic_server_attachment(data, attachment)
    result["data"]["diagnostic_upload_status"] = "uploaded"
    return result


def diagnostic_screenshot_from_wait_data(data: dict) -> dict:
    decision = data.get("supervisor_decision") if isinstance(data.get("supervisor_decision"), dict) else {}
    diagnosis = decision.get("diagnosis") if isinstance(decision.get("diagnosis"), dict) else {}
    visual = diagnosis.get("visual") if isinstance(diagnosis.get("visual"), dict) else {}
    screenshot = visual.get("screenshot") if isinstance(visual.get("screenshot"), dict) else {}
    return screenshot


def _set_diagnostic_server_attachment(data: dict, attachment: dict) -> dict:
    updated = dict(data)
    decision = dict(updated.get("supervisor_decision") or {})
    diagnosis = dict(decision.get("diagnosis") or {})
    visual = dict(diagnosis.get("visual") or {})
    screenshot = dict(visual.get("screenshot") or {})
    screenshot["server_attachment"] = attachment
    visual["screenshot"] = screenshot
    diagnosis["visual"] = visual
    decision["diagnosis"] = diagnosis
    updated["supervisor_decision"] = decision
    updated["diagnostic_server_attachment"] = attachment
    return updated


def try_auto_launch_trae(runner: Any) -> None:
    try:
        result = runner.ensure_trae_ready()
    except Exception as exc:
        log(f"Trae auto-start failed: {exc}. Worker will keep polling and retry when a Trae command arrives.")
        return
    status = result.get("status", "ready")
    title = result.get("window_title") or "-"
    log(f"Trae ready: {status}; window={title}")


def post_worker_event(
    client: WorkerClient,
    worker_id: str,
    command: dict,
    stage: str,
    *,
    level: str = "info",
    extra: dict[str, Any] | None = None,
) -> None:
    payload = command.get("payload") or {}
    command_type = str(command.get("type") or "")
    event_extra = {
        "command_type": command_type,
        "worker_id": worker_id,
        "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
    }
    if isinstance(payload, dict):
        for key in (
            "trae_workspace_path",
            "workspace_path",
            "browser_url",
            "url",
            "command",
            "round_index",
        ):
            if key in payload:
                event_extra[key] = payload[key]
    if extra:
        event_extra.update(extra)
    try:
        client.post_log(
            worker_id,
            {
                "command_id": command.get("command_id"),
                "job_id": command.get("job_id"),
                "round_id": command.get("round_id"),
                "level": level,
                "stage": stage,
                "message": f"{command_type} {stage}",
                "extra": event_extra,
            },
        )
    except Exception as exc:
        log(f"Could not post worker event {stage}: {exc}")


def worker_command_finished_level(command_type: str, status: str) -> str:
    if status in {"ok", "success", "completed"}:
        return "info"
    if command_type in {"wait_completion", "copy_latest_reply"}:
        return "info"
    return "warning"


def print_banner(config_path: Path) -> None:
    print("AgentOps Windows Worker")
    print("=" * 24)
    print(f"Config: {config_path}")
    print("Close this window to stop the worker. Open agentops-worker.exe again to restart it.")
    print()


def print_registered_status(worker_settings: WorkerSettings) -> None:
    print("Registered worker found.")
    print(f"Server: {worker_settings.server_url}")
    print(f"Worker ID: {worker_settings.worker_id}")
    print(f"Display name: {worker_settings.display_name or socket.gethostname()}")
    print(f"Workspace root: {worker_settings.workspace_root}")
    print(f"Trae CN path: {worker_settings.trae_exe_path}")
    print()


def print_runtime_summary(worker_settings: WorkerSettings) -> None:
    log("Starting worker runtime.")
    log(f"Polling {worker_settings.server_url} every {worker_settings.poll_interval_seconds:g}s.")
    if sys.stdin.isatty():
        log("Keep this window open while the worker should stay online.")


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def is_cancelled_command(command: dict) -> bool:
    status = str(command.get("status") or "").lower()
    if status == "cancelled":
        return True
    if status in ACTIVE_COMMAND_STATUSES:
        return False
    command_type = str(command.get("type") or "")
    return bool(status) and command_type != "stop_current_task"


def is_stale_lease_response(command: dict) -> bool:
    status = str(command.get("status") or command.get("ack_status") or command.get("read_status") or "").lower()
    reason = str(command.get("reason") or "").lower()
    return status in STALE_LEASE_STATUSES or reason in STALE_LEASE_STATUSES


def cancelled_result(worker_id: str, command: dict, message: str) -> dict:
    return {
        "command_id": command.get("command_id", ""),
        "worker_id": worker_id,
        "lease_id": str(command.get("lease_id") or ""),
        "status": "cancelled",
        "message": message,
        "data": {},
        "error": "",
    }


def cancelled_stop_result(worker_id: str, command: dict, runner: Any, message: str) -> dict:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    try:
        data = runner._cancelled_stop_data(payload) if hasattr(runner, "_cancelled_stop_data") else {}
    except Exception as exc:
        data = {
            "stopped": False,
            "message": "Worker stop cleanup failed after command cancellation.",
            "stop_reason": "server_cancelled_current_command",
            "stop_report": {
                "worker_command_cancelled": True,
                "stop_confirmed": False,
                "cleanup_status": "failed",
                "cleanup_error": str(exc),
                "trae_stop_clicked": False,
                "local_processes_matched": 0,
                "local_processes_killed": 0,
                "local_process_kill_errors": 1,
            },
        }
    result = cancelled_result(worker_id, command, message)
    result["data"] = data if isinstance(data, dict) else {}
    return result


def should_convert_to_cancelled_stop(command: dict, runner: Any, result: dict) -> bool:
    if str(command.get("type") or "") == "stop_current_task":
        return False
    if str(result.get("status") or "") == "cancelled":
        return False
    state = getattr(runner, "state", None)
    return bool(getattr(state, "stop_requested", False))


def refresh_cancelled_state_after_run(client: WorkerClient, worker_id: str, command: dict, runner: Any) -> None:
    if str(command.get("type") or "") == "stop_current_task":
        return
    lease_id = str(command.get("lease_id") or "")
    command_id = str(command.get("command_id") or "")
    if not lease_id or not command_id:
        return
    try:
        latest = client.get_command(worker_id, command_id, lease_id=lease_id)
    except Exception as exc:
        log(f"Could not refresh command status after run for {command_id}: {exc}")
        return
    if is_cancelled_command(latest) or is_stale_lease_response(latest):
        state = getattr(runner, "state", None)
        if state is not None:
            state.stop_requested = True


def pause_before_exit() -> None:
    if sys.stdin.isatty():
        try:
            input("Press Enter to exit...")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
