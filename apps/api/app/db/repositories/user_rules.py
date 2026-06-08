from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import UserRuleFile
from app.db.models.base import now_utc
from app.services.rules.loader import RuleLoader


def normalize_rule_name(name: str) -> str:
    value = name.strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid rule name")
    return value


def ensure_user_rule_files(db: Session, user_id: str) -> None:
    loader = RuleLoader()
    existing = set(db.scalars(select(UserRuleFile.name).where(UserRuleFile.user_id == user_id)).all())
    for item in loader.list_rules():
        name = item["name"]
        if name in existing:
            continue
        db.add(
            UserRuleFile(
                user_id=user_id,
                name=name,
                content=loader.read_rule(name),
                source_name=name,
                config={},
            )
        )
    db.commit()


def list_user_rule_files(db: Session, user_id: str) -> list[UserRuleFile]:
    ensure_user_rule_files(db, user_id)
    return list(
        db.scalars(
            select(UserRuleFile)
            .where(UserRuleFile.user_id == user_id)
            .order_by(UserRuleFile.name)
        ).all()
    )


def get_user_rule_file(db: Session, user_id: str, name: str) -> UserRuleFile | None:
    rule_name = normalize_rule_name(name)
    ensure_user_rule_files(db, user_id)
    return db.scalar(
        select(UserRuleFile).where(UserRuleFile.user_id == user_id, UserRuleFile.name == rule_name)
    )


def create_user_rule_file(db: Session, user_id: str, name: str, content: str) -> UserRuleFile:
    rule_name = normalize_rule_name(name)
    existing = db.scalar(
        select(UserRuleFile).where(UserRuleFile.user_id == user_id, UserRuleFile.name == rule_name)
    )
    if existing:
        raise ValueError("Rule already exists")
    item = UserRuleFile(
        user_id=user_id,
        name=rule_name,
        content=content,
        source_name="",
        config={},
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_user_rule_file(db: Session, item: UserRuleFile, content: str) -> UserRuleFile:
    item.content = content
    db.commit()
    db.refresh(item)
    return item


def append_user_rule_note(
    db: Session,
    item: UserRuleFile,
    note: str,
    heading: str = "Manual Capability Note",
) -> UserRuleFile:
    stamp = now_utc().replace(microsecond=0).isoformat()
    clean_note = note.strip()
    block = f"\n\n## {heading} - {stamp}\n\n{clean_note}\n"
    item.content = f"{item.content.rstrip()}{block}"
    db.commit()
    db.refresh(item)
    return item


def reset_user_rule_file(db: Session, item: UserRuleFile) -> UserRuleFile:
    source_name = item.source_name or item.name
    content = RuleLoader().read_rule(source_name)
    item.content = content
    item.source_name = source_name
    db.commit()
    db.refresh(item)
    return item


def read_user_rule_many(db: Session, user_id: str, names: list[str]) -> dict[str, str]:
    ensure_user_rule_files(db, user_id)
    result: dict[str, str] = {}
    for name in names:
        rule_name = normalize_rule_name(name)
        item = db.scalar(
            select(UserRuleFile).where(UserRuleFile.user_id == user_id, UserRuleFile.name == rule_name)
        )
        if not item:
            raise FileNotFoundError(rule_name)
        result[rule_name] = item.content
    return result
