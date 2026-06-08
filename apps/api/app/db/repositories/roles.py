from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RoleTemplate, UserRole
from app.services.roles.registry import ROLE_REGISTRY, RoleDefinition


def ensure_role_templates(db: Session) -> None:
    existing = {item.role_key: item for item in db.scalars(select(RoleTemplate)).all()}
    for definition in ROLE_REGISTRY:
        if definition.key in existing:
            continue
        db.add(role_template_from_definition(definition))
    db.commit()


def list_role_templates(db: Session) -> list[RoleTemplate]:
    return list(db.scalars(select(RoleTemplate).order_by(RoleTemplate.role_key)).all())


def get_role_template(db: Session, role_key: str) -> RoleTemplate | None:
    return db.scalar(select(RoleTemplate).where(RoleTemplate.role_key == role_key))


def ensure_user_roles(db: Session, user_id: str) -> None:
    ensure_role_templates(db)
    existing = {
        item.role_key: item
        for item in db.scalars(select(UserRole).where(UserRole.user_id == user_id)).all()
    }
    for template in list_role_templates(db):
        if template.role_key in existing:
            continue
        db.add(user_role_from_template(user_id, template))
    db.commit()


def list_user_roles(db: Session, user_id: str) -> list[UserRole]:
    ensure_user_roles(db, user_id)
    return list(
        db.scalars(
            select(UserRole).where(UserRole.user_id == user_id).order_by(UserRole.role_key)
        ).all()
    )


def get_user_role(db: Session, user_id: str, role_key: str) -> UserRole | None:
    ensure_user_roles(db, user_id)
    return db.scalar(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role_key == role_key)
    )


def update_user_role(db: Session, role: UserRole, values: dict) -> UserRole:
    for field in ("name", "purpose", "rules", "enabled", "model_config_key", "config"):
        if field in values and values[field] is not None:
            setattr(role, field, values[field])
    db.commit()
    db.refresh(role)
    return role


def role_template_from_definition(definition: RoleDefinition) -> RoleTemplate:
    return RoleTemplate(
        role_key=definition.key,
        name=definition.name,
        purpose=definition.purpose,
        rules=definition.rules,
        enabled=definition.enabled,
        model_config_key=definition.model_config_key,
        config={},
    )


def user_role_from_template(user_id: str, template: RoleTemplate) -> UserRole:
    return UserRole(
        user_id=user_id,
        template_id=template.id,
        role_key=template.role_key,
        name=template.name,
        purpose=template.purpose,
        rules=template.rules,
        enabled=template.enabled,
        model_config_key=template.model_config_key,
        config=dict(template.config or {}),
    )
