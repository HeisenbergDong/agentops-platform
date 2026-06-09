from dataclasses import dataclass, field
from typing import Any


PRODUCT_LABEL = "\u4ea7\u7269\u4e0d\u6ee1\u610f\uff1a"
PROCESS_LABEL = "\u8fc7\u7a0b\u4e0d\u6ee1\u610f\uff1a"


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


def _product_reason(evidence: DissatisfactionEvidence) -> str:
    stage = evidence.failure_stage
    key_lines = _key_lines(evidence)
    if stage == "product_reviewing":
        detail = _join_evidence(key_lines) or "自动构建或测试没有通过，当前代码产物不能证明已经满足任务要求。"
        return f"{detail} 因此本轮产物不能作为完成结果提交。"
    if stage == "browser_accepting":
        detail = _join_evidence(key_lines) or evidence.failure_message
        return f"{detail} 页面验收没有形成可用证据，当前产物不能证明具备可交付的运行效果。"
    if stage == "github_submitting":
        detail = _join_evidence(key_lines) or evidence.failure_message
        return f"{detail} 代码成果没有形成可追踪的提交或推送结果，不能作为最终交付物闭环。"
    if stage == "feishu_writing" or stage == "feishu_failed_abort":
        detail = _join_evidence(key_lines) or evidence.failure_message
        return f"{detail} 产物虽已通过前置验收，但业务记录没有写入成功，交付结果不完整。"
    detail = _join_evidence(key_lines) or evidence.failure_message
    return f"{detail} 当前结果没有达到可验收、可提交或可记录的完成标准。"


def _process_reason(evidence: DissatisfactionEvidence) -> str:
    trace_note = "已基于完整模型回复、运行证据和流程日志生成原因。" if evidence.trace_text else "已基于当前流程日志和失败证据生成原因。"
    screenshot_note = f" 截图证据：{evidence.screenshot_path}。" if evidence.screenshot_path else ""
    return (
        f"模型回复完成后的自动流程在 {evidence.failure_stage} 阶段失败，"
        f"{evidence.failure_message}{screenshot_note} {trace_note}"
    )


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
    if not lines:
        return ""
    return "关键证据：" + "；".join(lines)
