import hashlib
import time
from typing import Callable

from worker.trae.diagnose import detect_terminal_prompt, diagnose_ui
from worker.trae.intervene import apply_intervention
from worker.trae.session_probe import probe_latest_trae_turn
from worker.trae.supervisor import SupervisorObservation, decide_next_action
from worker.trae.trace_copy import probe_trace
from worker.trae.window import TraeAutomationError, find_trae_window, focus_trae, window_text_snapshot

BUSY_MARKERS = (
    "\u751f\u6210\u4e2d",
    "\u6b63\u5728\u751f\u6210",
    "\u505c\u6b62\u751f\u6210",
    "Stop generating",
    "Generating",
    "Running",
    "Thinking",
    "\u8fdb\u884c\u4e2d",
    "\u6267\u884c\u4e2d",
    "\u5904\u7406\u4e2d",
    "\u601d\u8003\u4e2d",
)
RECOVERABLE_OUTPUT_REASONS = {"awaiting_continuation", "service_interrupted"}
WINDOW_CHROME_TEXTS = {"最小化", "最大化", "关闭", "Minimize", "Maximize", "Close"}
MIN_COMPLETION_TEXT_CHARS = 80
WINDOW_CHROME_TEXTS = WINDOW_CHROME_TEXTS | {
    "\u6700\u5c0f\u5316",
    "\u6700\u5927\u5316",
    "\u6062\u590d",
    "\u8fd8\u539f",
    "\u5173\u95ed",
    "Restore",
}


def wait_completion(
    timeout_seconds: float = 900.0,
    stable_seconds: float = 15.0,
    poll_interval_seconds: float = 2.0,
    intervention_idle_seconds: float = 60.0,
    max_interventions: int = 3,
    cancellation_check: Callable[[], None] | None = None,
    prompt: str = "",
    workspace_path: str = "",
    sent_at_epoch: float | None = None,
    sent_at: str = "",
) -> dict:
    focus_trae(timeout_seconds=min(10.0, timeout_seconds))
    deadline = time.monotonic() + timeout_seconds
    stable_since: float | None = None
    last_signature = ""
    latest_text = ""
    last_change_at = time.monotonic()
    interventions: list[dict] = []

    while time.monotonic() < deadline:
        if cancellation_check:
            cancellation_check()
        window = find_trae_window(timeout_seconds=2.0)
        latest_text = window_text_snapshot(window)
        signature = hashlib.sha256(latest_text.encode("utf-8", errors="ignore")).hexdigest()
        busy = any(marker in latest_text for marker in BUSY_MARKERS)
        changed = signature != last_signature
        if not latest_text.strip():
            stable_since = None
            last_signature = signature
        elif not busy and not changed:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= stable_seconds:
                decision = _supervisor_decision(
                    latest_text=latest_text,
                    busy=busy,
                    prompt=prompt,
                    workspace_path=workspace_path,
                    sent_at_epoch=sent_at_epoch,
                    sent_at=sent_at,
                    idle_seconds=time.monotonic() - last_change_at,
                    intervention_idle_seconds=intervention_idle_seconds,
                    interventions=interventions,
                    max_interventions=max_interventions,
                )
                outcome = _handle_supervisor_decision(
                    decision=decision,
                    stable_seconds=stable_seconds,
                    latest_text=latest_text,
                    interventions=interventions,
                    timeout_seconds=timeout_seconds,
                )
                if outcome.get("status") == "completed":
                    return outcome
                if outcome.get("status") == "failed":
                    raise TraeAutomationError(str(outcome.get("error") or "Trae supervisor decision failed"))
                if outcome.get("status") == "applied":
                    stable_since = None
                    last_change_at = time.monotonic()
                    _sleep_with_cancellation(poll_interval_seconds, cancellation_check)
                    continue
                stable_since = None
                if not decision.get("recoverable"):
                    last_change_at = time.monotonic()
                _sleep_with_cancellation(poll_interval_seconds, cancellation_check)
                continue
        else:
            stable_since = None
            last_signature = signature
            if changed:
                last_change_at = time.monotonic()
        if (
            intervention_idle_seconds > 0
            and not busy
            and time.monotonic() - last_change_at >= intervention_idle_seconds
            and len(interventions) < max_interventions
        ):
            decision = _supervisor_decision(
                latest_text=latest_text,
                busy=busy,
                prompt=prompt,
                workspace_path=workspace_path,
                sent_at_epoch=sent_at_epoch,
                sent_at=sent_at,
                idle_seconds=time.monotonic() - last_change_at,
                intervention_idle_seconds=intervention_idle_seconds,
                interventions=interventions,
                max_interventions=max_interventions,
            )
            outcome = _handle_supervisor_decision(
                decision=decision,
                stable_seconds=stable_seconds,
                latest_text=latest_text,
                interventions=interventions,
                timeout_seconds=timeout_seconds,
            )
            if outcome.get("status") == "completed":
                return outcome
            if outcome.get("status") == "failed":
                raise TraeAutomationError(str(outcome.get("error") or "Trae supervisor decision failed"))
            if outcome.get("status") == "applied":
                last_change_at = time.monotonic()
                stable_since = None
        _sleep_with_cancellation(poll_interval_seconds, cancellation_check)

    raise TraeAutomationError("Trae output did not become stable before wait_completion timeout")


