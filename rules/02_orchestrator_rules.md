# 02 Orchestrator Rules

## Start

When the user clicks Start, ask for or use selected project directions, clean old runtime logs/screenshots/pending state, preserve configuration/secrets/rules/history, create a new job, load the active rule version, and dispatch the first prompt generation.

## Continue

When the user clicks Continue, preserve pending prompt, current task, direction queue, current round, collected evidence, and local pending drafts. Do not clean runtime state.

## Stop

When the user clicks Stop, stop server scheduling, background queues, and the bound Windows Worker. Do not delete existing records.

Stop is successful only after the bound Worker has had a chance to execute `stop_current_task` and report a structured result. The scheduler should log whether local project processes were killed, whether Trae stop was clicked, whether Trae still appears to be generating, and whether Continue must send a resume prompt before re-observing.

Do not treat Stop as discard/delete/reset. Preserve the job, current round, project workspace, prompt, trace candidates, and downstream evidence gathered so far.

## Trae Completion Handoff

The scheduler must not wait forever after Trae visibly finishes. Completion-to-trace handoff is a first-class transition:

1. Worker sends the prompt to Trae.
2. Worker observes Trae until the current turn is complete or safely recoverable.
3. When Trae appears complete, the next command is `copy_latest_reply`.
4. Trace validation decides whether downstream review/GitHub/Feishu may proceed.

The completion decision should combine multiple signals instead of requiring a perfect local log match: Trae task card completed, assistant reply stopped generating, project files were written, Trae logs contain a plausible completed turn, no recent meaningful activity, visual analyst says completed, or a keep/adopt/save banner is visible after changes are complete.

`keep/adopt/save changes` is evidence that Trae finished producing changes. It may be clicked only when the Worker is still in a safe UI-intervention state, but it must not block trace collection when other evidence already shows the turn is complete.

When `waiting_trae` transitions to `collecting_trace`, persist the completion observation into the next Worker command payload. If `copy_latest_reply` later fails, exhausts retries, or returns only an incomplete trace, do not enqueue `click_continue` merely because trace collection failed. A completed Trae turn with unavailable trace is a trace-gate problem, not an unfinished-Trae problem.

Formal mode must stop at `trace_missing_abort` with a clear message when the completed Trae turn cannot provide a verified trace. Test-chain mode may create a labeled test trace exception and continue screenshot/review/GitHub/Feishu validation, but every downstream record must say it is a test exception and not formal acceptance.

## Gate Rules

- Trace validation must pass before screenshot, review, GitHub, or Feishu business flow proceeds.
- GitHub must complete before Feishu business write.
- GitHub or Feishu failures create automation errors and stop the current business write.
- Manual-required states must not start new Trae prompts.
