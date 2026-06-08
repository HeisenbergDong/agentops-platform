from functools import cached_property
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_secret_key: str = "change-me"
    access_token_ttl_seconds: int = 60 * 60 * 24 * 7
    bootstrap_admin_email: str = "admin@agentops.local"
    bootstrap_admin_password: str = "agentops-admin"
    bootstrap_admin_name: str = "AgentOps Admin"
    database_url: str = "postgresql+psycopg://agentops:agentops_dev_password_change_me@localhost:5432/agentops"
    redis_url: str = "redis://localhost:6379/0"
    attachment_root: Path = Field(default=Path("./storage"))
    cors_origins_raw: str = Field(default="http://localhost:5173", alias="CORS_ORIGINS")
    worker_token_dev: str = "change-me-worker-token"

    @cached_property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins_raw.split(",") if item.strip()]

    @cached_property
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parents[4]

    @cached_property
    def rules_dir(self) -> Path:
        return self.repo_root / "rules"


settings = Settings()
