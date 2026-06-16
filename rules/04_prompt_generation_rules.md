# 04 Prompt Generation Rules

- Prompts must directly describe the requested work.
- Prompts must not contain acceptance conclusions or dissatisfaction wording.
- Forbidden prompt phrases include: 产物不满意, 过程不满意, 结果不满意, 不满意原因, 证据：.
- First-round prompts must create an iterable system prototype, not a single-file minimal static demo.
- First-round prompts should include multiple business modules, multiple views, data model, list/detail/edit or operation area, state feedback, local mock data, and key interactions.
- Follow-up prompts must continue the current project.
- Bugfix prompts must rewrite prior problems as user operation phenomenon, expected result, and review method.
- Do not copy prior dissatisfaction reason text into prompts.
- Avoid fixed opening templates and repeated sentence structures.
- Soft de-duplication should rewrite prompts naturally rather than stopping the round.
- The prompt writer should understand that AgentOps is automating the user's real manual workflow, but prompts sent to Trae must read like normal user development requests.
- Do not put internal platform workflow terms such as trace gate, GitHub evidence commit, Feishu write, Worker stop report, or scheduler state into Trae prompts unless the requested product itself is AgentOps and those are actual product features.
- When a round is a resume after Stop, the prompt must ask Trae to continue from the interrupted point, preserve existing files and structure, and avoid rebuilding the project from scratch.
- Prompt writing should help Trae finish with a concise final reply after code changes, but it must not ask Trae to expose platform internals. The platform will separately decide completion, trace collection, GitHub, and Feishu flow.
