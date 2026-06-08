from sqlalchemy.orm import Session

from app.db.models import Job, TaskRound, User
from app.db.repositories.jobs import add_log
from app.db.repositories.roles import get_user_role
from app.db.repositories.user_rules import read_user_rule_many
from app.services.llm import LLMClient, LLMError, model_config_from_settings
from app.services.orchestrator.states import JobState
from app.services.user_settings import load_user_settings


class PromptGenerationError(RuntimeError):
    pass


def generate_round_prompt(db: Session, user: User, job: Job, round_: TaskRound) -> str:
    role = get_user_role(db, user.id, "prompt_writer")
    if not role:
        raise PromptGenerationError("Prompt writer role not found")
    try:
        rules = read_user_rule_many(db, user.id, role.rules)
    except FileNotFoundError as exc:
        raise PromptGenerationError(f"Rule not found: {exc.args[0]}") from exc

    messages = [
        {
            "role": "system",
            "content": (
                f"You are the AgentOps role named {role.name}.\n"
                f"Role purpose: {role.purpose}\n"
                "Generate the exact prompt that will be pasted into Trae CN. "
                "Output only the prompt text. Do not wrap it in Markdown fences. "
                "Do not include secrets, tokens, or credentials.\n\n"
                f"Rules:\n{_format_rules(rules)}"
            ),
        },
        {
            "role": "user",
            "content": (
                "Create the first-round implementation prompt for this job.\n"
                f"Directions: {job.directions}\n"
                f"Round index: {round_.round_index}\n"
                "The prompt should be actionable, self-contained, and suitable for a coding agent "
                "working in the user's configured Trae workspace. Use Chinese unless the task itself "
                "requires another language."
            ),
        },
    ]
    try:
        result = LLMClient().complete(
            model_config_from_settings(load_user_settings(db, user.id), role.model_config_key),
            messages,
            purpose="prompt_generation",
        )
    except LLMError as exc:
        raise PromptGenerationError(str(exc)) from exc

    prompt = result.text.strip()
    if not prompt:
        raise PromptGenerationError("Prompt writer returned empty prompt")
    round_.prompt = prompt
    round_.status = JobState.PROMPT_READY
    job.status = JobState.PROMPT_READY
    add_log(
        db,
        job_id=job.id,
        round_id=round_.id,
        stage=JobState.PROMPT_READY,
        message="Prompt writer generated the first Trae prompt.",
        extra={"model": result.model, "wire_api": result.wire_api, "prompt_chars": len(prompt)},
    )
    return prompt


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


def _format_rules(rules: dict[str, str]) -> str:
    return "\n\n".join([f"## {name}\n{content}" for name, content in rules.items()])
