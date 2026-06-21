import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import jobs as jobs_api
from app.api.jobs import StartJobRequest, reopen_job, retry_worker_command, start_job, stop_job
from app.api.workers import assigned_worker_config
from app.db.models import Attachment, AutomationError, Job, Project, RuntimeLog, TaskRound, User, Worker, WorkerCommand
from app.db.models.base import now_utc
from app.db.session import Base
from app.services.llm.client import LLMError
from app.services.orchestrator.states import JobState
from app.services.orchestrator.directions import normalize_job_directions, split_direction_text
from app.services.orchestrator.intent import force_test_mode_intent, infer_job_intent
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
                "trae_exe_path": "D:/app/Trae CN/Trae CN.exe",
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
    assert checks["worker.trae_exe_path"]["status"] == "pass"
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


def test_feishu_write_url_updates_bitable_ids():
    db = _test_session()
    user = _create_user(db, "user1")
    write_url = (
        "https://bcnrsnl3m9wk.feishu.cn/base/NVLabI2piaNiVHsn6GYchYlmnRt"
        "?table=tblONK81uEWGM2jF&view=vewKdKoVia"
    )

    save_user_settings(
        db,
        user.id,
        {
            "feishu": {
                "app_id": "cli_test",
                "app_secret": "dummy_feishu_secret",
                "write_url": write_url,
                "app_token": "old_base",
                "table_id": "old_table",
                "view_id": "old_view",
            }
        },
    )
    db.commit()

    feishu = load_user_settings(db, user.id)["feishu"]

    assert feishu["write_url"] == write_url
    assert feishu["app_token"] == "NVLabI2piaNiVHsn6GYchYlmnRt"
    assert feishu["table_id"] == "tblONK81uEWGM2jF"
    assert feishu["view_id"] == "vewKdKoVia"


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
                "trae_exe_path": "D:/app/Trae CN/Trae CN.exe",
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
    assert result["job"]["scope_text"] == "做一个订单看板"
    assert command is not None
    assert command.status == "queued"
    prompt = command.payload["prompt"]
    assert "做一个订单看板" in command.payload["prompt"]
    assert any(
        term in prompt
        for term in (
            "别做成单页静态 demo",
            "不要只做一个很小的 demo",
            "不要停在说明页",
            "按真实业务人员会使用的方式实现",
            "不能只给文案或占位",
        )
    )
    assert any(term in prompt for term in ("业务模块", "详情", "状态", "本地模拟", "可运行"))
    assert any(stack in prompt for stack in ("Python", "Go", "Vue", "Java"))
    assert "你现在在 Trae CN" not in prompt
    assert "AgentOps 自动作业" not in prompt
    assert "平台侧 LLM" not in prompt
    assert fallback_log is not None
    assert fallback_log.level == "warning"


def test_prompt_writer_retries_when_quality_gate_rejects(monkeypatch):
    db = _test_session()
    user = _create_user(db, "user1")
    _save_required_settings(db, user.id)
    job = Job(
        id="job1",
        user_id=user.id,
        status=JobState.GENERATING_PROMPT,
        directions=["招聘平台：投简历、发布招聘信息和双方沟通渠道", "中介平台：实名认证和在线匹配下单"],
    )
    round_ = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.GENERATING_PROMPT)
    db.add_all([job, round_])
    db.commit()
    calls: list[dict] = []

    class Result:
        def __init__(self, text: str, model: str):
            self.text = text
            self.raw = {}
            self.model = model
            self.wire_api = "responses"

    class RetryingLLMClient:
        def complete(self, _config, messages, purpose=""):
            calls.append({"purpose": purpose, "messages": messages})
            if len(calls) == 1:
                return Result(
                    json.dumps(
                        {
                            "prompt": "招聘平台和中介平台一起做成综合工作台，支持投简历、实名认证和在线匹配下单。",
                            "prompt_kind": "feature",
                        },
                        ensure_ascii=False,
                    ),
                    "gpt-first",
                )
            return Result(
                json.dumps(
                    {
                        "prompt": (
                            "招聘平台：先做一个可运行的招聘工作台，包含岗位列表、简历投递、招聘方发布职位、"
                            "双方沟通状态和本地模拟数据；关键操作后要刷新列表、详情和统计，并保留异常提示。"
                        ),
                        "prompt_kind": "feature",
                        "focus": "招聘平台",
                        "acceptance_checks": ["投简历后列表和详情同步变化"],
                        "difference_from_previous": "移除队列里的中介平台范围",
                    },
                    ensure_ascii=False,
                ),
                "gpt-retry",
            )

    monkeypatch.setattr(prompt_writer, "LLMClient", RetryingLLMClient)

    prompt = prompt_writer.generate_round_prompt(db, user, job, round_)

    assert len(calls) == 2
    assert calls[1]["purpose"] == "prompt_generation_retry"
    feedback = json.loads(calls[1]["messages"][-1]["content"])
    assert feedback["type"] == "quality_gate_rejection"
    assert feedback["quality_error"] == "prompt_mentions_other_direction:中介平台"
    assert "中介平台" in feedback["quality_reason"]
    assert "招聘平台" in prompt
    assert "实名认证" not in prompt
    retry_log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == "prompt_generation_retry"))
    ready_log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == JobState.PROMPT_READY))
    fallback_log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == "prompt_generation_fallback"))
    assert retry_log is not None
    assert retry_log.extra["quality_error"] == "prompt_mentions_other_direction:中介平台"
    assert ready_log is not None
    assert ready_log.extra["model"] == "gpt-retry"
    assert ready_log.extra["prompt_retry"]["original_quality_error"] == "prompt_mentions_other_direction:中介平台"
    assert fallback_log is None


