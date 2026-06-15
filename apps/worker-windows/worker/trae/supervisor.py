from __future__ import annotations

from dataclasses import dataclass
from typing import Any


RECOVERABLE_OUTPUT_REASONS = {"awaiting_continuation", "service_interrupted"}
RECOVERABLE_TURN_REASONS = {
    "awaiting_current_continuation",
    "no_completed_turn_after_prompt_send",
    "current_turn_missing",
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
    "Keep",
    "Keep Changes",
    "Save",
    "Run anyway",
    "Ok to proceed",
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
    activity_summary = {
        "recent": bool(observation.recent_activity),
        "source": str(observation.activity_source or ""),
        "quiet_seconds": observation.activity_quiet_seconds,
        "log_tail_hash": str(observation.log_tail_hash or ""),
        "project_last_write": str(observation.project_last_write or ""),
    }
    context = {
        "completion_gate": gate,
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

    ui_completion_visible = has_ui_completion_text(observation.latest_text)

    if gate["passed"]:
        return {
            "action": "collect_trace",
            "reason": "trae_turn_completed",
            "recoverable": False,
            **context,
        }

    output_reason = str((observation.output_probe or {}).get("reason") or "")
    if ui_completion_visible and output_reason not in RECOVERABLE_OUTPUT_REASONS and not observation.window_chrome_only:
        return {
            "action": "collect_trace",
            "reason": "ui_completion_detected",
            "recoverable": False,
            **context,
        }

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
                "action": "wait",
                "reason": "pending_intervention_visible_limit_reached",
                "recoverable": True,
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