def _supervisor_decision(
    *,
    latest_text: str,
    busy: bool,
    prompt: str,
    workspace_path: str,
    sent_at_epoch: float | None,
    sent_at: str,
    idle_seconds: float,
    intervention_idle_seconds: float,
    interventions: list[dict],
    max_interventions: int,
) -> dict:
    output_probe = probe_trace(latest_text)
    turn_probe = probe_latest_trae_turn(
        prompt=prompt,
        workspace_path=workspace_path,
        sent_after_epoch=sent_at_epoch,
        sent_after=sent_at,
    )
    terminal_prompt = detect_terminal_prompt(latest_text)
    return decide_next_action(
        SupervisorObservation(
            latest_text=latest_text,
            output_probe=output_probe,
            turn_probe=turn_probe,
            busy=busy,
            terminal_prompt=terminal_prompt,
            idle_seconds=idle_seconds,
            intervention_idle_seconds=intervention_idle_seconds,
            intervention_count=len(interventions),
            max_interventions=max_interventions,
            window_chrome_only=_looks_like_window_chrome_only(latest_text),
        )
    )


def _handle_supervisor_decision(
    *,
    decision: dict,
    stable_seconds: float,
    latest_text: str,
    interventions: list[dict],
    timeout_seconds: float,
) -> dict:
    action = str(decision.get("action") or "wait")
    if action == "collect_trace":
        return _completed_result(
            stable_seconds=stable_seconds,
            latest_text=latest_text,
            output_probe=decision.get("output_probe") or {},
            turn_probe=decision.get("turn_probe") or {},
            gate=decision.get("completion_gate") or {},
            interventions=interventions,
            supervisor_decision=decision,
        )
    if action == "fail":
        reason = str(decision.get("reason") or "supervisor_failed")
        if reason == "window_chrome_only":
            message = "Trae output did not contain assistant content; only window chrome text was detected"
        else:
            message = f"Trae supervisor could not recover current turn ({reason})"
        return {"status": "failed", "error": message, "supervisor_decision": decision}
    if action in {"recover_service_interruption", "continue_output", "answer_terminal_prompt", "apply_pending_ui", "diagnose_idle"}:
        reason = str(decision.get("reason") or action)
        if action == "answer_terminal_prompt":
            reason = "terminal_prompt"
        elif action == "diagnose_idle" and reason not in RECOVERABLE_OUTPUT_REASONS:
            reason = f"completion_gate:{reason}"
        intervention = _try_auto_intervention(
            reason=reason,
            timeout_seconds=min(10.0, max(2.0, timeout_seconds)),
        )
        intervention["supervisor_action"] = action
        intervention["supervisor_reason"] = str(decision.get("reason") or "")
        interventions.append(intervention)
        if action in {"recover_service_interruption", "continue_output"} and intervention.get("status") != "applied":
            return {
                "status": "failed",
                "error": f"Trae output is stable but not complete ({decision.get('reason')}); auto intervention failed",
                "supervisor_decision": decision,
            }
        return {"status": "applied", "intervention": intervention, "supervisor_decision": decision}
    return {"status": "waiting", "supervisor_decision": decision}


def _completed_result(
    *,
    stable_seconds: float,
    latest_text: str,
    output_probe: object,
    turn_probe: object,
    gate: dict,
    interventions: list[dict],
    supervisor_decision: dict | None = None,
) -> dict:
    return {
        "status": "completed",
        "stable_seconds": stable_seconds,
        "text_chars": len(latest_text),
        "text_sample": latest_text[-1000:],
        "output_probe": output_probe,
        "trae_turn": turn_probe,
        "completion_gate": gate,
        "supervisor_decision": supervisor_decision or {},
        "interventions": interventions,
    }


def _looks_like_window_chrome_only(text: str) -> bool:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return True
    if len("\n".join(lines)) < MIN_COMPLETION_TEXT_CHARS and all(line in WINDOW_CHROME_TEXTS for line in lines):
        return True
    return False


def _sleep_with_cancellation(seconds: float, cancellation_check: Callable[[], None] | None) -> None:
    if not cancellation_check:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        cancellation_check()
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    cancellation_check()


def _try_auto_intervention(reason: str, timeout_seconds: float) -> dict:
    try:
        diagnosis = diagnose_ui(timeout_seconds=timeout_seconds, scroll_bottom=True)
    except Exception as exc:
        return {
            "status": "failed",
            "reason": reason,
            "error": str(exc),
            "diagnosis_state": "",
        }
    suggested = diagnosis.get("suggested_intervention") if isinstance(diagnosis, dict) else {}
    if reason == "service_interrupted":
        suggested = {"mode": "continue-text", "text": "\u7ee7\u7eed", "action": "continue"}
    elif not suggested and reason in RECOVERABLE_OUTPUT_REASONS:
        suggested = {"mode": "continue-text", "text": "\u7ee7\u7eed", "action": "continue"}
    if not isinstance(suggested, dict) or not suggested:
        return {
            "status": "skipped",
            "reason": reason,
            "diagnosis_state": diagnosis.get("state") if isinstance(diagnosis, dict) else "",
        }
    try:
        result = apply_intervention(suggested, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return {
            "status": "failed",
            "reason": reason,
            "suggested_intervention": suggested,
            "error": str(exc),
            "diagnosis_state": diagnosis.get("state") if isinstance(diagnosis, dict) else "",
        }
    return {
        "status": result.get("status") or "attempted",
        "reason": reason,
        "suggested_intervention": suggested,
        "result": result,
        "diagnosis_state": diagnosis.get("state") if isinstance(diagnosis, dict) else "",
    }
