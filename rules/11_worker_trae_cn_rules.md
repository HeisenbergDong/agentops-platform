# 11 Worker Trae CN Rules

- Confirm the active window is Trae CN before clicking.
- Prefer real UIA or detected visual button positions over fixed coordinates.
- Before diagnosing completion or copy buttons, scroll the assistant reply area to the bottom.
- Recognize and safely click continue, confirm, execute, run, keep changes, and save buttons.
- For output-too-long prompts, click the yellow continue button first.
- Copy trace only from the full assistant reply bottom toolbar.
- If screenshot is not Trae CN or appears locked/unrelated, wait and report instead of clicking.
- If no safe action is available, return manual_required.
- Stop signal must prevent new prompts and automatic clicks.
- `stop_current_task` must run even when another command was just cancelled. It should clean workspace/project-local shells, dev servers, build/test processes, and Trae sandbox children; it must not kill the main Trae window unless explicitly requested.
- During stop, click only an explicit safe Trae stop-generation control. Never click delete, discard, reset, cancel, abandon, or close-project controls.
- The stop result must include `stopped`, cleanup status, killed process records, whether a Trae stop click was applied, whether activity still appears to be changing, and whether Continue should first send a resume prompt.
- Completion detection should return `collect_trace` when the current turn is complete or when a robust completion decision says trace collection should be attempted. Do not keep observing only because a keep/adopt/save banner remains visible.
- Treat `changes completed`, `keep changes`, `adopt`, and `save` banners as completion evidence. Treat 3003/service interruption/terminal prompts as recoverable states only when the turn is not already complete.
- If UIA reads only window chrome, combine visual analysis, project writes, Trae logs, and inactivity before giving up. Do not block trace collection solely because UIA text is sparse.
- After the scheduler passes a previous completion observation into `copy_latest_reply`, trace-copy failure means "trace unavailable after completed", not "continue Trae". Return enough diagnostics for the scheduler to stop formal mode or continue labeled test-chain mode; do not click continue from that state.
- Stop verification must not mark Trae as still generating from noisy Trae renderer/log mtime alone. Project file writes, explicit generation UI, or meaningful agent activity are stronger signals; log churn without project changes should be reported as diagnostic noise.
