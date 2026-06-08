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
from app.services.llm import LLMClient, LLMError, model_config_from_settings
from app.services.user_settings import load_user_settings

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
    mode = (
        payload.mode
        if payload.mode in {"record_only", "llm_reply", "append_rule_note", "llm_append_rule_note"}
        else "record_only"
    )
    target_rule = payload.target_rule.strip() if payload.target_rule else ""

    action: dict = {"type": mode, "status": "recorded"}
    assistant_message = "已记录这次补充。"
    llm_result = None
    if mode in {"llm_reply", "llm_append_rule_note"}:
        try:
            history = list_role_chat_messages(db, user.id, role.role_key, limit=12)
            llm_result = LLMClient().complete(
                model_config_from_settings(load_user_settings(db, user.id), role.model_config_key),
                build_role_messages(role, message, mode, target_rule, db, user.id, history),
            )
        except LLMError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Rule not found: {exc.args[0]}") from None
        assistant_message = llm_result.text
        action = {
            "type": mode,
            "status": "generated",
            "model": llm_result.model,
            "wire_api": llm_result.wire_api,
        }

    if mode in {"append_rule_note", "llm_append_rule_note"}:
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
        note = llm_result.text if llm_result else message
        append_user_rule_note(db, rule_file, note)
        action.update({"status": "applied", "target_rule": target_rule})
        assistant_message = f"已追加到规则文件：{target_rule}"
        if llm_result:
            assistant_message = f"已生成规则补充并追加到：{target_rule}\n\n{llm_result.text}"

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


def build_role_messages(
    role: UserRole,
    message: str,
    mode: str,
    target_rule: str,
    db: Session,
    user_id: str,
    history: list[RoleChatMessage],
) -> list[dict[str, str]]:
    rules = read_user_rule_many(db, user_id, role.rules)
    rule_text = "\n\n".join([f"## {name}\n{content}" for name, content in rules.items()])
    if mode == "llm_append_rule_note":
        user_prompt = (
            "用户希望给这个角色补充能力。请把用户输入整理成可以追加到规则文件的规则段落。"
            "要求：只输出规则正文；使用中文；保留具体约束；不要编造不存在的需求。\n\n"
            f"目标规则文件：{target_rule or '未指定'}\n"
            f"用户输入：\n{message}"
        )
    else:
        user_prompt = message
    messages = [
        {
            "role": "system",
            "content": (
                f"你是 AgentOps 平台中的角色：{role.name}。\n"
                f"职责：{role.purpose}\n"
                "你必须遵守当前用户的角色规则。回答要简洁、可执行；涉及规则修改时只给明确建议。\n\n"
                f"当前绑定规则：\n{rule_text}"
            ),
        },
    ]
    for item in history:
        if item.sender not in {"user", "assistant"}:
            continue
        messages.append({"role": item.sender, "content": item.message})
    messages.append({"role": "user", "content": user_prompt})
    return messages
