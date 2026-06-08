from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.rules.loader import RuleLoader

router = APIRouter()


class RuleCollectRequest(BaseModel):
    source: str
    source_type: str = "url"


@router.get("")
def list_rules() -> list[dict]:
    return RuleLoader().list_rules()


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
