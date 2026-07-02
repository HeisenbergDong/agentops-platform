from __future__ import annotations

import json
import re
from typing import Any

from app.services.llm.client import LLMClient, LLMError, model_config_from_settings


SYSTEM_INSTRUCTIONS = """
You are Trae UI Analyst.
Analyze a Trae CN screenshot and classify whether Trae is still working, completed, blocked, or awaiting a safe user action.
You never control the mouse or keyboard. Return JSON only.
Do not guess. If uncertain, return status "need_more_context" or "not_found".
Coordinates must be absolute screen coordinates when window.bounds is provided, and also include ratios relative to that window.
Allowed click targets include prompt_input, send_button, copy_trace_button, more_actions_button, continue_button, run_button, confirm_button, keep_button, save_button, delete_button, discard_button, remove_button, reset_button, cancel_button, expand_confirm_card.
Allowed recommended actions are wait, collect_trace_candidate, scroll_reply_bottom, scroll_inner_panel, click_run_button, click_confirm_button, click_keep_button, click_save_button, click_continue_button, click_delete_button, click_discard_button, click_cancel_button, expand_confirm_card, type_continue, answer_terminal_prompt, do_not_click, need_more_context.
If the screenshot contains "正在等待你的操作", "等待您的操作", "确认执行", "保留", "删除", "应用", "继续", "运行", "允许", or "拒绝", classify it as a pending user action instead of ordinary generation.
Hard rule: a visible waiting-for-user-action card is never a completed task. If that card explicitly asks whether Trae can delete/remove something, return awaiting_delete_confirmation + click_delete_button with risk "safe". If it asks to clear, reset, discard, cancel, or abandon, return screen_state "manual_required", recommended_action "do_not_click", risk "blocked", and explain blocked_reason.
Example: if Trae shows "正在等待你的操作" with "删除 startup.log" and buttons "保留" / "删除", the correct answer is awaiting_delete_confirmation + click_delete_button with risk safe, not completed.
If the pending action is inside a nested reply card or small inner panel and the card content is cut off or not fully visible, use screen_state "needs_scroll_inner_panel" and recommended_action "scroll_inner_panel".
If a confirmation card has collapsed and only the "确认执行" header is visible, use screen_state "awaiting_collapsed_confirm_card" and recommended_action "expand_confirm_card" with the header target.
For destructive-looking actions such as clear, reset, discard, cancel, or abandon, use risk "blocked" and recommended_action "do_not_click" unless task context explicitly allows that exact destructive action. Trae delete/remove confirmation cards are allowed when explicit.
If the model request failed with 3003 or service interruption, prefer recommended_action "type_continue" with risk "safe".
If the assistant appears to still be generating or tools are running, use recommended_action "wait".
If the left task card or assistant area visibly says the task is complete, classify screen_state "completed" and recommended_action "collect_trace_candidate" unless there is a visible generating spinner, terminal prompt, service error, or explicit continue prompt.
When task context desired_action is copy_trace_button, locate the safe copy button/icon for the latest completed assistant reply or its execution trace. Prefer the assistant message bottom toolbar copy control. Do not choose code-block copy buttons, editor toolbar copy icons, file explorer controls, or window chrome. If the reply area is narrow and the toolbar has a "..." / more / overflow button where copy may be hidden, return a safe more_actions_button target first, not not_found. If the overflow menu is already open, choose the Copy item inside that menu only when it belongs to the latest assistant reply toolbar. If no direct copy or overflow menu path is visible, return status "not_found" and need_scroll true when scrolling may reveal it.
When task context desired_action is send_button, the target must be the active green send/up-arrow/paper-plane button at the lower-right of the Trae chat composer. Never choose microphone, voice input, audio, lightning, attachment, model selector, seed selector, or any toolbar icon as send_button. If the visible candidate is a microphone/voice icon, return not_found or do_not_click and explain it.
When task context desired_action is verify_prompt_submission, classify only whether the prompt submission succeeded. If the prompt text still appears in the composer or the active green send/up-arrow button is still visible beside a filled composer, return screen_state "prompt_still_in_composer", recommended_action "do_not_click", risk "blocked". If the composer button is a green stop/square while Trae is working, treat that as submitted/generating, not as an active send button. If no submitted user prompt or generation is visible, return screen_state "prompt_not_submitted". If the prompt has left the composer and Trae is generating or waiting for a safe follow-up action, return screen_state "prompt_submitted" or the more specific generating/awaiting_* state. Do not return click targets for this task.
If Trae is asking whether to execute, continue, or keep/adopt/save changes, classify the prompt and return the exact visible safe button target when that is the correct next action.
If Trae is asking whether to delete/remove something, classify it as awaiting_delete_confirmation/click_delete_button with risk safe. Keep discard/reset/cancel/clear prompts manual_required/do_not_click unless task context explicitly allows that destructive action.
If Windows Security or Defender Firewall asks whether to allow network access for the current local development server, classify the Allow/允许 button as awaiting_confirm + click_confirm_button with risk "safe". Do not choose Cancel/Block.
When task context asks for wait_completion_state, the screenshot is the source of truth for pending UI actions; do not treat visible confirmation cards as completion unless the prompt is already resolved.
""".strip()

