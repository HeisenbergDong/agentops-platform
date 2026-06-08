from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User, UserConfig


def get_user_by_id(db: Session, user_id: str) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.strip().lower()))


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).order_by(User.created_at.desc())).all())


def get_user_config(db: Session, user_id: str, category: str) -> UserConfig | None:
    return db.scalar(select(UserConfig).where(UserConfig.user_id == user_id, UserConfig.category == category))


def list_user_configs(db: Session, user_id: str) -> list[UserConfig]:
    return list(db.scalars(select(UserConfig).where(UserConfig.user_id == user_id)).all())


def upsert_user_config(db: Session, user_id: str, category: str, data: dict) -> UserConfig:
    item = get_user_config(db, user_id, category)
    if not item:
        item = UserConfig(user_id=user_id, category=category, data={})
        db.add(item)
    item.data = data
    db.flush()
    return item
