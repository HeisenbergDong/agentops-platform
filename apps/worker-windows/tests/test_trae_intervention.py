import pytest

from worker.trae import wait as wait_module
from worker.trae.diagnose import diagnose_ui
from worker.trae.intervene import apply_intervention


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


def test_apply_intervention_rejects_unknown_mode():
    with pytest.raises(Exception):
        apply_intervention({"mode": "unknown"})


def test_continue_text_intervention_targets_chat_prompt(monkeypatch: pytest.MonkeyPatch):
    calls = []

    monkeypatch.setattr(
        "worker.trae.intervene.send_prompt",
        lambda text, submit=True: calls.append((text, submit)) or {"input": {"method": "solo_coordinate_primary"}},
    )

    result = apply_intervention({"mode": "continue-text", "text": "\u7ee7\u7eed"})

    assert calls == [("\u7ee7\u7eed", True)]
    assert result["status"] == "applied"
    assert result["mode"] == "continue-text"
    assert result["input"]["method"] == "solo_coordinate_primary"


def test_wait_completion_runs_idle_intervention(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    interventions = []

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "Trae waiting for confirmation")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(
        wait_module,
        "diagnose_ui",
        lambda timeout_seconds, scroll_bottom: {
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


def test_wait_completion_intervenes_before_marking_stable_done(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    interventions = []
    diagnosis_calls = {"count": 0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "Trae waiting for run confirmation")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)

    def fake_diagnose(timeout_seconds, scroll_bottom):
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
        intervention_idle_seconds=100,
        max_interventions=1,
    )

    assert result["status"] == "completed"
    assert interventions == [{"mode": "click-point", "x": 1, "y": 2}]
