from app.services.orchestrator.dissatisfaction import DissatisfactionEvidence, _compact_evidence_for_reviewer


def test_dissatisfaction_reviewer_context_uses_product_review_and_dual_prompt():
    evidence = DissatisfactionEvidence(
        failure_stage="product_reviewing",
        failure_message="Product review found blocking artifact issues.",
        original_user_requirement="用户原始需求：做招聘平台",
        prompt="Trae prompt: 实现职位和候选人工作台",
        data={
            "product_review": {
                "issues": ["src/App.tsx:12 保存按钮没有更新状态"],
                "warnings": ["src/App.tsx 还有 TODO"],
                "changed_files": ["src/App.tsx"],
                "evidence": ["GitHub review snapshot abc123"],
            }
        },
    )

    context = _compact_evidence_for_reviewer(evidence, {"reason": "draft"}, "")

    assert context["draft"]["Original User Requirement"] == "用户原始需求：做招聘平台"
    assert context["draft"]["Trae Prompt Sent"] == "Trae prompt: 实现职位和候选人工作台"
    assert context["product_review"]["issues"] == ["src/App.tsx:12 保存按钮没有更新状态"]
    assert context["code_review"]["deprecated"] is True
    assert context["code_review"]["use"] == "product_review"
