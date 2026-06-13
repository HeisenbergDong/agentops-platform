import hashlib
import time
from typing import Callable

from worker.trae.diagnose import detect_terminal_prompt, diagnose_ui
from worker.trae.intervene import apply_intervention
from worker.trae.session_probe import probe_latest_trae_turn
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
RECOVERABLE_TURN_REASONS = {
    "awaiting_current_continuation",
    "no_completed_turn_after_prompt_send",
    "current_turn_missing",
}
WINDOW_CHROME_TEXTS = WINDOW_CHROME_TEXTS | {
    "\u6700\u5c0f\u5316",
    "\u6700\u5927\u5316",
    "\u6062\u590d",
    "\u8fd8\u539f",
    "\u5173\u95ed",
    "Restore",
}
PENDING_INTERVENTION_MARKERS = (
    "\u53d8\u66f4\u5df2\u5b8c\u6210",
    "\u8bf7\u786e\u8ba4\u662f\u5426",
    "\u4fdd\u7559",
    "\u4fdd\u7559\u53d8\u66f4",
    "\u4fdd\u5b58",
    "\u786e\u8ba4\u6267\u884c",
    "\u7ee7\u7eed\u6267\u884c",
    "\u4ecd\u8981\u8fd0\u884c",
    "\u662f\u5426\u7ee7\u7eed",
    "Keep",
    "Keep Changes",
    "Save",
    "Run anyway",
    "Ok to proceed",
)


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
                output_probe = probe_trace(latest_text)
                turn_probe = probe_latest_trae_turn(
                    prompt=prompt,
                    workspace_path=workspace_path,
                    sent_after_epoch=sent_at_epoch,
                    sent_after=sent_at,
                )
                gate = _completion_gate(turn_probe, output_probe, latest_text)
                if gate["passed"]:
                    if _looks_like_window_chrome_only(latest_text):
                        raise TraeAutomationError(
                            "Trae output did not contain assistant content; only window chrome text was detected"
                        )
                    return _completed_result(
                        stable_seconds=stable_seconds,
                        latest_text=latest_text,
                        output_probe=output_probe,
                        turn_probe=turn_probe,
                        gate=gate,
                        interventions=interventions,
                    )
                if output_probe.get("reason") in RECOVERABLE_OUTPUT_REASONS:
                    if len(interventions) >= max_interventions:
                        raise TraeAutomationError(
                            f"Trae output is stable but not complete ({output_probe.get('reason')}); auto intervention limit reached"
                        )
                    intervention = _try_auto_intervention(
                        reason=str(output_probe.get("reason") or "output_recoverable"),
                        timeout_seconds=min(10.0, max(2.0, timeout_seconds)),
                    )
                    interventions.append(intervention)
                    if intervention.get("status") == "applied" and len(interventions) <= max_interventions:
                        stable_since = None
                        last_change_at = time.monotonic()
                        _sleep_with_cancellation(poll_interval_seconds, cancellation_check)
                        continue
                    raise TraeAutomationError(
                        f"Trae output is stable but not complete ({output_probe.get('reason')}); auto intervention failed"
                    )
                terminal_prompt = detect_terminal_prompt(latest_text)
                if terminal_prompt and len(interventions) < max_interventions:
                    intervention = _try_auto_intervention(
                        reason="terminal_prompt",
                        timeout_seconds=min(10.0, max(2.0, timeout_seconds)),
                    )
                    interventions.append(intervention)
                    if intervention.get("status") == "applied":
                        stable_since = None
                        last_change_at = time.monotonic()
                        _sleep_with_cancellation(poll_interval_seconds, cancellation_check)
                        continue
                if _looks_like_window_chrome_only(latest_text):
                    raise TraeAutomationError(
                        "Trae output did not contain assistant content; only window chrome text was detected"
                    )
                if gate.get("recoverable") and len(interventions) < max_interventions:
                    intervention = _try_completion_gate_intervention(
                        gate,
                        timeout_seconds=min(10.0, max(2.0, timeout_seconds)),
                    )
                    if intervention:
                        interventions.append(intervention)
                        if intervention.get("status") == "applied":
                            stable_since = None
                            last_change_at = time.monotonic()
                            _sleep_with_cancellation(poll_interval_seconds, cancellation_check)
                            continue
                if (
                    gate.get("recoverable")
                    and intervention_idle_seconds > 0
                    and time.monotonic() - last_change_at >= intervention_idle_seconds
                    and len(interventions) < max_interventions
                ):
                    intervention = _try_auto_intervention(
                        reason=f"completion_gate:{gate.get('reason') or 'waiting'}",
                        timeout_seconds=min(10.0, max(2.0, timeout_seconds)),
                    )
                    interventions.append(intervention)
                    stable_since = None
                    last_change_at = time.monotonic()
                    _sleep_with_cancellation(poll_interval_seconds, cancellation_check)
                    continue
                stable_since = None
                if not gate.get("recoverable"):
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
            output_probe = probe_trace(latest_text)
            turn_probe = probe_latest_trae_turn(
                prompt=prompt,
                workspace_path=workspace_path,
                sent_after_epoch=sent_at_epoch,
                sent_after=sent_at,
            )
            gate = _completion_gate(turn_probe, output_probe, latest_text)
            if gate["passed"]:
                if _looks_like_window_chrome_only(latest_text):
                    raise TraeAutomationError(
                        "Trae output did not contain assistant content; only window chrome text was detected"
                    )
                return _completed_result(
                    stable_seconds=stable_seconds,
                    latest_text=latest_text,
                    output_probe=output_probe,
                    turn_probe=turn_probe,
                    gate=gate,
                    interventions=interventions,
                )
            reason = str(output_probe.get("reason") or "idle_no_output_change")
            if reason not in RECOVERABLE_OUTPUT_REASONS:
                reason = "idle_no_output_change"
            intervention = _try_auto_intervention(reason=reason, timeout_seconds=min(10.0, max(2.0, timeout_seconds)))
            interventions.append(intervention)
            last_change_at = time.monotonic()
            stable_since = None
        _sleep_with_cancellation(poll_interval_seconds, cancellation_check)

    raise TraeAutomationError("Trae output did not become stable before wait_completion timeout")


