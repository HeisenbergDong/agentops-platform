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


def test_send_prompt_clicks_bottom_input_before_paste(monkeypatch):
    bottom_input = FakeControl(FakeRect(120, 680, 520, 730), name="Ask Trae")
    top_editor = FakeControl(FakeRect(700, 100, 1100, 700), name="Editor")
    fake_window = FakeWindow([top_editor, bottom_input])
    keys: list[str] = []
    clipboard: list[str] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "find_trae_window", lambda timeout_seconds=3.0: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (0, 0, 1200, 800))
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: clipboard.append(text))
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: keys.append(keys_))
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)

    result = prompt_module.send_prompt("  build it  ")

    assert bottom_input.focused is True
    assert bottom_input.clicked is True
    assert top_editor.clicked is False
    assert clipboard == ["build it"]
    assert keys == ["^a", "{BACKSPACE}", "^v", "{ENTER}"]
    assert result["input"]["method"] == "uia_candidate"


def test_send_prompt_falls_back_to_legacy_coordinate(monkeypatch):
    fake_window = FakeWindow([])
    clicks: list[tuple[int, int]] = []
    keys: list[str] = []

    monkeypatch.setattr(prompt_module, "focus_trae", lambda: {"status": "focused", "window_title": "Trae CN"})
    monkeypatch.setattr(prompt_module, "find_trae_window", lambda timeout_seconds=3.0: fake_window)
    monkeypatch.setattr(prompt_module, "_window_rect", lambda hwnd: (100, 100, 1100, 900))
    monkeypatch.setattr(prompt_module, "_mouse_click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(prompt_module, "set_clipboard_text", lambda text: None)
    monkeypatch.setattr(prompt_module, "_send_keys", lambda keys_: keys.append(keys_))
    monkeypatch.setattr(prompt_module.time, "sleep", lambda seconds: None)

    result = prompt_module.send_prompt("build it", submit=False)

    assert clicks == [(360, 804)]
    assert keys == ["^a", "{BACKSPACE}", "^v"]
    assert result["input"]["method"] == "coordinate_fallback"
    assert result["input"]["click_ratio"] == {"x": 0.26, "y": 0.88}