def test_start_job_test_mode_forces_test_intent_and_short_prompt_policy(monkeypatch):
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id)

    class FailingLLMClient:
        def complete(self, *_args, **_kwargs):
            raise LLMError("LLM unavailable")

    monkeypatch.setattr(prompt_writer, "LLMClient", FailingLLMClient)

    result = start_job(StartJobRequest(directions=["small hiring prototype"], run_mode="test"), user=user, db=db)

    job = db.scalar(select(Job))
    command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.SEND_PROMPT.value))
    assert result["job"]["intent"]["run_mode"] == "test"
    assert job.intent["dissatisfaction_policy"] == "force_test_unsatisfied"
    assert job.intent["downstream_policy"] == "test_chain_allowed"
    assert job.intent["trace_gate_policy"] == "test_exception"
    assert "skip_trae_self_tests" in job.intent["flags"]
    assert command is not None
    assert "不要运行耗时测试" in command.payload["prompt"]
    assert "npm.cmd" in command.payload["prompt"]
    assert "Test constraint:" not in command.payload["prompt"]
    assert "Windows command note:" not in command.payload["prompt"]


def test_start_job_test_mode_does_not_call_intent_or_prompt_llms(monkeypatch):
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id)

    class ExplodingLLMClient:
        def complete(self, *_args, **_kwargs):
            raise AssertionError("test mode should not call LLM")

    monkeypatch.setattr(prompt_writer, "LLMClient", ExplodingLLMClient)
    monkeypatch.setattr("app.services.orchestrator.intent.LLMClient", ExplodingLLMClient)

    result = start_job(
        StartJobRequest(directions=["AgentOps E2E smoke: create README with AgentOps E2E smoke OK only."], run_mode="test"),
        user=user,
        db=db,
    )

    command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.SEND_PROMPT.value))
    fallback_log = db.scalar(
        select(RuntimeLog)
        .where(RuntimeLog.stage == "prompt_generation_fallback")
        .order_by(RuntimeLog.created_at.desc())
        .limit(1)
    )
    assert result["job"]["intent"]["run_mode"] == "test"
    assert command is not None
    assert "AgentOps E2E smoke OK" in command.payload["prompt"]
    assert len(command.payload["prompt"]) < 700
    assert fallback_log is not None
    assert fallback_log.extra["test_mode_fast_path"] is True


def test_test_mode_smoke_fallback_keeps_prompt_tiny():
    db = _test_session()
    job = Job(
        id="job1",
        user_id="user1",
        status=JobState.GENERATING_PROMPT,
        directions=["AgentOps E2E smoke: create README with AgentOps E2E smoke OK only."],
        intent={
            "run_mode": "test",
            "prompt_brief": "AgentOps E2E smoke: create README with AgentOps E2E smoke OK only.",
            "flags": ["test_run", "chain_validation_only", "single_page_quick"],
        },
    )
    round_ = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.GENERATING_PROMPT)

    prompt = prompt_writer.build_fallback_prompt(job, round_)

    assert "AgentOps E2E smoke OK" in prompt
    assert "不要运行耗时测试" in prompt
    assert "npm.cmd" in prompt
    assert "Test constraint:" not in prompt
    assert "Windows command note:" not in prompt
    assert "涓氬姟妯″潡" not in prompt
    assert "鍋氭垚涓€涓兘鐩存帴杩愯鐨勪笟鍔″伐浣滃彴" not in prompt
    assert len(prompt) < 520


def test_test_mode_prompt_does_not_duplicate_user_scope():
    scope = (
        "AgentOps E2E smoke: create a tiny page titled AgentOps E2E Smoke Test "
        "with one clickable button."
    )
    intent = force_test_mode_intent(infer_job_intent(scope_text=scope, directions=[scope]), scope_text=scope)
    job = Job(
        id="job1",
        user_id="user1",
        status=JobState.GENERATING_PROMPT,
        directions=[scope],
        intent=intent,
    )
    round_ = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.GENERATING_PROMPT)

    prompt = prompt_writer.build_fallback_prompt(job, round_)

    assert prompt.count("AgentOps E2E Smoke Test") == 1
    assert prompt.count("测试约束：") == 1
    assert len(prompt) < 520