SYSTEM_INSTRUCTIONS += (
    "\nTrae delete confirmation policy: when Trae explicitly asks whether it can delete/remove something, "
    "click_delete_button may be risk=safe. Discard/reset/cancel/clear prompts or uncertain destructive prompts "
    "must remain manual_required/do_not_click."
)


OUTPUT_SCHEMA = {
    "status": "found | partial | need_more_context | not_found",
    "screen_state": "generating | completed | prompt_submitted | prompt_still_in_composer | prompt_not_submitted | manual_required | awaiting_run_confirmation | awaiting_confirm | awaiting_keep_changes | awaiting_save | awaiting_continue | awaiting_collapsed_confirm_card | awaiting_delete_confirmation | awaiting_discard_confirmation | awaiting_cancel_confirmation | model_error_3003 | service_interrupted | terminal_prompt | needs_scroll_reply_bottom | needs_scroll_inner_panel | editor_only | window_chrome_only | unknown",
    "recommended_action": "wait | collect_trace_candidate | scroll_reply_bottom | scroll_inner_panel | click_run_button | click_confirm_button | click_keep_button | click_save_button | click_continue_button | click_delete_button | click_discard_button | click_cancel_button | expand_confirm_card | type_continue | answer_terminal_prompt | do_not_click | need_more_context",
    "confidence": 0.0,
    "risk": "safe | blocked | unknown",
    "target": {
        "action": "copy_trace_button",
        "label": "Copy latest assistant reply",
        "center": {"x": 0, "y": 0},
        "ratio": {"x": 0.0, "y": 0.0},
    },
    "evidence": ["short visual evidence"],
    "blocked_reason": "",
    "need_screenshot": False,
    "need_scroll": False,
    "targets": [
        {
            "action": "prompt_input",
            "label": "chat input",
            "center": {"x": 0, "y": 0},
            "ratio": {"x": 0.0, "y": 0.0},
            "confidence": 0.0,
            "risk": "safe | blocked | unknown",
            "reason": "short visual reason",
        }
    ],
}

