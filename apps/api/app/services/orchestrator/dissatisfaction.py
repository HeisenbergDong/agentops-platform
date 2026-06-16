from dataclasses import dataclass, field
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RuntimeLog, User
from app.db.repositories.roles import get_user_role
from app.services.llm import LLMClient, LLMError, model_config_from_settings
from app.services.user_settings import load_user_settings


PRODUCT_LABEL = "产物不满意："
PROCESS_LABEL = "过程不满意："
TASK_DONE_INCOMPLETE = "未完成任务"
TASK_DONE_OPTIONS = {"完成了任务", TASK_DONE_INCOMPLETE}
REVIEWER_SYSTEM = """你是自动化作业里的“验收员”。你不能编造点击、截图、运行结果，也不能直接写飞书；只根据输入证据给不满意原因提案。
要求：
1. 必须区分产物问题和过程问题。
2. 只能引用输入中存在的代码路径、命令结果、日志、文件变更和审查证据。
3. 没有浏览器实测证据时，不能写“我点了/我点到/实际点击后”。
4. 如果没有真实日志轨迹，应明确不能验真，不要把本地恢复摘要当日志。
5. 如果日志轨迹因字段过长保存为 txt 附件，且上下文标明是已校验的真实原始日志轨迹，不得写“没有真实日志轨迹”或“只有超长占位说明”。
6. 不满意原因要贴近当前业务系统，避免连续多轮同一句模板。
只输出 JSON：{"task_done": "完成了任务|未完成任务", "satisfaction": "满意|不满意", "product_reason": "...", "process_reason": "...", "evidence_refs": ["..."], "confidence": 0.0}"""

REVIEWER_SYSTEM += """

AgentOps SOP context:
- Formal GitHub/Feishu business records require a verified full Trae assistant trace. Without it, describe the process gap; do not invent trace, commit, or Feishu evidence.
- If a Worker stop report is the evidence, separate local stop/cleanup process evidence from product quality. Stop success or failure is not automatically a product bug.
- If test mode continues after incomplete trace, label the record as a test exception and never present it as formal business acceptance.
""".strip()


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
    orchestrator_intent: dict[str, Any] = field(default_factory=dict)


def generate_dissatisfaction_reason(
    evidence: DissatisfactionEvidence,
    *,
    db: Session | None = None,
    user: User | None = None,
    previous_reason: str = "",
) -> dict[str, Any]:
    forced = _forced_test_unsatisfied(evidence)
    if forced:
        return _finalize_reason(forced, evidence, previous_reason=previous_reason)
    product = _product_reason(evidence)
    process = _process_reason(evidence)
    rule_result = _finalize_reason(
        {
            "status": "generated",
            "reason": f"{PRODUCT_LABEL}{product}\n{PROCESS_LABEL}{process}",
            "product_reason": product,
            "process_reason": process,
            "task_done": TASK_DONE_INCOMPLETE,
            "satisfaction": "不满意",
            "failure_stage": evidence.failure_stage,
            "evidence_summary": _evidence_summary(evidence),
        },
        evidence,
        previous_reason=previous_reason,
    )
    llm_result = _reviewer_reason(db, user, evidence, rule_result, previous_reason)
    if llm_result:
        return llm_result
    return rule_result


def _forced_test_unsatisfied(evidence: DissatisfactionEvidence) -> dict[str, Any] | None:
    intent = evidence.orchestrator_intent if isinstance(evidence.orchestrator_intent, dict) else {}
    if intent.get("run_mode") != "test" or intent.get("dissatisfaction_policy") != "force_test_unsatisfied":
        return None
    product = "本轮是链路验证测试，即使产物结果可接受，也按用户要求标记为不满意，用于验证后续 GitHub 和飞书记录链路，不能当作正式业务验收结论。"
    process = "本轮重点不是评估真实业务产物质量，而是确认提示发送、执行观察、提交和写入等自动化流程是否能被完整记录。"
    return {
        "status": "generated",
        "reason": f"{PRODUCT_LABEL}{product}\n{PROCESS_LABEL}{process}",
        "product_reason": product,
        "process_reason": process,
        "task_done": TASK_DONE_INCOMPLETE,
        "satisfaction": "不满意",
        "failure_stage": evidence.failure_stage,
        "evidence_summary": _evidence_summary(evidence),
        "orchestrator_intent": intent,
        "test_mode": True,
    }


