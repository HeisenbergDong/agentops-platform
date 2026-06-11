from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.db.models import RuleVersion, User
from app.db.models.base import now_utc
from app.db.session import Base, engine
from app.core.config import settings
from app.core.security import hash_password
from app.db.repositories.roles import ensure_role_templates
from app.services.rules.loader import RuleLoader


DEV_USER_EMAIL = "dev@agentops.local"


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema_extensions()


def ensure_schema_extensions() -> None:
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("users")}
    statements: list[str] = []
    if "password_hash" not in columns:
        statements.append("alter table users add column password_hash text not null default ''")
    if "role" not in columns:
        statements.append("alter table users add column role varchar(32) not null default 'user'")
    if "auth_token_version" not in columns:
        statements.append("alter table users add column auth_token_version integer not null default 0")
    if "last_login_at" not in columns:
        statements.append("alter table users add column last_login_at timestamp with time zone null")
    if "workers" in inspector.get_table_names():
        worker_columns = {item["name"] for item in inspector.get_columns("workers")}
        if "token_hash" not in worker_columns:
            statements.append("alter table workers add column token_hash varchar(128) not null default ''")
        if "display_name" not in worker_columns:
            statements.append("alter table workers add column display_name varchar(255) not null default ''")
        if "worker_type" not in worker_columns:
            statements.append("alter table workers add column worker_type varchar(64) not null default 'windows_trae'")
        if "machine_fingerprint" not in worker_columns:
            statements.append("alter table workers add column machine_fingerprint varchar(512) not null default ''")
        if "version" not in worker_columns:
            statements.append("alter table workers add column version varchar(64) not null default ''")
        if "capabilities" not in worker_columns:
            statements.append("alter table workers add column capabilities json not null default '[]'::json")
        if "status" not in worker_columns:
            statements.append("alter table workers add column status varchar(32) not null default 'online'")
        if "registered_at" not in worker_columns:
            statements.append("alter table workers add column registered_at timestamp with time zone not null default now()")
        if "revoked_at" not in worker_columns:
            statements.append("alter table workers add column revoked_at timestamp with time zone null")
    if "worker_commands" in inspector.get_table_names():
        command_columns = {item["name"] for item in inspector.get_columns("worker_commands")}
        if "lease_id" not in command_columns:
            statements.append("alter table worker_commands add column lease_id varchar(64) not null default ''")
        if "lease_expires_at" not in command_columns:
            statements.append("alter table worker_commands add column lease_expires_at timestamp with time zone null")
    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def ensure_dev_user(db: Session) -> User:
    user = db.scalar(select(User).where(User.email == DEV_USER_EMAIL))
    if user:
        return user
    user = User(email=DEV_USER_EMAIL, display_name="Development User", role="user")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def ensure_admin_user(db: Session) -> User:
    admin = db.scalar(select(User).where(User.role == "admin"))
    if admin:
        if not admin.password_hash:
            admin.password_hash = hash_password(settings.bootstrap_admin_password)
            db.commit()
            db.refresh(admin)
        return admin
    email = settings.bootstrap_admin_email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        user = User(email=email, display_name=settings.bootstrap_admin_name, role="admin")
        db.add(user)
    user.display_name = user.display_name or settings.bootstrap_admin_name
    user.role = "admin"
    user.is_active = True
    user.password_hash = hash_password(settings.bootstrap_admin_password)
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
        admin = ensure_admin_user(db)
        ensure_dev_user(db)
        ensure_initial_rule_version(db, admin)
        ensure_role_templates(db)
