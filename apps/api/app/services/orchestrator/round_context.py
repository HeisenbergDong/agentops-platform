from __future__ import annotations

from typing import Any

from app.db.models import Job, TaskRound


def build_round_context(job: Job | None, round_: TaskRound | None, *, stage: str = "") -> dict[str, Any]:
    """Compact context shared with LLM-assisted orchestration roles."""
    if not job:
        return {}
    directions = _direction_queue(job)
    current_direction = directions[0] if directions else ""
    prompt = str(round_.prompt or "").strip() if round_ else ""
    context = {
        "job_id": job.id,
        "round_id": round_.id if round_ else "",
        "round_index": round_.round_index if round_ else None,
        "stage": stage or str(job.status or ""),
        "job_status": str(job.status or ""),
        "round_status": str(round_.status or "") if round_ else "",
        "original_user_requirement": str(job.scope_text or "").strip() or "\n".join(directions),
        "current_direction": current_direction,
        "direction_queue": directions[:8],
        "remaining_direction_count": max(0, len(directions) - 1),
        "trae_prompt_sent": prompt,
        "prompt": prompt,
        "orchestrator_intent": _compact_intent(job.intent if isinstance(job.intent, dict) else {}),
        "role_boundaries": {
            "scene_supervisor": "Judge the current Trae UI state and recommend one safe whitelisted action.",
            "product_reviewer": "Compare generated artifacts against the original requirement and Trae prompt.",
            "dissatisfaction_writer": "Write a business-readable reason from verified product findings only.",
        },
        "safety_contract": {
            "llm_decides": "state_and_recommended_action",
            "worker_executes": "whitelisted_safe_actions_only",
            "do_not_convert_platform_failures_to_product_issues": True,
        },
    }
    return {key: value for key, value in context.items() if value not in ("", None, [], {})}


def merge_round_context(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not context:
        return payload
    result = dict(payload)
    existing = result.get("round_context") if isinstance(result.get("round_context"), dict) else {}
    result["round_context"] = {**context, **existing}
    return result


def _direction_queue(job: Job) -> list[str]:
    if not isinstance(job.directions, list):
        return []
    return [str(item).strip() for item in job.directions if str(item).strip()]


def _compact_intent(intent: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "run_mode",
        "dissatisfaction_policy",
        "flags",
        "range_plan",
        "prompt_brief",
        "current_direction",
    }
    return {key: intent[key] for key in allowed if key in intent}
