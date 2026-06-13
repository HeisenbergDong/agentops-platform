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
Allowed safe click targets include prompt_input, send_button, continue_button, run_button, confirm_button, keep_button, save_button.
Allowed recommended actions are wait, collect_trace_candidate, scroll_reply_bottom, click_run_button, click_keep_button, click_continue_button, type_continue, answer_terminal_prompt, do_not_click, need_more_context.
Dangerous actions such as delete, remove, clear, reset, discard, cancel, abandon must have risk "blocked" and recommended_action "do_not_click".
If the model request failed with 3003 or service interruption, prefer recommended_action "type_continue" with risk "safe".
If the assistant appears to still be generating or tools are running, use recommended_action "wait".
""".strip()


OUTPUT_SCHEMA = {
    "status": "found | partial | need_more_context | not_found",
    "screen_state": "generating | completed | awaiting_run_confirmation | awaiting_keep_changes | awaiting_continue | model_error_3003 | service_interrupted | terminal_prompt | needs_scroll_reply_bottom | editor_only | window_chrome_only | unknown",
    "recommended_action": "wait | collect_trace_candidate | scroll_reply_bottom | click_run_button | click_keep_button | click_continue_button | type_continue | answer_terminal_prompt | do_not_click | need_more_context",
    "confidence": 0.0,
    "risk": "safe | blocked | unknown",
    "target": {
        "action": "continue_button",
        "label": "Continue",
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
    "awaiting_run_confirmation",
    "awaiting_keep_changes",
    "awaiting_continue",
    "model_error_3003",
    "service_interrupted",
    "terminal_prompt",
    "needs_scroll_reply_bottom",
    "editor_only",
    "window_chrome_only",
    "unknown",
}
RECOMMENDED_ACTIONS = {
    "wait",
    "collect_trace_candidate",
    "scroll_reply_bottom",
    "click_run_button",
    "click_keep_button",
    "click_continue_button",
    "type_continue",
    "answer_terminal_prompt",
    "do_not_click",
    "need_more_context",
}
RISKS = {"safe", "blocked", "unknown"}
ACTION_TO_RECOMMENDATION = {
    "continue_button": "click_continue_button",
    "run_button": "click_run_button",
    "confirm_button": "click_run_button",
    "keep_button": "click_keep_button",
    "save_button": "click_keep_button",
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
        if _looks_unsafe_target(target):
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
    screen_state = _normalized_choice(str(data.get("screen_state") or ""), SCREEN_STATES, "unknown")
    recommended_action = _normalized_choice(str(data.get("recommended_action") or ""), RECOMMENDED_ACTIONS, "")
    if not recommended_action:
        recommended_action = _recommended_action_from_state(screen_state, target, targets)
    confidence = _clamped_float(data.get("confidence"), default=_target_confidence(target, targets))
    risk = _normalized_choice(str(data.get("risk") or ""), RISKS, "")
    if not risk:
        risk = _risk_from_target(target, targets)
    blocked_reason = str(data.get("blocked_reason") or "")
    if _looks_unsafe_target(target):
        risk = "blocked"
        recommended_action = "do_not_click"
        blocked_reason = blocked_reason or "unsafe_target_label"
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
    if screen_state in {"model_error_3003", "service_interrupted"}:
        return "type_continue"
    if screen_state == "terminal_prompt":
        return "answer_terminal_prompt"
    if screen_state in {"editor_only", "window_chrome_only"}:
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


def _target_in_list(target: dict, targets: list[dict]) -> bool:
    action = str(target.get("action") or "")
    center = target.get("center") if isinstance(target.get("center"), dict) else {}
    for item in targets:
        item_center = item.get("center") if isinstance(item.get("center"), dict) else {}
        if str(item.get("action") or "") == action and item_center == center:
            return True
    return False
