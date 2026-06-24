from pathlib import Path
from typing import Any
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, User, UserConfig
from app.services.feishu.local_writer import _local_record_path
from app.services.user_settings import load_user_settings

LOCAL_FEISHU_FIELD_ORDER = [
    "Trae Session ID",
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
    "截图（userprompt附件/产物/运行结果/对话）",
]


def list_local_feishu_records(db: Session, user: User, *, limit: int = 200) -> dict[str, Any]:
    limit = max(1, min(int(limit or 200), 1000))
    records = _read_records(_record_paths_for_user(db, user))
    job_ids = {item["job_id"] for item in records if item["job_id"]}
    owners = _job_owner_map(db, job_ids)

    visible: list[dict[str, Any]] = []
    for item in records:
        owner = owners.get(item["job_id"], {})
        if user.role != "admin" and item["job_id"] and owner.get("user_id") != user.id:
            continue
        if user.role != "admin" and not item["job_id"]:
            continue
        visible.append({**item, **owner})

    visible.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "fields": LOCAL_FEISHU_FIELD_ORDER,
        "records": visible[:limit],
        "paths": [str(path) for path in _record_paths_for_user(db, user)],
    }


def _record_paths_for_user(db: Session, user: User) -> list[Path]:
    configs: list[dict[str, Any]] = []
    if user.role == "admin":
        configs.append({})
        rows = db.scalars(select(UserConfig).where(UserConfig.category == "feishu")).all()
        configs.extend(row.data or {} for row in rows)
    else:
        configs.append(load_user_settings(db, user.id).get("feishu", {}))

    paths: list[Path] = []
    seen: set[str] = set()
    for config in configs:
        path = _local_record_path(config)
        key = str(path)
        if key not in seen:
            paths.append(path)
            seen.add(key)
    return paths


def _read_records(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
            metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            records.append(
                {
                    "record_id": str(raw.get("record_id") or f"{path.name}:{line_number}"),
                    "created_at": str(raw.get("created_at") or ""),
                    "fields": fields,
                    "metadata": metadata,
                    "job_id": str(metadata.get("job_id") or ""),
                    "round_id": str(metadata.get("round_id") or ""),
                    "source_path": str(path),
                    "line_number": line_number,
                }
            )
    return records


def _job_owner_map(db: Session, job_ids: set[str]) -> dict[str, dict[str, str]]:
    if not job_ids:
        return {}
    jobs = db.scalars(select(Job).where(Job.id.in_(job_ids))).all()
    return {job.id: {"user_id": job.user_id} for job in jobs}
