import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.jobs import StartJobRequest, retry_worker_command, start_job
from app.api.workers import assigned_worker_config
from app.db.models import Job, RuntimeLog, TaskRound, User, Worker, WorkerCommand
from app.db.models.base import now_utc
from app.db.session import Base
from app.services.llm.client import LLMError
from app.services.orchestrator.states import JobState
from app.services.orchestrator import prompt_writer
from app.services.orchestrator.worker_dispatch import dispatch_prompt_to_worker
from app.services.preflight import REQUIRED_WORKER_CAPABILITIES, build_preflight
from app.services.user_settings import load_user_settings, save_user_settings
from app.services.llm.client import model_config_from_settings
from app.worker_gateway.contracts import WorkerCommandType


def test_preflight_reports_blockers_when_user_settings_are_missing():
    db = _test_session()
    user = _create_user(db, "user1")

    result = build_preflight(db, user)

    assert result["ready"] is False
    assert "模型 API Key" in result["blocking"]
    assert "关联 Worker" in result["blocking"]
    assert "浏览器验收 URL" in result["blocking"]


def test_preflight_is_ready_with_current_user_worker_and_required_settings():
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    save_user_settings(
        db,
        user.id,
        {
            "model": {"api_key": "dummy_model_key", "model_name": "gpt-5.5"},
            "feishu": {
                "app_id": "cli_test",
                "app_secret": "dummy_feishu_secret",
                "app_token": "bascn_test",
                "table_id": "tbl_test",
            },
            "worker": {
                "worker_id": "worker1",
                "trae_workspace_path": "D:/work/project",
                "browser_url": "localhost:5173",
            },
        },
    )
    db.commit()

    result = build_preflight(db, user)

    assert result["ready"] is True
    assert result["blocking"] == []
    checks = {item["key"]: item for item in result["checks"]}
    assert checks["worker.browser_url"]["status"] == "pass"
    assert checks["github.token"]["status"] == "warning"


def test_model_secret_survives_public_settings_save_without_api_key():
    db = _test_session()
    user = _create_user(db, "user1")
    save_user_settings(
        db,
        user.id,
        {
            "model": {
                "provider": "OpenAI",
                "base_url": "https://pikachu.claudecode.love",
                "api_key": "sk-real-test-key",
                "model_name": "gpt-5.5",
            }
        },
    )
    db.commit()

    save_user_settings(
        db,
        user.id,
        {
            "model": {
                "provider": "OpenAI",
                "base_url": "https://pikachu.claudecode.love",
                "model_name": "gpt-5.5",
                "api_key_configured": True,
                "api_key_mask": "sk-0********",
            }
        },
    )
    db.commit()

    config = model_config_from_settings(load_user_settings(db, user.id))

    assert config.api_key == "sk-real-test-key"


def test_preflight_rejects_worker_bound_to_another_user():
    db = _test_session()
    user = _create_user(db, "user1")
    other = _create_user(db, "user2")
    _create_worker(db, other.id)
    save_user_settings(
        db,
        user.id,
        {
            "model": {"api_key": "dummy_model_key", "model_name": "gpt-5.5"},
            "feishu": {
                "app_id": "cli_test",
                "app_secret": "dummy_feishu_secret",
                "app_token": "bascn_test",
                "table_id": "tbl_test",
            },
            "worker": {
                "worker_id": "worker1",
                "trae_workspace_path": "D:/work/project",
                "browser_url": "http://localhost:5173",
            },
        },
    )
    db.commit()

    result = build_preflight(db, user)

    assert result["ready"] is False
    assert "关联 Worker" in result["blocking"]


def test_start_job_preflight_blocker_does_not_create_job():
    db = _test_session()
    user = _create_user(db, "user1")

    with pytest.raises(HTTPException) as exc_info:
        start_job(StartJobRequest(directions=["demo"]), user=user, db=db)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["preflight"]["ready"] is False
    assert db.scalar(select(Job)) is None


def test_start_job_fallback_prompt_dispatches_worker_when_llm_fails(monkeypatch):
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id)

    class FailingLLMClient:
        def complete(self, *_args, **_kwargs):
            raise LLMError("LLM request failed with status 401: USER_INACTIVE")

    monkeypatch.setattr(prompt_writer, "LLMClient", FailingLLMClient)

    result = start_job(StartJobRequest(directions=["做一个订单看板"]), user=user, db=db)

    command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.SEND_PROMPT.value))
    fallback_log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == "prompt_generation_fallback"))
    assert result["status"] == JobState.SENDING_TO_WORKER
    assert command is not None
    assert command.status == "queued"
    prompt = command.payload["prompt"]
    assert "做一个订单看板" in command.payload["prompt"]
    assert "中等规模" in prompt
    assert any(stack in prompt for stack in ("Python", "Go", "Vue", "Java"))
    assert "你现在在 Trae CN" not in prompt
    assert "AgentOps 自动作业" not in prompt
    assert "平台侧 LLM" not in prompt
    assert fallback_log is not None
    assert fallback_log.level == "warning"


