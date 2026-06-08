import json
import re
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.db.models import UserRole
from app.db.repositories.user_rules import list_user_rule_files, read_user_rule_many
from app.services.llm import LLMClient, LLMError, model_config_from_settings
from app.services.user_settings import load_user_settings

MAX_SOURCE_CHARS = 24000


class RuleCollectorError(RuntimeError):
    pass


def load_rule_source(source: str, source_type: str) -> tuple[str, dict[str, Any]]:
    if source_type == "text":
        text = source.strip()
        if not text:
            raise RuleCollectorError("Source text is required")
        return text[:MAX_SOURCE_CHARS], {"source_type": "text", "length": len(text)}
    if source_type != "url":
        raise RuleCollectorError("Unsupported source type")

    url = source.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise RuleCollectorError("Only http(s) URLs are supported")
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "AgentOps-RuleCollector/0.1"})
    except httpx.HTTPError as exc:
        raise RuleCollectorError(f"Failed to fetch URL: {exc.__class__.__name__}") from exc
    if response.status_code >= 400:
        raise RuleCollectorError(f"Failed to fetch URL: status {response.status_code}")
    text = _html_to_text(response.text)
    return text[:MAX_SOURCE_CHARS], {
        "source_type": "url",
        "url": url,
        "status_code": response.status_code,
        "length": len(text),
    }


def collect_rule_proposal(
    db: Session,
    user_id: str,
    role: UserRole,
    source_text: str,
    source_meta: dict[str, Any],
) -> dict[str, Any]:
    try:
        role_rules = read_user_rule_many(db, user_id, role.rules)
    except FileNotFoundError as exc:
        raise RuleCollectorError(f"Rule not found: {exc.args[0]}") from exc

    available_rules = [item.name for item in list_user_rule_files(db, user_id)]
    messages = [
        {
            "role": "system",
            "content": (
                f"You are the AgentOps role named {role.name}.\n"
                f"Role purpose: {role.purpose}\n"
                "Read product, process, or requirement documents and decide which framework rule "
                "files should receive additions. Output JSON only. Do not output Markdown.\n\n"
                f"Current rule collector rules:\n{_format_rules(role_rules)}\n\n"
                f"Allowed target rule files: {', '.join(available_rules)}"
            ),
        },
        {
            "role": "user",
            "content": (
                "Generate a rule-change proposal from the document below. The JSON object must be:\n"
                "{\"summary\":\"...\",\"changes\":[{\"rule_name\":\"...\",\"title\":\"...\","
                "\"reason\":\"...\",\"content\":\"...\"}],\"warnings\":[\"...\"]}\n"
                "Constraints: rule_name must be one of the allowed target rule files; content must be "
                "a concise rule section that can be appended to that file; preserve concrete user "
                "requirements; do not include secrets, tokens, passwords, or credentials; when no "
                "rule update is needed, return an empty changes array. Use Chinese for summary, "
                "reason, content, and warnings when the source is Chinese.\n\n"
                f"Source metadata: {json.dumps(source_meta, ensure_ascii=False)}\n\n"
                f"Document content:\n{source_text}"
            ),
        },
    ]
    try:
        result = LLMClient().complete(
            model_config_from_settings(load_user_settings(db, user_id), role.model_config_key),
            messages,
            purpose="rule_collect",
        )
    except LLMError as exc:
        raise RuleCollectorError(str(exc)) from exc

    data = _parse_json_object(result.text)
    changes = []
    allowed = set(available_rules)
    for item in data.get("changes") or []:
        if not isinstance(item, dict):
            continue
        rule_name = str(item.get("rule_name") or "").strip()
        content = str(item.get("content") or "").strip()
        if not rule_name or rule_name not in allowed or not content:
            continue
        changes.append(
            {
                "rule_name": rule_name,
                "title": str(item.get("title") or "Collected Rule").strip(),
                "reason": str(item.get("reason") or "").strip(),
                "content": content,
            }
        )
    return {
        "status": "proposal_ready",
        "source": source_meta,
        "summary": str(data.get("summary") or "").strip(),
        "changes": changes,
        "warnings": [str(item) for item in data.get("warnings") or []],
        "model": result.model,
        "wire_api": result.wire_api,
    }


def _format_rules(rules: dict[str, str]) -> str:
    return "\n\n".join([f"## {name}\n{content}" for name, content in rules.items()])


def _html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    start = clean.find("{")
    end = clean.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuleCollectorError("LLM did not return a JSON object")
    try:
        data = json.loads(clean[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RuleCollectorError("LLM returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuleCollectorError("LLM JSON response must be an object")
    return data
