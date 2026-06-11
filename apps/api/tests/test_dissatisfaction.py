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
