from worker.trae.supervisor import SupervisorObservation, decide_next_action


def test_supervisor_collects_trace_when_turn_completed_despite_keep_bar():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="\u4efb\u52a1\u5b8c\u6210\n\u53d8\u66f4\u5df2\u5b8c\u6210\uff0c\u8bf7\u786e\u8ba4\u662f\u5426\u91c7\u7eb3\n\u4fdd\u7559 Ctrl+Enter",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={"status": "found", "turn_status": "completed", "session_id": "s1", "user_message_id": "u1"},
            idle_seconds=1,
            intervention_idle_seconds=300,
            max_interventions=3,
        )
    )

    assert decision["action"] == "collect_trace"
    assert decision["completion_gate"]["pending_intervention_visible"] is True


def test_supervisor_recovers_3003_before_visible_keep_button_when_turn_not_complete():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="\u6a21\u578b\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002(3003)\n\u4fdd\u7559 Ctrl+Enter",
            output_probe={"reason": "service_interrupted"},
            turn_probe={"status": "missing", "reason": "current_turn_missing"},
            idle_seconds=1,
            intervention_idle_seconds=300,
            max_interventions=3,
        )
    )

    assert decision["action"] == "recover_service_interruption"
    assert decision["reason"] == "service_interrupted"


def test_supervisor_applies_pending_ui_only_when_turn_is_not_completed():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="\u786e\u8ba4\u6267\u884c\nTrae waiting for run confirmation",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={"status": "missing", "reason": "no_completed_turn_after_prompt_send"},
            idle_seconds=31,
            intervention_idle_seconds=30,
            max_interventions=3,
        )
    )

    assert decision["action"] == "apply_pending_ui"
    assert decision["completion_gate"]["reason"] == "pending_intervention_visible"


def test_supervisor_pending_ui_beats_recent_activity_when_idle_ready():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="\u786e\u8ba4\u6267\u884c\nTrae waiting for run confirmation",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={"status": "missing", "reason": "no_completed_turn_after_prompt_send"},
            idle_seconds=31,
            intervention_idle_seconds=30,
            max_interventions=3,
            recent_activity=True,
            activity_source="agent_log",
            activity_quiet_seconds=2.4,
            log_tail_hash="abc123",
        )
    )

    assert decision["action"] == "apply_pending_ui"
    assert decision["reason"] == "pending_intervention_visible"
    assert decision["activity_summary"]["recent"] is True
    assert decision["activity_summary"]["source"] == "agent_log"


def test_supervisor_waits_on_pending_ui_until_idle_ready_even_with_recent_activity():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="\u786e\u8ba4\u6267\u884c\nTrae waiting for run confirmation",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={"status": "missing", "reason": "no_completed_turn_after_prompt_send"},
            idle_seconds=15,
            intervention_idle_seconds=30,
            max_interventions=3,
            recent_activity=True,
            activity_source="agent_log",
            activity_quiet_seconds=2.4,
            log_tail_hash="abc123",
        )
    )

    assert decision["action"] == "wait"
    assert decision["reason"] == "pending_intervention_visible"


def test_supervisor_recovers_3003_even_with_recent_activity():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="\u6a21\u578b\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002(3003)\n\u786e\u8ba4\u6267\u884c",
            output_probe={"reason": "service_interrupted"},
            turn_probe={"status": "missing", "reason": "current_turn_missing"},
            idle_seconds=5,
            intervention_idle_seconds=300,
            max_interventions=3,
            recent_activity=True,
            activity_source="project",
            activity_quiet_seconds=1.1,
        )
    )

    assert decision["action"] == "recover_service_interruption"
    assert decision["reason"] == "service_interrupted"


def test_supervisor_waits_for_slow_first_round_before_idle_diagnosis():
    observation = SupervisorObservation(
        latest_text="Trae is quiet but has no completed turn yet",
        output_probe={"reason": "missing_tool_trace_markers"},
        turn_probe={"status": "missing", "reason": "no_completed_turn_after_prompt_send"},
            idle_seconds=29,
            intervention_idle_seconds=30,
            max_interventions=3,
        )

    decision = decide_next_action(observation)

    assert decision["action"] == "wait"
    assert decision["reason"] == "no_completed_turn_after_prompt_send"


def test_supervisor_diagnoses_idle_after_configured_quiet_period():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="Trae is quiet and no completed turn arrived",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={"status": "missing", "reason": "no_completed_turn_after_prompt_send"},
            idle_seconds=31,
            intervention_idle_seconds=30,
            max_interventions=3,
        )
    )

    assert decision["action"] == "diagnose_idle"
    assert decision["reason"] == "no_completed_turn_after_prompt_send"


def test_supervisor_collects_trace_when_turn_completed_even_if_uia_reads_chrome_only():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="\u6700\u5c0f\u5316\n\u6062\u590d\n\u5173\u95ed",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={"status": "found", "turn_status": "completed", "session_id": "s1", "user_message_id": "u1"},
            window_chrome_only=True,
            idle_seconds=1,
            intervention_idle_seconds=300,
            max_interventions=3,
        )
    )

    assert decision["action"] == "collect_trace"
    assert decision["reason"] == "trae_turn_completed"


