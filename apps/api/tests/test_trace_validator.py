from app.services.trace.validator import validate_full_trace


def test_trace_validator_accepts_full_tool_trace():
    trace = (
        "toolName: edit\n"
        "status: success\n"
        "filePath: app.py\n"
        "command: pytest\n"
        "Todos updated: done\n"
        + ("trace detail line\n" * 80)
    )

    assert validate_full_trace(trace) == {"valid": True, "reason": "ok"}


def test_trace_validator_rejects_final_summary_only_as_recoverable():
    trace = (
        "\u6784\u5efa\u5b8c\u6210\uff0c\u6d4b\u8bd5\u7ed3\u679c\u901a\u8fc7\uff0c"
        "\u9879\u76ee\u6280\u672f\u6808\u4e3a React\u3002\u672c\u6b21\u4fee\u590d\u5b8c\u6210\u3002\n"
        * 60
    )

    assert validate_full_trace(trace) == {"valid": False, "reason": "final_summary_only"}


def test_trace_validator_rejects_partial_code_copy():
    trace = "```tsx\n" + ("const value = 1;\n" * 80) + "```\n"

    assert validate_full_trace(trace) == {"valid": False, "reason": "partial_code_copy"}


def test_trace_validator_rejects_single_tool_fragment():
    trace = (
        "toolName: view_folder\n"
        "status: success\n"
        "d:\\code-space\\coding-soler\\workspace-dashboard-analytics-19cc3bb3\n"
        + ("我先探索相关代码文件。\n" * 80)
    )

    assert validate_full_trace(trace) == {"valid": False, "reason": "partial_tool_trace"}


def test_trace_validator_rejects_trace_that_requires_continue():
    trace = (
        "toolName: edit\n"
        "status: success\n"
        "filePath: app.py\n"
        "command: pytest\n"
        + ("trace detail line\n" * 80)
        + "\u8f93\u51fa\u8fc7\u957f\uff0c\u8bf7\u8f93\u5165\u201c\u7ee7\u7eed\u201d\u540e\u83b7\u5f97\u66f4\u591a\u7ed3\u679c"
    )

    assert validate_full_trace(trace) == {"valid": False, "reason": "awaiting_continuation"}


def test_trace_validator_rejects_service_interruption_as_recoverable():
    trace = (
        "toolName: edit\n"
        "status: success\n"
        "filePath: app.py\n"
        "command: pytest\n"
        + ("trace detail line\n" * 80)
        + "\u670d\u52a1\u7aef\u5f02\u5e38\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5"
    )

    assert validate_full_trace(trace) == {"valid": False, "reason": "service_interrupted"}


def test_trace_validator_allows_body_mentions_of_continue():
    trace = (
        "toolName: edit\n"
        "status: success\n"
        "filePath: app.py\n"
        "command: pytest\n"
        "Todos updated: done\n"
        + ("continue detail line but finished\n" * 80)
    )

    assert validate_full_trace(trace) == {"valid": True, "reason": "ok"}
