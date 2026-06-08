from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import RoleChatMessage


def list_role_chat_messages(
    db: Session,
    user_id: str,
    role_key: str,
    limit: int = 50,
) -> list[RoleChatMessage]:
    rows = db.scalars(
        select(RoleChatMessage)
        .where(RoleChatMessage.user_id == user_id, RoleChatMessage.role_key == role_key)
        .order_by(desc(RoleChatMessage.created_at))
        .limit(limit)
    ).all()
    return list(reversed(rows))


def add_role_chat_message(
    db: Session,
    user_id: str,
    role_key: str,
    sender: str,
    message: str,
    mode: str = "record_only",
    target_rule: str = "",
    action: dict | None = None,
) -> RoleChatMessage:
    item = RoleChatMessage(
        user_id=user_id,
        role_key=role_key,
        sender=sender,
        message=message,
        mode=mode,
        target_rule=target_rule,
        action=action or {},
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item
