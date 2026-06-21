from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.db.models import User, Worker
from app.db.models.base import now_utc
from app.db.repositories.workers import get_worker_by_worker_id
from app.services.user_settings import load_user_settings, safe_open_secret
from app.worker_gateway.contracts import WorkerCommandType


LOCAL_BROWSER_HOSTS = {"localhost", "127.0.0.1", "::1"}

REQUIRED_WORKER_CAPABILITIES = {
    WorkerCommandType.SEND_PROMPT.value,
    WorkerCommandType.WAIT_COMPLETION.value,
    WorkerCommandType.CLICK_CONTINUE.value,
    WorkerCommandType.COPY_LATEST_REPLY.value,
    WorkerCommandType.CAPTURE_SCREENSHOT.value,
    WorkerCommandType.SCAN_PROJECT.value,
    WorkerCommandType.RUN_COMMAND.value,
    WorkerCommandType.BROWSER_ACCEPTANCE.value,
    WorkerCommandType.GIT_SUBMIT.value,
}


@dataclass(frozen=True)
class PreflightCheck:
    key: str
    label: str
    status: str
    message: str
    required: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "message": self.message,
            "required": self.required,
            "details": self.details,
        }


def build_preflight(db: Session, user: User) -> dict[str, Any]:
    configs = load_user_settings(db, user.id)
    model = configs.get("model", {})
    github = configs.get("github", {})
    feishu = configs.get("feishu", {})
    webhook = configs.get("webhook", {})
    worker_settings = configs.get("worker", {})
    defaults = configs.get("defaults", {})

    worker_id = str(worker_settings.get("worker_id") or "").strip()
    worker = get_worker_by_worker_id(db, worker_id) if worker_id else None
    checks: list[PreflightCheck] = [
        _secret_check("model.api_key", "模型 API Key", model.get("api_key")),
        _value_check(
            "model.model_name",
            "模型名称",
            model.get("model_name") or model.get("model"),
            missing_message="缺少默认模型名称，Prompt 生成会在调用模型前失败。",
        ),
        _worker_binding_check(user, worker_id, worker),
        _worker_status_check(worker),
        _worker_capability_check(worker),
        _value_check(
            "worker.trae_exe_path",
            "Trae 安装路径",
            worker_settings.get("trae_exe_path"),
            missing_message="缺少 Worker 本机 Trae 安装路径，Worker 无法可靠打开 Trae CN。",
        ),
        _worker_runtime_trae_path_check(worker, worker_settings.get("trae_exe_path")),
        _value_check(
            "worker.trae_workspace_path",
            "Trae 工作目录",
            worker_settings.get("trae_workspace_path"),
            missing_message="缺少 Worker 侧 Trae 工作目录，无法打开目标项目并扫描/提交产物。",
        ),
        _browser_url_check(worker_settings.get("browser_url")),
        _secret_check(
            "github.token",
            "GitHub Token",
            github.get("token"),
            required=False,
            missing_message="未配置服务端 GitHub Token；当前 git_submit 会使用 Worker 本机 git 凭证。",
        ),
        _value_check(
            "feishu.app_id",
            "飞书 App ID",
            feishu.get("app_id"),
            missing_message="缺少飞书 App ID，完成 Git 提交后无法写入飞书记录。",
        ),
        _secret_check(
            "feishu.app_secret",
            "飞书 App Secret",
            feishu.get("app_secret"),
            missing_message="缺少飞书 App Secret，无法刷新飞书 tenant_access_token。",
        ),
        _value_check(
            "feishu.app_token",
            "飞书 Base/App Token",
            feishu.get("app_token") or feishu.get("base_token") or feishu.get("bitable_app_token"),
            missing_message="缺少飞书 Base/App Token，无法定位要写入的多维表格。",
        ),
        _value_check(
            "feishu.table_id",
            "飞书 Table ID",
            feishu.get("table_id"),
            missing_message="缺少飞书 Table ID，无法创建验收记录。",
        ),
        _value_check(
            "feishu.view_id",
            "飞书 View ID",
            feishu.get("view_id"),
            required=False,
            missing_message="未配置飞书 View ID；写入记录时会使用表格默认视图。",
        ),
        _value_check(
            "webhook.url",
            "Webhook",
            webhook.get("url"),
            required=False,
            missing_message="未配置 Webhook；当前主链路不依赖它。",
        ),
        _value_check(
            "defaults.default_rule_version_id",
            "默认规则版本",
            defaults.get("default_rule_version_id"),
            required=False,
            missing_message="未选择默认规则版本；启动时会继续使用当前激活规则版本。",
        ),
    ]

    checks = _apply_worker_runtime_trae_path_fallback(checks, worker, worker_settings.get("trae_exe_path"))
    serialized = [item.as_dict() for item in checks]
    blocking = [item.label for item in checks if item.required and item.status == "fail"]
    warnings = [item.label for item in checks if item.status == "warning"]
    return {
        "ready": not blocking,
        "blocking": blocking,
        "warnings": warnings,
        "checks": serialized,
        "summary": _summary(blocking, warnings),
    }


