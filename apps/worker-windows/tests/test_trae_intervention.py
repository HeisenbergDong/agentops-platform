import pytest
from PIL import Image, ImageDraw

from worker.trae import wait as wait_module
from worker.trae.diagnose import diagnose_ui
from worker.trae.intervene import TraeAutomationError, apply_intervention, click_continue
from worker.trae.ui_locator import locate_prompt_targets, locate_visible_action_targets, target_for_action


def _watcher_observation(recent: bool = False) -> dict:
    return {
        "activity": {
            "recent": recent,
            "source": "agent_log" if recent else "",
            "quiet_seconds": 1.2 if recent else 999.0,
            "path": "C:/Trae/ai-agent_stdout.log" if recent else "",
            "last_write": "2026-06-13T12:00:00" if recent else "",
        },
        "project_write": {"mtime": 0.0, "path": "", "last_write": ""},
        "log": {"path": "C:/Trae/ai-agent_stdout.log", "mtime": 1.0, "tail_hash": "abc123"},
        "log_sample": [],
        "latest_text_hash": "txt",
        "idle_seconds": 0.0,
    }


def test_diagnose_ui_classifies_safe_action_button(monkeypatch: pytest.MonkeyPatch):
    class Rect:
        left = 100
        top = 200
        right = 260
        bottom = 240

    class FakeButton:
        def window_text(self):
            return "\u4ecd\u8981\u8fd0\u884c"

        def rectangle(self):
            return Rect()

    class FakeWindow:
        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return [FakeButton()] if control_type == "Button" else []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr("worker.trae.diagnose.window_text_snapshot", lambda window, limit=500: "waiting")

    result = diagnose_ui()

    assert result["ok"] is True
    assert result["state"] == "awaiting_run_anyway"
    assert result["suggested_intervention"]["mode"] == "click-point"


def test_diagnose_ui_allows_local_delete_confirmation(monkeypatch: pytest.MonkeyPatch):
    class Rect:
        left = 325
        top = 555
        right = 683
        bottom = 590

    class FakeButton:
        def window_text(self):
            return "\u786e\u8ba4"

        def rectangle(self):
            return Rect()

    class FakeWindow:
        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return [FakeButton()] if control_type == "Button" else []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr(
        "worker.trae.diagnose.window_text_snapshot",
        lambda window, limit=500: "\u786e\u8ba4\u5220\u9664 4 \u4e2a\u6587\u4ef6\uff1f\u5220\u9664\u540e\u6587\u4ef6\u65e0\u6cd5\u6062\u590d",
    )

    result = diagnose_ui()

    assert result["ok"] is True
    assert result["state"] == "awaiting_delete_confirmation"
    assert result["suggested_intervention"]["action"] == "delete_button"
    assert result["suggested_intervention"]["mode"] == "click-point"
    assert result["suggested_intervention"]["risk"] == "safe"
    assert result["suggested_intervention"]["recommended_action"] == "click_delete_button"


def test_diagnose_ui_allows_waiting_delete_card_even_if_vision_says_completed(monkeypatch: pytest.MonkeyPatch, tmp_path):
    screenshot = tmp_path / "trae-waiting-delete-card.png"
    Image.new("RGB", (1920, 1032), (28, 29, 30)).save(screenshot)
    seen_context = {}

    class DeleteRect:
        left = 378
        top = 655
        right = 435
        bottom = 686

    class FakeButton:
        def window_text(self):
            return "\u5220\u9664"

        def rectangle(self):
            return DeleteRect()

    class FakeWindow:
        hwnd = 101

        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return [FakeButton()] if control_type == "Button" else []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr(
        "worker.trae.diagnose.window_text_snapshot",
        lambda window, limit=500: "\u6b63\u5728\u7b49\u5f85\u4f60\u7684\u64cd\u4f5c\n\u5220\u9664 startup.log\n\u4fdd\u7559\n\u5220\u9664",
    )
    monkeypatch.setattr(
        "worker.trae.diagnose._window_rect",
        lambda window: {"left": 0, "top": 0, "right": 1920, "bottom": 1032, "width": 1920, "height": 1032},
    )
    monkeypatch.setattr(
        "worker.trae.diagnose.capture_screenshot",
        lambda target, timeout_seconds, quality_required: {"status": "captured", "path": str(screenshot)},
    )

    def fake_analyst(path, context):
        seen_context.update(context)
        return {
            "analysis": {
                "status": "found",
                "screen_state": "completed",
                "recommended_action": "collect_trace_candidate",
                "confidence": 0.93,
                "risk": "safe",
            }
        }

    result = diagnose_ui(ui_analyst=fake_analyst)

    assert result["ok"] is True
    assert result["state"] == "awaiting_delete_confirmation"
    assert result["reason"] == "local_delete_confirmation_allowed"
    assert result["suggested_intervention"]["mode"] == "click-point"
    assert result["suggested_intervention"]["recommended_action"] == "click_delete_button"
    assert result["suggested_intervention"]["risk"] == "safe"
    assert any("waiting-for-user-action" in item for item in seen_context["manual_required_rules"])
    assert "click_delete_button" in seen_context["manual_required_rules"][1]


def test_diagnose_ui_scrolls_again_when_action_card_is_below_view(monkeypatch: pytest.MonkeyPatch):
    class Rect:
        left = 100
        top = 650
        right = 220
        bottom = 690

    class FakeButton:
        def window_text(self):
            return "\u6267\u884c"

        def rectangle(self):
            return Rect()

    class FakeWindow:
        def __init__(self):
            self.scans = 0

        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            if control_type != "Button":
                return []
            self.scans += 1
            return [] if self.scans == 1 else [FakeButton()]

    fake_window = FakeWindow()
    scrolls = []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: fake_window)
    monkeypatch.setattr(
        "worker.trae.diagnose.scroll_assistant_to_bottom",
        lambda window: scrolls.append("scroll") or {"status": "scrolled", "attempt": len(scrolls)},
    )
    monkeypatch.setattr("worker.trae.diagnose.window_text_snapshot", lambda window, limit=500: "waiting")

    result = diagnose_ui()

    assert result["ok"] is True
    assert result["state"] == "awaiting_execute"
    assert len(scrolls) == 2
    assert result["diagnosis_attempts"][0]["match_count"] == 0
    assert result["diagnosis_attempts"][1]["match_count"] == 1


