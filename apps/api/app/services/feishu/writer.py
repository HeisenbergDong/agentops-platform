from typing import Any
import mimetypes
from pathlib import Path

import httpx

from app.services.feishu.auth import FEISHU_BASE_URL, get_feishu_access_token

SESSION_FIELD = "Trae Session ID"
ATTACHMENT_FIELD = "截图（userprompt附件/产物/运行结果/对话）"
REQUIRED_WRITABLE_FIELDS = {
    SESSION_FIELD,
    "轮次",
    "User Prompt",
    "任务类型",
    "业务领域",
    "修改范围",
    "任务是否完成",
    "产物及过程是否满意",
    "不满意原因",
    "github地址",
    "commit id",
    "分支/文件夹",
    "日志轨迹",
    ATTACHMENT_FIELD,
}
ALWAYS_CLEAR_WHEN_EMPTY = {"不满意原因"}
VALUE_ALIASES = {
    "轮次": {
        "1": "第一轮",
        "2": "第二轮",
        "3": "第三轮",
        "4": "第四轮",
        "5": "第五轮",
    },
    "任务类型": {
        "implementation": "工程化",
        "feature": "Feature迭代",
        "bugfix": "Bug修复",
        "bug": "Bug修复",
        "new": "0-1代码生成",
    },
    "任务是否完成": {
        "completed": "完成了任务",
        "complete": "完成了任务",
        "done": "完成了任务",
        "success": "完成了任务",
        "failed": "未完成任务",
        "incomplete": "未完成任务",
    },
}
TARGET_RECORD_ID_KEYS = ("target_record_id", "record_id", "feishu_record_id")
TARGET_UID_KEYS = ("target_uid", "row_number", "uid", "feishu_uid")


class FeishuWriteError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        token_cache: dict[str, Any] | None = None,
        auth_mode: str = "",
        status_code: int | None = None,
        code: Any = None,
        operation: str = "",
    ) -> None:
        super().__init__(message)
        self.token_cache = token_cache
        self.auth_mode = auth_mode
        self.status_code = status_code
        self.code = code
        self.operation = operation


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

    access_token, refreshed_cache, auth_mode = get_feishu_access_token(feishu_config)
    try:
        field_meta = _list_fields(access_token, app_token, table_id)
    except FeishuWriteError as exc:
        if refreshed_cache and exc.token_cache is None:
            exc.token_cache = refreshed_cache
        if auth_mode and not exc.auth_mode:
            exc.auth_mode = auth_mode
        raise
    available_fields = {item.get("field_name", "") for item in field_meta if item.get("field_name")}
    options = _option_names(field_meta)
    explicit_target = _find_explicit_target_record(access_token, app_token, table_id, feishu_config)
    if explicit_target is None and _explicit_target_requested(feishu_config):
        raise FeishuWriteError("Explicit Feishu target row was not found.")
    target_record = explicit_target or _find_empty_session_record(access_token, app_token, table_id)
    target_record_id = _record_id(target_record)
    field_report = _field_mapping_report(fields, available_fields)
    if field_report["missing_required_fields"]:
        raise FeishuWriteError(
            "Feishu table is missing required writable fields: "
            + ", ".join(field_report["missing_required_fields"])
        )
    mapped = _filter_allowed_fields(fields, available_fields)
    try:
        mapped = _prepare_attachment_field(access_token, app_token, mapped, available_fields)
    except FeishuWriteError as exc:
        if refreshed_cache and exc.token_cache is None:
            exc.token_cache = refreshed_cache
        if auth_mode and not exc.auth_mode:
            exc.auth_mode = auth_mode
        raise
    duplicate = _find_duplicate_record_for_search(access_token, app_token, table_id, target_record_id, mapped)
    if duplicate:
        return {
            "status": "skipped_duplicate",
            "record_id": _record_id(duplicate),
            "operation": "skipped_duplicate",
            "duplicate_existing_uid": (duplicate.get("fields") or {}).get("UID"),
            "app_token": app_token,
            "table_id": table_id,
            "view_id": view_id,
            "token_cache": refreshed_cache,
            "auth_mode": auth_mode,
            "field_report": field_report,
            "response": {},
        }

    if not target_record and not bool(feishu_config.get("allow_create_record")):
        raise FeishuWriteError("No record with empty Trae Session ID was found.")

    updates = _build_updates(mapped, target_record, options, _overwrite_fields(feishu_config))
    if not updates:
        return {
            "status": "skipped_no_updates",
            "record_id": target_record_id,
            "operation": "skipped_no_updates",
            "app_token": app_token,
            "table_id": table_id,
            "view_id": view_id,
            "token_cache": refreshed_cache,
            "auth_mode": auth_mode,
            "field_report": field_report,
            "response": {},
        }

    try:
        if target_record_id:
            data = _request_json(
                "PUT",
                f"{FEISHU_BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{target_record_id}",
                access_token,
                json={"fields": updates},
            )
            operation = "updated"
        else:
            data = _request_json(
                "POST",
                f"{FEISHU_BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                access_token,
                json={"fields": updates},
            )
            operation = "created"
    except FeishuWriteError as exc:
        if refreshed_cache and exc.token_cache is None:
            exc.token_cache = refreshed_cache
        if auth_mode and not exc.auth_mode:
            exc.auth_mode = auth_mode
        raise

    record = data.get("data", {}).get("record", {}) if isinstance(data.get("data"), dict) else {}
    return {
        "status": "written",
        "record_id": str(record.get("record_id") or data.get("data", {}).get("record_id") or ""),
        "operation": operation,
        "update_field_count": len(updates),
        "update_fields": updates,
        "app_token": app_token,
        "table_id": table_id,
        "view_id": view_id,
        "token_cache": refreshed_cache,
        "auth_mode": auth_mode,
        "field_report": field_report,
        "response": data,
    }


