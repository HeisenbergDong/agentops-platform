import hashlib
import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, RuntimeLog, TaskRound, User
from app.db.repositories.jobs import add_log
from app.db.repositories.roles import get_user_role
from app.db.repositories.user_rules import read_user_rule_many
from app.services.llm import LLMClient, LLMError, model_config_from_settings
from app.services.orchestrator.states import JobState
from app.services.user_settings import load_user_settings


class PromptGenerationError(RuntimeError):
    pass


STACKS = ("Python", "Go", "Vue", "Java")
FORBIDDEN_PROMPT_PHRASES = (
    "产物不满意",
    "过程不满意",
    "结果不满意",
    "不满意原因",
    "证据：",
    "证据:",
    "关键证据",
    "判定依据",
    "上一轮我主要不满意",
    "你现在在 Trae CN",
    "你现在在Trae CN",
    "AgentOps 自动作业",
    "AgentOps自动作业",
    "平台侧 LLM",
    "工具调用",
    "watcher",
    "LLM",
    "AI 判断",
)
REUSED_PROMPT_PATTERNS = (
    "先修上一轮验收里暴露的问题",
    "基于现有产物再做一次验收修补",
    "优先修复上次审查中发现的具体问题",
    "类似问题按同类处理",
    "缺少真实日志轨迹",
)
FORBIDDEN_PROMPT_REGEXES = (
    re.compile(r"你现在.{0,12}trae\s*cn", re.IGNORECASE),
    re.compile(r"\btrae\s*cn\b", re.IGNORECASE),
    re.compile(r"调度(角色|器|流程)|平台侧|自动作业流程"),
    re.compile(r"关键证据|验收证据|不满意原因|产物不满意|过程不满意|结果不满意"),
    re.compile(r"(日志|轨迹|扫描|工具调用|watcher|trace|session)", re.IGNORECASE),
)
FIRST_ROUND_TEMPLATE_PREFIXES = (
    "按这个项目方向做一个能继续迭代的系统雏形",
    "基于这个项目方向做一个能继续迭代的系统雏形",
)
PROMPT_WRITER_RETRY_LIMIT = 1
CONCISE_FAST_SCOPE_NOTE = "这轮保持小范围，把改动和建议复查方式说清楚即可。"
PROMPT_WRITER_SYSTEM = """你是自动化作业里的“提示词策略员”。你不能直接写飞书，不能决定作业完成，只负责给编码助手的本轮或下一轮用户提示词提案。
要求：
1. 你是长期任务设计角色，不是简单改写器。调度会告诉你当前范围、轮次、模式和范围计划，你只为当前范围生成当前轮 Trae prompt。
2. 用户给多个范围时，每个范围是独立项目；你只能写 current_direction/current_range，不能合并其他 queued directions，也不能提前写下一个范围。同一范围可以分多组 1-5 轮在同一个项目里续作，新组第一轮是新 Trae 会话但不是重搭项目。
3. 任务粒度必须中等：一个明确业务模块、两到四个相关区域、三到六个可验证交互、本地模拟数据或简单接口、多文件结构，并保留清楚的运行入口和复查路径。
4. 任务不能太小：不能只是改标题、颜色、按钮文案、单字段、README，不能小到单文件静态页。
5. 任务不能太大：不能一次要求完整商业平台、前后台移动端全套、过多模块，避免 Trae 迟迟做不完。
6. 首轮要做可运行业务骨架：列表、详情、操作入口、统计状态、本地模拟数据，不能是说明页或单文件 demo。
7. 后续轮要基于现有项目继续，不重搭，不重复修同一个 bug；同类问题连续修过两次后，应改做相关业务路径扩展或提示调度考虑切换范围。
8. 上一轮满意时，不硬找问题，选择当前范围 module_map 里未覆盖或覆盖不足的下一个自然模块继续扩展。
9. 上一轮不满意时，把用户可见问题转换成自然开发要求，不复述原文，不写内部工具问题。
10. 不能在提示词里出现“产物不满意”“过程不满意”“结果不满意”“不满意原因”“证据：”“日志”“轨迹”“扫描”“工具调用”“watcher”“trace”“session”“LLM”“AI”。
11. 提示词要像真实用户给开发助手的需求：明确现象、期望结果和必要复查路径，但不要套固定交付模板。
12. 如果上一轮问题是内部证据不足、轨迹缺失、GitHub/飞书链路失败，除非当前产品本身就是 AgentOps，否则不要让编码助手修这些内部链路，而要回到当前业务系统的可验证交互或工程交付。
13. 调度会提供 prompt_delivery_policy。只有当 policy 明确要求 fast_scope 或 command_note 时，才用一句自然话轻轻收住范围；不要默认写“执行边界”、不要默认禁止测试/构建/打开页面。
14. 避免统一话术。不要每轮固定要求“入口文件、主要目录结构、建议运行命令、默认访问路径”等长清单；只写当前任务真正相关的交付说明。
只输出 JSON：{"prompt": "...", "prompt_kind": "bugfix|feature|workflow|edge_case|engineering|closure", "focus": "...", "acceptance_checks": ["..."], "difference_from_previous": "...", "used_module": "...", "should_scheduler_consider_switch": false, "reason": "..."}"""

PROMPT_WRITER_SYSTEM += """

AgentOps 流程上下文：
- 平台会在 Trae 完成后处理执行轨迹采集、GitHub 提交和飞书写入，这些不是发给 Trae 的需求内容。
- 发给 Trae 的提示词必须像普通用户的开发需求，不要写成内部调度指令。
- 如果本轮是暂停后的继续，请让 Trae 从中断点继续，保留已有文件和结构，避免从零重做。
- 不要提到内部轨迹门禁、Worker 停止报告、调度状态或证据提交，除非正在开发的产品本身就是 AgentOps 且这些是真实产品功能。
- 不要把一次性的运行/测试限制当成每轮模板。普通任务优先写需求本身；需要收范围时，只用自然短句。
- Windows PowerShell 的 Node.js 命令提示只在 policy 要求且 prompt 里确实涉及 npm/npx/pnpm/yarn/vite 等命令时出现。
""".strip()


