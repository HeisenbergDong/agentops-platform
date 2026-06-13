from pathlib import Path

from worker.config import WorkerSettings
from worker import main as worker_main


class FakeClient:
    def __init__(self, commands, ack_response, heartbeat_response=None):
        self.commands = commands
        self.ack_response = ack_response
        self.heartbeat_response = heartbeat_response or {"status": "ok"}
        self.results = []
        self.logs = []

    def heartbeat(self, payload):
        return self.heartbeat_response

    def poll_commands(self, worker_id):
        return self.commands

    def ack_command(self, worker_id, command_id, lease_id=""):
        return self.ack_response

    def post_result(self, worker_id, payload):
        self.results.append(payload)
        return {"status": "received"}

    def post_log(self, worker_id, payload):
        self.logs.append(payload)
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
    command = {"command_id": "cmd1", "type": "wait_completion", "payload": {}, "lease_id": "claim-1"}
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


def test_wait_completion_recovery_event_is_info_and_carries_result(tmp_path: Path):
    command = {
        "command_id": "cmd1",
        "job_id": "job1",
        "round_id": "round1",
        "type": "wait_completion",
        "lease_id": "claim-1",
        "payload": {},
    }
    client = FakeClient(commands=[command], ack_response={**command, "status": "running", "lease_id": "run-1"})

    class WaitingRunner(FakeRunner):
        def run(self, command):
            self.ran = True
            return {
                "command_id": command["command_id"],
                "worker_id": "worker-test",
                "status": "failed",
                "message": "Command failed",
                "data": {"output_probe": {"reason": "trace_too_short"}},
                "error": "not complete",
            }

    settings = WorkerSettings(
        worker_id="worker-test",
        token="test-token",
        workspace_root=tmp_path,
        trae_exe_path=tmp_path / "Trae.exe",
    )

    processed = worker_main.run_once(client=client, runner=WaitingRunner(), worker_settings=settings)

    assert processed == 1
    finished = client.logs[-1]
    assert finished["stage"] == "worker_command_finished"
    assert finished["level"] == "info"
    assert finished["extra"]["result_status"] == "failed"
    assert finished["extra"]["result"]["output_probe"]["reason"] == "trace_too_short"


def test_run_once_uploads_screenshot_before_posting_result(tmp_path: Path):
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"png")
    command = {
        "command_id": "cmd1",
        "job_id": "job1",
        "round_id": "round1",
        "type": "capture_screenshot",
        "lease_id": "claim-1",
        "payload": {},
    }
    client = FakeClient(commands=[command], ack_response={**command, "status": "running", "lease_id": "run-1"})

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
    assert client.results[0]["lease_id"] == "run-1"


def test_run_once_applies_assigned_config_from_heartbeat(tmp_path: Path):
    assigned_root = tmp_path / "assigned-root"
    client = FakeClient(
        commands=[],
        ack_response={},
        heartbeat_response={
            "status": "ok",
            "assigned_config": {
                "trae_workspace_path": str(assigned_root),
                "browser_url": "http://localhost:5173",
            },
        },
    )
    runner = FakeRunner()
    settings = WorkerSettings(
        worker_id="worker-test",
        token="test-token",
        workspace_root=tmp_path / "old-root",
        trae_exe_path=tmp_path / "Trae.exe",
    )

    processed = worker_main.run_once(client=client, runner=runner, worker_settings=settings)

    assert processed == 0
    assert settings.workspace_root == assigned_root
    assert settings.browser_url == "http://localhost:5173"
    assert runner.settings is settings


def test_run_forever_does_not_auto_launch_trae_on_startup(monkeypatch, tmp_path):
    settings = worker_main.WorkerSettings(
        server_url="http://server",
        token="token",
        worker_id="worker1",
        trae_exe_path=tmp_path / "Trae.exe",
        workspace_root=tmp_path,
        auto_launch_trae_on_startup=True,
        poll_interval_seconds=0,
    )
    calls = {"launch": 0, "run_once": 0}

    class FakeClient:
        def __init__(self, server_url, token):
            self.server_url = server_url
            self.token = token

    class FakeRunner:
        state = type(
            "State",
            (),
            {
                "stage": "idle",
                "current_window_title": "",
                "busy": False,
                "current_lease_id": "",
            },
        )()
        cancellation_checker = None

    def fake_run_once(client, runner, worker_settings):
        calls["run_once"] += 1
        raise KeyboardInterrupt

    monkeypatch.setattr(worker_main, "WorkerClient", FakeClient)
    monkeypatch.setattr(worker_main, "create_command_runner", lambda worker_settings: FakeRunner())
    monkeypatch.setattr(worker_main, "attach_cancellation_checker", lambda runner, client, worker_settings: None)
    monkeypatch.setattr(worker_main, "try_auto_launch_trae", lambda runner: calls.__setitem__("launch", calls["launch"] + 1))
    monkeypatch.setattr(worker_main, "run_once", fake_run_once)

    worker_main.run_forever(settings)

    assert calls == {"launch": 0, "run_once": 1}


def test_run_once_skips_stale_lease_after_ack(tmp_path: Path):
    command = {"command_id": "cmd1", "type": "wait_completion", "payload": {}, "lease_id": "claim-1"}
    client = FakeClient(commands=[command], ack_response={**command, "status": "stale_lease"})
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
    assert client.results == []