def _reviewer_reason(
    db: Session | None,
    user: User | None,
    evidence: DissatisfactionEvidence,
    rule_result: dict[str, Any],
    previous_reason: str,
) -> dict[str, Any] | None:
    if not db or not user:
        return None
    role = get_user_role(db, user.id, "dissatisfaction_writer")
    if role and not role.enabled:
        return None
    model_key = role.model_config_key if role else "default"
    messages = [
        {"role": "system", "content": REVIEWER_SYSTEM},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "role": "reviewer",
                    "hard_rules": {
                        "must_not_fabricate_clicks": True,
                        "must_not_use_local_recovery_as_log_trace": True,
                        "must_reference_existing_evidence_only": True,
                        "formal_write_requires_real_trace": True,
                        "test_mode_must_be_labeled": True,
                    },
                    "context": _compact_evidence_for_reviewer(evidence, rule_result, previous_reason),
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        result = LLMClient().complete(
            model_config_from_settings(load_user_settings(db, user.id), model_key),
            messages,
            purpose="dissatisfaction_reason",
        )
        proposal = _parse_json_object(result.text)
    except (LLMError, Exception) as exc:
        rule_result["llm_reviewer_error"] = str(exc)[:500]
        return None

    accepted = _accept_reviewer_proposal(proposal, evidence, previous_reason)
    if not accepted:
        rule_result["llm_reviewer"] = {
            "accepted": False,
            "rejected": _reviewer_reject_reason(proposal, evidence, previous_reason),
            "model": result.model,
            "wire_api": result.wire_api,
        }
        return None

    product_reason = _strip_reason_role_prefix(sanitize_reason_phrase(proposal.get("product_reason") or ""))
    process_reason = _strip_reason_role_prefix(sanitize_reason_phrase(proposal.get("process_reason") or ""))
    llm_generated = _finalize_reason(
        {
            "status": "generated",
            "reason": f"{PRODUCT_LABEL}{_sentence(product_reason)}\n{PROCESS_LABEL}{_sentence(process_reason)}",
            "product_reason": product_reason,
            "process_reason": process_reason,
            "task_done": str(proposal.get("task_done") or TASK_DONE_INCOMPLETE),
            "satisfaction": "不满意",
            "failure_stage": evidence.failure_stage,
            "evidence_summary": _evidence_summary(evidence),
            "llm_reviewer": {
                "accepted": True,
                "model": result.model,
                "wire_api": result.wire_api,
                "confidence": proposal.get("confidence"),
                "evidence_refs": proposal.get("evidence_refs") or [],
            },
        },
        evidence,
        previous_reason=previous_reason,
    )
    return llm_generated


def _finalize_reason(result: dict[str, Any], evidence: DissatisfactionEvidence, previous_reason: str = "") -> dict[str, Any]:
    reason = _normalize_dissatisfaction_prefix(str(result.get("reason") or ""))
    lines = [line.strip() for line in reason.splitlines() if line.strip()]
    product_line = next((line for line in lines if line.startswith(PRODUCT_LABEL)), "")
    process_line = next((line for line in lines if line.startswith(PROCESS_LABEL)), "")
    if not product_line or _too_generic_reason(product_line.replace(PRODUCT_LABEL, "", 1), evidence.prompt):
        product_line = PRODUCT_LABEL + _sentence(_product_reason(evidence))
    if not process_line or _too_generic_reason(process_line.replace(PROCESS_LABEL, "", 1), evidence.prompt):
        process_line = PROCESS_LABEL + _sentence(_process_reason(evidence))
    reason = _normalize_dissatisfaction_prefix(product_line + "\n" + process_line)
    if previous_reason and _reason_is_too_similar(reason, previous_reason):
        fallback = _current_round_fallback_reason(evidence)
        reason = _normalize_dissatisfaction_prefix(fallback)
        product_line = next((line for line in reason.splitlines() if line.startswith(PRODUCT_LABEL)), product_line)
        process_line = next((line for line in reason.splitlines() if line.startswith(PROCESS_LABEL)), process_line)
    return {
        "reason": reason,
        "product_reason": product_line.replace(PRODUCT_LABEL, "", 1).strip(),
        "process_reason": process_line.replace(PROCESS_LABEL, "", 1).strip(),
        "task_done": TASK_DONE_INCOMPLETE,
        "satisfaction": "不满意",
        **{key: value for key, value in result.items() if key not in {"reason", "product_reason", "process_reason", "task_done", "satisfaction"}},
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
        elif _is_tmc_context(evidence.prompt) and not all(term in detail for term in ["快递下单", "网点接单", "异常件处理"]):
            detail = f"{detail}；这次要验收的是{domain}"
        elif _is_agentops_context(evidence.prompt) and not all(term in detail for term in ["提示发送", "底部日志复制", "角色配置"]):
            detail = f"{detail}；这次要验收的是{domain}"
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
    diagnostics = data.get("diagnostics")
    if isinstance(diagnostics, dict):
        summary = str(diagnostics.get("summary") or "").strip()
        if summary:
            values.append(summary)
        location = diagnostics.get("primary_location")
        if isinstance(location, dict) and location.get("path"):
            line = f":{location.get('line')}" if location.get("line") else ""
            values.append(f"{diagnostics.get('error_type') or 'command_failed'} at {location.get('path')}{line}")
    runtime_diagnostics = data.get("runtime_diagnostics")
    if isinstance(runtime_diagnostics, dict):
        for item in runtime_diagnostics.get("blocking_issues") or []:
            values.append(str(item))
        interaction = runtime_diagnostics.get("interaction")
        if isinstance(interaction, dict):
            values.append(
                "browser interaction summary: "
                f"buttons={interaction.get('buttons', 0)}, "
                f"inputs={interaction.get('inputs', 0)}, "
                f"links={interaction.get('links', 0)}"
            )
    push_diagnostics = data.get("push_diagnostics")
    if isinstance(push_diagnostics, dict):
        reason = str(push_diagnostics.get("reason") or "").strip()
        message = str(push_diagnostics.get("message") or "").strip()
        hint = str(push_diagnostics.get("credential_hint") or "").strip()
        if reason:
            values.append(f"git push failure: {reason}")
        if message:
            values.append(message)
        if hint:
            values.append(hint)
    for key in ("stderr", "stdout", "message", "error"):
        raw = data.get(key)
        if isinstance(raw, str) and raw.strip():
            values.extend(_clean_lines(raw))
    for key in ("status", "http_status", "returncode"):
        if key in data and data.get(key) not in ("", None):
            values.append(f"{key}: {data[key]}")
    review = data.get("product_review")
    if isinstance(review, dict):
        raw_findings = review.get("file_findings")
        if isinstance(raw_findings, list):
            for finding in raw_findings[:4]:
                if isinstance(finding, dict):
                    line = f":{finding.get('line')}" if finding.get("line") else ""
                    values.append(f"{finding.get('code')} at {finding.get('path')}{line}: {finding.get('snippet')}")
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
        return "任务轮次、角色配置、模型参数、提示发送、继续处理、底部日志复制、代码审查、浏览器验收、GitHub提交、飞书预览和异常状态记录"
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


def _compact_evidence_for_reviewer(evidence: DissatisfactionEvidence, rule_result: dict[str, Any], previous_reason: str) -> dict[str, Any]:
    return {
        "orchestrator_intent": evidence.orchestrator_intent or {},
        "mapped_before_llm": {
            "任务是否完成": TASK_DONE_INCOMPLETE,
            "产物及过程是否满意": "不满意",
            "不满意原因": rule_result.get("reason") or "",
        },
        "draft": {
            "User Prompt": evidence.prompt,
            "failure_stage": evidence.failure_stage,
            "failure_message": evidence.failure_message,
            "command_type": evidence.command_type,
            "result_status": evidence.result_status,
            "previous_dissatisfaction_reason": previous_reason,
            "日志轨迹": _short_text(evidence.trace_text, 18000),
            "日志轨迹原始长度": len(evidence.trace_text or ""),
            "截图路径": evidence.screenshot_path,
            "runtime_log_text": _short_text(evidence.runtime_log_text, 8000),
        },
        "evidence_summary": _evidence_summary(evidence),
        "data": _compact_data(evidence.data),
        "domain_acceptance_areas": _expected_acceptance_areas(evidence.prompt),
        "domain_boundary_examples": _expected_boundary_examples(evidence.prompt),
    }


def _compact_data(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = _short_text(value, 3000)
        elif isinstance(value, dict):
            result[key] = _compact_data(value)
        elif isinstance(value, list):
            result[key] = [_short_text(item, 800) if isinstance(item, str) else item for item in value[:12]]
        else:
            result[key] = value
    return result


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty reviewer response")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else raw
    if not candidate.lstrip().startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("reviewer JSON is not an object")
    return data


def _accept_reviewer_proposal(proposal: dict[str, Any], evidence: DissatisfactionEvidence, previous_reason: str) -> bool:
    return not _reviewer_reject_reason(proposal, evidence, previous_reason)


def _reviewer_reject_reason(proposal: dict[str, Any], evidence: DissatisfactionEvidence, previous_reason: str) -> str:
    product = _strip_reason_role_prefix(sanitize_reason_phrase(proposal.get("product_reason") or ""))
    process = _strip_reason_role_prefix(sanitize_reason_phrase(proposal.get("process_reason") or ""))
    satisfaction = str(proposal.get("satisfaction") or "").strip()
    if satisfaction != "不满意":
        return "satisfaction_not_unsatisfied"
    if len(product) < 35:
        return "product_reason_too_short"
    if len(process) < 45:
        return "process_reason_too_short"
    if _process_reason_cross_domain(product, evidence.prompt) or _process_reason_cross_domain(process, evidence.prompt):
        return "cross_domain_reason"
    if (_has_unsupported_click_claim(product) or _has_unsupported_click_claim(process)) and not _has_browser_action_evidence(evidence):
        return "unsupported_click_claim"
    full_reason = _normalize_dissatisfaction_prefix(f"{PRODUCT_LABEL}{_sentence(product)}\n{PROCESS_LABEL}{_sentence(process)}")
    if previous_reason and _reason_is_too_similar(full_reason, previous_reason):
        return "too_similar_to_previous"
    return ""


def _strip_reason_role_prefix(value: str) -> str:
    text = str(value or "").strip()
    return re.sub(r"^(产物|过程)(问题|不满意|不满意原因)\s*[:：]\s*", "", text)


def _normalize_dissatisfaction_prefix(text: str) -> str:
    value = str(text or "")
    value = value.replace("产物不满意原因：", PRODUCT_LABEL)
    value = value.replace("过程不满意原因：", PROCESS_LABEL)
    return "\n".join(sanitize_reason_phrase(line) for line in value.splitlines() if sanitize_reason_phrase(line).strip())


def _sentence(value: str) -> str:
    text = str(value or "").strip(" 。；;")
    if not text:
        return ""
    return text if text.endswith(("。", "！", "？")) else text + "。"


def _too_generic_reason(value: str, prompt: str) -> bool:
    text = str(value or "").strip()
    if len(text) < 18:
        return True
    generic = {"功能不好用", "功能不好用。", "不好用", "不好用。", "代码有问题", "不够完善"}
    if text in generic:
        return True
    return _process_reason_cross_domain(text, prompt)


def _reason_key(value: str) -> str:
    text = sanitize_reason_phrase(value)
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"\s+", "", text)
    return text


def _reason_is_too_similar(current: str, previous: str) -> bool:
    if not current or not previous:
        return False
    left = _reason_key(current)
    right = _reason_key(previous)
    if not left or not right:
        return False
    if left == right:
        return True
    short, long = sorted((left, right), key=len)
    return len(short) >= 90 and short in long


def _current_round_fallback_reason(evidence: DissatisfactionEvidence) -> str:
    areas = _expected_acceptance_areas(evidence.prompt)
    product = (
        f"{PRODUCT_LABEL}这次要验收 {areas}，当前记录还没有提供足够的代码审查、运行测试或页面复查证据来证明入口、状态变化、失败反馈和边界提示已经闭环，当前只能判定为无法确认可用。"
    )
    process = (
        f"{PROCESS_LABEL}过程记录没有按“入口点击、接口或页面状态变化、错误提示、操作记录”把 {areas} 收口说明；"
        "我看不到哪一步实际点了、预期是什么、结果变成什么，所以这轮还不能按已经验收通过处理。"
    )
    return product + "\n" + process


def _process_reason_cross_domain(line: str, prompt: str) -> bool:
    text = str(line or "")
    if _is_agentops_context(prompt):
        return _has_any_term(
            text,
            [
                "物业质量",
                "服务合同",
                "在线招标",
                "快递下单",
                "网点接单",
                "骑手取件",
                "运单",
                "线路",
                "车辆调度",
                "库位",
                "入库",
                "出库",
                "全过程链路看板",
                "告警规则",
                "节点状态",
            ],
        )
    if _is_tmc_context(prompt):
        return _has_any_term(
            text,
            [
                "举报入口",
                "帖子/消息",
                "帖子",
                "热度",
                "缺车",
                "线路冲突",
                "装车失败",
                "节点滞留",
                "费用核算",
                "仓配联动",
            ],
        )
    if _is_logistics_context(prompt):
        return _has_any_term(text, ["举报入口", "帖子/消息", "骑手取件", "网点接单", "异常件处理", "客服仲裁"])
    if _is_monitor_context(prompt):
        return _has_any_term(text, ["帖子/消息", "骑手取件", "网点接单", "合同签订", "库位冲突"])
    return False


def _has_browser_action_evidence(evidence: DissatisfactionEvidence) -> bool:
    text_parts = [
        evidence.runtime_log_text,
        evidence.trace_text,
        json.dumps(evidence.data, ensure_ascii=False, default=str),
    ]
    text = "\n".join(str(item) for item in text_parts if item).lower()
    return any(
        term in text
        for term in (
            "playwright",
            "puppeteer",
            "page.goto",
            "page.click",
            "locator(",
            "browser-use",
            "browser click",
            "浏览器实测",
        )
    )


def _has_unsupported_click_claim(text: str) -> bool:
    return any(
        term in str(text or "")
        for term in (
            "我点到",
            "我点了",
            "我点击",
            "实际点击后",
            "点击后发现",
            "点开后发现",
            "打开页面后发现",
        )
    )


def _expected_acceptance_areas(prompt: str) -> str:
    text = str(prompt or "")
    if _is_agentops_context(text):
        return "任务轮次、角色配置、模型参数、提示发送、继续处理、底部日志复制、代码审查、浏览器验收、GitHub提交、飞书预览和异常状态记录"
    if _is_tmc_context(text):
        return "快递下单、网点接单、骑手取件、分拨扫描、派送签收、异常件处理、客户通知、客服仲裁和时效统计"
    if _is_logistics_context(text):
        return "运单、线路、车辆调度、节点轨迹、签收回单、异常滞留和费用统计"
    if any(term in text for term in ["仓储系统", "仓储", "入库", "出库", "库位", "盘点", "波次"]):
        return "入库、出库、库位、库存批次、盘点差异、波次拣货和异常库存预警"
    if _is_monitor_context(text):
        return "全过程链路看板、节点状态、告警规则、异常追踪、指标趋势和处理闭环"
    if any(term in text for term in ["审批", "审批人", "权限", "申请"]):
        return "申请列表、审批节点、角色权限、操作记录、统计图和异常提醒"
    if any(term in text for term in ["看板", "工作台", "配置台", "仪表盘"]):
        return "列表入口、编辑区、联动预览、数据概览、校验和异常反馈"
    return "核心列表、编辑操作、联动预览、数据统计、校验和异常反馈"


def _expected_boundary_examples(prompt: str) -> str:
    text = str(prompt or "")
    if _is_agentops_context(text):
        return "Trae 卡住、输出过长、底部复制拿不到完整轨迹、构建失败、浏览器验收失败、GitHub 提交失败、飞书无空行或附件上传失败这些边界反馈"
    if _is_tmc_context(text):
        return "地址缺失、重量超限、无人接单、取件超时、分拨漏扫、派送失败、签收异常、费用估算不一致这些边界反馈"
    if _is_logistics_context(text):
        return "缺车、线路冲突、装车失败、节点滞留、签收异常、费用核算不一致这些边界反馈"
    if any(term in text for term in ["仓储系统", "仓储", "库存", "库位", "入库", "出库", "盘点", "波次", "SKU"]):
        return "缺库存、库位冲突、批次过期、盘点差异、出库复核失败这些边界反馈"
    if _is_monitor_context(text):
        return "节点滞留、轨迹中断、告警未处理、重复告警这些边界反馈"
    if any(term in text for term in ["审批", "审批人", "权限", "申请"]):
        return "越权、缺审批人、记录为空这些边界反馈"
    return "空数据、必填项缺失、不可用状态和异常提示这些边界反馈"


def _is_agentops_context(text: str) -> bool:
    return _has_any_term(str(text or ""), ["AgentOps", "自动作业平台", "角色工作台", "模型配置", "提示发送", "日志轨迹", "Worker", "Trae"])


def _is_tmc_context(text: str) -> bool:
    return _has_any_term(str(text or ""), ["TMC", "tmc", "快递", "骑手", "取件", "派送", "异常件", "网点接单"])


def _is_logistics_context(text: str) -> bool:
    return _has_any_term(str(text or ""), ["物流", "运单", "线路", "车辆", "装车", "签收", "回单"])


def _is_monitor_context(text: str) -> bool:
    return _has_any_term(str(text or ""), ["监控", "告警", "链路", "全过程", "节点状态"])


def _has_any_term(text: str, terms: list[str]) -> bool:
    return any(term in str(text or "") for term in terms)


def _short_text(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text
