# ADBZ Role Capability Gap Checklist

Baseline: `D:\adbz` as of 2026-06-10.

Goal: the server platform must reproduce the `D:\adbz` automation loop while keeping the platform-only design: multi-user isolation, per-user configuration, per-user role sets, editable/chat-editable rules, and rule learning/distribution.

## Role-by-role status

### 1. Orchestrator

ADBZ source:

- `trae_autorun_cycle.py`
- `trae_autorun_forever.py`

Platform status:

- Start, Continue, Stop APIs exist.
- Single-round worker command chain exists: send prompt, wait, copy trace, screenshot, scan/review, browser acceptance, GitHub, Feishu.
- Runtime cleanup preserves user settings and rules.

Completed in this pass:

- After a dissatisfied Feishu business write succeeds, the platform now prepares the next round instead of always closing the project.
- Satisfied results, fifth round, and daily target still close the project.

Gaps:

- Not yet equivalent to the ADBZ continuous loop: direction queue, satisfied-ratio cap, first-round gate, first-round discard/retry, and automatic dispatch of the prepared next round.
- Continue does not yet preserve the same rich pending state as `pending_prompt/current_task/direction_queue`.
- First-round "satisfied means discard, do not submit GitHub/Feishu" is not implemented in code.
- Feishu/GitHub failure currently stops the platform job, but external automation notification parity is still missing.

Next implementation steps:

- Add server-side round policy helpers for max rounds, first-round gate, satisfied ratio, and next-round creation.
- After Feishu success, create the next `TaskRound` or next project instead of always marking `PROJECT_COMPLETED`.
- Add tests for first-round satisfied discard, first-round dissatisfied formal write, fifth-round closure, and daily limit.

### 2. Prompt Writer

ADBZ source:

- `llm_roles.py`
- `trae_autorun_cycle.py`

Platform status:

- Per-user role and rules are loaded.
- LLM generation with local fallback exists.
- Basic forbidden dissatisfaction/meta phrase and duplicate prompt checks exist.

Completed in this pass:

- Rejects reused internal template phrases such as `判定依据`.
- Rejects first-round fixed template prefixes such as `按这个项目方向做一个能继续迭代的系统雏形`.
- Rejects first-round positive scope that asks for an overly small page/demo.

Gaps:

- Follow-up prompt generation is still first-round shaped; it does not yet use previous dissatisfaction, current project state, or bugfix/feature ratio.
- Soft rewrite/naturalization of repeated prompts is not implemented; platform falls back instead of rewriting.
- Pending prompts already submitted to Trae are not modeled deeply enough for "do not rewrite old pending" behavior.

Next implementation steps:

- Add prompt context from previous round logs and dissatisfaction reason.
- Add prompt kind selection: new, followup, bugfix.
- Add soft rewrite helper instead of failing generation on similarity.

### 3. Worker Controller

ADBZ source:

- `trae_prompt_input.py`
- `trae_ui_reply.py`
- `trae_ui_diagnose.py`
- `trae_auto_intervene.py`
- `trae_watch_and_submit.py`

Platform status:

- Windows worker can register and poll server commands.
- Worker supports opening/focusing Trae, sending prompt, waiting, continue click, copying latest reply, screenshot, scan project, run command, browser acceptance, Git submit.
- Worker has exe/package direction.

Gaps:

- UIA safety needs more real-world validation against Trae CN: bottom reply copy button, left panel scroll-to-bottom, output-too-long continue, run/confirm/keep/save actions.
- Worker is command-driven; the server does not yet model all ADBZ watcher states such as `awaiting_current_continuation`, `stuck_trace_missing_abort`, and local recovery.
- No service/auto-start by design, per user request.

Next implementation steps:

- Expand worker result statuses for stuck/local-recovery/current-continuation.
- Add server-side handling for those statuses without submitting GitHub/Feishu.
- Continue verifying packaged worker on a clean machine or clean venv-free directory.

### 4. Trace Collector / Validator

ADBZ source:

- `trae_collect_real.py`
- `trae_ui_reply.py`
- `trae_watch_and_submit.py`
- `check_trace_and_prompt_rules.py`

Platform status:

- Trace validation blocks downstream GitHub/Feishu when trace is too short, pseudo-local, or incomplete.
- Long trace overflow is stored as txt attachment and Feishu field gets the ADBZ placeholder.
- Recoverable incomplete trace queues continue recovery.

Gaps:

