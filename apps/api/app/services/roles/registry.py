from dataclasses import dataclass


@dataclass(frozen=True)
class RoleDefinition:
    key: str
    name: str
    rules: list[str]
    purpose: str


ROLE_REGISTRY: list[RoleDefinition] = [
    RoleDefinition(
        key="orchestrator",
        name="主调度角色",
        rules=["01_global_rules.md", "02_orchestrator_rules.md", "12_state_machine_rules.yaml"],
        purpose="Owns state transitions and decides which role or worker command runs next.",
    ),
    RoleDefinition(
        key="rule_collector",
        name="规则采集角色",
        rules=["00_framework_capabilities.md", "03_rule_collector_rules.md"],
        purpose="Reads online or local documents and turns them into rule change proposals.",
    ),
    RoleDefinition(
        key="prompt_writer",
        name="写提示词角色",
        rules=["01_global_rules.md", "04_prompt_generation_rules.md"],
        purpose="Generates first-round, follow-up, and bugfix prompts.",
    ),
    RoleDefinition(
        key="product_reviewer",
        name="成果检查角色",
        rules=["06_product_review_rules.md", "07_browser_acceptance_rules.md"],
        purpose="Reviews code, build/test evidence, screenshots, and browser acceptance.",
    ),
    RoleDefinition(
        key="dissatisfaction_writer",
        name="不满意原因角色",
        rules=["08_dissatisfaction_reason_rules.md"],
        purpose="Writes human-style dissatisfaction reasons from real evidence.",
    ),
    RoleDefinition(
        key="github_submitter",
        name="GitHub 提交角色",
        rules=["09_github_rules.md"],
        purpose="Prepares and validates GitHub submission decisions.",
    ),
    RoleDefinition(
        key="feishu_writer",
        name="飞书写入角色",
        rules=["10_feishu_write_rules.md"],
        purpose="Maps fields, validates data, uploads attachments, and writes Feishu records.",
    ),
    RoleDefinition(
        key="worker_controller",
        name="Windows Worker 角色",
        rules=["11_worker_trae_cn_rules.md"],
        purpose="Decides safe Trae CN GUI actions from screenshots and UI signals.",
    ),
]


def role_by_key(key: str) -> RoleDefinition | None:
    return next((role for role in ROLE_REGISTRY if role.key == key), None)
