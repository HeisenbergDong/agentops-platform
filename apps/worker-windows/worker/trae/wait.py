import hashlib
import time
from typing import Callable

from worker.trae.diagnose import detect_terminal_prompt, diagnose_ui
from worker.trae.intervene import apply_intervention
from worker.trae.session_probe import probe_latest_trae_turn
from worker.trae.supervisor import SupervisorObservation, decide_next_action
from worker.trae.trace_copy import probe_trace
from worker.trae.watcher import build_trae_observation
from worker.trae.window import (
    TraeAutomationError,
    find_trae_window,
    focus_trae,
    focus_trae_workspace_or_any,
    wait_for_workspace_window_or_any,
    window_text_snapshot,
)

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
    intervention_idle_seconds: float = 30.0,
    max_interventions: int = 3,
    cancellation_check: Callable[[], None] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    progress_interval_seconds: float = 10.0,
    prompt: str = "",
    workspace_path: str = "",
    sent_at_epoch: float | None = None,
    sent_at: str = "",
    ui_analyst: Callable[[str, dict], dict] | None = None,
    continue_text_already_sent: bool = False,
) -> dict:
    if workspace_path:
        focus_trae_workspace_or_any(
            timeout_seconds=min(10.0, timeout_seconds),
            workspace_path=workspace_path,
        )
    else:
        focus_trae(timeout_seconds=min(10.0, timeout_seconds))
    deadline = time.monotonic() + timeout_seconds
    stable_since: float | None = None
    last_signature = ""
    latest_text = ""
    last_change_at = time.monotonic()
    interventions: list[dict] = []
    diagnostics: list[dict] = []
    last_progress_at: dict[str, float] = {}
    last_decision: dict = {}

    while time.monotonic() < deadline:
        if cancellation_check:
            cancellation_check()
        if workspace_path:
            window = wait_for_workspace_window_or_any(
                timeout_seconds=2.0,
                workspace_path=workspace_path,
            )
        else:
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
                last_decision = decision
                _emit_supervisor_progress(
                    progress_callback,
                    decision,
                    last_progress_at,
                    min_interval=progress_interval_seconds,
                )
                outcome = _handle_supervisor_decision(
                    decision=decision,
                    stable_seconds=stable_seconds,
                    latest_text=latest_text,
                    interventions=interventions,
                    timeout_seconds=timeout_seconds,
                    workspace_path=workspace_path,
                    ui_analyst=ui_analyst,
                    suppress_continue_text=continue_text_already_sent or _has_continue_text_intervention(interventions),
                    progress_callback=progress_callback,
                    last_progress_at=last_progress_at,
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
                if latest_text.strip():
                    _emit_progress(
                        progress_callback,
                        "ui_changed",
                        "\u0054\u0072\u0061\u0065 \u0043\u004e \u6b63\u5e38\u5de5\u4f5c\u4e2d\uff0c\u68c0\u6d4b\u5230\u754c\u9762\u5185\u5bb9\u66f4\u65b0\u3002",
                        {
                            "event": "trae_ui_changed",
                            "text_chars": len(latest_text),
                            "busy": busy,
                        },
                        last_progress_at,
                        min_interval=progress_interval_seconds,
                    )
        if (
            intervention_idle_seconds > 0
            and not busy
            and time.monotonic() - last_change_at >= intervention_idle_seconds
            and len(interventions) < max_interventions
        ):
            idle_seconds = time.monotonic() - last_change_at
            _emit_progress(
                progress_callback,
                "idle_check",
                f"\u0054\u0072\u0061\u0065 \u0043\u004e \u5df2 {int(idle_seconds)} \u79d2\u65e0\u660e\u663e\u53d8\u5316\uff0c\u0057\u006f\u0072\u006b\u0065\u0072 \u6b63\u5728\u68c0\u67e5\u662f\u5426\u9700\u8981\u64cd\u4f5c\u3002",
                {
                    "event": "trae_idle_check",
                    "idle_seconds": round(float(idle_seconds), 3),
                    "intervention_idle_seconds": intervention_idle_seconds,
                },
                last_progress_at,
                min_interval=max(1.0, progress_interval_seconds),
            )
            visual_completion = _try_visual_completion(
                latest_text=latest_text,
                stable_seconds=stable_seconds,
                interventions=interventions,
                diagnostics=diagnostics,
                timeout_seconds=timeout_seconds,
                workspace_path=workspace_path,
                ui_analyst=ui_analyst,
                suppress_continue_text=continue_text_already_sent or _has_continue_text_intervention(interventions),
            )
            if visual_completion:
                return visual_completion
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
            last_decision = decision
            _emit_supervisor_progress(
                progress_callback,
                decision,
                last_progress_at,
                min_interval=progress_interval_seconds,
            )
            outcome = _handle_supervisor_decision(
                decision=decision,
                stable_seconds=stable_seconds,
                latest_text=latest_text,
                interventions=interventions,
                timeout_seconds=timeout_seconds,
                workspace_path=workspace_path,
                ui_analyst=ui_analyst,
                suppress_continue_text=continue_text_already_sent or _has_continue_text_intervention(interventions),
                progress_callback=progress_callback,
                last_progress_at=last_progress_at,
            )
            if outcome.get("status") == "completed":
                return outcome
            if outcome.get("status") == "failed":
                raise TraeAutomationError(str(outcome.get("error") or "Trae supervisor decision failed"))
            if outcome.get("status") == "applied":
                last_change_at = time.monotonic()
                stable_since = None
            elif outcome.get("status") == "waiting":
                last_change_at = time.monotonic()
                stable_since = None
        _sleep_with_cancellation(poll_interval_seconds, cancellation_check)

    timeout_decision = _final_timeout_decision(
        latest_text=latest_text,
        busy=any(marker in latest_text for marker in BUSY_MARKERS),
        prompt=prompt,
        workspace_path=workspace_path,
        sent_at_epoch=sent_at_epoch,
        sent_at=sent_at,
        idle_seconds=time.monotonic() - last_change_at,
        intervention_idle_seconds=intervention_idle_seconds,
        interventions=interventions,
        max_interventions=max_interventions,
        fallback_decision=last_decision,
    )
    if str(timeout_decision.get("action") or "") == "collect_trace":
        return _completed_result(
            stable_seconds=stable_seconds,
            latest_text=latest_text,
            output_probe=timeout_decision.get("output_probe") or {},
            turn_probe=timeout_decision.get("turn_probe") or {},
            gate=timeout_decision.get("completion_gate") or {},
            interventions=interventions,
            supervisor_decision=_with_latest_diagnosis(
                {**timeout_decision, "reason": timeout_decision.get("reason") or "timeout_completion_detected"},
                diagnostics,
            ),
        )
    timeout_decision = _with_latest_diagnosis(timeout_decision, diagnostics)
    raise TraeAutomationError(
        "Trae output did not become stable before wait_completion timeout",
        {
            "output_probe": timeout_decision.get("output_probe") or {},
            "trae_turn": timeout_decision.get("turn_probe") or {},
            "completion_gate": timeout_decision.get("completion_gate") or {},
            "supervisor_decision": timeout_decision,
            "watcher_observation": timeout_decision.get("watcher_observation") or {},
            "activity_summary": timeout_decision.get("activity_summary") or {},
            "text_chars": len(latest_text),
            "text_sample": latest_text[-1000:],
            "interventions": interventions,
            "diagnostics": diagnostics[-3:],
        },
    )


def _try_visual_completion(
    *,
    latest_text: str,
    stable_seconds: float,
    interventions: list[dict],
    diagnostics: list[dict],
    timeout_seconds: float,
    workspace_path: str = "",
    ui_analyst: Callable[[str, dict], dict] | None,
    suppress_continue_text: bool = False,
) -> dict | None:
    if not ui_analyst:
        return None
    intervention = _try_auto_intervention(
        reason="visual_completion_check",
        timeout_seconds=min(10.0, max(2.0, timeout_seconds)),
        workspace_path=workspace_path,
        ui_analyst=ui_analyst,
        suppress_continue_text=suppress_continue_text,
    )
    if intervention.get("status") != "completed":
        _remember_diagnostic(diagnostics, intervention)
        return None
    decision = {
        "action": "collect_trace",
        "reason": str(intervention.get("reason") or "visual_completion_detected"),
        "diagnosis": intervention.get("diagnosis") or {},
        "output_probe": probe_trace(latest_text),
    }
    return _completed_result(
        stable_seconds=stable_seconds,
        latest_text=latest_text,
        output_probe=decision["output_probe"],
        turn_probe={},
        gate={},
        interventions=interventions,
        supervisor_decision=decision,
    )


def _final_timeout_decision(
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
    fallback_decision: dict,
) -> dict:
    try:
        return _supervisor_decision(
            latest_text=latest_text,
            busy=busy,
            prompt=prompt,
            workspace_path=workspace_path,
            sent_at_epoch=sent_at_epoch,
            sent_at=sent_at,
            idle_seconds=idle_seconds,
            intervention_idle_seconds=intervention_idle_seconds,
            interventions=interventions,
            max_interventions=max_interventions,
        )
    except Exception:
        return fallback_decision or {"action": "wait", "reason": "timeout_without_supervisor_decision"}


def _remember_diagnostic(diagnostics: list[dict], intervention: dict) -> None:
    diagnosis = intervention.get("diagnosis") if isinstance(intervention.get("diagnosis"), dict) else {}
    if not diagnosis:
        return
    diagnostics.append(
        {
            "status": str(intervention.get("status") or ""),
            "reason": str(intervention.get("reason") or ""),
            "diagnosis_state": str(intervention.get("diagnosis_state") or diagnosis.get("state") or ""),
            "diagnosis": diagnosis,
        }
    )
    del diagnostics[:-3]


def _with_latest_diagnosis(decision: dict, diagnostics: list[dict]) -> dict:
    if not diagnostics:
        return decision
    latest = diagnostics[-1]
    diagnosis = latest.get("diagnosis") if isinstance(latest.get("diagnosis"), dict) else {}
    if not diagnosis:
        return decision
    updated = dict(decision or {})
    updated.setdefault("diagnosis", diagnosis)
    updated.setdefault("diagnosis_state", latest.get("diagnosis_state") or diagnosis.get("state") or "")
    updated.setdefault("diagnosis_reason", latest.get("reason") or diagnosis.get("reason") or "")
    return updated


def _emit_progress(
    callback: Callable[[dict], None] | None,
    key: str,
    display_message: str,
    extra: dict | None,
    last_progress_at: dict[str, float],
    *,
    min_interval: float = 10.0,
    force: bool = False,
) -> None:
    if not callback:
        return
    now = time.monotonic()
    previous = float(last_progress_at.get(key) or 0.0)
    if not force and previous and now - previous < max(0.0, min_interval):
        return
    last_progress_at[key] = now
    payload = dict(extra or {})
    payload.setdefault("event", key)
    payload["display_message"] = display_message
    try:
        callback(payload)
    except Exception:
        return


def _emit_supervisor_progress(
    callback: Callable[[dict], None] | None,
    decision: dict,
    last_progress_at: dict[str, float],
    *,
    min_interval: float = 10.0,
) -> None:
    message = _supervisor_progress_message(decision)
    if not message:
        return
    reason = str(decision.get("reason") or "")
    _emit_progress(
        callback,
        f"supervisor:{reason or decision.get('action') or 'decision'}",
        message,
        {
            "event": "trae_supervisor_progress",
            "supervisor_action": str(decision.get("action") or ""),
            "supervisor_reason": reason,
            "activity_summary": decision.get("activity_summary") or {},
            "watcher_observation": decision.get("watcher_observation") or {},
            "idle_seconds": decision.get("idle_seconds"),
        },
        last_progress_at,
        min_interval=min_interval,
    )


def _emit_intervention_progress(
    callback: Callable[[dict], None] | None,
    intervention: dict,
    decision: dict,
    last_progress_at: dict[str, float],
) -> None:
    message = _intervention_progress_message(intervention)
    if not message:
        return
    extra = _compact_intervention_progress(intervention)
    extra.update(
        {
            "event": "trae_intervention_result",
            "supervisor_action": str(decision.get("action") or ""),
            "supervisor_reason": str(decision.get("reason") or ""),
        }
    )
    _emit_progress(
        callback,
        f"intervention:{extra.get('intervention_status') or 'unknown'}:{extra.get('suggested_action') or ''}",
        message,
        extra,
        last_progress_at,
        force=True,
    )


def _intervention_progress_message(intervention: dict) -> str:
    status = str(intervention.get("status") or "")
    suggested = intervention.get("suggested_intervention") if isinstance(intervention.get("suggested_intervention"), dict) else {}
    result = intervention.get("result") if isinstance(intervention.get("result"), dict) else {}
    action = str(suggested.get("action") or result.get("action") or "")
    button = str(suggested.get("button") or result.get("button") or "").strip()
    if status == "completed":
        return "Worker 通过视觉检查确认 Trae CN 已完成回复，准备采集结果。"
    if status in {"applied", "clicked"}:
        if action in {"run_button", "run", "execute", "confirm_button", "continue_button"}:
            label = button or _display_action_name(action)
            return f"Worker 已处理 Trae CN 待确认操作：点击「{label}」按钮，继续观察结果。"
        if str(suggested.get("mode") or "") == "continue-text":
            return "Worker 已向 Trae CN 发送继续指令，继续等待模型完成当前回复。"
        if str(suggested.get("mode") or "") == "terminal-input":
            return "Worker 已处理 Trae CN 终端确认输入，继续观察执行结果。"
        if str(suggested.get("mode") or "") == "scroll-inner-panel":
            return "Worker 已滚动 Trae CN 内层确认区域，继续查找安全操作按钮。"
        if str(suggested.get("mode") or "") == "expand-confirm-card":
            return "Worker 检测到 Trae CN「确认执行」卡片已收起，已点击标题展开，继续查找「执行」按钮。"
        return "Worker 已完成一次 Trae CN 自动介入操作，继续观察结果。"
    if status == "skipped":
        return "Worker 识别到 Trae CN 待处理状态，但没有找到符合安全策略的操作目标，继续观察。"
    if status == "failed":
        error = str(intervention.get("error") or result.get("error") or "").strip()
        suffix = f"：{error[:120]}" if error else ""
        return f"Worker 尝试处理 Trae CN 待确认操作失败{suffix}。"
    if status:
        return f"Worker 已尝试处理 Trae CN 待确认操作，返回状态 {status}，继续观察。"
    return ""


def _display_action_name(action: str) -> str:
    mapping = {
        "run_button": "执行",
        "run": "执行",
        "execute": "执行",
        "confirm_button": "确认",
        "continue_button": "继续",
        "keep_button": "保留",
        "save_button": "保存",
        "delete_button": "删除",
    }
    return mapping.get(action, action or "确认")


def _compact_intervention_progress(intervention: dict) -> dict:
    suggested = intervention.get("suggested_intervention") if isinstance(intervention.get("suggested_intervention"), dict) else {}
    result = intervention.get("result") if isinstance(intervention.get("result"), dict) else {}
    diagnosis = intervention.get("diagnosis") if isinstance(intervention.get("diagnosis"), dict) else {}
    visual = diagnosis.get("visual") if isinstance(diagnosis.get("visual"), dict) else {}
    local_visual = visual.get("local_analysis") if isinstance(visual.get("local_analysis"), dict) else {}
    payload = {
        "intervention_status": str(intervention.get("status") or ""),
        "intervention_reason": str(intervention.get("reason") or ""),
        "diagnosis_state": str(intervention.get("diagnosis_state") or diagnosis.get("state") or ""),
        "diagnosis_reason": str(diagnosis.get("reason") or ""),
        "suggested_mode": str(suggested.get("mode") or ""),
        "suggested_action": str(suggested.get("action") or ""),
        "recommended_action": str(suggested.get("recommended_action") or ""),
        "button": str(suggested.get("button") or result.get("button") or ""),
        "source": str(suggested.get("source") or ""),
        "risk": str(suggested.get("risk") or ""),
        "click_result_status": str(result.get("status") or ""),
        "click_mode": str(result.get("mode") or ""),
        "result_action": str(result.get("action") or ""),
        "local_visual_status": str(local_visual.get("status") or ""),
        "local_visual_reason": str(local_visual.get("reason") or ""),
    }
    for key in ("x", "y"):
        if suggested.get(key) not in ("", None):
            payload[f"click_{key}"] = suggested.get(key)
    if intervention.get("error"):
        payload["error"] = str(intervention.get("error") or "")[:300]
    return payload


def _supervisor_progress_message(decision: dict) -> str:
    action = str(decision.get("action") or "")
    reason = str(decision.get("reason") or "")
    if action == "wait" and reason == "recent_trae_activity":
        activity = decision.get("activity_summary") if isinstance(decision.get("activity_summary"), dict) else {}
        source = str(activity.get("source") or "\u6d3b\u52a8")
        return f"\u0054\u0072\u0061\u0065 \u0043\u004e \u6b63\u5e38\u5de5\u4f5c\u4e2d\uff0c\u68c0\u6d4b\u5230 {source} \u66f4\u65b0\uff0c\u7ee7\u7eed\u7b49\u5f85\u3002"
    if action == "wait" and reason == "window_chrome_only":
        return "\u0057\u006f\u0072\u006b\u0065\u0072 \u6682\u65f6\u53ea\u8bfb\u5230 \u0054\u0072\u0061\u0065 \u0043\u004e \u7a97\u53e3\u63a7\u4ef6\u6587\u5b57\uff0c\u672a\u53d1\u73b0\u660e\u786e\u53ef\u64cd\u4f5c\u6309\u94ae\uff0c\u7ee7\u7eed\u89c2\u5bdf\u3002"
    if action == "diagnose_idle":
        idle = int(float(decision.get("idle_seconds") or 0))
        return f"\u0054\u0072\u0061\u0065 \u0043\u004e \u5df2 {idle} \u79d2\u65e0\u660e\u663e\u53d8\u5316\uff0c\u0057\u006f\u0072\u006b\u0065\u0072 \u6b63\u5728\u68c0\u67e5\u662f\u5426\u9700\u8981\u64cd\u4f5c\u3002"
    if action in {"apply_pending_ui", "recover_service_interruption", "continue_output", "answer_terminal_prompt"}:
        return "\u0057\u006f\u0072\u006b\u0065\u0072 \u53d1\u73b0\u660e\u786e\u7684 \u0054\u0072\u0061\u0065 \u0043\u004e \u5f85\u5904\u7406\u72b6\u6001\uff0c\u6b63\u5728\u5c1d\u8bd5\u5904\u7406\u3002"
    return ""


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
    watcher_observation = build_trae_observation(
        project_path=workspace_path,
        started_at_epoch=sent_at_epoch,
        quiet_seconds=intervention_idle_seconds,
        latest_text=latest_text,
        turn_probe=turn_probe if isinstance(turn_probe, dict) else {},
        output_probe=output_probe,
        idle_seconds=idle_seconds,
    )
    activity = watcher_observation.get("activity") if isinstance(watcher_observation, dict) else {}
    project_write = watcher_observation.get("project_write") if isinstance(watcher_observation, dict) else {}
    log = watcher_observation.get("log") if isinstance(watcher_observation, dict) else {}
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
            recent_activity=bool(activity.get("recent")) if isinstance(activity, dict) else False,
            activity_source=str(activity.get("source") or "") if isinstance(activity, dict) else "",
            activity_quiet_seconds=activity.get("quiet_seconds") if isinstance(activity, dict) else None,
            log_tail_hash=str(log.get("tail_hash") or "") if isinstance(log, dict) else "",
            project_last_write=str(project_write.get("last_write") or "") if isinstance(project_write, dict) else "",
            watcher_observation=watcher_observation,
        )
    )