def _secret_check(
    key: str,
    label: str,
    value: Any,
    required: bool = True,
    missing_message: str = "缺少必需密钥。",
) -> PreflightCheck:
    if safe_open_secret(value):
        return PreflightCheck(key, label, "pass", "已配置。", required)
    status = "fail" if required else "warning"
    return PreflightCheck(key, label, status, missing_message, required)


def _value_check(
    key: str,
    label: str,
    value: Any,
    required: bool = True,
    missing_message: str = "缺少必需配置。",
) -> PreflightCheck:
    if str(value or "").strip():
        return PreflightCheck(key, label, "pass", "已配置。", required)
    status = "fail" if required else "warning"
    return PreflightCheck(key, label, status, missing_message, required)


def _apply_worker_runtime_trae_path_fallback(
    checks: list[PreflightCheck],
    worker: Worker | None,
    configured_path: Any,
) -> list[PreflightCheck]:
    if str(configured_path or "").strip():
        return checks
    runtime = worker.runtime_status if worker and isinstance(worker.runtime_status, dict) else {}
    if runtime.get("trae_exe_exists") is not True:
        return checks
    worker_path = str(runtime.get("trae_exe_resolved_path") or runtime.get("trae_exe_path") or "").strip()
    if not worker_path:
        return checks
    return [
        PreflightCheck(
            item.key,
            item.label,
            "pass",
            f"Worker 已上报本机 Trae 可执行文件：{worker_path}",
            item.required,
            {
                **item.details,
                "source": "worker_runtime_status",
                "worker_path": worker_path,
            },
        )
        if item.key == "worker.trae_exe_path"
        else item
        for item in checks
    ]


def _worker_binding_check(user: User, worker_id: str, worker: Worker | None) -> PreflightCheck:
    if not worker_id:
        return PreflightCheck("worker.worker_id", "关联 Worker", "fail", "缺少当前用户绑定的 Worker。")
    if not worker:
        return PreflightCheck("worker.worker_id", "关联 Worker", "fail", f"找不到 Worker：{worker_id}。")
    if worker.user_id != user.id:
        return PreflightCheck(
            "worker.worker_id",
            "关联 Worker",
            "fail",
            f"Worker {worker_id} 未绑定到当前用户。",
            details={"worker_id": worker_id, "worker_user_id": worker.user_id},
        )
    return PreflightCheck("worker.worker_id", "关联 Worker", "pass", f"已绑定 Worker：{worker_id}。")


def _worker_status_check(worker: Worker | None) -> PreflightCheck:
    if not worker:
        return PreflightCheck("worker.status", "Worker 在线状态", "fail", "无法检查 Worker 状态。")
    status = _effective_worker_status(worker)
    details = {"worker_id": worker.worker_id, "status": status, "busy": bool(worker.busy)}
    if status == "online" and not worker.busy:
        return PreflightCheck("worker.status", "Worker 在线状态", "pass", "Worker 在线且空闲。", details=details)
    if status in {"online", "busy"}:
        return PreflightCheck(
            "worker.status",
            "Worker 在线状态",
            "warning",
            "Worker 在线但当前忙碌，任务可能需要排队。",
            details=details,
        )
    return PreflightCheck(
        "worker.status",
        "Worker 在线状态",
        "fail",
        f"Worker 当前不可用：{status}。",
        details=details,
    )


