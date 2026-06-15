from pathlib import Path
import tempfile

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Attachment, AutomationError, Job, Project, RuntimeLog, TaskRound, UserConfig, WorkerCommand
from app.db.session import Base
from app.services.orchestrator.states import JobState
from app.services.orchestrator import worker_results
from app.services.orchestrator.events import build_display_message
from app.services.orchestrator.worker_results import handle_worker_result
from app.services import webhook_notifier
from app.worker_gateway.contracts import WorkerCommandType, WorkerResult

VALID_TRAE_SESSION_ID = "41867a07a0b34471bd185ecc93ebf73b"
VALID_TRAE_USER_MESSAGE_ID = "6a26227be4db5ceccde3e54e"
VALID_TRAE_TASK_ID = "7a26227be4db5ceccde3e54f"
VALID_TRAE_TRACE_ID = "8f0bce1835c9654e637c14de711bd35b"


def test_send_prompt_success_advances_job_to_waiting_trae():
    db = _test_session()
    job, round_, command = _create_send_prompt_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(command_id=command.id, worker_id=command.worker_id, status="success", data={"chars": 10}),
    )

    db.refresh(job)
    db.refresh(round_)
    logs = list(db.scalars(select(RuntimeLog).order_by(RuntimeLog.created_at)).all())
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.WAIT_COMPLETION.value))
    assert job.status == JobState.WAITING_TRAE
    assert round_.status == JobState.WAITING_TRAE
    assert next_command is not None
    assert next_command.status == "queued"
    assert next_command.payload["intervention_idle_seconds"] == worker_results.FIRST_ROUND_INTERVENTION_IDLE_SECONDS
    assert next_command.payload["max_interventions"] == 3
    assert [item.stage for item in logs] == [JobState.PROMPT_SENT, JobState.WAITING_TRAE, JobState.WAITING_TRAE]
    assert logs[0].display_message == "Worker 已把提示词输入 Trae CN 并发送。"


def test_send_prompt_unconfirmed_result_marks_manual_required():
    db = _test_session()
    job, round_, command = _create_send_prompt_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "chars": 10,
                "submission": {
                    "status": "unconfirmed",
                    "error": "Prompt was pasted/submitted, but no new Trae user turn was detected.",
                },
                "automation": {"submission_verified": False},
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.WAIT_COMPLETION.value))
    log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == JobState.MANUAL_REQUIRED))
    assert job.status == JobState.MANUAL_REQUIRED
    assert round_.status == JobState.MANUAL_REQUIRED
    assert next_command is None
    assert log is not None
    assert log.extra["unconfirmed_reason"] == "Prompt was pasted/submitted, but no new Trae user turn was detected."


def test_full_worker_result_happy_path_reaches_project_completed(monkeypatch, tmp_path):
    db = _test_session()
    job, round_, command = _create_send_prompt_rows(db)
    round_.round_index = 2
    job.submitted_count = 4
    job.satisfied_count = 0
    command.payload = {
        "prompt": "demo",
        "browser_url": "http://localhost:5173",
        "github_push": False,
    }
    _create_feishu_config(db, job.user_id)
    monkeypatch.setattr(worker_results.settings, "attachment_root", tmp_path / "storage")
    monkeypatch.setattr(worker_results, "write_feishu_record", lambda feishu_config, fields: {"status": "written", "record_id": "rec1"})

    _finish(
        db,
        command,
        WorkerResult(command_id=command.id, worker_id=command.worker_id, status="success", data={"chars": 10}),
    )
    wait = _latest_command(db, WorkerCommandType.WAIT_COMPLETION)
    assert wait.payload["intervention_idle_seconds"] == worker_results.FOLLOWUP_ROUND_INTERVENTION_IDLE_SECONDS
    _finish(db, wait, WorkerResult(command_id=wait.id, worker_id=wait.worker_id, status="success", data={"text_chars": 1000}))
    copy = _latest_command(db, WorkerCommandType.COPY_LATEST_REPLY)
    _finish(
        db,
        copy,
        WorkerResult(
            command_id=copy.id,
            worker_id=copy.worker_id,
            status="success",
            data={"raw_text": _valid_trace(), "trae_turn": _valid_trae_turn()},
        ),
    )
    screenshot = _latest_command(db, WorkerCommandType.CAPTURE_SCREENSHOT)
    _finish(
        db,
        screenshot,
        WorkerResult(
            command_id=screenshot.id,
            worker_id=screenshot.worker_id,
            status="success",
            data={
                "status": "captured",
                "path": str(tmp_path / "screen.png"),
                "filename": "screen.png",
                "content_type": "image/png",
                "size_bytes": 1234,
            },
        ),
    )
    scan = _latest_command(db, WorkerCommandType.SCAN_PROJECT)
    _finish(
        db,
        scan,
        WorkerResult(
            command_id=scan.id,
            worker_id=scan.worker_id,
            status="success",
            data={"status": "scanned", "root": "D:/work/project", "recommended_commands": [["npm", "test"]]},
        ),
    )
    run = _latest_command(db, WorkerCommandType.RUN_COMMAND)
    _finish(db, run, WorkerResult(command_id=run.id, worker_id=run.worker_id, status="success", data={"returncode": 0}))
    browser = _latest_command(db, WorkerCommandType.BROWSER_ACCEPTANCE)
    _finish(
        db,
        browser,
        WorkerResult(
            command_id=browser.id,
            worker_id=browser.worker_id,
            status="success",
            data={"status": "passed", "url": "http://localhost:5173", "http_status": 200},
        ),
    )
    git = _latest_command(db, WorkerCommandType.GIT_SUBMIT)
    _finish(
        db,
        git,
        WorkerResult(
            command_id=git.id,
            worker_id=git.worker_id,
            status="success",
            data={"status": "committed", "commit_sha": "abc123", "remote_url": "https://github.com/acme/repo.git"},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.PROJECT_COMPLETED
    assert round_.status == JobState.ROUND_COMPLETED
    assert round_.trace_status == "valid"
    assert round_.github_status == "committed"
    assert round_.feishu_status == "written"
    assert db.scalar(select(Attachment).where(Attachment.kind == "trace")) is not None
    assert db.scalar(select(Attachment).where(Attachment.kind == "screenshot")) is not None
    assert _latest_dissatisfaction_reason(db) is None


def test_full_worker_result_failure_path_generates_reason_after_trace_gate(tmp_path):
    db = _test_session()
    job, round_, command = _create_run_command_rows(db)
    _create_trace_attachment(db, job.id, round_.id, tmp_path, "full trace evidence")

    _finish(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"returncode": 1, "stderr": "src/app.ts:10 build failed"},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    reason = _latest_dissatisfaction_reason(db)
    assert job.status == JobState.BROWSER_ACCEPTING
    assert round_.status == JobState.BROWSER_ACCEPTING
    assert reason is not None
    assert "src/app.ts:10 build failed" in reason.extra["reason"]
    assert reason.extra["evidence_summary"]["trace_chars"] > 0
    assert db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.BROWSER_ACCEPTANCE.value)) is not None


