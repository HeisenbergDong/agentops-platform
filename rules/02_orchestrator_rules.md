# 02 Orchestrator Rules

## Start

When the user clicks Start, ask for or use selected project directions, clean old runtime logs/screenshots/pending state, preserve configuration/secrets/rules/history, create a new job, load the active rule version, and dispatch the first prompt generation.

## Continue

When the user clicks Continue, preserve pending prompt, current task, direction queue, current round, collected evidence, and local pending drafts. Do not clean runtime state.

## Stop

When the user clicks Stop, stop server scheduling, background queues, and the bound Windows Worker. Do not delete existing records.

## Gate Rules

- Trace validation must pass before screenshot, review, GitHub, or Feishu business flow proceeds.
- GitHub must complete before Feishu business write.
- GitHub or Feishu failures create automation errors and stop the current business write.
- Manual-required states must not start new Trae prompts.