def test_start_job_fallback_prompt_when_llm_prompt_contains_meta_phrase(monkeypatch):
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id)

    class Result:
        text = "产物不满意：把证据补齐"
        model = "gpt-test"
        wire_api = "responses"

    class MetaPhraseLLMClient:
        def complete(self, *_args, **_kwargs):
            return Result()

    monkeypatch.setattr(prompt_writer, "LLMClient", MetaPhraseLLMClient)

    start_job(StartJobRequest(directions=["做一个订单看板"]), user=user, db=db)

    command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.SEND_PROMPT.value))
    fallback_log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == "prompt_generation_fallback"))
    assert command is not None
    assert "做一个订单看板" in command.payload["prompt"]
    assert "产物不满意" not in command.payload["prompt"]
    assert fallback_log is not None
    assert fallback_log.extra["quality_error"] == "prompt_contains_meta_phrase:产物不满意"


def test_prompt_quality_rejects_duplicate_recent_prompt():
    db = _test_session()
    user = _create_user(db, "user1")
    job = Job(id="job1", user_id=user.id, status=JobState.PROMPT_READY, directions=["demo"])
    previous = TaskRound(
        id="round1",
        job_id=job.id,
        round_index=1,
        status=JobState.ROUND_COMPLETED,
        prompt="继续把订单看板的筛选和统计联动补完整",
    )
    current = TaskRound(id="round2", job_id=job.id, round_index=2, status=JobState.PROMPT_READY, prompt="")
    db.add_all([job, previous, current])
    db.commit()

    error = prompt_writer.prompt_quality_error(db, job, current, "继续把订单看板的筛选和统计联动补完整")

    assert error == "prompt_duplicate_recent"


def test_prompt_quality_rejects_first_round_template_prefix():
    db = _test_session()
    user = _create_user(db, "user1")
    job = Job(id="job1", user_id=user.id, status=JobState.PROMPT_READY, directions=["demo"])
    current = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.PROMPT_READY, prompt="")
    db.add_all([job, current])
    db.commit()

    error = prompt_writer.prompt_quality_error(
        db,
        job,
        current,
        "按这个项目方向做一个能继续迭代的系统雏形：订单管理系统。",
    )

    assert error.startswith("first_round_template_prefix:")


def test_prompt_quality_rejects_internal_process_language():
    db = _test_session()
    user = _create_user(db, "user1")
    job = Job(id="job1", user_id=user.id, status=JobState.PROMPT_READY, directions=["demo"])
    current = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.PROMPT_READY, prompt="")
    db.add_all([job, current])
    db.commit()

    error = prompt_writer.prompt_quality_error(
        db,
        job,
        current,
        "你现在在 trae cn 里帮我根据关键证据继续修复这个项目。",
    )

    assert error.startswith(("prompt_contains_internal_process:", "prompt_contains_meta_phrase:"))


def test_prompt_quality_rejects_first_round_too_small_positive_scope():
    db = _test_session()
    user = _create_user(db, "user1")
    job = Job(id="job1", user_id=user.id, status=JobState.PROMPT_READY, directions=["demo"])
    current = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.PROMPT_READY, prompt="")
    db.add_all([job, current])
    db.commit()

    error = prompt_writer.prompt_quality_error(db, job, current, "做一个很简单的小 demo，只要一个页面。")

    assert error == "first_round_too_small"


def test_assigned_worker_config_is_scoped_to_bound_user_settings():
    db = _test_session()
    user = _create_user(db, "user1")
    other = _create_user(db, "user2")
    worker = _create_worker(db, user.id)
    _save_required_settings(db, user.id, browser_url="http://localhost:5173", workspace_path="D:/mr-d")
    _save_required_settings(
        db,
        other.id,
        browser_url="http://localhost:4173",
        workspace_path="D:/other",
        worker_id="worker2",
    )

    result = assigned_worker_config(db, worker)

    assert result == {
        "trae_workspace_path": "D:/mr-d",
        "browser_url": "http://localhost:5173",
    }


def test_worker_dispatch_uses_current_user_worker_settings_only():
    db = _test_session()
    user = _create_user(db, "user1")
    other = _create_user(db, "user2")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id, browser_url="http://localhost:5173", workspace_path="D:/mr-d")
    _save_required_settings(
        db,
        other.id,
        browser_url="http://localhost:4173",
        workspace_path="D:/other",
        worker_id="worker2",
    )
    job = Job(id="job1", user_id=user.id, status=JobState.PROMPT_READY, directions=["demo"])
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        round_index=1,
        status=JobState.PROMPT_READY,
        prompt="demo prompt",
    )
    db.add_all([job, round_])
    db.commit()

    command = dispatch_prompt_to_worker(db, user, job, round_)

    assert command.user_id == user.id
    assert command.payload["browser_url"] == "http://localhost:5173"
    assert command.payload["workspace_root"] == "D:/mr-d"
    assert command.payload["project_name"].startswith("demo-")
    assert command.payload["trae_workspace_path"].replace("\\", "/").startswith("D:/mr-d/demo-")