def test_run_command_failure_reason_includes_structured_diagnostics(tmp_path):
    db = _test_session()
    job, round_, command = _create_run_command_rows(db)
    _create_trace_attachment(db, job.id, round_.id, tmp_path, "full trace evidence")

    _finish(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "returncode": 1,
                "stderr": "build failed",
                "diagnostics": {
                    "summary": "Command failed (type_error) at src/App.tsx:12: npm run build",
                    "error_type": "type_error",
                    "primary_location": {"path": "src/App.tsx", "line": 12, "column": 5},
                },
            },
        ),
    )

    reason = _latest_dissatisfaction_reason(db)
    assert reason is not None
    assert "src/App.tsx:12" in reason.extra["reason"]
    assert "type_error" in reason.extra["reason"]


def test_wait_completion_success_queues_trace_copy():
    db = _test_session()
    job, round_, command = _create_wait_completion_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "text_chars": 1000,
                "watcher_observation": {
                    "activity": {"recent": False, "source": "agent_log", "quiet_seconds": 301.0},
                    "log": {"tail_hash": "abc123"},
                },
                "activity_summary": {"recent": False, "source": "agent_log", "quiet_seconds": 301.0},
                "supervisor_decision": {
                    "action": "collect_trace",
                    "reason": "trae_turn_completed",
                    "completion_gate": {"passed": True, "reason": "ok"},
                },
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.COPY_LATEST_REPLY.value))
    collect_log = db.scalar(
        select(RuntimeLog)
        .where(RuntimeLog.stage == JobState.COLLECTING_TRACE)
        .where(RuntimeLog.message == "Trae output appears stable; collecting the full assistant trace.")
    )
    assert job.status == JobState.COLLECTING_TRACE
    assert round_.status == JobState.COLLECTING_TRACE
    assert next_command is not None
    assert next_command.status == "queued"
    assert collect_log is not None
    assert collect_log.extra["supervisor_decision"]["action"] == "collect_trace"
    assert collect_log.extra["watcher_observation"]["activity"]["source"] == "agent_log"
    assert collect_log.extra["activity_summary"]["quiet_seconds"] == 301.0
    assert collect_log.display_message == "Supervisor 已确认 Trae CN 当前回合完成，Worker 开始获取回复内容和执行轨迹。"


def test_wait_completion_chrome_only_requeues_observation_without_click_continue():
    db = _test_session()
    job, round_, command = _create_wait_completion_rows(db)
    command.payload = {"prompt": "demo", "workspace_path": "project-a"}
    db.commit()

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="manual_required",
            error="Trae output did not contain assistant content; only window chrome text was detected",
            data={
                "supervisor_decision": {"action": "wait", "reason": "window_chrome_only"},
                "activity_summary": {"recent": False},
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    wait_commands = list(
        db.scalars(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.WAIT_COMPLETION.value)).all()
    )
    click_commands = list(
        db.scalars(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.CLICK_CONTINUE.value)).all()
    )
    retry = [item for item in wait_commands if item.id != command.id][0]
    last_log = list(db.scalars(select(RuntimeLog).order_by(RuntimeLog.created_at)).all())[-1]

    assert job.status == JobState.WAITING_TRAE
    assert round_.status == JobState.WAITING_TRAE
    assert retry.payload["wait_observation_attempts"] == 1
    assert retry.payload["intervention_idle_seconds"] == 30
    assert retry.payload["workspace_path"] == "project-a"
    assert click_commands == []
    assert "不会点击恢复按钮" in last_log.display_message


def test_wait_completion_worker_command_error_requeues_observation_without_click_continue():
    db = _test_session()
    job, round_, command = _create_wait_completion_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="failed",
            error="No explicit Trae intervention target was found; diagnosis_state=idle_or_running",
            data={},
        ),
    )

    click_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.CLICK_CONTINUE.value))
    retry = db.scalar(
        select(WorkerCommand)
        .where(WorkerCommand.command_type == WorkerCommandType.WAIT_COMPLETION.value)
        .where(WorkerCommand.id != command.id)
    )

    assert job.status == JobState.WAITING_TRAE
    assert round_.status == JobState.WAITING_TRAE
    assert click_command is None
    assert retry is not None


def test_wait_completion_timeout_with_completed_turn_queues_trace_collection():
    db = _test_session()
    job, round_, command = _create_wait_completion_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="manual_required",
            error="Trae output did not become stable before wait_completion timeout",
            data={
                "completion_gate": {"passed": True, "reason": "ok"},
                "trae_turn": {
                    "status": "found",
                    "turn_status": "completed",
                    "session_id": "s1",
                    "user_message_id": "u1",
                    "trace_id": VALID_TRAE_TRACE_ID,
                    "tool_call_count": 6,
                },
                "supervisor_decision": {"action": "collect_trace", "reason": "timeout_completion_detected"},
                "watcher_observation": {"activity": {"recent": False, "quiet_seconds": 42.0}},
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    copy_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.COPY_LATEST_REPLY.value))
    click_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.CLICK_CONTINUE.value))

    assert job.status == JobState.COLLECTING_TRACE
    assert round_.status == JobState.COLLECTING_TRACE
    assert copy_command is not None
    assert copy_command.payload["allow_local_trace_fallback"] is True
    assert click_command is None


def test_copy_latest_reply_validates_trace_and_advances_to_screenshot(monkeypatch, tmp_path):
    db = _test_session()
    job, round_, command = _create_copy_latest_reply_rows(db)
    trace = _valid_trace()
    monkeypatch.setattr(worker_results.settings, "attachment_root", tmp_path / "storage")

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"raw_text": trace, "trae_turn": _valid_trae_turn()},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.SCREENSHOT_CAPTURING
    assert round_.status == JobState.SCREENSHOT_CAPTURING
    assert round_.trace_status == "valid"
    assert round_.trae_session_id == VALID_TRAE_SESSION_ID
    assert round_.trae_user_message_id == VALID_TRAE_USER_MESSAGE_ID
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.CAPTURE_SCREENSHOT.value))
    assert next_command is not None
    assert next_command.status == "queued"


