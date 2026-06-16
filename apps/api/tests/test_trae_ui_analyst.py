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
    assert result["recommended_action"] == "need_more_context"
    assert result["risk"] == "safe"


def test_trae_ui_analyst_normalizes_decision_schema_from_target():
    data = {
        "status": "found",
        "screen_state": "awaiting_continue",
        "recommended_action": "click_continue_button",
        "confidence": 0.88,
        "risk": "safe",
        "target": {
            "action": "continue_button",
            "label": "继续",
            "ratio": {"x": 0.32, "y": 0.64},
        },
        "evidence": ["visible continue button"],
    }
    context = {"window": {"bounds": {"left": 100, "top": 50, "width": 1000, "height": 700}}}

    result = trae_ui_analyst._normalize_analysis(data, context)

    assert result["screen_state"] == "awaiting_continue"
    assert result["recommended_action"] == "click_continue_button"
    assert result["target"]["center"] == {"x": 420, "y": 498}
    assert result["confidence"] == 0.88
    assert result["evidence"] == ["visible continue button"]


def test_trae_ui_analyst_blocks_unsafe_target():
    data = {
        "status": "found",
        "screen_state": "awaiting_keep_changes",
        "recommended_action": "click_keep_button",
        "risk": "safe",
        "target": {"action": "delete_button", "label": "Delete all changes", "center": {"x": 10, "y": 20}},
    }
    result = trae_ui_analyst._normalize_analysis(data, {"window": {"bounds": {"left": 0, "top": 0, "width": 100, "height": 100}}})

    assert result["risk"] == "blocked"
    assert result["recommended_action"] == "do_not_click"
    assert result["blocked_reason"] == "unsafe_target_label"


def test_trae_ui_analyst_treats_keep_bar_as_trace_candidate_during_wait_completion():
    data = {
        "status": "found",
        "screen_state": "awaiting_keep_changes",
        "confidence": 0.82,
        "risk": "safe",
        "target": {"action": "keep_button", "label": "Keep changes", "center": {"x": 500, "y": 80}},
        "evidence": ["changes completed banner is visible"],
    }

    result = trae_ui_analyst._normalize_analysis(
        data,
        {"task": "wait_completion_state", "window": {"bounds": {"left": 0, "top": 0, "width": 1000, "height": 800}}},
    )

    assert result["screen_state"] == "awaiting_keep_changes"
    assert result["recommended_action"] == "collect_trace_candidate"
    assert result["target"]["action"] == "keep_button"