def generate_round_prompt(db: Session, user: User, job: Job, round_: TaskRound) -> str:
    intent = job.intent if isinstance(job.intent, dict) else {}
    if intent.get("run_mode") == "test":
        prompt = build_fallback_prompt(job, round_, {}, "test mode fast path")
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="prompt_generation_fallback",
            message="Test mode uses deterministic prompt fallback for a fast chain check.",
            level="warning",
            extra={"prompt_chars": len(prompt), "test_mode_fast_path": True},
        )
        return _store_prompt(db, job, round_, prompt, model="built-in-test-fallback", wire_api="local")

    role = get_user_role(db, user.id, "prompt_writer")
    rules: dict[str, str] = {}
    role_name = "提示词策略员"
    role_purpose = "为本轮 Trae 执行生成普通用户口吻的项目需求。"
    role_model_config_key = "default"
    if role:
        role_name = role.name
        role_purpose = role.purpose
        role_model_config_key = role.model_config_key
        try:
            rules = read_user_rule_many(db, user.id, role.rules)
        except FileNotFoundError as exc:
            raise PromptGenerationError(f"Rule not found: {exc.args[0]}") from exc
    else:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="prompt_generation_fallback",
            message="Prompt writer role is missing; using built-in prompt fallback.",
            level="warning",
        )

    previous_reason = _previous_dissatisfaction(db, job, round_)
    current_direction = _current_direction(job)
    visible_directions = _visible_directions_for_prompt(job)
    prompt_intent = _prompt_writer_intent(job, current_direction)
    delivery_policy = _prompt_delivery_policy(job, round_)
    state = _compact_prompt_state(db, job, round_, previous_reason)
    current = _current_task(job, round_, previous_reason)
    meta = _prompt_meta(job, round_)
    messages = [
        {
            "role": "system",
            "content": (
                PROMPT_WRITER_SYSTEM
                + "\n\n"
                f"平台当前角色名：{role_name}。\n"
                f"角色目标：{role_purpose}\n"
                f"用户规则：\n{_format_rules(rules)}"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "role": "prompt_writer",
                    "hard_rules": {
                        "must_keep_current_project": True,
                        "formal_write_requires_real_trace": True,
                        "first_round_must_be_operable_system": True,
                        "must_not_leak_internal_evidence_terms": True,
                        "must_use_only_current_direction": True,
                        "must_not_merge_other_queued_directions": True,
                        "must_generate_medium_sized_tasks": True,
                        "must_not_repeat_same_bug_loop": True,
                        "satisfied_round_should_expand_next_module": True,
                    },
                    "prompt_delivery_policy": delivery_policy,
                    "state": state,
                    "current": current,
                    "meta": meta,
                    "orchestrator_intent": prompt_intent,
                    "user_rules": rules,
                    "current_direction": current_direction,
                    "directions": visible_directions,
                    "direction_queue": _direction_queue_meta(job),
                    "range_plan": _range_plan_meta(job),
                    "preferred_stack": _select_stack(job, round_),
                },
                ensure_ascii=False,
            ),
        },
    ]
    llm_client = LLMClient()
    llm_config = model_config_from_settings(load_user_settings(db, user.id), role_model_config_key)
    try:
        result = llm_client.complete(llm_config, messages, purpose="prompt_generation")
    except LLMError as exc:
        prompt = build_fallback_prompt(job, round_, rules, previous_reason or str(exc))
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="prompt_generation_fallback",
            message="LLM prompt writer failed; built-in prompt fallback will be sent to worker.",
            level="warning",
            extra={"error": str(exc), "prompt_chars": len(prompt)},
        )
        return _store_prompt(db, job, round_, prompt, model="built-in-fallback", wire_api="local")

    proposal, prompt, prompt_kind, quality_error = _prompt_candidate_from_result(db, job, round_, result)
    if quality_error:
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="prompt_generation_retry",
            message="Prompt writer output failed the quality gate; scheduler is asking the role to regenerate.",
            level="warning",
            extra={
                "quality_error": quality_error,
                "quality_reason": _quality_error_explanation(quality_error),
                "attempt": 1,
                "max_attempts": PROMPT_WRITER_RETRY_LIMIT,
                "rejected_prompt_preview": prompt[:500],
            },
        )
        retry_messages = [
            *messages,
            {
                "role": "user",
                "content": json.dumps(
                    _prompt_quality_feedback_payload(job, round_, prompt, quality_error),
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            retry_result = llm_client.complete(llm_config, retry_messages, purpose="prompt_generation_retry")
        except LLMError as exc:
            fallback = build_fallback_prompt(job, round_, rules, previous_reason or str(exc))
            add_log(
                db,
                job_id=job.id,
                round_id=round_.id,
                stage="prompt_generation_fallback",
                message="Prompt writer retry failed; built-in prompt fallback will be sent to worker.",
                level="warning",
                extra={
                    "error": str(exc),
                    "quality_error": quality_error,
                    "original_quality_error": quality_error,
                    "prompt_chars": len(fallback),
                },
            )
            return _store_prompt(db, job, round_, fallback, model="built-in-fallback", wire_api="local")

        retry_proposal, retry_prompt, retry_prompt_kind, retry_quality_error = _prompt_candidate_from_result(
            db,
            job,
            round_,
            retry_result,
        )
        if retry_prompt and not retry_quality_error:
            return _store_prompt(
                db,
                job,
                round_,
                retry_prompt,
                model=retry_result.model,
                wire_api=retry_result.wire_api,
                extra={
                    "prompt_kind": retry_prompt_kind,
                    "llm_prompt_writer": {
                        "focus": retry_proposal.get("focus") or "",
                        "acceptance_checks": retry_proposal.get("acceptance_checks") or [],
                        "difference_from_previous": retry_proposal.get("difference_from_previous") or "",
                    },
                    "prompt_retry": {
                        "attempts": 1,
                        "original_quality_error": quality_error,
                        "original_quality_reason": _quality_error_explanation(quality_error),
                    },
                },
            )

        fallback_reason = retry_quality_error or "Prompt writer retry returned empty prompt"
        prompt = build_fallback_prompt(job, round_, rules, previous_reason or fallback_reason)
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="prompt_generation_fallback",
            message="Prompt writer retry did not pass the quality gate; built-in prompt fallback will be sent to worker.",
            level="warning",
            extra={
                "quality_error": retry_quality_error or quality_error,
                "original_quality_error": quality_error,
                "retry_quality_error": retry_quality_error,
                "retry_prompt_empty": not bool(retry_prompt),
                "prompt_chars": len(prompt),
                "retry_prompt_preview": retry_prompt[:500] if retry_prompt else "",
            },
        )
        return _store_prompt(db, job, round_, prompt, model="built-in-fallback", wire_api="local")
    return _store_prompt(
        db,
        job,
        round_,
        prompt,
        model=result.model,
        wire_api=result.wire_api,
        extra={
            "prompt_kind": prompt_kind,
            "llm_prompt_writer": {
                "focus": proposal.get("focus") or "",
                "acceptance_checks": proposal.get("acceptance_checks") or [],
                "difference_from_previous": proposal.get("difference_from_previous") or "",
            },
        },
    )


def _prompt_candidate_from_result(
    db: Session,
    job: Job,
    round_: TaskRound,
    result,
) -> tuple[dict, str, str, str]:
    proposal = _parse_prompt_writer_result(result.text)
    prompt = _naturalize_prompt(str(proposal.get("prompt") or result.text or ""))
    prompt_kind = str(proposal.get("prompt_kind") or ("feature" if _is_new_project_first_round(round_) else "bugfix")).strip().lower()
    if prompt_kind not in {"bugfix", "feature", "workflow", "edge_case", "engineering", "closure"}:
        prompt_kind = "feature"
    if not prompt:
        return proposal, "", prompt_kind, "prompt_empty"
    prompt = _soften_prompt_repetition(db, job, round_, prompt, prompt_kind)
    return proposal, prompt, prompt_kind, prompt_quality_error(db, job, round_, prompt)


def _prompt_quality_feedback_payload(job: Job, round_: TaskRound, rejected_prompt: str, quality_error: str) -> dict:
    return {
        "type": "quality_gate_rejection",
        "role": "prompt_writer",
        "quality_error": quality_error,
        "quality_reason": _quality_error_explanation(quality_error),
        "rejected_prompt": rejected_prompt,
        "required_action": (
            "请根据 quality_reason 重新生成一个完全合格的 JSON。只输出 JSON，不要解释。"
            "新 prompt 必须只围绕 current_direction，避开 queued directions、内部调度词、质量门禁止词，"
            "保持中等任务粒度，并像普通用户给开发助手的需求。"
            "不要把执行边界、测试限制、命令提示或页面路径写成固定模板；只有 prompt_delivery_policy 明确要求时才自然带一句。"
        ),
        "current_direction": _current_direction(job),
        "prompt_delivery_policy": _prompt_delivery_policy(job, round_),
        "direction_queue": _direction_queue_meta(job),
        "round_index": round_.round_index,
    }


def _quality_error_explanation(quality_error: str) -> str:
    if quality_error.startswith("prompt_mentions_other_direction:"):
        target = quality_error.split(":", 1)[1]
        return f"提示词提到了非当前范围的内容：{target}。只能围绕 current_direction，不要提前合并后续范围或扩展能力。"
    if quality_error.startswith("prompt_contains_meta_phrase:"):
        target = quality_error.split(":", 1)[1]
        return f"提示词包含内部判定或质量反馈用语：{target}。要改成普通用户需求口吻。"
    if quality_error.startswith("prompt_contains_internal_process:"):
        return "提示词包含平台内部流程、日志、轨迹、扫描、工具调用等调度词。要只描述用户可见产品需求。"
    if quality_error.startswith("prompt_reuses_template_phrase:"):
        target = quality_error.split(":", 1)[1]
        return f"提示词复用了不适合直接发给开发助手的模板句：{target}。需要换成自然业务需求。"
    if quality_error.startswith("prompt_contains_operational_boilerplate:"):
        target = quality_error.split(":", 1)[1]
        return f"提示词出现了模板化执行边界或通用验收话术：{target}。请只保留当前任务相关要求，用自然用户口吻重写。"
    explanations = {
        "first_round_template_prefix": "第一轮提示词不能套用过泛模板，要直接给出当前业务范围的可操作系统骨架。",
        "first_round_too_small": "第一轮任务太小，不能只是单页、小 demo 或极简改动。",
        "prompt_too_small": "提示词任务粒度太小，需要扩展到可验证的业务路径或模块。",
        "prompt_too_short": "提示词过短，缺少可执行范围和验收方式。",
        "prompt_reuses_last_dissatisfaction_phrase": "提示词直接复述了上一轮不满意原因，需要转换成自然开发要求。",
        "prompt_duplicate_recent": "提示词和最近一轮重复，需要换一个自然模块或明确不同改动点。",
        "prompt_too_similar_to_recent": "提示词和最近历史太相似，需要避免重复循环。",
        "prompt_empty": "提示词角色没有返回可用 prompt。",
    }
    for key, explanation in explanations.items():
        if quality_error.startswith(key):
            return explanation
    return "提示词没有通过质量门，需要按当前范围重新生成。"


def build_fallback_prompt(
    job: Job,
    round_: TaskRound,
    rules: dict[str, str] | None = None,
    fallback_reason: str = "",
) -> str:
    directions = _visible_directions_for_prompt(job)
    if round_.round_index > 1 or _is_existing_project_new_interaction(round_):
        return build_followup_fallback_prompt(job, round_, fallback_reason)
    intent = job.intent if isinstance(job.intent, dict) else {}
    prompt_brief = str(intent.get("prompt_brief") or "").strip()
    direction = directions[0] if directions else (prompt_brief or "做一个方便后续继续迭代的业务系统。")
    smoke_source = prompt_brief if intent.get("run_mode") == "test" and prompt_brief else direction
    smoke_prompt = _test_smoke_prompt(smoke_source, intent)
    if smoke_prompt:
        return smoke_prompt
    task = _build_direction_task(direction, _select_stack(job, round_))
    prompt = _naturalize_prompt(str(task["base"]))
    quality_error = prompt_quality_error(None, job, round_, prompt)
    if not quality_error:
        return prompt
    direction_text = "\n".join(f"{index}. {item}" for index, item in enumerate(directions, start=1))
    if not direction_text:
        direction_text = "1. 做一个方便后续继续迭代的业务系统。"
    stack = _select_stack(job, round_)
    project_hint = _project_hint(directions, stack)
    return _structured_prompt(
        topic=project_hint,
        stack=stack,
        objective=f"围绕下面这个范围先做一个能继续迭代的业务项目：{direction_text}",
        module="核心工作台和第一条业务主流程",
        interactions=[
            "列表和详情能联动，选择不同记录后右侧或详情区状态同步变化",
            "新增或编辑入口能完成本地保存，并刷新列表、统计或状态标签",
            "至少处理一个空状态、一个字段校验失败状态和一个操作成功反馈",
            "统计区或概览区要跟当前数据变化联动，不要只是写死装饰数字",
        ],
        data="先用本地模拟数据或轻量接口组织业务对象，字段要能支撑列表、详情、状态、负责人、时间和操作记录。",
        verification="完成后简要说明建议检查命令、预期结果和还能人工复查的页面路径。",
    )


def _test_smoke_prompt(direction: str, intent: dict) -> str:
    flags = {str(item) for item in intent.get("flags", []) if str(item)}
    text = str(direction or "").strip()
    lower_text = text.lower()
    if intent.get("run_mode") != "test":
        return ""
    if not (flags & {"chain_validation_only", "single_page_quick"} or "smoke" in lower_text):
        return ""
    base = text or "AgentOps E2E smoke: create a tiny README or static page that says AgentOps E2E smoke OK."
    return _naturalize_prompt(
        f"{base}\n\n"
        "测试说明：只做最小可复查改动，方便平台快速跑通后续链路；"
        "完成后用一两句中文列出改动文件。"
    )


def _windows_node_command_note() -> str:
    return (
        "如果需要给 Node.js 命令，按 Windows PowerShell 写法给：先设置 "
        "`$env:npm_config_cache = \"$PWD\\.npm-cache\"`，再用 npm.cmd/npx.cmd/pnpm.cmd/yarn.cmd 或 cmd /c。"
    )


def _append_windows_command_note(prompt: str) -> str:
    text = _strip_foreign_windows_command_note(str(prompt or "").strip())
    note = _windows_node_command_note()
    has_cmd_guard = any(token in text for token in ("npm.cmd", "npx.cmd", "pnpm.cmd", "yarn.cmd", "cmd /c"))
    has_cache_guard = "npm_config_cache" in text or ".npm-cache" in text
    if has_cmd_guard and has_cache_guard:
        return text
    if "Windows 命令提示：" in text and not has_cache_guard:
        return _naturalize_prompt(f"{text} 另外，安装依赖前必须把 npm 缓存设到当前项目的 .npm-cache，例如 `$env:npm_config_cache = \"$PWD\\.npm-cache\"`。")
    return _naturalize_prompt(f"{text}\n\n{note}")


def _append_trae_self_test_guard(prompt: str) -> str:
    text = str(prompt or "").strip()
    if CONCISE_FAST_SCOPE_NOTE in text:
        return text
    return _naturalize_prompt(f"{text}\n\n{CONCISE_FAST_SCOPE_NOTE}")


def append_windows_command_note(prompt: str) -> str:
    return _append_windows_command_note(prompt)


def _strip_foreign_windows_command_note(prompt: str) -> str:
    text = str(prompt or "")
    patterns = (
        r"Windows\s+command\s+note\s*:\s*if\s+you\s+run\s+Node\.?js\s+tooling.*?(?=(?:\n\s*\n|$))",
        r"Windows\s+command\s+note\s*:\s*.*?(?=(?:\n\s*\n|$))",
    )
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    return text.replace("Windows command note:", "").strip()


def _prompt_delivery_policy(job: Job, round_: TaskRound) -> dict:
    intent = job.intent if isinstance(job.intent, dict) else {}
    flags = {str(item).strip() for item in intent.get("flags") or [] if str(item).strip()}
    run_mode = str(intent.get("run_mode") or "normal").strip().lower()
    fast_scope = bool(
        run_mode == "test"
        or flags
        & {
            "quick_prompt",
            "single_page_quick",
            "chain_validation_only",
            "skip_trae_self_tests",
            "test_run",
            "test_start_button",
        }
    )
    direction_text = " ".join([_current_direction(job), _directions_text(job.directions), str(intent.get("prompt_brief") or "")])
    node_context = bool(re.search(r"\b(node|npm|npx|pnpm|yarn|vite|react|vue|next\.?js)\b", direction_text, flags=re.IGNORECASE))
    return {
        "style": "natural_brief",
        "focus": "write task-specific instructions first; avoid boilerplate",
        "fast_scope": fast_scope,
        "command_note": "node_powershell_only_when_prompt_mentions_node_commands" if node_context or fast_scope else "omit",
        "avoid": [
            "repeated execution boundaries",
            "default test/build/browser prohibitions",
            "irrelevant route checklist",
            "fixed delivery checklist",
        ],
        "review": "scheduler should reject prompts that read like a policy template instead of a user request",
    }


def _apply_prompt_delivery_policy(prompt: str, job: Job, round_: TaskRound) -> tuple[str, dict]:
    policy = _prompt_delivery_policy(job, round_)
    text = _strip_legacy_operational_boilerplate(_strip_foreign_windows_command_note(str(prompt or "").strip()))
    applied: list[str] = []
    if policy.get("fast_scope") and CONCISE_FAST_SCOPE_NOTE not in text:
        text = _append_trae_self_test_guard(text)
        applied.append("fast_scope_note")
    if policy.get("command_note") != "omit" and _prompt_mentions_node_tooling(text):
        text = _append_windows_command_note(text)
        applied.append("node_powershell_note")
    return _naturalize_prompt(text), {**policy, "applied": applied}


def _prompt_mentions_node_tooling(prompt: str) -> bool:
    return bool(re.search(r"\b(npm|npx|pnpm|yarn|vite|node)\b", str(prompt or ""), flags=re.IGNORECASE))


def _strip_legacy_operational_boilerplate(prompt: str) -> str:
    text = str(prompt or "")
    patterns = (
        r"执行边界[:：][^。！？]*(?:我后续统一执行|统一执行)[。！？]?",
        r"不要自行启动开发服务器、?不要运行浏览器验收[^。！？]*[。！？]?",
        r"不要自行启动服务或跑测试[；;，,]?",
        r"不要运行耗时测试或构建[；;，,]?",
        r"需要验证时只写出建议命令和预期结果[，,]?",
    )
    for pattern in patterns:
        text = re.sub(pattern, "", text)
    return re.sub(r"\s+", " ", text).strip(" ；;，,。")


def _contains_legacy_operational_boilerplate(prompt: str) -> str:
    text = str(prompt or "")
    phrases = (
        "执行边界",
        "我后续统一执行",
        "不要自行启动开发服务器",
        "不要运行浏览器验收",
        "不要运行耗时测试或构建",
        "完成后请写清：实际入口文件",
        "默认访问路径、/jobs、/candidates、/interviews",
    )
    for phrase in phrases:
        if phrase in text:
            return phrase
    if text.count("完成后") >= 3:
        return "完成后"
    return ""


def build_followup_fallback_prompt(job: Job, round_: TaskRound, previous_reason: str = "") -> str:
    directions = [_current_direction(job)] if _current_direction(job) else []
    direction_text = "；".join(directions) or "当前项目"
    reason = previous_reason or _previous_dissatisfaction_from_round(round_)
    topic = _direction_topic(direction_text)
    if reason and _has_fixable_previous_issue(reason):
        action = _issue_action_from_reason(reason, topic, direction_text)
        return _structured_prompt(
            topic=topic,
            stack=_select_stack(job, round_),
            objective=f"继续在现有项目上处理一个会影响验收的业务问题：{action}",
            module="当前问题所在的页面、状态流转和关联数据",
            interactions=[
                "不要重搭项目，先沿着现有入口复现并修正对应操作路径",
                "修正按钮、表单、列表刷新、详情状态或统计联动里真正不一致的部分",
                "同类数据状态要一起处理，避免只改一个样例或只改展示文案",
                "补上失败提示、空数据反馈或边界状态，保证用户能看懂当前结果",
            ],
            data="沿用当前项目已有的数据结构；如果字段不足，可以补少量必要字段，但不要把业务重做成另一个系统。",
            verification="完成后说明从哪个页面入口复查、怎么操作、操作后应该看到什么变化，以及建议检查命令。",
        )
    module_prompt = _module_map_followup_prompt(job, round_, topic)
    if module_prompt:
        return _naturalize_prompt(module_prompt)
    followups = _direction_followup_prompts(direction_text, topic)
    if followups:
        return _naturalize_prompt(followups[(round_.round_index - 2) % len(followups)])
    reason_summary = _prompt_problem_summary(reason) or "上一轮复查时发现核心业务入口、运行验证和异常反馈还没有收口清楚"
    return _structured_prompt(
        topic=topic,
        stack=_select_stack(job, round_),
        objective=f"继续完善现有项目，方向仍然是：{direction_text}。需要避开上一轮暴露出的薄弱点：{reason_summary[:420]}",
        module="下一段自然业务流程",
        interactions=[
            "补一个和现有列表、详情或统计有关的业务模块，不要只改文案或 README",
            "让新增模块和已有数据发生联动，例如状态流转、负责人变化、筛选结果或统计数字变化",
            "至少补三处可实际复查的交互，包括成功反馈和异常反馈",
            "保留已有结构和页面风格，避免从零重建",
        ],
        data="继续使用当前项目的数据模型，必要时补模拟数据、状态枚举和操作记录。",
        verification="完成后写清楚复查路径、建议检查命令和预期结果。",
    )


def _module_map_followup_prompt(job: Job, round_: TaskRound, topic: str) -> str:
    range_meta = _range_plan_meta(job).get("current_range") or {}
    module_map = range_meta.get("module_map") if isinstance(range_meta, dict) else []
    modules = [str(item).strip() for item in module_map or [] if str(item).strip()]
    if not modules:
        return ""
    index = max(0, int(round_.round_index or 1) - 1) % len(modules)
    module = modules[index]
    if module in {"系统骨架"} and (round_.round_index > 1 or _is_existing_project_new_interaction(round_)):
        index = (index + 1) % len(modules)
        module = modules[index]
    return _structured_prompt(
        topic=topic,
        stack=_select_stack(job, round_),
        objective=f"基于当前项目继续扩展 {module}，不要重搭项目，也不要重复修同一个小问题。",
        module=module,
        interactions=[
            "给这个模块补清楚的入口，并接到已有列表、详情、工作台或统计区域",
            "完成一个新增、编辑、状态切换、筛选或批量处理中的中等业务动作",
            "操作后要同步更新相关列表、详情、统计或提示，不要只做静态展示",
            "补空状态、校验失败和成功反馈，保证复查时能看出真实流程",
        ],
        data="沿用当前项目已有模拟数据或接口组织方式，补足这个模块需要的字段、状态和关联记录。",
        verification="完成后说明复查入口、关键操作路径、建议检查命令和预期结果。",
    )


def _structured_prompt(
    *,
    topic: str,
    stack: str,
    objective: str,
    module: str,
    interactions: list[str],
    data: str,
    verification: str,
) -> str:
    items = "\n".join(f"{index}. {item}" for index, item in enumerate(interactions, start=1))
    prompt = (
        f"请继续做一个中等规模的{topic}，技术栈优先用 {stack}。\n\n"
        f"目标：{objective}\n\n"
        f"本次范围：集中在“{module}”，但要和现有页面、数据和状态联动起来。不要做成单文件静态页，也不要一次扩成完整商业平台。\n\n"
        f"交互要求：\n{items}\n\n"
        f"数据和结构：{data} 代码要拆成合理的组件、页面、数据或接口文件，方便后续继续迭代。\n\n"
        f"验收方式：{verification}\n\n"
        "收尾时用中文简单说一下主要改动，以及后面从哪里复查比较合适。"
    )
    return _naturalize_prompt(prompt)


def prompt_quality_error(db: Session | None, job: Job, round_: TaskRound, prompt: str) -> str:
    text = " ".join(str(prompt or "").split())
    agentops_context = _is_agentops_context(_current_direction(job) or _directions_text(job.directions))
    for phrase in FORBIDDEN_PROMPT_PHRASES:
        if agentops_context and phrase in {"LLM", "工具调用"}:
            continue
        if phrase in text:
            return f"prompt_contains_meta_phrase:{phrase}"
    for phrase in REUSED_PROMPT_PATTERNS:
        if phrase in text:
            return f"prompt_reuses_template_phrase:{phrase}"
    for pattern in FORBIDDEN_PROMPT_REGEXES:
        if agentops_context and "(日志|轨迹|扫描|工具调用|watcher|trace|session)" in pattern.pattern:
            continue
        if pattern.search(text):
            return f"prompt_contains_internal_process:{pattern.pattern}"
    legacy_boilerplate = _contains_legacy_operational_boilerplate(text)
    if legacy_boilerplate:
        return f"prompt_contains_operational_boilerplate:{legacy_boilerplate}"
    if other_direction := _queued_other_direction_mention(job, text):
        return f"prompt_mentions_other_direction:{other_direction}"
    if _is_new_project_first_round(round_):
        stripped = text.lstrip()
        for prefix in FIRST_ROUND_TEMPLATE_PREFIXES:
            if stripped.startswith(prefix):
                return f"first_round_template_prefix:{prefix}"
        positive_scope_text = re.sub(
            r"(不要|不能|不允许)[^。！？；;]*(单文件|一个文件|极简|很简单|demo)[^。！？；;]*",
            "",
            text,
        )
        too_small_patterns = (
            r"只要一?个页面",
            r"只做一?个页面",
            r"只要一?个文件",
            r"单文件\s*(小页面|demo|演示)",
            r"极简",
            r"很简单",
            r"小\s*demo",
        )
        if any(re.search(pattern, positive_scope_text, re.IGNORECASE) for pattern in too_small_patterns):
            return "first_round_too_small"
    small_followup_patterns = (
        r"只(改|调整|修改).{0,10}(标题|颜色|文案|按钮|字段|README)",
        r"(改|调整|修改).{0,8}(标题|颜色|文案)$",
        r"补一?个字段",
        r"只补\s*README",
    )
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in small_followup_patterns):
        return "prompt_too_small"
    if len(text) < 12:
        return "prompt_too_short"
    previous_reason = _previous_dissatisfaction(db, job, round_) if db else ""
    if previous_reason and _prompt_reuses_dissatisfaction_phrase(text, previous_reason):
        return "prompt_reuses_last_dissatisfaction_phrase"
    current_key = _prompt_reuse_key(text)
    if db and current_key:
        previous_prompts = db.scalars(
            select(TaskRound.prompt)
            .where(TaskRound.job_id == job.id, TaskRound.id != round_.id, TaskRound.prompt != "")
            .order_by(TaskRound.created_at.desc())
            .limit(12)
        ).all()
        for previous in previous_prompts:
            previous_key = _prompt_reuse_key(str(previous or ""))
            if previous_key and previous_key == current_key:
                return "prompt_duplicate_recent"
            if previous and _prompt_style_similarity(text, str(previous)) >= 0.90:
                return "prompt_too_similar_to_recent"
    return ""


def mark_prompt_generation_failed(
    db: Session,
    job: Job,
    round_: TaskRound | None,
    message: str,
) -> None:
    job.status = JobState.MANUAL_REQUIRED
    if round_:
        round_.status = JobState.MANUAL_REQUIRED
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id if round_ else None,
        stage=JobState.MANUAL_REQUIRED,
        message=f"Prompt generation requires manual action: {message}",
        level="warning",
    )


