from dataclasses import dataclass, field
import re
from typing import Any


PRODUCT_LABEL = "产物不满意："
PROCESS_LABEL = "过程不满意："


@dataclass(frozen=True)
class DissatisfactionEvidence:
    failure_stage: str
    failure_message: str
    command_type: str = ""
    result_status: str = ""
    prompt: str = ""
    trace_text: str = ""
    screenshot_path: str = ""
    runtime_log_text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def generate_dissatisfaction_reason(evidence: DissatisfactionEvidence) -> dict[str, Any]:
    product = _product_reason(evidence)
    process = _process_reason(evidence)
    reason = f"{PRODUCT_LABEL}{product}\n{PROCESS_LABEL}{process}"
    return {
        "status": "generated",
        "reason": reason,
        "product_reason": product,
        "process_reason": process,
        "failure_stage": evidence.failure_stage,
        "evidence_summary": _evidence_summary(evidence),
    }


def sanitize_reason_phrase(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"For more information check:\s*https?://\S+", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"HTTP\s*403[^。；;\n]*",
        "飞书接口返回 403，当前应用权限或表格可写位置不满足写入要求",
        text,
        flags=re.IGNORECASE,
    )
    replacements = {
        "Dissatisfaction reason generated from completed trace and failure evidence.": "",
        "关键证据：": "具体问题是：",
        "关键证据": "具体问题",
        "判定依据：": "",
        "判定依据": "",
        "failure evidence": "失败信息",
        "Worker": "本地执行环境",
        "worker": "本地执行环境",
        "Trae CN": "模型",
        "Trae": "模型",
        "command": "命令",
        "manual review": "人工复查",
        "manual intervention": "人工接管",
        "Browser acceptance": "浏览器验收",
        "Git submission": "GitHub 提交",
        "Feishu write": "飞书写入",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"\b(status|code|msg|returncode)\s*[:=]\s*", "", text, flags=re.IGNORECASE)
    text = " ".join(text.split()).strip(" ，。；;")
    return text


def _product_reason(evidence: DissatisfactionEvidence) -> str:
    stage = evidence.failure_stage
    key_lines = _key_lines(evidence)
    domain = _domain_hint(evidence.prompt)
    detail = _join_evidence(key_lines) or sanitize_reason_phrase(evidence.failure_message)

    if stage == "product_reviewing":
        if not detail:
            detail = f"这轮要验收的是{domain}，但构建、测试或静态检查没有留下能通过的结果"
        return f"{detail}，所以这版还不能当作完成的项目结果提交。"
    if stage == "browser_accepting":
        if not detail:
            detail = "浏览器验收没有拿到可用页面或主流程反馈"
        return f"{detail}，我还不能确认{domain}的主要入口、状态变化和结果反馈已经真实跑通。"
    if stage == "github_submitting":
        if not detail:
            detail = "代码仓库同步没有成功"
        return f"{detail}，代码成果没有形成可追溯的提交和远端仓库结果，交付闭环不完整。"
    if stage in {"feishu_writing", "feishu_failed_abort"}:
        if not detail:
            detail = "飞书记录没有写入成功"
        return f"{detail}，虽然前面的代码流程已经推进到记录阶段，但最终业务记录没有完成入表。"
    if not detail:
        detail = "当前结果没有收束到可复查的完成状态"
    return f"{detail}，这版还没有达到可验收、可提交、可记录的完成标准。"


def _process_reason(evidence: DissatisfactionEvidence) -> str:
    stage = _stage_label(evidence.failure_stage)
    detail = sanitize_reason_phrase(evidence.failure_message) or "没有拿到明确的成功结果"
    trace_note = (
        "模型的完整回复已经拿到，但后续复查或提交没有完成闭环。"
        if evidence.trace_text
        else "当前缺少足够完整的模型回复轨迹支撑继续提交。"
    )
    screenshot_note = "我这边也保留了当时截图，方便回看页面状态。" if evidence.screenshot_path else ""
    return f"我按本轮需求复查到{stage}时没有收口，{detail}。{trace_note}{screenshot_note}".strip()


