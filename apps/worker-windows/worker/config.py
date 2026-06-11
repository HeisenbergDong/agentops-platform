import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTOPS_WORKER_", extra="ignore")

    server_url: str = "http://localhost:8000"
    token: str = "change-me-worker-token"
    worker_id: str = "local-windows-worker"
    display_name: str = ""
    worker_type: str = "windows_trae"
    version: str = "0.1.0"
    trae_exe_path: Path = Path(r"D:\app\Trae CN\Trae CN.exe")
    workspace_root: Path = Path(r"D:\code-space\coding-soler")
    poll_interval_seconds: float = 3.0
    auto_launch_trae_on_startup: bool = True


def default_config_path() -> Path:
    configured = os.environ.get("AGENTOPS_WORKER_CONFIG")
    if configured:
        return Path(configured).expanduser()
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "AgentOps" / "worker.json"
    return Path.home() / ".agentops" / "worker.json"


def load_worker_settings(config_path: Path | str | None = None) -> WorkerSettings:
    path = Path(config_path).expanduser() if config_path else default_config_path()
    data = _read_config_file(path)
    return WorkerSettings(**data)


def save_worker_settings(
    worker_settings: WorkerSettings,
    config_path: Path | str | None = None,
) -> Path:
    path = Path(config_path).expanduser() if config_path else default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = worker_settings.model_dump(mode="json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def running_from_frozen_exe() -> bool:
    return bool(getattr(sys, "frozen", False))


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Worker config is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Worker config must be a JSON object: {path}")
    return data


settings = load_worker_settings()
