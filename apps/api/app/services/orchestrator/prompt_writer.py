import hashlib
import re

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
    re.compile(r"\bworker\b", re.IGNORECASE),
    re.compile(r"调度(角色|器|流程)|平台侧|自动作业流程"),
    re.compile(r"飞书(写入|记录|多维表格)|github\s*(提交|推送|记录)", re.IGNORECASE),
    re.compile(r"关键证据|验收证据|不满意原因|产物不满意|过程不满意"),
)
FIRST_ROUND_TEMPLATE_PREFIXES = (
    "按这个项目方向做一个能继续迭代的系统雏形",
    "基于这个项目方向做一个能继续迭代的系统雏形",
)


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
    prompt_kind = "首轮新项目" if round_.round_index <= 1 else "后续迭代"
    current_direction = _current_direction(job)
    stack = _select_stack(job, round_)
    messages = [
        {
            "role": "system",
            "content": (
                f"你是自动化作业里的“{role_name}”。\n"
                f"角色目标：{role_purpose}\n"
                "你只负责生成要发给编码助手的一段用户需求。只输出提示词正文，不要 Markdown 代码块。\n"
                "提示词必须像真实用户在描述自己要做的项目或要修的问题，不能暴露平台内部流程。\n"
                "禁止出现 AgentOps、Trae CN、Worker、调度器、飞书、GitHub 提交、关键证据、判定依据、"
                "产物不满意、过程不满意、不满意原因等词。\n"
                "首轮要让对方做一个规模适中的项目，不要是单页面小 demo；需要包含业务模块、数据结构、"
                "列表/详情/编辑或操作区、状态反馈、本地模拟数据和可运行检查。\n"
                "后续轮必须基于上一轮暴露的问题继续改当前项目，用自然语言描述现象、期望结果和复查方式。\n\n"
                f"用户规则：\n{_format_rules(rules)}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"请生成{prompt_kind}提示词。\n"
                f"当前项目方向：{current_direction or _directions_text(job.directions)}\n"
                f"待做项目队列：{_directions_text(job.directions)}\n"
                f"当前轮次：{round_.round_index}\n"
                f"首选技术栈：{stack}\n"
                f"上一轮需要继续处理的问题：{_prompt_problem_summary(previous_reason) or '无'}\n"
                "当输入范围没有指定技术栈时，用首选技术栈；不要说“随机”。"
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

    prompt = result.text.strip()
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
    return _store_prompt(db, job, round_, prompt, model=result.model, wire_api=result.wire_api)


def build_fallback_prompt(
    job: Job,
    round_: TaskRound,
    rules: dict[str, str] | None = None,
    fallback_reason: str = "",
) -> str:
    directions = [_current_direction(job)] if _current_direction(job) else []
    direction_text = "\n".join(f"{index}. {item}" for index, item in enumerate(directions, start=1))
    if not direction_text:
        direction_text = "1. 做一个方便后续继续迭代的业务系统。"
    stack = _select_stack(job, round_)
    project_hint = _project_hint(directions, stack)
    if round_.round_index > 1:
        return build_followup_fallback_prompt(job, round_, fallback_reason)
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
    reason = _prompt_problem_summary(previous_reason) or "上一轮复查时发现核心业务入口、运行验证和异常反馈还没有收口清楚"
    return (
        f"继续完善这个项目，方向还是：{direction_text}。\n\n"
        f"上一轮复查时暴露的问题是：{reason[:500]}。\n\n"
        "这一轮不要重做一个新项目，也不要只改文档或补一个很小的示例。请直接在现有代码上把问题修好：\n"
        "1. 先定位和当前需求最相关的页面、接口、数据结构或命令入口。\n"
        "2. 针对上一轮暴露的问题补真实功能、状态变化、异常提示和可复查的运行路径。\n"
        "3. 如果是前端或全栈项目，至少把一个主流程从入口到结果状态走通；如果是后端或脚本项目，补清楚输入、处理、输出和错误处理。\n"
        "4. 完成后运行合适的检查命令，并在回复里说明改了什么、怎么验证、还有没有环境阻塞。"
    )


def prompt_quality_error(db: Session, job: Job, round_: TaskRound, prompt: str) -> str:
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
    current_key = _prompt_reuse_key(text)
    if current_key:
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
) -> str:
    round_.prompt = prompt
    round_.status = JobState.PROMPT_READY
    job.status = JobState.PROMPT_READY
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.PROMPT_READY,
        message="Prompt writer generated the Trae prompt.",
        extra={
            "model": model,
            "wire_api": wire_api,
            "prompt_chars": len(prompt),
            "prompt_preview": _prompt_preview(prompt),
        },
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
