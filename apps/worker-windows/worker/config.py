from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTOPS_WORKER_", extra="ignore")

    server_url: str = "http://localhost:8000"
    token: str = "change-me-worker-token"
    worker_id: str = "local-windows-worker"
    trae_exe_path: Path = Path(r"D:\app\Trae CN\Trae CN.exe")
    workspace_root: Path = Path(r"D:\code-space\coding-soler")
    poll_interval_seconds: float = 3.0


settings = WorkerSettings()