def test_start_job_test_button_preserves_chain_validation_flags_when_llm_omits_them(monkeypatch):
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id)

    class SparseLLMClient:
        def complete(self, _config, _messages, purpose=""):
            class Result:
                text = (
                    '{"run_mode":"test","intent_summary":"测试单页面链路",'
                    '"prompt_brief":"做单页面快速回复，用来测试日志轨迹、GitHub提交和飞书写入",'
                    '"dissatisfaction_policy":"force_test_unsatisfied",'
                    '"downstream_policy":"test_chain_allowed",'
                    '"trace_gate_policy":"test_exception",'
                    '"notification_policy":"测试通知",'
                    '"flags":["test_run"],"risk_notes":[]}'
                )
                model = "gpt-test"
                wire_api = "responses"

            return Result()

    monkeypatch.setattr("app.services.orchestrator.intent.LLMClient", SparseLLMClient)

    result = start_job(
        StartJobRequest(directions=["弄个单页面，让 Trae 快速回复完毕，我方便测试日志轨迹、GitHub提交和飞书写入"], run_mode="test"),
        user=user,
        db=db,
    )

    intent = result["job"]["intent"]
    assert intent["run_mode"] == "test"
    assert "test_start_button" in intent["flags"]
    assert "skip_trae_self_tests" in intent["flags"]
    assert "chain_validation_only" in intent["flags"]
    assert "single_page_quick" in intent["flags"]
    assert "日志轨迹" in intent["prompt_brief"]


def test_job_directions_split_and_expand_to_100_round_target():
    raw = (
        "1.招聘平台\n"
        "可以投简历\n"
        "可以发招聘信息\n"
        "能够给招聘方和应聘方建立沟通渠道\n"
        "2.中介平台\n"
        "可以在线实名认证\n"
        "用户可以在线匹配下单\n"
        "就从以上两个范围入手吧\n"
        "尽量都弄成前后端分离"
    )

    split = split_direction_text(raw)
    normalized = normalize_job_directions([raw])

    assert len(split) == 2
    assert split[0].startswith("招聘平台：")
    assert "可以投简历" in split[0]
    assert "可以发招聘信息" in split[0]
    assert "中介平台" not in split[0]
    assert "前后端分离" in split[0]
    assert split[1].startswith("中介平台：")
    assert "在线实名认证" in split[1]
    assert "匹配下单" in split[1]
    assert "招聘平台" not in split[1]
    assert "就从以上" not in " ".join(split)
    assert normalized[:2] == split
    assert len(normalized) == 2


def test_job_directions_fold_flat_frontend_items_into_top_level_ranges():
    raw_items = [
        "招聘平台",
        "可以投简历",
        "可以发招聘信息",
        "能够给招聘方和应聘方建立沟通渠道。后续可以展开登录注册",
        "权限",
        "找平台介入",
        "支付。简历评分",
        "智能匹配候选人等",
        "中介平台",
        "可以在线实名认证",
        "说明自己期望做的职业比如保姆",
        "维修工等",
        "用户可以在线匹配下单。后续可以补充担保",
        "支付",
        "平台介入等",
        "就从以上两个范围入手吧",
        "尽量都弄成前后端分离",
    ]

    normalized = normalize_job_directions(raw_items)

    assert len(normalized) == 2
    assert normalized[0].startswith("招聘平台")
    assert "可以投简历" in normalized[0]
    assert "中介平台" not in normalized[0]
    assert normalized[1].startswith("中介平台")
    assert "可以在线实名认证" in normalized[1]
    assert "前后端分离" in normalized[1]
    assert "就从以上两个范围入手吧" not in " ".join(normalized)


def test_start_job_prompt_writer_receives_only_current_direction(monkeypatch):
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id)
    seen_payloads: list[dict] = []

    class Result:
        text = (
            '{"prompt":"招聘平台：先做投简历、发布招聘信息和双方沟通渠道。'
            '做成可运行的业务工作台，包含列表、详情、状态统计、编辑操作和本地模拟数据。",'
            '"prompt_kind":"feature","focus":"招聘平台","acceptance_checks":[],"difference_from_previous":""}'
        )
        model = "gpt-test"
        wire_api = "responses"

    class CapturingLLMClient:
        def complete(self, _config, messages, purpose=""):
            if purpose == "prompt_generation":
                seen_payloads.append(__import__("json").loads(messages[1]["content"]))
            return Result()

    monkeypatch.setattr(prompt_writer, "LLMClient", CapturingLLMClient)

    start_job(
        StartJobRequest(
            directions=[
                (
                    "1.招聘平台\n"
                    "可以投简历\n"
                    "可以发招聘信息\n"
                    "能够给招聘方和应聘方建立沟通渠道\n"
                    "2.中介平台\n"
                    "可以在线实名认证\n"
                    "用户可以在线匹配下单\n"
                    "尽量都弄成前后端分离"
                )
            ]
        ),
        user=user,
        db=db,
    )

    payload = seen_payloads[-1]
    assert payload["directions"] == [payload["current_direction"]]
    assert "招聘平台" in payload["current_direction"]
    assert "投简历" in payload["current_direction"]
    assert "中介平台" not in payload["current_direction"]
    assert "中介平台" not in payload["orchestrator_intent"]["prompt_brief"]
    assert payload["direction_queue"]["remaining_count"] == 1
    assert payload["range_plan"]["current_range"]["title"] == "招聘平台"
    assert payload["range_plan"]["current_range"]["target_rounds"] > 0


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


