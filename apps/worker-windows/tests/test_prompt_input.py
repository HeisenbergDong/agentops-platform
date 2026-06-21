import pytest
from PIL import Image, ImageDraw

from worker.trae import prompt as prompt_module
from worker.trae.ui_locator import locate_prompt_targets


class FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class FakeControl:
    def __init__(self, rect: FakeRect, name: str = "prompt"):
        self._rect = rect
        self._name = name
        self.focused = False
        self.clicked = False

    def rectangle(self):
        return self._rect

    def window_text(self):
        return self._name

    def set_focus(self):
        self.focused = True

    def click_input(self):
        self.clicked = True


class FakeWindow:
    hwnd = 100

    def __init__(self, controls: list[FakeControl]):
        self.controls = controls

    def descendants(self, control_type: str):
        if control_type == "Edit":
            return self.controls
        return []


@pytest.fixture(autouse=True)
def isolate_ui_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt_module.ui_cache, "default_cache_path", lambda: tmp_path / "trae-ui-cache.json")
    monkeypatch.setattr(prompt_module, "_verify_send_button_visual", lambda *args, **kwargs: {"status": "passed"})


def test_send_prompt_clicks_solo_input_area_before_paste(monkeypatch):
    bottom_input = FakeControl(FakeRect(120, 680, 520, 730), name="Ask Trae")
    top_editor = FakeControl(FakeRect(700, 100, 1100, 700), name="Editor")
    fake_window = FakeWindow([top_editor, bottom_input])
    keys: list[str] = []
    clipboard: list[str] = []
    clicks: list[tuple[int, int]] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: clipboard.append(text))
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: keys.append(keys_))
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)

    result = prompt_module.send_prompt("  build it  ")

    assert clicks == [(312, 704), (436, 756)]
    assert bottom_input.focused is False
    assert bottom_input.clicked is False
    assert top_editor.clicked is False
    assert clipboard == ["build it"]
    assert keys == ["^a", "{BACKSPACE}", "^v"]
    assert result["input"]["method"] == "adbz_coordinate_primary"
    assert result["input"]["click_ratio"] == {"x": 0.26, "y": 0.88}
    assert result["submit"]["method"] == "adbz_send_button"
    assert result["submit"]["click_ratio"] == {"x": 0.364, "y": 0.945}


def test_send_prompt_verifies_submission_with_trae_turn_probe(monkeypatch):
    fake_window = FakeWindow([])
    keys: list[str] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: None)
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: keys.append(keys_))
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        prompt_module,
        "probe_latest_trae_turn",
        lambda **kwargs: {
            "status": "found",
            "turn_status": "running",
            "session_id": "sid",
            "user_message_id": "mid",
            "probe_scope": "workspace_sessions",
        },
    )

    result = prompt_module.send_prompt(
        "build it",
        workspace_path="D:/code-space/project",
        verify_submission=True,
        sent_at_epoch=123.0,
    )

    assert keys == ["^a", "{BACKSPACE}", "^v"]
    assert result["submission"]["status"] == "confirmed"
    assert result["submission"]["probe"]["status"] == "found"


def test_send_prompt_can_open_new_task_before_paste(monkeypatch):
    fake_window = FakeWindow([])
    keys: list[str] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: None)
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: keys.append(keys_))
    monkeypatch.setattr(
        prompt_module,
        "_wait_for_composer_ready",
        lambda **kwargs: {"status": "ready"},
    )
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)

    result = prompt_module.send_prompt("build it", open_new_task=True)

    assert keys[:4] == ["^%n", "^a", "{BACKSPACE}", "^v"]
    assert result["automation"]["new_task"] == {"status": "sent", "method": "ctrl_alt_n"}


def test_send_prompt_waits_for_composer_ready_before_paste(monkeypatch):
    fake_window = FakeWindow([])
    keys: list[str] = []
    ready_calls: list[dict] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: None)
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: keys.append(keys_))
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)

    def fake_ready(**kwargs):
        ready_calls.append(kwargs)
        return {
            "status": "ready",
            "target_set": {
                "source": "composer_ready",
                "input": {
                    "action": "prompt_input",
                    "center": {"x": 260, "y": 710},
                    "ratio": {"x": 0.2167, "y": 0.8875},
                    "confidence": 0.82,
                    "risk": "safe",
                    "method": "local_vision",
                },
                "send": {
                    "action": "send_button",
                    "center": {"x": 430, "y": 755},
                    "ratio": {"x": 0.3583, "y": 0.9438},
                    "confidence": 0.86,
                    "risk": "safe",
                    "method": "local_vision",
                },
            },
        }

    monkeypatch.setattr(prompt_module, "_wait_for_composer_ready", fake_ready)

    result = prompt_module.send_prompt("build it", open_new_task=True)

    assert len(ready_calls) == 1
    assert keys == ["^%n", "^a", "{BACKSPACE}", "^v"]
    assert result["automation"]["strategy"] == "composer_ready"
    assert result["automation"]["composer_ready"]["status"] == "ready"
    assert result["input"]["click_x"] == 260
    assert result["submit"]["click_x"] == 430


