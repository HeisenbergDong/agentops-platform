from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.repositories.rules import active_rule_version, list_rule_versions
from app.db.session import get_db
from app.services.rules.loader import RuleLoader

router = APIRouter()


class RuleCollectRequest(BaseModel):
    source: str
    source_type: str = "url"


@router.get("")
def list_rules() -> list[dict]:
    return RuleLoader().list_rules()


@router.get("/versions")
def versions(db: Session = Depends(get_db)) -> list[dict]:
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
def active_version(db: Session = Depends(get_db)) -> dict:
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
def read_rule(name: str) -> dict:
    try:
        return {"name": name, "content": RuleLoader().read_rule(name)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Rule not found") from None


@router.post("/collect")
def collect_rules(payload: RuleCollectRequest) -> dict:
    return {
        "source": payload.source,
        "source_type": payload.source_type,
        "status": "proposal_only",
        "message": "Rule collector scaffold created; document fetching and splitting will be implemented next.",
    }
