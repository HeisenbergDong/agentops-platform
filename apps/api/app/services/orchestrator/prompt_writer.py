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
    re.compile(r"关键证据|验收证据|不满意原因|产物不满意|过程不满意"),
)
FIRST_ROUND_TEMPLATE_PREFIXES = (
    "按这个项目方向做一个能继续迭代的系统雏形",
    "基于这个项目方向做一个能继续迭代的系统雏形",
)
PROMPT_WRITER_SYSTEM = """你是自动化作业里的“提示词策略员”。你不能直接写飞书，不能决定作业完成，只负责给编码助手的本轮或下一轮用户提示词提案。
要求：
1. 必须基于输入的完整作业状态、当前项目、轮次、上一轮真实不满意原因、已用提示词来写。
2. 不能复读上一轮提示词，不能套娃式重复“把 XX 里这条不好用的路径修顺”。
3. 不能在提示词里出现“产物不满意”“过程不满意”“不满意原因”“证据：”。
4. 提示词要像真实用户给开发助手的需求：明确现象、期望结果、复查路径。
5. 如果上一轮问题是证据不足或日志缺失，不要让编码助手修“日志轨迹/飞书/GitHub”，而要回到当前业务系统的可验证交互或工程交付。
只输出 JSON：{"prompt": "...", "prompt_kind": "bugfix|feature", "focus": "...", "acceptance_checks": ["..."], "difference_from_previous": "..."}"""