def test_first_round_prompt_ignores_combined_intent_brief_for_direction_queue():
    db = _test_session()
    user = _create_user(db, "user1")
    job = Job(
        id="job1",
        user_id=user.id,
        status=JobState.PROMPT_READY,
        directions=["招聘平台：投简历、发招聘信息、沟通渠道", "中介平台：实名认证、匹配下单"],
        intent={
            "run_mode": "normal",
            "prompt_brief": "招聘平台和中介平台一起做成双业务综合平台",
        },
    )
    current = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.PROMPT_READY, prompt="")
    db.add_all([job, current])
    db.commit()

    prompt = prompt_writer.build_fallback_prompt(job, current)

    assert "招聘平台" in prompt
    assert "投简历" in prompt
    assert "中介平台" not in prompt
    assert prompt_writer.prompt_quality_error(db, job, current, prompt) == ""


def test_prompt_quality_rejects_prompt_that_merges_queued_directions():
    db = _test_session()
    user = _create_user(db, "user1")
    job = Job(
        id="job1",
        user_id=user.id,
        status=JobState.PROMPT_READY,
        directions=["招聘平台：投简历、发招聘信息、沟通渠道", "中介平台：实名认证、匹配下单"],
    )
    current = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.PROMPT_READY, prompt="")
    db.add_all([job, current])
    db.commit()

    error = prompt_writer.prompt_quality_error(
        db,
        job,
        current,
        "请做一个招聘平台和中介服务平台，可以投简历，也可以在线实名认证后匹配下单。",
    )

    assert error == "prompt_mentions_other_direction:中介平台"


def test_reopen_job_resets_current_job_rounds_counts_and_runtime(monkeypatch):
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id)
    job = Job(
        id="job1",
        user_id=user.id,
        status=JobState.MANUAL_REQUIRED,
        directions=["old scope"],
        submitted_count=7,
        satisfied_count=2,
    )
    project = Project(
        id="project1",
        job_id=job.id,
        name="old-project",
        direction="old scope",
        workspace_path="D:/work/project/old-project",
        status="active",
    )
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        project_id=project.id,
        round_index=4,
        status=JobState.MANUAL_REQUIRED,
        prompt="old prompt",
    )
    log = RuntimeLog(
        id="log1",
        job_id=job.id,
        round_id=round_.id,
        stage="old_stage",
        message="old log",
    )
    attachment = Attachment(
        id="attachment1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        kind="screenshot",
        filename="old.png",
        path="old.png",
    )
    error = AutomationError(
        id="error1",
        job_id=job.id,
        round_id=round_.id,
        kind="old_error",
        stage="old_stage",
        message="old error",
    )
    active_command = WorkerCommand(
        id="active_cmd",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.WAIT_COMPLETION.value,
        payload={},
        status="running",
        lease_id="lease1",
    )
    queued_command = WorkerCommand(
        id="queued_cmd",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.SEND_PROMPT.value,
        payload={},
        status="queued",
    )
    db.add_all([job, project, round_, log, attachment, error, active_command, queued_command])
    db.commit()

    def fake_generate(_db, _user, job_arg, round_arg):
        round_arg.prompt = "new prompt"
        job_arg.status = JobState.PROMPT_READY
        round_arg.status = JobState.PROMPT_READY

    monkeypatch.setattr(jobs_api, "generate_round_prompt", fake_generate)
    monkeypatch.setattr(jobs_api, "dispatch_prompt_to_worker", lambda *_args, **_kwargs: None)

    result = reopen_job(StartJobRequest(directions=["new scope"]), background_tasks=None, user=user, db=db)

    db.refresh(job)
    new_rounds = list(db.scalars(select(TaskRound).where(TaskRound.job_id == job.id)).all())
    stop_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.STOP_CURRENT_TASK.value))
    reset_log = db.scalar(select(RuntimeLog).where(RuntimeLog.job_id == job.id, RuntimeLog.stage == JobState.CLEANING_OLD_RUNTIME))
    active_after = db.get(WorkerCommand, "active_cmd")

    assert result["job"]["id"] == job.id
    assert job.directions[0] == "new scope"
    assert len(job.directions) == 1
    assert job.submitted_count == 0
    assert job.satisfied_count == 0
    assert len(new_rounds) == 1
    assert new_rounds[0].round_index == 1
    assert new_rounds[0].prompt == "new prompt"
    assert db.scalar(select(Project).where(Project.id == "project1")) is None
    assert db.scalar(select(RuntimeLog).where(RuntimeLog.id == "log1")) is None
    assert db.scalar(select(Attachment).where(Attachment.id == "attachment1")) is None
    assert db.scalar(select(AutomationError).where(AutomationError.id == "error1")) is None
    assert db.scalar(select(WorkerCommand).where(WorkerCommand.id == "queued_cmd")) is None
    assert active_after is not None
    assert active_after.status == "cancelled"
    assert active_after.job_id is None
    assert active_after.round_id is None
    assert stop_command is not None
    assert stop_command.job_id == job.id
    assert stop_command.round_id is None
    assert stop_command.payload["reason"] == "user_reopen"
    assert stop_command.payload["project_name"] == "old-project"
    assert stop_command.payload["workspace_path"] == "D:/work/project/old-project"
    assert reset_log is not None
    assert reset_log.extra["old_rounds"] == 1
    assert reset_log.extra["cancelled_active_commands"] == 1


