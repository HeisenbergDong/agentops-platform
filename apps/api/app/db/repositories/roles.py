from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RoleTemplate
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