def _store_prompt(
    db: Session,
    job: Job,
    round_: TaskRound,
    prompt: str,
    model: str,
    wire_api: str,
    extra: dict | None = None,
) -> str:
    prompt, delivery_policy = _apply_prompt_delivery_policy(prompt, job, round_)
    round_.prompt = prompt
    round_.status = JobState.PROMPT_READY
    job.status = JobState.PROMPT_READY
    log_extra = {
        "model": model,
        "wire_api": wire_api,
        "prompt_chars": len(prompt),
        "prompt_preview": _prompt_preview(prompt),
        "prompt_delivery_policy": delivery_policy,
    }
    if extra:
        log_extra.update(extra)
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.PROMPT_READY,
        message="Prompt writer generated the Trae prompt.",
        extra=log_extra,
    )
    return prompt


def _select_stack(job: Job, round_: TaskRound) -> str:
    text = _current_direction(job) or " ".join(str(item) for item in (job.directions or []))
    lower = text.lower()
    explicit = (
        ("Vue", ("vue", "前端", "页面", "vite", "react")),
        ("Python", ("python", "fastapi", "flask", "脚本")),
        ("Go", ("go", "golang")),
        ("Java", ("java", "spring")),
    )
    for stack, markers in explicit:
        if any(marker in lower for marker in markers):
            return stack
    seed = f"{job.id}:{round_.id}:{text}".encode("utf-8", errors="ignore")
    index = int(hashlib.sha256(seed).hexdigest()[:8], 16) % len(STACKS)
    return STACKS[index]


