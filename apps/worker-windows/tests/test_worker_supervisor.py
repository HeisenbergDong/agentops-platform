import sys
import threading
from pathlib import Path

from worker.config import WorkerSettings, save_worker_settings
from worker.runtime import supervisor
from worker.runtime.supervisor import RotatingLogWriter, SupervisorOptions


def test_supervisor_builds_source_worker_command(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "running_from_frozen_exe", lambda: False)
    command = supervisor.build_worker_command(tmp_path / "worker.json")

    assert command[0] == sys.executable
    assert command[1:4] == ["-m", "worker.main", "run"]
    assert command[4:] == ["--config", str(tmp_path / "worker.json")]


def test_rotating_log_writer_keeps_bounded_backups(tmp_path):
    log_path = tmp_path / "agentops-worker.log"
    writer = RotatingLogWriter(log_path, max_bytes=10, backups=2)

    writer.write("first-line\n")
    writer.write("second-line\n")
    writer.write("third-line\n")

    assert log_path.exists()
    assert (tmp_path / "agentops-worker.log.1").exists()
    assert (tmp_path / "agentops-worker.log.2").exists()
    assert not (tmp_path / "agentops-worker.log.3").exists()


def test_supervisor_refuses_unregistered_config(monkeypatch, tmp_path):
    config_path = tmp_path / "worker.json"
    save_worker_settings(
        WorkerSettings(server_url="http://server", worker_id="worker-1", token="change-me-worker-token"),
        config_path,
    )
    started = []

    def fake_run_child(command, writer, stop_event):
        started.append(command)
        return 0

    monkeypatch.setattr(supervisor, "_run_child", fake_run_child)

    code = supervisor.run_supervisor(
        SupervisorOptions(
            config_path=config_path,
            log_dir=tmp_path / "logs",
            pid_file=tmp_path / "worker.pid",
        ),
        stop_event=threading.Event(),
    )

    assert code == 2
    assert started == []
    log_text = (tmp_path / "logs" / "agentops-worker.log").read_text(encoding="utf-8")
    assert "Worker is not registered" in log_text


def test_supervisor_restarts_failed_child_then_stops(monkeypatch, tmp_path):
    config_path = tmp_path / "worker.json"
    save_worker_settings(
        WorkerSettings(server_url="http://server", worker_id="worker-1", token="token-1"),
        config_path,
    )
    exits = iter([1, 0])
    launches = []

    def fake_run_child(command, writer, stop_event):
        launches.append(command)
        return next(exits)

    monkeypatch.setattr(supervisor, "_run_child", fake_run_child)

    code = supervisor.run_supervisor(
        SupervisorOptions(
            config_path=config_path,
            log_dir=tmp_path / "logs",
            restart_delay_seconds=0,
            pid_file=tmp_path / "worker.pid",
        ),
        stop_event=threading.Event(),
    )

    assert code == 0
    assert len(launches) == 2
