from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import User
from app.db.session import get_db
from app.services.feishu.discovery import FeishuDiscoveryError, discover_feishu_resources
from app.services.preflight import build_preflight
from app.services.user_settings import (
    load_user_settings,
    public_user_settings,
    readiness,
    save_user_settings,
)

router = APIRouter()


class SettingsUpdate(BaseModel):
    model: dict = Field(default_factory=dict)
    github: dict = Field(default_factory=dict)
    feishu: dict = Field(default_factory=dict)
    webhook: dict = Field(default_factory=dict)
    worker: dict = Field(default_factory=dict)
    defaults: dict = Field(default_factory=dict)
    trae: dict = Field(default_factory=dict)


@router.get("")
def get_settings(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    configs = load_user_settings(db, user.id)
    return {
        "sections": public_user_settings(configs),
        "readiness": readiness(configs),
        "preflight": build_preflight(db, user),
    }


@router.put("")
def save_settings(
    payload: SettingsUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    save_user_settings(db, user.id, payload.model_dump())
    db.commit()
    return get_settings(user, db)


@router.get("/preflight")
def get_preflight(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return build_preflight(db, user)


@router.post("/feishu/discover")
def discover_feishu(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    configs = load_user_settings(db, user.id)
    feishu = dict(configs.get("feishu", {}))
    try:
        discovered = discover_feishu_resources(feishu)
    except FeishuDiscoveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Feishu discovery failed: {exc}") from exc
    feishu.update(discovered)
    save_user_settings(db, user.id, {"feishu": feishu}, allow_internal=True)
    db.commit()
    configs = load_user_settings(db, user.id)
    return {
        "status": "ok",
        "feishu": public_user_settings(configs)["feishu"],
        "resources": configs.get("feishu", {}).get("discovered_resources", {}),
    }