def generate_round_prompt(db: Session, user: User, job: Job, round_: TaskRound) -> str:
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
                    },
                    "state": state,
                    "current": current,
                    "meta": meta,
                    "orchestrator_intent": job.intent or {},
                    "user_rules": rules,
                    "current_direction": current_direction or _directions_text(job.directions),
                    "directions": job.directions or [],
                    "preferred_stack": _select_stack(job, round_),
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        result = LLMClient().complete(
            model_config_from_settings(load_user_settings(db, user.id), role_model_config_key),
            messages,
            purpose="prompt_generation",
        )
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

    proposal = _parse_prompt_writer_result(result.text)
    prompt = _naturalize_prompt(str(proposal.get("prompt") or result.text or ""))
    prompt_kind = str(proposal.get("prompt_kind") or ("feature" if round_.round_index <= 1 else "bugfix")).strip().lower()
    if prompt_kind not in {"bugfix", "feature"}:
        prompt_kind = "feature"
    if not prompt:
        prompt = build_fallback_prompt(job, round_, rules, previous_reason or "Prompt writer returned empty prompt")
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="prompt_generation_fallback",
            message="LLM prompt writer returned empty text; built-in prompt fallback will be sent to worker.",
            level="warning",
            extra={"prompt_chars": len(prompt)},
        )
        return _store_prompt(db, job, round_, prompt, model="built-in-fallback", wire_api="local")

    prompt = _soften_prompt_repetition(db, job, round_, prompt, prompt_kind)
    quality_error = prompt_quality_error(db, job, round_, prompt)
    if quality_error:
        prompt = build_fallback_prompt(job, round_, rules, previous_reason or "Prompt writer output failed quality gate")
        add_log(
            db,
            job_id=job.id,
            round_id=round_.id,
            stage="prompt_generation_fallback",
            message="LLM prompt writer output failed the quality gate; built-in prompt fallback will be sent to worker.",
            level="warning",
            extra={"quality_error": quality_error, "prompt_chars": len(prompt)},
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


def build_fallback_prompt(
    job: Job,
    round_: TaskRound,
    rules: dict[str, str] | None = None,
    fallback_reason: str = "",
) -> str:
    directions = [_current_direction(job)] if _current_direction(job) else []
    if round_.round_index > 1:
        return build_followup_fallback_prompt(job, round_, fallback_reason)
    intent = job.intent if isinstance(job.intent, dict) else {}
    prompt_brief = str(intent.get("prompt_brief") or "").strip()
    direction = prompt_brief or (directions[0] if directions else "做一个方便后续继续迭代的业务系统。")
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
    return (
        f"我想做一个中等规模的{project_hint}，主技术栈用 {stack}。\n\n"
        "需求范围：\n"
        f"{direction_text}\n\n"
        "请按真实项目的标准来实现，不要只做一个很小的 demo，也不要只放一个静态页面。"
        "功能要有清楚的数据结构、核心业务流程、错误处理和基础页面/接口组织，代码结构要方便后续继续迭代。\n\n"
        "实现要求：\n"
        "1. 在当前工作目录里完成项目实现；如果目录里已经有工程，先理解现有结构再改。\n"
        "2. 按所选技术栈建立合理的目录、模块和启动方式，必要时补 README 或运行说明。\n"
        "3. 至少完成一个可运行的主流程，并补上必要的校验、空状态、错误提示和示例数据。\n"
        "4. 完成后运行合适的检查或启动命令；如果因为环境缺依赖导致不能运行，请把原因和下一步写清楚。\n"
        "5. 最终回复用自然中文说明：完成了哪些功能、改了哪些关键文件、运行了哪些验证、还有没有阻塞。"
    )


def build_followup_fallback_prompt(job: Job, round_: TaskRound, previous_reason: str = "") -> str:
    directions = [_current_direction(job)] if _current_direction(job) else []
    direction_text = "；".join(directions) or "当前项目"
    reason = previous_reason or _previous_dissatisfaction_from_round(round_)
    topic = _direction_topic(direction_text)
    if reason and _has_fixable_previous_issue(reason):
        action = _issue_action_from_reason(reason, topic, direction_text)
        candidates = [
            f"这次先帮我修 {topic} 里不好用的操作，不重搭页面：{action}。修完后用实际点击路径说明入口、操作后状态和失败提示。",
            f"基于 {topic} 现在的页面按线上验收会点到的路径修一下：{action}。不要只改文案，相关按钮、弹窗、列表刷新和统计联动都要能实际跑。",
            f"接下来集中处理 {topic} 的操作链路问题：{action}。保留现有结构，修完跑构建或本地检查，并把可复查路径交代清楚。",
        ]
        return _naturalize_prompt(candidates[_stable_index(job.id, round_.id, "bugfix", modulo=len(candidates))])
    followups = _direction_followup_prompts(direction_text, topic)
    if followups:
        return _naturalize_prompt(followups[(round_.round_index - 2) % len(followups)])
    reason_summary = _prompt_problem_summary(reason) or "上一轮复查时发现核心业务入口、运行验证和异常反馈还没有收口清楚"
    return _naturalize_prompt(
        f"继续完善这个项目，方向还是：{direction_text}。上一轮复查时暴露的问题是：{reason_summary[:500]}。"
        "这一轮不要重做一个新项目，也不要只改文档或补一个很小的示例。请直接在现有代码上把真实功能、状态变化、异常提示和可复查的运行路径补好。"
    )


def prompt_quality_error(db: Session | None, job: Job, round_: TaskRound, prompt: str) -> str:
    text = " ".join(str(prompt or "").split())
    for phrase in FORBIDDEN_PROMPT_PHRASES:
        if phrase in text:
            return f"prompt_contains_meta_phrase:{phrase}"
    for phrase in REUSED_PROMPT_PATTERNS:
        if phrase in text:
            return f"prompt_reuses_template_phrase:{phrase}"
    for pattern in FORBIDDEN_PROMPT_REGEXES:
        if pattern.search(text):
            return f"prompt_contains_internal_process:{pattern.pattern}"
    if round_.round_index == 1:
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
            .order_by(TaskRound.round_index.desc())
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
    round_.prompt = prompt
    round_.status = JobState.PROMPT_READY
    job.status = JobState.PROMPT_READY
    log_extra = {
        "model": model,
        "wire_api": wire_api,
        "prompt_chars": len(prompt),
        "prompt_preview": _prompt_preview(prompt),
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


def _current_direction(job: Job) -> str:
    if not isinstance(job.directions, list):
        return ""
    for item in job.directions:
        text = str(item).strip()
        if text:
            return text
    return ""


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
    if round_.round_index <= 1:
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
            .order_by(TaskRound.round_index.desc())
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
        "orchestrator_intent": job.intent or {},
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
    intent = job.intent if isinstance(job.intent, dict) else {}
    prompt_brief = str(intent.get("prompt_brief") or "").strip()
    if prompt_brief:
        direction = prompt_brief
    previous_prompts = []
    if round_.job_id:
        # The caller's DB session is not available here, so generate_round_prompt passes detailed state separately.
        previous_prompts = []
    return {
        "topic": _direction_topic(direction),
        "direction": direction,
        "project_slug": _project_slug_from_direction(direction),
        "round_index": round_.round_index,
        "last_dissatisfaction": previous_reason,
        "used_prompts": previous_prompts,
        "followups": _direction_followup_prompts(direction, _direction_topic(direction)),
    }


def _prompt_meta(job: Job, round_: TaskRound) -> dict:
    intent = job.intent if isinstance(job.intent, dict) else {}
    return {
        "kind": "new" if round_.round_index <= 1 else "followup",
        "round": "第一轮" if round_.round_index <= 1 else f"第{round_.round_index}轮",
        "topic": _direction_topic(_current_direction(job) or _directions_text(job.directions)),
        "project_slug": _project_slug_from_direction(_current_direction(job) or _directions_text(job.directions)),
        "first_round_gate": round_.round_index <= 1,
        "run_mode": intent.get("run_mode") or "normal",
        "intent_summary": intent.get("intent_summary") or "",
    }


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
            .order_by(TaskRound.round_index.desc())
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
        "base": f"{direction_text}。{suffix}技术选型优先 {stack}，依赖尽量少，保证能在本机直接安装、运行、构建或测试。",
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
