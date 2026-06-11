from pathlib import Path

from worker.config import WorkerSettings
from worker import main as worker_main


class FakeClient:
    def __init__(self, commands, ack_response):
        self.commands = commands
        self.ack_response = ack_response
        self.results = []

    def heartbeat(self, payload):
        return {"status": "ok"}

    def poll_commands(self, worker_id):
        return self.commands

    def ack_command(self, worker_id, command_id):
        return self.ack_response

    def post_result(self, worker_id, payload):
        self.results.append(payload)
        return {"status": "received"}

    def upload_attachment(self, worker_id, path, *, kind, job_id=None, round_id=None, content_type="application/octet-stream"):
        return {
            "status": "uploaded",
            "attachment": {
                "id": "att1",
                "kind": kind,
                "job_id": job_id,
                "round_id": round_id,
                "path": "storage/workers/worker-test/screenshot/screen.png",
                "filename": "screen.png",
                "content_type": content_type,
                "size_bytes": 3,
            },
        }


class FakeRunner:
    def __init__(self):
        self.state = type("State", (), {"stage": "idle", "current_window_title": "", "busy": False})()
        self.cancellation_checker = None
        self.ran = False

    def run(self, command):
        self.ran = True
        return {
            "command_id": command["command_id"],
            "worker_id": "worker-test",
            "status": "success",
            "message": "ran",
            "data": {},
        }


def test_run_once_skips_command_cancelled_after_ack(tmp_path: Path):
    command = {"command_id": "cmd1", "type": "wait_completion", "payload": {}}
    client = FakeClient(commands=[command], ack_response={**command, "status": "cancelled"})
    runner = FakeRunner()
    settings = WorkerSettings(
        worker_id="worker-test",
        token="test-token",
        workspace_root=tmp_path,
        trae_exe_path=tmp_path / "Trae.exe",
    )

    processed = worker_main.run_once(client=client, runner=runner, worker_settings=settings)

    assert processed == 1
    assert runner.ran is False
    assert client.results[0]["status"] == "cancelled"


def test_run_once_uploads_screenshot_before_posting_result(tmp_path: Path):
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"png")
    command = {
        "command_id": "cmd1",
        "job_id": "job1",
        "round_id": "round1",
        "type": "capture_screenshot",
        "payload": {},
    }
    client = FakeClient(commands=[command], ack_response={**command, "status": "running"})

    class ScreenshotRunner(FakeRunner):
        def run(self, command):
            self.ran = True
            return {
                "command_id": command["command_id"],
                "worker_id": "worker-test",
                "status": "success",
                "message": "captured",
                "data": {
                    "status": "captured",
                    "path": str(screenshot),
                    "filename": screenshot.name,
                    "content_type": "image/png",
                    "size_bytes": 3,
                },
            }

    runner = ScreenshotRunner()
    settings = WorkerSettings(
        worker_id="worker-test",
        token="test-token",
        workspace_root=tmp_path,
        trae_exe_path=tmp_path / "Trae.exe",
    )

    processed = worker_main.run_once(client=client, runner=runner, worker_settings=settings)

    assert processed == 1
    assert client.results[0]["data"]["upload_status"] == "uploaded"
    assert client.results[0]["data"]["server_attachment"]["id"] == "att1"
    assert client.results[0]["data"]["server_attachment"]["job_id"] == "job1"