def test_stop_job_queues_workspace_cleanup_payload():
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id, workspace_path="D:/mr-d", browser_url="http://localhost:5173")
    job = Job(id="job1", user_id=user.id, status=JobState.WAITING_TRAE, directions=["demo"])
    project = Project(
        id="project1",
        job_id=job.id,
        name="roles-dashboard",
        direction="demo",
        workspace_path="D:/mr-d/roles-dashboard",
    )
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        project_id=project.id,
        round_index=1,
        status=JobState.WAITING_TRAE,
    )
    db.add_all([job, project, round_])
    db.commit()

    result = stop_job(user=user, db=db)

    stop_command = db.scalar(select(WorkerCommand).where(WorkerCommand.command_type == WorkerCommandType.STOP_CURRENT_TASK.value))
    assert result["job"]["status"] == JobState.PAUSED
    assert "等待停止确认" in result["message"]
    assert stop_command is not None
    assert stop_command.payload["reason"] == "user_stop"
    assert stop_command.payload["use_ai_ui_analyst"] is True
    assert stop_command.payload["project_name"] == "roles-dashboard"
    assert stop_command.payload["workspace_path"] == "D:/mr-d/roles-dashboard"
    assert stop_command.payload["browser_url"] == "http://localhost:5173"
    assert stop_command.payload["url"] == "http://localhost:5173"


def test_stop_job_cancels_active_work_and_leaves_stop_command_queued():
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id, workspace_path="D:/mr-d")
    job = Job(id="job1", user_id=user.id, status=JobState.WAITING_TRAE, directions=["demo"])
    project = Project(
        id="project1",
        job_id=job.id,
        name="roles-dashboard",
        direction="demo",
        workspace_path="D:/mr-d/roles-dashboard",
    )
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        project_id=project.id,
        round_index=1,
        status=JobState.WAITING_TRAE,
    )
    active = WorkerCommand(
        id="active-wait",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.WAIT_COMPLETION.value,
        payload={"workspace_path": "D:/mr-d/roles-dashboard"},
        status="running",
    )
    db.add_all([job, project, round_, active])
    db.commit()

    stop_job(user=user, db=db)

    db.refresh(active)
    stop_command = db.scalar(
        select(WorkerCommand)
        .where(WorkerCommand.command_type == WorkerCommandType.STOP_CURRENT_TASK.value)
        .order_by(WorkerCommand.created_at.desc())
        .limit(1)
    )
    assert active.status == "cancelled"
    assert stop_command is not None
    assert stop_command.status == "queued"
    assert stop_command.payload["reason"] == "user_stop"
    assert stop_command.payload["use_ai_ui_analyst"] is True
    assert stop_command.payload["project_name"] == "roles-dashboard"
    assert stop_command.payload["workspace_path"] == "D:/mr-d/roles-dashboard"


def test_stop_job_queues_stop_for_stale_worker_that_is_running_active_command():
    db = _test_session()
    user = _create_user(db, "user1")
    worker = _create_worker(db, user.id)
    worker.last_seen_at = now_utc() - timedelta(minutes=10)
    _save_required_settings(db, user.id, workspace_path="D:/mr-d")
    job = Job(id="job1", user_id=user.id, status=JobState.WAITING_TRAE, directions=["demo"])
    project = Project(
        id="project1",
        job_id=job.id,
        name="roles-dashboard",
        direction="demo",
        workspace_path="D:/mr-d/roles-dashboard",
    )
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        project_id=project.id,
        round_index=1,
        status=JobState.WAITING_TRAE,
    )
    active = WorkerCommand(
        id="active-wait",
        worker_id=worker.worker_id,
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.WAIT_COMPLETION.value,
        payload={"workspace_path": "D:/mr-d/roles-dashboard"},
        status="running",
    )
    db.add_all([job, project, round_, active])
    db.commit()

    stop_job(user=user, db=db)

    db.refresh(active)
    stop_command = db.scalar(
        select(WorkerCommand)
        .where(WorkerCommand.command_type == WorkerCommandType.STOP_CURRENT_TASK.value)
        .order_by(WorkerCommand.created_at.desc())
        .limit(1)
    )
    assert active.status == "cancelled"
    assert stop_command is not None
    assert stop_command.worker_id == worker.worker_id
    assert stop_command.status == "queued"
    assert stop_command.payload["workspace_path"] == "D:/mr-d/roles-dashboard"


