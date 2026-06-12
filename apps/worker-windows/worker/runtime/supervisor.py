from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from worker.config import default_config_path, load_worker_settings, running_from_frozen_exe
from worker.registration import is_registered

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 5
DEFAULT_RESTART_DELAY_SECONDS = 5.0


@dataclass
class SupervisorOptions:
    config_path: Path | None = None
    log_dir: Path | None = None
    restart_delay_seconds: float = DEFAULT_RESTART_DELAY_SECONDS
    max_restart_attempts: int = 0
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backups: int = DEFAULT_LOG_BACKUPS
    worker_command: list[str] | None = None
    pid_file: Path | None = None
    require_registered: bool = True


class RotatingLogWriter:
    def __init__(self, path: Path, *, max_bytes: int = DEFAULT_LOG_MAX_BYTES, backups: int = DEFAULT_LOG_BACKUPS) -> None:
        self.path = path
        self.max_bytes = max(0, int(max_bytes))
        self.backups = max(0, int(backups))
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, text: str) -> None:
        if not text:
            return
        data = text.encode("utf-8", errors="replace")
        with self._lock:
            self._rotate_if_needed(len(data))
            with self.path.open("ab") as handle:
                handle.write(data)

    def write_event(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.write(f"[{timestamp}] [supervisor] {message}\n")

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if self.max_bytes <= 0 or self.backups <= 0:
            return
        current_size = self.path.stat().st_size if self.path.exists() else 0
        if current_size == 0 or current_size + incoming_bytes <= self.max_bytes:
            return
        oldest = _backup_path(self.path, self.backups)
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backups - 1, 0, -1):
            source = _backup_path(self.path, index)
            target = _backup_path(self.path, index + 1)
            if source.exists():
                source.replace(target)
        if self.path.exists():
            self.path.replace(_backup_path(self.path, 1))


def run_supervisor(options: SupervisorOptions, stop_event: threading.Event | None = None) -> int:
    stop_event = stop_event or threading.Event()
    config_path = Path(options.config_path).expanduser() if options.config_path else default_config_path()
    log_dir = Path(options.log_dir).expanduser() if options.log_dir else default_log_dir()
    writer = RotatingLogWriter(
        log_dir / "agentops-worker.log",
        max_bytes=options.log_max_bytes,
        backups=options.log_backups,
    )
    pid_file = Path(options.pid_file).expanduser() if options.pid_file else default_pid_file()
    command = options.worker_command or build_worker_command(config_path)
    restart_attempts = 0

    try:
        _write_pid_file(pid_file)
        writer.write_event(f"Supervisor started with log_dir={log_dir}")
        if options.require_registered and not _registered_for_supervision(config_path, writer):
            return 2

        while not stop_event.is_set():
            writer.write_event(f"Starting worker command: {_format_command(command)}")
            exit_code = _run_child(command, writer, stop_event)
            if stop_event.is_set():
                writer.write_event("Supervisor stop requested.")
                return 0
            writer.write_event(f"Worker process exited with code {exit_code}.")
            if exit_code == 0:
                return 0

            restart_attempts += 1
            if options.max_restart_attempts > 0 and restart_attempts > options.max_restart_attempts:
                writer.write_event(
                    "Restart limit reached "
                    f"({options.max_restart_attempts}); leaving worker stopped."
                )
                return exit_code or 1

            delay = max(0.0, float(options.restart_delay_seconds))
            writer.write_event(f"Restarting worker in {delay:g}s.")
            if stop_event.wait(delay):
                writer.write_event("Supervisor stop requested during restart delay.")
                return 0
    finally:
        _remove_pid_file(pid_file)
    return 0


def build_worker_command(config_path: Path | None = None) -> list[str]:
    if running_from_frozen_exe():
        command = [sys.executable, "run"]
    else:
        command = [sys.executable, "-m", "worker.main", "run"]
    if config_path:
        command.extend(["--config", str(config_path)])
    return command


def default_log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "AgentOps" / "Worker" / "logs"
    return Path.home() / ".agentops" / "worker" / "logs"


def default_pid_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "AgentOps" / "Worker" / "agentops-worker-supervisor.pid"
    return Path.home() / ".agentops" / "worker" / "agentops-worker-supervisor.pid"


def _run_child(command: list[str], writer: RotatingLogWriter, stop_event: threading.Event) -> int:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    pump = threading.Thread(target=_pump_output, args=(process, writer), daemon=True)
    pump.start()
    while process.poll() is None:
        if stop_event.wait(0.5):
            _terminate_child(process, writer)
            break
    pump.join(timeout=5)
    return int(process.returncode or 0)


def _pump_output(process: subprocess.Popen[str], writer: RotatingLogWriter) -> None:
    if process.stdout is None:
        return
    try:
        for line in process.stdout:
            writer.write(line)
    finally:
        process.stdout.close()


def _terminate_child(process: subprocess.Popen[str], writer: RotatingLogWriter) -> None:
    if process.poll() is not None:
        return
    writer.write_event(f"Stopping worker process pid={process.pid}.")
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        writer.write_event(f"Worker process pid={process.pid} did not stop; killing it.")
        process.kill()
        process.wait(timeout=10)


def _registered_for_supervision(config_path: Path, writer: RotatingLogWriter) -> bool:
    try:
        worker_settings = load_worker_settings(config_path)
    except Exception as exc:
        writer.write_event(f"Cannot load worker config {config_path}: {exc}")
        return False
    if is_registered(worker_settings):
        return True
    writer.write_event(
        "Worker is not registered. Run `agentops-worker register --server-url ... "
        "--registration-code ...` before enabling supervision."
    )
    return False


def _write_pid_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")


def _remove_pid_file(path: Path) -> None:
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        return


def _format_command(command: list[str]) -> str:
    return " ".join(_quote_arg(item) for item in command)


def _quote_arg(value: str) -> str:
    if not value:
        return '""'
    if any(ch.isspace() for ch in value) or '"' in value:
        return '"' + value.replace('"', r'\"') + '"'
    return value


def _backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")