def test_diagnose_ui_expands_collapsed_confirm_execution_card(monkeypatch: pytest.MonkeyPatch):
    class Rect:
        left = 90
        top = 340
        right = 410
        bottom = 382

    class FakeHeaderButton:
        def window_text(self):
            return "\u786e\u8ba4\u6267\u884c"

        def rectangle(self):
            return Rect()

    class FakeWindow:
        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return [FakeHeaderButton()] if control_type == "Button" else []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr(
        "worker.trae.diagnose.window_text_snapshot",
        lambda window, limit=500: "\u786e\u8ba4\u6267\u884c\n\u6b63\u5728\u7b49\u5f85\u4f60\u7684\u64cd\u4f5c",
    )

    result = diagnose_ui()

    assert result["ok"] is True
    assert result["state"] == "awaiting_collapsed_confirm_card"
    assert result["matches"] == []
    assert result["suggested_intervention"] == {
        "mode": "expand-confirm-card",
        "action": "expand_confirm_card",
        "x": 250,
        "y": 361,
        "button": "\u786e\u8ba4\u6267\u884c",
        "confidence": 0.88,
        "source": "local_uia",
        "risk": "safe",
        "recommended_action": "expand_confirm_card",
    }


def test_diagnose_ui_expands_collapsed_card_before_ai_scroll(monkeypatch: pytest.MonkeyPatch, tmp_path):
    screenshot = tmp_path / "collapsed-confirm-card.png"
    Image.new("RGB", (1920, 1032), (28, 29, 30)).save(screenshot)

    class Rect:
        left = 90
        top = 340
        right = 410
        bottom = 382

    class FakeHeaderButton:
        def window_text(self):
            return "\u786e\u8ba4\u6267\u884c"

        def rectangle(self):
            return Rect()

    class FakeWindow:
        hwnd = 101

        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return [FakeHeaderButton()] if control_type == "Button" else []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr(
        "worker.trae.diagnose.window_text_snapshot",
        lambda window, limit=500: "\u786e\u8ba4\u6267\u884c\n\u6b63\u5728\u7b49\u5f85\u4f60\u7684\u64cd\u4f5c",
    )
    monkeypatch.setattr(
        "worker.trae.diagnose._window_rect",
        lambda window: {"left": 0, "top": 0, "right": 1920, "bottom": 1032, "width": 1920, "height": 1032},
    )
    monkeypatch.setattr(
        "worker.trae.diagnose.capture_screenshot",
        lambda target, timeout_seconds, quality_required: {"status": "captured", "path": str(screenshot)},
    )

    result = diagnose_ui(
        ui_analyst=lambda path, context: {
            "analysis": {
                "status": "partial",
                "screen_state": "needs_scroll_inner_panel",
                "recommended_action": "scroll_inner_panel",
                "confidence": 0.82,
                "risk": "safe",
            }
        }
    )

    assert result["state"] == "awaiting_collapsed_confirm_card"
    assert result["suggested_intervention"]["mode"] == "expand-confirm-card"
    assert result["suggested_intervention"]["recommended_action"] == "expand_confirm_card"


def test_diagnose_ui_prioritizes_3003_recovery_over_keep_button(monkeypatch: pytest.MonkeyPatch):
    class Rect:
        left = 100
        top = 200
        right = 220
        bottom = 240

    class FakeButton:
        def window_text(self):
            return "\u4fdd\u7559"

        def rectangle(self):
            return Rect()

    class FakeWindow:
        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return [FakeButton()] if control_type == "Button" else []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr(
        "worker.trae.diagnose.window_text_snapshot",
        lambda window, limit=500: "模型请求失败，请稍后重试。(3003)",
    )

    result = diagnose_ui()

    assert result["state"] == "service_interrupted"
    assert result["suggested_intervention"] == {"mode": "continue-text", "action": "continue", "text": "\u7ee7\u7eed"}


def test_diagnose_ui_prefers_llm_continue_over_local_run_button(monkeypatch: pytest.MonkeyPatch):
    class Rect:
        left = 100
        top = 200
        right = 220
        bottom = 240

    class FakeButton:
        def window_text(self):
            return "\u6267\u884c"

        def rectangle(self):
            return Rect()

    class FakeWindow:
        hwnd = 101

        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return [FakeButton()] if control_type == "Button" else []

    def fake_ui_analyst(path, context):
        return {
            "analysis": {
                "status": "found",
                "screen_state": "model_error_3003",
                "recommended_action": "type_continue",
                "confidence": 0.95,
                "risk": "safe",
                "reason": "model request failed and the composer is available",
            }
        }

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr("worker.trae.diagnose.window_text_snapshot", lambda window, limit=500: "\u6700\u5c0f\u5316\n\u6062\u590d\n\u5173\u95ed")
    monkeypatch.setattr("worker.trae.diagnose._window_rect", lambda window: {"left": 0, "top": 0, "right": 1000, "bottom": 1000})
    monkeypatch.setattr(
        "worker.trae.diagnose._local_visual_suggested_intervention",
        lambda visual, window_rect: {
            "state": "awaiting_run_confirmation",
            "confidence": 0.86,
            "reason": "local_run_button",
            "suggested_intervention": {
                "mode": "click-point",
                "action": "run_button",
                "x": 677,
                "y": 394,
                "button": "\u6267\u884c",
            },
        },
    )
    monkeypatch.setattr(
        "worker.trae.diagnose.capture_screenshot",
        lambda target, timeout_seconds, quality_required: {
            "path": "fake.png",
            "status": "captured",
        },
    )

    result = diagnose_ui(ui_analyst=fake_ui_analyst)

    assert result["state"] == "model_error_3003"
    assert result["suggested_intervention"] == {"mode": "continue-text", "action": "continue", "text": "\u7ee7\u7eed"}