def test_continue_paused_job_requeues_cancelled_worker_command():
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id, browser_url="http://localhost:5173", workspace_path="D:/mr-d")
    job = Job(id="job1", user_id=user.id, status=JobState.PAUSED, directions=["demo"], scope_text="demo")
    project = Project(
        id="project1",
        job_id=job.id,
        name="demo-project",
        direction="demo",
        workspace_path="D:/mr-d/demo-project",
    )
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        project_id=project.id,
        round_index=1,
        status=JobState.PAUSED,
        prompt="demo prompt",
    )
    previous = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.WAIT_COMPLETION.value,
        payload={"workspace_path": "D:/old/demo"},
        status="cancelled",
        message="Cancelled by user stop.",
    )
    db.add_all([job, project, round_, previous])
    db.commit()

    result = jobs_api.continue_job(user=user, db=db)

    new_command = db.scalar(
        select(WorkerCommand)
        .where(WorkerCommand.id != previous.id)
        .order_by(WorkerCommand.created_at.desc())
        .limit(1)
    )
    assert result["job"]["status"] == JobState.WAITING_TRAE
    assert new_command is not None
    assert new_command.command_type == WorkerCommandType.DIAGNOSE_UI.value
    assert new_command.status == "queued"
    assert new_command.payload["retry_of_command_id"] == previous.id
    assert new_command.payload["previous_command_type"] == WorkerCommandType.WAIT_COMPLETION.value
    assert new_command.payload["resume_previous_payload"]["workspace_path"] == "D:/old/demo"
    assert new_command.payload["workspace_path"] == "D:/mr-d/demo-project"
    assert new_command.payload["trae_workspace_path"] == "D:/mr-d/demo-project"
    db.refresh(round_)
    assert round_.status == JobState.WAITING_TRAE


def test_continue_after_trae_stop_queues_resume_prompt_before_wait():
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id, browser_url="http://localhost:5173", workspace_path="D:/mr-d")
    job = Job(
        id="job1",
        user_id=user.id,
        status=JobState.PAUSED,
        directions=["demo"],
        scope_text="demo",
        intent={"run_mode": "test", "flags": ["test_run"]},
    )
    project = Project(
        id="project1",
        job_id=job.id,
        name="demo-project",
        direction="demo",
        workspace_path="D:/mr-d/demo-project",
    )
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        project_id=project.id,
        round_index=1,
        status=JobState.PAUSED,
        prompt="demo prompt",
    )
    previous = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.WAIT_COMPLETION.value,
        payload={"workspace_path": "D:/old/demo"},
        status="cancelled",
        message="Cancelled by user stop.",
    )
    stop_command = WorkerCommand(
        id="stop1",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.STOP_CURRENT_TASK.value,
        payload={"reason": "user_stop"},
        status="completed",
        result={
            "status": "success",
            "data": {
                "stop_report": {
                    "trae_stop_clicked": True,
                    "requires_resume_prompt": True,
                    "sandbox_killed": 2,
                }
            },
        },
    )
    db.add_all([job, project, round_, previous, stop_command])
    db.commit()

    result = jobs_api.continue_job(user=user, db=db)

    new_command = db.scalar(
        select(WorkerCommand)
        .where(WorkerCommand.id.notin_([previous.id, stop_command.id]))
        .order_by(WorkerCommand.created_at.desc())
        .limit(1)
    )
    assert result["job"]["status"] == JobState.WAITING_TRAE
    assert new_command is not None
    assert new_command.command_type == WorkerCommandType.DIAGNOSE_UI.value
    assert new_command.payload["resume_after_stop"] is True
    assert new_command.payload["retry_of_command_id"] == previous.id
    assert new_command.payload["stop_command_id"] == stop_command.id
    assert new_command.payload["previous_command_type"] == WorkerCommandType.WAIT_COMPLETION.value
    assert new_command.payload["workspace_path"] == "D:/mr-d/demo-project"
    db.refresh(round_)
    assert round_.status == JobState.WAITING_TRAE
    return
    assert "继续刚才被暂停的测试任务" in new_command.payload["prompt"]
    assert "npm.cmd" in new_command.payload["prompt"]
    assert "npm_config_cache" in new_command.payload["prompt"]
    assert ".npm-cache" in new_command.payload["prompt"]
    assert new_command.payload["workspace_path"] == "D:/mr-d/demo-project"
    db.refresh(round_)
    assert round_.status == JobState.SENDING_TO_WORKER


