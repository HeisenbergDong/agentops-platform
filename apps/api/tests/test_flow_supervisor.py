from app.services.orchestrator.flow_supervisor import _can_accept_llm_decision


def test_flow_supervisor_rejects_manual_required_for_safe_firewall_allow():
    rule_decision = {
        "action": "apply_ui_suggestion",
        "context": {
            "suggested_action": "confirm_button",
            "suggested_recommended_action": "click_confirm_button",
            "suggested_risk": "safe",
            "visual_screen_state": "awaiting_confirm",
            "visual_recommended_action": "click_confirm_button",
            "visual_risk": "safe",
            "visual_target_action": "confirm_button",
            "visual_target_label": "\u5141\u8bb8",
            "visual_target_reason": "\u5141\u8bb8 server.exe \u7f51\u7edc\u8bbf\u95ee",
            "visual_evidence": [
                "Windows \u5b89\u5168\u4e2d\u5fc3 asks whether to allow public network access for server.exe",
                "The app is a localhost development preview.",
            ],
        },
    }
    llm_decision = {
        "action": "manual_required",
        "confidence": 0.96,
        "source": "llm",
    }

    assert _can_accept_llm_decision(rule_decision, llm_decision) is False
