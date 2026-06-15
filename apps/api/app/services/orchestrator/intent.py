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
  "flags": ["quick_prompt", "test_run", "force_unsatisfied", "continue_chain_on_trae_error"],
  "risk_notes": ["..."]
}

约束：
1. 如果用户表达这是测试、快速跑链路、提示词简单、满意也写不满意，必须识别为 test。
2. 不能绕过正式安全规则；没有完整 Trae trace 时只能标记为测试例外，不能伪装成正式验收。
3. prompt_brief 要给提示词角色可执行的用户本意，不要包含内部状态机术语。
4. dissatisfaction_policy 只能是 evidence_based 或 force_test_unsatisfied。
5. downstream_policy 只能是 formal_only 或 test_chain_allowed。
6. trace_gate_policy 只能是 strict_formal 或 test_exception。
"""


def resolve_job_intent(db: Session, user: User, *, scope_text: str, directions: list[str]) -> dict[str, Any]:
    fallback = infer_job_intent(scope_text=scope_text, directions=directions)
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
    return _normalize_intent({**fallback, **parsed}, resolver="llm", model=result.model, wire_api=result.wire_api)


def infer_job_intent(*, scope_text: str, directions: list[str]) -> dict[str, Any]:
    text = " ".join([scope_text, *directions]).lower()
    is_test = _has_any(text, ["测试", "test", "跑链路", "验证链路", "看看填写飞书", "看看飞书", "提交github能力", "提交 github"])
    quick = _has_any(text, ["简单", "快速", "快点", "尽快", "prompt可以简单", "提示词可以简单", "小一点"])
    force_unsatisfied = _has_any(text, ["写不满意", "不满意", "故意写", "故意不满意"])
    continue_on_error = _has_any(text, ["异常也", "异常了但是", "trae异常", "把流程跑完", "流程跑完"])
    flags: list[str] = []
    if is_test:
        flags.append("test_run")
    if quick:
        flags.append("quick_prompt")
    if force_unsatisfied:
        flags.append("force_unsatisfied")
    if continue_on_error:
        flags.append("continue_chain_on_trae_error")
    intent = {
        "run_mode": "test" if is_test else "normal",
        "intent_summary": _summary(scope_text, is_test=is_test, quick=quick, force_unsatisfied=force_unsatisfied),
        "prompt_brief": _prompt_brief(scope_text, quick=quick, is_test=is_test),
        "dissatisfaction_policy": "force_test_unsatisfied" if is_test and force_unsatisfied else "evidence_based",
        "downstream_policy": "test_chain_allowed" if is_test and continue_on_error else "formal_only",
        "trace_gate_policy": "test_exception" if is_test and continue_on_error else "strict_formal",
        "notification_policy": (
            "如果 Trae 异常但当前是测试模式，通知用户异常原因，并说明后续仅按测试链路验证。"
            if is_test and continue_on_error
            else "按正式异常处理通知用户。"
        ),
        "flags": flags,
        "risk_notes": ["测试模式输出必须明确标记，不能伪装成正式业务验收。"] if is_test else [],
        "resolver": "rules",
    }
    return _normalize_intent(intent, resolver="rules")


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


def _prompt_brief(scope_text: str, *, quick: bool, is_test: bool) -> str:
    text = scope_text.strip()
    if quick:
        return f"{text}\n\n本轮重点是快速完成一个可运行、可验证的小范围结果，提示词应简洁，不要扩大需求。"
    if is_test:
        return f"{text}\n\n本轮重点是验证自动化链路是否能跑通，产物范围可以收小，但必须可运行、可复查。"
    return text
