import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import User
from app.db.repositories.roles import get_user_role
from app.services.llm import LLMClient, LLMError, model_config_from_settings
from app.services.user_settings import load_user_settings


INTENT_SYSTEM = """你是自动作业平台的调度意图解析角色。你不直接生成给 Trae 的开发提示词，也不直接写飞书或提交 GitHub。
你的任务是把用户原始作业范围和补充说明解析成结构化调度策略，供提示词角色、不满意原因角色、GitHub/飞书链路使用。

只输出 JSON：
{
  "run_mode": "normal|test",
  "intent_summary": "...",
  "prompt_brief": "...",
  "dissatisfaction_policy": "evidence_based|force_test_unsatisfied",
  "downstream_policy": "formal_only|test_chain_allowed",
  "trace_gate_policy": "strict_formal|test_exception",
  "notification_policy": "...",
  "flags": ["quick_prompt", "test_run", "force_unsatisfied", "continue_chain_on_trae_error", "skip_trae_self_tests", "single_page_quick", "chain_validation_only"],
  "risk_notes": ["..."]
}

约束：
1. 如果用户表达这是测试、快速跑链路、提示词简单、满意也写不满意，必须识别为 test。
2. 如果用户表达单页面、快速回复、只为验证日志轨迹/GitHub/飞书链路，必须把 prompt_brief 明显收敛到单页面最小结果，并设置 skip_trae_self_tests、single_page_quick、chain_validation_only。
3. 不能绕过正式安全规则；没有完整 Trae trace 时只能标记为测试例外，不能伪装成正式验收。
4. prompt_brief 要给提示词角色可执行的用户本意，不要包含内部状态机术语。
5. dissatisfaction_policy 只能是 evidence_based 或 force_test_unsatisfied。
6. downstream_policy 只能是 formal_only 或 test_chain_allowed。
7. trace_gate_policy 只能是 strict_formal 或 test_exception。
"""

INTENT_SYSTEM += """

AgentOps core SOP for every role:
- prompt generation -> Trae execution/UI operation -> Trae turn completion decision -> trace/evidence collection -> product/process review -> GitHub evidence commit -> Feishu write.
- Stop/Pause means preserve resumable state, cancel scheduler work, ask Worker to clean project-local processes and sandboxes, safely pause Trae generation if a real stop button exists, and report the stop result.
- Trae keep/adopt/save UI is a completion signal. When Trae appears complete, downstream roles should prefer trace collection before recovery clicks.
- Formal mode stays strict: no verified full Trae assistant trace means no GitHub or Feishu business write. Test mode may continue only with an explicit test-exception label.
""".strip()