def _handle_supervisor_decision(
    *,
    decision: dict,
    stable_seconds: float,
    latest_text: str,
    interventions: list[dict],
    timeout_seconds: float,
    workspace_path: str = "",
    ui_analyst: Callable[[str, dict], dict] | None = None,
    suppress_continue_text: bool = False,
    progress_callback: Callable[[dict], None] | None = None,
    last_progress_at: dict[str, float] | None = None,
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
        return {"status": "failed", "error": message, "supervisor_decision": decision, **_decision_observation_fields(decision)}
    if action in {"recover_service_interruption", "continue_output", "answer_terminal_prompt", "apply_pending_ui", "diagnose_idle"}:
        reason = str(decision.get("reason") or action)
        if action == "answer_terminal_prompt":
            reason = "terminal_prompt"
        elif action == "diagnose_idle" and reason not in RECOVERABLE_OUTPUT_REASONS:
            reason = f"completion_gate:{reason}"
        intervention = _try_auto_intervention(
            reason=reason,
            timeout_seconds=min(10.0, max(2.0, timeout_seconds)),
            workspace_path=workspace_path,
            ui_analyst=ui_analyst,
            suppress_continue_text=suppress_continue_text,
        )
        intervention["supervisor_action"] = action
        intervention["supervisor_reason"] = str(decision.get("reason") or "")
        _emit_intervention_progress(progress_callback, intervention, decision, last_progress_at or {})
        if intervention.get("status") == "completed":
            completion_decision = {
                **decision,
                "action": "collect_trace",
                "reason": str(intervention.get("reason") or "visual_completion_detected"),
                "diagnosis": intervention.get("diagnosis") or {},
            }
            return _completed_result(
                stable_seconds=stable_seconds,
                latest_text=latest_text,
                output_probe=decision.get("output_probe") or {},
                turn_probe=decision.get("turn_probe") or {},
                gate=decision.get("completion_gate") or {},
                interventions=interventions,
                supervisor_decision=completion_decision,
            )
        if intervention.get("status") == "skipped":
            return {
                "status": "waiting",
                "intervention": intervention,
                "supervisor_decision": decision,
                **_decision_observation_fields(decision),
            }
        interventions.append(intervention)
        if action in {"recover_service_interruption", "continue_output"} and intervention.get("status") != "applied":
            return {
                "status": "failed",
                "error": f"Trae output is stable but not complete ({decision.get('reason')}); auto intervention failed",
                "supervisor_decision": decision,
                **_decision_observation_fields(decision),
            }
        return {
            "status": "applied",
            "intervention": intervention,
            "supervisor_decision": decision,
            **_decision_observation_fields(decision),
        }
    return {"status": "waiting", "supervisor_decision": decision, **_decision_observation_fields(decision)}


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
        "watcher_observation": (supervisor_decision or {}).get("watcher_observation") or {},
        "activity_summary": (supervisor_decision or {}).get("activity_summary") or {},
        "interventions": interventions,
    }


