from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import serialize_user
from app.api.deps import require_admin
from app.core.security import hash_password
from app.db.models import User
from app.db.repositories.users import get_user_by_email, list_users
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