def _project_hint(directions: list[str], stack: str) -> str:
    text = " ".join(directions).strip()
    if text:
        compact = re.sub(r"\s+", "，", text)[:80]
        return f"项目，主题是：{compact}"
    hints = {
        "Vue": "Web 管理系统",
        "Python": "数据处理与管理后端",
        "Go": "后端服务系统",
        "Java": "业务管理系统",
    }
    return hints.get(stack, "业务系统")


def _directions_text(directions: object) -> str:
    if not isinstance(directions, list) or not directions:
        return "做一个方便后续迭代的业务系统"
    return "；".join(str(item).strip() for item in directions if str(item).strip())


def _direction_items(job: Job) -> list[str]:
    if not isinstance(job.directions, list):
        return []
    return [str(item).strip() for item in job.directions if str(item).strip()]


def _visible_directions_for_prompt(job: Job) -> list[str]:
    current = _current_direction(job)
    return [current] if current else []


def _direction_queue_meta(job: Job) -> dict:
    items = _direction_items(job)
    return {
        "policy": "queue_first_only",
        "total": len(items),
        "current_index": 1 if items else 0,
        "remaining_count": max(0, len(items) - 1),
    }


def _range_plan_meta(job: Job) -> dict:
    intent = job.intent if isinstance(job.intent, dict) else {}
    plan = intent.get("range_plan") if isinstance(intent.get("range_plan"), dict) else {}
    ranges = plan.get("ranges") if isinstance(plan.get("ranges"), list) else []
    current = _current_direction(job)
    current_range = {}
    for item in ranges:
        if not isinstance(item, dict):
            continue
        if str(item.get("source_text") or "").strip() == current:
            current_range = item
            break
    if not current_range and ranges and isinstance(ranges[0], dict):
        current_range = ranges[0]
    return {
        "total_target_rounds": plan.get("total_target_rounds") or job.daily_target or 100,
        "current_range": current_range,
        "ranges": ranges,
        "synthetic_range_policy": plan.get("synthetic_range_policy") if isinstance(plan.get("synthetic_range_policy"), dict) else {},
        "scheduler_policy": {
            "role": "global_orchestrator",
            "prompt_writer_does_not_switch_ranges": True,
            "prompt_writer_may_suggest_switch": True,
            "satisfied_round_should_expand_next_module_or_switch_by_scheduler": True,
            "max_rounds_per_interaction_group": 5,
            "same_project_new_interaction_preferred_before_switch_when_modules_remain": True,
        },
    }