def _decision_observation_fields(decision: dict) -> dict:
    return {
        "watcher_observation": decision.get("watcher_observation") or {},
        "activity_summary": decision.get("activity_summary") or {},
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


def _try_auto_intervention(
    reason: str,
    timeout_seconds: float,
    workspace_path: str = "",
    ui_analyst: Callable[[str, dict], dict] | None = None,
    suppress_continue_text: bool = False,
) -> dict:
    try:
        diagnosis = diagnose_ui(
            timeout_seconds=timeout_seconds,
            scroll_bottom=True,
            ui_analyst=ui_analyst,
            task="wait_completion_state",
            workspace_path=workspace_path or None,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "reason": reason,
            "error": str(exc),
            "diagnosis_state": "",
        }
    if isinstance(diagnosis, dict) and diagnosis.get("state") == "completed":
        return {
            "status": "completed",
            "reason": str(diagnosis.get("reason") or "visual_completion_detected"),
            "diagnosis_state": "completed",
            "diagnosis": _compact_diagnosis(diagnosis),
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
            "diagnosis": _compact_diagnosis(diagnosis) if isinstance(diagnosis, dict) else {},
        }
    if suppress_continue_text and _is_continue_text_intervention(suggested):
        return {
            "status": "skipped",
            "reason": "continue_text_already_sent",
            "suggested_intervention": suggested,
            "diagnosis_state": diagnosis.get("state") if isinstance(diagnosis, dict) else "",
            "diagnosis": _compact_diagnosis(diagnosis) if isinstance(diagnosis, dict) else {},
        }
    try:
        if workspace_path:
            result = apply_intervention(
                suggested,
                timeout_seconds=timeout_seconds,
                workspace_path=workspace_path,
            )
        else:
            result = apply_intervention(suggested, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return {
            "status": "failed",
            "reason": reason,
            "suggested_intervention": suggested,
            "error": str(exc),
            "diagnosis_state": diagnosis.get("state") if isinstance(diagnosis, dict) else "",
            "diagnosis": _compact_diagnosis(diagnosis) if isinstance(diagnosis, dict) else {},
        }
    return {
        "status": result.get("status") or "attempted",
        "reason": reason,
        "suggested_intervention": suggested,
        "result": result,
        "diagnosis_state": diagnosis.get("state") if isinstance(diagnosis, dict) else "",
        "diagnosis": _compact_diagnosis(diagnosis) if isinstance(diagnosis, dict) else {},
    }


def _is_continue_text_intervention(intervention: object) -> bool:
    if not isinstance(intervention, dict):
        return False
    return (
        str(intervention.get("mode") or "") == "continue-text"
        or str(intervention.get("action") or "") in {"continue", "continue_button"}
        or str(intervention.get("recommended_action") or "") == "click_continue_button"
    )


def _has_continue_text_intervention(interventions: list[dict]) -> bool:
    for item in interventions:
        suggested = item.get("suggested_intervention") if isinstance(item, dict) else {}
        result = item.get("result") if isinstance(item, dict) else {}
        if _is_continue_text_intervention(suggested) or _is_continue_text_intervention(result):
            return True
    return False


def _compact_diagnosis(diagnosis: dict[str, object]) -> dict[str, object]:
    visual = diagnosis.get("visual") if isinstance(diagnosis.get("visual"), dict) else {}
    screenshot = visual.get("screenshot") if isinstance(visual.get("screenshot"), dict) else {}
    ai_analysis = visual.get("ai_analysis") if isinstance(visual.get("ai_analysis"), dict) else {}
    result = {
        "ok": bool(diagnosis.get("ok")),
        "state": str(diagnosis.get("state") or ""),
        "confidence": diagnosis.get("confidence") or 0.0,
        "reason": str(diagnosis.get("reason") or ""),
        "text_chars": diagnosis.get("text_chars") or 0,
        "button_count": diagnosis.get("button_count") or 0,
        "output_probe": diagnosis.get("output_probe") or {},
        "terminal_prompt": diagnosis.get("terminal_prompt") or {},
        "suggested_intervention": diagnosis.get("suggested_intervention") or {},
        "visual": {
            "status": visual.get("status") or "",
            "reason": visual.get("reason") or "",
            "screenshot": screenshot,
            "ai_analysis": ai_analysis,
            "ai_error": visual.get("ai_error") or "",
        },
    }
    return result
