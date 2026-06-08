from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import RoleTemplate, User
from app.db.repositories.roles import get_role_template, list_role_templates
from app.db.session import get_db
from app.services.rules.loader import RuleLoader

router = APIRouter()


class RoleChatRequest(BaseModel):
    message: str
    mode: str = "temporary_instruction"


@router.get("")
def list_roles(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [serialize_role_template(item) for item in list_role_templates(db)]


@router.get("/{role_key}/capabilities")
def role_capabilities(
    role_key: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    role = get_role_template(db, role_key)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    rules = RuleLoader().read_many(role.rules)
    return {"role": serialize_role_template(role), "rules": rules}


@router.post("/{role_key}/chat")
def role_chat(
    role_key: str,
    payload: RoleChatRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    role = get_role_template(db, role_key)
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
