from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import RoleTemplate, User, UserRole
from app.db.repositories.roles import (
    get_user_role,
    list_role_templates,
    list_user_roles,
    update_user_role,
)
from app.db.repositories.user_rules import read_user_rule_many
from app.db.session import get_db

router = APIRouter()


class RoleChatRequest(BaseModel):
    message: str
    mode: str = "temporary_instruction"


class RoleUpdateRequest(BaseModel):
    name: str | None = None
    purpose: str | None = None
    rules: list[str] | None = None
    enabled: bool | None = None
    model_config_key: str | None = None
    config: dict | None = None


@router.get("")
def list_roles(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [serialize_user_role(item) for item in list_user_roles(db, user.id)]


@router.get("/templates")
def list_templates(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [serialize_role_template(item) for item in list_role_templates(db)]


@router.get("/{role_key}/capabilities")
def role_capabilities(
    role_key: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    role = get_user_role(db, user.id, role_key)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    try:
        rules = read_user_rule_many(db, user.id, role.rules)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Rule not found: {exc.args[0]}") from None
    return {"role": serialize_user_role(role), "rules": rules}


@router.patch("/{role_key}")
def update_role(
    role_key: str,
    payload: RoleUpdateRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    role = get_user_role(db, user.id, role_key)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    values = payload.model_dump(exclude_unset=True)
    if "rules" in values:
        values["rules"] = [item.strip() for item in values["rules"] if item.strip()]
    return serialize_user_role(update_user_role(db, role, values))


@router.post("/{role_key}/chat")
def role_chat(
    role_key: str,
    payload: RoleChatRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    role = get_user_role(db, user.id, role_key)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return {
        "role": role.role_key,
        "mode": payload.mode,
        "message": payload.message,
        "proposal": "Role runtime is scaffolded; LLM execution will be implemented next.",
    }


def serialize_role_template(item: RoleTemplate) -> dict:
    return {
        "id": item.id,
        "key": item.role_key,
        "name": item.name,
        "purpose": item.purpose,
        "rules": item.rules,
        "enabled": item.enabled,
        "model_config_key": item.model_config_key,
        "config": item.config,
    }


def serialize_user_role(item: UserRole) -> dict:
    return {
        "id": item.id,
        "template_id": item.template_id,
        "key": item.role_key,
        "name": item.name,
        "purpose": item.purpose,
        "rules": item.rules,
        "enabled": item.enabled,
        "model_config_key": item.model_config_key,
        "config": item.config,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }
