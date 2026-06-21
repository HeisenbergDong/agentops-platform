from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy.orm import Session

from app.core.secrets import mask_secret, open_secret, seal_secret
from app.db.repositories.users import list_user_configs, upsert_user_config

CATEGORIES = ("model", "github", "feishu", "webhook", "worker", "defaults")

SECRET_FIELDS = {
    "model": {"api_key"},
    "github": {"token"},
    "feishu": {"app_secret"},
    "webhook": {"secret"},
}

INTERNAL_FIELDS = {
    "feishu": {"token_cache"},
}

DEPRECATED_FIELDS = {
    "github": {"repo_url"},
    "feishu": {"base_token"},
    "trae": {"workspace_path"},
}

DISPLAY_SUFFIXES = ("_configured", "_mask")

FEISHU_WRITE_URL_FIELDS = ("write_url", "bitable_url", "resource_url")


@dataclass(frozen=True)
class ReadinessItem:
    key: str
    label: str
    configured: bool
    required: bool = True


def load_user_settings(db: Session, user_id: str) -> dict[str, dict[str, Any]]:
    configs = {item.category: dict(item.data or {}) for item in list_user_configs(db, user_id)}
    worker = dict(configs.get("worker", {}))
    legacy_trae = configs.get("trae", {})
    if legacy_trae.get("workspace_path") and not worker.get("trae_workspace_path"):
        worker["trae_workspace_path"] = legacy_trae["workspace_path"]
    configs["worker"] = worker
    return {category: _strip_deprecated(category, configs.get(category, {})) for category in CATEGORIES}


def save_user_settings(
    db: Session,
    user_id: str,
    incoming: dict[str, dict[str, Any]],
    allow_internal: bool = False,
) -> None:
    existing = load_user_settings(db, user_id)
    if incoming.get("trae", {}).get("workspace_path"):
        incoming.setdefault("worker", {})["trae_workspace_path"] = incoming["trae"]["workspace_path"]

    for category in CATEGORIES:
        merged = _strip_deprecated(category, dict(existing.get(category, {})))
        incoming_category = dict(incoming.get(category, {}) or {})
        if category == "feishu":
            incoming_category = _normalize_feishu_config(incoming_category)
        for key, value in incoming_category.items():
            if _is_display_field(key) or key in DEPRECATED_FIELDS.get(category, set()):
                continue
            if key in SECRET_FIELDS.get(category, set()):
                if value:
                    text = str(value)
                    merged[key] = text if text.startswith("enc:v1:") else seal_secret(text)
                continue
            if key in INTERNAL_FIELDS.get(category, set()) and not allow_internal:
                continue
            merged[key] = value
        if category == "feishu":
            merged = _normalize_feishu_config(merged)
        upsert_user_config(db, user_id, category, merged)


def public_user_settings(configs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {category: public_config(category, configs.get(category, {})) for category in CATEGORIES}


def public_config(category: str, data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    secret_fields = SECRET_FIELDS.get(category, set())
    internal_fields = INTERNAL_FIELDS.get(category, set())
    for key, value in _strip_deprecated(category, data).items():
        if key in internal_fields:
            continue
        if key in secret_fields:
            plain = safe_open_secret(value)
            result[f"{key}_configured"] = bool(plain)
            result[f"{key}_mask"] = mask_secret(plain)
        else:
            result[key] = value
    for key in secret_fields:
        result.setdefault(f"{key}_configured", False)
        result.setdefault(f"{key}_mask", "")
    return result


def readiness(configs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    model = configs.get("model", {})
    github = configs.get("github", {})
    feishu = configs.get("feishu", {})
    worker = configs.get("worker", {})
    defaults = configs.get("defaults", {})
    items = [
        ReadinessItem("model.api_key", "模型 API Key", _has_secret(model.get("api_key"))),
        ReadinessItem("model.model_name", "模型名称", bool(model.get("model_name") or model.get("model"))),
        ReadinessItem("github.token", "GitHub Token", _has_secret(github.get("token")), required=False),
        ReadinessItem("feishu.app_id", "飞书 App ID", bool(feishu.get("app_id"))),
        ReadinessItem("feishu.app_secret", "飞书 App Secret", _has_secret(feishu.get("app_secret"))),
        ReadinessItem("feishu.app_token", "飞书 Base/App Token", bool(feishu.get("app_token"))),
        ReadinessItem("feishu.table_id", "飞书 Table ID", bool(feishu.get("table_id"))),
        ReadinessItem("worker.worker_id", "关联 Worker", bool(worker.get("worker_id"))),
        ReadinessItem("worker.trae_exe_path", "Trae 安装路径", bool(worker.get("trae_exe_path"))),
        ReadinessItem("worker.trae_workspace_path", "Trae 工作目录", bool(worker.get("trae_workspace_path"))),
        ReadinessItem("worker.browser_url", "浏览器验收 URL", bool(worker.get("browser_url"))),
        ReadinessItem(
            "defaults.default_rule_version_id",
            "默认规则版本",
            bool(defaults.get("default_rule_version_id")),
            required=False,
        ),
    ]
    missing_required = [item.label for item in items if item.required and not item.configured]
    return {
        "complete": not missing_required,
        "missing_required": missing_required,
        "items": [item.__dict__ for item in items],
    }


def safe_open_secret(value: Any) -> str:
    try:
        return open_secret(str(value)) if value else ""
    except Exception:
        return ""


def _has_secret(value: Any) -> bool:
    return bool(safe_open_secret(value))


def _strip_deprecated(category: str, data: dict[str, Any]) -> dict[str, Any]:
    deprecated = DEPRECATED_FIELDS.get(category, set())
    return {
        key: value
        for key, value in data.items()
        if key not in deprecated and not _is_display_field(key)
    }


def _is_display_field(key: str) -> bool:
    return any(key.endswith(suffix) for suffix in DISPLAY_SUFFIXES)


def _normalize_feishu_config(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    write_url = _first_text(result, FEISHU_WRITE_URL_FIELDS)
    if not write_url:
        return result

    parsed = _parse_feishu_write_url(write_url)
    result["write_url"] = write_url
    result.update(parsed)
    return result


def _first_text(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _parse_feishu_write_url(write_url: str) -> dict[str, str]:
    parsed = urlparse(write_url)
    parts = [part for part in parsed.path.split("/") if part]
    result: dict[str, str] = {}
    if "base" in parts:
        index = parts.index("base")
        if len(parts) > index + 1:
            result["app_token"] = parts[index + 1]

    query = parse_qs(parsed.query)
    table_id = _first_query_value(query, "table")
    view_id = _first_query_value(query, "view")
    if table_id:
        result["table_id"] = table_id
    if view_id:
        result["view_id"] = view_id
    return result


def _first_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0]).strip() if values else ""
