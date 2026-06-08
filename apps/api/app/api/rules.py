from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import User
from app.db.repositories.rules import active_rule_version, list_rule_versions
from app.db.repositories.roles import get_user_role
from app.db.repositories.user_rules import (
    append_user_rule_note,
    create_user_rule_file,
    get_user_rule_file,
    list_user_rule_files,
    reset_user_rule_file,
    update_user_rule_file,
)
from app.db.session import get_db
from app.services.rules.collector import RuleCollectorError, collect_rule_proposal, load_rule_source
from app.services.rules.loader import RuleLoader

router = APIRouter()


class RuleCollectRequest(BaseModel):
    source: str
    source_type: str = "url"


class RuleCreateRequest(BaseModel):
    name: str
    content: str = ""


class RuleUpdateRequest(BaseModel):
    content: str


class RuleCollectChange(BaseModel):
    rule_name: str
    title: str = "Collected Rule"
    reason: str = ""
    content: str


class RuleCollectApplyRequest(BaseModel):
    changes: list[RuleCollectChange]


@router.get("")
def list_rules(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [serialize_rule_file(item) for item in list_user_rule_files(db, user.id)]


@router.get("/versions")
def versions(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [
        {
            "id": item.id,
            "version": item.version,
            "name": item.name,
            "is_active": item.is_active,
            "summary": item.summary,
            "created_at": item.created_at.isoformat(),
        }
        for item in list_rule_versions(db)
    ]


@router.get("/active")
def active_version(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    item = active_rule_version(db)
    if not item:
        return {"active": None}
    return {
        "id": item.id,
        "version": item.version,
        "name": item.name,
        "is_active": item.is_active,
        "summary": item.summary,
        "file_count": len(item.snapshot),
    }


@router.get("/system")
def system_rules(user: User = Depends(current_user)) -> list[dict]:
    return RuleLoader().list_rules()


@router.post("")
def create_rule(
    payload: RuleCreateRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return serialize_rule_file(create_user_rule_file(db, user.id, payload.name, payload.content))
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if "already exists" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from None


@router.get("/{name}")
def read_rule(name: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    try:
        item = get_user_rule_file(db, user.id, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not item:
        raise HTTPException(status_code=404, detail="Rule not found")
    return serialize_rule_file(item, include_content=True)


@router.put("/{name}")
def update_rule(
    name: str,
    payload: RuleUpdateRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        item = get_user_rule_file(db, user.id, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not item:
        raise HTTPException(status_code=404, detail="Rule not found")
    return serialize_rule_file(update_user_rule_file(db, item, payload.content), include_content=True)


@router.post("/{name}/reset")
def reset_rule(name: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    try:
        item = get_user_rule_file(db, user.id, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not item:
        raise HTTPException(status_code=404, detail="Rule not found")
    try:
        return serialize_rule_file(reset_user_rule_file(db, item), include_content=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="System rule not found") from None


@router.post("/collect")
def collect_rules(
    payload: RuleCollectRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    role = get_user_role(db, user.id, "rule_collector")
    if not role:
        raise HTTPException(status_code=404, detail="Rule collector role not found")
    try:
        source_text, source_meta = load_rule_source(payload.source, payload.source_type)
        return collect_rule_proposal(db, user.id, role, source_text, source_meta)
    except RuleCollectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/collect/apply")
def apply_collected_rules(
    payload: RuleCollectApplyRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    applied = []
    for change in payload.changes:
        try:
            item = get_user_rule_file(db, user.id, change.rule_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not item:
            raise HTTPException(status_code=404, detail=f"Rule not found: {change.rule_name}")
        note = _format_collected_change(change)
        append_user_rule_note(db, item, note, heading=change.title or "Collected Rule")
        applied.append(change.rule_name)
    return {"status": "applied", "applied": applied}


def serialize_rule_file(item, include_content: bool = False) -> dict:
    data = {
        "id": item.id,
        "name": item.name,
        "size": len(item.content.encode("utf-8")),
        "source_name": item.source_name,
        "is_active": item.is_active,
        "updated_at": item.updated_at.isoformat(),
        "created_at": item.created_at.isoformat(),
    }
    if include_content:
        data["content"] = item.content
    return data


def _format_collected_change(change: RuleCollectChange) -> str:
    parts = []
    if change.reason.strip():
        parts.append(f"Reason: {change.reason.strip()}")
    parts.append(change.content.strip())
    return "\n\n".join(parts)