def test_send_prompt_falls_back_when_workspace_title_is_missing(monkeypatch):
    fake_window = FakeWindow([])
    focus_calls: list[bool] = []

    def fake_focus(**kwargs):
        focus_calls.append(bool(kwargs.get("require_workspace_match")))
        if kwargs.get("require_workspace_match"):
            raise prompt_module.TraeAutomationError("workspace title not found")
        return {
            "status": "focused",
            "window_title": "Trae CN",
            "workspace_match": False,
        }

    monkeypatch.setattr(prompt_module, "focus_trae", fake_focus)
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: None)
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: None)
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)

    result = prompt_module.send_prompt("build it", workspace_path="D:/work/project", submit=False)

    assert focus_calls == [True, False]
    assert result["window_title"] == "Trae CN"
    assert result["workspace_match"] is False


def test_send_prompt_rejects_unconfirmed_submission_by_default(monkeypatch):
    fake_window = FakeWindow([])

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: None)
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: None)
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        prompt_module,
        "_verify_prompt_submission",
        lambda **kwargs: (_ for _ in ()).throw(
            prompt_module.PromptSendError(
                "Prompt was pasted/submitted, but no new Trae user turn was detected.",
                {"submission_probe": {"status": "missing", "reason": "no_completed_turn_after_prompt_send"}},
            )
        ),
    )

    with pytest.raises(prompt_module.PromptSendError):
        prompt_module.send_prompt(
            "build it",
            verify_submission=True,
        )


def test_send_prompt_can_continue_when_submission_probe_is_explicitly_non_strict(monkeypatch):
    fake_window = FakeWindow([])

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: None)
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: None)
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        prompt_module,
        "_verify_prompt_submission",
        lambda **kwargs: (_ for _ in ()).throw(
            prompt_module.PromptSendError(
                "Prompt was pasted/submitted, but no new Trae user turn was detected.",
                {"submission_probe": {"status": "missing", "reason": "no_completed_turn_after_prompt_send"}},
            )
        ),
    )

    result = prompt_module.send_prompt(
        "build it",
        verify_submission=True,
        strict_submission_verification=False,
    )

    assert result["status"] == "sent"
    assert result["submission"]["status"] == "unconfirmed"
    assert result["automation"]["submission_verified"] is False


def test_send_prompt_does_not_click_send_again_after_unverified_click(monkeypatch, tmp_path):
    fake_window = FakeWindow([])
    clicks: list[tuple[int, int]] = []
    probe_calls = {"count": 0}
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"png")

    monkeypatch.setattr(prompt_module.ui_cache, "default_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: None)
    monkeypatch.setattr(
        prompt_module,
        "_capture_ui_analysis_screenshot",
        lambda **kwargs: {"status": "captured", "path": str(screenshot)},
    )
    monkeypatch.setattr(
        prompt_module,
        "locate_prompt_targets",
        lambda path, rect: {"status": "not_found", "targets": []},
    )
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)

    def fake_verify(**kwargs):
        probe_calls["count"] += 1
        if probe_calls["count"] == 1:
            raise prompt_module.PromptSendError("no new Trae user turn was detected")
        return {"status": "confirmed", "probe": {"status": "found"}}

    def fake_ai(path, context):
        return {
            "analysis": {
                "status": "found",
                "targets": [
                    {
                        "action": "prompt_input",
                        "center": {"x": 240, "y": 700},
                        "ratio": {"x": 0.2, "y": 0.875},
                        "confidence": 0.92,
                        "risk": "safe",
                    },
                    {
                        "action": "send_button",
                        "center": {"x": 500, "y": 760},
                        "ratio": {"x": 0.417, "y": 0.95},
                        "confidence": 0.94,
                        "risk": "safe",
                    },
                ],
            }
        }

    monkeypatch.setattr(prompt_module, "_verify_prompt_submission", fake_verify)

    with pytest.raises(prompt_module.PromptSendError):
        prompt_module.send_prompt(
            "build it",
            verify_submission=True,
            strict_submission_verification=True,
            submission_timeout_seconds=0.5,
            ui_analyst=fake_ai,
        )

    assert clicks == [(312, 704), (436, 756)]
    assert probe_calls["count"] == 1


