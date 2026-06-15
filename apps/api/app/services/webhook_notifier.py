from __future__ import annotations

import hmac
import time
from hashlib import sha256
from typing import Any

import httpx

from app.services.user_settings import safe_open_secret


class WebhookNotifyError(RuntimeError):
    pass


def notify_manual_required(
    webhook_config: dict[str, Any],
    *,
    job_id: str,
    round_id: str | None,
    message: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    url = str(webhook_config.get("url") or "").strip()
    if not url:
        return {"status": "skipped", "reason": "missing_webhook_url"}

    text = _manual_required_text(job_id=job_id, round_id=round_id, message=message, details=details)
    payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
    secret = safe_open_secret(webhook_config.get("secret"))
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = _feishu_sign(secret, timestamp)

    try:
        response = httpx.post(url, json=payload, timeout=6)
    except httpx.HTTPError as exc:
        raise WebhookNotifyError(f"Webhook notification failed: {exc.__class__.__name__}") from exc

    if response.status_code >= 400:
        raise WebhookNotifyError(f"Webhook notification failed with status {response.status_code}: {response.text[:200]}")
    return {"status": "sent", "http_status": response.status_code}


def notify_text(webhook_config: dict[str, Any], text: str) -> dict[str, Any]:
    url = str(webhook_config.get("url") or "").strip()
    if not url:
        return {"status": "skipped", "reason": "missing_webhook_url"}
    payload: dict[str, Any] = {"msg_type": "text", "content": {"text": str(text or "").strip()}}
    secret = safe_open_secret(webhook_config.get("secret"))
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = _feishu_sign(secret, timestamp)
    try:
        response = httpx.post(url, json=payload, timeout=6)
    except httpx.HTTPError as exc:
        raise WebhookNotifyError(f"Webhook notification failed: {exc.__class__.__name__}") from exc
    if response.status_code >= 400:
        raise WebhookNotifyError(f"Webhook notification failed with status {response.status_code}: {response.text[:200]}")
    return {"status": "sent", "http_status": response.status_code}


def _manual_required_text(*, job_id: str, round_id: str | None, message: str, details: dict[str, Any]) -> str:
    lines = [
        "AgentOps 需要人工介入",
        f"Job: {job_id}",
        f"Round: {round_id or '-'}",
        f"原因: {message}",
    ]
    command_type = str(details.get("command_type") or "")
    result_status = str(details.get("result_status") or "")
    error = str(details.get("error") or "")
    if command_type:
        lines.append(f"命令: {command_type}")
    if result_status:
        lines.append(f"状态: {result_status}")
    if error:
        lines.append(f"错误: {error[:500]}")
    data = details.get("data") if isinstance(details.get("data"), dict) else {}
    if isinstance(data, dict):
        hint = data.get("manual_hint") or data.get("reason") or data.get("stage")
        if hint:
            lines.append(f"提示: {str(hint)[:500]}")
        screenshot = data.get("screenshot") if isinstance(data.get("screenshot"), dict) else {}
        if screenshot.get("path"):
            lines.append(f"截图: {screenshot['path']}")
    return "\n".join(lines)


def _feishu_sign(secret: str, timestamp: str) -> str:
    import base64

    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=sha256).digest()
    return base64.b64encode(digest).decode("ascii")
