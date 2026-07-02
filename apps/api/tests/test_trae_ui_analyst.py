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


def test_trae_ui_analyst_allows_trae_delete_by_default_when_llm_marks_safe():
    data = {
        "status": "found",
        "screen_state": "awaiting_delete_confirmation",
        "recommended_action": "click_delete_button",
        "risk": "safe",
        "target": {"action": "delete_button", "label": "Delete generated draft", "center": {"x": 10, "y": 20}},
    }
    result = trae_ui_analyst._normalize_analysis(data, {"window": {"bounds": {"left": 0, "top": 0, "width": 100, "height": 100}}})

    assert result["risk"] == "safe"
    assert result["recommended_action"] == "click_delete_button"
    assert result["target"]["action"] == "delete_button"


def test_trae_ui_analyst_allows_trae_file_delete_target_list_by_default():
    data = {
        "status": "found",
        "screen_state": "awaiting_delete_confirmation",
        "recommended_action": "click_delete_button",
        "risk": "safe",
        "targets": [
            {
                "action": "delete_button",
                "label": "确认删除 4 个文件",
                "center": {"x": 10, "y": 20},
                "confidence": 0.91,
                "risk": "safe",
            }
        ],
    }
    result = trae_ui_analyst._normalize_analysis(
        data,
        {"task": "wait_completion_state", "window": {"bounds": {"left": 0, "top": 0, "width": 100, "height": 100}}},
    )

    assert result["risk"] == "safe"
    assert result["recommended_action"] == "click_delete_button"
    assert result["targets"][0]["risk"] == "safe"


def test_trae_ui_analyst_forces_waiting_delete_even_if_llm_says_completed():
    data = {
        "status": "found",
        "screen_state": "completed",
        "recommended_action": "collect_trace_candidate",
        "confidence": 0.93,
        "risk": "safe",
        "evidence": ["left task card looks stable"],
    }
    result = trae_ui_analyst._normalize_analysis(
        data,
        {
            "task": "wait_completion_state",
            "visible_text_sample": "正在等待你的操作\n删除 startup.log\n保留\n删除",
            "uia_buttons": [{"name": "保留"}, {"name": "删除"}],
            "window": {"bounds": {"left": 0, "top": 0, "width": 1000, "height": 800}},
        },
    )

    assert result["screen_state"] == "awaiting_delete_confirmation"
    assert result["recommended_action"] == "click_delete_button"
    assert result["risk"] == "safe"
    assert result["blocked_reason"] == ""


def test_trae_ui_analyst_prompt_teaches_waiting_delete_card():
    prompt = trae_ui_analyst._build_prompt(
        {
            "task": "wait_completion_state",
            "visible_text_sample": "正在等待你的操作\n删除 startup.log\n保留\n删除",
        }
    )

    assert "click_delete_button" in prompt
    assert "risk safe" in prompt
    assert "删除 startup.log" in prompt
    assert "not completed" in prompt


def test_trae_ui_analyst_blocks_target_when_llm_marks_blocked():
    data = {
        "status": "found",
        "screen_state": "awaiting_delete_confirmation",
        "recommended_action": "click_delete_button",
        "risk": "blocked",
        "target": {
            "action": "delete_button",
            "label": "Delete all changes",
            "center": {"x": 10, "y": 20},
            "risk": "blocked",
        },
    }
    result = trae_ui_analyst._normalize_analysis(data, {"window": {"bounds": {"left": 0, "top": 0, "width": 100, "height": 100}}})

    assert result["risk"] == "blocked"
    assert result["recommended_action"] == "do_not_click"


def test_trae_ui_analyst_allows_windows_firewall_for_local_server():
    data = {
        "status": "found",
        "screen_state": "awaiting_confirm",
        "recommended_action": "do_not_click",
        "confidence": 0.96,
        "risk": "blocked",
        "blocked_reason": "destructive_action_requires_manual_confirmation",
        "target": {
            "action": "confirm_button",
            "label": "\u5141\u8bb8",
            "center": {"x": 868, "y": 668},
            "confidence": 0.96,
            "risk": "blocked",
        },
        "targets": [
            {
                "action": "confirm_button",
                "label": "\u5141\u8bb8",
                "center": {"x": 868, "y": 668},
                "confidence": 0.96,
                "risk": "safe",
                "reason": "\u5141\u8bb8 server.exe \u7f51\u7edc\u8bbf\u95ee\u4ee5\u7ee7\u7eed\u672c\u5730 Web \u5e94\u7528\u9884\u89c8",
            },
            {
                "action": "cancel_button",
                "label": "\u53d6\u6d88",
                "center": {"x": 1066, "y": 668},
                "confidence": 0.94,
                "risk": "blocked",
            },
        ],
        "evidence": [
            "Windows \u5b89\u5168\u4e2d\u5fc3\u5f39\u7a97\u8be2\u95ee\u662f\u5426\u5141\u8bb8\u516c\u5171\u7f51\u7edc\u8bbf\u95ee server.exe",
            "The app is a localhost development preview.",
        ],
    }

    result = trae_ui_analyst._normalize_analysis(
        data,
        {"task": "wait_completion_state", "window": {"bounds": {"left": 0, "top": 0, "width": 1920, "height": 1032}}},
    )

    assert result["screen_state"] == "awaiting_confirm"
    assert result["recommended_action"] == "click_confirm_button"
    assert result["risk"] == "safe"
    assert result["blocked_reason"] == ""
    assert result["target"]["label"] == "\u5141\u8bb8"
    assert result["target"]["risk"] == "safe"


def test_trae_ui_analyst_normalizes_inner_panel_scroll_action():
    data = {
        "status": "partial",
        "screen_state": "needs_scroll_inner_panel",
        "recommended_action": "scroll_inner_panel",
        "confidence": 0.87,
        "risk": "safe",
        "evidence": ["正在等待你的操作", "操作卡片内部未完整显示"],
    }

    result = trae_ui_analyst._normalize_analysis(
        data,
        {"task": "wait_completion_state", "window": {"bounds": {"left": 0, "top": 0, "width": 100, "height": 100}}},
    )

    assert result["screen_state"] == "needs_scroll_inner_panel"
    assert result["recommended_action"] == "scroll_inner_panel"
    assert result["risk"] == "safe"


def test_trae_ui_analyst_preserves_expand_confirm_card_action():
    data = {
        "status": "found",
        "screen_state": "awaiting_collapsed_confirm_card",
        "recommended_action": "expand_confirm_card",
        "confidence": 0.89,
        "risk": "safe",
        "target": {
            "action": "expand_confirm_card",
            "label": "确认执行",
            "center": {"x": 240, "y": 320},
            "confidence": 0.89,
            "risk": "safe",
        },
        "evidence": ["Only the confirmation header is visible."],
    }

    result = trae_ui_analyst._normalize_analysis(
        data,
        {"task": "wait_completion_state", "window": {"bounds": {"left": 0, "top": 0, "width": 1000, "height": 800}}},
    )

    assert result["screen_state"] == "awaiting_collapsed_confirm_card"
    assert result["recommended_action"] == "expand_confirm_card"
    assert result["target"]["action"] == "expand_confirm_card"
    assert result["risk"] == "safe"


def test_trae_ui_analyst_treats_keep_bar_as_click_target_during_wait_completion():
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
    assert result["recommended_action"] == "click_keep_button"
    assert result["target"]["action"] == "keep_button"