def test_copy_latest_reply_does_not_store_uncompleted_trae_turn(monkeypatch, tmp_path):
    db = _test_session()
    job, round_, command = _create_copy_latest_reply_rows(db)
    trace = _valid_trace()
    monkeypatch.setattr(worker_results.settings, "attachment_root", tmp_path / "storage")

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"raw_text": trace, "trae_turn": {**_valid_trae_turn(), "turn_status": "interrupted"}},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.CLICK_CONTINUE.value))
    assert job.status == JobState.AWAITING_CONTINUE
    assert round_.status == JobState.AWAITING_CONTINUE
    assert round_.trace_status == "trae_turn_not_completed:interrupted"
    assert round_.trae_session_id == ""
    assert next_command is not None
    assert next_command.status == "queued"


def test_copy_latest_reply_rejects_context_mismatched_old_turn(monkeypatch, tmp_path):
    db = _test_session()
    job, round_, command = _create_copy_latest_reply_rows(db)
    trace = _valid_trace()
    monkeypatch.setattr(worker_results.settings, "attachment_root", tmp_path / "storage")

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "raw_text": trace,
                "current_turn_gate": {"passed": False, "reason": "workspace_mismatch", "recoverable": False},
                "trae_turn": {"status": "missing", "reason": "workspace_mismatch"},
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    screenshot_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.CAPTURE_SCREENSHOT.value))
    assert job.status == JobState.TRACE_MISSING_ABORT
    assert round_.status == JobState.TRACE_MISSING_ABORT
    assert round_.trace_status == "workspace_mismatch"
    assert screenshot_command is None
    assert db.scalar(select(Attachment).where(Attachment.kind == "trace")) is None


def test_copy_latest_reply_service_interruption_queues_continue_recovery():
    db = _test_session()
    job, round_, command = _create_copy_latest_reply_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "raw_text": _valid_trace(),
                "trace_probe": {"complete_like": False, "reason": "service_interrupted"},
                "trae_turn": _valid_trae_turn(),
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.CLICK_CONTINUE.value))
    assert job.status == JobState.AWAITING_CONTINUE
    assert round_.status == JobState.AWAITING_CONTINUE
    assert round_.trace_status == "service_interrupted"
    assert next_command is not None


def test_copy_latest_reply_recoverable_current_turn_gate_queues_continue(tmp_path, monkeypatch):
    db = _test_session()
    job, round_, command = _create_copy_latest_reply_rows(db)
    trace = _valid_trace()
    monkeypatch.setattr(worker_results.settings, "attachment_root", tmp_path / "storage")

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "raw_text": trace,
                "trace_probe": {"complete_like": True, "reason": "ok"},
                "current_turn_gate": {
                    "passed": False,
                    "reason": "awaiting_current_continuation",
                    "recoverable": True,
                },
                "supervisor_decision": {
                    "action": "continue_output",
                    "reason": "awaiting_current_continuation",
                    "recoverable": True,
                },
                "trae_turn": {"status": "missing", "reason": "awaiting_current_continuation"},
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.CLICK_CONTINUE.value))
    assert job.status == JobState.AWAITING_CONTINUE
    assert round_.status == JobState.AWAITING_CONTINUE
    assert round_.trace_status == "awaiting_current_continuation"
    assert next_command is not None
    recovery_log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == JobState.AWAITING_CONTINUE).limit(1))
    assert recovery_log is not None
    assert recovery_log.extra["data"]["supervisor_decision"]["action"] == "continue_output"


def test_capture_screenshot_records_attachment_and_advances_to_product_reviewing():
    db = _test_session()
    job, round_, command = _create_capture_screenshot_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "status": "captured",
                "path": "screenshots/worker-screen.png",
                "filename": "worker-screen.png",
                "content_type": "image/png",
                "size_bytes": 1234,
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    attachment = db.scalar(select(Attachment).where(Attachment.kind == "screenshot"))
    assert job.status == JobState.PRODUCT_REVIEWING
    assert round_.status == JobState.PRODUCT_REVIEWING
    assert attachment is not None
    assert attachment.path == "screenshots/worker-screen.png"
    assert attachment.size_bytes == 1234
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.SCAN_PROJECT.value))
    assert next_command is not None
    assert next_command.status == "queued"


def test_capture_screenshot_uses_uploaded_server_attachment():
    db = _test_session()
    job, round_, command = _create_capture_screenshot_rows(db)
    uploaded = Attachment(
        id="att-uploaded",
        user_id=command.user_id,
        job_id=job.id,
        round_id=round_.id,
        kind="screenshot",
        filename="server-screen.png",
        path="storage/workers/worker1/screenshot/server-screen.png",
        content_type="image/png",
        size_bytes=4321,
    )
    db.add(uploaded)
    db.commit()

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "status": "captured",
                "path": "C:/Users/PC/AppData/Roaming/AgentOps/screenshots/local.png",
                "filename": "local.png",
                "content_type": "image/png",
                "size_bytes": 1234,
                "server_attachment": {"id": uploaded.id},
            },
        ),
    )

    attachments = list(db.scalars(select(Attachment).where(Attachment.kind == "screenshot")).all())
    assert len(attachments) == 1
    assert attachments[0].id == uploaded.id
    assert attachments[0].path == "storage/workers/worker1/screenshot/server-screen.png"


def test_capture_screenshot_rejects_failed_quality_gate():
    db = _test_session()
    job, round_, command = _create_capture_screenshot_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "status": "captured",
                "path": "screenshots/bad.png",
                "filename": "bad.png",
                "content_type": "image/png",
                "size_bytes": 10,
                "quality": {"ok": False, "reason": "mostly_blank"},
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.MANUAL_REQUIRED
    assert round_.status == JobState.MANUAL_REQUIRED
    assert db.scalar(select(Attachment).where(Attachment.kind == "screenshot")) is None


def test_scan_project_with_recommended_command_queues_product_review_command():
    db = _test_session()
    job, round_, command = _create_scan_project_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "status": "scanned",
                "root": "D:/work/project",
                "files": ["package.json", "src/App.tsx"],
                "recommended_commands": [["npm", "test"], ["npm", "run", "build"]],
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.RUN_COMMAND.value))
    assert job.status == JobState.PRODUCT_REVIEWING
    assert round_.status == JobState.PRODUCT_REVIEWING
    assert next_command is not None
    assert next_command.payload["command"] == ["npm", "test"]
    assert next_command.payload["cwd"] == "D:/work/project"
    assert next_command.payload["remaining_commands"] == [["npm", "run", "build"]]


def test_scan_project_without_commands_advances_to_browser_accepting():
    db = _test_session()
    job, round_, command = _create_scan_project_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "scanned", "root": "D:/work/project", "files": ["README.md"], "recommended_commands": []},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.BROWSER_ACCEPTANCE.value))
    assert job.status == JobState.BROWSER_ACCEPTING
    assert round_.status == JobState.BROWSER_ACCEPTING
    assert next_command is not None
    assert next_command.status == "queued"