def test_send_prompt_uses_ai_vision_when_default_target_fails_before_click(monkeypatch, tmp_path):
    fake_window = FakeWindow([])
    clicks: list[tuple[int, int]] = []
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"png")

    monkeypatch.setattr(prompt_module.ui_cache, "default_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: None)
    monkeypatch.setattr(
        prompt_module,
        "_capture_ui_analysis_screenshot",
        lambda **kwargs: {"status": "captured", "path": str(screenshot)},
    )
    monkeypatch.setattr(
        prompt_module,
        "locate_prompt_targets",
        lambda path, rect: {"status": "not_found", "targets": []},
    )
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(prompt_module, "_verify_send_button_visual", lambda *args, **kwargs: {"status": "failed", "reason": "not_send_button"})
    monkeypatch.setattr(prompt_module, "_verify_prompt_submission", lambda **kwargs: {"status": "confirmed", "probe": {"status": "found"}})

    def fake_ai(path, context):
        return {
            "analysis": {
                "status": "found",
                "targets": [
                    {
                        "action": "prompt_input",
                        "center": {"x": 240, "y": 700},
                        "ratio": {"x": 0.2, "y": 0.875},
                        "confidence": 0.92,
                        "risk": "safe",
                    },
                    {
                        "action": "send_button",
                        "center": {"x": 500, "y": 760},
                        "ratio": {"x": 0.417, "y": 0.95},
                        "confidence": 0.94,
                        "risk": "safe",
                    },
                ],
            }
        }

    def verify_guard(target, *args, **kwargs):
        if target.get("center", {}).get("x") == 436:
            return {"status": "failed", "reason": "not_send_button"}
        return {"status": "passed"}

    monkeypatch.setattr(prompt_module, "_verify_send_button_visual", verify_guard)

    result = prompt_module.send_prompt(
        "build it",
        verify_submission=True,
        strict_submission_verification=True,
        submission_timeout_seconds=0.5,
        ui_analyst=fake_ai,
    )

    assert clicks == [(312, 704), (240, 700), (500, 760)]
    assert result["automation"]["strategy"] == "ai_vision"
    assert result["input"]["click_x"] == 240
    assert result["submit"]["click_x"] == 500


def test_send_prompt_rejects_voice_button_as_ai_send_target(monkeypatch):
    fake_window = FakeWindow([])
    clicks: list[tuple[int, int]] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: None)
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(prompt_module, "_verify_prompt_submission", lambda **kwargs: (_ for _ in ()).throw(prompt_module.PromptSendError("not sent")))
    monkeypatch.setattr(
        prompt_module,
        "_capture_ui_analysis_screenshot",
        lambda workspace_path=None: {
            "path": "trae.png",
            "status": "captured",
            "capture": {"bounds": {"left": 0, "top": 0, "right": 1200, "bottom": 800, "width": 1200, "height": 800}},
        },
    )
    monkeypatch.setattr(prompt_module, "locate_prompt_targets", lambda path, rect: {"status": "not_found", "targets": []})

    def fake_ai(path, context):
        return {
            "analysis": {
                "status": "found",
                "targets": [
                    {
                        "action": "prompt_input",
                        "label": "input",
                        "center": {"x": 240, "y": 700},
                        "ratio": {"x": 0.2, "y": 0.875},
                        "confidence": 0.92,
                        "risk": "safe",
                    },
                    {
                        "action": "send_button",
                        "label": "voice microphone",
                        "center": {"x": 500, "y": 760},
                        "ratio": {"x": 0.417, "y": 0.95},
                        "confidence": 0.94,
                        "risk": "safe",
                    },
                ],
            }
        }

    with pytest.raises(prompt_module.PromptSendError) as exc_info:
        prompt_module.send_prompt(
            "build it",
            verify_submission=True,
            strict_submission_verification=True,
            submission_timeout_seconds=0.5,
            ui_analyst=fake_ai,
        )

    assert "not sent" in str(exc_info.value)
    assert (500, 760) not in clicks
    assert clicks == [(312, 704), (436, 756)]


