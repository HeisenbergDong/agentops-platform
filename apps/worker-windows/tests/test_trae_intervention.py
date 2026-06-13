import pytest

from worker.trae import wait as wait_module
from worker.trae.diagnose import diagnose_ui
from worker.trae.intervene import apply_intervention, click_continue


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
        lambda text, submit=True: calls.append((text, submit)) or {"input": {"method": "adbz_coordinate_primary"}},
    )

    result = apply_intervention({"mode": "continue-text", "text": "\u7ee7\u7eed"})

    assert calls == [("\u7ee7\u7eed", True)]
    assert result["status"] == "applied"
    assert result["mode"] == "continue-text"
    assert result["input"]["method"] == "adbz_coordinate_primary"


def test_click_continue_reports_typed_continue_when_no_button(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        def descendants(self, control_type):
            return []

    monkeypatch.setattr("worker.trae.intervene.focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(
        "worker.trae.intervene.diagnose_ui",
        lambda timeout_seconds, scroll_bottom: {
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
        lambda timeout_seconds, scroll_bottom: {
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


def test_wait_completion_runs_idle_intervention(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    interventions = []

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
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


def test_wait_completion_intervenes_on_pending_ui_before_local_turn_completes(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    interventions = []
    diagnosis_calls = {"count": 0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
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


def test_wait_completion_accepts_completed_turn_with_pending_keep_text(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}
    diagnosis_calls = {"count": 0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
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

    def fake_diagnose(timeout_seconds, scroll_bottom):
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

    def fake_diagnose(timeout_seconds, scroll_bottom):
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


def test_wait_completion_rejects_window_chrome_only_text(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "最小化\n最大化\n关闭")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(wait_module, "diagnose_ui", lambda timeout_seconds, scroll_bottom: {"state": "idle_or_running"})

    with pytest.raises(wait_module.TraeAutomationError) as exc:
        wait_module.wait_completion(
            timeout_seconds=3,
            stable_seconds=0.5,
            poll_interval_seconds=0.5,
            intervention_idle_seconds=100,
            max_interventions=0,
        )

    assert "only window chrome text" in str(exc.value)


def test_wait_completion_rejects_restored_window_chrome_text(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
    monkeypatch.setattr(wait_module, "window_text_snapshot", lambda window: "\u6700\u5c0f\u5316\n\u6062\u590d\n\u5173\u95ed")
    monkeypatch.setattr(wait_module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(wait_module, "build_trae_observation", lambda **kwargs: _watcher_observation(False))

    def fake_sleep(seconds, cancellation_check):
        now["value"] += max(seconds, 0.1)

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(wait_module, "diagnose_ui", lambda timeout_seconds, scroll_bottom: {"state": "idle_or_running"})

    with pytest.raises(wait_module.TraeAutomationError) as exc:
        wait_module.wait_completion(
            timeout_seconds=3,
            stable_seconds=0.5,
            poll_interval_seconds=0.5,
            intervention_idle_seconds=100,
            max_interventions=0,
        )

    assert "only window chrome text" in str(exc.value)


def test_wait_completion_keeps_waiting_when_current_turn_is_pending(monkeypatch: pytest.MonkeyPatch):
    class FakeWindow:
        pass

    now = {"value": 100.0}

    monkeypatch.setattr(wait_module, "focus_trae", lambda timeout_seconds: {"status": "focused"})
    monkeypatch.setattr(wait_module, "find_trae_window", lambda timeout_seconds: FakeWindow())
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
    monkeypatch.setattr(wait_module, "diagnose_ui", lambda timeout_seconds, scroll_bottom: {"state": "idle_or_running"})

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


def test_wait_completion_does_not_diagnose_pending_ui_while_recent_activity(monkeypatch: pytest.MonkeyPatch):
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

    def fake_diagnose(timeout_seconds, scroll_bottom):
        diagnosis_calls["count"] += 1
        return {"state": "awaiting_execute", "suggested_intervention": {"mode": "click-point", "x": 1, "y": 2}}

    monkeypatch.setattr(wait_module, "_sleep_with_cancellation", fake_sleep)
    monkeypatch.setattr(wait_module, "diagnose_ui", fake_diagnose)

    with pytest.raises(wait_module.TraeAutomationError):
        wait_module.wait_completion(
            timeout_seconds=2,
            stable_seconds=0.5,
            poll_interval_seconds=0.5,
            intervention_idle_seconds=100,
            max_interventions=1,
        )

    assert diagnosis_calls["count"] == 0