def test_diagnose_ui_prefers_ai_firewall_allow_over_local_run_button(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    screenshot = tmp_path / "windows-firewall.png"
    Image.new("RGB", (1920, 1032), (28, 29, 30)).save(screenshot)

    class FakeWindow:
        hwnd = 101

        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return []

    def fake_ui_analyst(path, context):
        assert path == str(screenshot)
        return {
            "analysis": {
                "status": "found",
                "screen_state": "awaiting_confirm",
                "recommended_action": "click_confirm_button",
                "confidence": 0.96,
                "risk": "safe",
                "target": {
                    "action": "confirm_button",
                    "label": "\u5141\u8bb8",
                    "center": {"x": 868, "y": 668},
                    "ratio": {"x": 0.4525, "y": 0.645},
                    "confidence": 0.96,
                    "risk": "safe",
                    "reason": "\u5141\u8bb8 server.exe \u7f51\u7edc\u8bbf\u95ee",
                },
                "evidence": [
                    "Windows \u5b89\u5168\u4e2d\u5fc3 asks whether to allow public network access for server.exe",
                ],
            }
        }

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr("worker.trae.diagnose.window_text_snapshot", lambda window, limit=500: "\u6700\u5c0f\u5316\n\u5173\u95ed")
    monkeypatch.setattr(
        "worker.trae.diagnose._window_rect",
        lambda window: {"left": 0, "top": 0, "right": 1920, "bottom": 1032, "width": 1920, "height": 1032},
    )
    monkeypatch.setattr(
        "worker.trae.diagnose.capture_screenshot",
        lambda target, timeout_seconds, quality_required: {"status": "captured", "path": str(screenshot)},
    )
    monkeypatch.setattr(
        "worker.trae.diagnose._local_visual_suggested_intervention",
        lambda visual, window_rect: {
            "state": "awaiting_run_confirmation",
            "confidence": 0.86,
            "reason": "local_run_button",
            "suggested_intervention": {
                "mode": "click-point",
                "action": "run_button",
                "x": 834,
                "y": 449,
                "button": "\u6267\u884c",
                "source": "local_vision",
                "risk": "safe",
                "recommended_action": "click_run_button",
            },
        },
    )

    result = diagnose_ui(ui_analyst=fake_ui_analyst)

    assert result["state"] == "awaiting_confirm"
    assert result["suggested_intervention"]["action"] == "confirm_button"
    assert result["suggested_intervention"]["x"] == 868
    assert result["suggested_intervention"]["y"] == 668
    assert result["suggested_intervention"]["source"] == "ai_vision"


def test_diagnose_ui_detects_terminal_prompt(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr(
        "worker.trae.diagnose.window_text_snapshot",
        lambda window, limit=500: "Need to install create-vite@latest\nOk to proceed? (y)",
    )

    result = diagnose_ui()

    assert result["state"] == "awaiting_terminal_input"
    assert result["suggested_intervention"]["text"] == "y"


def test_local_vision_finds_high_risk_run_button(tmp_path):
    path = tmp_path / "trae-risk-card.png"
    image = Image.new("RGB", (1920, 1032), (28, 29, 30))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((300, 356, 722, 608), radius=8, fill=(48, 49, 52), outline=(120, 120, 126))
    draw.rounded_rectangle((300, 528, 722, 608), radius=8, fill=(80, 58, 35))
    draw.rounded_rectangle((380, 498, 430, 528), radius=4, fill=(226, 228, 232))
    draw.rounded_rectangle((442, 498, 650, 528), radius=4, fill=(226, 228, 232))
    draw.rounded_rectangle((662, 498, 709, 528), radius=4, fill=(240, 243, 248))
    image.save(path)

    analysis = locate_visible_action_targets(path, (0, 0, 1920, 1032))
    target = target_for_action(analysis, "run_button", min_confidence=0.72)

    assert analysis["status"] == "found"
    assert target is not None
    assert target["center"]["x"] > 640
    assert 490 <= target["center"]["y"] <= 540


def test_diagnose_ui_uses_llm_visual_when_webview_buttons_are_hidden(monkeypatch: pytest.MonkeyPatch, tmp_path):
    screenshot = tmp_path / "trae-risk-card.png"
    Image.new("RGB", (1920, 1032), (28, 29, 30)).save(screenshot)

    class FakeWindow:
        hwnd = 101

        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr("worker.trae.diagnose.window_text_snapshot", lambda window, limit=500: "\u6700\u5c0f\u5316\n\u5173\u95ed")
    monkeypatch.setattr(
        "worker.trae.diagnose._window_rect",
        lambda window: {"left": 0, "top": 0, "right": 1920, "bottom": 1032, "width": 1920, "height": 1032},
    )
    monkeypatch.setattr(
        "worker.trae.diagnose.capture_screenshot",
        lambda target, timeout_seconds, quality_required: {"status": "captured", "path": str(screenshot)},
    )
    def fake_analyst(path, context):
        assert path == str(screenshot)
        assert context["task"] == "find_reply_action_button"
        return {
            "analysis": {
                "status": "found",
                "screen_state": "awaiting_run_confirmation",
                "recommended_action": "click_run_button",
                "confidence": 0.91,
                "risk": "safe",
                "target": {
                    "action": "run_button",
                    "label": "\u6267\u884c",
                    "center": {"x": 685, "y": 512},
                    "ratio": {"x": 0.3568, "y": 0.4961},
                    "confidence": 0.91,
                    "risk": "safe",
                    "reason": "visible execute button",
                },
            }
        }

    result = diagnose_ui(ui_analyst=fake_analyst)

    assert result["ok"] is True
    assert result["state"] == "awaiting_run_confirmation"
    assert result["suggested_intervention"]["mode"] == "click-point"
    assert result["suggested_intervention"]["action"] == "run_button"
    assert result["suggested_intervention"]["source"] == "ai_vision"


def test_diagnose_ui_uses_local_visual_run_button_without_llm(monkeypatch: pytest.MonkeyPatch, tmp_path):
    screenshot = tmp_path / "trae-risk-card.png"
    image = Image.new("RGB", (1920, 1032), (28, 29, 30))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((300, 356, 722, 608), radius=8, fill=(48, 49, 52), outline=(120, 120, 126))
    draw.rounded_rectangle((300, 528, 722, 608), radius=8, fill=(80, 58, 35))
    draw.rounded_rectangle((380, 498, 430, 528), radius=4, fill=(226, 228, 232))
    draw.rounded_rectangle((442, 498, 650, 528), radius=4, fill=(226, 228, 232))
    draw.rounded_rectangle((662, 498, 709, 528), radius=4, fill=(240, 243, 248))
    image.save(screenshot)

    class FakeWindow:
        hwnd = 101

        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr("worker.trae.diagnose.window_text_snapshot", lambda window, limit=500: "\u786e\u8ba4\u6267\u884c\n\u6b63\u5728\u7b49\u5f85\u4f60\u7684\u64cd\u4f5c")
    monkeypatch.setattr(
        "worker.trae.diagnose._window_rect",
        lambda window: {"left": 0, "top": 0, "right": 1920, "bottom": 1032, "width": 1920, "height": 1032},
    )
    monkeypatch.setattr(
        "worker.trae.diagnose.capture_screenshot",
        lambda target, timeout_seconds, quality_required: {"status": "captured", "path": str(screenshot)},
    )

    result = diagnose_ui(ui_analyst=None)

    assert result["ok"] is True
    assert result["state"] == "awaiting_run_confirmation"
    assert result["suggested_intervention"]["mode"] == "click-point"
    assert result["suggested_intervention"]["action"] == "run_button"
    assert result["suggested_intervention"]["source"] == "local_vision"


def test_diagnose_ui_allows_llm_delete_confirmation(monkeypatch: pytest.MonkeyPatch, tmp_path):
    screenshot = tmp_path / "trae-delete-card.png"
    Image.new("RGB", (1920, 1032), (28, 29, 30)).save(screenshot)

    class FakeWindow:
        hwnd = 101

        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr("worker.trae.diagnose.window_text_snapshot", lambda window, limit=500: "\u6700\u5c0f\u5316\n\u5173\u95ed")
    monkeypatch.setattr(
        "worker.trae.diagnose._window_rect",
        lambda window: {"left": 0, "top": 0, "right": 1920, "bottom": 1032, "width": 1920, "height": 1032},
    )
    monkeypatch.setattr(
        "worker.trae.diagnose.capture_screenshot",
        lambda target, timeout_seconds, quality_required: {"status": "captured", "path": str(screenshot)},
    )

    result = diagnose_ui(
        ui_analyst=lambda path, context: {
            "analysis": {
                "status": "found",
                "screen_state": "awaiting_delete_confirmation",
                "recommended_action": "click_delete_button",
                "confidence": 0.88,
                "risk": "safe",
                "target": {
                    "action": "delete_button",
                    "label": "\u5220\u9664",
                    "center": {"x": 600, "y": 620},
                    "confidence": 0.88,
                    "risk": "safe",
                },
            }
        }
    )

    assert result["ok"] is True
    assert result["state"] == "awaiting_safe_delete_confirmation"
    assert result["suggested_intervention"]["mode"] == "click-point"
    assert result["suggested_intervention"]["risk"] == "safe"
    assert result["suggested_intervention"]["recommended_action"] == "click_delete_button"


def test_locate_prompt_targets_does_not_treat_stop_square_as_send(tmp_path):
    image = Image.new("RGB", (1000, 800), (28, 29, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 610, 420, 790), fill=(40, 42, 48), outline=(70, 72, 82), width=2)
    send_x = int(1000 * 0.364)
    send_y = int(800 * 0.945)
    draw.rounded_rectangle((send_x - 20, send_y - 20, send_x + 20, send_y + 20), radius=8, fill=(37, 220, 132))
    draw.rectangle((send_x - 7, send_y - 7, send_x + 7, send_y + 7), fill=(22, 48, 47))
    screenshot = tmp_path / "trae-stop-square.png"
    image.save(screenshot)

    result = locate_prompt_targets(screenshot, (0, 0, 1000, 800))
    actions = {item["action"] for item in result["targets"]}

    assert "prompt_input" in actions
    assert "send_button" not in actions


def test_apply_intervention_rejects_bottom_stop_button_region(monkeypatch: pytest.MonkeyPatch):
    clicks = []

    class FakeWindow:
        hwnd = 101

    monkeypatch.setattr("worker.trae.intervene.focus_trae", lambda **_kwargs: {"status": "focused"})
    monkeypatch.setattr("worker.trae.intervene.find_trae_window", lambda **_kwargs: FakeWindow())
    monkeypatch.setattr("worker.trae.intervene._window_rect", lambda _hwnd: (0, 0, 1000, 800))
    monkeypatch.setattr("worker.trae.intervene._mouse_click", lambda x, y: clicks.append((x, y)))

    with pytest.raises(TraeAutomationError):
        apply_intervention({"mode": "click-point", "action": "continue_button", "risk": "safe", "x": 364, "y": 756})

    assert clicks == []


def test_apply_intervention_rejects_misclassified_send_button_in_stop_region(monkeypatch: pytest.MonkeyPatch):
    clicks = []

    class FakeWindow:
        hwnd = 101

    monkeypatch.setattr("worker.trae.intervene.focus_trae", lambda **_kwargs: {"status": "focused"})
    monkeypatch.setattr("worker.trae.intervene.find_trae_window", lambda **_kwargs: FakeWindow())
    monkeypatch.setattr("worker.trae.intervene._window_rect", lambda _hwnd: (0, 0, 1000, 800))
    monkeypatch.setattr("worker.trae.intervene._mouse_click", lambda x, y: clicks.append((x, y)))

    with pytest.raises(TraeAutomationError):
        apply_intervention({"mode": "click-point", "action": "send_button", "risk": "safe", "x": 364, "y": 756})

    assert clicks == []


def test_diagnose_ui_uses_llm_inner_panel_scroll(monkeypatch: pytest.MonkeyPatch, tmp_path):
    screenshot = tmp_path / "trae-inner-card.png"
    Image.new("RGB", (1920, 1032), (28, 29, 30)).save(screenshot)

    class FakeWindow:
        hwnd = 101

        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr("worker.trae.diagnose.window_text_snapshot", lambda window, limit=500: "\u6b63\u5728\u7b49\u5f85\u4f60\u7684\u64cd\u4f5c")
    monkeypatch.setattr(
        "worker.trae.diagnose._window_rect",
        lambda window: {"left": 0, "top": 0, "right": 1920, "bottom": 1032, "width": 1920, "height": 1032},
    )
    monkeypatch.setattr(
        "worker.trae.diagnose.capture_screenshot",
        lambda target, timeout_seconds, quality_required: {"status": "captured", "path": str(screenshot)},
    )

    result = diagnose_ui(
        ui_analyst=lambda path, context: {
            "analysis": {
                "status": "partial",
                "screen_state": "needs_scroll_inner_panel",
                "recommended_action": "scroll_inner_panel",
                "confidence": 0.89,
                "risk": "safe",
                "evidence": ["\u6b63\u5728\u7b49\u5f85\u4f60\u7684\u64cd\u4f5c", "\u5185\u5c42\u5361\u7247\u672a\u5b8c\u5168\u663e\u793a"],
            }
        }
    )

    assert result["ok"] is True
    assert result["state"] == "needs_scroll_inner_panel"
    assert result["suggested_intervention"]["mode"] == "scroll-inner-panel"


def test_diagnose_ui_scrolls_inner_panel_from_waiting_action_text(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        hwnd = 101

        def window_text(self):
            return "Trae CN"

        def descendants(self, control_type):
            return []

    monkeypatch.setattr("worker.trae.diagnose.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.diagnose.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr("worker.trae.diagnose.scroll_assistant_to_bottom", lambda window: {"status": "scrolled"})
    monkeypatch.setattr("worker.trae.diagnose.window_text_snapshot", lambda window, limit=500: "正在等待您的操作")
    monkeypatch.setattr(
        "worker.trae.diagnose._window_rect",
        lambda window: {"left": 0, "top": 0, "right": 1920, "bottom": 1032, "width": 1920, "height": 1032},
    )

    result = diagnose_ui(ui_analyst=None)

    assert result["ok"] is True
    assert result["state"] == "needs_scroll_inner_panel"
    assert result["reason"] == "waiting_action_inner_panel_hidden"
    assert result["suggested_intervention"]["mode"] == "scroll-inner-panel"


def test_apply_intervention_rejects_unknown_mode():
    with pytest.raises(Exception):
        apply_intervention({"mode": "unknown"})


def test_continue_text_intervention_targets_chat_prompt(monkeypatch: pytest.MonkeyPatch):
    calls = []

    monkeypatch.setattr(
        "worker.trae.intervene.send_prompt",
        lambda text, submit=True: calls.append((text, submit)) or {"input": {"method": "adbz_coordinate_primary"}},
    )

    result = apply_intervention({"mode": "continue-text", "text": "\u7ee7\u7eed"})

    assert calls == [("\u7ee7\u7eed", True)]
    assert result["status"] == "applied"
    assert result["mode"] == "continue-text"
    assert result["input"]["method"] == "adbz_coordinate_primary"


def test_apply_intervention_expands_confirm_card(monkeypatch: pytest.MonkeyPatch):
    clicks = []

    monkeypatch.setattr("worker.trae.intervene.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr("worker.trae.intervene.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "worker.trae.intervene.click_screen_point",
        lambda x, y: clicks.append((x, y)) or {"status": "applied", "mode": "click-point", "x": int(x), "y": int(y)},
    )

    result = apply_intervention(
        {
            "mode": "expand-confirm-card",
            "action": "expand_confirm_card",
            "x": 250,
            "y": 361,
            "risk": "safe",
        }
    )

    assert clicks == [(250, 361)]
    assert result["status"] == "applied"
    assert result["mode"] == "expand-confirm-card"
    assert result["action"] == "expand_confirm_card"


def test_click_continue_reports_typed_continue_when_no_button(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        def descendants(self, control_type):
            return []

    monkeypatch.setattr("worker.trae.intervene.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(
        "worker.trae.intervene.diagnose_ui",
        lambda timeout_seconds, scroll_bottom, **_kwargs: {
            "state": "awaiting_continue",
            "suggested_intervention": {"mode": "continue-text", "text": "\u7ee7\u7eed"},
        },
    )
    monkeypatch.setattr(
        "worker.trae.intervene.send_prompt",
        lambda text, submit=True: {"input": {"method": "adbz_coordinate_primary"}},
    )
    monkeypatch.setattr("worker.trae.intervene.find_trae_window", lambda timeout_seconds: FakeWindow())

    result = click_continue()

    assert result["status"] == "clicked"
    assert result["action_taken"] == "typed_continue"
    assert result["intervention"]["mode"] == "continue-text"


def test_click_continue_types_continue_for_service_interruption_reason(monkeypatch: pytest.MonkeyPatch):
    calls = []

    class FakeWindow:
        def descendants(self, control_type):
            return []

    monkeypatch.setattr("worker.trae.intervene.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(
        "worker.trae.intervene.diagnose_ui",
        lambda timeout_seconds, scroll_bottom, **_kwargs: {
            "state": "idle_or_running",
            "suggested_intervention": {},
            "output_probe": {"reason": "missing_tool_trace_markers"},
        },
    )
    monkeypatch.setattr("worker.trae.intervene.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(
        "worker.trae.intervene.send_prompt",
        lambda text, submit=True: calls.append((text, submit)) or {"input": {"method": "adbz_coordinate_primary"}},
    )
    monkeypatch.setattr("worker.trae.intervene.click_visual_intervention", lambda **kwargs: {"status": "not_clicked"})
    monkeypatch.setattr("worker.trae.intervene.click_primary_fallback", lambda: {"status": "clicked", "mode": "primary-fallback"})

    result = click_continue(recovery_reason="service_interrupted")

    assert calls == [("\u7ee7\u7eed", True)]
    assert result["action_taken"] == "typed_continue"
    assert result["intervention"]["mode"] == "continue-text"


def test_click_continue_rejects_idle_state_without_explicit_target(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        def descendants(self, control_type):
            return []

    monkeypatch.setattr("worker.trae.intervene.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(
        "worker.trae.intervene.diagnose_ui",
        lambda timeout_seconds, scroll_bottom, **_kwargs: {
            "state": "idle_or_running",
            "suggested_intervention": {},
            "output_probe": {"reason": "missing_tool_trace_markers"},
        },
    )
    monkeypatch.setattr("worker.trae.intervene.find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(
        "worker.trae.intervene.click_visual_intervention",
        lambda **kwargs: pytest.fail("visual cache should not be used without explicit evidence"),
    )
    monkeypatch.setattr(
        "worker.trae.intervene.click_primary_fallback",
        lambda: pytest.fail("primary fallback should not be used without explicit evidence"),
    )

    with pytest.raises(TraeAutomationError) as exc:
        click_continue(recovery_reason="worker_command_error")

    assert "No explicit Trae intervention target" in str(exc.value)


def test_wait_completion_runs_idle_intervention(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    interventions = []

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds, **_kwargs: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds, **_kwargs: FakeWindow())
    monkeypatch.setattr(wait_module, "focus_trae_workspace_or_any", lambda timeout_seconds, workspace_path, **_kwargs: {"status": "focused"})
    monkeypatch.setattr(wait_module, "wait_for_workspace_window_or_any", lambda timeout_seconds, workspace_path, **_kwargs: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "Trae waiting for confirmation")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        wait_module,
        "probe_latest_trae_turn",
        lambda **kwargs: (
            {"status": "found", "turn_status": "completed", "session_id": "s1", "user_message_id": "u1"}
            if interventions
            else {"status": "missing", "reason": "no_completed_turn_after_prompt_send"}
        ),
    )
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(
        wait_module,
        "diagnose_ui",
        lambda timeout_seconds, scroll_bottom, **_kwargs: {
            "state": "awaiting_continue",
            "suggested_intervention": {"mode": "continue-text", "text": "\u7ee7\u7eed"},
        },
    )

    def fake_apply(intervention, timeout_seconds):
        interventions.append(intervention)
        return {"status": "applied"}

    monkeypatch.setattr(wait_module, "apply_intervention", fake_apply)

    result = wait_module.wait_completion(
        timeout_seconds=3,
        stable_seconds=1,
        poll_interval_seconds=0.5,
        intervention_idle_seconds=0.5,
        max_interventions=1,
    )

    assert result["status"] == "completed"
    assert interventions == [{"mode": "continue-text", "text": "\u7ee7\u7eed"}]
    assert result["interventions"]


def test_wait_completion_intervenes_on_pending_ui_before_local_turn_completes(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    interventions = []
    diagnosis_calls = {"count": 0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds, **_kwargs: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds, **_kwargs: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "\u786e\u8ba4\u6267\u884c\nTrae waiting for run confirmation")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        wait_module,
        "probe_latest_trae_turn",
        lambda **kwargs: (
            {"status": "found", "turn_status": "completed", "session_id": "s1", "user_message_id": "u1"}
            if interventions
            else {"status": "missing", "reason": "no_completed_turn_after_prompt_send"}
        ),
    )
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)

    def fake_diagnose(timeout_seconds, scroll_bottom, **_kwargs):
        diagnosis_calls["count"] += 1
        if diagnosis_calls["count"] <= 2:
            return {
                "state": "awaiting_run_anyway",
                "suggested_intervention": {"mode": "click-point", "x": 1, "y": 2},
            }
        return {"state": "idle_or_running", "suggested_intervention": {}}

    monkeypatch.setattr(wait_module, "diagnose_ui", fake_diagnose)

    def fake_apply(intervention, timeout_seconds):
        interventions.append(intervention)
        return {"status": "applied"}

    monkeypatch.setattr(wait_module, "apply_intervention", fake_apply)

    result = wait_module.wait_completion(
        timeout_seconds=4,
        stable_seconds=0.5,
        poll_interval_seconds=0.5,
        intervention_idle_seconds=0.5,
        max_interventions=1,
    )

    assert result["status"] == "completed"
    assert interventions == [{"mode": "click-point", "x": 1, "y": 2}]


def test_wait_completion_accepts_completed_turn_with_pending_keep_text(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    diagnosis_calls = {"count": 0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds, **_kwargs: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds, **_kwargs: FakeWindow())
    monkeypatch.setattr(
        wait_module,
        "window_text_snapshot",
        lambda window: "\u4efb\u52a1\u5b8c\u6210\n\u53d8\u66f4\u5df2\u5b8c\u6210\uff0c\u8bf7\u786e\u8ba4\u662f\u5426\u91c7\u7eb3\u3002\n\u4fdd\u7559 Ctrl+Enter",
    )
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        wait_module,
        "probe_latest_trae_turn",
        lambda **kwargs: {"status": "found", "turn_status": "completed", "session_id": "s1", "user_message_id": "u1"},
    )
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    def fake_diagnose(timeout_seconds, scroll_bottom, **_kwargs):
        diagnosis_calls["count"] += 1
        return {"state": "awaiting_keep", "suggested_intervention": {"mode": "click-point", "x": 1, "y": 2}}

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(wait_module, "diagnose_ui", fake_diagnose)

    result = wait_module.wait_completion(
        timeout_seconds=3,
        stable_seconds=0.5,
        poll_interval_seconds=0.5,
        intervention_idle_seconds=0.5,
        max_interventions=1,
    )

    assert result["status"] == "completed"
    assert result["completion_gate"]["pending_intervention_visible"] is True
    assert diagnosis_calls["count"] == 0
    assert result["interventions"] == []


def test_wait_completion_prioritizes_service_interruption_before_visible_buttons(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    interventions = []
    diagnosis_calls = {"count": 0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(
        wait_module,
        "window_text_snapshot",
        lambda window: "模型请求失败，请稍后重试。(3003)\n保留 Ctrl+Enter",
    )
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(wait_module, "probe_latest_trae_turn", lambda **kwargs: {"status": "missing", "reason": "current_turn_missing"})
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)

    def fake_diagnose(timeout_seconds, scroll_bottom, **_kwargs):
        diagnosis_calls["count"] += 1
        return {"state": "awaiting_keep", "suggested_intervention": {"mode": "click-point", "x": 1, "y": 2}}

    monkeypatch.setattr(wait_module, "diagnose_ui", fake_diagnose)

    def fake_apply(intervention, timeout_seconds):
        interventions.append(intervention)
        return {"status": "applied"}

    monkeypatch.setattr(wait_module, "apply_intervention", fake_apply)

    with pytest.raises(wait_module.TraeAutomationError):
        wait_module.wait_completion(
            timeout_seconds=2,
            stable_seconds=0.5,
            poll_interval_seconds=0.5,
            intervention_idle_seconds=100,
            max_interventions=1,
        )

    assert interventions == [{"mode": "continue-text", "text": "\u7ee7\u7eed", "action": "continue"}]
    assert diagnosis_calls["count"] == 1


def test_wait_completion_keeps_observing_window_chrome_only_text(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "最小化\n最大化\n关闭")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(wait_module, "probe_latest_trae_turn", lambda **kwargs: {"status": "missing", "reason": "current_turn_missing"})
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(wait_module, "diagnose_ui", lambda timeout_seconds, scroll_bottom, **_kwargs: {"state": "idle_or_running"})

    with pytest.raises(wait_module.TraeAutomationError) as exc:
        wait_module.wait_completion(
            timeout_seconds=3,
            stable_seconds=0.5,
            poll_interval_seconds=0.5,
            intervention_idle_seconds=100,
            max_interventions=0,
        )

    assert "did not become stable" in str(exc.value)


def test_wait_completion_keeps_observing_restored_window_chrome_text(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "\u6700\u5c0f\u5316\n\u6062\u590d\n\u5173\u95ed")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(wait_module, "probe_latest_trae_turn", lambda **kwargs: {"status": "missing", "reason": "current_turn_missing"})
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(wait_module, "diagnose_ui", lambda timeout_seconds, scroll_bottom, **_kwargs: {"state": "idle_or_running"})

    with pytest.raises(wait_module.TraeAutomationError) as exc:
        wait_module.wait_completion(
            timeout_seconds=3,
            stable_seconds=0.5,
            poll_interval_seconds=0.5,
            intervention_idle_seconds=100,
            max_interventions=0,
        )

    assert "did not become stable" in str(exc.value)


def test_wait_completion_recovers_interrupted_turn_from_chrome_only_text(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    interventions = []
    progress = []

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "\u6700\u5c0f\u5316\n\u6062\u590d\n\u5173\u95ed")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        wait_module,
        "probe_latest_trae_turn",
        lambda **kwargs: {"status": "found", "turn_status": "interrupted", "session_id": "sid", "user_message_id": "uid"},
    )
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)

    def fake_diagnose(timeout_seconds, scroll_bottom, **_kwargs):
        assert _kwargs["recovery_reason"] == "trae_turn_not_completed:interrupted"
        return {"state": "idle_or_running", "suggested_intervention": {}}

    monkeypatch.setattr(wait_module, "diagnose_ui", fake_diagnose)

    def fake_apply(intervention, timeout_seconds, **_kwargs):
        interventions.append(intervention)
        return {"status": "applied", "mode": intervention.get("mode")}

    monkeypatch.setattr(wait_module, "apply_intervention", fake_apply)

    with pytest.raises(wait_module.TraeAutomationError):
        wait_module.wait_completion(
            timeout_seconds=2,
            stable_seconds=0.5,
            poll_interval_seconds=0.5,
            intervention_idle_seconds=30,
            max_interventions=3,
            progress_callback=progress.append,
        )

    assert interventions == [{"mode": "continue-text", "text": "\u7ee7\u7eed", "action": "continue"}]
    assert any(event.get("supervisor_action") == "recover_interrupted_turn" for event in progress)


def test_wait_completion_allows_continue_text_again_after_cooldown(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    interventions = []

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "\u6700\u5c0f\u5316\n\u6062\u590d\n\u5173\u95ed")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(wait_module.time, "time", lambda: 1200.0)
    monkeypatch.setattr(
        wait_module,
        "probe_latest_trae_turn",
        lambda **kwargs: {"status": "found", "turn_status": "interrupted", "session_id": "sid", "user_message_id": "uid"},
    )
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(
        wait_module,
        "diagnose_ui",
        lambda timeout_seconds, scroll_bottom, **_kwargs: {
            "state": "model_error_3003",
            "suggested_intervention": {"mode": "continue-text", "text": "\u7ee7\u7eed", "action": "continue"},
        },
    )

    def fake_apply(intervention, timeout_seconds, **_kwargs):
        interventions.append(intervention)
        return {"status": "applied", "mode": intervention.get("mode")}

    monkeypatch.setattr(wait_module, "apply_intervention", fake_apply)

    with pytest.raises(wait_module.TraeAutomationError):
        wait_module.wait_completion(
            timeout_seconds=2,
            stable_seconds=0.5,
            poll_interval_seconds=0.5,
            intervention_idle_seconds=30,
            max_interventions=1,
            continue_text_already_sent=True,
            continue_sent_at="1970-01-01T00:18:00+00:00",
        )

    assert interventions == [{"mode": "continue-text", "text": "\u7ee7\u7eed", "action": "continue"}]


def test_wait_completion_suppresses_recent_continue_text(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    interventions = []

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "\u6700\u5c0f\u5316\n\u6062\u590d\n\u5173\u95ed")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(wait_module.time, "time", lambda: 1200.0)
    monkeypatch.setattr(
        wait_module,
        "probe_latest_trae_turn",
        lambda **kwargs: {"status": "found", "turn_status": "interrupted", "session_id": "sid", "user_message_id": "uid"},
    )
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(
        wait_module,
        "diagnose_ui",
        lambda timeout_seconds, scroll_bottom, **_kwargs: {
            "state": "model_error_3003",
            "suggested_intervention": {"mode": "continue-text", "text": "\u7ee7\u7eed", "action": "continue"},
        },
    )

    def fake_apply(intervention, timeout_seconds, **_kwargs):
        interventions.append(intervention)
        return {"status": "applied", "mode": intervention.get("mode")}

    monkeypatch.setattr(wait_module, "apply_intervention", fake_apply)

    with pytest.raises(wait_module.TraeAutomationError):
        wait_module.wait_completion(
            timeout_seconds=2,
            stable_seconds=0.5,
            poll_interval_seconds=0.5,
            intervention_idle_seconds=30,
            max_interventions=1,
            continue_text_already_sent=True,
            continue_sent_at="1970-01-01T00:19:50+00:00",
        )

    assert interventions == []


def test_wait_completion_accepts_visible_task_complete_text(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    diagnosis_calls = {"count": 0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(
        wait_module,
        "window_text_snapshot",
        lambda window: "\u4efb\u52a1\u5b8c\u6210\n9 \u4e2a\u6587\u4ef6\u53d8\u66f4\nindex.html +193 -0\napp.js +506 -0",
    )
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        wait_module,
        "probe_latest_trae_turn",
        lambda **kwargs: {"status": "missing", "reason": "no_completed_turn_after_prompt_send"},
    )
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    def fake_diagnose(timeout_seconds, scroll_bottom, **_kwargs):
        diagnosis_calls["count"] += 1
        return {"state": "idle_or_running", "suggested_intervention": {}}

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(wait_module, "diagnose_ui", fake_diagnose)

    result = wait_module.wait_completion(
        timeout_seconds=3,
        stable_seconds=0.5,
        poll_interval_seconds=0.5,
        intervention_idle_seconds=0.5,
        max_interventions=1,
    )

    assert result["status"] == "completed"
    assert result["supervisor_decision"]["reason"] == "ui_completion_detected"
    assert diagnosis_calls["count"] == 0


def test_wait_completion_accepts_low_confidence_candidate_with_project_write(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    diagnosis_calls = {"count": 0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(
        wait_module,
        "window_text_snapshot",
        lambda window: "Changes completed. Keep changes\napp.js +120 -4",
    )
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        wait_module,
        "probe_latest_trae_turn",
        lambda **kwargs: {
            "status": "missing",
            "reason": "low_confidence_context_match",
            "candidate": {"turn_status": "completed", "session_id": "s1", "user_message_id": "u1"},
        },
    )
    monkeypatch.setattr(
        wait_module,
        "build_trae_observation",
        lambda **kwargs: {
            "activity": {"recent": True, "source": "agent_log", "quiet_seconds": 120.0},
            "project_write": {"mtime": 1000.0, "path": "D:/work/demo/app.js", "last_write": "2026-06-16T10:00:00"},
            "log": {"tail_hash": "abc123"},
        },
    )

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    def fake_diagnose(timeout_seconds, scroll_bottom, **_kwargs):
        diagnosis_calls["count"] += 1
        return {"state": "idle_or_running", "suggested_intervention": {}}

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(wait_module, "diagnose_ui", fake_diagnose)

    result = wait_module.wait_completion(
        timeout_seconds=3,
        stable_seconds=0.5,
        poll_interval_seconds=0.5,
        intervention_idle_seconds=0.5,
        max_interventions=1,
    )

    assert result["status"] == "completed"
    completion = result["supervisor_decision"]["trae_turn_completion_decision"]
    assert completion["is_complete"] is True
    assert completion["next_action"] == "copy_trace"
    assert diagnosis_calls["count"] == 0


def test_wait_completion_keeps_waiting_when_current_turn_is_pending(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds, **_kwargs: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds, **_kwargs: FakeWindow())
    monkeypatch.setattr(wait_module, "focus_trae_workspace_or_any", lambda timeout_seconds, workspace_path, **_kwargs: {"status": "focused"})
    monkeypatch.setattr(wait_module, "wait_for_workspace_window_or_any", lambda timeout_seconds, workspace_path, **_kwargs: FakeWindow())
    monkeypatch.setattr(
        wait_module,
        "window_text_snapshot",
        lambda window: "toolName: edit\nstatus: running\nfilePath: app.py\n" * 20,
    )
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        wait_module,
        "probe_latest_trae_turn",
        lambda **kwargs: {
            "status": "missing",
            "reason": "awaiting_current_continuation",
            "candidate": {"turn_status": "unknown"},
        },
    )
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(wait_module, "diagnose_ui", lambda timeout_seconds, scroll_bottom, **_kwargs: {"state": "idle_or_running"})

    with pytest.raises(wait_module.TraeAutomationError) as exc:
        wait_module.wait_completion(
            timeout_seconds=2,
            stable_seconds=0.5,
            poll_interval_seconds=0.5,
            intervention_idle_seconds=100,
            max_interventions=0,
            prompt="build feature",
            workspace_path="D:/work/current",
            sent_at_epoch=123.0,
        )

    assert "did not become stable" in str(exc.value)


def test_wait_completion_diagnoses_pending_ui_before_recent_activity_when_idle_ready(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    diagnosis_calls = {"count": 0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "\u786e\u8ba4\u6267\u884c\nTrae still working")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        wait_module,
        "probe_latest_trae_turn",
        lambda **kwargs: {"status": "missing", "reason": "no_completed_turn_after_prompt_send"},
    )
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(True))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    def fake_diagnose(timeout_seconds, scroll_bottom, **_kwargs):
        diagnosis_calls["count"] += 1
        return {"state": "awaiting_execute", "suggested_intervention": {"mode": "click-point", "x": 1, "y": 2}}

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(wait_module, "diagnose_ui", fake_diagnose)

    def fake_apply(intervention, timeout_seconds):
        return {"status": "applied"}

    monkeypatch.setattr(wait_module, "apply_intervention", fake_apply)

    with pytest.raises(wait_module.TraeAutomationError):
        wait_module.wait_completion(
            timeout_seconds=2,
            stable_seconds=0.5,
            poll_interval_seconds=0.5,
            intervention_idle_seconds=0.5,
            max_interventions=1,
        )

    assert diagnosis_calls["count"] == 1
