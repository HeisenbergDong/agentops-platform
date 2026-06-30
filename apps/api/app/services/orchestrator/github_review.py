import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import User
from app.db.repositories.roles import get_user_role
from app.services.github.repository import fetch_github_review_snapshot
from app.services.llm import LLMClient, LLMError, model_config_from_settings
from app.services.user_settings import load_user_settings


PRODUCT_REVIEW_SYSTEM = """你是自动化作业里的成果检查角色。你审查的是 Trae 已经提交到 GitHub 的本次代码快照。
你只做“Trae 成果物 vs 本次发给 Trae 的需求”的审查；不要审查 AgentOps 调度流程、Worker、GitHub 凭据或飞书写入。
你不能写飞书，也不能直接写最终不满意原因；你只输出用于后续验收的审查事实。

要求：
1. 当前任务优先：先看本次 User Prompt 要求，不要只抓历史遗留问题。
2. 产物问题必须具体：尽量指出文件/页面/接口/函数/路由，说明代码层原因、客观表现、未满足哪条需求。
3. 过程观察必须包含触发环节、模型实际行为、业务影响；不要堆“日志”“轨迹”“工具调用”“edit_file_search_replace”“Write”“changes”等字段名。
4. 页面问题要落到 404、空白、路由、渲染、按钮无响应、状态不变化、入口缺失等客观现象。
5. 接口问题要区分 400/500，说明更像前端参数、后端处理、路由或权限哪一类问题。
6. 构建/测试失败只摘关键错误，不要大片复制报错。
7. 不要复制 User Prompt 或其他审核意见；不要机械模板化。
8. 如果没有发现 Trae 成果物相对本次需求的真实问题，decision 必须是 no_issue；不要为了写不满意而硬凑。
9. 如果 GitHub 快照信息不足以审查成果物，decision 必须是 needs_more_evidence；不要编造点击、运行结果或不存在的文件。

只输出 JSON：
{
  "decision": "has_issue|no_issue|needs_more_evidence",
  "status": "ok|needs_more_evidence",
  "current_task_summary": "...",
  "findings": [
    {
      "requirement": "本次需求里的具体要求",
      "location": "path:line 或页面/接口",
      "feature": "...",
      "code_problem": "...",
      "objective_symptom": "...",
      "unmet_requirement": "...",
      "root_cause": "...",
      "current_task_related": true
    }
  ],
  "blocking_issues": [
    {
      "location": "path:line 或页面/接口",
      "feature": "...",
      "code_problem": "...",
      "objective_symptom": "...",
      "unmet_requirement": "...",
      "root_cause": "...",
      "current_task_related": true
    }
  ],
  "warnings": [],
  "process_observations": [
    {
      "trigger_node": "...",
      "actual_behavior": "...",
      "impact": "..."
    }
  ],
  "confidence": 0.0
}"""


def review_github_snapshot(
    db: Session,
    user: User,
    *,
    github_config: dict[str, Any],
    git_data: dict[str, Any],
    prompt: str,
    trace_text: str = "",
    runtime_log_text: str = "",
) -> dict[str, Any]:
    snapshot = fetch_github_review_snapshot(github_config, git_data, prompt=prompt)
    if not snapshot.get("ok"):
        return {"ok": False, "snapshot": snapshot, "reason": snapshot.get("reason") or "snapshot_fetch_failed"}

    role = get_user_role(db, user.id, "product_reviewer")
    if role and not role.enabled:
        return {"ok": True, "snapshot": snapshot, "skipped": True, "reason": "product_reviewer_disabled"}
    model_key = role.model_config_key if role else "default"
    context = {
        "github_snapshot": _compact_snapshot(snapshot),
        "user_prompt": prompt,
        "trace_summary": _short_text(trace_text, 12000),
        "runtime_log_summary": _short_text(runtime_log_text, 5000),
        "review_contract": {
            "review_target": "Trae generated project files in the GitHub snapshot",
            "compare_against": "the user_prompt sent to Trae for this round",
            "do_not_use_as_product_issue": ["AgentOps scheduler logs", "Worker status", "GitHub auth/push failures", "Feishu write failures"],
            "valid_finding_requires": ["requirement", "location_or_feature", "code_problem", "objective_symptom", "unmet_requirement"],
        },
    }
    messages = [
        {"role": "system", "content": PRODUCT_REVIEW_SYSTEM},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]
    try:
        result = LLMClient().complete(
            model_config_from_settings(load_user_settings(db, user.id), model_key),
            messages,
            purpose="github_product_review",
        )
        review = _parse_json_object(result.text)
    except (LLMError, Exception) as exc:
        return {"ok": True, "snapshot": snapshot, "llm_error": str(exc)[:500]}
    return {
        "ok": True,
        "snapshot": snapshot,
        "llm_review": review,
        "model": result.model,
        "wire_api": result.wire_api,
        "product_review": _to_product_review(review, snapshot),
    }


