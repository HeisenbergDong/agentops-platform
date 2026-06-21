import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


WORKER_VERSION = "0.1.13-stop-session-browser"


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTOPS_WORKER_", extra="ignore")

    server_url: str = "http://localhost:8000"
    token: str = "change-me-worker-token"
    worker_id: str = "local-windows-worker"
    display_name: str = ""
    worker_type: str = "windows_trae"
    version: str = WORKER_VERSION
    trae_exe_path: Path = Path(r"D:\app\Trae CN\Trae CN.exe")
    workspace_root: Path = Path(r"D:\code-space\coding-soler")
    browser_url: str = ""
    poll_interval_seconds: float = 3.0
    auto_launch_trae_on_startup: bool = False
    keep_trae_foreground: bool = True


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
    data.pop("version", None)
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


def apply_assigned_config(
    worker_settings: WorkerSettings,
    assigned_config: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Apply server-assigned runtime config without persisting local registration secrets."""
    if not isinstance(assigned_config, Mapping):
        return {}

    changes: dict[str, str] = {}
    workspace_value = _first_non_empty(assigned_config, "trae_workspace_path", "workspace_root")
    if workspace_value is not None:
        workspace_root = Path(str(workspace_value)).expanduser()
        if worker_settings.workspace_root != workspace_root:
            worker_settings.workspace_root = workspace_root
            changes["workspace_root"] = str(workspace_root)

    trae_exe_value = _first_non_empty(assigned_config, "trae_exe_path")
    if trae_exe_value is not None:
        trae_exe_path = Path(str(trae_exe_value)).expanduser()
        if worker_settings.trae_exe_path != trae_exe_path:
            worker_settings.trae_exe_path = trae_exe_path
            changes["trae_exe_path"] = str(trae_exe_path)

    if "browser_url" in assigned_config:
        browser_url = str(assigned_config.get("browser_url") or "").strip()
        if worker_settings.browser_url != browser_url:
            worker_settings.browser_url = browser_url
            changes["browser_url"] = browser_url

    return changes


def running_from_frozen_exe() -> bool:
    return bool(getattr(sys, "frozen", False))


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Worker config is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Worker config must be a JSON object: {path}")
    return data


def _first_non_empty(data: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = data.get(key)
        if str(value or "").strip():
            return value
    return None


settings = load_worker_settings()
