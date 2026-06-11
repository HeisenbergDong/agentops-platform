import json

from worker.config import WorkerSettings
from worker import registration
from worker import main as worker_main
from worker.registration import RegistrationOptions, is_registered, normalize_server_url, register_worker


def test_normalize_server_url_defaults_to_http():
    assert normalize_server_url("115.190.113.8/") == "http://115.190.113.8"
    assert normalize_server_url("https://example.test/") == "https://example.test"


def test_register_worker_persists_server_token_and_worker_id(monkeypatch, tmp_path):
    captured = {}

    class FakeClient:
        def __init__(self, server_url: str, token: str) -> None:
            captured["server_url"] = server_url
            captured["token"] = token

        def register_worker(self, payload: dict) -> dict:
            captured["payload"] = payload
            return {"worker_id": "worker-registered", "worker_token": "token-registered"}

    monkeypatch.setattr(registration, "WorkerClient", FakeClient)
    config_path = tmp_path / "worker.json"

    worker_settings, saved_path, _response = register_worker(
        RegistrationOptions(
            server_url="115.190.113.8",
            registration_code="reg-code",
            display_name="Build Box",
            config_path=config_path,
            workspace_root=tmp_path,
        )
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved_path == config_path
    assert captured["server_url"] == "http://115.190.113.8"
    assert captured["token"] == ""
    assert captured["payload"]["registration_code"] == "reg-code"
    assert captured["payload"]["display_name"] == "Build Box"
    assert "send_prompt" in captured["payload"]["capabilities"]
    assert worker_settings.worker_id == "worker-registered"
    assert worker_settings.token == "token-registered"
    assert data["server_url"] == "http://115.190.113.8"
    assert data["worker_id"] == "worker-registered"
    assert data["token"] == "token-registered"


def test_is_registered_rejects_default_token():
    assert not is_registered(WorkerSettings())
    assert is_registered(
        WorkerSettings(server_url="http://server", worker_id="worker-1", token="token-1")
    )


def test_run_once_posts_worker_process_events(tmp_path):
    events = []
    posted_results = []

    class FakeClient:
        def heartbeat(self, payload: dict) -> dict:
            return {"status": "ok"}

        def poll_commands(self, worker_id: str) -> list[dict]:
            return [
                {
                    "command_id": "cmd-1",
                    "job_id": "job-1",
                    "round_id": "round-1",
                    "type": "send_prompt",
                    "payload": {"prompt": "hello", "trae_workspace_path": str(tmp_path)},
                }
            ]

        def ack_command(self, worker_id: str, command_id: str) -> dict:
            return {"status": "claimed"}

        def post_log(self, worker_id: str, payload: dict) -> dict:
            events.append(payload)
            return {"status": "received"}

        def post_result(self, worker_id: str, payload: dict) -> dict:
            posted_results.append(payload)
            return {"status": "received"}

    class FakeState:
        stage = "idle"
        current_window_title = ""
        busy = False

    class FakeRunner:
        state = FakeState()

        def run(self, command: dict) -> dict:
            return {
                "command_id": command["command_id"],
                "worker_id": "worker-1",
                "status": "success",
                "message": "Command processed",
                "data": {},
            }

    processed = worker_main.run_once(
        client=FakeClient(),
        runner=FakeRunner(),
        worker_settings=WorkerSettings(
            server_url="http://server",
            worker_id="worker-1",
            token="token-1",
            workspace_root=tmp_path,
        ),
    )

    assert processed == 1
    assert [item["stage"] for item in events] == ["worker_command_started", "worker_command_finished"]
    assert events[0]["job_id"] == "job-1"
    assert events[0]["extra"]["command_type"] == "send_prompt"
    assert posted_results[0]["status"] == "success"
