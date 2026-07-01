from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RoleDefinition:
    key: str
    name: str
    rules: list[str]
    purpose: str
    enabled: bool = True
    model_config_key: str = "default"

    def to_dict(self) -> dict:
        return asdict(self)


ROLE_REGISTRY: list[RoleDefinition] = [
    RoleDefinition(
        key="orchestrator",
        name="主调度角色",
        rules=["01_global_rules.md", "02_orchestrator_rules.md", "12_state_machine_rules.yaml"],
        purpose="负责状态流转、任务拆解，并决定下一步调用哪个角色或 Worker。",
    ),
    RoleDefinition(
        key="orchestrator_intent",
        name="调度意图解析角色",
        rules=["01_global_rules.md", "02_orchestrator_rules.md"],
        purpose="把用户原始作业范围和补充说明解析成结构化调度意图，传给提示词、不满意原因和下游链路角色。",
    ),
    RoleDefinition(
        key="rule_collector",
        name="规则采集角色",
        rules=["00_framework_capabilities.md", "03_rule_collector_rules.md"],
        purpose="读取在线或本地需求文档，拆分并生成规则修改建议。",
    ),
    RoleDefinition(
        key="prompt_writer",
        name="提示词编写角色",
        rules=["01_global_rules.md", "04_prompt_generation_rules.md"],
        purpose="根据用户配置、规则和上下文生成首轮、追问、修复类 Prompt。",
    ),
    RoleDefinition(
        key="product_reviewer",
        name="成果检查角色",
        rules=["06_product_review_rules.md", "07_browser_acceptance_rules.md"],
        purpose="检查代码、构建测试证据、截图和浏览器验收结果。",
    ),
    RoleDefinition(
        key="dissatisfaction_writer",
        name="不满意原因角色",
        rules=["08_dissatisfaction_reason_rules.md"],
        purpose="根据真实证据生成结构化、自然的不满意原因。",
    ),
    RoleDefinition(
        key="github_submitter",
        name="GitHub 提交角色",
        rules=["09_github_rules.md"],
        purpose="根据任务和规则决定提交目标、分支、commit、push 和结果链接。",
    ),
    RoleDefinition(
        key="feishu_writer",
        name="飞书写入角色",
        rules=["10_feishu_write_rules.md"],
        purpose="刷新飞书 token、发现资源、映射字段并写入多维表格。",
    ),
    RoleDefinition(
        key="worker_controller",
        name="Worker 控制角色",
        rules=["11_worker_trae_cn_rules.md"],
        purpose="根据截图、日志和 UI 信号决定 Trae CN / Windows Worker 的安全操作。",
    ),
    RoleDefinition(
        key="flow_supervisor",
        name="Flow Supervisor",
        rules=["01_global_rules.md", "02_orchestrator_rules.md", "12_state_machine_rules.yaml"],
        purpose=(
            "Decides whether a stuck pause/resume or wait-completion recovery should continue the deterministic "
            "state machine or send a resume prompt into the existing Trae task."
        ),
    ),
]


def role_by_key(key: str) -> RoleDefinition | None:
    return next((role for role in ROLE_REGISTRY if role.key == key), None)