SCREEN_STATES = {
    "generating",
    "completed",
    "prompt_submitted",
    "prompt_still_in_composer",
    "prompt_not_submitted",
    "manual_required",
    "awaiting_run_confirmation",
    "awaiting_confirm",
    "awaiting_keep_changes",
    "awaiting_save",
    "awaiting_continue",
    "awaiting_collapsed_confirm_card",
    "awaiting_delete_confirmation",
    "awaiting_discard_confirmation",
    "awaiting_cancel_confirmation",
    "model_error_3003",
    "service_interrupted",
    "terminal_prompt",
    "needs_scroll_reply_bottom",
    "needs_scroll_inner_panel",
    "editor_only",
    "window_chrome_only",
    "unknown",
}
RECOMMENDED_ACTIONS = {
    "wait",
    "collect_trace_candidate",
    "scroll_reply_bottom",
    "scroll_inner_panel",
    "click_run_button",
    "click_confirm_button",
    "click_keep_button",
    "click_save_button",
    "click_continue_button",
    "click_delete_button",
    "click_discard_button",
    "click_cancel_button",
    "expand_confirm_card",
    "type_continue",
    "answer_terminal_prompt",
    "do_not_click",
    "need_more_context",
}
RISKS = {"safe", "blocked", "unknown"}
ACTION_TO_RECOMMENDATION = {
    "copy_trace_button": "collect_trace_candidate",
    "more_actions_button": "collect_trace_candidate",
    "continue_button": "click_continue_button",
    "run_button": "click_run_button",
    "confirm_button": "click_confirm_button",
    "keep_button": "click_keep_button",
    "save_button": "click_save_button",
    "delete_button": "click_delete_button",
    "remove_button": "click_delete_button",
    "discard_button": "click_discard_button",
    "reset_button": "click_discard_button",
    "cancel_button": "click_cancel_button",
    "expand_confirm_card": "expand_confirm_card",
}
UNSAFE_TEXT_MARKERS = (
    "delete",
    "remove",
    "clear",
    "reset",
    "discard",
    "cancel",
    "abandon",
    "删除",
    "清空",
    "重置",
    "丢弃",
    "放弃",
    "取消",
)
WAITING_ACTION_TEXT_MARKERS = (
    "正在等待你的操作",
    "正在等待您的操作",
    "等待你的操作",
    "等待您的操作",
    "等待操作",
    "waiting for your operation",
    "waiting for your action",
)
DESTRUCTIVE_RECOMMENDED_ACTIONS = {
    "click_delete_button",
    "click_discard_button",
    "click_cancel_button",
}
DESTRUCTIVE_SCREEN_STATES = {
    "awaiting_delete_confirmation",
    "awaiting_discard_confirmation",
    "awaiting_cancel_confirmation",
}
DESTRUCTIVE_CONFIRMATION_ACTIONS = {
    "delete_button": ("awaiting_delete_confirmation", "click_delete_button"),
    "remove_button": ("awaiting_delete_confirmation", "click_delete_button"),
    "discard_button": ("awaiting_discard_confirmation", "click_discard_button"),
    "reset_button": ("awaiting_discard_confirmation", "click_discard_button"),
    "cancel_button": ("awaiting_cancel_confirmation", "click_cancel_button"),
}
FIREWALL_ALLOW_CONTEXT_MARKERS = (
    "windows security",
    "windows defender",
    "defender firewall",
    "firewall",
    "\u9632\u706b\u5899",
    "\u5b89\u5168\u4e2d\u5fc3",
    "\u7f51\u7edc\u8bbf\u95ee",
    "public network",
    "private network",
    "allow access",
)
FIREWALL_LOCAL_SERVICE_MARKERS = (
    "server.exe",
    "localhost",
    "127.0.0.1",
    "local development",
    "local dev",
    "\u672c\u5730",
    "vite",
    "node.exe",
    "go.exe",
)
FIREWALL_ALLOW_LABEL_MARKERS = (
    "allow",
    "allow access",
    "\u5141\u8bb8",
    "\u5141\u8bb8\u8bbf\u95ee",
)


