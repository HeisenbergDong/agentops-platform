TRACE_TOOL_MARKERS = ("toolName:", "status:", "filePath:", "command:", "Todos updated:")
TOOL_TRACE_REQUIRED_MARKERS = ("toolName:", "Todos updated:", "filePath:", "command:")
PARTIAL_COPY_MARKERS = ("PlainText", "\u590d\u5236\u4ee3\u7801", "Copy code")
CONTINUE_MARKERS = (
    "\u8f93\u51fa\u8fc7\u957f",
    "\u8f93\u51fa\u8fc7\u957f\uff0c\u8bf7\u8f93\u5165\u201c\u7ee7\u7eed\u201d",
    "\u8bf7\u8f93\u5165\u201c\u7ee7\u7eed\u201d\u540e\u83b7\u5f97\u66f4\u591a\u7ed3\u679c",
    "\u7ee7\u7eed\u540e\u83b7\u5f97\u66f4\u591a",
    "\u7ee7\u7eed\u751f\u6210",
    "\u70b9\u51fb\u7ee7\u7eed",
    "\u66f4\u591a\u7ed3\u679c",
    "exceeded output window",
    "input continue",
    "type continue",
    "click continue",
    "continue generating",
    "more results",
)
CONTINUE_BARE_MARKERS = ("\u7ee7\u7eed", "continue")
FINAL_SUMMARY_MARKERS = (
    "\u6784\u5efa\u5b8c\u6210",
    "\u4fee\u590d\u5b8c\u6210",
    "\u672c\u6b21\u4fee\u590d",
    "\u8fd0\u884c\u547d\u4ee4",
    "\u6838\u5fc3\u7279\u6027",
    "\u9879\u76ee\u6280\u672f\u6808",
    "\u4ea4\u4ed8",
    "\u9a8c\u8bc1\u7ed3\u679c",
    "\u6d4b\u8bd5\u7ed3\u679c",
    "summary",
    "implemented",
    "completed",
)
RECOVERABLE_TRACE_REASONS = {
    "empty_trace",
    "trace_too_short",
    "awaiting_continuation",
    "partial_code_copy",
    "final_summary_only",
    "missing_tool_trace_markers",
}


def validate_full_trace(text: str) -> dict:
    normalized = (text or "").strip()
    if not normalized:
        return {"valid": False, "reason": "empty_trace"}
    if _looks_like_final_summary_only(normalized):
        return {"valid": False, "reason": "final_summary_only"}
    if _needs_continue(normalized):
        return {"valid": False, "reason": "awaiting_continuation"}
    if _looks_like_partial_code_copy(normalized):
        return {"valid": False, "reason": "partial_code_copy"}
    if len(normalized) < 800:
        return {"valid": False, "reason": "trace_too_short"}
    marker_count = sum(1 for marker in TRACE_TOOL_MARKERS if marker in normalized)
    required_marker_count = sum(1 for marker in TOOL_TRACE_REQUIRED_MARKERS if marker in normalized)
    if marker_count == 0 or required_marker_count == 0:
        if _looks_like_final_summary_only(normalized):
            return {"valid": False, "reason": "final_summary_only"}
        return {"valid": False, "reason": "missing_tool_trace_markers"}
    if "toolName:" in normalized and "status:" not in normalized:
        return {"valid": False, "reason": "missing_tool_trace_markers"}
    return {"valid": True, "reason": "ok"}


def is_recoverable_trace_reason(reason: str) -> bool:
    return reason in RECOVERABLE_TRACE_REASONS


def _needs_continue(text: str) -> bool:
    tail = text[-1600:].lower()
    if any(marker.lower() in tail for marker in CONTINUE_MARKERS):
        return True
    tail_lines = [line.strip().lower() for line in text.splitlines()[-8:] if line.strip()]
    return any(line in CONTINUE_BARE_MARKERS for line in tail_lines)


def _looks_like_final_summary_only(text: str) -> bool:
    if any(marker in text for marker in TOOL_TRACE_REQUIRED_MARKERS):
        return False
    normalized = text.lower()
    if len(text) < 600:
        return any(marker.lower() in normalized for marker in FINAL_SUMMARY_MARKERS)
    return any(marker.lower() in normalized for marker in FINAL_SUMMARY_MARKERS)


def _looks_like_partial_code_copy(text: str) -> bool:
    if any(marker in text for marker in TOOL_TRACE_REQUIRED_MARKERS):
        return False
    if any(marker in text[:500] for marker in PARTIAL_COPY_MARKERS):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    fenced = lines[0].startswith("```") or lines[-1].startswith("```")
    codeish = sum(
        1
        for line in lines
        if line.startswith(("import ", "from ", "export ", "const ", "let ", "var ", "function ", "class "))
        or line.endswith(("{", "}", ";"))
        or line.startswith(("<", "</"))
    )
    return fenced or (len(lines) <= 120 and codeish >= max(6, len(lines) // 3))
