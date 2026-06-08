# 06 Product Review Rules

- Review changed files first.
- Ignore node_modules, dist, build, target, .venv, __pycache__, and .git.
- Run relevant install/build/test/static checks when possible.
- Extract concrete build errors instead of generic failure wording.
- Check project scale, modules, business keywords, interaction entry points, state feedback, exception handling, and data flow.
- Identify code issues such as empty handlers, TODO logic, alert/confirm, swallowed exceptions, missing API calls, and route/state mismatch.
- Keep evidence specific and concise with file path and line number when available.
- Product review results are internal evidence and must never be appended to 日志轨迹.