- Does not yet inspect Trae local DB/logs to detect newer active continuation turns.
- Does not yet distinguish every ADBZ interruption signal (`task_failed`, `send_error`, `ErrorResponse`) across DB/log sources.
- Local recovery and stuck fallback drafts are not modeled as first-class server records.

Next implementation steps:

- Add worker command/result for Trae local state probe.
- Add non-business automation error records for trace missing/stuck.

### 5. Product Reviewer

ADBZ source:

- `product_reviewer.py`
- `fill_feishu_row.py`

Platform status:

- Worker scanner returns static product review evidence.
- Product review can block browser/GitHub/Feishu.
- Build/test command chain is supported.

Gaps:

- Browser acceptance is still shallower than ADBZ target: mostly HTTP/HTML inspection, not full browser workflow evidence with clicks, console errors, screenshots, and primary workflow assertions.
- Review does not yet prioritize changed files as strongly as ADBZ.
- Domain-specific acceptance wording from ADBZ is only partially reflected.

Next implementation steps:

- Add Playwright/browser workflow evidence command for local URLs.
- Carry changed file list from Git/scan into review prioritization.

### 6. Dissatisfaction Writer

ADBZ source:

- `fill_feishu_row.py`
- `llm_roles.py`

Platform status:

- Generates both `产物不满意：` and `过程不满意：`.
- Uses real worker/build/browser/GitHub/Feishu evidence.

Gaps:

- Current text can still sound too machine-like for process reasons.
- Domain guardrails are much thinner than ADBZ: TMC/logistics/community/AgentOps wording protection is incomplete.
- LLM-backed reason generation is not yet wired with the same JSON role contract as ADBZ.

Next implementation steps:

- Port domain hint and humanized issue helpers from `fill_feishu_row.py`.
- Add tests for cross-domain contamination and build-error extraction.

### 7. GitHub Submitter

ADBZ source:

- `git_auto_submit.py`
- `fill_feishu_row.py`

Platform status:

- Worker can commit/push.
- API can ensure GitHub repository using user token.
- GitHub failure aborts Feishu business write.

Completed in this pass:

- Feishu `github地址` is normalized to HTTPS clone URL ending in `.git`.
- SSH remotes and commit/tree/pull URLs are converted to clone URL form.

Gaps:

- Repository/project naming parity with ADBZ is partial.
- GitHub abnormal failure should trigger external automation notification, not only UI runtime logs.

Next implementation steps:

- Add tests for SSH remote normalization and no-token failure path.
- Add automation notification role flow.

### 8. Feishu Writer

ADBZ source:

- `feishu_bitable.py`
- `fill_feishu_row.py`
- `feishu_notify.py`
- `automation_exception_notify.py`

Platform status:

- Tenant/user token flow exists.
- Writes next empty `Trae Session ID` row by UID order.
- Does not overwrite filled fields by default.
- Long traces and screenshot attachments are supported.

Completed in this pass:

- Supports explicit target record by `record_id` or `UID`.
- For filled target rows, only configured `overwrite_fields` or empty fields are updated; `Trae Session ID` is protected.
- Duplicate detection now checks exact Session ID and prompt+round+task/domain matches.
- AgentOps/full-stack field inference is protected so platform work is written as `全栈Web应用`.
- Task type now considers the current round prompt, and modification scope handles numeric changed-file counts.

Gaps:

- Field inference is still thinner than ADBZ for TMC/logistics/community domain-specific wording and satisfaction details.
- Automation error notification via Feishu message is not equivalent yet.
- The "do not click Feishu AI quality check" rule exists, but there is no UI automation path to enforce because platform writes by API only.

Next implementation steps:

- Port field inference helpers for AgentOps/full-stack/TMC/logistics/community cases.
- Add explicit no-empty-row/403 automation notification handling.

### 9. Rule Collector / Role Workspace

ADBZ source:

- Design requirement, not a direct ADBZ script.

Platform status:

- User roles, user rule files, role chat messages, and rule center exist.
- Rule collector role has rules for reading docs and proposing diffs.

Gaps:

- Online document learning and distribution to affected role rule files needs end-to-end implementation/verification.
- Chat-based rule modification does not yet fully enforce preview diff, confirmation, conflict detection, and role-specific distribution.

Next implementation steps:

- Add role chat apply-preview API tests.
- Add document ingestion path and rule proposal persistence.

## Current priority order

1. Finish Feishu/GitHub/prompt role parity tests from this pass.
2. Port Feishu field inference and dissatisfaction humanization.
3. Implement multi-round scheduler policies.
4. Expand worker/trace stuck and local recovery states.
5. Deepen browser acceptance.
6. Complete rule collector document-learning flow.
