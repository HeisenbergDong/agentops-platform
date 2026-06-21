import time
from typing import Any

import httpx

from app.core.secrets import seal_secret
from app.services.user_settings import safe_open_secret

FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_REFRESH_SKEW_SECONDS = 300


class FeishuAuthError(RuntimeError):
    pass


def get_feishu_access_token(
    feishu_config: dict[str, Any],
    *,
    require_user_oauth: bool = False,
) -> tuple[str, dict[str, Any] | None, str]:
    token_cache = feishu_config.get("token_cache") if isinstance(feishu_config.get("token_cache"), dict) else {}

    cached_user_token = _cached_secret(token_cache, "user_access_token", "access_token")
    user_expires_at = _cached_expires_at(token_cache, "user_expires_at", "expires_at")
    if cached_user_token and user_expires_at > int(time.time()) + TOKEN_REFRESH_SKEW_SECONDS:
        return cached_user_token, None, "user_oauth"

    refresh_token = _cached_secret(token_cache, "refresh_token")
    refresh_expires_at = _cached_expires_at(token_cache, "refresh_expires_at")
    if refresh_token and (not refresh_expires_at or refresh_expires_at > int(time.time()) + TOKEN_REFRESH_SKEW_SECONDS):
        try:
            refreshed = refresh_user_token(feishu_config, refresh_token)
            refreshed_cache = cache_user_token(token_cache, refreshed, refresh_token)
            return _oauth_data_token(refreshed), refreshed_cache, "user_oauth"
        except (FeishuAuthError, httpx.HTTPError):
            pass

    if require_user_oauth:
        raise FeishuAuthError("Feishu user OAuth is required. Please authorize this user first.")

    cached_tenant_token = _cached_secret(token_cache, "tenant_access_token")
    tenant_expires_at = _cached_expires_at(token_cache, "tenant_expires_at", "expires_at")
    if cached_tenant_token and tenant_expires_at > int(time.time()) + 60:
        return cached_tenant_token, None, "tenant"

    app_id = str(feishu_config.get("app_id") or "").strip()
    app_secret = safe_open_secret(feishu_config.get("app_secret"))
    if not app_id or not app_secret:
        raise FeishuAuthError("Feishu App ID and App Secret are required.")

    token_payload = tenant_access_token(app_id, app_secret)
    tenant_token = token_payload["tenant_access_token"]
    expire = int(token_payload.get("expire", 7200))
    refreshed_cache = dict(token_cache)
    refreshed_cache["tenant_access_token"] = seal_secret(tenant_token)
    refreshed_cache["tenant_expires_at"] = int(time.time()) + max(expire - TOKEN_REFRESH_SKEW_SECONDS, 60)
    return tenant_token, refreshed_cache, "tenant"


def exchange_authorization_code(feishu_config: dict[str, Any], code: str, redirect_uri: str) -> dict[str, Any]:
    app_id = str(feishu_config.get("app_id") or "").strip()
    app_secret = safe_open_secret(feishu_config.get("app_secret"))
    if not app_id or not app_secret:
        raise FeishuAuthError("Feishu App ID and App Secret are required.")
    if not code:
        raise FeishuAuthError("Feishu authorization code is required.")

    response = httpx.post(
        f"{FEISHU_BASE_URL}/authen/v2/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": app_id,
            "client_secret": app_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code", 0) not in (0, None):
        raise FeishuAuthError(str(payload.get("msg") or "Failed to exchange Feishu authorization code."))
    data = payload.get("data") or payload
    if not _oauth_data_token(data):
        raise FeishuAuthError("Feishu authorization response did not include access_token.")
    return data


def refresh_user_token(feishu_config: dict[str, Any], refresh_token: str) -> dict[str, Any]:
    app_id = str(feishu_config.get("app_id") or "").strip()
    app_secret = safe_open_secret(feishu_config.get("app_secret"))
    if not app_id or not app_secret:
        raise FeishuAuthError("Feishu App ID and App Secret are required.")

    response = httpx.post(
        f"{FEISHU_BASE_URL}/authen/v2/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code", 0) not in (0, None):
        raise FeishuAuthError(str(payload.get("msg") or "Failed to refresh Feishu user token."))
    data = payload.get("data") or payload
    if not _oauth_data_token(data):
        raise FeishuAuthError("Feishu refresh response did not include access_token.")
    return data


def tenant_access_token(app_id: str, app_secret: str) -> dict[str, Any]:
    response = httpx.post(
        f"{FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise FeishuAuthError(str(data.get("msg") or "Failed to get Feishu tenant token."))
    if not data.get("tenant_access_token"):
        raise FeishuAuthError("Feishu response did not include tenant_access_token.")
    return data


def cache_user_token(
    existing_cache: dict[str, Any],
    oauth_data: dict[str, Any],
    fallback_refresh_token: str = "",
) -> dict[str, Any]:
    access_token = _oauth_data_token(oauth_data)
    refresh_token = str(oauth_data.get("refresh_token") or fallback_refresh_token)
    expires_at = int(time.time()) + max(_oauth_data_expires_in(oauth_data), 60)
    refreshed_cache = dict(existing_cache)
    refreshed_cache["user_access_token"] = seal_secret(access_token)
    refreshed_cache["user_expires_at"] = expires_at
    refreshed_cache["access_token"] = seal_secret(access_token)
    refreshed_cache["expires_at"] = expires_at
    if refresh_token:
        refreshed_cache["refresh_token"] = seal_secret(refresh_token)
    refresh_expires_in = _oauth_data_refresh_expires_in(oauth_data)
    if refresh_expires_in:
        refreshed_cache["refresh_expires_at"] = int(time.time()) + max(refresh_expires_in, 60)
    if oauth_data.get("scope"):
        refreshed_cache["scope"] = oauth_data["scope"]
    return refreshed_cache


def _cached_secret(cache: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = cache.get(key)
        if value:
            return safe_open_secret(value)
    return ""


def _cached_expires_at(cache: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = cache.get(key)
        if value:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return 0
    return 0


def _oauth_data_token(data: dict[str, Any]) -> str:
    return str(data.get("access_token") or data.get("user_access_token") or "")


def _oauth_data_expires_in(data: dict[str, Any]) -> int:
    value = data.get("expires_in") or data.get("user_access_token_expires_in") or data.get("access_token_expires_in") or 7200
    try:
        return int(value)
    except (TypeError, ValueError):
        return 7200


def _oauth_data_refresh_expires_in(data: dict[str, Any]) -> int | None:
    value = data.get("refresh_expires_in") or data.get("refresh_token_expires_in") or 0
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed or None
