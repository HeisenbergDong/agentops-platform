from __future__ import annotations

from dataclasses import replace
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, TaskRound, User, UserRole, WorkerCommand
from app.services.llm import LLMClient, LLMError, model_config_from_settings
from app.services.user_settings import load_user_settings
from app.worker_gateway.contracts import WorkerCommandType


RECOVERABLE_REASONS = {
    "awaiting_continuation",
    "awaiting_current_continuation",
    "service_interrupted",
    "missing_tool_trace_markers",
    "no_completed_turn_after_prompt_send",
}

MAX_RESUME_PROMPT_ATTEMPTS = 2
SAFE_ACTIONS_BEFORE_PROMPT = 2
WAIT_OBSERVATIONS_BEFORE_PROMPT = 2
FLOW_SUPERVISOR_LLM_TIMEOUT_SECONDS = 12.0
FLOW_SUPERVISOR_MIN_CONFIDENCE = 0.62

FLOW_SUPERVISOR_SYSTEM = """You are the flow supervisor for an automation platform that controls Trae CN through a Windows Worker.
You decide whether the scheduler should continue the existing deterministic state machine or send a resume prompt to Trae.

Return one JSON object only:
{
  "action": "continue_state_machine|send_resume_prompt",
  "reason": "...",
  "confidence": 0.0,
  "stuck_pattern": "...",
  "evidence": ["..."]
}

Rules:
- Do not judge product quality or write dissatisfaction reasons.
- Choose send_resume_prompt when the old Trae turn is likely stuck after pause, service interruption, collapsed confirmation cards, repeated safe actions, or wait-completion loops.
- Choose continue_state_machine when the current state machine still has a clear safe next action and there is not enough evidence that the old Trae turn is stale.
- Never suggest arbitrary UI clicks, deleting files, restarting unrelated local processes, or opening a new task.
- Prefer preserving the current project and continuing the same round over starting a new Trae task.
"""


