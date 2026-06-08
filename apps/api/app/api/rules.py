from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import User
from app.db.repositories.rules import active_rule_version, list_rule_versions
from app.db.session import get_db
from app.services.rules.loader import RuleLoader

router = APIRouter()


class RuleCollectRequest(BaseModel):
    source: str
    source_type: str = "url"


@router.get("")
def list_rules(user: User = Depends(current_user)) -> list[dict]:
    return RuleLoader().list_rules()


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


@router.get("/{name}")
def read_rule(name: str, user: User = Depends(current_user)) -> dict:
    try:
        return {"name": name, "content": RuleLoader().read_rule(name)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Rule not found") from None


@router.post("/collect")
def collect_rules(payload: RuleCollectRequest, user: User = Depends(current_user)) -> dict:
    return {
        "source": payload.source,
        "source_type": payload.source_type,
        "status": "proposal_only",
        "message": "Rule collector scaffold created; document fetching and splitting will be implemented next.",
    }