def _to_product_review(review: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    issues = []
    accepted_findings = []
    skipped_findings = []
    raw_findings = review.get("findings") or review.get("blocking_issues") or []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        if not _valid_product_finding(item):
            skipped_findings.append(item)
            continue
        pieces = [
            str(item.get("location") or "").strip(),
            str(item.get("feature") or "").strip(),
            str(item.get("code_problem") or "").strip(),
            str(item.get("objective_symptom") or "").strip(),
            str(item.get("unmet_requirement") or "").strip(),
            str(item.get("root_cause") or "").strip(),
        ]
        text = "；".join(piece for piece in pieces if piece)
        if text:
            issues.append(text)
            accepted_findings.append(
                {
                    "requirement": str(item.get("requirement") or item.get("unmet_requirement") or "").strip(),
                    "location": str(item.get("location") or "").strip(),
                    "feature": str(item.get("feature") or "").strip(),
                    "code_problem": str(item.get("code_problem") or "").strip(),
                    "objective_symptom": str(item.get("objective_symptom") or "").strip(),
                    "unmet_requirement": str(item.get("unmet_requirement") or "").strip(),
                    "root_cause": str(item.get("root_cause") or "").strip(),
                }
            )
    decision = _review_decision(review, bool(issues), bool(skipped_findings))
    warnings = [str(item).strip() for item in review.get("warnings") or [] if str(item).strip()]
    process = []
    for item in review.get("process_observations") or []:
        if isinstance(item, dict):
            text = "；".join(
                str(item.get(key) or "").strip()
                for key in ("trigger_node", "actual_behavior", "impact")
                if str(item.get(key) or "").strip()
            )
            if text:
                process.append(text)
    evidence = [
        f"GitHub 审查快照：{snapshot.get('commit_url') or snapshot.get('commit_sha') or ''}".strip("："),
        f"变更文件：{'、'.join((snapshot.get('changed_files') or [])[:8])}" if snapshot.get("changed_files") else "",
    ]
    return {
        "ok": decision != "has_issue",
        "decision": decision,
        "review_decision": decision,
        "issues": issues[:12],
        "accepted_findings": accepted_findings[:12],
        "skipped_findings": skipped_findings[:8],
        "needs_more_evidence": decision == "needs_more_evidence",
        "warnings": warnings[:8],
        "evidence": [item for item in evidence if item][:12],
        "summary": str(review.get("current_task_summary") or ""),
        "blocking_issue_count": len(issues),
        "warning_count": len(warnings),
        "file_findings": [],
        "stack": [],
        "file_count": len(snapshot.get("changed_files") or []),
        "changed_files": snapshot.get("changed_files") or [],
        "llm_process_observations": process[:8],
        "github_review": {
            "status": review.get("status") or "",
            "decision": decision,
            "confidence": review.get("confidence"),
            "commit_url": snapshot.get("commit_url") or "",
            "commit_sha": snapshot.get("commit_sha") or "",
        },
    }


def _valid_product_finding(item: dict[str, Any]) -> bool:
    if item.get("current_task_related") is False:
        return False
    location_or_feature = str(item.get("location") or item.get("feature") or "").strip()
    code_problem = str(item.get("code_problem") or "").strip()
    unmet = str(item.get("unmet_requirement") or item.get("requirement") or "").strip()
    symptom = str(item.get("objective_symptom") or "").strip()
    if not location_or_feature or not code_problem or not unmet:
        return False
    if len(code_problem) < 8 or len(unmet) < 6:
        return False
    forbidden_blob = " ".join(str(item.get(key) or "") for key in ("location", "feature", "code_problem", "objective_symptom", "unmet_requirement"))
    if any(term in forbidden_blob for term in ("job_starting", "cleaning_old_runtime", "direction_queue", "loading_rules", "Worker", "飞书写入", "GitHub 凭据")):
        return False
    return bool(symptom or str(item.get("root_cause") or "").strip())


def _review_decision(review: dict[str, Any], has_valid_issue: bool, has_invalid_issue: bool) -> str:
    decision = str(review.get("decision") or "").strip()
    status = str(review.get("status") or "").strip()
    if has_valid_issue:
        return "has_issue"
    if decision in {"no_issue", "needs_more_evidence"}:
        return decision
    if status == "needs_more_evidence" or has_invalid_issue:
        return "needs_more_evidence"
    return "no_issue"


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    selected = []
    for item in snapshot.get("selected_files") or []:
        if not isinstance(item, dict):
            continue
        selected.append({"path": item.get("path"), "content": _short_text(item.get("content"), 12000)})
    return {
        "commit_url": snapshot.get("commit_url"),
        "commit_sha": snapshot.get("commit_sha"),
        "changed_files": snapshot.get("changed_files") or [],
        "diff": _short_text(snapshot.get("diff"), 50000),
        "selected_files": selected,
    }


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty product reviewer response")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else raw
    if not candidate.lstrip().startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("product reviewer JSON is not an object")
    return data


def _short_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