def decide_flow_recovery(
    *,
    event: str,
    command: WorkerCommand,
    job: Job | None,
    round_: TaskRound | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic fallback decision for stuck Trae orchestration states."""

    context = _context(event=event, command=command, job=job, round_=round_, extra=extra or {})
    resume_attempts = int(context.get("resume_prompt_attempts") or 0)
    if resume_attempts >= MAX_RESUME_PROMPT_ATTEMPTS:
        return _decision(
            "continue_state_machine",
            "resume_prompt_limit_reached",
            context,
            confidence=0.8,
        )

    reason = str(context.get("recovery_reason") or "")
    previous_type = str(context.get("previous_command_type") or "")
    resume_after_pause = bool(context.get("resume_after_pause"))

    if event == "resume_diagnosis" and previous_type == WorkerCommandType.WAIT_COMPLETION.value:
        if resume_after_pause and _is_recoverable(reason):
            return _decision(
                "send_resume_prompt",
                "paused_wait_completion_recoverable_state",
                context,
                confidence=0.92,
            )
        if _safe_action_attempts(context) >= SAFE_ACTIONS_BEFORE_PROMPT and _is_recoverable(reason):
            return _decision(
                "send_resume_prompt",
                "safe_action_loop_after_resume_diagnosis",
                context,
                confidence=0.86,
            )

    if event == "wait_failure":
        if _is_recoverable(reason) and (
            bool(context.get("resume_after_pause"))
            or _wait_observations(context) >= WAIT_OBSERVATIONS_BEFORE_PROMPT
            or _safe_action_attempts(context) >= SAFE_ACTIONS_BEFORE_PROMPT
        ):
            return _decision(
                "send_resume_prompt",
                "wait_failure_recoverable_loop",
                context,
                confidence=0.84,
            )
        if (
            reason == "wait_completion_timeout"
            and _wait_observations(context) >= WAIT_OBSERVATIONS_BEFORE_PROMPT
            and _safe_action_attempts(context) >= 1
        ):
            return _decision(
                "send_resume_prompt",
                "wait_timeout_after_recovery_actions",
                context,
                confidence=0.78,
            )

    return _decision("continue_state_machine", "no_flow_override", context, confidence=0.55)


def decide_flow_recovery_for_orchestrator(
    db: Session,
    *,
    event: str,
    command: WorkerCommand,
    job: Job | None,
    round_: TaskRound | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flow-level recovery decision with an optional LLM supervisor.

    The deterministic decision remains the hard fallback. The LLM can only
    confirm the fallback action or upgrade an uncertain state into a resume
    prompt; it cannot force an unsafe action outside the supported contract.
    """

    rule_decision = decide_flow_recovery(event=event, command=command, job=job, round_=round_, extra=extra)
    if not _should_consult_llm(rule_decision):
        return rule_decision

    llm_result = _llm_flow_decision(
        db,
        event=event,
        command=command,
        job=job,
        round_=round_,
        extra=extra or {},
        rule_decision=rule_decision,
    )
    if not llm_result:
        return rule_decision
    if "error" in llm_result:
        enriched = dict(rule_decision)
        enriched["llm_error"] = llm_result["error"]
        return enriched

    llm_decision = llm_result.get("decision") if isinstance(llm_result.get("decision"), dict) else {}
    if _can_accept_llm_decision(rule_decision, llm_decision):
        return llm_decision

    enriched = dict(rule_decision)
    enriched["llm_advice"] = _compact_decision(llm_decision)
    return enriched


def _context(
    *,
    event: str,
    command: WorkerCommand,
    job: Job | None,
    round_: TaskRound | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    payload = command.payload if isinstance(command.payload, dict) else {}
    data = extra.get("data") if isinstance(extra.get("data"), dict) else {}
    supervisor = data.get("supervisor_decision") if isinstance(data.get("supervisor_decision"), dict) else {}
    turn = data.get("trae_turn") if isinstance(data.get("trae_turn"), dict) else {}
    current_gate = data.get("current_turn_gate") if isinstance(data.get("current_turn_gate"), dict) else {}
    output_probe = data.get("output_probe") if isinstance(data.get("output_probe"), dict) else {}
    trace_probe = data.get("trace_probe") if isinstance(data.get("trace_probe"), dict) else {}
    suggested = data.get("suggested_intervention") if isinstance(data.get("suggested_intervention"), dict) else {}

    reason = _first_reason(
        extra.get("recovery_reason"),
        extra.get("diagnosis_state"),
        output_probe.get("reason"),
        trace_probe.get("reason"),
        current_gate.get("reason"),
        supervisor.get("reason"),
        turn.get("reason"),
    )

    return {
        "event": event,
        "job_id": job.id if job else command.job_id,
        "round_id": round_.id if round_ else command.round_id,
        "job_status": str(job.status) if job else "",
        "round_status": str(round_.status) if round_ else "",
        "command_id": command.id,
        "command_type": command.command_type,
        "previous_command_type": str(payload.get("previous_command_type") or ""),
        "resume_after_pause": bool(payload.get("resume_after_stop") or payload.get("resume_after_pause")),
        "resume_strategy": str(payload.get("resume_strategy") or ""),
        "recovery_reason": reason,
        "diagnosis_state": str(extra.get("diagnosis_state") or ""),
        "suggested_mode": str(suggested.get("mode") or ""),
        "suggested_action": str(suggested.get("action") or ""),
        "continue_attempts": int(payload.get("continue_attempts") or 0),
        "wait_observation_attempts": int(payload.get("wait_observation_attempts") or 0),
        "resume_prompt_attempts": int(payload.get("resume_prompt_attempts") or 0),
        "safe_action_attempts": _count_safe_actions(data),
        "turn_reason": str(turn.get("reason") or ""),
        "supervisor_reason": str(supervisor.get("reason") or ""),
        "source": "rule_fallback",
    }


def _decision(action: str, reason: str, context: dict[str, Any], *, confidence: float) -> dict[str, Any]:
    return {
        "action": action,
        "reason": reason,
        "confidence": confidence,
        "source": "rule_fallback",
        "context": context,
        "allowed_actions": [
            "continue_state_machine",
            "send_resume_prompt",
            "manual_required",
            "collect_trace",
        ],
    }


def _is_recoverable(reason: str) -> bool:
    if reason in RECOVERABLE_REASONS:
        return True
    return reason.startswith("trae_turn_not_completed")


def _wait_observations(context: dict[str, Any]) -> int:
    return int(context.get("wait_observation_attempts") or 0)


def _safe_action_attempts(context: dict[str, Any]) -> int:
    return max(int(context.get("continue_attempts") or 0), int(context.get("safe_action_attempts") or 0))


def _count_safe_actions(data: dict[str, Any]) -> int:
    interventions = data.get("interventions") if isinstance(data.get("interventions"), list) else []
    count = 0
    for item in interventions:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") in {"applied", "clicked", "completed"}:
            count += 1
    return count


def _first_reason(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _should_consult_llm(rule_decision: dict[str, Any]) -> bool:
    context = rule_decision.get("context") if isinstance(rule_decision.get("context"), dict) else {}
    if str(context.get("event") or "") not in {"resume_diagnosis", "wait_failure"}:
        return False
    if int(context.get("resume_prompt_attempts") or 0) >= MAX_RESUME_PROMPT_ATTEMPTS:
        return False
    return any(
        [
            rule_decision.get("action") == "send_resume_prompt",
            context.get("resume_after_pause"),
            context.get("recovery_reason"),
            context.get("diagnosis_state"),
            int(context.get("wait_observation_attempts") or 0) > 0,
            _safe_action_attempts(context) > 0,
        ]
    )


def _llm_flow_decision(
    db: Session,
    *,
    event: str,
    command: WorkerCommand,
    job: Job | None,
    round_: TaskRound | None,
    extra: dict[str, Any],
    rule_decision: dict[str, Any],
) -> dict[str, Any] | None:
    if not job or not job.user_id:
        return None
    user = db.scalar(select(User).where(User.id == job.user_id))
    if not user:
        return None
    role = db.scalar(select(UserRole).where(UserRole.user_id == user.id, UserRole.role_key == "flow_supervisor"))
    if role and not role.enabled:
        return None
    model_key = role.model_config_key if role else "default"
    messages = [
        {"role": "system", "content": FLOW_SUPERVISOR_SYSTEM},
        {
            "role": "user",
            "content": json.dumps(
                _llm_context(
                    event=event,
                    command=command,
                    job=job,
                    round_=round_,
                    extra=extra,
                    rule_decision=rule_decision,
                ),
                ensure_ascii=False,
            ),
        },
    ]
    try:
        config = model_config_from_settings(load_user_settings(db, user.id), model_key)
        config = replace(
            config,
            timeout_seconds=min(max(float(config.timeout_seconds or 1.0), 1.0), FLOW_SUPERVISOR_LLM_TIMEOUT_SECONDS),
        )
        result = LLMClient().complete(config, messages, purpose="flow_supervisor")
        proposal = _parse_json_object(result.text)
    except (LLMError, Exception) as exc:
        return {"error": str(exc)[:500]}

    decision = _normalize_llm_decision(proposal, rule_decision, model=result.model, wire_api=result.wire_api)
    if not decision:
        return {"error": "LLM flow supervisor returned an unsupported decision"}
    return {"decision": decision}


def _llm_context(
    *,
    event: str,
    command: WorkerCommand,
    job: Job,
    round_: TaskRound | None,
    extra: dict[str, Any],
    rule_decision: dict[str, Any],
) -> dict[str, Any]:
    payload = command.payload if isinstance(command.payload, dict) else {}
    return {
        "event": event,
        "allowed_actions": ["continue_state_machine", "send_resume_prompt"],
        "rule_fallback_decision": _compact_decision(rule_decision),
        "flow_context": rule_decision.get("context") if isinstance(rule_decision.get("context"), dict) else {},
        "job": {
            "id": job.id,
            "status": str(job.status),
            "original_requirement": _short_text(job.scope_text, 5000),
            "directions": [_short_text(item, 1200) for item in (job.directions or [])[:8]],
        },
        "round": {
            "id": round_.id if round_ else None,
            "status": str(round_.status) if round_ else "",
            "round_index": round_.round_index if round_ else None,
            "prompt_sent_to_trae": _short_text(round_.prompt if round_ else "", 8000),
        },
        "worker_command": {
            "id": command.id,
            "type": command.command_type,
            "payload": _compact_value(payload, max_string=2500, max_items=80),
        },
        "worker_result_extra": _compact_value(extra, max_string=2500, max_items=80),
        "decision_contract": {
            "send_resume_prompt_means": "send a natural-language continuation prompt into the existing Trae task, with open_new_task=false",
            "continue_state_machine_means": "let the existing deterministic wait/diagnose/click/trace logic keep running",
            "unsupported_actions_are_invalid": True,
        },
    }


def _normalize_llm_decision(
    proposal: dict[str, Any],
    rule_decision: dict[str, Any],
    *,
    model: str,
    wire_api: str,
) -> dict[str, Any] | None:
    action = str(proposal.get("action") or "").strip()
    if action not in {"continue_state_machine", "send_resume_prompt"}:
        return None
    confidence = _float(proposal.get("confidence"), 0.0)
    reason = str(proposal.get("reason") or "llm_flow_supervisor").strip()[:300]
    context = rule_decision.get("context") if isinstance(rule_decision.get("context"), dict) else {}
    decision = _decision(action, reason, context, confidence=confidence)
    decision["source"] = "llm"
    decision["llm_model"] = model
    decision["llm_wire_api"] = wire_api
    decision["llm_stuck_pattern"] = str(proposal.get("stuck_pattern") or "").strip()[:300]
    decision["llm_evidence"] = _string_list(proposal.get("evidence"), limit=8)
    decision["fallback_decision"] = _compact_decision(rule_decision)
    return decision


def _can_accept_llm_decision(rule_decision: dict[str, Any], llm_decision: dict[str, Any]) -> bool:
    if not llm_decision:
        return False
    action = str(llm_decision.get("action") or "")
    confidence = _float(llm_decision.get("confidence"), 0.0)
    if action == rule_decision.get("action"):
        return confidence >= 0.5
    if rule_decision.get("action") == "continue_state_machine" and action == "send_resume_prompt":
        return confidence >= FLOW_SUPERVISOR_MIN_CONFIDENCE
    return False


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM flow supervisor response must be a JSON object")
    return parsed


def _compact_decision(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": decision.get("action"),
        "reason": decision.get("reason"),
        "confidence": decision.get("confidence"),
        "source": decision.get("source"),
        "context": _compact_value(decision.get("context"), max_string=1200, max_items=40),
    }


def _compact_value(value: Any, *, max_string: int = 2000, max_items: int = 50, depth: int = 0) -> Any:
    if depth >= 5:
        return _short_text(value, 300)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                result["_truncated"] = True
                break
            key_text = str(key)
            if _secret_like_key(key_text):
                result[key_text] = "[redacted]"
                continue
            result[key_text] = _compact_value(item, max_string=max_string, max_items=max_items, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [
            _compact_value(item, max_string=max_string, max_items=max_items, depth=depth + 1)
            for item in value[:max_items]
        ]
    if isinstance(value, tuple):
        return [_compact_value(item, max_string=max_string, max_items=max_items, depth=depth + 1) for item in value[:max_items]]
    if isinstance(value, str):
        return _short_text(value, max_string)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _short_text(value, max_string)


def _secret_like_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in ("token", "secret", "password", "api_key", "private_key", "authorization"))


def _short_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def _string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(_short_text(text, 300))
        if len(result) >= limit:
            break
    return result


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
