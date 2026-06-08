from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.security import create_access_token, verify_password
from app.db.models import User
from app.db.models.base import now_utc
from app.db.repositories.users import get_user_by_email
from app.db.session import get_db

router = APIRouter()


class LoginRequest(BaseModel):
    email: str
    password: str


def serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": user.is_active,
    }


@router.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict:
    user = get_user_by_email(db, payload.email)
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user.last_login_at = now_utc()
    db.commit()
    return {
        "access_token": create_access_token(user.id),
        "token_type": "bearer",
        "user": serialize_user(user),
    }


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return serialize_user(user)


@router.post("/logout")
def logout() -> dict:
    return {"status": "ok"}
