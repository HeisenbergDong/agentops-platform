import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import User
from app.db.repositories.roles import get_user_role
from app.services.github.repository import fetch_github_review_snapshot
from app.services.llm import LLMClient, LLMError, model_config_from_settings
from app.services.user_settings import load_user_settings


PRODUCT_REVIEW_SYSTEM = """你是自动化作业里的成果检查角色。你审查的是已经提交到 GitHub 的本次代码快照。
你不能写飞书，也不能直接写最终不满意原因；你只输出用于后续验收的审查事实。

要求：
1. 当前任务优先：先看本次 User Prompt 要求，不要只抓历史遗留问题。
2. 产物问题必须具体：尽量指出文件/页面/接口/函数/路由，说明代码层原因、客观表现、未满足哪条需求。
3. 过程观察必须包含触发环节、模型实际行为、业务影响；不要堆“日志”“轨迹”“工具调用”“edit_file_search_replace”“Write”“changes”等字段名。
4. 页面问题要落到 404、空白、路由、渲染、按钮无响应、状态不变化、入口缺失等客观现象。
5. 接口问题要区分 400/500，说明更像前端参数、后端处理、路由或权限哪一类问题。
6. 构建/测试失败只摘关键错误，不要大片复制报错。
7. 不要复制 User Prompt 或其他审核意见；不要机械模板化。
8. 如果证据不足，只输出 needs_more_evidence，不要编造点击、运行结果或不存在的文件。

只输出 JSON：
{
  "status": "ok|needs_more_evidence",
  "current_task_summary": "...",
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
    for item in review.get("blocking_issues") or []:
        if not isinstance(item, dict):
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
        "ok": not issues,
        "issues": issues[:12],
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
            "confidence": review.get("confidence"),
            "commit_url": snapshot.get("commit_url") or "",
            "commit_sha": snapshot.get("commit_sha") or "",
        },
    }


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
