import argparse
import socket
import time

from worker.config import settings
from worker.connection.client import WorkerClient
from worker.runtime.command_runner import CommandRunner


def run_once() -> None:
    client = WorkerClient(settings.server_url, settings.token)
    runner = CommandRunner()
    heartbeat = {
        "worker_id": settings.worker_id,
        "machine_name": socket.gethostname(),
        "supported_apps": ["Trae CN"],
        "current_stage": runner.state.stage,
        "current_window_title": runner.state.current_window_title,
        "busy": runner.state.busy,
    }
    client.heartbeat(heartbeat)
    commands = client.poll_commands(settings.worker_id)
    for command in commands:
        result = runner.run(command)
        client.post_result(settings.worker_id, result)


def run_forever() -> None:
    while True:
        run_once()
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
