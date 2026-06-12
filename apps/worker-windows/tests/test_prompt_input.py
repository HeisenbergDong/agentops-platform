from worker.trae import prompt as prompt_module


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


def test_send_prompt_clicks_solo_input_area_before_paste(monkeypatch):
    bottom_input = FakeControl(FakeRect(120, 680, 520, 730), name="Ask Trae")
    top_editor = FakeControl(FakeRect(700, 100, 1100, 700), name="Editor")
    fake_window = FakeWindow([top_editor, bottom_input])
    keys: list[str] = []
    clipboard: list[str] = []
    clicks: list[tuple[int, int]] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "find_trae_window", lambda **kwargs: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: clipboard.append(text))
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: keys.append(keys_))
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)

    result = prompt_module.send_prompt("  build it  ")

    assert clicks == [(312, 716)]
    assert bottom_input.focused is False
    assert bottom_input.clicked is False
    assert top_editor.clicked is False
    assert clipboard == ["build it"]
    assert keys == ["^a", "{BACKSPACE}", "^v", "{ENTER}"]
    assert result["input"]["method"] == "solo_coordinate_primary"
    assert result["input"]["click_ratio"] == {"x": 0.26, "y": 0.895}


def test_send_prompt_verifies_submission_with_trae_turn_probe(monkeypatch):
    fake_window = FakeWindow([])
    keys: list[str] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "find_trae_window", lambda **kwargs: fake_window)
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

    assert keys == ["^a", "{BACKSPACE}", "^v", "{ENTER}"]
    assert result["submission"]["status"] == "confirmed"
    assert result["submission"]["probe"]["status"] == "found"


def test_send_prompt_fails_when_submission_probe_never_sees_turn(monkeypatch):
    fake_window = FakeWindow([])

    monkeypatch.setattr(prompt_module, "focus_trae", lambda **kwargs: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "find_trae_window", lambda **kwargs: fake_window)
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
    monkeypatch.setattr(prompt_module, "find_trae_window", lambda **kwargs: fake_window)
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
    monkeypatch.setattr(prompt_module, "find_trae_window", lambda **kwargs: fake_window)
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