def test_scan_project_without_commands_continues_after_static_review_issues():
    db = _test_session()
    job, round_, command = _create_scan_project_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "status": "scanned",
                "root": "D:/work/project",
                "files": ["src/App.vue"],
                "recommended_commands": [],
                "product_review": {
                    "ok": False,
                    "issues": ["src/App.vue:2 函数体为空：function save() {}"],
                    "warnings": [],
                    "evidence": ["审查了 1 个项目文件，其中主要代码文件 1 个。"],
                },
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    browser_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.BROWSER_ACCEPTANCE.value))
    reason = _latest_dissatisfaction_reason(db)
    assert job.status == JobState.BROWSER_ACCEPTING
    assert round_.status == JobState.BROWSER_ACCEPTING
    assert browser_command is not None
    assert reason is not None
    assert "函数体为空" in reason.extra["reason"]


def test_run_command_success_advances_to_browser_accepting():
    db = _test_session()
    job, round_, command = _create_run_command_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(command_id=command.id, worker_id=command.worker_id, status="success", data={"returncode": 0}),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.BROWSER_ACCEPTANCE.value))
    assert job.status == JobState.BROWSER_ACCEPTING
    assert round_.status == JobState.BROWSER_ACCEPTING
    assert next_command is not None
    assert next_command.status == "queued"
    assert next_command.payload["url"] == "http://localhost:5173"


def test_run_command_success_with_static_review_issues_continues_to_browser_acceptance():
    db = _test_session()
    job, round_, command = _create_run_command_rows(db)
    command.payload = {
        **command.payload,
        "product_review": {
            "ok": False,
            "issues": ["src/App.vue:3 事件绑定为空：<button @click=\"\">保存</button>"],
            "warnings": [],
            "evidence": ["审查了 4 个项目文件，其中主要代码文件 2 个。"],
        },
    }
    db.commit()

    handle_worker_result(
        db,
        command,
        WorkerResult(command_id=command.id, worker_id=command.worker_id, status="success", data={"returncode": 0}),
    )

    db.refresh(job)
    db.refresh(round_)
    browser_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.BROWSER_ACCEPTANCE.value))
    reason = _latest_dissatisfaction_reason(db)
    assert job.status == JobState.BROWSER_ACCEPTING
    assert round_.status == JobState.BROWSER_ACCEPTING
    assert browser_command is not None
    assert reason is not None
    assert "事件绑定为空" in reason.extra["reason"]


def test_run_command_failure_generates_reason_and_continues_to_browser_acceptance():
    db = _test_session()
    job, round_, command = _create_run_command_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"returncode": 1, "stderr": "build failed"},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.BROWSER_ACCEPTING
    assert round_.status == JobState.BROWSER_ACCEPTING
    assert db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.BROWSER_ACCEPTANCE.value)) is not None
    reason = _latest_dissatisfaction_reason(db)
    assert reason is not None
    assert "产物不满意：" in reason.extra["reason"]
    assert "过程不满意：" in reason.extra["reason"]
    assert "build failed" in reason.extra["reason"]


def test_browser_acceptance_success_advances_to_github_submitting_after_first_round():
    db = _test_session()
    job, round_, command = _create_browser_acceptance_rows(db)
    round_.round_index = 2
    db.commit()

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "passed", "url": "http://localhost:5173", "http_status": 200},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.GIT_SUBMIT.value))
    assert job.status == JobState.GITHUB_SUBMITTING
    assert round_.status == JobState.GITHUB_SUBMITTING
    assert round_.github_status == "submitting"
    assert next_command is not None
    assert next_command.status == "queued"
    assert next_command.payload["commit_message"].startswith("AgentOps: demo")


def test_first_round_satisfied_is_discarded_without_github_submission():
    db = _test_session()
    job, round_, command = _create_browser_acceptance_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "passed", "url": "http://localhost:5173", "http_status": 200},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    git_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.GIT_SUBMIT.value))
    next_round = db.scalar(
        select(TaskRound)
        .where(TaskRound.job_id == job.id, TaskRound.id != round_.id)
        .order_by(TaskRound.created_at.desc())
        .limit(1)
    )
    assert job.status == JobState.GENERATING_PROMPT
    assert round_.status == "first_round_discarded"
    assert git_command is None
    assert next_round is not None
    assert next_round.round_index == 1


def test_browser_acceptance_blocks_without_real_trae_session():
    db = _test_session()
    job, round_, command = _create_browser_acceptance_rows(db)
    round_.trae_session_id = ""
    db.commit()

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "passed", "url": "http://localhost:5173", "http_status": 200},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.GIT_SUBMIT.value))
    assert job.status == "session_missing_abort"
    assert round_.status == "session_missing_abort"
    assert next_command is None


def test_browser_acceptance_missing_url_generates_reason_and_continues_to_github():
    db = _test_session()
    job, round_, command = _create_browser_acceptance_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "no_browser_evidence"},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    db.refresh(command)
    assert job.status == JobState.GITHUB_SUBMITTING
    assert round_.status == JobState.GITHUB_SUBMITTING
    assert db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.GIT_SUBMIT.value)) is not None
    assert _latest_dissatisfaction_reason(db) is not None


def test_browser_acceptance_failure_generates_reason_and_continues_to_github():
    db = _test_session()
    job, round_, command = _create_browser_acceptance_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "status": "failed",
                "url": "http://localhost:5173",
                "http_status": 500,
                "inspection": {"issues": ["页面正文为空或接近空白，且没有可见交互入口。"], "warnings": []},
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    db.refresh(command)
    assert job.status == JobState.GITHUB_SUBMITTING
    assert round_.status == JobState.GITHUB_SUBMITTING
    assert db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.GIT_SUBMIT.value)) is not None
    reason = _latest_dissatisfaction_reason(db)
    assert reason is not None
    assert "接近空白" in reason.extra["reason"]


def test_git_submit_success_advances_to_feishu_preparing():
    db = _test_session()
    job, round_, command = _create_git_submit_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "pushed", "commit_sha": "abc123", "changed_files": 2},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.FEISHU_FAILED_ABORT
    assert round_.status == JobState.FEISHU_FAILED_ABORT
    assert round_.github_status == "pushed"
    assert job.submitted_count == 1
    assert _latest_dissatisfaction_reason(db) is not None


