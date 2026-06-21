import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import User
from app.db.session import get_db
from app.services.feishu.auth import FEISHU_AUTHORIZE_URL, FeishuAuthError, cache_user_token, exchange_authorization_code
from app.services.feishu.discovery import FeishuDiscoveryError, discover_feishu_resources
from app.services.preflight import build_preflight
from app.services.user_settings import (
    load_user_settings,
    public_user_settings,
    readiness,
    save_user_settings,
)

router = APIRouter()
FEISHU_OAUTH_STATE_TTL_SECONDS = 10 * 60
DEFAULT_FEISHU_OAUTH_SCOPE = "offline_access"


class SettingsUpdate(BaseModel):
    model: dict = Field(default_factory=dict)
    github: dict = Field(default_factory=dict)
    feishu: dict = Field(default_factory=dict)
    webhook: dict = Field(default_factory=dict)
    worker: dict = Field(default_factory=dict)
    defaults: dict = Field(default_factory=dict)
    trae: dict = Field(default_factory=dict)


class FeishuOAuthBeginRequest(BaseModel):
    redirect_uri: str = ""


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
        discovered = discover_feishu_resources(feishu, require_user_oauth=True)
    except FeishuAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
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


@router.post("/feishu/oauth/begin")
def begin_feishu_oauth(
    payload: FeishuOAuthBeginRequest,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    configs = load_user_settings(db, user.id)
    feishu = dict(configs.get("feishu", {}))
    app_id = str(feishu.get("app_id") or "").strip()
    app_secret_configured = bool(feishu.get("app_secret"))
    if not app_id or not app_secret_configured:
        raise HTTPException(status_code=400, detail="Feishu App ID and App Secret are required before authorization.")

    redirect_uri = str(payload.redirect_uri or "").strip() or str(request.url_for("feishu_oauth_callback"))
    state = secrets.token_urlsafe(32)
    token_cache = dict(feishu.get("token_cache") or {}) if isinstance(feishu.get("token_cache"), dict) else {}
    token_cache["oauth_state"] = state
    token_cache["oauth_state_expires_at"] = int(time.time()) + FEISHU_OAUTH_STATE_TTL_SECONDS
    token_cache["oauth_redirect_uri"] = redirect_uri
    token_cache["oauth_user_id"] = user.id
    token_cache["oauth_started_at"] = int(time.time())
    feishu["token_cache"] = token_cache
    save_user_settings(db, user.id, {"feishu": feishu}, allow_internal=True)
    db.commit()

    oauth_scope = str(feishu.get("oauth_scope") or DEFAULT_FEISHU_OAUTH_SCOPE).strip()
    authorize_params = {
        'response_type': 'code',
        'client_id': app_id,
        'app_id': app_id,
        'redirect_uri': redirect_uri,
        'state': state,
    }
    if oauth_scope:
        authorize_params["scope"] = oauth_scope
    authorize_url = f"{FEISHU_AUTHORIZE_URL}?{urlencode(authorize_params)}"
    return {
        "status": "authorization_required",
        "authorize_url": authorize_url,
        "redirect_uri": redirect_uri,
        "expires_in": FEISHU_OAUTH_STATE_TTL_SECONDS,
    }


@router.get("/feishu/oauth/callback", name="feishu_oauth_callback", response_class=HTMLResponse)
def feishu_oauth_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if error:
        return _oauth_callback_page(False, f"{error}: {error_description or 'Feishu authorization failed.'}")
    if not code or not state:
        return _oauth_callback_page(False, "Missing Feishu authorization code or state.")

    user, feishu = _feishu_config_by_oauth_state(db, state)
    if not user or not feishu:
        return _oauth_callback_page(False, "Feishu authorization state expired or does not match current user.")
    token_cache = dict(feishu.get("token_cache") or {}) if isinstance(feishu.get("token_cache"), dict) else {}
    expires_at = int(token_cache.get("oauth_state_expires_at") or 0)
    if expires_at and expires_at < int(time.time()):
        return _oauth_callback_page(False, "Feishu authorization state expired. Please start authorization again.")
    redirect_uri = str(token_cache.get("oauth_redirect_uri") or "")
    if not redirect_uri:
        return _oauth_callback_page(False, "Missing Feishu OAuth redirect_uri in state cache.")

    try:
        oauth_data = exchange_authorization_code(feishu, code, redirect_uri)
        refreshed_cache = cache_user_token(token_cache, oauth_data)
        for key in ("oauth_state", "oauth_state_expires_at", "oauth_redirect_uri", "oauth_user_id", "oauth_started_at"):
            refreshed_cache.pop(key, None)
        feishu["token_cache"] = refreshed_cache
        discovered = discover_feishu_resources(feishu, require_user_oauth=True)
        feishu.update(discovered)
        save_user_settings(db, user.id, {"feishu": feishu}, allow_internal=True)
        db.commit()
    except (FeishuAuthError, FeishuDiscoveryError) as exc:
        return _oauth_callback_page(False, str(exc))
    except Exception as exc:
        return _oauth_callback_page(False, f"Feishu OAuth callback failed: {exc}")

    return _oauth_callback_page(True, "Feishu authorization completed. You can return to AgentOps.")


def _feishu_config_by_oauth_state(db: Session, state: str) -> tuple[User | None, dict | None]:
    from app.db.models import UserConfig
    from sqlalchemy import select

    configs = db.scalars(select(UserConfig).where(UserConfig.category == "feishu")).all()
    for config in configs:
        data = dict(config.data or {})
        token_cache = data.get("token_cache") if isinstance(data.get("token_cache"), dict) else {}
        if token_cache.get("oauth_state") == state:
            user = db.get(User, config.user_id)
            return user, data
    return None, None


def _oauth_callback_page(ok: bool, message: str) -> HTMLResponse:
    status = "success" if ok else "error"
    safe_message = (
        str(message or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>Feishu OAuth</title>
  <style>
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; padding: 32px; }}
    .box {{ max-width: 560px; margin: 0 auto; line-height: 1.7; }}
    .success {{ color: #1677ff; }}
    .error {{ color: #cf1322; }}
  </style>
</head>
<body>
  <div class="box">
    <h2 class="{status}">{"授权成功" if ok else "授权失败"}</h2>
    <p>{safe_message}</p>
    <p>这个窗口可以关闭。</p>
  </div>
  <script>
    const payload = {{ source: "agentops-feishu-oauth", status: "{status}", message: "{safe_message}" }};
    if (window.opener) {{
      window.opener.postMessage(payload, window.location.origin);
      setTimeout(() => window.close(), 800);
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(html, status_code=200 if ok else 400)
