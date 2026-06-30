from __future__ import annotations

from dataclasses import dataclass
from typing import Any


RECOVERABLE_OUTPUT_REASONS = {"awaiting_continuation", "service_interrupted"}
RECOVERABLE_INTERRUPTED_TURN_REASONS = {"trae_turn_not_completed:interrupted"}
RECOVERABLE_TURN_REASONS = {
    "awaiting_current_continuation",
    "no_completed_turn_after_prompt_send",
    "current_turn_missing",
    "low_confidence_context_match",
    "trae_turn_not_completed:interrupted",
}
PENDING_INTERVENTION_MARKERS = (
    "\u53d8\u66f4\u5df2\u5b8c\u6210",
    "\u8bf7\u786e\u8ba4\u662f\u5426",
    "\u68c0\u6d4b\u5230\u9ad8\u98ce\u9669\u547d\u4ee4",
    "\u4fdd\u7559",
    "\u4fdd\u7559\u53d8\u66f4",
    "\u4fdd\u5b58",
    "\u786e\u8ba4\u6267\u884c",
    "\u7ee7\u7eed\u6267\u884c",
    "\u4ecd\u8981\u8fd0\u884c",
    "\u8fd0\u884c\u547d\u4ee4\u53ef\u80fd\u4f1a\u5e26\u6765\u4e25\u91cd\u540e\u679c",
    "\u662f\u5426\u4ecd\u8981\u5728\u6c99\u7bb1\u4e2d\u8fd0\u884c",
    "\u662f\u5426\u4ecd\u8981\u8fd0\u884c",
    "\u662f\u5426\u7ee7\u7eed",
    "\u6b63\u5728\u7b49\u5f85\u4f60\u7684\u64cd\u4f5c",
    "\u6b63\u5728\u7b49\u5f85\u60a8\u7684\u64cd\u4f5c",
    "\u7b49\u5f85\u4f60\u7684\u64cd\u4f5c",
    "\u7b49\u5f85\u60a8\u7684\u64cd\u4f5c",
    "\u7b49\u5f85\u64cd\u4f5c",
    "\u5220\u9664",
    "\u79fb\u9664",
    "\u4e22\u5f03",
    "\u653e\u5f03",
    "Keep",
    "Keep Changes",
    "Save",
    "Run anyway",
    "Ok to proceed",
    "waiting for your operation",
    "waiting for your action",
    "Delete",
    "Remove",
    "Discard",
)
UI_COMPLETION_MARKERS = (
    "\u4efb\u52a1\u5b8c\u6210",
    "\u4efb\u52a1\u5df2\u5b8c\u6210",
    "\u5df2\u5b8c\u6210\u4efb\u52a1",
    "Task completed",
    "task completed",
)


@dataclass(frozen=True)
class SupervisorObservation:
    latest_text: str
    output_probe: dict[str, Any]
    turn_probe: dict[str, Any] | None
    busy: bool = False
    terminal_prompt: dict[str, Any] | None = None
    idle_seconds: float = 0.0
    intervention_idle_seconds: float = 0.0
    intervention_count: int = 0
    max_interventions: int = 0
    window_chrome_only: bool = False
    recent_activity: bool = False
    activity_source: str = ""
    activity_quiet_seconds: float | None = None
    log_tail_hash: str = ""
    project_last_write: str = ""
    watcher_observation: dict[str, Any] | None = None


