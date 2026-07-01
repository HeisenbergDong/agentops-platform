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
FORBIDDEN_VISIBLE_REASON_TERMS = (
    "结果不满意",
    "不满意原因",
    "本轮",
    "这轮",
    "第x轮",
    "0-1代码生成",
    "日志",
    "轨迹",
    "扫描",
    "工具调用",
    "edit_file_search_replace",
    "npm install undefined",
    "3003模型失败",
    "LLM",
    "AI",
    "未确认",
    "不能确认",
    "无法确认",
)
FORBIDDEN_VISIBLE_REASON_PATTERNS = (
    re.compile(r"第\s*\d+\s*轮"),
    re.compile(r"\bWrite\b"),
    re.compile(r"\bchanges\b", flags=re.IGNORECASE),
)
TASK_DONE_INCOMPLETE = "未完成任务"
TASK_DONE_OPTIONS = {"完成了任务", TASK_DONE_INCOMPLETE}
REVIEWER_SYSTEM = """你是自动化作业里的“验收员”。你不能编造点击、截图、运行结果，也不能直接写飞书；只根据输入证据给验收结论提案。
要求：
1. 必须区分产物问题和过程问题，但 product_reason/process_reason 里不要写“产物不满意”“过程不满意”“结果不满意”“不满意原因”这些标签。
2. 产物问题只看生成代码质量、构建/运行结果、浏览器页面效果和具体交互结果；要写清楚哪个页面、按钮、接口、函数、文件或报错导致需求没满足。
   如果是页面或流程失败，要写“不能进入/点击无响应/接口返回/代码缺失”等确定问题，禁止用“未确认/不能确认/无法确认”替代失败原因。
   如果输入里有 product_review、inspection、runtime_diagnostics、changed_files、stderr/stdout、http_status，必须先做专业代码审查和可运行服务复查归纳：优先引用具体文件路径、函数/事件绑定、接口/URL、按钮/表单、状态码或运行错误。
   如果输入里有 github_review 或 llm_process_observations，要优先使用这些 GitHub commit 审查事实，但不能逐字复制审核文本。
3. 过程问题只看模型回复里的思考、计划、执行说明和交付描述；不要从平台运行日志、调度记录、Worker 状态、扫描动作里硬凑过程问题。
4. 只能引用输入中存在的代码路径、命令结果、文件变更、浏览器验收和模型回复内容。
3. 没有浏览器实测证据时，不能写“我点了/我点到/实际点击后”。
5. 如果没有真实可复查的模型回复内容，应表达为“当前缺少足够完整的交付过程记录”，不要使用“日志”“轨迹”“扫描”“工具调用”等工具化词汇。
6. 如果上下文标明完整原始记录已作为 txt 附件保存，不得写“没有真实记录”或“只有超长占位说明”。
7. 结论要贴近当前业务系统，避免连续多轮同一句模板。
8. 如果输入证据不足以说明问题、且任务已完成，应返回 satisfaction="满意"，product_reason 和 process_reason 可留空，不能硬编不满意。
9. 用户可见文本必须自然，像人工验收反馈；不要出现 AI/LLM/watcher/trace/session/证据/判定依据 等内部或工具化词。
10. 不满意原因必须优先评价当前任务，历史遗留只能作为补充。产物问题要能定位到文件/页面/API/功能和客观表现；过程问题要包含触发节点、实际行为和业务影响。
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
    original_user_requirement: str = ""
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
    if _is_platform_record_write_failure(evidence):
        return {
            "status": "skipped_platform_record_write_failure",
            "reason": "",
            "product_reason": "",
            "process_reason": "",
            "task_done": TASK_DONE_INCOMPLETE,
            "satisfaction": "满意",
            "failure_stage": evidence.failure_stage,
            "platform_failure": sanitize_reason_phrase(evidence.failure_message),
        }
    if _is_platform_submission_failure(evidence):
        return {
            "status": "skipped_platform_submission_failure",
            "reason": "",
            "product_reason": "",
            "process_reason": "",
            "task_done": TASK_DONE_INCOMPLETE,
            "satisfaction": "满意",
            "failure_stage": evidence.failure_stage,
            "platform_failure": sanitize_reason_phrase(evidence.failure_message),
            "evidence_summary": _evidence_summary(evidence),
        }
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
    product = "链路验证测试功能按用户要求强制标记为不满意，即使产物结果可接受，也只能用于验证 GitHub 和飞书记录链路，不能当作正式业务验收结论。"
    process = "在链路验证环节，用户明确要求把满意结果也按不满意写入；模型需要确认提示发送、执行观察、提交和写入流程是否完整，业务影响是这条记录只服务流程演练，不代表真实项目失败。"
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


def _is_platform_record_write_failure(evidence: DissatisfactionEvidence) -> bool:
    return str(evidence.failure_stage or "") in {"feishu_writing", "feishu_failed_abort"}


def _is_platform_submission_failure(evidence: DissatisfactionEvidence) -> bool:
    if str(evidence.failure_stage or "") != "github_submitting":
        return False
    data = evidence.data if isinstance(evidence.data, dict) else {}
    return not _has_trae_product_failure_evidence(data)


def _has_trae_product_failure_evidence(data: dict[str, Any]) -> bool:
    review = _extract_product_review(data)
    if review and _string_list(review.get("issues")):
        return True
    if review and review.get("accepted_findings"):
        return True
    inspection = data.get("inspection")
    if isinstance(inspection, dict) and _string_list(inspection.get("issues")):
        return True
    if isinstance(data.get("diagnostics"), dict):
        return True
    if str(data.get("returncode") or "") not in {"", "0", "None"}:
        return True
    if str(data.get("http_status") or "").startswith(("4", "5")):
        return True
    return False


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

    satisfaction = str(proposal.get("satisfaction") or "").strip()
    if satisfaction == "满意":
        return {
            "reason": "",
            "product_reason": "",
            "process_reason": "",
            "task_done": str(proposal.get("task_done") or "完成了任务"),
            "satisfaction": "满意",
            "failure_stage": evidence.failure_stage,
            "evidence_summary": _evidence_summary(evidence),
            "llm_reviewer": {
                "accepted": True,
                "model": result.model,
                "wire_api": result.wire_api,
                "confidence": proposal.get("confidence"),
                "evidence_refs": proposal.get("evidence_refs") or [],
            },
        }

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
    product_reason = product_line.replace(PRODUCT_LABEL, "", 1).strip()
    process_reason = process_line.replace(PROCESS_LABEL, "", 1).strip()
    reason = _compose_visible_reason(product_reason, process_reason, evidence)
    if previous_reason and _reason_is_too_similar(reason, previous_reason):
        fallback = _current_round_fallback_reason(evidence)
        reason = _normalize_dissatisfaction_prefix(fallback)
        product_line = next((line for line in reason.splitlines() if line.startswith(PRODUCT_LABEL)), product_line)
        process_line = next((line for line in reason.splitlines() if line.startswith(PROCESS_LABEL)), process_line)
        product_reason = product_line.replace(PRODUCT_LABEL, "", 1).strip()
        process_reason = process_line.replace(PROCESS_LABEL, "", 1).strip()
        reason = _compose_visible_reason(product_reason, process_reason, evidence)
    quality_issues = _visible_reason_quality_issues(reason, evidence)
    if quality_issues:
        fallback = _normalize_dissatisfaction_prefix(_current_round_fallback_reason(evidence))
        fallback_lines = [line.strip() for line in fallback.splitlines() if line.strip()]
        product_line = next((line for line in fallback_lines if line.startswith(PRODUCT_LABEL)), PRODUCT_LABEL + _product_reason(evidence))
        process_line = next((line for line in fallback_lines if line.startswith(PROCESS_LABEL)), PROCESS_LABEL + _process_reason(evidence))
        product_reason = product_line.replace(PRODUCT_LABEL, "", 1).strip()
        process_reason = process_line.replace(PROCESS_LABEL, "", 1).strip()
        reason = _compose_visible_reason(product_reason, process_reason, evidence)
        result["visible_reason_quality_gate"] = {"rewritten": True, "issues": quality_issues}
    final_lines = [line.strip() for line in reason.splitlines() if line.strip()]
    product_reason = next(
        (line.replace(PRODUCT_LABEL, "", 1).strip() for line in final_lines if line.startswith(PRODUCT_LABEL)),
        _clean_visible_reason(product_reason, evidence),
    )
    process_reason = next(
        (line.replace(PROCESS_LABEL, "", 1).strip() for line in final_lines if line.startswith(PROCESS_LABEL)),
        _clean_visible_reason(process_reason, evidence),
    )
    return {
        "reason": reason,
        "product_reason": product_reason,
        "process_reason": process_reason,
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


def _compose_visible_reason(product_reason: str, process_reason: str, evidence: DissatisfactionEvidence) -> str:
    product = _clean_visible_reason(product_reason, evidence)
    process = _clean_visible_reason(process_reason, evidence)
    if not product:
        product = _clean_visible_reason(_product_reason(evidence), evidence)
    if not process or process == product:
        process = _clean_visible_reason(_process_reason(evidence), evidence)
    return f"{PRODUCT_LABEL}{_sentence(product)}\n{PROCESS_LABEL}{_sentence(process)}"


def _clean_visible_reason(value: str, evidence: DissatisfactionEvidence) -> str:
    text = _strip_reason_role_prefix(sanitize_reason_phrase(value))
    replacements = {
        "日志轨迹": "完整过程记录",
        "日志": "过程记录",
        "轨迹": "过程记录",
        "扫描": "检查",
        "工具调用": "执行记录",
        "本轮": "当前任务",
        "这轮": "当前任务",
        "第x轮": "当前任务",
        "0-1代码生成": "代码生成阶段",
        "edit_file_search_replace": "文件替换编辑",
        "npm install undefined": "依赖安装命令参数异常",
        "3003模型失败": "模型服务请求失败",
        "LLM": "模型",
        "AI": "模型",
        "trace": "record",
        "session": "会话",
        "watcher": "观察流程",
        "证据": "情况",
        "判定依据": "",
        "不满意原因": "",
        "产物不满意": "",
        "过程不满意": "",
        "结果不满意": "",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"第\s*\d+\s*轮", "当前任务", text)
    text = re.sub(r"\bWrite\b", "写入动作", text)
    text = re.sub(r"\bchanges\b", "变更", text, flags=re.IGNORECASE)
    if not _is_agentops_context(evidence.prompt):
        text = text.replace("Worker", "本地执行环境").replace("worker", "本地执行环境")
        text = text.replace("Trae CN", "模型").replace("Trae", "模型")
    text = re.sub(r"\s+", " ", text).strip(" ，。；;")
    return text


def _visible_reason_has_forbidden_terms(reason: str) -> bool:
    return bool(_visible_reason_forbidden_hits(reason, None))


def _visible_reason_forbidden_hits(reason: str, evidence: DissatisfactionEvidence | None) -> list[str]:
    body = str(reason or "")
    for label in (PRODUCT_LABEL, PROCESS_LABEL):
        body = body.replace(label, "")
    hits = [term for term in FORBIDDEN_VISIBLE_REASON_TERMS if term and term in body]
    hits.extend(pattern.pattern for pattern in FORBIDDEN_VISIBLE_REASON_PATTERNS if pattern.search(body))
    if "3003" in body:
        evidence_blob = _evidence_blob(evidence) if evidence else ""
        if "3003" not in evidence_blob:
            hits.append("unsupported_3003")
    return hits


def _visible_reason_quality_issues(reason: str, evidence: DissatisfactionEvidence) -> list[str]:
    issues: list[str] = []
    forbidden = _visible_reason_forbidden_hits(reason, evidence)
    if forbidden:
        issues.append("forbidden_terms:" + ",".join(forbidden[:5]))
    lines = [line.strip() for line in str(reason or "").splitlines() if line.strip()]
    product = next((line.replace(PRODUCT_LABEL, "", 1).strip() for line in lines if line.startswith(PRODUCT_LABEL)), "")
    process = next((line.replace(PROCESS_LABEL, "", 1).strip() for line in lines if line.startswith(PROCESS_LABEL)), "")
    if not product:
        issues.append("missing_product_reason")
    elif not _product_reason_is_specific(product, evidence):
        issues.append("product_reason_not_specific")
    if not process:
        issues.append("missing_process_reason")
    elif not _process_reason_has_chain(process):
        issues.append("process_reason_missing_when_what_impact")
    if product and process and _reason_key(product) == _reason_key(process):
        issues.append("product_process_duplicated")
    return issues


def _product_reason_is_specific(product: str, evidence: DissatisfactionEvidence) -> bool:
    text = str(product or "")
    if len(text) < 35:
        return False
    if re.search(r"https?://|/[A-Za-z0-9_./-]+|[A-Za-z0-9_./\\-]+\.(?:vue|tsx|jsx|ts|js|go|py|java|css|html)", text):
        location_ok = True
    else:
        location_ok = any(term in text for term in ("页面", "接口", "文件", "功能", "入口", "按钮", "表单", "路由", "HTTP", "状态码", "函数", "组件", "浏览器验收"))
    unmet_ok = any(term in text for term in ("无法", "不能", "没有", "缺失", "失败", "未满足", "不满足", "达不到", "不完整", "不可用"))
    if "历史" in text[:18] and not any(term in text[:40] for term in ("当前", "这次", "浏览器", "代码", "页面", "接口")):
        return False
    return location_ok and unmet_ok


def _process_reason_has_chain(process: str) -> bool:
    text = str(process or "")
    if len(text) < 45:
        return False
    when_ok = any(term in text for term in ("在", "环节", "步骤", "阶段", "验收", "复查", "提交", "写入", "生成", "交付"))
    what_ok = any(term in text for term in ("模型", "交付说明", "创建", "没有", "缺少", "跳过", "声称", "描述", "复查", "处理", "执行"))
    impact_ok = any(term in text for term in ("导致", "影响", "无法", "不能", "使", "业务影响", "不能按", "不代表"))
    return when_ok and what_ok and impact_ok


def _evidence_blob(evidence: DissatisfactionEvidence | None) -> str:
    if not evidence:
        return ""
    try:
        return json.dumps(
            {
                "failure_stage": evidence.failure_stage,
                "failure_message": evidence.failure_message,
                "data": evidence.data,
                "trace_text": evidence.trace_text,
                "runtime_log_text": evidence.runtime_log_text,
            },
            ensure_ascii=False,
            default=str,
        )
    except Exception:
        return f"{evidence.failure_stage}\n{evidence.failure_message}\n{evidence.trace_text}\n{evidence.runtime_log_text}"


def _product_reason(evidence: DissatisfactionEvidence) -> str:
    stage = evidence.failure_stage
    key_lines = _key_lines(evidence)
    domain = _domain_hint(evidence.prompt)
    detail = _product_review_problem(evidence) or _join_evidence(key_lines) or sanitize_reason_phrase(evidence.failure_message)

    if stage == "product_reviewing":
        if not detail:
            detail = f"当前要验收的是{domain}，但构建、测试或静态检查没有留下能通过的结果"
        elif _is_tmc_context(evidence.prompt) and not all(term in detail for term in ["快递下单", "网点接单", "异常件处理"]):
            detail = f"{detail}；这次要验收的是{domain}"
        elif _is_agentops_context(evidence.prompt) and not all(term in detail for term in ["提示发送", "底部日志复制", "角色配置"]):
            detail = f"{detail}；这次要验收的是{domain}"
        return f"{detail}，所以这版还不能当作完成的项目结果提交。"
    if stage == "browser_accepting":
        data = evidence.data if isinstance(evidence.data, dict) else {}
        browser_data = data.get("browser_acceptance") if isinstance(data.get("browser_acceptance"), dict) else {}
        browser_status = str(browser_data.get("status") or data.get("status") or "").strip()
        browser_problem = _browser_acceptance_problem(evidence) if browser_status != "passed" else ""
        detail = browser_problem or _product_review_problem(evidence) or detail
        if not detail:
            detail = "浏览器验收没有拿到可用页面或主流程反馈"
        return f"{detail}，{domain}里的页面入口、状态变化或结果反馈没有达到当前可验收状态。"
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
        else "当前缺少足够完整的模型回复内容支撑继续提交。"
    )
    screenshot_note = "我这边也保留了当时截图，方便回看页面状态。" if evidence.screenshot_path else ""
    return f"在{stage}，模型交付说明没有把当前需求收口到可验收状态，实际结果是{detail}，导致业务侧不能按已完成项目处理。{trace_note}{screenshot_note}".strip()


def _browser_acceptance_problem(evidence: DissatisfactionEvidence) -> str:
    data = evidence.data if isinstance(evidence.data, dict) else {}
    browser_data = data.get("browser_acceptance") if isinstance(data.get("browser_acceptance"), dict) else {}
    source = browser_data or data
    url = str(source.get("url") or source.get("requested_url") or source.get("acceptance_url") or "").strip()
    status = source.get("http_status")
    tried = _string_list(source.get("candidate_urls_tried"))
    parts: list[str] = []
    if url:
        parts.append(f"{url} 浏览器验收失败")
    else:
        parts.append("浏览器验收失败")
    if tried:
        parts.append("候选访问地址包括：" + "、".join(tried[:6]))
    if status not in ("", None):
        parts.append(f"HTTP 状态为 {status}")
    inspection = source.get("inspection") if isinstance(source.get("inspection"), dict) else {}
    issues = inspection.get("issues") if isinstance(inspection, dict) else []
    if isinstance(issues, list) and issues:
        parts.append("页面问题：" + "；".join(str(item) for item in issues[:3] if item))
    runtime_diagnostics = source.get("runtime_diagnostics") if isinstance(source.get("runtime_diagnostics"), dict) else {}
    blocking = runtime_diagnostics.get("blocking_issues") if isinstance(runtime_diagnostics, dict) else []
    if isinstance(blocking, list) and blocking:
        parts.append("运行检查问题：" + "；".join(str(item) for item in blocking[:3] if item))
    interaction = {}
    if isinstance(inspection.get("interaction"), dict):
        interaction = inspection["interaction"]
    elif isinstance(runtime_diagnostics.get("interaction"), dict):
        interaction = runtime_diagnostics["interaction"]
    if interaction:
        total = interaction.get("total")
        if total == 0:
            parts.append("页面没有检测到可操作入口")
        labels = interaction.get("button_labels")
        if isinstance(labels, list) and labels:
            parts.append("页面按钮包括：" + "、".join(str(item) for item in labels[:5] if item))
    message = sanitize_reason_phrase(str(source.get("message") or evidence.failure_message or ""))
    if message and message not in "；".join(parts):
        parts.append(message)
    auto_start = source.get("auto_start") if isinstance(source.get("auto_start"), dict) else {}
    if auto_start:
        auto_status = str(auto_start.get("status") or "")
        command = auto_start.get("command") if isinstance(auto_start.get("command"), list) else []
        cwd = str(auto_start.get("cwd") or "")
        if auto_status:
            parts.append(f"本地服务启动状态为 {auto_status}")
        if cwd:
            parts.append(f"服务目录为 {cwd}")
        if command:
            parts.append("启动命令为 " + " ".join(str(item) for item in command[:8]))
    return "；".join(part for part in parts if part)


def _product_review_problem(evidence: DissatisfactionEvidence) -> str:
    review = _extract_product_review(evidence.data if isinstance(evidence.data, dict) else {})
    if not review:
        return ""
    parts: list[str] = []
    issues = _string_list(review.get("issues"))
    warnings = _string_list(review.get("warnings"))
    evidence_items = _string_list(review.get("evidence"))
    changed_files = _string_list(review.get("changed_files"))
    stack = _string_list(review.get("stack"))
    if issues:
        parts.append("代码审查发现：" + "；".join(issues[:4]))
    if warnings:
        parts.append("审查提醒：" + "；".join(warnings[:2]))
    if changed_files:
        parts.append("当前重点变更文件：" + "、".join(changed_files[:6]))
    if evidence_items:
        parts.append("审查范围：" + "；".join(evidence_items[:2]))
    elif review.get("file_count") not in ("", None):
        parts.append(f"审查范围：项目文件 {review.get('file_count')} 个")
    if stack:
        parts.append("识别技术栈：" + "、".join(stack[:4]))
    process_observations = _string_list(review.get("llm_process_observations"))
    if process_observations:
        parts.append("过程观察：" + "；".join(process_observations[:2]))
    github_review = review.get("github_review") if isinstance(review.get("github_review"), dict) else {}
    if github_review.get("commit_url"):
        parts.append("GitHub审查快照：" + str(github_review.get("commit_url")))
    return "；".join(parts)


def _extract_product_review(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    nested_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    double_nested_data = nested_data.get("data") if isinstance(nested_data.get("data"), dict) else {}
    for candidate in (data.get("product_review"), nested_data.get("product_review"), double_nested_data.get("product_review")):
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _evidence_summary(evidence: DissatisfactionEvidence) -> dict[str, Any]:
    review = _extract_product_review(evidence.data if isinstance(evidence.data, dict) else {})
    return {
        "failure_stage": evidence.failure_stage,
        "failure_message": evidence.failure_message,
        "command_type": evidence.command_type,
        "result_status": evidence.result_status,
        "trace_chars": len(evidence.trace_text),
        "has_screenshot": bool(evidence.screenshot_path),
        "data_keys": sorted(evidence.data.keys()),
        "product_review_issue_count": len(_string_list(review.get("issues"))) if review else 0,
        "product_review_warning_count": len(_string_list(review.get("warnings"))) if review else 0,
        "changed_files": _string_list(review.get("changed_files"))[:8] if review else [],
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
    review = _extract_product_review(data)
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
        "trace_missing_abort": "获取模型完整回复这一步",
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
    product_review = _extract_product_review(evidence.data if isinstance(evidence.data, dict) else {})
    return {
        "orchestrator_intent": evidence.orchestrator_intent or {},
        "mapped_before_llm": {
            "任务是否完成": TASK_DONE_INCOMPLETE,
            "产物及过程是否满意": "不满意",
            "不满意原因": rule_result.get("reason") or "",
        },
        "draft": {
            "Original User Requirement": evidence.original_user_requirement,
            "Trae Prompt Sent": evidence.prompt,
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
        "product_review": {
            "issues": _string_list(product_review.get("issues"))[:8],
            "warnings": _string_list(product_review.get("warnings"))[:6],
            "changed_files": _string_list(product_review.get("changed_files"))[:10],
            "evidence": _string_list(product_review.get("evidence"))[:6],
            "stack": _string_list(product_review.get("stack"))[:6],
            "file_count": product_review.get("file_count"),
            "llm_process_observations": _string_list(product_review.get("llm_process_observations"))[:6],
            "github_review": product_review.get("github_review") if isinstance(product_review.get("github_review"), dict) else {},
        },
        "code_review": {
            "deprecated": True,
            "use": "product_review",
            "issues": _string_list(product_review.get("issues"))[:8],
            "warnings": _string_list(product_review.get("warnings"))[:6],
            "changed_files": _string_list(product_review.get("changed_files"))[:10],
            "evidence": _string_list(product_review.get("evidence"))[:6],
        },
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
    if satisfaction == "满意":
        task_done = str(proposal.get("task_done") or "").strip()
        if task_done and task_done not in TASK_DONE_OPTIONS:
            return "invalid_task_done"
        return ""
    if satisfaction != "不满意":
        return "satisfaction_not_unsatisfied"
    if len(product) < 35:
        return "product_reason_too_short"
    if len(process) < 45:
        return "process_reason_too_short"
    if _process_reason_cross_domain(product, evidence.prompt) or _process_reason_cross_domain(process, evidence.prompt):
        return "cross_domain_reason"
    if _has_vague_acceptance_claim(product):
        return "vague_product_acceptance_claim"
    if _has_structured_product_issue(evidence) and not _mentions_structured_product_issue(product, evidence):
        return "product_reason_ignored_structured_review"
    if (_has_unsupported_click_claim(product) or _has_unsupported_click_claim(process)) and not _has_browser_action_evidence(evidence):
        return "unsupported_click_claim"
    full_reason = _compose_visible_reason(product, process, evidence)
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
    if _has_vague_acceptance_claim(text):
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
    detail = _product_review_problem(evidence)
    if not detail and evidence.failure_stage == "browser_accepting":
        detail = _browser_acceptance_problem(evidence)
    if not detail:
        detail = _join_evidence(_key_lines(evidence)) or sanitize_reason_phrase(evidence.failure_message)
    if not detail:
        detail = "没有形成可复查的页面、接口、代码或运行失败详情"
    product = (
        f"这次要验收 {areas}，当前失败点是：{detail}。这些失败项直接影响入口、状态变化、失败反馈或边界提示闭环。"
    )
    process = (
        f"过程记录没有按“入口点击、接口或页面状态变化、错误提示、操作记录”把 {areas} 收口说明；"
        "模型交付说明没有讲清哪一步实际点了、预期是什么、结果变成什么，导致业务侧不能按已经验收通过处理。"
    )
    return f"{PRODUCT_LABEL}{product}\n{PROCESS_LABEL}{process}"


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


def _has_vague_acceptance_claim(text: str) -> bool:
    return any(term in str(text or "") for term in ("未确认", "不能确认", "无法确认", "未看到", "没有看到后续确认"))


def _has_structured_product_issue(evidence: DissatisfactionEvidence) -> bool:
    data = evidence.data if isinstance(evidence.data, dict) else {}
    review = _extract_product_review(data)
    if _string_list(review.get("issues")):
        return True
    inspection = data.get("inspection") if isinstance(data.get("inspection"), dict) else {}
    return bool(_string_list(inspection.get("issues")))


def _mentions_structured_product_issue(text: str, evidence: DissatisfactionEvidence) -> bool:
    value = str(text or "")
    data = evidence.data if isinstance(evidence.data, dict) else {}
    review = _extract_product_review(data)
    needles: list[str] = []
    for issue in _string_list(review.get("issues"))[:4]:
        needles.extend(_issue_needles(issue))
    inspection = data.get("inspection") if isinstance(data.get("inspection"), dict) else {}
    for issue in _string_list(inspection.get("issues"))[:4]:
        needles.extend(_issue_needles(issue))
    return any(needle and needle in value for needle in needles)


def _issue_needles(issue: str) -> list[str]:
    text = str(issue or "").strip()
    if not text:
        return []
    needles = [text[:30]]
    path_match = re.search(r"([A-Za-z0-9_./\\-]+\.(?:vue|tsx|jsx|ts|js|go|py|java|css|html))", text)
    if path_match:
        needles.append(path_match.group(1))
    for marker in ("事件绑定为空", "函数体为空", "接口请求失败", "页面正文为空", "构建错误", "Internal Server Error"):
        if marker in text:
            needles.append(marker)
    return needles


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
