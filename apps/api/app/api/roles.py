from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.roles.registry import ROLE_REGISTRY, role_by_key
from app.services.rules.loader import RuleLoader

router = APIRouter()


class RoleChatRequest(BaseModel):
    message: str
    mode: str = "temporary_instruction"


@router.get("")
def list_roles() -> list[dict]:
    return [role.__dict__ for role in ROLE_REGISTRY]


@router.get("/{role_key}/capabilities")
def role_capabilities(role_key: str) -> dict:
    role = role_by_key(role_key)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    rules = RuleLoader().read_many(role.rules)
    return {"role": role.__dict__, "rules": rules}


@router.post("/{role_key}/chat")
def role_chat(role_key: str, payload: RoleChatRequest) -> dict:
    role = role_by_key(role_key)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return {
        "role": role.key,
        "mode": payload.mode,
        "message": payload.message,
        "proposal": "Role runtime is scaffolded; LLM execution will be implemented next.",
    }
