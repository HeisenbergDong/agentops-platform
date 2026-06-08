from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import RoleChatMessage, RoleTemplate, User, UserRole
from app.db.repositories.role_chats import add_role_chat_message, list_role_chat_messages
from app.db.repositories.roles import (
    get_user_role,
    list_role_templates,
    list_user_roles,
    update_user_role,
)
from app.db.repositories.user_rules import append_user_rule_note, get_user_rule_file, read_user_rule_many
from app.db.session import get_db

router = APIRouter()


class RoleChatRequest(BaseModel):
    message: str
    mode: str = "record_only"
    target_rule: str | None = None


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


@router.get("/{role_key}/chat")
def role_chat_history(
    role_key: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    role = get_user_role(db, user.id, role_key)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return {
        "role": serialize_user_role(role),
        "messages": [
            serialize_role_chat_message(item) for item in list_role_chat_messages(db, user.id, role.role_key)
        ],
    }


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
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")
    mode = payload.mode if payload.mode in {"record_only", "append_rule_note"} else "record_only"
    target_rule = payload.target_rule.strip() if payload.target_rule else ""

    action: dict = {"type": mode, "status": "recorded"}
    assistant_message = "已记录这次补充。"
    if mode == "append_rule_note":
        available_rules = [item.strip() for item in role.rules if item.strip()]
        if not available_rules:
            raise HTTPException(status_code=400, detail="Role has no bound rules")
        target_rule = target_rule or available_rules[0]
        if target_rule not in available_rules:
            raise HTTPException(status_code=400, detail="Target rule is not bound to this role")
        try:
            rule_file = get_user_rule_file(db, user.id, target_rule)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not rule_file:
            raise HTTPException(status_code=404, detail="Rule not found")
        append_user_rule_note(db, rule_file, message)
        action = {"type": mode, "status": "applied", "target_rule": target_rule}
        assistant_message = f"已追加到规则文件：{target_rule}"

    add_role_chat_message(
        db,
        user.id,
        role.role_key,
        sender="user",
        message=message,
        mode=mode,
        target_rule=target_rule,
    )
    assistant = add_role_chat_message(
        db,
        user.id,
        role.role_key,
        sender="assistant",
        message=assistant_message,
        mode=mode,
        target_rule=target_rule,
        action=action,
    )
    return {
        "role": serialize_user_role(role),
        "assistant": serialize_role_chat_message(assistant),
        "messages": [
            serialize_role_chat_message(item) for item in list_role_chat_messages(db, user.id, role.role_key)
        ],
        "action": action,
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


def serialize_role_chat_message(item: RoleChatMessage) -> dict:
    return {
        "id": item.id,
        "role_key": item.role_key,
        "sender": item.sender,
        "message": item.message,
        "mode": item.mode,
        "target_rule": item.target_rule,
        "action": item.action,
        "created_at": item.created_at.isoformat(),
    }