def _list_fields(access_token: str, app_token: str, table_id: str) -> list[dict[str, Any]]:
    data = _request_json(
        "GET",
        f"{FEISHU_BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        access_token,
        params={"page_size": "100"},
    )
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    return list(payload.get("items") or [])


def _search_records(
    access_token: str,
    app_token: str,
    table_id: str,
    *,
    field_names: list[str] | None = None,
    conditions: list[dict[str, Any]] | None = None,
    page_size: int = 100,
    max_pages: int = 1,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page_token = ""
    body: dict[str, Any] = {"automatic_fields": False}
    if field_names:
        body["field_names"] = field_names
    if conditions:
        body["filter"] = {"conjunction": "and", "conditions": conditions}
    for _ in range(max_pages):
        params = {"page_size": str(page_size)}
        if page_token:
            params["page_token"] = page_token
        data = _request_json(
            "POST",
            f"{FEISHU_BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
            access_token,
            params=params,
            json=body,
        )
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        records.extend(list(payload.get("items") or []))
        if not payload.get("has_more"):
            break
        page_token = str(payload.get("page_token") or "")
        if not page_token:
            break
    return records


def _find_empty_session_record(access_token: str, app_token: str, table_id: str) -> dict[str, Any]:
    records = _search_records(
        access_token,
        app_token,
        table_id,
        field_names=["UID", SESSION_FIELD],
        conditions=[{"field_name": SESSION_FIELD, "operator": "isEmpty", "value": []}],
        page_size=100,
        max_pages=5,
    )
    candidates: list[tuple[int, int, str]] = []
    by_id = {}
    for index, item in enumerate(records, start=1):
        fields = item.get("fields") or {}
        uid = str(fields.get("UID") or "").strip()
        session = fields.get(SESSION_FIELD)
        record_id = _record_id(item)
        if uid.isdigit() and record_id and _is_empty(session):
            candidates.append((int(uid), index, record_id))
            by_id[record_id] = item
    if not candidates:
        return {}
    candidates.sort(key=lambda item: (item[0], item[1]))
    return by_id.get(candidates[0][2], {})


def _explicit_target_requested(feishu_config: dict[str, Any]) -> bool:
    return any(str(feishu_config.get(key) or "").strip() for key in (*TARGET_RECORD_ID_KEYS, *TARGET_UID_KEYS))


def _find_explicit_target_record(
    access_token: str,
    app_token: str,
    table_id: str,
    feishu_config: dict[str, Any],
) -> dict[str, Any] | None:
    target_record_id = _first_config_value(feishu_config, TARGET_RECORD_ID_KEYS)
    if target_record_id:
        return _get_record(access_token, app_token, table_id, target_record_id)

    target_uid = _first_config_value(feishu_config, TARGET_UID_KEYS)
    if not target_uid:
        return {}
    records = _search_records(
        access_token,
        app_token,
        table_id,
        field_names=["UID", SESSION_FIELD],
        conditions=[{"field_name": "UID", "operator": "is", "value": [target_uid]}],
        page_size=10,
    )
    return records[0] if records else None


def _find_explicit_target_record_from_records(
    records: list[dict[str, Any]],
    feishu_config: dict[str, Any],
) -> dict[str, Any] | None:
    target_record_id = _first_config_value(feishu_config, TARGET_RECORD_ID_KEYS)
    if target_record_id:
        for record in records:
            if _record_id(record) == target_record_id:
                return record
        return None

    target_uid = _first_config_value(feishu_config, TARGET_UID_KEYS)
    if not target_uid:
        return {}
    for record in records:
        fields = record.get("fields") or {}
        if str(fields.get("UID") or "").strip() == target_uid:
            return record
    return None


def _get_record(access_token: str, app_token: str, table_id: str, record_id: str) -> dict[str, Any] | None:
    data = _request_json(
        "GET",
        f"{FEISHU_BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        access_token,
    )
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
    return record if _record_id(record) else None


def _filter_allowed_fields(fields: dict[str, Any], available_fields: set[str]) -> dict[str, Any]:
    return {
        name: value
        for name, value in fields.items()
        if name in REQUIRED_WRITABLE_FIELDS and name in available_fields
    }


def _field_mapping_report(fields: dict[str, Any], available_fields: set[str]) -> dict[str, list[str]]:
    requested = {name for name in fields if name in REQUIRED_WRITABLE_FIELDS}
    missing_required = sorted(name for name in requested if name not in available_fields)
    ignored = sorted(name for name in fields if name not in REQUIRED_WRITABLE_FIELDS or name not in available_fields)
    return {
        "requested_fields": sorted(requested),
        "available_writable_fields": sorted(name for name in available_fields if name in REQUIRED_WRITABLE_FIELDS),
        "missing_required_fields": missing_required,
        "ignored_fields": ignored,
    }


def _build_updates(
    mapped: dict[str, Any],
    target_record: dict[str, Any],
    options: dict[str, set[str]],
    overwrite_fields: set[str] | None = None,
) -> dict[str, Any]:
    current = target_record.get("fields") or {}
    filling_empty_session_row = not target_record or _is_empty(current.get(SESSION_FIELD))
    overwrite_allowed = set(overwrite_fields or set())
    if filling_empty_session_row:
        overwrite_allowed.update(REQUIRED_WRITABLE_FIELDS)
    else:
        overwrite_allowed.discard(SESSION_FIELD)
    updates: dict[str, Any] = {}
    for name, value in mapped.items():
        normalized = _normalize_option(name, value, options)
        if normalized in ("", None, [], {}):
            if name in ALWAYS_CLEAR_WHEN_EMPTY and name in overwrite_allowed:
                updates[name] = ""
            continue
        if name in overwrite_allowed or _is_empty(current.get(name)):
            updates[name] = normalized
    return updates


def _find_duplicate_record_for_search(
    access_token: str,
    app_token: str,
    table_id: str,
    target_record_id: str,
    mapped: dict[str, Any],
) -> dict[str, Any]:
    field_names = ["UID", SESSION_FIELD, "User Prompt", "杞", "浠诲姟绫诲瀷", "涓氬姟棰嗗煙"]
    session = str(mapped.get(SESSION_FIELD) or "").strip()
    if session:
        duplicate = _first_matching_record(
            _search_records(
                access_token,
                app_token,
                table_id,
                field_names=field_names,
                conditions=[{"field_name": SESSION_FIELD, "operator": "is", "value": [session]}],
                page_size=20,
            ),
            target_record_id,
            lambda fields: str(fields.get(SESSION_FIELD) or "").strip() == session,
        )
        if duplicate:
            return duplicate

    prompt = _normalized_duplicate_text(mapped.get("User Prompt"))
    round_label = _normalized_duplicate_text(mapped.get("杞"))
    if not prompt or not round_label:
        return {}
    task_type = _normalized_duplicate_text(mapped.get("浠诲姟绫诲瀷"))
    domain = _normalized_duplicate_text(mapped.get("涓氬姟棰嗗煙"))
    return _first_matching_record(
        _search_records(
            access_token,
            app_token,
            table_id,
            field_names=field_names,
            conditions=[
                {"field_name": "User Prompt", "operator": "is", "value": [str(mapped.get("User Prompt") or "")]},
                {"field_name": "杞", "operator": "is", "value": [str(mapped.get("杞") or "")]},
            ],
            page_size=50,
        ),
        target_record_id,
        lambda fields: (
            _normalized_duplicate_text(fields.get("User Prompt")) == prompt
            and _normalized_duplicate_text(fields.get("杞")) == round_label
            and (
                not task_type
                or not _normalized_duplicate_text(fields.get("浠诲姟绫诲瀷"))
                or _normalized_duplicate_text(fields.get("浠诲姟绫诲瀷")) == task_type
            )
            and (
                not domain
                or not _normalized_duplicate_text(fields.get("涓氬姟棰嗗煙"))
                or _normalized_duplicate_text(fields.get("涓氬姟棰嗗煙")) == domain
            )
        ),
    )


def _first_matching_record(
    records: list[dict[str, Any]],
    target_record_id: str,
    predicate,
) -> dict[str, Any]:
    for record in records:
        if _record_id(record) == target_record_id:
            continue
        fields = record.get("fields") or {}
        if predicate(fields):
            return record
    return {}


def _find_duplicate_record(
    records: list[dict[str, Any]],
    target_record_id: str,
    mapped: dict[str, Any],
) -> dict[str, Any]:
    session = str(mapped.get(SESSION_FIELD) or "").strip()
    if not session:
        return {}
    for record in records:
        if _record_id(record) == target_record_id:
            continue
        existing_session = str((record.get("fields") or {}).get(SESSION_FIELD) or "").strip()
        if existing_session and existing_session == session:
            return record

    prompt = _normalized_duplicate_text(mapped.get("User Prompt"))
    round_label = _normalized_duplicate_text(mapped.get("轮次"))
    if prompt and round_label:
        task_type = _normalized_duplicate_text(mapped.get("任务类型"))
        domain = _normalized_duplicate_text(mapped.get("业务领域"))
        for record in records:
            if _record_id(record) == target_record_id:
                continue
            fields = record.get("fields") or {}
            if _normalized_duplicate_text(fields.get("User Prompt")) != prompt:
                continue
            if _normalized_duplicate_text(fields.get("轮次")) != round_label:
                continue
            existing_task = _normalized_duplicate_text(fields.get("任务类型"))
            existing_domain = _normalized_duplicate_text(fields.get("业务领域"))
            task_matches = not task_type or not existing_task or task_type == existing_task
            domain_matches = not domain or not existing_domain or domain == existing_domain
            if task_matches and domain_matches:
                return record
    return {}


def _first_config_value(feishu_config: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(feishu_config.get(key) or "").strip()
        if value:
            return value
    return ""


def _overwrite_fields(feishu_config: dict[str, Any]) -> set[str]:
    value = feishu_config.get("overwrite_fields") or []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(item).strip() for item in value]
    else:
        raw_items = []
    return {item for item in raw_items if item in REQUIRED_WRITABLE_FIELDS}


def _normalized_duplicate_text(value: Any) -> str:
    if isinstance(value, list):
        value = ",".join(str(item) for item in value)
    return " ".join(str(value or "").strip().lower().split())


def _option_names(fields: list[dict[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for field in fields:
        names = {
            option.get("name")
            for option in (field.get("property") or {}).get("options", [])
            if option.get("name")
        }
        if names:
            result[str(field.get("field_name") or "")] = names
    return result


def _normalize_option(field_name: str, value: Any, options: dict[str, set[str]]) -> Any:
    if not isinstance(value, str):
        return value
    normalized = VALUE_ALIASES.get(field_name, {}).get(value, value)
    allowed = options.get(field_name)
    if allowed is None or not normalized:
        return normalized
    if normalized in allowed:
        return normalized
    if field_name == "业务领域" and "自动化与工具脚本" in allowed:
        return "自动化与工具脚本"
    if field_name == "修改范围" and "模块内多文件" in allowed:
        return "模块内多文件"
    return ""


def _request_json(
    method: str,
    url: str,
    access_token: str,
    params: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = httpx.request(
        method,
        url,
        params=params,
        json=json,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    try:
        data = response.json()
    except Exception as exc:
        raise FeishuWriteError(f"Feishu returned non-JSON response: HTTP {response.status_code}") from exc
    if response.status_code >= 400:
        raise _feishu_request_error(response.status_code, data, response.text, method, url)
    if data.get("code") != 0:
        raise _feishu_request_error(response.status_code, data, response.text, method, url)
    return data


def _feishu_request_error(
    status_code: int,
    data: dict[str, Any],
    response_text: str,
    method: str,
    url: str,
) -> FeishuWriteError:
    return FeishuWriteError(
        _format_feishu_error(status_code, data, response_text),
        status_code=status_code,
        code=data.get("code"),
        operation=_operation_from_url(method, url),
    )


def _format_feishu_error(status_code: int, data: dict[str, Any], response_text: str = "") -> str:
    code = data.get("code")
    msg = str(data.get("msg") or response_text[:200] or "Feishu request failed.")
    prefix = f"HTTP {status_code}: " if status_code >= 400 else ""
    if status_code == 403 or str(code) in {"91403", "99991663"}:
        return (
            f"{prefix}Feishu permission denied: code={code}, msg={msg}. "
            "Please reauthorize Feishu user OAuth or add the app as a collaborator "
            "with readable/writable access to this Bitable base/table."
        )
    if "field" in msg.lower() or "字段" in msg:
        return f"{prefix}Feishu field mapping failed: code={code}, msg={msg}"
    return f"{prefix}code={code}, msg={msg}"


def _operation_from_url(method: str, url: str) -> str:
    method = method.upper()
    if "/fields" in url:
        return "list_fields"
    if "/views" in url:
        return "list_views"
    if "/records" in url and method == "GET":
        return "list_records"
    if "/records" in url and method == "POST":
        return "create_record"
    if "/records" in url and method == "PUT":
        return "update_record"
    return method.lower()


def _prepare_attachment_field(
    access_token: str,
    app_token: str,
    mapped: dict[str, Any],
    available_fields: set[str],
) -> dict[str, Any]:
    if ATTACHMENT_FIELD not in mapped or ATTACHMENT_FIELD not in available_fields:
        return mapped
    raw_value = mapped.get(ATTACHMENT_FIELD)
    raw_items = raw_value if isinstance(raw_value, list) else [raw_value]
    items: list[dict[str, str]] = []
    seen_tokens: set[str] = set()
    for item in raw_items:
        file_token = ""
        if isinstance(item, dict):
            file_token = str(item.get("file_token") or "").strip()
        elif isinstance(item, str) and item.strip():
            file_token = _upload_bitable_attachment(access_token, app_token, item.strip())
        if file_token and file_token not in seen_tokens:
            items.append({"file_token": file_token})
            seen_tokens.add(file_token)
    if items:
        mapped = dict(mapped)
        mapped[ATTACHMENT_FIELD] = items
    else:
        mapped = dict(mapped)
        mapped.pop(ATTACHMENT_FIELD, None)
    return mapped


def _upload_bitable_attachment(access_token: str, app_token: str, file_path: str) -> str:
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        raise FeishuWriteError(f"Attachment file not found: {path}")
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    preferred = "bitable_image" if mime_type.startswith("image/") else "bitable_file"
    fallbacks = [preferred, "bitable_file" if preferred == "bitable_image" else "bitable_image"]
    last_error = ""
    for parent_type in fallbacks:
        with path.open("rb") as file_obj:
            response = httpx.post(
                f"{FEISHU_BASE_URL}/drive/v1/medias/upload_all",
                headers={"Authorization": f"Bearer {access_token}"},
                data={
                    "file_name": path.name,
                    "parent_type": parent_type,
                    "parent_node": app_token,
                    "size": str(path.stat().st_size),
                },
                files={"file": (path.name, file_obj, mime_type)},
                timeout=60,
            )
        try:
            data = response.json()
        except Exception as exc:
            raise FeishuWriteError(f"Feishu attachment upload returned non-JSON response: HTTP {response.status_code}") from exc
        if response.status_code >= 400 or data.get("code") != 0:
            last_error = f"HTTP {response.status_code}: code={data.get('code')}, msg={data.get('msg') or response.text[:200]}"
            continue
        file_token = str(((data.get("data") or {}).get("file_token") or "")).strip()
        if file_token:
            return file_token
        last_error = f"No file_token in upload response: {data}"
    raise FeishuWriteError(f"Attachment upload failed: {last_error}")


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _record_id(record: dict[str, Any] | None) -> str:
    if not record:
        return ""
    return str(record.get("record_id") or record.get("id") or "")
