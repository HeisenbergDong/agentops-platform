from fastapi import Header, HTTPException

from app.core.config import settings


def require_worker_token(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {settings.worker_token_dev}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid worker token")
