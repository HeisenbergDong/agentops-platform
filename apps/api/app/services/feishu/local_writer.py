from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
import json

from app.core.config import settings

DEFAULT_LOCAL_FEISHU_RECORD = "local-feishu-records/records.jsonl"


def write_local_feishu_record(
    feishu_config: dict[str, Any],
    fields: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = _local_record_path(feishu_config)
    path.parent.mkdir(parents=True, exist_ok=True)

    record_id = f"local_{uuid4().hex}"
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "record_id": record_id,
        "fields": fields,
        "metadata": metadata or {},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str))
        handle.write("\n")

    return {
        "status": "local_written",
        "record_id": record_id,
        "operation": "local_file_write",
        "local_file_path": str(path),
    }


def _local_record_path(feishu_config: dict[str, Any]) -> Path:
    configured = str(
        feishu_config.get("local_file_path")
        or feishu_config.get("local_record_path")
        or feishu_config.get("local_records_path")
        or ""
    ).strip()
    if configured:
        path = Path(configured)
        if path.is_absolute():
            return path
        return _local_storage_root() / path
    return _local_storage_root() / DEFAULT_LOCAL_FEISHU_RECORD


def _local_storage_root() -> Path:
    root = settings.attachment_root
    if root.is_absolute():
        return root
    return settings.repo_root / root