def _prompt_writer_intent(job: Job, current_direction: str) -> dict:
    intent = dict(job.intent) if isinstance(job.intent, dict) else {}
    if current_direction:
        intent["prompt_brief"] = current_direction
        intent["current_direction"] = current_direction
    intent["direction_queue_policy"] = "only_current_direction"
    return intent


def _current_direction(job: Job) -> str:
    if not isinstance(job.directions, list):
        return ""
    for item in job.directions:
        text = str(item).strip()
        if text:
            return text
    return ""


def _queued_other_direction_mention(job: Job, prompt: str) -> str:
    directions = _direction_items(job)
    if len(directions) <= 1:
        return ""
    current = _current_direction(job)
    current_text = _normalized_phrase_text(current)
    prompt_text = _normalized_phrase_text(prompt)
    for direction in directions[1:]:
        if _normalized_phrase_text(direction) == current_text:
            continue
        for label in _direction_match_labels(direction):
            normalized_label = _normalized_phrase_text(label)
            if not normalized_label or normalized_label in current_text:
                continue
            if normalized_label in prompt_text:
                return _direction_public_label(direction)
    return ""


def _direction_match_labels(direction: str) -> list[str]:
    text = str(direction or "").strip()
    head = re.split(r"[：:，,。；;\s]", text, maxsplit=1)[0].strip()
    topic = _direction_topic(text)
    labels: list[str] = []
    for candidate in (head, topic):
        candidate = str(candidate or "").strip("：:，,。；; ")
        if candidate:
            labels.append(candidate)
            labels.extend(_domain_label_variants(candidate))
    generic = {"平台", "系统", "服务", "项目", "业务", "应用", "前端", "后端", "web", "Web"}
    result: list[str] = []
    seen: set[str] = set()
    for label in labels:
        clean = re.sub(r"\s+", "", label)
        if len(clean) < 2 or clean in generic:
            continue
        if clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _direction_public_label(direction: str) -> str:
    text = str(direction or "").strip()
    head = re.split(r"[：:，,。；;\s]", text, maxsplit=1)[0].strip()
    return head or _direction_topic(text)