def decide_next_action(observation: SupervisorObservation) -> dict[str, Any]:
    pending_intervention_visible = has_pending_intervention_text(observation.latest_text)
    gate = completion_gate(observation.turn_probe, observation.output_probe, observation.latest_text)
    completion_decision = trae_turn_completion_decision(observation, gate)
    activity_summary = {
        "recent": bool(observation.recent_activity),
        "source": str(observation.activity_source or ""),
        "quiet_seconds": observation.activity_quiet_seconds,
        "log_tail_hash": str(observation.log_tail_hash or ""),
        "project_last_write": str(observation.project_last_write or ""),
    }
    context = {
        "completion_gate": gate,
        "trae_turn_completion_decision": completion_decision,
        "pending_intervention_visible": pending_intervention_visible,
        "output_probe": observation.output_probe,
        "turn_probe": observation.turn_probe or {},
        "watcher_observation": observation.watcher_observation or {},
        "activity_summary": activity_summary,
        "idle_seconds": round(float(observation.idle_seconds or 0.0), 3),
        "intervention_idle_seconds": round(float(observation.intervention_idle_seconds or 0.0), 3),
        "intervention_count": int(observation.intervention_count or 0),
        "max_interventions": int(observation.max_interventions or 0),
    }

    if completion_decision["is_complete"]:
        return {
            "action": "collect_trace",
            "reason": str(completion_decision.get("reason") or "trae_turn_completed"),
            "recoverable": False,
            **context,
        }

    output_reason = str((observation.output_probe or {}).get("reason") or "")
    if output_reason in RECOVERABLE_OUTPUT_REASONS:
        if observation.intervention_count >= observation.max_interventions:
            return {
                "action": "fail",
                "reason": f"{output_reason}_intervention_limit_reached",
                "recoverable": False,
                **context,
            }
        return {
            "action": "recover_service_interruption" if output_reason == "service_interrupted" else "continue_output",
            "reason": output_reason,
            "recoverable": True,
            **context,
        }

    gate_reason = str(gate.get("reason") or "")
    if gate_reason in RECOVERABLE_INTERRUPTED_TURN_REASONS:
        return {
            "action": "recover_interrupted_turn",
            "reason": gate_reason,
            "recoverable": True,
            **context,
        }

    if observation.terminal_prompt:
        if observation.intervention_count >= observation.max_interventions:
            return {
                "action": "fail",
                "reason": "terminal_prompt_intervention_limit_reached",
                "recoverable": False,
                **context,
            }
        return {
            "action": "answer_terminal_prompt",
            "reason": str(observation.terminal_prompt.get("reason") or "terminal_prompt"),
            "recoverable": True,
            "terminal_prompt": observation.terminal_prompt,
            **context,
        }

    if gate.get("reason") == "pending_intervention_visible":
        idle_ready = _idle_ready(observation)
        if not idle_ready:
            return {
                "action": "wait",
                "reason": "pending_intervention_visible",
                "recoverable": True,
                **context,
            }
        if observation.intervention_count >= observation.max_interventions:
            return {
                "action": "fail",
                "reason": "pending_intervention_visible_limit_reached",
                "recoverable": False,
                **context,
            }
        return {
            "action": "apply_pending_ui",
            "reason": "pending_intervention_visible",
            "recoverable": True,
            **context,
        }

    if observation.window_chrome_only:
        if _idle_ready(observation):
            return {
                "action": "diagnose_idle",
                "reason": "window_chrome_only",
                "recoverable": True,
                **context,
            }
        return {
            "action": "wait",
            "reason": "window_chrome_only",
            "recoverable": True,
            **context,
        }

    if observation.recent_activity:
        return {
            "action": "wait",
            "reason": "recent_trae_activity",
            "recoverable": True,
            **context,
        }

    if gate.get("recoverable"):
        idle_ready = _idle_ready(observation)
        if idle_ready:
            return {
                "action": "diagnose_idle",
                "reason": str(gate.get("reason") or "completion_gate_waiting"),
                "recoverable": True,
                **context,
            }
        return {
            "action": "wait",
            "reason": str(gate.get("reason") or "completion_gate_waiting"),
            "recoverable": True,
            **context,
        }

    return {
        "action": "wait",
        "reason": str(gate.get("reason") or "supervisor_no_action"),
        "recoverable": False,
        **context,
    }


def completion_gate(turn_probe: object, output_probe: object, latest_text: str) -> dict[str, Any]:
    pending_intervention_visible = has_pending_intervention_text(latest_text)
    if isinstance(turn_probe, dict) and turn_probe.get("status") == "found":
        turn_status = str(turn_probe.get("turn_status") or "")
        if turn_status == "completed":
            return {
                "passed": True,
                "reason": "ok",
                "session_id": str(turn_probe.get("session_id") or ""),
                "user_message_id": str(turn_probe.get("user_message_id") or ""),
                "pending_intervention_visible": pending_intervention_visible,
            }
    if pending_intervention_visible:
        return {"passed": False, "reason": "pending_intervention_visible", "recoverable": True}
    if not isinstance(turn_probe, dict):
        return {"passed": False, "reason": "turn_probe_unavailable", "recoverable": False}
    if turn_probe.get("status") != "found":
        reason = str(turn_probe.get("reason") or "current_turn_missing")
        return {
            "passed": False,
            "reason": reason,
            "recoverable": reason in RECOVERABLE_TURN_REASONS,
            "candidate": turn_probe.get("candidate") if isinstance(turn_probe.get("candidate"), dict) else None,
        }
    turn_status = str(turn_probe.get("turn_status") or "")
    if turn_status != "completed":
        return {
            "passed": False,
            "reason": f"trae_turn_not_completed:{turn_status or 'unknown'}",
            "recoverable": True,
            "session_id": str(turn_probe.get("session_id") or ""),
            "user_message_id": str(turn_probe.get("user_message_id") or ""),
        }
    return {
        "passed": True,
        "reason": "ok",
        "session_id": str(turn_probe.get("session_id") or ""),
        "user_message_id": str(turn_probe.get("user_message_id") or ""),
        "pending_intervention_visible": pending_intervention_visible,
    }


