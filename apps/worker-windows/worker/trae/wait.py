import hashlib
import time
from typing import Callable

from worker.trae.diagnose import detect_terminal_prompt, diagnose_ui
from worker.trae.intervene import apply_intervention
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
)
RECOVERABLE_OUTPUT_REASONS = {"awaiting_continuation", "service_interrupted"}


def wait_completion(
    timeout_seconds: float = 900.0,
    stable_seconds: float = 15.0,
    poll_interval_seconds: float = 2.0,
    intervention_idle_seconds: float = 60.0,
    max_interventions: int = 3,
    cancellation_check: Callable[[], None] | None = None,
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
                intervention_reason = ""
                terminal_prompt = detect_terminal_prompt(latest_text)
                if terminal_prompt:
                    intervention_reason = "terminal_prompt"
                elif len(interventions) < max_interventions:
                    quick_intervention = _diagnose_suggested_intervention(timeout_seconds=min(10.0, max(2.0, timeout_seconds)))
                    if quick_intervention:
                        intervention_reason = "stable_waiting_for_intervention"
                if intervention_reason and len(interventions) < max_interventions:
                    intervention = _try_auto_intervention(
                        reason=intervention_reason,
                        timeout_seconds=min(10.0, max(2.0, timeout_seconds)),
                    )
                    interventions.append(intervention)
                    if intervention.get("status") == "applied":
                        stable_since = None
                        last_change_at = time.monotonic()
                        _sleep_with_cancellation(poll_interval_seconds, cancellation_check)
                        continue
                output_probe = probe_trace(latest_text)
                if output_probe.get("reason") in RECOVERABLE_OUTPUT_REASONS:
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
                return {
                    "status": "completed",
                    "stable_seconds": stable_seconds,
                    "text_chars": len(latest_text),
                    "text_sample": latest_text[-1000:],
                    "output_probe": output_probe,
                    "interventions": interventions,
                }
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
            intervention = _try_auto_intervention(reason="idle_no_output_change", timeout_seconds=min(10.0, max(2.0, timeout_seconds)))
            interventions.append(intervention)
            last_change_at = time.monotonic()
            stable_since = None
        _sleep_with_cancellation(poll_interval_seconds, cancellation_check)

    raise TraeAutomationError("Trae output did not become stable before wait_completion timeout")


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
    if not suggested and reason in RECOVERABLE_OUTPUT_REASONS:
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


def _diagnose_suggested_intervention(timeout_seconds: float) -> dict:
    try:
        diagnosis = diagnose_ui(timeout_seconds=timeout_seconds, scroll_bottom=True)
    except Exception:
        return {}
    suggested = diagnosis.get("suggested_intervention") if isinstance(diagnosis, dict) else {}
    return suggested if isinstance(suggested, dict) else {}
