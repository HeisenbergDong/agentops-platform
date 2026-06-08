from dataclasses import dataclass
from typing import Any

import httpx

from app.services.user_settings import safe_open_secret


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    api_key: str
    model_name: str
    review_model_name: str
    wire_api: str = "responses"
    reasoning_effort: str = ""
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class LLMResult:
    text: str
    raw: dict[str, Any]
    model: str
    wire_api: str


def model_config_from_settings(settings: dict[str, dict[str, Any]], model_key: str = "default") -> LLMConfig:
    model = dict(settings.get("model", {}))
    profiles = model.get("profiles") if isinstance(model.get("profiles"), dict) else {}
    if model_key and model_key != "default" and isinstance(profiles.get(model_key), dict):
        merged = dict(model)
        merged.update(profiles[model_key])
        model = merged

    api_key = safe_open_secret(model.get("api_key"))
    base_url = str(model.get("base_url") or "https://api.openai.com").rstrip("/")
    model_name = str(model.get("model_name") or model.get("model") or "").strip()
    if not api_key:
        raise LLMError("Model API Key is not configured")
    if not model_name:
        raise LLMError("Model name is not configured")

    return LLMConfig(
        provider=str(model.get("provider") or "OpenAI"),
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
        review_model_name=str(model.get("review_model_name") or model_name),
        wire_api=str(model.get("wire_api") or "responses"),
        reasoning_effort=str(model.get("reasoning_effort") or "").strip(),
        timeout_seconds=float(model.get("timeout_seconds") or 60),
    )


class LLMClient:
    def complete(
        self,
        config: LLMConfig,
        messages: list[dict[str, str]],
        purpose: str = "role_chat",
    ) -> LLMResult:
        if config.wire_api == "chat_completions":
            return self._chat_completions(config, messages, purpose)
        return self._responses(config, messages, purpose)

    def _responses(
        self,
        config: LLMConfig,
        messages: list[dict[str, str]],
        purpose: str,
    ) -> LLMResult:
        url = f"{config.base_url}/v1/responses"
        payload: dict[str, Any] = {
            "model": config.model_name,
            "input": messages,
            "store": False,
            "metadata": {"purpose": purpose},
        }
        if config.reasoning_effort:
            payload["reasoning"] = {"effort": config.reasoning_effort}
        try:
            data = self._post_json(config, url, payload)
        except LLMError as exc:
            if "status 400" not in str(exc):
                raise
            data = self._post_json(config, url, {"model": config.model_name, "input": messages})
        return LLMResult(
            text=_extract_responses_text(data),
            raw=data,
            model=str(data.get("model") or config.model_name),
            wire_api="responses",
        )

    def _chat_completions(
        self,
        config: LLMConfig,
        messages: list[dict[str, str]],
        purpose: str,
    ) -> LLMResult:
        url = f"{config.base_url}/v1/chat/completions"
        payload = {
            "model": config.model_name,
            "messages": messages,
            "metadata": {"purpose": purpose},
        }
        try:
            data = self._post_json(config, url, payload)
        except LLMError as exc:
            if "status 400" not in str(exc):
                raise
            data = self._post_json(config, url, {"model": config.model_name, "messages": messages})
        return LLMResult(
            text=_extract_chat_text(data),
            raw=data,
            model=str(data.get("model") or config.model_name),
            wire_api="chat_completions",
        )

    def _post_json(self, config: LLMConfig, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=config.timeout_seconds) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise LLMError(f"LLM request failed with status {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise LLMError("LLM response is not valid JSON") from exc


def _extract_responses_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"].strip()

    chunks: list[str] = []
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
    text = "\n".join(chunks).strip()
    if not text:
        raise LLMError("LLM response did not include text")
    return text


def _extract_chat_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("LLM response did not include choices")
    content = choices[0].get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMError("LLM response did not include message content")
    return content.strip()