def _domain_label_variants(label: str) -> list[str]:
    variants: list[str] = []
    for suffix in ("服务平台", "管理平台", "业务平台", "平台", "管理系统", "系统", "服务"):
        if label.endswith(suffix) and len(label) > len(suffix) + 1:
            variants.append(label[: -len(suffix)])
    return variants


def _prompt_preview(prompt: str, limit: int = 220) -> str:
    text = " ".join(str(prompt or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _prompt_reuse_key(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "").lower())
    text = re.sub(r"[，。！？；：、,.!?;:\-_\s]+", "", text)
    return text[:260]


def _prompt_style_similarity(a: str, b: str) -> float:
    a_terms = set(_prompt_terms(a))
    b_terms = set(_prompt_terms(b))
    if not a_terms or not b_terms:
        return 0.0
    return len(a_terms & b_terms) / max(len(a_terms), len(b_terms))


def _prompt_terms(value: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", str(value or ""))


def _previous_dissatisfaction(db: Session, job: Job, round_: TaskRound) -> str:
    if round_.round_index <= 1 and not _is_existing_project_new_interaction(round_):
        return ""
    item = db.scalar(
        select(RuntimeLog)
        .where(RuntimeLog.job_id == job.id, RuntimeLog.stage == "dissatisfaction_reason")
        .order_by(RuntimeLog.created_at.desc())
        .limit(1)
    )
    if not item:
        return ""
    if isinstance(item.extra, dict):
        reason = item.extra.get("reason") or item.extra.get("product_reason") or item.extra.get("process_reason")
        if reason:
            return str(reason)
    return str(item.message or "")


def _prompt_problem_summary(value: str) -> str:
    text = str(value or "")
    replacements = {
        "产物不满意：": "",
        "过程不满意：": "",
        "结果不满意：": "",
        "不满意原因：": "",
        "关键证据：": "具体问题是：",
        "关键证据": "具体问题",
        "判定依据：": "",
        "判定依据": "",
        "Worker": "本地执行环境",
        "worker": "本地执行环境",
        "Trae CN": "模型",
        "Trae": "模型",
        "飞书": "记录表",
        "GitHub": "代码仓库",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"\s+", " ", text).strip(" ，。")
    return text[:700]


def _format_rules(rules: dict[str, str]) -> str:
    if not rules:
        return "无额外规则。"
    return "\n\n".join([f"## {name}\n{content}" for name, content in rules.items()])


def _parse_prompt_writer_result(text: str) -> dict:
    raw = str(text or "").strip()
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else raw
    if not candidate.lstrip().startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    try:
        data = json.loads(candidate)
    except Exception:
        return {"prompt": raw}
    return data if isinstance(data, dict) else {"prompt": raw}


def _compact_prompt_state(db: Session, job: Job, round_: TaskRound, previous_reason: str) -> dict:
    previous_rounds = list(
        db.scalars(
            select(TaskRound)
            .where(TaskRound.job_id == job.id, TaskRound.id != round_.id)
            .order_by(TaskRound.created_at.desc())
            .limit(8)
        ).all()
    )
    recent_logs = list(
        db.scalars(
            select(RuntimeLog)
            .where(RuntimeLog.job_id == job.id)
            .order_by(RuntimeLog.created_at.desc())
            .limit(12)
        ).all()
    )
    return {
        "now": datetime.now().isoformat(),
        "round_index": round_.round_index,
        "current_task": _current_task(job, round_, previous_reason),
        "orchestrator_intent": _prompt_writer_intent(job, _current_direction(job)),
        "direction_queue": _direction_queue_meta(job),
        "daily_counts": {"submitted": job.submitted_count or 0, "satisfied": job.satisfied_count or 0},
        "recent_history": [
            {
                "round": item.round_index,
                "prompt": item.prompt,
                "status": item.status,
                "trace_status": item.trace_status,
            }
            for item in reversed(previous_rounds)
            if item.prompt
        ],
        "recent_dissatisfaction": [
            (item.extra or {}).get("reason") or item.message
            for item in recent_logs
            if item.stage == "dissatisfaction_reason"
        ][:4],
    }


def _current_task(job: Job, round_: TaskRound, previous_reason: str) -> dict:
    direction = _current_direction(job) or _directions_text(job.directions)
    previous_prompts = []
    if round_.job_id:
        # The caller's DB session is not available here, so generate_round_prompt passes detailed state separately.
        previous_prompts = []
    return {
        "topic": _direction_topic(direction),
        "direction": direction,
        "project_slug": _project_slug_from_direction(direction),
        "round_index": round_.round_index,
        "existing_project_new_interaction": _is_existing_project_new_interaction(round_),
        "last_dissatisfaction": previous_reason,
        "used_prompts": previous_prompts,
        "followups": _direction_followup_prompts(direction, _direction_topic(direction)),
    }


def _prompt_meta(job: Job, round_: TaskRound) -> dict:
    intent = job.intent if isinstance(job.intent, dict) else {}
    existing_project_new_interaction = _is_existing_project_new_interaction(round_)
    is_new_project = _is_new_project_first_round(round_)
    return {
        "kind": "new" if is_new_project else "followup",
        "round": "第一轮" if round_.round_index <= 1 else f"第{round_.round_index}轮",
        "topic": _direction_topic(_current_direction(job) or _directions_text(job.directions)),
        "project_slug": _project_slug_from_direction(_current_direction(job) or _directions_text(job.directions)),
        "first_round_gate": is_new_project,
        "existing_project_new_interaction": existing_project_new_interaction,
        "open_new_trae_task_but_keep_project": existing_project_new_interaction,
        "run_mode": intent.get("run_mode") or "normal",
        "intent_summary": intent.get("intent_summary") or "",
    }


def _is_new_project_first_round(round_: TaskRound) -> bool:
    return int(round_.round_index or 1) <= 1 and not bool(round_.project_id)


def _is_existing_project_new_interaction(round_: TaskRound) -> bool:
    return int(round_.round_index or 1) <= 1 and bool(round_.project_id)


def _previous_dissatisfaction_from_round(round_: TaskRound) -> str:
    return ""


def _naturalize_prompt(prompt: str) -> str:
    text = str(prompt or "")
    for banned in ("本轮", "首轮", "第一轮", "第二轮", "第三轮", "第四轮", "第五轮"):
        text = text.replace(banned, "")
    text = re.sub(r"第\s*[0-9一二三四五六七八九十]+\s*轮", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _prompt_reuses_dissatisfaction_phrase(prompt: str, reason: str) -> bool:
    prompt_norm = _normalized_phrase_text(prompt)
    reason_text = re.sub(r"(产物|过程|结果)不满意(原因)?[:：]?", "", str(reason or ""))
    clauses = [
        _normalized_phrase_text(item.strip(" 。；;，,\n\t"))
        for item in re.split(r"[。\n；;]", reason_text)
        if item.strip()
    ]
    for clause in clauses:
        clause = re.sub(r"（?证据[:：][^)）]+[)）]?", "", clause)
        if len(clause) >= 18 and clause in prompt_norm:
            return True
    return False


def _normalized_phrase_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").replace("：", ":").replace("；", ";"))


def _soften_prompt_repetition(db: Session, job: Job, round_: TaskRound, prompt: str, prompt_kind: str) -> str:
    text = _naturalize_prompt(prompt)
    if prompt_quality_error(None, job, round_, text):
        return text
    previous = list(
        db.scalars(
            select(TaskRound.prompt)
            .where(TaskRound.job_id == job.id, TaskRound.id != round_.id, TaskRound.prompt != "")
            .order_by(TaskRound.created_at.desc())
            .limit(12)
        ).all()
    )
    if not previous:
        return text
    current_key = _prompt_reuse_key(text)
    too_close = any(_prompt_reuse_key(str(item or "")) == current_key or _prompt_style_similarity(text, str(item or "")) >= 0.78 for item in previous)
    if not too_close:
        return text
    direction = _current_direction(job) or _directions_text(job.directions)
    topic = _direction_topic(direction)
    for index in range(8):
        candidate = _restyle_prompt_candidate(text, topic, prompt_kind, index)
        if prompt_quality_error(None, job, round_, candidate):
            continue
        candidate_key = _prompt_reuse_key(candidate)
        if not any(
            _prompt_reuse_key(str(item or "")) == candidate_key or _prompt_style_similarity(candidate, str(item or "")) >= 0.78
            for item in previous
        ):
            return candidate
    return text


def _restyle_prompt_candidate(prompt: str, topic: str, prompt_kind: str, index: int = 0) -> str:
    base = _naturalize_prompt(prompt)
    tail = base
    for pattern in (
        r"^这个地方出了个问题需要修[:：]\s*",
        rf"^先别重搭，?帮我把\s*{re.escape(topic)}\s*里这条不好用的路径修顺[:：]\s*",
        rf"^把\s*{re.escape(topic)}\s*里这条不好用的路径修顺[:：]\s*",
        rf"^{re.escape(topic)}\s*现在有个操作不太对，?按真实用户会点的方式处理一下[:：]\s*",
        rf"^帮我修一下\s*{re.escape(topic)}\s*的这个\s*bug[:：]\s*",
    ):
        tail = re.sub(pattern, "", tail).strip(" ，,")
    if prompt_kind == "bugfix":
        templates = [
            "这个地方出了个问题需要修：{tail}",
            "先别重搭，帮我把 {topic} 里这条不好用的路径修顺：{tail}",
            "{topic} 现在有个操作不太对，按真实用户会点的方式处理一下：{tail}",
            "帮我修一下 {topic} 的这个 bug：{tail}",
        ]
    else:
        templates = [
            "我还需要 {topic} 增加一块能力：{tail}",
            "再补一下 {topic}：{tail}",
            "{topic} 继续往下做，重点是 {tail}",
            "这个项目还差一块，帮我补 {tail}",
        ]
    return _naturalize_prompt(templates[index % len(templates)].format(topic=topic, tail=tail or base))


def _build_direction_task(direction: str, stack: str) -> dict:
    topic = _direction_topic(direction)
    direction_text = str(direction or "").strip().rstrip("。") or "业务系统"
    suffixes = [
        "做成一个能直接运行的业务工作台，不要停在说明页。界面至少拆出四个业务模块，包含列表、详情、编辑或操作区、统计状态和本地模拟数据，关键操作点完后要能看到状态变化。",
        "直接落到可操作界面里，别做成单页静态 demo。需要有清楚的数据模型、两个以上主要视图区域、角色或状态区分、异常反馈和三个以上能点击验证的业务动作。",
        "按真实业务人员会使用的方式实现，首页进去就能处理主要流程。列表筛选、详情查看、状态流转、保存反馈、统计联动和空数据/错误输入提示都要有本地模拟。",
        "做成后续还能继续迭代的系统骨架，但这次交付本身就要能打开使用。模块、数据、操作入口、状态标签、异常提示和基础运行脚本都要齐，不能只给文案或占位。",
    ]
    suffix = suffixes[_stable_index(direction_text, stack, modulo=len(suffixes))]
    return {
        "topic": topic,
        "project_slug": _project_slug_from_direction(direction_text),
        "direction": direction_text,
        "base": f"{direction_text}。{suffix}技术选型优先 {stack}，依赖尽量少，保留清楚的安装、启动和检查命令，方便后续统一验收。",
        "followups": _direction_followup_prompts(direction_text, topic),
    }


def _direction_topic(direction: str) -> str:
    text = str(direction or "").strip()
    if not re.match(r"^\s*agentops\b", text, flags=re.IGNORECASE):
        text = re.sub(r"^\s*[a-z][a-z0-9_-]*\s+", "", text, flags=re.IGNORECASE)
    text = re.split(r"[。；;\n\r]", text, maxsplit=1)[0].strip()
    if "：" in text:
        left, right = text.split("：", 1)
        right = re.sub(r"^(支持|包含|实现|用于|可以)", "", right.strip())
        right = re.split(r"[，,、]", right, maxsplit=1)[0].strip()
        text = f"{left}{right}" if right else left
    else:
        text = re.split(r"[，,、]", text, maxsplit=1)[0].strip()
    text = re.sub(r"\s+", "", text)
    return text.strip("：:，,。；; ")[:36] or "这个项目"


def _project_slug_from_direction(direction: str) -> str:
    text = str(direction or "")
    if any(term in text for term in ["AgentOps", "AI Agent", "多Agent", "自动作业平台"]):
        return "agentops-ai-agentmonorepoweb"
    explicit = re.search(r"\b([a-z][a-z0-9]*(?:[-_][a-z0-9]+){1,5})\b", text.lower())
    if explicit:
        return explicit.group(1).replace("_", "-")[:48]
    dictionary = [
        ("物流系统", "logistics-control"),
        ("运单", "logistics-control"),
        ("TMC", "tmc-express"),
        ("快递", "express-tmc"),
        ("监控系统", "process-monitor"),
        ("告警", "alert-monitor"),
        ("仓储", "warehouse-system"),
        ("审批", "approval-workbench"),
        ("库存", "inventory-dashboard"),
        ("订单", "order-dashboard"),
        ("工单", "ticket-board"),
        ("CRM", "crm-console"),
    ]
    for marker, slug in dictionary:
        if marker in text:
            return slug
    slug = re.sub(r"\s+", "-", text.strip().lower())
    slug = re.sub(r"[^a-z0-9_-]+", "", slug).strip("-_")
    return slug[:48] or "directed-project"


def _direction_followup_prompts(direction: str, topic: str) -> list[str]:
    text = str(direction or "")
    if _is_agentops_context(text):
        return [
            f"{topic}继续补角色配置和编排链路：需求分析、提示生成、Trae控制、代码审查、浏览器验收、GitHub和飞书这些Agent要能分开配置并在同一轮任务里串起来。",
            f"再把{topic}的真实采集闭环做细：Trae发送、继续按钮、底部复制、超长txt附件、GitHub提交和飞书预览都要能在页面里复查。",
            f"{topic}补人工复核入口：代码扫描、构建结果、浏览器点击、截图附件和写表前校验要能关联到同一轮记录。",
            f"在现在基础上收紧{topic}的异常处理：token过期、日志截断、无空行、重复提交和网络失败都要有状态、重试和停止入口。",
        ]
    if any(term in text for term in ["物流系统", "物流", "运输", "运单", "配送", "路线", "线路", "车辆", "司机", "装车", "在途", "签收", "回单", "仓配联动"]):
        return [
            f"{topic}接上物流执行链路：运单创建、车辆分配、路线节点、司机状态和异常签收要能在同一套数据里联动。",
            f"{topic}补路线和成本视图：展示预计时效、里程、费用、延误原因和节点时间，切换运单时这些信息要同步更新。",
            f"{topic}增加仓储到物流的衔接状态：出库完成后生成运单，装车失败、缺件、超时这些情况要有处理入口。",
            f"{topic}完善可验收细节：筛选、搜索、状态流转、空列表、异常节点和本地校验都要能跑通。",
        ]
    if any(term in text for term in ["仓储", "库存", "库位", "入库", "出库", "盘点", "SKU"]):
        return [
            f"{topic}把仓储作业链路补完整：入库、上架、库位调整、库存冻结和出库拣货要能从列表进入详情操作。",
            f"{topic}增加库存校验和异常处理：SKU 缺货、库位冲突、批次过期、盘点差异要有提示，并能影响库存状态。",
            f"{topic}把角色和数据看板补上：仓管、主管、质检看到的操作入口不同，库存周转、预警数量和待处理任务要联动。",
            f"{topic}收紧工程可验收状态：补运行/构建脚本，页面空数据、错误输入和窄屏布局都要能直接检查。",
        ]
    if any(term in text for term in ["TMC", "tmc", "快递", "下单", "接单", "送单", "骑手", "网点"]):
        return [
            f"{topic}把快递下单到接单流程补完整：寄件信息、费用估算、网点/骑手接单、订单状态和取消原因要联动。",
            f"{topic}增加送单履约视图：揽收、运输、派送、签收、异常件和超时预警要能按订单查看。",
            f"{topic}补角色工作台：寄件人、接单员、配送员和管理员看到的操作不同，状态变更后列表统计也要变化。",
            f"{topic}把异常和验证补齐：地址缺失、重量超限、无人接单、配送失败、签收异常都要有明确反馈。",
        ]
    if any(term in text for term in ["监控", "告警", "看板", "全过程", "链路", "指标"]):
        return [
            f"{topic}补全过程监控链路：仓储、物流、快递订单的关键节点要汇总到统一看板，点击告警能看到来源数据。",
            f"{topic}增加告警规则配置：超时、库存异常、运单延误、无人接单和签收失败要能配置阈值并看到触发结果。",
            f"{topic}做事件追踪和处置闭环：告警确认、派发、备注、关闭和复盘统计要跟状态变化联动。",
            f"{topic}完善筛选和可靠性细节：按系统、等级、时间、状态筛选，空数据、重复告警和异常数据都要有处理。",
        ]
    return [
        f"{topic}把核心业务流程补完整：列表、详情、编辑、状态流转和统计要围绕同一批本地数据联动。",
        f"{topic}增加角色和异常处理：不同角色看到不同操作，空数据、失败、缺字段和不可用状态要有明确反馈。",
        f"{topic}补可验证交互：筛选、搜索、新增、保存、切换详情和统计变化都要能在页面上直接操作。",
        f"{topic}收紧工程交付：补运行/构建脚本，整理数据结构和边界提示，保持低依赖、容易复查。",
    ]


def _has_fixable_previous_issue(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        word in text
        for word in [
            "代码问题",
            "bug",
            "错误",
            "失败",
            "测试",
            "校验",
            "构建",
            "运行",
            "lint",
            "依赖",
            "不可操作",
            "浏览器弹窗",
            "confirm",
            "alert",
            "缺少",
            "没有接",
            "没有看到点击处理",
            "没有联动",
            "不可用",
        ]
    )


def _issue_action_from_reason(reason: str, topic: str, direction: str) -> str:
    text = str(reason or "")
    context = "\n".join([text, str(topic or ""), str(direction or "")])
    target = _concrete_fix_target(context)
    lower = text.lower()
    if "alert" in lower or "confirm" in lower or "浏览器原生弹窗" in text or "浏览器弹窗" in text:
        return f"把关键确认动作改成页面内的确认层或状态面板，取消时保持原数据，确认后复查列表、详情和统计是否同步；复查范围包括{target}"
    if any(term in text for term in ["按钮", "点击", "入口", "不可操作", "没有接", "没有看到点击处理"]):
        return f"把关键按钮的点击链路补上，点进去后能完成输入、保存、取消和结果刷新；复查范围包括{target}"
    if any(term in text for term in ["跳转", "路由", "定位", "待办"]):
        return f"检查带参数跳转后的初始化逻辑，让页面能自动定位到对应单据，并复查返回和刷新；复查范围包括{target}"
    if any(term in text for term in ["构建", "运行", "依赖", "端口", "环境", "校验"]):
        return "把本地运行和构建链路整理干净，脚本、依赖、端口和预览地址都要能复查，失败时在页面或 README 里给出明确处理方式"
    if any(term in text for term in ["角色", "权限", "越权", "登录"]):
        return "按普通用户、运营人员和管理员分别走一遍入口，修正越权按钮、角色切换和登录后的默认状态"
    return f"沿着真实用户会操作的入口做缺陷修补，重点检查按钮反馈、状态刷新、异常提示和数据联动，再按 {target} 验证"


def _concrete_fix_target(reason: str) -> str:
    text = str(reason or "")
    text = re.sub(r"(产物|过程)不满意(原因)?：?", "", text)
    if _is_agentops_context(text):
        return "从任务创建、提示发送、继续处理、底部日志复制、代码审查、浏览器验收、GitHub提交和飞书预览这些入口逐项跑一遍，保证每个节点都有状态、错误提示和可复查记录"
    if any(term in text for term in ["物流系统", "物流", "运单", "线路", "路线", "车辆", "装车", "在途", "签收", "回单", "仓配联动", "费用核算"]):
        return "从运单创建、车辆调度、装车交接、在途节点、签收回单和异常流转这些入口逐项点一遍，保证节点变化会同步到列表、详情和费用统计"
    if any(term in text for term in ["仓储系统", "仓储", "入库", "出库", "库位", "库存", "批次", "盘点", "波次", "预警"]):
        return "从入库预约、质检上架、库位占用、批次库存、盘点差异、波次拣货、出库复核和异常预警这些入口逐项点一遍，保证每一步保存后列表、详情和统计同步变化"
    if any(term in text for term in ["TMC", "tmc", "快递", "下单", "取件", "送单", "派送", "骑手", "网点"]):
        return "从快递下单、费用估算、网点接单、骑手取件、派送签收和异常件处理这些入口逐项点一遍，保证不同角色看到的状态和按钮对得上"
    if any(term in text for term in ["监控", "告警", "链路", "全过程", "节点", "处置", "审计"]):
        return "从全过程看板、节点告警、责任定位、处置派发、关闭复盘和日志审计这些入口逐项点一遍，保证告警来源、处理状态和统计指标同步变化"
    if any(term in text for term in ["构建", "运行", "环境", "依赖", "端口", "校验"]):
        return "重点把本地运行、构建命令和预览地址整理到可复查状态，避免页面写完但环境跑不起来"
    return "按真实用户会走的入口逐项复查，补齐按钮反馈、状态刷新、异常提示和构建校验"


def _is_agentops_context(text: str) -> bool:
    return any(term in str(text or "") for term in ["AgentOps", "AI Agent", "多Agent", "自动作业", "日志轨迹", "提示词", "飞书", "GitHub", "状态机", "模型配置", "Worker", "Trae"])


def _stable_index(*parts: str, modulo: int) -> int:
    seed = ":".join(str(part or "") for part in parts)
    return int(hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % max(1, modulo)
