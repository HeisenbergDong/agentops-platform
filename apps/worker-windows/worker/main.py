from __future__ import annotations

import argparse
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker.capabilities import CAPABILITIES, SUPPORTED_APPS
from worker.config import WorkerSettings, default_config_path, load_worker_settings
from worker.connection.client import WorkerClient
from worker.registration import RegistrationOptions, is_registered, machine_fingerprint, register_worker
from worker.system.console import disable_quick_edit_mode

ACTIVE_COMMAND_STATUSES = {"queued", "claimed", "running"}
MAX_COMMAND_STATUS_FAILURES = 3


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
    heartbeat = {
        "worker_id": worker_settings.worker_id,
        "machine_name": socket.gethostname(),
        "display_name": worker_settings.display_name,
        "worker_type": worker_settings.worker_type,
        "machine_fingerprint": machine_fingerprint(),
        "version": worker_settings.version,
        "supported_apps": SUPPORTED_APPS,
        "capabilities": CAPABILITIES,
        "current_stage": runner.state.stage,
        "current_window_title": runner.state.current_window_title,
        "busy": runner.state.busy,
    }
    client.heartbeat(heartbeat)
    commands = client.poll_commands(worker_settings.worker_id)
    processed = 0
    for command in commands:
        if is_cancelled_command(command):
            result = cancelled_result(worker_settings.worker_id, command, "Command was cancelled before ack.")
            client.post_result(worker_settings.worker_id, result)
            processed += 1
            continue
        acked = client.ack_command(worker_settings.worker_id, command["command_id"])
        if is_cancelled_command(acked):
            result = cancelled_result(worker_settings.worker_id, command, "Command was cancelled before worker execution.")
            client.post_result(worker_settings.worker_id, result)
            processed += 1
            continue
        post_worker_event(client, worker_settings.worker_id, command, "worker_command_started")
        result = runner.run(command)
        post_worker_event(
            client,
            worker_settings.worker_id,
            command,
            "worker_command_finished",
            level="info" if result.get("status") in {"ok", "success", "completed"} else "warning",
            extra={
                "result_status": result.get("status"),
                "error": result.get("error") or "",
            },
        )
        client.post_result(worker_settings.worker_id, result)
        processed += 1
    return processed


def run_forever(worker_settings: WorkerSettings | None = None) -> None:
    worker_settings = worker_settings or load_worker_settings()
    print_runtime_summary(worker_settings)
    client = WorkerClient(worker_settings.server_url, worker_settings.token)
    runner = create_command_runner(worker_settings)
    attach_cancellation_checker(runner, client, worker_settings)
    if worker_settings.auto_launch_trae_on_startup:
        try_auto_launch_trae(runner)
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
    print_banner(config_path)
    if args.command == "register":
        worker_settings = register_from_args(args, config_path)
        if args.start:
            run_forever(worker_settings)
        return

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

    return CommandRunner(worker_settings.worker_id, runtime_settings=worker_settings)


def attach_cancellation_checker(
    runner: Any,
    client: WorkerClient,
    worker_settings: WorkerSettings,
) -> None:
    failures_by_command: dict[str, int] = {}

    def checker(command_id: str) -> bool:
        try:
            command = client.get_command(worker_settings.worker_id, command_id)
        except Exception as exc:
            failures = failures_by_command.get(command_id, 0) + 1
            failures_by_command[command_id] = failures
            log(f"Could not read command status for {command_id}: {exc} ({failures}/{MAX_COMMAND_STATUS_FAILURES}).")
            return failures >= MAX_COMMAND_STATUS_FAILURES
        failures_by_command[command_id] = 0
        return is_cancelled_command(command)

    runner.cancellation_checker = checker


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


def cancelled_result(worker_id: str, command: dict, message: str) -> dict:
    return {
        "command_id": command.get("command_id", ""),
        "worker_id": worker_id,
        "status": "cancelled",
        "message": message,
        "data": {},
        "error": "",
    }


def pause_before_exit() -> None:
    if sys.stdin.isatty():
        try:
            input("Press Enter to exit...")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