def test_continue_after_pause_with_missing_trae_window_reopens_workspace_first():
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id, browser_url="http://localhost:5173", workspace_path="D:/mr-d")
    job = Job(id="job1", user_id=user.id, status=JobState.PAUSED, directions=["demo"], scope_text="demo")
    project = Project(
        id="project1",
        job_id=job.id,
        name="demo-project",
        direction="demo",
        workspace_path="D:/mr-d/demo-project",
    )
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        project_id=project.id,
        round_index=1,
        status=JobState.PAUSED,
        prompt="demo prompt",
    )
    previous = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.WAIT_COMPLETION.value,
        payload={"workspace_path": "D:/old/demo"},
        status="cancelled",
        message="Cancelled by user stop.",
    )
    stop_command = WorkerCommand(
        id="stop1",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.STOP_CURRENT_TASK.value,
        payload={"reason": "user_stop"},
        status="completed",
        result={
            "status": "success",
            "data": {
                "stop_report": {
                    "stop_confirmed": True,
                    "trae_stop_clicked": False,
                    "trae_stop_click": {"status": "not_clicked", "error": "Trae window was not found"},
                }
            },
        },
    )
    db.add_all([job, project, round_, previous, stop_command])
    db.commit()

    result = jobs_api.continue_job(user=user, db=db)

    new_command = db.scalar(
        select(WorkerCommand)
        .where(WorkerCommand.id.notin_([previous.id, stop_command.id]))
        .order_by(WorkerCommand.created_at.desc())
        .limit(1)
    )
    assert result["job"]["status"] == JobState.WAITING_TRAE
    assert new_command is not None
    assert new_command.command_type == WorkerCommandType.DIAGNOSE_UI.value
    assert new_command.payload["previous_command_type"] == WorkerCommandType.WAIT_COMPLETION.value
    assert new_command.payload["workspace_path"] == "D:/mr-d/demo-project"


def test_continue_after_pause_reads_top_level_stop_report_for_missing_trae_window():
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id, browser_url="http://localhost:5173", workspace_path="D:/mr-d")
    job = Job(id="job1", user_id=user.id, status=JobState.PAUSED, directions=["demo"], scope_text="demo")
    project = Project(
        id="project1",
        job_id=job.id,
        name="demo-project",
        direction="demo",
        workspace_path="D:/mr-d/demo-project",
    )
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        project_id=project.id,
        round_index=1,
        status=JobState.PAUSED,
        prompt="demo prompt",
    )
    previous = WorkerCommand(
        id="cmd1",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.WAIT_COMPLETION.value,
        payload={"workspace_path": "D:/old/demo"},
        status="cancelled",
    )
    stop_command = WorkerCommand(
        id="stop1",
        worker_id="worker1",
        user_id=user.id,
        job_id=job.id,
        round_id=round_.id,
        command_type=WorkerCommandType.STOP_CURRENT_TASK.value,
        payload={"reason": "user_stop"},
        status="completed",
        result={
            "stopped": True,
            "stop_report": {
                "stop_confirmed": True,
                "trae_stop_clicked": False,
                "trae_stop_click": {"status": "not_clicked", "error": "Trae window was not found"},
            },
        },
    )
    db.add_all([job, project, round_, previous, stop_command])
    db.commit()

    jobs_api.continue_job(user=user, db=db)

    new_command = db.scalar(
        select(WorkerCommand)
        .where(WorkerCommand.id.notin_([previous.id, stop_command.id]))
        .order_by(WorkerCommand.created_at.desc())
        .limit(1)
    )
    assert new_command is not None
    assert new_command.command_type == WorkerCommandType.DIAGNOSE_UI.value
    assert new_command.payload["previous_command_type"] == WorkerCommandType.WAIT_COMPLETION.value


def test_reopen_job_with_background_task_returns_before_prompt_generation(monkeypatch):
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id)
    job = Job(
        id="job1",
        user_id=user.id,
        status=JobState.MANUAL_REQUIRED,
        directions=["old scope"],
    )
    round_ = TaskRound(
        id="round1",
        job_id=job.id,
        round_index=3,
        status=JobState.MANUAL_REQUIRED,
        prompt="old prompt",
    )
    db.add_all([job, round_])
    db.commit()

    class FakeBackgroundTasks:
        def __init__(self):
            self.tasks = []

        def add_task(self, func, *args, **kwargs):
            self.tasks.append((func, args, kwargs))

    background = FakeBackgroundTasks()
    calls = {"generate": 0}

    def fake_generate(*_args, **_kwargs):
        calls["generate"] += 1
        raise AssertionError("prompt generation should be deferred")

    monkeypatch.setattr(jobs_api, "generate_round_prompt", fake_generate)

    result = reopen_job(
        StartJobRequest(directions=["new scope"]),
        background_tasks=background,
        user=user,
        db=db,
    )

    db.refresh(job)
    new_round = db.scalar(select(TaskRound).where(TaskRound.job_id == job.id))
    assert calls["generate"] == 0
    assert len(background.tasks) == 1
    assert background.tasks[0][0] is jobs_api.generate_and_dispatch_reopened_round
    assert background.tasks[0][1] == (user.id, job.id, new_round.id)
    assert result["status"] == JobState.GENERATING_PROMPT
    assert result["message"] == "Reopen reset complete; prompt generation is running in the background."
    assert result["job"]["scope_text"] == "new scope"
    assert new_round.prompt == ""


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