def test_git_submit_success_writes_feishu_and_completes(monkeypatch, tmp_path):
    db = _test_session()
    job, round_, command = _create_git_submit_rows(db)
    job.submitted_count = 4
    job.satisfied_count = 0
    _create_feishu_config(db, job.user_id)
    _create_trace_attachment(db, job.id, round_.id, tmp_path, "full trae trace")
    written = {}

    def fake_write(feishu_config, fields):
        written["feishu_config"] = feishu_config
        written["fields"] = fields
        return {"status": "written", "record_id": "rec1", "app_token": feishu_config["app_token"], "table_id": feishu_config["table_id"]}

    monkeypatch.setattr(worker_results, "write_feishu_record", fake_write)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "pushed", "commit_sha": "abc123", "remote_url": "https://github.com/acme/repo.git"},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.PROJECT_COMPLETED
    assert round_.status == JobState.ROUND_COMPLETED
    assert round_.feishu_status == "written"
    assert job.submitted_count == 5
    assert job.satisfied_count == 1
    assert written["fields"]["Trae Session ID"] == VALID_TRAE_SESSION_ID
    assert written["fields"]["日志轨迹"] == "full trae trace"
    assert written["fields"]["github地址"] == "https://github.com/acme/repo.git"


def test_git_submit_unsatisfied_feishu_success_prepares_next_round(monkeypatch, tmp_path):
    db = _test_session()
    job, round_, command = _create_git_submit_rows(db)
    _create_feishu_config(db, job.user_id)
    _create_trace_attachment(db, job.id, round_.id, tmp_path, "full trae trace")
    db.add(
        RuntimeLog(
            job_id=job.id,
            round_id=round_.id,
            stage="dissatisfaction_reason",
            message="Dissatisfaction reason generated.",
            level="warning",
            extra={"reason": "产物不满意：构建没有通过。\n过程不满意：没有回到失败点继续修。"},
        )
    )
    db.commit()
    monkeypatch.setattr(worker_results, "write_feishu_record", lambda feishu_config, fields: {"status": "written", "record_id": "rec1"})

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "pushed", "commit_sha": "abc123", "remote_url": "https://github.com/acme/repo.git"},
        ),
    )

    next_round = db.scalar(
        select(TaskRound)
        .where(TaskRound.job_id == job.id, TaskRound.round_index == 2)
        .limit(1)
    )
    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.GENERATING_PROMPT
    assert round_.status == JobState.ROUND_COMPLETED
    assert round_.feishu_status == "written"
    assert next_round is not None
    assert next_round.status == JobState.GENERATING_PROMPT


def test_satisfied_ratio_cap_prepares_followup_round(monkeypatch, tmp_path):
    db = _test_session()
    job, round_, command = _create_git_submit_rows(db)
    round_.round_index = 2
    job.submitted_count = 1
    job.satisfied_count = 0
    _create_feishu_config(db, job.user_id)
    _create_trace_attachment(db, job.id, round_.id, tmp_path, "full trae trace")
    monkeypatch.setattr(worker_results, "write_feishu_record", lambda feishu_config, fields: {"status": "written", "record_id": "rec1"})

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "pushed", "commit_sha": "abc123", "remote_url": "https://github.com/acme/repo.git"},
        ),
    )

    next_round = db.scalar(
        select(TaskRound)
        .where(TaskRound.job_id == job.id, TaskRound.round_index == 3)
        .limit(1)
    )
    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.GENERATING_PROMPT
    assert round_.status == JobState.ROUND_COMPLETED
    assert job.submitted_count == 2
    assert job.satisfied_count == 0
    assert next_round is not None


def test_daily_target_stops_after_feishu_success(monkeypatch, tmp_path):
    db = _test_session()
    job, round_, command = _create_git_submit_rows(db)
    round_.round_index = 2
    job.daily_target = 2
    job.submitted_count = 1
    job.satisfied_count = 0
    _create_feishu_config(db, job.user_id)
    _create_trace_attachment(db, job.id, round_.id, tmp_path, "full trae trace")
    monkeypatch.setattr(worker_results, "write_feishu_record", lambda feishu_config, fields: {"status": "written", "record_id": "rec1"})

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "pushed", "commit_sha": "abc123", "remote_url": "https://github.com/acme/repo.git"},
        ),
    )

    next_round = db.scalar(
        select(TaskRound)
        .where(TaskRound.job_id == job.id, TaskRound.id != round_.id)
        .limit(1)
    )
    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.PROJECT_COMPLETED
    assert round_.status == JobState.ROUND_COMPLETED
    assert job.submitted_count == 2
    assert job.satisfied_count == 0
    assert next_round is None


def test_max_round_stops_when_result_remains_unsatisfied(monkeypatch, tmp_path):
    db = _test_session()
    job, round_, command = _create_git_submit_rows(db)
    round_.round_index = 5
    job.submitted_count = 4
    _create_feishu_config(db, job.user_id)
    _create_trace_attachment(db, job.id, round_.id, tmp_path, "full trae trace")
    db.add(
        RuntimeLog(
            job_id=job.id,
            round_id=round_.id,
            stage="dissatisfaction_reason",
            message="Dissatisfaction reason generated.",
            level="warning",
            extra={"reason": "still not shippable"},
        )
    )
    db.commit()
    monkeypatch.setattr(worker_results, "write_feishu_record", lambda feishu_config, fields: {"status": "written", "record_id": "rec1"})

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "pushed", "commit_sha": "abc123", "remote_url": "https://github.com/acme/repo.git"},
        ),
    )

    next_round = db.scalar(
        select(TaskRound)
        .where(TaskRound.job_id == job.id, TaskRound.id != round_.id)
        .limit(1)
    )
    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.PROJECT_COMPLETED
    assert round_.status == JobState.ROUND_COMPLETED
    assert next_round is None


def test_completed_project_advances_to_next_direction(monkeypatch, tmp_path):
    db = _test_session()
    job, round_, command = _create_git_submit_rows(db)
    job.directions = ["first direction", "second direction"]
    job.submitted_count = 4
    job.satisfied_count = 0
    project = Project(
        id="project1",
        job_id=job.id,
        name="first-project",
        direction="first direction",
        workspace_path="D:/work/first-project",
        status="active",
    )
    round_.project_id = project.id
    _create_feishu_config(db, job.user_id)
    _create_trace_attachment(db, job.id, round_.id, tmp_path, "full trae trace")
    db.add(project)
    db.commit()
    monkeypatch.setattr(worker_results, "write_feishu_record", lambda feishu_config, fields: {"status": "written", "record_id": "rec1"})

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "pushed", "commit_sha": "abc123", "remote_url": "https://github.com/acme/repo.git"},
        ),
    )

    next_round = db.scalar(
        select(TaskRound)
        .where(TaskRound.job_id == job.id, TaskRound.id != round_.id, TaskRound.round_index == 1)
        .limit(1)
    )
    db.refresh(job)
    db.refresh(round_)
    db.refresh(project)
    assert job.status == JobState.GENERATING_PROMPT
    assert job.directions == ["second direction"]
    assert project.status == "completed"
    assert next_round is not None
    assert next_round.project_id is None


