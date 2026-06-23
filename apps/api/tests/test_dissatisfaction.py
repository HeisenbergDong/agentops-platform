from app.services.orchestrator.dissatisfaction import (
    DissatisfactionEvidence,
    generate_dissatisfaction_reason,
)


def test_dissatisfaction_reason_skips_platform_feishu_failure():
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

    assert result["status"] == "skipped_platform_record_write_failure"
    assert result["reason"] == ""
    assert result["product_reason"] == ""
    assert result["process_reason"] == ""
    assert "飞书接口返回 403" in result["platform_failure"]
    assert "developer.mozilla.org" not in result["platform_failure"]


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


def test_dissatisfaction_reason_states_specific_browser_failure_not_uncertainty():
    result = generate_dissatisfaction_reason(
        DissatisfactionEvidence(
            failure_stage="browser_accepting",
            failure_message="Browser acceptance did not pass with usable local page evidence.",
            prompt="招聘方工作台首页要直达工作台，并支持职位、候选人、面试和统计四个入口",
            data={
                "status": "failed",
                "url": "http://localhost:5174/",
                "http_status": 500,
                "inspection": {
                    "issues": ["页面包含运行错误或构建错误信号：Internal Server Error"],
                    "interaction": {"total": 0, "button_labels": []},
                },
            },
        )
    )

    reason = result["reason"]
    assert "http://localhost:5174/ 浏览器验收失败" in reason
    assert "HTTP 状态为 500" in reason
    assert "页面没有检测到可操作入口" in reason
    assert "未确认" not in reason
    assert "不能确认" not in reason
    assert "无法确认" not in reason


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
    assert reason.startswith("产物不满意：")
    assert "\n过程不满意：" in reason
    assert "提示发送" in reason
    assert "底部过程记录复制" in reason
    body = reason.replace("产物不满意：", "").replace("过程不满意：", "")
    assert "日志" not in body
    assert "轨迹" not in body
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


def test_dissatisfaction_reason_prioritizes_static_code_review_findings():
    result = generate_dissatisfaction_reason(
        DissatisfactionEvidence(
            failure_stage="product_reviewing",
            failure_message="Product review found blocking code issues.",
            prompt="招聘方工作台要支持职位、候选人、面试和统计联动",
            data={
                "product_review": {
                    "issues": [
                        "frontend/src/api/job.js:12 接口请求失败后直接吞掉错误，职位列表不能显示失败反馈。",
                        "frontend/src/components/JobForm.jsx:20 事件绑定为空：<button onClick={}>保存</button>",
                    ],
                    "warnings": ["frontend/src/App.jsx 仍包含 TODO 标记。"],
                    "changed_files": ["frontend/src/api/job.js", "frontend/src/components/JobForm.jsx"],
                    "evidence": ["审查了 18 个项目文件，其中主要代码文件 9 个。"],
                    "stack": ["React", "Go"],
                    "file_count": 18,
                }
            },
        )
    )

    reason = result["reason"]
    assert "代码审查发现" in reason
    assert "frontend/src/api/job.js" in reason
    assert "frontend/src/components/JobForm.jsx" in reason
    assert "事件绑定为空" in reason
    assert "未确认" not in reason
    assert "不能确认" not in reason
    assert "无法确认" not in reason


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
    assert result["reason"].startswith("产物不满意：")
    assert "\n过程不满意：" in result["reason"]
    assert "链路验证测试" in result["reason"]
    assert "正式业务验收" in result["reason"]