def trae_turn_completion_decision(observation: SupervisorObservation, gate: dict[str, Any] | None = None) -> dict[str, Any]:
    gate = gate or completion_gate(observation.turn_probe, observation.output_probe, observation.latest_text)
    output_reason = str((observation.output_probe or {}).get("reason") or "")
    evidence: list[str] = []
    risk = "safe"
    score = 0.0

    if gate.get("passed"):
        evidence.append("completion_gate_passed")
        score += 0.88

    if has_ui_completion_text(observation.latest_text):
        evidence.append("ui_completion_visible")
        score += 0.42

    if has_pending_intervention_text(observation.latest_text):
        evidence.append("pending_keep_or_safe_action_visible")
        score += 0.16

    turn = observation.turn_probe if isinstance(observation.turn_probe, dict) else {}
    candidate = turn.get("candidate") if isinstance(turn.get("candidate"), dict) else {}
    if str(turn.get("turn_status") or "") == "completed":
        evidence.append("turn_probe_completed")
        score += 0.62
    elif str(candidate.get("turn_status") or candidate.get("status") or "") == "completed":
        evidence.append("completed_turn_candidate")
        score += 0.35
    elif str(turn.get("reason") or "") == "low_confidence_context_match":
        evidence.append("low_confidence_completed_turn_candidate")
        score += 0.25

    watcher = observation.watcher_observation if isinstance(observation.watcher_observation, dict) else {}
    project_write = watcher.get("project_write") if isinstance(watcher.get("project_write"), dict) else {}
    if project_write.get("mtime") or observation.project_last_write:
        evidence.append("project_write_detected")
        score += 0.18

    if not observation.recent_activity:
        evidence.append("no_recent_meaningful_activity")
        score += 0.16
    elif observation.activity_quiet_seconds is not None:
        try:
            quiet_seconds = float(observation.activity_quiet_seconds)
        except (TypeError, ValueError):
            quiet_seconds = 0.0
        if quiet_seconds >= max(30.0, min(float(observation.intervention_idle_seconds or 0.0), 120.0)):
            evidence.append("activity_quiet_long_enough")
            score += 0.12

    if output_reason in RECOVERABLE_OUTPUT_REASONS:
        evidence.append(f"recoverable_output:{output_reason}")
        score -= 0.72
        risk = "recoverable_before_trace"
    if observation.terminal_prompt:
        evidence.append("terminal_prompt_visible")
        score -= 0.55
        risk = "blocked_terminal_prompt"
    if observation.busy:
        evidence.append("busy_marker_visible")
        score -= 0.5
        risk = "still_generating"
    if observation.window_chrome_only and not gate.get("passed"):
        evidence.append("window_chrome_only")
        score -= 0.2

    confidence = max(0.0, min(0.99, score))
    strong_gate = bool(gate.get("passed"))
    robust_visual = "ui_completion_visible" in evidence and "no_recent_meaningful_activity" in evidence
    robust_candidate = (
        any(item in evidence for item in {"completed_turn_candidate", "low_confidence_completed_turn_candidate"})
        and "project_write_detected" in evidence
        and any(item in evidence for item in {"no_recent_meaningful_activity", "activity_quiet_long_enough"})
    )
    is_complete = bool(
        output_reason not in RECOVERABLE_OUTPUT_REASONS
        and not observation.busy
        and not observation.terminal_prompt
        and (
            strong_gate
            or confidence >= 0.62
            or robust_visual
            or robust_candidate
        )
    )
    reason = "not_complete"
    if is_complete:
        if strong_gate:
            reason = "trae_turn_completed"
        elif "ui_completion_visible" in evidence:
            reason = "ui_completion_detected"
        elif robust_candidate:
            reason = "completion_candidate_with_project_write"
        else:
            reason = "completion_evidence_threshold"
    return {
        "is_complete": is_complete,
        "confidence": round(confidence, 3),
        "next_action": "copy_trace" if is_complete else "wait_or_recover",
        "evidence": evidence,
        "risk": risk,
        "reason": reason,
    }


def has_pending_intervention_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in PENDING_INTERVENTION_MARKERS)


def has_ui_completion_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in UI_COMPLETION_MARKERS)


def _idle_ready(observation: SupervisorObservation) -> bool:
    return (
        observation.intervention_idle_seconds > 0
        and observation.idle_seconds >= observation.intervention_idle_seconds
        and observation.intervention_count < observation.max_interventions
        and not observation.busy
    )
