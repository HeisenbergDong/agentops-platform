# 01 Global Rules

- Never expose model keys, GitHub tokens, Feishu secrets, passwords, or private credentials.
- Never write secrets into prompts, logs, rule files, attachments, or public UI.
- No verified full Trae assistant trace means no GitHub submission.
- No verified full Trae assistant trace means no Feishu business write.
- Automation errors must go to the exception center and notifications, not business rows.
- Daily business records are capped at 100 unless explicitly changed by rule version.
- Satisfied samples must not exceed 20 percent unless explicitly changed by rule version.
- A project has at most 5 rounds.
- A valid first round must be dissatisfied.
- Every important action must leave a runtime log and state transition record.
- The core workflow is: prompt generation -> Trae execution and UI operation -> Trae turn completion decision -> trace/evidence collection -> product/process review -> GitHub evidence commit -> Feishu write.
- Trae "keep/adopt/save changes" UI is a completion signal, not the core goal. Once Trae is reasonably complete, the next platform action is trace/evidence collection.
- Stop means pause the current automation safely: cancel scheduler work, stop project-local scripts/sandboxes, pause Trae generation when a safe stop button exists, report the stop result, and preserve resumable state.
- Formal mode must stay strict: no verified full Trae assistant trace means no GitHub submission and no Feishu business write. Test mode may continue only with an explicit test exception label.