def resolve_job_intent(
    db: Session,
    user: User,
    *,
    scope_text: str,
    directions: list[str],
    run_mode: str = "normal",
) -> dict[str, Any]:
    fallback = infer_job_intent(scope_text=scope_text, directions=directions)
    if run_mode == "test":
        fallback = force_test_mode_intent(fallback, scope_text=scope_text)
    role = get_user_role(db, user.id, "orchestrator_intent")
    if role and not role.enabled:
        return fallback
    model_key = role.model_config_key if role else "default"
    messages = [
        {"role": "system", "content": INTENT_SYSTEM},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "scope_text": scope_text,
                    "directions": directions[:20],
                    "rule_inferred": fallback,
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        result = LLMClient().complete(
            model_config_from_settings(load_user_settings(db, user.id), model_key),
            messages,
            purpose="orchestrator_intent",
        )
        parsed = _parse_json_object(result.text)
    except (LLMError, Exception) as exc:
        return {**fallback, "resolver": "rule_fallback", "llm_error": str(exc)[:500]}
    merged = {**fallback, **parsed}
    merged["flags"] = [*(fallback.get("flags") or []), *(parsed.get("flags") or [])]
    return _normalize_intent(merged, resolver="llm", model=result.model, wire_api=result.wire_api)


def infer_job_intent(*, scope_text: str, directions: list[str]) -> dict[str, Any]:
    text = " ".join([scope_text, *directions]).lower()
    is_test = _has_any(text, ["测试", "test", "跑链路", "验证链路", "看看填写飞书", "看看飞书", "提交github能力", "提交 github"])
    quick = _has_any(text, ["简单", "快速", "快点", "尽快", "prompt可以简单", "提示词可以简单", "小一点"])
    force_unsatisfied = _has_any(text, ["写不满意", "不满意", "故意写", "故意不满意"])
    continue_on_error = _has_any(text, ["异常也", "异常了但是", "trae异常", "把流程跑完", "流程跑完"])
    single_page_quick = _has_any(text, ["单页面", "单页", "一个页面", "快速回复", "快速完成"])
    chain_validation = _has_any(text, ["日志轨迹", "github提交", "github 提交", "飞书写入", "填写飞书", "后面链路", "后续链路"])
    flags: list[str] = []
    if is_test:
        flags.append("test_run")
    if quick:
        flags.append("quick_prompt")
    if force_unsatisfied:
        flags.append("force_unsatisfied")
    if continue_on_error:
        flags.append("continue_chain_on_trae_error")
    if single_page_quick:
        flags.append("single_page_quick")
    if chain_validation:
        flags.append("chain_validation_only")
    if is_test or single_page_quick or chain_validation:
        flags.append("skip_trae_self_tests")
    if chain_validation:
        flags.append("continue_chain_on_trae_error")
    intent = {
        "run_mode": "test" if is_test or chain_validation else "normal",
        "intent_summary": _summary(scope_text, is_test=is_test or chain_validation, quick=quick or single_page_quick, force_unsatisfied=force_unsatisfied),
        "prompt_brief": _prompt_brief(
            scope_text,
            quick=quick,
            is_test=is_test or chain_validation,
            single_page_quick=single_page_quick,
            chain_validation=chain_validation,
        ),
        "dissatisfaction_policy": "force_test_unsatisfied" if (is_test or chain_validation) and (force_unsatisfied or chain_validation) else "evidence_based",
        "downstream_policy": "test_chain_allowed" if (is_test or chain_validation) and (continue_on_error or chain_validation) else "formal_only",
        "trace_gate_policy": "test_exception" if (is_test or chain_validation) and (continue_on_error or chain_validation) else "strict_formal",
        "notification_policy": (
            "如果 Trae 异常但当前是测试模式，通知用户异常原因，并说明后续仅按测试链路验证。"
            if (is_test or chain_validation) and (continue_on_error or chain_validation)
            else "按正式异常处理通知用户。"
        ),
        "flags": flags,
        "risk_notes": ["测试模式输出必须明确标记，不能伪装成正式业务验收。"] if is_test or chain_validation else [],
        "resolver": "rules",
    }
    return _normalize_intent(intent, resolver="rules")


def force_test_mode_intent(intent: dict[str, Any] | None = None, *, scope_text: str = "") -> dict[str, Any]:
    base = dict(intent or {})
    flags = [str(item).strip() for item in base.get("flags") or [] if str(item).strip()]
    flags.extend([
        "test_start_button",
        "test_run",
        "quick_prompt",
        "force_unsatisfied",
        "continue_chain_on_trae_error",
        "skip_trae_self_tests",
        "chain_validation_only",
    ])
    if _has_any(scope_text.lower(), ["单页面", "单页", "一个页面", "快速回复", "快速完成"]):
        flags.append("single_page_quick")
    merged = {
        **base,
        "run_mode": "test",
        "intent_summary": "本轮由测试开始按钮触发，只验证 AgentOps 提示发送、Trae 观察、GitHub 和飞书链路，产物范围需要明显收小。",
        "prompt_brief": _test_button_prompt_brief(scope_text),
        "dissatisfaction_policy": "force_test_unsatisfied",
        "downstream_policy": "test_chain_allowed",
        "trace_gate_policy": "test_exception",
        "notification_policy": "如果 Trae 异常，通知用户这是测试链路异常；必要时继续 GitHub/飞书链路验证并明确标记为测试。",
        "flags": flags,
        "risk_notes": [
            "测试开始按钮触发的记录必须明确标记为测试，不作为正式业务验收结论。",
            "不要要求 Trae 自己执行耗时测试；平台后续只做轻量链路验证。",
        ],
    }
    return _normalize_intent(merged, resolver=str(base.get("resolver") or "rules"))


def _normalize_intent(
    value: dict[str, Any],
    *,
    resolver: str,
    model: str = "",
    wire_api: str = "",
) -> dict[str, Any]:
    flags = [str(item).strip() for item in value.get("flags") or [] if str(item).strip()]
    run_mode = str(value.get("run_mode") or "").strip().lower()
    if run_mode not in {"normal", "test"}:
        run_mode = "test" if "test_run" in flags else "normal"
    dissatisfaction_policy = str(value.get("dissatisfaction_policy") or "").strip()
    if dissatisfaction_policy not in {"evidence_based", "force_test_unsatisfied"}:
        dissatisfaction_policy = "evidence_based"
    downstream_policy = str(value.get("downstream_policy") or "").strip()
    if downstream_policy not in {"formal_only", "test_chain_allowed"}:
        downstream_policy = "test_chain_allowed" if run_mode == "test" and "continue_chain_on_trae_error" in flags else "formal_only"
    trace_gate_policy = str(value.get("trace_gate_policy") or "").strip()
    if trace_gate_policy not in {"strict_formal", "test_exception"}:
        trace_gate_policy = "test_exception" if downstream_policy == "test_chain_allowed" else "strict_formal"
    result = {
        "run_mode": run_mode,
        "intent_summary": str(value.get("intent_summary") or "").strip(),
        "prompt_brief": str(value.get("prompt_brief") or "").strip(),
        "dissatisfaction_policy": dissatisfaction_policy,
        "downstream_policy": downstream_policy,
        "trace_gate_policy": trace_gate_policy,
        "notification_policy": str(value.get("notification_policy") or "").strip(),
        "flags": sorted(set(flags)),
        "risk_notes": [str(item).strip() for item in value.get("risk_notes") or [] if str(item).strip()][:8],
        "resolver": resolver,
    }
    if model:
        result["model"] = model
    if wire_api:
        result["wire_api"] = wire_api
    if value.get("llm_error"):
        result["llm_error"] = str(value["llm_error"])[:500]
    return result


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else raw
    if not candidate.lstrip().startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("intent JSON is not an object")
    return data


def _has_any(text: str, terms: list[str]) -> bool:
    return any(term.lower() in text for term in terms)


def _summary(scope_text: str, *, is_test: bool, quick: bool, force_unsatisfied: bool) -> str:
    base = "本轮是测试链路。" if is_test else "本轮按正式作业执行。"
    if quick:
        base += " 用户希望提示词简化，让 Trae 尽快完成。"
    if force_unsatisfied:
        base += " 用户要求测试记录可以故意写成不满意。"
    return base or scope_text[:160]


def _prompt_brief(
    scope_text: str,
    *,
    quick: bool,
    is_test: bool,
    single_page_quick: bool = False,
    chain_validation: bool = False,
) -> str:
    text = scope_text.strip()
    if single_page_quick or chain_validation:
        return (
            f"{text}\n\n"
            "本轮按链路测试收敛范围：只做单页面最小可运行结果，快速完成并简短回复；"
            "不要扩展成完整前后端或复杂业务系统，不要主动执行耗时自测、长时间构建或完整浏览器验收。"
            "重点让平台继续验证日志轨迹、GitHub 提交和飞书写入；即使结果可接受，也按测试不满意记录。"
        )
    if quick:
        return f"{text}\n\n本轮重点是快速完成一个可运行、可验证的小范围结果，提示词应简洁，不要扩大需求。"
    if is_test:
        return f"{text}\n\n本轮重点是验证自动化链路是否能跑通，产物范围可以收小，但必须可运行、可复查。"
    return text


def _test_button_prompt_brief(scope_text: str) -> str:
    text = scope_text.strip() or "做一个最小可运行的小范围原型"
    if _has_any(text.lower(), ["单页面", "单页", "一个页面", "快速回复", "日志轨迹", "github", "飞书"]):
        return (
            f"{text}\n\n"
            "本轮是 AgentOps 测试开始按钮触发的链路测试：请只做单页面最小可运行结果，快速完成并简短回复。"
            "不要扩展成完整前后端或复杂业务系统，不要主动执行耗时自测、长时间构建或完整浏览器验收。"
            "重点让平台继续验证日志轨迹、GitHub 提交和飞书写入；即使结果可接受，也按测试不满意记录。"
        )
    return (
        f"{text}\n\n"
        "本轮是 AgentOps 测试开始按钮触发的链路测试：请只做最小可运行、可复查的小范围实现，"
        "不要主动执行耗时自测、长时间构建或完整浏览器验收；如需说明验证情况，只写你做了哪些最小检查。"
        "如果无法完整完成，也要给出当前改动和限制，便于平台继续验证 GitHub 和飞书链路。"
    )
