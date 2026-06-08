from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RuleVersion, User
from app.db.models.base import now_utc
from app.db.session import Base, engine
from app.services.rules.loader import RuleLoader


DEV_USER_EMAIL = "dev@agentops.local"


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)


def ensure_dev_user(db: Session) -> User:
    user = db.scalar(select(User).where(User.email == DEV_USER_EMAIL))
    if user:
        return user
    user = User(email=DEV_USER_EMAIL, display_name="Development User")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def ensure_initial_rule_version(db: Session, user: User | None = None) -> RuleVersion:
    active = db.scalar(select(RuleVersion).where(RuleVersion.is_active.is_(True)))
    if active:
        return active
    loader = RuleLoader()
    snapshot = {item["name"]: loader.read_rule(item["name"]) for item in loader.list_rules()}
    version = RuleVersion(
        version=1,
        name="Initial rules",
        is_active=True,
        snapshot=snapshot,
        summary="Initial snapshot from repository rules directory.",
        created_by=user.id if user else None,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def bootstrap_database() -> None:
    create_schema()
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        user = ensure_dev_user(db)
        ensure_initial_rule_version(db, user)