def test_local_vision_finds_muted_green_send_button(tmp_path):
    path = tmp_path / "trae-composer.png"
    image = Image.new("RGB", (1800, 1033), (28, 29, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle((270, 830, 735, 1015), fill=(42, 43, 48), outline=(65, 66, 72))
    draw.rectangle((680, 950, 718, 982), fill=(36, 74, 57))
    image.save(path)

    result = locate_prompt_targets(path, (0, 0, 1800, 1033))

    actions = {item["action"]: item for item in result["targets"]}
    assert result["status"] == "found"
    assert "prompt_input" in actions
    assert "send_button" in actions
    assert 660 <= actions["send_button"]["center"]["x"] <= 730
    assert 940 <= actions["send_button"]["center"]["y"] <= 990


def test_local_vision_does_not_treat_loading_screen_as_prompt_input(tmp_path):
    path = tmp_path / "trae-loading.png"
    image = Image.new("RGB", (1938, 1048), (28, 29, 30))
    draw = ImageDraw.Draw(image)
    draw.text((500, 560), "Loading...", fill=(245, 245, 245))
    image.save(path)

    result = locate_prompt_targets(path, (0, 0, 1938, 1048))

    assert result["status"] == "not_found"
    assert result["targets"] == []


def test_send_prompt_does_not_click_send_button_when_submit_false(monkeypatch):
    fake_window = FakeWindow([])
    clicks: list[tuple[int, int]] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: None)
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)

    result = prompt_module.send_prompt("build it", submit=False)

    assert clicks == [(312, 704)]
    assert result["submitted"] is False
    assert result["submit"] == {}


def test_send_prompt_fails_when_submission_probe_never_sees_turn(monkeypatch):
    fake_window = FakeWindow([])

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: None)
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: None)
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)
    now = {"value": 100.0}
    monkeypatch.setattr(prompt_module.time, "monotonic", lambda: now.__setitem__("value", now["value"] + 1.0) or now["value"])
    monkeypatch.setattr(
        prompt_module,
        "probe_latest_trae_turn",
        lambda **kwargs: {"status": "missing", "reason": "no_completed_turn_after_prompt_send"},
    )

    try:
        prompt_module.send_prompt(
            "build it",
            workspace_path="D:/code-space/project",
            verify_submission=True,
            sent_at_epoch=123.0,
            submission_timeout_seconds=0.5,
        )
    except prompt_module.PromptSendError as exc:
        assert "no new Trae user turn was detected" in str(exc)
    else:
        raise AssertionError("PromptSendError was not raised")


def test_send_prompt_uses_uia_candidate_when_window_bounds_missing(monkeypatch):
    bottom_input = FakeControl(FakeRect(120, 680, 520, 730), name="Ask Trae")
    fake_window = FakeWindow([bottom_input])
    keys: list[str] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: None)
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: keys.append(keys_))
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)

    result = prompt_module.send_prompt("build it", submit=False)

    assert bottom_input.focused is True
    assert bottom_input.clicked is True
    assert keys == ["^a", "{BACKSPACE}", "^v"]
    assert result["input"]["method"] == "uia_candidate"


def test_prompt_input_candidates_reject_editor_and_right_sidebar():
    left_bottom = FakeControl(FakeRect(270, 812, 729, 1004), name="SOLO Agent input")
    center_editor = FakeControl(FakeRect(753, 50, 1594, 1000), name="README.md")
    right_sidebar = FakeControl(FakeRect(1600, 50, 1916, 1000), name="资源管理器")
    fake_window = FakeWindow([center_editor, right_sidebar, left_bottom])

    candidates = prompt_module._prompt_input_candidates(fake_window, (0, 0, 1920, 1030))

    assert [item["name"] for item in candidates] == ["SOLO Agent input"]
    assert candidates[0]["x_ratio"] < 0.4
    assert candidates[0]["y_ratio"] > 0.7


def test_send_prompt_falls_back_to_legacy_coordinate_without_uia(monkeypatch):
    fake_window = FakeWindow([])
    clicks: list[tuple[int, int]] = []
    keys: list[str] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "wait_for_workspace_window_or_any", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: None)
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: keys.append(keys_))
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)

    try:
        prompt_module.send_prompt("build it", submit=False)
    except prompt_module.PromptSendError as exc:
        assert "no window bounds" in str(exc)
    else:
        raise AssertionError("PromptSendError was not raised")

    assert clicks == []
    assert keys == []