def _worker_capability_check(worker: Worker | None) -> PreflightCheck:
    if not worker:
        return PreflightCheck("worker.capabilities", "Worker 能力", "fail", "无法检查 Worker 能力。")
    capabilities = set(worker.capabilities or [])
    missing = sorted(REQUIRED_WORKER_CAPABILITIES - capabilities)
    if not missing:
        return PreflightCheck(
            "worker.capabilities",
            "Worker 能力",
            "pass",
            "Worker 支持 Trae、追踪、产物检查、浏览器验收和 Git 提交命令。",
            details={"capabilities": sorted(capabilities)},
        )
    return PreflightCheck(
        "worker.capabilities",
        "Worker 能力",
        "fail",
        "Worker 缺少真实运行所需命令能力。",
        details={"missing": missing, "capabilities": sorted(capabilities)},
    )


def _worker_runtime_trae_path_check(worker: Worker | None, configured_path: Any) -> PreflightCheck:
    if not worker or not str(configured_path or "").strip():
        return PreflightCheck(
            "worker.runtime.trae_exe_path",
            "Worker Trae 路径校验",
            "warning",
            "Worker 尚未回报 Trae 路径校验结果。",
            required=False,
        )
    runtime = worker.runtime_status if isinstance(worker.runtime_status, dict) else {}
    if not runtime:
        return PreflightCheck(
            "worker.runtime.trae_exe_path",
            "Worker Trae 路径校验",
            "warning",
            "Worker 下一次心跳后会回报本机 Trae 路径是否存在。",
            required=False,
            details={"configured_path": str(configured_path or "")},
        )
    details = {
        "configured_path": str(configured_path or ""),
        "worker_path": str(runtime.get("trae_exe_path") or ""),
        "resolved_path": str(runtime.get("trae_exe_resolved_path") or ""),
        "candidates": runtime.get("trae_exe_candidates") if isinstance(runtime.get("trae_exe_candidates"), list) else [],
    }
    if runtime.get("trae_exe_exists") is True:
        return PreflightCheck(
            "worker.runtime.trae_exe_path",
            "Worker Trae 路径校验",
            "pass",
            "Worker 已确认本机 Trae 可执行文件存在。",
            required=False,
            details=details,
        )
    return PreflightCheck(
        "worker.runtime.trae_exe_path",
        "Worker Trae 路径校验",
        "warning",
        "Worker 当前找不到配置的 Trae 可执行文件，请确认个人配置里的安装路径。",
        required=False,
        details=details,
    )


def _browser_url_check(value: Any) -> PreflightCheck:
    raw_url = str(value or "").strip()
    if not raw_url:
        return PreflightCheck(
            "worker.browser_url",
            "浏览器验收 URL",
            "fail",
            "缺少本地浏览器验收 URL，产物检查后无法做真实 HTTP 验收。",
        )
    normalized_url = raw_url if "://" in raw_url else f"http://{raw_url}"
    parsed = urlparse(normalized_url)
    host = parsed.hostname or ""
    details = {"url": normalized_url, "host": host, "scheme": parsed.scheme}
    if parsed.scheme in {"http", "https"} and host in LOCAL_BROWSER_HOSTS:
        return PreflightCheck(
            "worker.browser_url",
            "浏览器验收 URL",
            "pass",
            "已配置本地 HTTP 验收地址。",
            details=details,
        )
    return PreflightCheck(
        "worker.browser_url",
        "浏览器验收 URL",
        "fail",
        "浏览器验收目前只支持 localhost、127.0.0.1 或 ::1 的 HTTP/HTTPS 地址。",
        details=details,
    )


def _effective_worker_status(worker: Worker) -> str:
    if worker.revoked_at:
        return "revoked"
    if not worker.last_seen_at:
        return "offline"
    current = now_utc()
    last_seen = worker.last_seen_at
    if last_seen.tzinfo is None:
        current = current.replace(tzinfo=None)
    if current - last_seen > timedelta(minutes=2):
        return "offline"
    return str(worker.status or "offline")


def _summary(blocking: list[str], warnings: list[str]) -> str:
    if blocking:
        return f"运行前清单未通过：{', '.join(blocking)}。"
    if warnings:
        return f"运行前清单可运行，但有提醒项：{', '.join(warnings)}。"
    return "运行前清单已通过，可以开始真实运行。"