def _evidence_summary(evidence: DissatisfactionEvidence) -> dict[str, Any]:
    return {
        "failure_stage": evidence.failure_stage,
        "failure_message": evidence.failure_message,
        "command_type": evidence.command_type,
        "result_status": evidence.result_status,
        "trace_chars": len(evidence.trace_text),
        "has_screenshot": bool(evidence.screenshot_path),
        "data_keys": sorted(evidence.data.keys()),
    }


def _key_lines(evidence: DissatisfactionEvidence) -> list[str]:
    values: list[str] = []
    data = evidence.data if isinstance(evidence.data, dict) else {}
    for key in ("stderr", "stdout", "message", "error"):
        raw = data.get(key)
        if isinstance(raw, str) and raw.strip():
            values.extend(_clean_lines(raw))
    for key in ("status", "http_status", "returncode"):
        if key in data and data.get(key) not in ("", None):
            values.append(f"{key}: {data[key]}")
    review = data.get("product_review")
    if isinstance(review, dict):
        for key in ("issues", "warnings", "evidence"):
            raw_items = review.get(key)
            if isinstance(raw_items, list):
                values.extend(_clean_lines("\n".join(str(item) for item in raw_items if item)))
    inspection = data.get("inspection")
    if isinstance(inspection, dict):
        for key in ("issues", "warnings"):
            raw_items = inspection.get(key)
            if isinstance(raw_items, list):
                values.extend(_clean_lines("\n".join(str(item) for item in raw_items if item)))
    if not values and evidence.runtime_log_text:
        values.extend(_clean_lines(evidence.runtime_log_text))
    return values[:4]


def _clean_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        item = " ".join(line.strip().split())
        if item:
            lines.append(item[:240])
    return lines


def _join_evidence(lines: list[str]) -> str:
    cleaned = [sanitize_reason_phrase(line).strip("。；; ") for line in lines if sanitize_reason_phrase(line).strip()]
    if not cleaned:
        return ""
    return "我复查到的具体问题是：" + "；".join(cleaned)


def _stage_label(stage: str) -> str:
    labels = {
        "product_reviewing": "项目检查这一步",
        "browser_accepting": "浏览器验收这一步",
        "github_submitting": "提交 GitHub 这一步",
        "feishu_writing": "写入飞书这一步",
        "feishu_failed_abort": "写入飞书这一步",
        "trace_missing_abort": "获取模型回复轨迹这一步",
    }
    return labels.get(stage, str(stage or "当前步骤"))


def _domain_hint(prompt: str) -> str:
    text = str(prompt or "")
    if any(term in text for term in ["AgentOps", "自动作业平台", "角色工作台", "模型配置", "提示发送"]):
        return "任务轮次、角色配置、模型参数、提示发送、代码审查、浏览器验收和记录闭环"
    if any(term in text for term in ["TMC", "tmc", "快递", "骑手", "取件", "派送", "异常件"]):
        return "快递下单、网点接单、骑手取派、异常件处理和时效统计"
    if any(term in text for term in ["物流", "运单", "线路", "车辆", "装车", "签收", "回单"]):
        return "运单、线路调度、在途轨迹、签收回单和异常流转"
    if any(term in text for term in ["仓储", "入库", "出库", "库位", "盘点", "批次", "库存"]):
        return "入库、出库、库位、库存批次和盘点流程"
    if any(term in text for term in ["社区", "帖子", "举报", "通知", "频道", "消息"]):
        return "帖子、消息、举报审核、通知联动和用户互动"
    if any(term in text for term in ["审批", "权限", "角色", "申请"]):
        return "申请流转、审批节点、角色权限和操作记录"
    return "核心业务流程、页面入口、数据状态和异常反馈"