def _completed_result(
    *,
    stable_seconds: float,
    latest_text: str,
    output_probe: object,
    turn_probe: object,
    gate: dict,
    interventions: list[dict],
) -> dict:
    return {
        "status": "completed",
        "stable_seconds": stable_seconds,
        "text_chars": len(latest_text),
        "text_sample": latest_text[-1000:],
        "output_probe": output_probe,
        "trae_turn": turn_probe,
        "completion_gate": gate,
        "interventions": interventions,
    }


def _looks_like_window_chrome_only(text: str) -> bool:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return True
    if len("\n".join(lines)) < MIN_COMPLETION_TEXT_CHARS and all(line in WINDOW_CHROME_TEXTS for line in lines):
        return True
    return False


def _completion_gate(turn_probe: object, output_probe: object, latest_text: str) -> dict:
    pending_intervention_visible = _has_pending_intervention_text(latest_text)
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
    }


def _has_pending_intervention_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in PENDING_INTERVENTION_MARKERS)


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


def _try_completion_gate_intervention(gate: dict, timeout_seconds: float) -> dict:
    reason = str(gate.get("reason") or "completion_gate_waiting")
    if reason in {"pending_intervention_visible", "awaiting_current_continuation"}:
        return _try_auto_intervention(reason=reason, timeout_seconds=timeout_seconds)
    return {}


def _diagnose_suggested_intervention(timeout_seconds: float) -> dict:
    try:
        diagnosis = diagnose_ui(timeout_seconds=timeout_seconds, scroll_bottom=True)
    except Exception:
        return {}
    suggested = diagnosis.get("suggested_intervention") if isinstance(diagnosis, dict) else {}
    return suggested if isinstance(suggested, dict) else {}
