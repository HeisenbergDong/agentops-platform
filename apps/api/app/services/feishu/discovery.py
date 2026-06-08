import time
from typing import Any

import httpx

from app.core.secrets import seal_secret
from app.services.user_settings import safe_open_secret

FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"


class FeishuDiscoveryError(RuntimeError):
    pass


def discover_feishu_resources(feishu_config: dict[str, Any]) -> dict[str, Any]:
    app_id = str(feishu_config.get("app_id") or "").strip()
    app_secret = safe_open_secret(feishu_config.get("app_secret"))
    if not app_id or not app_secret:
        raise FeishuDiscoveryError("Feishu App ID and App Secret are required.")

    token_payload = _tenant_access_token(app_id, app_secret)
    tenant_access_token = token_payload["tenant_access_token"]
    expire = int(token_payload.get("expire", 7200))
    expires_at = int(time.time()) + max(expire - 300, 60)

    return {
        "token_cache": {
            "tenant_access_token": seal_secret(tenant_access_token),
            "expires_at": expires_at,
        },
        "discovered_resources": {
            "bases": [],
            "tables": [],
            "views": [],
            "message": "飞书授权已验证；具体 base/table/view 将由飞书角色按权限和任务上下文自动发现。",
            "refreshed_at": int(time.time()),
        },
    }


def _tenant_access_token(app_id: str, app_secret: str) -> dict[str, Any]:
    url = f"{FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal"
    response = httpx.post(
        url,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise FeishuDiscoveryError(str(data.get("msg") or "Failed to get Feishu tenant token."))
    if not data.get("tenant_access_token"):
        raise FeishuDiscoveryError("Feishu response did not include tenant_access_token.")
    return data