def test_git_submit_feishu_payload_uses_trace_overflow_attachment(monkeypatch, tmp_path):
    db = _test_session()
    job, round_, command = _create_git_submit_rows(db)
    _create_feishu_config(db, job.user_id)
    trace = _create_trace_attachment(db, job.id, round_.id, tmp_path, "真实 Trae 日志\n" * 5000)
    screenshot_path = tmp_path / "screen.png"
    screenshot_path.write_bytes(b"png")
    db.add(
        Attachment(
            user_id=job.user_id,
            job_id=job.id,
            round_id=round_.id,
            kind="screenshot",
            filename="screen.png",
            path=str(screenshot_path),
            content_type="image/png",
            size_bytes=3,
        )
    )
    db.commit()
    written = {}

    def fake_write(feishu_config, fields):
        written["fields"] = fields
        return {"status": "written", "record_id": "rec1"}

    monkeypatch.setattr(worker_results, "write_feishu_record", fake_write)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "pushed", "commit_sha": "abc123", "remote_url": "https://github.com/acme/repo.git"},
        ),
    )

    attachments = written["fields"][worker_results.FEISHU_ATTACHMENT_FIELD]
    assert written["fields"]["日志轨迹"] == worker_results.LOG_TRACE_OVERFLOW_TEXT
    assert str(trace.path) in attachments
    assert str(screenshot_path) in attachments


def test_feishu_payload_infers_agentops_fullstack_and_clone_url(tmp_path):
    db = _test_session()
    job = Job(
        id="job1",
        user_id="user1",
        status=JobState.GITHUB_SUBMITTING,
        directions=["AgentOps 多角色 LLM + Windows Worker 自动作业平台，包含控制台、API、飞书和 GitHub 闭环"],
    )
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        round_index=2,
        status=JobState.GITHUB_SUBMITTING,
        trace_status="valid",
        trae_session_id=VALID_TRAE_SESSION_ID,
        prompt="修复 Worker 绑定、任务看板和飞书预览的联动问题",
    )
    command = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id="user1",
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.GIT_SUBMIT.value,
        payload={"trae_workspace_path": "D:/work/project"},
        status="completed",
    )
    db.add_all([job, round_, command])
    db.commit()
    _create_trace_attachment(db, job.id, round_.id, tmp_path, "full trae trace")

    fields = worker_results._prepare_feishu_fields(
        db,
        job,
        round_,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "status": "pushed",
                "commit_sha": "abc123",
                "remote_url": "git@github.com:acme/agentops-platform.git",
                "changed_files": 6,
            },
        ),
    )

    assert fields["任务类型"] == "Bug修复"
    assert fields["Trae Session ID"] == VALID_TRAE_SESSION_ID
    assert fields["业务领域"] == "全栈Web应用"
    assert fields["修改范围"] == "跨模块多文件"
    assert fields["github地址"] == "https://github.com/acme/agentops-platform.git"


def test_git_submit_nothing_to_commit_advances_without_incrementing_submitted_count(monkeypatch):
    db = _test_session()
    job, round_, command = _create_git_submit_rows(db)
    _create_feishu_config(db, job.user_id)
    monkeypatch.setattr(worker_results, "write_feishu_record", lambda feishu_config, fields: {"status": "written", "record_id": "rec1"})

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"status": "nothing_to_commit", "commit_sha": "abc123"},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.PROJECT_COMPLETED
    assert round_.status == JobState.ROUND_COMPLETED
    assert round_.github_status == "nothing_to_commit"
    assert job.submitted_count == 0


def test_git_submit_failure_marks_github_failed_abort():
    db = _test_session()
    job, round_, command = _create_git_submit_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={
                "status": "push_failed",
                "stderr": "auth failed",
                "push_diagnostics": {
                    "reason": "https_token_or_credential_failed",
                    "message": "fatal: Authentication failed",
                    "credential_hint": "Verify the HTTPS remote uses a valid GitHub token or configured credential helper.",
                },
            },
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    db.refresh(command)
    assert job.status == JobState.GITHUB_FAILED_ABORT
    assert round_.status == JobState.GITHUB_FAILED_ABORT
    assert round_.github_status == "push_failed"
    assert command.status == "manual_required"
    reason = _latest_dissatisfaction_reason(db)
    assert reason is not None
    assert "auth failed" in reason.extra["reason"]
    assert "https_token_or_credential_failed" in reason.extra["reason"]


def test_capture_screenshot_failure_marks_manual_required():
    db = _test_session()
    job, round_, command = _create_capture_screenshot_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(command_id=command.id, worker_id=command.worker_id, status="failed", error="no display"),
    )

    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.MANUAL_REQUIRED
    assert round_.status == JobState.MANUAL_REQUIRED


def test_copy_latest_reply_incomplete_trace_retries_copy_before_recovery():
    db = _test_session()
    job, round_, command = _create_copy_latest_reply_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(command_id=command.id, worker_id=command.worker_id, status="success", data={"raw_text": "short"}),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.COPY_LATEST_REPLY.value).where(WorkerCommand.id != command.id))
    assert job.status == JobState.COLLECTING_TRACE
    assert round_.status == JobState.COLLECTING_TRACE
    assert round_.trace_status == "trace_too_short"
    assert next_command is not None
    assert next_command.status == "queued"
    assert next_command.payload["trace_copy_attempts"] == 1
    assert next_command.payload["max_trace_copy_attempts"] == worker_results.DEFAULT_MAX_TRACE_COPY_ATTEMPTS
    recovery_log = db.scalar(
        select(RuntimeLog)
        .where(RuntimeLog.stage == JobState.COLLECTING_TRACE)
        .order_by(RuntimeLog.created_at)
        .limit(1)
    )
    assert recovery_log is not None
    assert recovery_log.level == "info"
    assert "先重试滚底和复制" in recovery_log.display_message
    assert "人工处理" not in recovery_log.display_message


def test_click_continue_success_queues_wait_completion_again():
    db = _test_session()
    job, round_, command = _create_click_continue_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(command_id=command.id, worker_id=command.worker_id, status="success", data={"status": "clicked"}),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.WAIT_COMPLETION.value))
    assert job.status == JobState.WAITING_TRAE
    assert round_.status == JobState.WAITING_TRAE
    assert next_command is not None
    assert next_command.payload["continue_attempts"] == 1
    assert next_command.payload["intervention_idle_seconds"] == worker_results.FIRST_ROUND_INTERVENTION_IDLE_SECONDS


