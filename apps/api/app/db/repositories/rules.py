from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import RuleVersion


def active_rule_version(db: Session) -> RuleVersion | None:
    return db.scalar(select(RuleVersion).where(RuleVersion.is_active.is_(True)))


def list_rule_versions(db: Session) -> list[RuleVersion]:
    return list(db.scalars(select(RuleVersion).order_by(desc(RuleVersion.version))).all())
