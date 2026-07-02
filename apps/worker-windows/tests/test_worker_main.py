from pathlib import Path
import time

from worker.config import WorkerSettings
from worker import main as worker_main
from worker.capabilities import WORKER_RUNTIME_VERSION


class FakeClient:
    def __init__(self, commands, ack_response, heartbeat_response=None):
        self.commands = commands
        self.ack_response = ack_response
        self.heartbeat_response = heartbeat_response or {"status": "ok"}
        self.heartbeats = []
        self.results = []
        self.logs = []
        self.command_reads = []

    def heartbeat(self, payload):
        self.heartbeats.append(payload)
        return self.heartbeat_response

    def poll_commands(self, worker_id):
        return self.commands

    def ack_command(self, worker_id, command_id, lease_id=""):
        return self.ack_response

    def get_command(self, worker_id, command_id, lease_id=""):
        self.command_reads.append({"worker_id": worker_id, "command_id": command_id, "lease_id": lease_id})
        return {**self.ack_response, "status": "running", "lease_id": lease_id or self.ack_response.get("lease_id", "")}

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


class FailingResultClient(FakeClient):
    def __init__(self, commands, ack_response, failures: int = 1):
        super().__init__(commands=commands, ack_response=ack_response)
        self.failures = failures

    def post_result(self, worker_id, payload):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("network down")
        return super().post_result(worker_id, payload)


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

    def _cancelled_stop_data(self, payload):
        return {
            "stopped": True,
            "message": "Worker stop completed.",
            "stop_report": {
                "worker_command_cancelled": True,
                "stop_confirmed": True,
                "cleanup_status": "no_matching_processes",
                "trae_stop_clicked": False,
            },
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


def test_run_once_heartbeat_reports_runtime_version(tmp_path: Path):
    client = FakeClient(commands=[], ack_response={})
    trae_exe = tmp_path / "Trae.exe"
    trae_exe.write_text("fake exe", encoding="utf-8")
    settings = WorkerSettings(
        worker_id="worker-test",
        token="test-token",
        workspace_root=tmp_path,
        trae_exe_path=trae_exe,
        version="0.1.0",
    )

    processed = worker_main.run_once(client=client, runner=FakeRunner(), worker_settings=settings)

    assert processed == 0
    assert client.heartbeats[0]["version"] == WORKER_RUNTIME_VERSION
    assert client.heartbeats[0]["config_version"] == "0.1.0"
    assert "trae_workspace_title_fallback" in client.heartbeats[0]["capabilities"]
    assert client.heartbeats[0]["runtime_status"]["trae_exe_exists"] is True
    assert client.heartbeats[0]["runtime_status"]["trae_exe_resolved_path"] == str(trae_exe)


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


def test_run_once_renews_running_command_lease(monkeypatch, tmp_path: Path):
    command = {
        "command_id": "cmd1",
        "job_id": "job1",
        "round_id": "round1",
        "type": "wait_completion",
        "lease_id": "claim-1",
        "payload": {},
    }
    client = FakeClient(commands=[command], ack_response={**command, "status": "running", "lease_id": "run-1"})
    monkeypatch.setattr(worker_main, "LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(worker_main, "COMMAND_HEARTBEAT_INTERVAL_SECONDS", 0.01)

    class SlowRunner(FakeRunner):
        def run(self, command):
            self.ran = True
            self.state.busy = True
            self.state.stage = "wait_completion"
            time.sleep(0.05)
            return {
                "command_id": command["command_id"],
                "worker_id": "worker-test",
                "status": "success",
                "message": "completed",
                "data": {},
            }

    settings = WorkerSettings(
        worker_id="worker-test",
        token="test-token",
        workspace_root=tmp_path,
        trae_exe_path=tmp_path / "Trae.exe",
    )

    processed = worker_main.run_once(client=client, runner=SlowRunner(), worker_settings=settings)

    assert processed == 1
    assert client.command_reads
    assert client.command_reads[0]["lease_id"] == "run-1"
    active_heartbeats = [
        item for item in client.heartbeats if item.get("runtime_status", {}).get("active_command", {}).get("command_id") == "cmd1"
    ]
    assert active_heartbeats
    assert active_heartbeats[-1]["busy"] is True
    assert active_heartbeats[-1]["runtime_status"]["active_command"]["type"] == "wait_completion"
    assert client.results[0]["status"] == "success"


def test_run_once_saves_result_to_outbox_when_post_result_fails(monkeypatch, tmp_path: Path):
    command = {"command_id": "cmd1", "type": "wait_completion", "payload": {}, "lease_id": "claim-1"}
    client = FailingResultClient(commands=[command], ack_response={**command, "status": "running", "lease_id": "run-1"}, failures=4)
    outbox_dir = tmp_path / "outbox"
    result_outbox_class = worker_main.ResultOutbox
    monkeypatch.setattr(worker_main, "ResultOutbox", lambda: result_outbox_class(outbox_dir, retry_delays=()))

    settings = WorkerSettings(
        worker_id="worker-test",
        token="test-token",
        workspace_root=tmp_path,
        trae_exe_path=tmp_path / "Trae.exe",
    )

    processed = worker_main.run_once(client=client, runner=FakeRunner(), worker_settings=settings)

    saved = list(outbox_dir.glob("*.json"))
    assert processed == 1
    assert client.results == []
    assert len(saved) == 1
    assert '"command_id": "cmd1"' in saved[0].read_text(encoding="utf-8")


def test_run_once_flushes_saved_result_outbox(monkeypatch, tmp_path: Path):
    command = {"command_id": "cmd1", "type": "wait_completion", "payload": {}, "lease_id": "claim-1"}
    outbox_dir = tmp_path / "outbox"
    result_outbox_class = worker_main.ResultOutbox
    outbox = result_outbox_class(outbox_dir)
    outbox.save(
        "worker-test",
        {
            "command_id": "old-cmd",
            "worker_id": "worker-test",
            "lease_id": "old-run",
            "status": "manual_required",
            "message": "saved",
            "data": {"reason": "service_interrupted"},
        },
    )
    client = FakeClient(commands=[command], ack_response={**command, "status": "running", "lease_id": "run-1"})
    monkeypatch.setattr(worker_main, "ResultOutbox", lambda: result_outbox_class(outbox_dir))

    settings = WorkerSettings(
        worker_id="worker-test",
        token="test-token",
        workspace_root=tmp_path,
        trae_exe_path=tmp_path / "Trae.exe",
    )

    processed = worker_main.run_once(client=client, runner=FakeRunner(), worker_settings=settings)

    assert processed == 1
    assert [item["command_id"] for item in client.results] == ["old-cmd", "cmd1"]
    assert list(outbox_dir.glob("*.json")) == []


def test_run_once_converts_success_to_cancelled_stop_when_server_cancelled_after_run(tmp_path: Path):
    command = {
        "command_id": "cmd1",
        "job_id": "job1",
        "round_id": "round1",
        "type": "send_prompt",
        "lease_id": "claim-1",
        "payload": {"workspace_path": str(tmp_path / "project")},
    }
    client = FakeClient(commands=[command], ack_response={**command, "status": "running", "lease_id": "run-1"})

    def cancelled_get_command(worker_id, command_id, lease_id=""):
        client.command_reads.append({"worker_id": worker_id, "command_id": command_id, "lease_id": lease_id})
        return {**command, "status": "cancelled", "lease_id": lease_id}

    client.get_command = cancelled_get_command
    runner = FakeRunner()
    settings = WorkerSettings(
        worker_id="worker-test",
        token="test-token",
        workspace_root=tmp_path,
        trae_exe_path=tmp_path / "Trae.exe",
    )

    processed = worker_main.run_once(client=client, runner=runner, worker_settings=settings)

    assert processed == 1
    assert runner.ran is True
    assert client.results[0]["status"] == "cancelled"
    assert client.results[0]["data"]["stop_report"]["stop_confirmed"] is True
    assert client.results[0]["data"]["stop_report"]["trae_stop_clicked"] is False
    assert client.results[0]["lease_id"] == "run-1"


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


def test_run_once_uploads_wait_completion_diagnostic_screenshot(tmp_path: Path):
    screenshot = tmp_path / "diagnostic.png"
    screenshot.write_bytes(b"png")
    command = {
        "command_id": "cmd1",
        "job_id": "job1",
        "round_id": "round1",
        "type": "wait_completion",
        "lease_id": "claim-1",
        "payload": {},
    }
    client = FakeClient(commands=[command], ack_response={**command, "status": "running", "lease_id": "run-1"})

    class WaitRunner(FakeRunner):
        def run(self, command):
            self.ran = True
            return {
                "command_id": command["command_id"],
                "worker_id": "worker-test",
                "status": "success",
                "message": "completed",
                "data": {
                    "status": "completed",
                    "supervisor_decision": {
                        "action": "collect_trace",
                        "reason": "visual_completion_detected",
                        "diagnosis": {
                            "visual": {
                                "screenshot": {
                                    "path": str(screenshot),
                                    "filename": screenshot.name,
                                    "content_type": "image/png",
                                }
                            }
                        },
                    },
                },
            }

    settings = WorkerSettings(
        worker_id="worker-test",
        token="test-token",
        workspace_root=tmp_path,
        trae_exe_path=tmp_path / "Trae.exe",
    )

    processed = worker_main.run_once(client=client, runner=WaitRunner(), worker_settings=settings)

    assert processed == 1
    data = client.results[0]["data"]
    assert data["diagnostic_upload_status"] == "uploaded"
    assert data["diagnostic_server_attachment"]["kind"] == "diagnostic_screenshot"
    assert data["supervisor_decision"]["diagnosis"]["visual"]["screenshot"]["server_attachment"]["id"] == "att1"


def test_run_once_applies_assigned_config_from_heartbeat(tmp_path: Path):
    assigned_root = tmp_path / "assigned-root"
    assigned_exe = tmp_path / "Trae CN.exe"
    client = FakeClient(
        commands=[],
        ack_response={},
        heartbeat_response={
            "status": "ok",
            "assigned_config": {
                "trae_workspace_path": str(assigned_root),
                "trae_exe_path": str(assigned_exe),
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
    assert settings.trae_exe_path == assigned_exe
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