def test_click_continue_typed_continue_event_message_is_precise():
    message = build_display_message(
        "worker_command_finished",
        "click_continue worker_command_finished",
        extra={
            "command_type": WorkerCommandType.CLICK_CONTINUE.value,
            "result_status": "success",
            "result": {
                "status": "clicked",
                "action_taken": "typed_continue",
                "intervention": {"mode": "continue-text", "text": "继续"},
            },
        },
    )

    assert "输入“继续”" in message
    assert "点击 Trae CN 的继续按钮" not in message


def test_incomplete_trace_falls_back_to_recovery_after_max_copy_attempts():
    db = _test_session()
    job, round_, command = _create_copy_latest_reply_rows(db)
    command.payload = {"trace_copy_attempts": 5, "max_trace_copy_attempts": 5}
    db.commit()

    handle_worker_result(
        db,
        command,
        WorkerResult(command_id=command.id, worker_id=command.worker_id, status="success", data={"raw_text": "short"}),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.CLICK_CONTINUE.value))
    assert job.status == JobState.AWAITING_CONTINUE
    assert round_.status == JobState.AWAITING_CONTINUE
    assert next_command is not None
    assert next_command.payload["recovery_reason"] == "trace_too_short"
    assert _latest_dissatisfaction_reason(db) is None


def test_incomplete_trace_stops_after_copy_and_continue_limits():
    db = _test_session()
    job, round_, command = _create_copy_latest_reply_rows(db)
    command.payload = {
        "trace_copy_attempts": 5,
        "max_trace_copy_attempts": 5,
        "continue_attempts": 2,
        "max_continue_attempts": 2,
    }
    db.commit()

    handle_worker_result(
        db,
        command,
        WorkerResult(command_id=command.id, worker_id=command.worker_id, status="success", data={"raw_text": "short"}),
    )

    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.TRACE_MISSING_ABORT
    assert round_.status == JobState.TRACE_MISSING_ABORT
    assert _latest_dissatisfaction_reason(db) is None


def test_downstream_command_without_verified_trace_aborts_before_dissatisfaction():
    db = _test_session()
    job, round_, command = _create_run_command_rows(db, with_trace=False)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="success",
            data={"returncode": 1, "stderr": "build failed"},
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.TRACE_MISSING_ABORT
    assert round_.status == JobState.TRACE_MISSING_ABORT
    assert _latest_dissatisfaction_reason(db) is None


def test_send_prompt_manual_required_marks_job_manual_required():
    db = _test_session()
    job, round_, command = _create_send_prompt_rows(db)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="manual_required",
            error="Trae window was not found",
        ),
    )

    db.refresh(job)
    db.refresh(round_)
    log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == JobState.MANUAL_REQUIRED))
    assert job.status == JobState.MANUAL_REQUIRED
    assert round_.status == JobState.MANUAL_REQUIRED
    assert log is not None
    assert log.level == "warning"
    error = db.scalar(select(AutomationError).where(AutomationError.kind == "manual_required"))
    assert error is not None
    assert error.details["error"] == "Trae window was not found"


def test_manual_required_sends_webhook_notification(monkeypatch):
    db = _test_session()
    job, _round, command = _create_send_prompt_rows(db)
    db.add(UserConfig(user_id=job.user_id, category="webhook", data={"url": "https://open.feishu.cn/webhook/test"}))
    db.commit()
    sent = {}

    def fake_post(url, json, timeout):
        sent["url"] = url
        sent["json"] = json
        sent["timeout"] = timeout

        class Response:
            status_code = 200
            text = "ok"

        return Response()

    monkeypatch.setattr(webhook_notifier.httpx, "post", fake_post)

    handle_worker_result(
        db,
        command,
        WorkerResult(
            command_id=command.id,
            worker_id=command.worker_id,
            status="manual_required",
            error="Trae input not found",
            data={"manual_hint": "check Trae composer"},
        ),
    )

    log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == "manual_required_notification"))
    assert sent["url"] == "https://open.feishu.cn/webhook/test"
    assert sent["json"]["msg_type"] == "text"
    assert "AgentOps" in sent["json"]["content"]["text"]
    assert "check Trae composer" in sent["json"]["content"]["text"]
    assert log is not None
    assert log.level == "info"


def test_late_worker_result_after_stop_is_ignored():
    db = _test_session()
    job, round_, command = _create_wait_completion_rows(db)
    job.status = JobState.STOPPED
    round_.status = JobState.STOPPED
    db.commit()

    handle_worker_result(
        db,
        command,
        WorkerResult(command_id=command.id, worker_id=command.worker_id, status="success", data={"text_chars": 1000}),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.COPY_LATEST_REPLY.value))
    log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == "stale_worker_result_ignored"))
    assert job.status == JobState.STOPPED
    assert round_.status == JobState.STOPPED
    assert next_command is None
    assert log is not None


def test_late_cancelled_command_result_is_ignored():
    db = _test_session()
    job, round_, command = _create_wait_completion_rows(db)
    command.status = "cancelled"
    db.commit()

    handle_worker_result(
        db,
        command,
        WorkerResult(command_id=command.id, worker_id=command.worker_id, status="success", data={"text_chars": 1000}),
    )

    db.refresh(job)
    db.refresh(round_)
    next_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.COPY_LATEST_REPLY.value))
    log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == "stale_worker_result_ignored"))
    assert job.status == JobState.WAITING_TRAE
    assert round_.status == JobState.WAITING_TRAE
    assert next_command is None
    assert log is not None


def _test_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _latest_dissatisfaction_reason(db):
    return db.scalar(
        select(RuntimeLog)
        .where(RuntimeLog.stage == "dissatisfaction_reason")
        .order_by(RuntimeLog.created_at.desc())
        .limit(1)
    )


def _latest_command(db, command_type: WorkerCommandType) -> WorkerCommand:
    command = db.scalar(
        select(WorkerCommand)
        .where(WorkerCommand.command_type == command_type.value)
        .order_by(WorkerCommand.created_at.desc())
        .limit(1)
    )
    assert command is not None
    return command


def _finish(db, command: WorkerCommand, result: WorkerResult) -> None:
    handle_worker_result(db, command, result)


def _create_send_prompt_rows(db):
    job = Job(id="job1", user_id="user1", status=JobState.SENDING_TO_WORKER, directions=["demo"])
    round_ = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.SENDING_TO_WORKER)
    command = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id="user1",
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.SEND_PROMPT.value,
        payload={"prompt": "demo"},
        status="completed",
    )
    db.add_all([job, round_, command])
    db.commit()
    return job, round_, command


def _create_wait_completion_rows(db):
    job = Job(id="job1", user_id="user1", status=JobState.WAITING_TRAE, directions=["demo"])
    round_ = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.WAITING_TRAE)
    command = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id="user1",
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.WAIT_COMPLETION.value,
        payload={},
        status="completed",
    )
    db.add_all([job, round_, command])
    db.commit()
    return job, round_, command


