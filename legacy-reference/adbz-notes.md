# D:\adbz Reference Notes

`D:\adbz` is the current local automation implementation and should be treated as a reference source, not as the final platform runtime.

Capabilities to migrate:

- Prompt generation and cycle state ideas from `trae_autorun_cycle.py`.
- Trae UI diagnosis and intervention ideas from `trae_ui_diagnose.py` and `trae_auto_intervene.py`.
- Full assistant trace copying and validation ideas from `trae_ui_reply.py`.
- Product review checks from `product_reviewer.py`.
- Feishu mapping and hard validation from `fill_feishu_row.py`.
- GitHub submit behavior from `git_auto_submit.py`.
- Role calling patterns from `llm_roles.py`.

Final placement:

- GUI-only behavior belongs in `apps/worker-windows`.
- Orchestration, rules, roles, Feishu, GitHub, and persistence belong in `apps/api`.
