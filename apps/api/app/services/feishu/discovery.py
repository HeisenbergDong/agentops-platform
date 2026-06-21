from typing import Any

import httpx

from app.services.feishu.auth import FEISHU_BASE_URL, get_feishu_access_token, tenant_access_token
from app.services.feishu.writer import _format_feishu_error


class FeishuDiscoveryError(RuntimeError):
    pass


def discover_feishu_resources(feishu_config: dict[str, Any], *, require_user_oauth: bool = False) -> dict[str, Any]:
    access_token, refreshed_cache, auth_mode = get_feishu_access_token(
        feishu_config,
        require_user_oauth=require_user_oauth,
    )
    app_token = str(feishu_config.get("app_token") or "").strip()
    table_id = str(feishu_config.get("table_id") or "").strip()

    resources: dict[str, Any] = {
        "bases": [],
        "tables": [],
        "views": [],
        "fields": [],
        "auth_mode": auth_mode,
        "message": "飞书用户授权已验证，资源信息已更新。" if auth_mode == "user_oauth" else "飞书应用授权已验证，资源信息已更新。",
    }
    if app_token:
        resources["tables"] = _get_items(
            f"{FEISHU_BASE_URL}/bitable/v1/apps/{app_token}/tables",
            access_token,
            params={"page_size": "100"},
        )
    if app_token and table_id:
        resources["fields"] = _get_items(
            f"{FEISHU_BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            access_token,
            params={"page_size": "100"},
        )
        resources["views"] = _get_items(
            f"{FEISHU_BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/views",
            access_token,
            params={"page_size": "100"},
        )

    result: dict[str, Any] = {"discovered_resources": resources}
    if refreshed_cache:
        result["token_cache"] = refreshed_cache
    return result


def _tenant_access_token(app_id: str, app_secret: str) -> dict[str, Any]:
    return tenant_access_token(app_id, app_secret)


def _get_items(url: str, access_token: str, params: dict[str, str]) -> list[dict[str, Any]]:
    response = httpx.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    try:
        payload = response.json()
    except Exception as exc:
        raise FeishuDiscoveryError(f"Feishu returned non-JSON response: HTTP {response.status_code}") from exc
    if response.status_code >= 400:
        raise FeishuDiscoveryError(_format_feishu_error(response.status_code, payload, response.text))
    if payload.get("code") != 0:
        raise FeishuDiscoveryError(_format_feishu_error(response.status_code, payload, response.text))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return list(data.get("items") or [])