def _create_copy_latest_reply_rows(db):
    job = Job(id="job1", user_id="user1", status=JobState.COLLECTING_TRACE, directions=["demo"])
    round_ = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.COLLECTING_TRACE)
    command = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id="user1",
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.COPY_LATEST_REPLY.value,
        payload={},
        status="completed",
    )
    db.add_all([job, round_, command])
    db.commit()
    return job, round_, command


def _create_capture_screenshot_rows(db):
    job = Job(id="job1", user_id="user1", status=JobState.SCREENSHOT_CAPTURING, directions=["demo"])
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        round_index=1,
        status=JobState.SCREENSHOT_CAPTURING,
        trace_status="valid",
        trae_session_id=VALID_TRAE_SESSION_ID,
    )
    command = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id="user1",
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.CAPTURE_SCREENSHOT.value,
        payload={},
        status="completed",
    )
    db.add_all([job, round_, command])
    db.commit()
    _create_valid_trace_attachment_for_round(db, job, round_)
    return job, round_, command


def _create_scan_project_rows(db):
    job = Job(id="job1", user_id="user1", status=JobState.PRODUCT_REVIEWING, directions=["demo"])
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        round_index=1,
        status=JobState.PRODUCT_REVIEWING,
        trace_status="valid",
        trae_session_id=VALID_TRAE_SESSION_ID,
    )
    command = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id="user1",
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.SCAN_PROJECT.value,
        payload={"trae_workspace_path": "D:/work/project"},
        status="completed",
    )
    db.add_all([job, round_, command])
    db.commit()
    _create_valid_trace_attachment_for_round(db, job, round_)
    return job, round_, command


def _create_run_command_rows(db, with_trace: bool = True):
    job = Job(id="job1", user_id="user1", status=JobState.PRODUCT_REVIEWING, directions=["demo"])
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        round_index=1,
        status=JobState.PRODUCT_REVIEWING,
        trace_status="valid" if with_trace else "missing",
        trae_session_id=VALID_TRAE_SESSION_ID if with_trace else "",
    )
    command = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id="user1",
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.RUN_COMMAND.value,
        payload={"command": ["npm", "test"], "purpose": "product_review", "browser_url": "http://localhost:5173"},
        status="completed",
    )
    db.add_all([job, round_, command])
    db.commit()
    if with_trace:
        _create_valid_trace_attachment_for_round(db, job, round_)
    return job, round_, command


def _create_browser_acceptance_rows(db):
    job = Job(id="job1", user_id="user1", status=JobState.BROWSER_ACCEPTING, directions=["demo"])
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        round_index=1,
        status=JobState.BROWSER_ACCEPTING,
        trace_status="valid",
        trae_session_id=VALID_TRAE_SESSION_ID,
        trae_user_message_id=VALID_TRAE_USER_MESSAGE_ID,
        trae_task_id=VALID_TRAE_TASK_ID,
        trae_trace_id=VALID_TRAE_TRACE_ID,
    )
    command = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id="user1",
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.BROWSER_ACCEPTANCE.value,
        payload={"url": "http://localhost:5173"},
        status="completed",
    )
    db.add_all([job, round_, command])
    db.commit()
    _create_valid_trace_attachment_for_round(db, job, round_)
    return job, round_, command


def _create_git_submit_rows(db):
    job = Job(id="job1", user_id="user1", status=JobState.GITHUB_SUBMITTING, directions=["demo"])
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        round_index=1,
        status=JobState.GITHUB_SUBMITTING,
        trace_status="valid",
        trae_session_id=VALID_TRAE_SESSION_ID,
        trae_user_message_id=VALID_TRAE_USER_MESSAGE_ID,
        trae_task_id=VALID_TRAE_TASK_ID,
        trae_trace_id=VALID_TRAE_TRACE_ID,
        github_status="submitting",
    )
    command = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id="user1",
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.GIT_SUBMIT.value,
        payload={"trae_workspace_path": "D:/work/project"},
        status="completed",
    )
    db.add_all([job, round_, command])
    db.commit()
    _create_valid_trace_attachment_for_round(db, job, round_)
    return job, round_, command


def _create_valid_trace_attachment_for_round(db, job: Job, round_: TaskRound) -> Attachment:
    trace_dir = Path(tempfile.mkdtemp(prefix="agentops-test-trace-"))
    path = trace_dir / f"{job.id}-{round_.id}.txt"
    text = "full trae trace evidence"
    path.write_text(text, encoding="utf-8")
    attachment = Attachment(
        user_id=job.user_id,
        job_id=job.id,
        round_id=round_.id,
        kind="trace",
        filename=path.name,
        path=str(path),
        content_type="text/plain; charset=utf-8",
        size_bytes=len(text.encode("utf-8")),
    )
    db.add(attachment)
    db.commit()
    return attachment


def _create_feishu_config(db, user_id: str):
    config = UserConfig(
        user_id=user_id,
        category="feishu",
        data={
            "app_token": "app_token_1",
            "table_id": "table_1",
            "token_cache": {"tenant_access_token": "cached-token", "expires_at": 4102444800},
        },
    )
    db.add(config)
    db.commit()
    return config


def _create_trace_attachment(db, job_id: str, round_id: str, tmp_path, text: str):
    path = tmp_path / "trace.txt"
    path.write_text(text, encoding="utf-8")
    attachment = Attachment(
        user_id="user1",
        job_id=job_id,
        round_id=round_id,
        kind="trace",
        filename="trace.txt",
        path=str(path),
        content_type="text/plain; charset=utf-8",
        size_bytes=len(text.encode("utf-8")),
    )
    db.add(attachment)
    db.commit()
    return attachment


def _create_click_continue_rows(db):
    job = Job(id="job1", user_id="user1", status=JobState.AWAITING_CONTINUE, directions=["demo"])
    round_ = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.AWAITING_CONTINUE)
    command = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id="user1",
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.CLICK_CONTINUE.value,
        payload={"continue_attempts": 1, "max_continue_attempts": 20},
        status="completed",
    )
    db.add_all([job, round_, command])
    db.commit()
    return job, round_, command


def _valid_trace() -> str:
    body = "toolName: edit\nstatus: success\nfilePath: app.py\ncommand: pytest\nTodos updated: done\n"
    return body + ("trace detail line\n" * 80)


def _valid_trae_turn() -> dict:
    return {
        "status": "found",
        "session_id": VALID_TRAE_SESSION_ID,
        "user_message_id": VALID_TRAE_USER_MESSAGE_ID,
        "task_id": VALID_TRAE_TASK_ID,
        "trace_id": VALID_TRAE_TRACE_ID,
        "turn_status": "completed",
        "confidence": "latest_completed_trae_log_turn",
    }
