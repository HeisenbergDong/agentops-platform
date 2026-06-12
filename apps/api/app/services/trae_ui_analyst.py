from __future__ import annotations

import json
import re
from typing import Any

from app.services.llm.client import LLMClient, LLMError, model_config_from_settings


SYSTEM_INSTRUCTIONS = """
You are Trae UI Analyst.
Analyze a Trae CN screenshot and locate requested UI controls.
You never control the mouse or keyboard. Return JSON only.
Do not guess. If uncertain, return status "need_more_context" or "not_found".
Coordinates must be absolute screen coordinates when window.bounds is provided, and also include ratios relative to that window.
Allowed safe actions include prompt_input, send_button, continue_button, run_button, confirm_button, keep_button, save_button.
Dangerous actions such as delete, remove, clear, reset, discard, cancel, abandon must have risk "blocked".
""".strip()


OUTPUT_SCHEMA = {
    "status": "found | partial | need_more_context | not_found",
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
        target = dict(item)
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
        target["confidence"] = float(target.get("confidence") or 0)
        target["risk"] = str(target.get("risk") or "unknown")
        targets.append(target)
    status = str(data.get("status") or ("found" if targets else "not_found"))
    return {
        "status": status,
        "need_screenshot": bool(data.get("need_screenshot", False)),
        "need_scroll": bool(data.get("need_scroll", False)),
        "targets": targets,
        "reason": str(data.get("reason") or ""),
    }