def test_worker_dispatch_names_project_from_chinese_core_feature():
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id, browser_url="http://localhost:5173", workspace_path="D:/mr-d")
    save_user_settings(
        db,
        user.id,
        {
            "github": {
                "owner": "heisenberg-good-man",
                "remote_protocol": "https",
            }
        },
    )
    job = Job(id="job1", user_id=user.id, status=JobState.PROMPT_READY, directions=["做一个订单看板"])
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        round_index=1,
        status=JobState.PROMPT_READY,
        prompt="做一个订单看板，支持筛选、统计和状态流转。",
    )
    db.add_all([job, round_])
    db.commit()

    command = dispatch_prompt_to_worker(db, user, job, round_)

    project_name = command.payload["project_name"]
    assert project_name.startswith("order-dashboard-")
    assert command.payload["github_repo_name"] == project_name
    assert command.payload["trae_workspace_path"].replace("\\", "/") == f"D:/mr-d/{project_name}"
    assert command.payload["github_remote_url"] == f"https://github.com/heisenberg-good-man/{project_name}.git"


def test_retry_worker_command_requeues_failed_command_with_current_settings():
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id, browser_url="http://localhost:5173", workspace_path="D:/mr-d")
    job, round_ = _create_manual_required_job(db, user.id)
    previous = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.BROWSER_ACCEPTANCE.value,
        payload={"url": "http://localhost:3000", "trae_workspace_path": "D:/old"},
        status="failed",
        error="connection refused",
    )
    db.add(previous)
    db.commit()

    result = retry_worker_command(user=user, db=db)

    new_command = db.scalar(
        select(WorkerCommand)
        .where(WorkerCommand.id != previous.id)
        .order_by(WorkerCommand.created_at.desc())
        .limit(1)
    )
    assert result["worker_command"]["command_id"] == new_command.id
    assert new_command.command_type == WorkerCommandType.BROWSER_ACCEPTANCE.value
    assert new_command.status == "queued"
    assert new_command.payload["retry_of_command_id"] == previous.id
    assert new_command.payload["browser_url"] == "http://localhost:5173"
    assert new_command.payload["url"] == "http://localhost:5173"
    assert new_command.payload["trae_workspace_path"] == "D:/mr-d"
    db.refresh(job)
    db.refresh(round_)
    assert job.status == JobState.BROWSER_ACCEPTING
    assert round_.status == JobState.BROWSER_ACCEPTING


def test_retry_worker_command_rejects_active_command():
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id)
    job, round_ = _create_manual_required_job(db, user.id)
    active = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.RUN_COMMAND.value,
        payload={},
        status="queued",
    )
    db.add(active)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        retry_worker_command(user=user, db=db)

    assert exc_info.value.status_code == 400
    assert "still active" in exc_info.value.detail


def _test_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _create_user(db, user_id: str) -> User:
    user = User(id=user_id, email=f"{user_id}@example.test", display_name=user_id)
    db.add(user)
    db.commit()
    return user


def _create_worker(db, user_id: str) -> Worker:
    worker = Worker(
        worker_id="worker1",
        user_id=user_id,
        machine_name="agent-host",
        capabilities=sorted(REQUIRED_WORKER_CAPABILITIES),
        status="online",
        busy=False,
        last_seen_at=now_utc(),
    )
    db.add(worker)
    db.commit()
    return worker


def _create_manual_required_job(db, user_id: str) -> tuple[Job, TaskRound]:
    job = Job(id="job1", user_id=user_id, status=JobState.MANUAL_REQUIRED, directions=["demo"])
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        round_index=1,
        status=JobState.MANUAL_REQUIRED,
        prompt="demo prompt",
    )
    db.add_all([job, round_])
    db.commit()
    return job, round_


def _save_required_settings(
    db,
    user_id: str,
    browser_url: str = "http://localhost:5173",
    workspace_path: str = "D:/work/project",
    worker_id: str = "worker1",
) -> None:
    save_user_settings(
        db,
        user_id,
        {
            "model": {"api_key": "dummy_model_key", "model_name": "gpt-5.5"},
            "feishu": {
                "app_id": "cli_test",
                "app_secret": "dummy_feishu_secret",
                "app_token": "bascn_test",
                "table_id": "tbl_test",
            },
            "worker": {
                "worker_id": worker_id,
                "trae_workspace_path": workspace_path,
                "browser_url": browser_url,
            },
        },
    )
    db.commit()
