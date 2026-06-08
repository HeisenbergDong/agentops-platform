from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import serialize_user
from app.api.deps import require_admin
from app.core.security import hash_password
from app.db.models import User, WorkerRegistrationCode
from app.db.repositories.users import get_user_by_email, list_users
from app.db.repositories.workers import bind_worker, create_registration_code, list_registration_codes
from app.db.session import get_db

router = APIRouter()


class CreateUserRequest(BaseModel):
    email: str
    display_name: str
    password: str
    role: str = "user"


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    password: str | None = None
    role: str | None = None
    is_active: bool | None = None


class CreateWorkerRegistrationCodeRequest(BaseModel):
    assigned_user_id: str | None = None
    expires_minutes: int = 60


class BindWorkerRequest(BaseModel):
    user_id: str | None = None


@router.get("/users")
def admin_list_users(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[dict]:
    return [serialize_user(item) for item in list_users(db)]


@router.post("/users")
def admin_create_user(
    payload: CreateUserRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    email = payload.email.strip().lower()
    if payload.role not in {"admin", "user"}:
        raise HTTPException(status_code=400, detail="Invalid role")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if get_user_by_email(db, email):
        raise HTTPException(status_code=409, detail="Email already exists")
    user = User(
        email=email,
        display_name=payload.display_name.strip() or email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return serialize_user(user)


@router.post("/worker-registration-codes")
def admin_create_worker_registration_code(
    payload: CreateWorkerRegistrationCodeRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    assigned_user_id = payload.assigned_user_id
    if assigned_user_id and not db.get(User, assigned_user_id):
        raise HTTPException(status_code=404, detail="Assigned user not found")
    code, row = create_registration_code(
        db,
        created_by=admin.id,
        assigned_user_id=assigned_user_id,
        expires_minutes=max(5, min(payload.expires_minutes, 24 * 60)),
    )
    return {"registration_code": code, "record": serialize_registration_code(row)}


@router.get("/worker-registration-codes")
def admin_list_worker_registration_codes(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[dict]:
    return [serialize_registration_code(item) for item in list_registration_codes(db)]


@router.patch("/workers/{worker_id}/bind")
def admin_bind_worker(
    worker_id: str,
    payload: BindWorkerRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    if payload.user_id and not db.get(User, payload.user_id):
        raise HTTPException(status_code=404, detail="User not found")
    worker = bind_worker(db, worker_id=worker_id, user_id=payload.user_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return {
        "id": worker.id,
        "worker_id": worker.worker_id,
        "user_id": worker.user_id,
        "status": worker.status,
    }


def serialize_registration_code(item: WorkerRegistrationCode) -> dict:
    return {
        "id": item.id,
        "assigned_user_id": item.assigned_user_id,
        "expires_at": item.expires_at.isoformat(),
        "used_at": item.used_at.isoformat() if item.used_at else None,
        "used_by_worker_id": item.used_by_worker_id,
        "status": item.status,
        "created_at": item.created_at.isoformat(),
    }


@router.patch("/users/{user_id}")
def admin_update_user(
    user_id: str,
    payload: UpdateUserRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.role is not None:
        if payload.role not in {"admin", "user"}:
            raise HTTPException(status_code=400, detail="Invalid role")
        user.role = payload.role
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip() or user.email
    if payload.is_active is not None:
        if user.id == admin.id and not payload.is_active:
            raise HTTPException(status_code=400, detail="Cannot disable current admin")
        user.is_active = payload.is_active
    if payload.password:
        if len(payload.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        user.password_hash = hash_password(payload.password)
        user.auth_token_version += 1
    db.commit()
    db.refresh(user)
    return serialize_user(user)
