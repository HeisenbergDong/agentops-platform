TRACE_TOOL_MARKERS = ("toolName:", "status:", "filePath:", "command:", "Todos updated:")
PARTIAL_COPY_MARKERS = ("PlainText", "```", "复制代码")
CONTINUE_MARKERS = ("输出过长", "请输入", "继续后获得更多", "continue")


def validate_full_trace(text: str) -> dict:
    normalized = (text or "").strip()
    if not normalized:
        return {"valid": False, "reason": "empty_trace"}
    if len(normalized) < 800:
        return {"valid": False, "reason": "trace_too_short"}
    if any(marker in normalized[-300:] for marker in CONTINUE_MARKERS):
        return {"valid": False, "reason": "awaiting_continuation"}
    marker_count = sum(1 for marker in TRACE_TOOL_MARKERS if marker in normalized)
    if marker_count == 0:
        return {"valid": False, "reason": "missing_tool_trace_markers"}
    return {"valid": True, "reason": "ok"}
