import argparse
import socket
import time

from worker.config import settings
from worker.connection.client import WorkerClient
from worker.runtime.command_runner import CommandRunner


def run_once(client: WorkerClient | None = None, runner: CommandRunner | None = None) -> None:
    client = client or WorkerClient(settings.server_url, settings.token)
    runner = runner or CommandRunner(settings.worker_id)
    heartbeat = {
        "worker_id": settings.worker_id,
        "machine_name": socket.gethostname(),
        "supported_apps": ["Trae CN"],
        "capabilities": [
            "open_trae",
            "focus_trae",
            "send_prompt",
            "wait_completion",
            "copy_latest_reply",
            "capture_screenshot",
            "scan_project",
            "run_command",
            "browser_acceptance",
            "git_submit",
        ],
        "current_stage": runner.state.stage,
        "current_window_title": runner.state.current_window_title,
        "busy": runner.state.busy,
    }
    client.heartbeat(heartbeat)
    commands = client.poll_commands(settings.worker_id)
    for command in commands:
        client.ack_command(settings.worker_id, command["command_id"])
        result = runner.run(command)
        client.post_result(settings.worker_id, result)


def run_forever() -> None:
    client = WorkerClient(settings.server_url, settings.token)
    runner = CommandRunner(settings.worker_id)
    while True:
        run_once(client, runner)
        time.sleep(settings.poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run a single heartbeat/poll cycle")
    args = parser.parse_args()
    if args.once:
        run_once()
    else:
        run_forever()


if __name__ == "__main__":
    main()
