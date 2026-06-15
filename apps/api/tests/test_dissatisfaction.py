from app.services.orchestrator.dissatisfaction import (
    DissatisfactionEvidence,
    generate_dissatisfaction_reason,
)


def test_dissatisfaction_reason_humanizes_feishu_403():
    result = generate_dissatisfaction_reason(
        DissatisfactionEvidence(
            failure_stage="feishu_failed_abort",
            failure_message=(
                "Feishu write failed: HTTP 403: code=99991663, msg=Permission denied. "
                "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403"
            ),
            prompt="做一个订单管理系统",
            trace_text="toolName: edit\nstatus: success\n" * 40,
        )
    )

    reason = result["reason"]
    assert "产物不满意：" in reason
    assert "过程不满意：" in reason
    assert "飞书接口返回 403" in reason
    assert "developer.mozilla.org" not in reason
    assert "关键证据" not in reason
    assert "判定依据" not in reason


def test_dissatisfaction_reason_uses_domain_hint_without_inventing_clicks():
    result = generate_dissatisfaction_reason(
        DissatisfactionEvidence(
            failure_stage="browser_accepting",
            failure_message="Browser acceptance did not pass with usable local page evidence.",
            prompt="做一个 TMC 快递系统，包含下单、网点接单、骑手取件、派送和异常件处理",
            data={"inspection": {"issues": ["页面正文为空或接近空白，且没有可见交互入口。"]}},
        )
    )

    reason = result["reason"]
    assert "快递下单、网点接单、骑手取派、异常件处理和时效统计" in reason
    assert "我点了" not in reason
    assert "可能" not in reason


def test_dissatisfaction_reason_agentops_uses_agentops_acceptance_without_monitor_leak():
    result = generate_dissatisfaction_reason(
        DissatisfactionEvidence(
            failure_stage="browser_accepting",
            failure_message="Browser acceptance did not pass with usable local page evidence.",
            prompt="AgentOps 多角色 LLM + Windows Worker 自动作业平台，需要提示发送、底部日志复制、代码审查、浏览器验收和飞书预览闭环",
            data={"inspection": {"issues": ["任务详情页没有展示底部日志复制状态。"]}},
        )
    )

    reason = result["reason"]
    assert "提示发送" in reason
    assert "底部日志复制" in reason
    assert "全过程链路看板" not in reason
    assert "告警规则" not in reason
    assert result["task_done"] == "未完成任务"


def test_dissatisfaction_reason_tmc_does_not_cross_domain_to_community_or_logistics():
    result = generate_dissatisfaction_reason(
        DissatisfactionEvidence(
            failure_stage="product_reviewing",
            failure_message="Product review found missing acceptance evidence.",
            prompt="做一个 TMC 快递系统，包含下单、网点接单、骑手取件、派送签收和异常件处理",
            data={"product_review": {"issues": ["没有看到异常件处理入口。"]}},
        )
    )

    reason = result["reason"]
    assert "快递下单" in reason
    assert "网点接单" in reason
    assert "异常件处理" in reason
    assert "举报入口" not in reason
    assert "帖子/消息" not in reason
    assert "装车失败" not in reason


def test_dissatisfaction_reason_can_force_labeled_test_unsatisfied():
    result = generate_dissatisfaction_reason(
        DissatisfactionEvidence(
            failure_stage="browser_accepting",
            failure_message="Browser acceptance passed, but user asked for a test dissatisfaction record.",
            prompt="本轮是测试，满意也写成不满意",
            orchestrator_intent={
                "run_mode": "test",
                "dissatisfaction_policy": "force_test_unsatisfied",
            },
        )
    )

    assert result["test_mode"] is True
    assert "链路验证测试" in result["reason"]
    assert "正式业务验收" in result["reason"]
