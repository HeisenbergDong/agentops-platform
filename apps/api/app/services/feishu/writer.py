import time
from typing import Any

import httpx

from app.core.secrets import seal_secret
from app.services.feishu.discovery import FEISHU_BASE_URL, _tenant_access_token
from app.services.user_settings import safe_open_secret


class FeishuWriteError(RuntimeError):
    pass


def write_feishu_record(feishu_config: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    app_token = str(
        feishu_config.get("app_token")
        or feishu_config.get("base_token")
        or feishu_config.get("bitable_app_token")
        or ""
    ).strip()
    table_id = str(feishu_config.get("table_id") or "").strip()
    view_id = str(feishu_config.get("view_id") or "").strip()
    if not app_token or not table_id:
        raise FeishuWriteError("Feishu app_token and table_id are required.")

    tenant_access_token, refreshed_cache = _tenant_token(feishu_config)
    url = f"{FEISHU_BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    params = {"user_id_type": "open_id"}
    if view_id:
        params["view_id"] = view_id
    response = httpx.post(
        url,
        params=params,
        json={"fields": fields},
        headers={"Authorization": f"Bearer {tenant_access_token}"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise FeishuWriteError(str(data.get("msg") or "Feishu record write failed."))

    record = data.get("data", {}).get("record", {}) if isinstance(data.get("data"), dict) else {}
    return {
        "status": "written",
        "record_id": str(record.get("record_id") or data.get("data", {}).get("record_id") or ""),
        "app_token": app_token,
        "table_id": table_id,
        "view_id": view_id,
        "token_cache": refreshed_cache,
        "response": data,
    }


def _tenant_token(feishu_config: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    token_cache = feishu_config.get("token_cache") if isinstance(feishu_config.get("token_cache"), dict) else {}
    cached_token = safe_open_secret(token_cache.get("tenant_access_token")) if token_cache else ""
    expires_at = int(token_cache.get("expires_at") or 0) if token_cache else 0
    if cached_token and expires_at > int(time.time()) + 60:
        return cached_token, None

    app_id = str(feishu_config.get("app_id") or "").strip()
    app_secret = safe_open_secret(feishu_config.get("app_secret"))
    if not app_id or not app_secret:
        raise FeishuWriteError("Feishu App ID and App Secret are required.")

    token_payload = _tenant_access_token(app_id, app_secret)
    tenant_access_token = token_payload["tenant_access_token"]
    expire = int(token_payload.get("expire", 7200))
    refreshed_cache = {
        "tenant_access_token": seal_secret(tenant_access_token),
        "expires_at": int(time.time()) + max(expire - 300, 60),
    }
    return tenant_access_token, refreshed_cache
