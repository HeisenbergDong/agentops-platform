from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.secrets import mask_secret, open_secret, seal_secret
from app.db.models import User
from app.db.repositories.users import list_user_configs, upsert_user_config
from app.db.session import get_db

router = APIRouter()

SECRET_FIELDS = {
    "model": {"api_key"},
    "github": {"token"},
    "feishu": {"app_secret"},
    "webhook": {"secret"},
}

CATEGORIES = ("model", "github", "feishu", "webhook", "trae")


class SettingsUpdate(BaseModel):
    model: dict = Field(default_factory=dict)
    github: dict = Field(default_factory=dict)
    feishu: dict = Field(default_factory=dict)
    webhook: dict = Field(default_factory=dict)
    trae: dict = Field(default_factory=dict)


@router.get("")
def get_settings(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    configs = {item.category: item.data for item in list_user_configs(db, user.id)}
    return {category: _public_config(category, configs.get(category, {})) for category in CATEGORIES}


@router.put("")
def save_settings(
    payload: SettingsUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    existing = {item.category: item.data for item in list_user_configs(db, user.id)}
    incoming = payload.model_dump()
    for category in CATEGORIES:
        merged = dict(existing.get(category, {}))
        for key, value in incoming.get(category, {}).items():
            if key.endswith("_configured") or key.endswith("_mask"):
                continue
            if key in SECRET_FIELDS.get(category, set()):
                if value:
                    merged[key] = seal_secret(str(value))
                continue
            merged[key] = value
        upsert_user_config(db, user.id, category, merged)
    db.commit()
    return get_settings(user, db)


def _public_config(category: str, data: dict) -> dict:
    result: dict = {}
    secret_fields = SECRET_FIELDS.get(category, set())
    for key, value in data.items():
        if key in secret_fields:
            plain = open_secret(value)
            result[f"{key}_configured"] = bool(plain)
            result[f"{key}_mask"] = mask_secret(plain)
        else:
            result[key] = value
    for key in secret_fields:
        result.setdefault(f"{key}_configured", False)
        result.setdefault(f"{key}_mask", "")
    return result
