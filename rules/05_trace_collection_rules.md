# 05 Trace Collection Rules

`日志轨迹` means the complete original Trae assistant reply for the current prompt, copied from the assistant reply area.

## Accepted Source

- Prefer the copy button in the bottom toolbar of the full assistant reply.
- The trace should include process text, Trae tool blocks, and final delivery summary.

## Rejected Source

- Code block copy.
- PlainText copy.
- Command/table fragment copy.
- Local structured summaries.
- Product review summaries.
- Automation error messages.
- GitHub or Feishu troubleshooting text.
- Replies that still ask the user to continue for more output.

## Continuation

If output is too long and asks for continuation, click the real continue button first. If button diagnosis fails, type `继续`. Re-copy the latest full assistant reply after continuation completes.

## Abort

If the trace is missing, too short, copied from the wrong source, incomplete, or unverifiable, enter trace_missing_abort and do not submit GitHub or Feishu business data.
