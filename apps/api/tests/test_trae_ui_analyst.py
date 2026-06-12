from app.services.llm.client import LLMClient, LLMConfig
from app.services import trae_ui_analyst


def test_llm_client_responses_image_payload(monkeypatch):
    captured = {}
    config = LLMConfig(
        provider="OpenAI",
        base_url="https://api.example.test",
        api_key="key",
        model_name="vision-model",
        review_model_name="vision-model",
        wire_api="responses",
    )

    def fake_post_json(self, cfg, url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return {"model": "vision-model", "output_text": '{"status":"found","targets":[]}'}

    monkeypatch.setattr(LLMClient, "_post_json", fake_post_json)

    result = LLMClient().complete_with_image(
        config,
        prompt="find send button",
        image_bytes=b"png",
        mime_type="image/png",
    )

    content = captured["payload"]["input"][0]["content"]
    assert result.text == '{"status":"found","targets":[]}'
    assert captured["url"] == "https://api.example.test/v1/responses"
    assert content[0] == {"type": "input_text", "text": "find send button"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")


def test_trae_ui_analyst_normalizes_ratio_from_center():
    data = {
        "status": "found",
        "targets": [
            {
                "action": "send_button",
                "center": {"x": 436, "y": 756},
                "confidence": 0.93,
                "risk": "safe",
            }
        ],
    }
    context = {"window": {"bounds": {"left": 0, "top": 0, "width": 1200, "height": 800}}}

    result = trae_ui_analyst._normalize_analysis(data, context)

    assert result["targets"][0]["ratio"] == {"x": 0.3633, "y": 0.945}
    assert result["targets"][0]["confidence"] == 0.93