def test_followup_fallback_uses_d_drive_bugfix_target_without_meta_language():
    db = _test_session()
    user = _create_user(db, "user1")
    job = Job(id="job1", user_id=user.id, status=JobState.PROMPT_READY, directions=["订单管理平台：客户、订单、售后"])
    current = TaskRound(id="round2", job_id=job.id, round_index=2, status=JobState.PROMPT_READY, prompt="")
    db.add_all([job, current])
    db.commit()

    prompt = prompt_writer.build_followup_fallback_prompt(
        job,
        current,
        "产物不满意：订单保存按钮没有接上点击处理。\n过程不满意：没有说明入口点击、状态变化和失败提示。",
    )

    assert "按钮" in prompt or "点击" in prompt
    assert "保存" in prompt
    assert "产物不满意" not in prompt
    assert "过程不满意" not in prompt
    assert "不满意原因" not in prompt


def test_agentops_first_round_fallback_keeps_business_github_feishu_trace_abilities():
    db = _test_session()
    user = _create_user(db, "user1")
    job = Job(
        id="job1",
        user_id=user.id,
        status=JobState.PROMPT_READY,
        directions=["AgentOps 多角色 LLM + Windows Worker 自动作业平台：提示发送、底部日志复制、GitHub提交和飞书预览闭环"],
    )
    current = TaskRound(id="round1", job_id=job.id, round_index=1, status=JobState.PROMPT_READY, prompt="")
    db.add_all([job, current])
    db.commit()

    prompt = prompt_writer.build_fallback_prompt(job, current)

    assert "底部日志复制" in prompt
    assert "GitHub" in prompt
    assert "飞书" in prompt
    assert prompt_writer.prompt_quality_error(db, job, current, prompt) == ""


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
        "trae_exe_path": "D:/app/Trae CN/Trae CN.exe",
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
    assert "force_open_workspace" not in command.payload
    assert command.payload["open_new_task"] is True
    assert command.payload["verify_submission"] is True
    assert command.payload["strict_submission_verification"] is True
    assert command.payload["submission_timeout_seconds"] == 30


def test_worker_dispatch_continues_same_trae_task_after_first_round():
    db = _test_session()
    user = _create_user(db, "user1")
    _create_worker(db, user.id)
    _save_required_settings(db, user.id, browser_url="http://localhost:5173", workspace_path="D:/mr-d")
    job = Job(id="job1", user_id=user.id, status=JobState.PROMPT_READY, directions=["demo"])
    project = Project(
        id="project1",
        job_id=job.id,
        name="demo-project",
        direction="demo",
        workspace_path="D:/mr-d/demo-project",
        status="active",
    )
    round_ = TaskRound(
        id="round2",
        job_id=job.id,
        project_id=project.id,
        round_index=2,
        status=JobState.PROMPT_READY,
        prompt="continue prompt",
    )
    db.add_all([job, project, round_])
    db.commit()

    command = dispatch_prompt_to_worker(db, user, job, round_)

    assert command.payload["round_index"] == 2
    assert command.payload["open_new_task"] is False
    assert command.payload["trae_workspace_path"] == "D:/mr-d/demo-project"


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


def test_retry_send_prompt_command_does_not_open_new_trae_task():
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
        command_type=WorkerCommandType.SEND_PROMPT.value,
        payload={"prompt": "demo", "open_new_task": True, "trae_workspace_path": "D:/old"},
        status="failed",
        error="submission unconfirmed",
    )
    db.add(previous)
    db.commit()

    retry_worker_command(user=user, db=db)

    new_command = db.scalar(
        select(WorkerCommand)
        .where(WorkerCommand.id != previous.id)
        .order_by(WorkerCommand.created_at.desc())
        .limit(1)
    )
    assert new_command is not None
    assert new_command.command_type == WorkerCommandType.SEND_PROMPT.value
    assert new_command.payload["retry_of_command_id"] == previous.id
    assert new_command.payload["open_new_task"] is False
    assert new_command.payload["open_new_task_suppressed_reason"] == "manual_retry"
    assert new_command.payload["trae_workspace_path"] == "D:/mr-d"


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
    trae_exe_path: str = "D:/app/Trae CN/Trae CN.exe",
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
                "trae_exe_path": trae_exe_path,
                "trae_workspace_path": workspace_path,
                "browser_url": browser_url,
            },
        },
    )
    db.commit()
