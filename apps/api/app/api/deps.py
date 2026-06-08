from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import decode_access_token, hash_worker_secret
from app.db.models import User, Worker
from app.db.repositories.users import get_user_by_id
from app.db.session import get_db

bearer = HTTPBearer(auto_error=False)


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = decode_access_token(credentials.credentials)
    user_id = str(payload.get("sub", ""))
    user = get_user_by_id(db, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Inactive or missing user")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin permission required")
    return user


def current_worker(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Worker:
    if not credentials:
        raise HTTPException(status_code=401, detail="Worker token required")
    token_hash = hash_worker_secret(credentials.credentials)
    worker = db.scalar(
        select(Worker).where(
            Worker.token_hash == token_hash,
            Worker.revoked_at.is_(None),
        )
    )
    if not worker:
        raise HTTPException(status_code=401, detail="Invalid worker token")
    return worker
