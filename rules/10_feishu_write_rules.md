# 10 Feishu Write Rules

- Do not overwrite a business row that already has Trae Session ID.
- Write the next empty Trae Session ID row or a row explicitly selected by the user.
- Required fields include Trae Session ID, round, prompt, task type, business domain, modification scope, completion, satisfaction, GitHub URL, branch/folder, and trace.
- 日志轨迹 is required and must be verified full trace.
- If trace exceeds field limit, write `因日志超长已经保存txt文档，放在截图列。` and upload the full txt as an attachment.
- Preserve screenshot attachment and append txt attachment; do not overwrite.
- If dissatisfied, task completion must be `未完成任务`.
- If satisfied, dissatisfaction reason must be empty.
- If no empty row exists and create record is forbidden, stop the job and create an automation error.