def analyze_trae_ui(
    settings: dict[str, dict[str, Any]],
    *,
    image_bytes: bytes,
    mime_type: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    config = model_config_from_settings(settings)
    prompt = _build_prompt(context)
    try:
        result = LLMClient().complete_with_image(
            config,
            prompt=prompt,
            image_bytes=image_bytes,
            mime_type=mime_type,
            purpose="trae_ui_analyst",
        )
    except LLMError:
        raise
    data = _parse_json_result(result.text)
    normalized = _normalize_analysis(data, context)
    normalized["model"] = result.model
    normalized["wire_api"] = result.wire_api
    return normalized


def _build_prompt(context: dict[str, Any]) -> str:
    return (
        f"{SYSTEM_INSTRUCTIONS}\n\n"
        "Task context JSON:\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        "Required output schema:\n"
        f"{json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, indent=2)}\n\n"
        "Return only one JSON object. No Markdown."
    )


def _parse_json_result(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise LLMError("Trae UI Analyst did not return JSON")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise LLMError("Trae UI Analyst response must be a JSON object")
    return data


def _normalize_analysis(data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    bounds = context.get("window", {}).get("bounds") if isinstance(context.get("window"), dict) else {}
    left = int(bounds.get("left") or 0) if isinstance(bounds, dict) else 0
    top = int(bounds.get("top") or 0) if isinstance(bounds, dict) else 0
    width = int(bounds.get("width") or 0) if isinstance(bounds, dict) else 0
    height = int(bounds.get("height") or 0) if isinstance(bounds, dict) else 0
    targets = []
    for item in data.get("targets") or []:
        if not isinstance(item, dict):
            continue
        target = _normalize_target_geometry(dict(item), left=left, top=top, width=width, height=height)
        target["confidence"] = float(target.get("confidence") or 0)
        target["risk"] = _normalized_choice(str(target.get("risk") or "unknown"), RISKS, "unknown")
        if _should_block_unsafe_target(target, data, context):
            target["risk"] = "blocked"
        targets.append(target)
    status = str(data.get("status") or ("found" if targets else "not_found"))
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    if not target and targets:
        target = targets[0]
    target = _normalize_target_geometry(dict(target), left=left, top=top, width=width, height=height) if target else {}
    if target:
        if "confidence" not in target:
            target["confidence"] = targets[0].get("confidence", data.get("confidence", 0.0)) if targets else data.get("confidence", 0.0)
        target["confidence"] = _clamped_float(target.get("confidence"))
        if "risk" not in target:
            target["risk"] = data.get("risk") or (targets[0].get("risk") if targets else "unknown")
        target["risk"] = _normalized_choice(str(target.get("risk") or "unknown"), RISKS, "unknown")
        if _should_block_unsafe_target(target, data, context):
            target["risk"] = "blocked"
    screen_state = _normalized_choice(str(data.get("screen_state") or ""), SCREEN_STATES, "unknown")
    recommended_action = _normalized_choice(str(data.get("recommended_action") or ""), RECOMMENDED_ACTIONS, "")
    if not recommended_action:
        recommended_action = _recommended_action_from_state(screen_state, target, targets)
    confidence = _clamped_float(data.get("confidence"), default=_target_confidence(target, targets))
    risk = _normalized_choice(str(data.get("risk") or ""), RISKS, "")
    if not risk:
        risk = _risk_from_target(target, targets)
    blocked_reason = str(data.get("blocked_reason") or "")
    firewall_allow_target = _firewall_allow_confirmation_target(target, targets, data, context)
    if _should_force_delete_confirmation(screen_state, recommended_action, target, targets, data, context):
        screen_state = "awaiting_delete_confirmation"
        recommended_action = "click_delete_button"
        risk = "safe"
        blocked_reason = ""
        if target and str(target.get("action") or "") in {"delete_button", "remove_button"}:
            target["risk"] = "safe"
        for item in targets:
            if str(item.get("action") or "") in {"delete_button", "remove_button"}:
                item["risk"] = "safe"
    if firewall_allow_target:
        screen_state = "awaiting_confirm"
        recommended_action = "click_confirm_button"
        risk = "safe"
        blocked_reason = ""
        target = {**firewall_allow_target, "action": "confirm_button", "risk": "safe"}
        for item in targets:
            if _is_firewall_allow_target(item):
                item["action"] = "confirm_button"
                item["risk"] = "safe"
    elif _should_block_destructive_analysis(screen_state, recommended_action, target, targets, data, context):
        if screen_state == "completed":
            screen_state = "manual_required"
        risk = "blocked"
        recommended_action = "do_not_click"
        blocked_reason = blocked_reason or _destructive_block_reason(data, context)
        if target:
            target["risk"] = "blocked"
        for item in targets:
            if _looks_unsafe_target(item):
                item["risk"] = "blocked"
    if target and str(target.get("risk") or "") == "blocked":
        risk = "blocked"
        recommended_action = "do_not_click"
        blocked_reason = blocked_reason or "target_marked_blocked"
        target["risk"] = "blocked"
    if target and str(target.get("action") or "") and not _target_in_list(target, targets):
        targets.insert(0, target)
    evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
    return {
        "status": status,
        "screen_state": screen_state,
        "recommended_action": recommended_action,
        "confidence": confidence,
        "risk": risk,
        "target": target,
        "evidence": [str(item)[:240] for item in evidence[:8]],
        "blocked_reason": blocked_reason,
        "need_screenshot": bool(data.get("need_screenshot", False)),
        "need_scroll": bool(data.get("need_scroll", False)),
        "targets": targets,
        "reason": str(data.get("reason") or ""),
    }


def _normalize_target_geometry(
    target: dict[str, Any],
    *,
    left: int,
    top: int,
    width: int,
    height: int,
) -> dict[str, Any]:
    center = target.get("center") if isinstance(target.get("center"), dict) else {}
    ratio = target.get("ratio") if isinstance(target.get("ratio"), dict) else {}
    if center and width > 0 and height > 0 and not ratio:
        try:
            ratio = {
                "x": round((float(center.get("x")) - left) / width, 4),
                "y": round((float(center.get("y")) - top) / height, 4),
            }
        except Exception:
            ratio = {}
    if ratio and width > 0 and height > 0 and not center:
        try:
            center = {
                "x": int(left + width * float(ratio.get("x"))),
                "y": int(top + height * float(ratio.get("y"))),
            }
        except Exception:
            center = {}
    target["center"] = center
    target["ratio"] = ratio
    if "action" in target:
        target["action"] = str(target.get("action") or "")
    if "label" in target:
        target["label"] = str(target.get("label") or "")
    return target


def _normalized_choice(value: str, allowed: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _recommended_action_from_state(screen_state: str, target: dict, targets: list[dict]) -> str:
    if screen_state == "generating":
        return "wait"
    if screen_state == "completed":
        return "collect_trace_candidate"
    if screen_state == "needs_scroll_reply_bottom":
        return "scroll_reply_bottom"
    if screen_state == "needs_scroll_inner_panel":
        return "scroll_inner_panel"
    if screen_state in {"model_error_3003", "service_interrupted"}:
        return "type_continue"
    if screen_state == "terminal_prompt":
        return "answer_terminal_prompt"
    if screen_state in {"editor_only", "window_chrome_only"}:
        return "do_not_click"
    if screen_state == "manual_required":
        return "do_not_click"
    action = str(target.get("action") or "")
    if action in ACTION_TO_RECOMMENDATION:
        return ACTION_TO_RECOMMENDATION[action]
    for item in targets:
        action = str(item.get("action") or "")
        if action in ACTION_TO_RECOMMENDATION:
            return ACTION_TO_RECOMMENDATION[action]
    return "need_more_context"


def _risk_from_target(target: dict, targets: list[dict]) -> str:
    candidates = [target] if target else []
    candidates.extend(targets)
    for item in candidates:
        risk = str(item.get("risk") or "")
        if risk in RISKS:
            return risk
    return "unknown"


def _target_confidence(target: dict, targets: list[dict]) -> float:
    for item in ([target] if target else []) + targets:
        if isinstance(item, dict) and item.get("confidence") is not None:
            return _clamped_float(item.get("confidence"))
    return 0.0


def _clamped_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    return max(0.0, min(1.0, result))


def _looks_unsafe_target(target: dict) -> bool:
    text = f"{target.get('action') or ''} {target.get('label') or ''} {target.get('reason') or ''}".lower()
    return any(marker.lower() in text for marker in UNSAFE_TEXT_MARKERS)


def _should_block_destructive_analysis(
    screen_state: str,
    recommended_action: str,
    target: dict,
    targets: list[dict],
    data: dict[str, Any],
    context: dict[str, Any],
) -> bool:
    if _delete_confirmation_allowed(target, targets, data, context):
        return False
    if screen_state in DESTRUCTIVE_SCREEN_STATES or recommended_action in DESTRUCTIVE_RECOMMENDED_ACTIONS:
        return True
    if _context_has_destructive_waiting_prompt(context) or _analysis_has_destructive_waiting_prompt(data):
        return True
    return any(_should_block_unsafe_target(item, data, context) for item in ([target] if target else []) + targets)


def _context_has_destructive_waiting_prompt(context: dict[str, Any]) -> bool:
    text_parts = [str(context.get("visible_text_sample") or "")]
    buttons = context.get("uia_buttons")
    if isinstance(buttons, list):
        for item in buttons:
            if isinstance(item, dict):
                text_parts.append(str(item.get("name") or item.get("label") or ""))
    text = "\n".join(text_parts).lower()
    return _has_waiting_marker(text) and _has_unsafe_marker(text)


def _analysis_has_destructive_waiting_prompt(data: dict[str, Any]) -> bool:
    evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
    text = "\n".join([str(data.get("reason") or ""), str(data.get("blocked_reason") or ""), *(str(item) for item in evidence)]).lower()
    return _has_waiting_marker(text) and _has_unsafe_marker(text)


def _has_waiting_marker(text: str) -> bool:
    return any(marker.lower() in text for marker in WAITING_ACTION_TEXT_MARKERS)


def _has_unsafe_marker(text: str) -> bool:
    return any(marker.lower() in text for marker in UNSAFE_TEXT_MARKERS)


def _destructive_block_reason(data: dict[str, Any], context: dict[str, Any]) -> str:
    if _context_has_destructive_waiting_prompt(context):
        return "visible_waiting_destructive_confirmation"
    if _analysis_has_destructive_waiting_prompt(data):
        return "analysis_waiting_destructive_confirmation"
    return "destructive_action_requires_manual_confirmation"


def _should_block_unsafe_target(target: dict, data: dict[str, Any], context: dict[str, Any]) -> bool:
    if not _looks_unsafe_target(target):
        return False
    if not _destructive_action_allowed(target, context, data):
        return True
    if str(target.get("risk") or "") != "safe" or str(data.get("risk") or "") == "blocked":
        return True
    action = str(target.get("action") or "")
    expected = DESTRUCTIVE_CONFIRMATION_ACTIONS.get(action)
    if not expected:
        return True
    expected_state, expected_action = expected
    screen_state = str(data.get("screen_state") or "")
    recommended_action = str(data.get("recommended_action") or "") or ACTION_TO_RECOMMENDATION.get(action, "")
    return not (screen_state == expected_state and recommended_action == expected_action)


def _destructive_action_allowed(target: dict, context: dict[str, Any], data: dict[str, Any] | None = None) -> bool:
    action = str(target.get("action") or "")
    if action not in DESTRUCTIVE_CONFIRMATION_ACTIONS:
        return False
    if bool(context.get("allow_destructive_actions")):
        return True
    allowed = context.get("allowed_destructive_actions")
    if isinstance(allowed, list) and action in {str(item) for item in allowed}:
        return True
    if action in {"delete_button", "remove_button"} and _looks_like_allowed_trae_delete_confirmation(target, context, data or {}):
        return True
    return False


def _delete_confirmation_allowed(
    target: dict,
    targets: list[dict],
    data: dict[str, Any],
    context: dict[str, Any],
) -> bool:
    for item in ([target] if target else []) + targets:
        if _destructive_action_allowed(item, context, data):
            return True
    return _context_or_analysis_has_allowed_delete_confirmation(context, data)


def _should_force_delete_confirmation(
    screen_state: str,
    recommended_action: str,
    target: dict,
    targets: list[dict],
    data: dict[str, Any],
    context: dict[str, Any],
) -> bool:
    if str(data.get("risk") or "") == "blocked":
        return False
    if target and str(target.get("risk") or "") == "blocked":
        return False
    if any(str(item.get("risk") or "") == "blocked" for item in targets):
        return False
    if not _delete_confirmation_allowed(target, targets, data, context):
        return False
    return screen_state == "completed" or recommended_action in {"collect_trace_candidate", "do_not_click", "need_more_context", ""}


def _looks_like_allowed_trae_delete_confirmation(target: dict, context: dict[str, Any], data: dict[str, Any]) -> bool:
    if bool(context.get("allow_trae_delete_confirmations")):
        return True
    if str(data.get("screen_state") or "") == "awaiting_delete_confirmation":
        return True
    if str(data.get("recommended_action") or "") == "click_delete_button":
        return True
    return _context_or_analysis_has_allowed_delete_confirmation(context, data, target)


def _context_or_analysis_has_allowed_delete_confirmation(
    context: dict[str, Any],
    data: dict[str, Any],
    target: dict | None = None,
) -> bool:
    text_parts = [
        str((target or {}).get("label") or ""),
        str((target or {}).get("reason") or ""),
        str(data.get("reason") or ""),
        str(data.get("blocked_reason") or ""),
        str(data.get("screen_state") or ""),
        str(data.get("recommended_action") or ""),
        str(context.get("visible_text_sample") or ""),
    ]
    buttons = context.get("uia_buttons")
    if isinstance(buttons, list):
        for item in buttons:
            if isinstance(item, dict):
                text_parts.extend(str(item.get(key) or "") for key in ("name", "label", "action", "reason"))
    evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
    text_parts.extend(str(item) for item in evidence)
    targets = data.get("targets") if isinstance(data.get("targets"), list) else []
    for item in targets:
        if isinstance(item, dict):
            text_parts.extend(str(item.get(key) or "") for key in ("action", "label", "reason"))
    text = "\n".join(text_parts).lower().replace("/", "\\")
    delete_markers = (
        "delete",
        "remove",
        "删除",
        "移除",
    )
    if not any(marker in text for marker in delete_markers):
        return False
    confirmation_markers = (
        "awaiting_delete_confirmation",
        "click_delete_button",
        "waiting for your operation",
        "waiting for your action",
        "正在等待你的操作",
        "正在等待您的操作",
        "等待你的操作",
        "等待您的操作",
        "确认删除",
        "是否要删除",
        "是否仍要删除",
    )
    if any(marker in text for marker in confirmation_markers):
        return True
    return bool(target and str(target.get("action") or "") in {"delete_button", "remove_button"})


def _firewall_allow_confirmation_target(
    target: dict,
    targets: list[dict],
    data: dict[str, Any],
    context: dict[str, Any],
) -> dict:
    candidates = [item for item in ([target] if target else []) + targets if isinstance(item, dict)]
    text = _analysis_text_blob(context, data, candidates)
    if not _has_marker(text, FIREWALL_ALLOW_CONTEXT_MARKERS):
        return {}
    if not _has_marker(text, FIREWALL_LOCAL_SERVICE_MARKERS):
        return {}
    safe_candidates = [item for item in candidates if str(item.get("risk") or "") == "safe"]
    for item in safe_candidates + candidates:
        if _is_firewall_allow_target(item):
            return dict(item)
    return {}


def _is_firewall_allow_target(target: dict) -> bool:
    if not isinstance(target, dict):
        return False
    action = str(target.get("action") or "")
    if action not in {"confirm_button", "run_button"}:
        return False
    text = _normalized_text(
        "\n".join(
            str(target.get(key) or "")
            for key in ("label", "name", "reason", "button", "action")
        )
    )
    return _has_marker(text, FIREWALL_ALLOW_LABEL_MARKERS)


def _analysis_text_blob(context: dict[str, Any], data: dict[str, Any], targets: list[dict]) -> str:
    text_parts = [
        str(data.get("reason") or ""),
        str(data.get("blocked_reason") or ""),
        str(data.get("screen_state") or ""),
        str(data.get("recommended_action") or ""),
        str(context.get("visible_text_sample") or ""),
    ]
    evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
    text_parts.extend(str(item) for item in evidence)
    buttons = context.get("uia_buttons")
    if isinstance(buttons, list):
        for item in buttons:
            if isinstance(item, dict):
                text_parts.extend(str(item.get(key) or "") for key in ("name", "label", "action", "reason"))
    for item in targets:
        if isinstance(item, dict):
            text_parts.extend(str(item.get(key) or "") for key in ("action", "label", "name", "reason", "button"))
    return _normalized_text("\n".join(text_parts))


def _normalized_text(text: str) -> str:
    return str(text or "").casefold().replace("/", "\\")


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    normalized = _normalized_text(text)
    return any(_normalized_text(marker) in normalized for marker in markers)


def _target_in_list(target: dict, targets: list[dict]) -> bool:
    action = str(target.get("action") or "")
    center = target.get("center") if isinstance(target.get("center"), dict) else {}
    for item in targets:
        item_center = item.get("center") if isinstance(item.get("center"), dict) else {}
        if str(item.get("action") or "") == action and item_center == center:
            return True
    return False