def test_supervisor_waits_on_chrome_only_when_current_turn_not_confirmed():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="\u6700\u5c0f\u5316\n\u6062\u590d\n\u5173\u95ed",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={"status": "missing", "reason": "current_turn_missing"},
            window_chrome_only=True,
            idle_seconds=15,
            intervention_idle_seconds=30,
            max_interventions=3,
        )
    )

    assert decision["action"] == "wait"
    assert decision["reason"] == "window_chrome_only"


def test_supervisor_diagnoses_chrome_only_after_idle_threshold():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="\u6700\u5c0f\u5316\n\u6062\u590d\n\u5173\u95ed",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={"status": "missing", "reason": "awaiting_current_continuation"},
            window_chrome_only=True,
            idle_seconds=31,
            intervention_idle_seconds=30,
            max_interventions=3,
        )
    )

    assert decision["action"] == "diagnose_idle"
    assert decision["reason"] == "window_chrome_only"


def test_supervisor_collects_trace_from_visible_task_complete_text():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="\u4efb\u52a1\u5b8c\u6210\n9 \u4e2a\u6587\u4ef6\u53d8\u66f4\nindex.html +193 -0",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={"status": "missing", "reason": "no_completed_turn_after_prompt_send"},
            idle_seconds=31,
            intervention_idle_seconds=30,
            max_interventions=3,
        )
    )

    assert decision["action"] == "collect_trace"
    assert decision["reason"] == "ui_completion_detected"
    assert decision["trae_turn_completion_decision"]["next_action"] == "copy_trace"


def test_supervisor_collects_trace_from_trae_code_change_completion_view():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text=(
                "任务完成\n"
                "代码变更\n"
                "67 个文件变更 +5675 -21\n"
                "build.bat +28 -0\n"
                "JobController.java +46 -0"
            ),
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={"status": "missing", "reason": "no_completed_turn_after_prompt_send"},
            idle_seconds=90,
            intervention_idle_seconds=30,
            max_interventions=3,
            watcher_observation={
                "project_write": {"mtime": 1000.0, "path": "D:/work/permission-system/build.bat"},
                "activity": {"recent": False, "quiet_seconds": 90.0, "source": "project"},
            },
        )
    )

    assert decision["action"] == "collect_trace"
    assert decision["reason"] == "ui_completion_detected"
    completion = decision["trae_turn_completion_decision"]
    assert completion["is_complete"] is True
    assert "ui_completion_visible" in completion["evidence"]
    assert "project_write_detected" in completion["evidence"]


def test_supervisor_collects_trace_from_low_confidence_completed_candidate_with_project_write():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="Changes completed. Keep changes\napp.js +120 -4",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={
                "status": "missing",
                "reason": "low_confidence_context_match",
                "candidate": {"turn_status": "completed", "session_id": "s1", "user_message_id": "u1"},
            },
            idle_seconds=180,
            intervention_idle_seconds=90,
            max_interventions=3,
            recent_activity=False,
            watcher_observation={
                "project_write": {"mtime": 1000.0, "path": "D:/work/demo/app.js", "last_write": "2026-06-16T10:00:00"},
                "activity": {"recent": False, "quiet_seconds": 180.0, "source": "project"},
            },
        )
    )

    assert decision["action"] == "collect_trace"
    assert decision["reason"] in {"ui_completion_detected", "completion_candidate_with_project_write"}
    completion = decision["trae_turn_completion_decision"]
    assert completion["is_complete"] is True
    assert "completed_turn_candidate" in completion["evidence"]
    assert "project_write_detected" in completion["evidence"]


def test_supervisor_does_not_collect_trace_from_completed_candidate_while_recent_activity():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="app.js changed",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={
                "status": "missing",
                "reason": "low_confidence_context_match",
                "candidate": {"turn_status": "completed"},
            },
            idle_seconds=5,
            intervention_idle_seconds=90,
            max_interventions=3,
            recent_activity=True,
            activity_source="agent_log",
            activity_quiet_seconds=2.0,
            watcher_observation={
                "project_write": {"mtime": 1000.0, "path": "D:/work/demo/app.js"},
                "activity": {"recent": True, "quiet_seconds": 2.0, "source": "agent_log"},
            },
        )
    )

    assert decision["action"] == "wait"
    assert decision["reason"] == "recent_trae_activity"
    assert decision["trae_turn_completion_decision"]["is_complete"] is False


def test_supervisor_collects_low_confidence_completed_candidate_after_quiet_heartbeat_activity():
    decision = decide_next_action(
        SupervisorObservation(
            latest_text="",
            output_probe={"reason": "missing_tool_trace_markers"},
            turn_probe={
                "status": "missing",
                "reason": "low_confidence_context_match",
                "candidate": {"turn_status": "completed", "session_id": "s1", "user_message_id": "u1"},
            },
            idle_seconds=390,
            intervention_idle_seconds=30,
            max_interventions=3,
            recent_activity=True,
            activity_source="agent_log",
            activity_quiet_seconds=383.0,
            watcher_observation={
                "project_write": {"mtime": 1000.0, "path": "D:/work/demo/index.html", "last_write": "2026-06-18T02:06:47"},
                "activity": {"recent": True, "quiet_seconds": 383.0, "source": "agent_log"},
            },
        )
    )

    assert decision["action"] == "collect_trace"
    assert decision["reason"] == "completion_candidate_with_project_write"
    completion = decision["trae_turn_completion_decision"]
    assert completion["is_complete"] is True
    assert "activity_quiet_long_enough" in completion["evidence"]
    assert "completed_turn_candidate" in completion["evidence"]
