# 06 Product Review Rules

- GitHub review snapshot is the primary review source when available. Review the commit diff and selected files before relying on local script heuristics.
- Local scripts and command outputs are evidence collectors/executors, not the final reviewer. Use them to locate files, errors, screenshots, and browser results; then reason over the current task.
- Review changed files first.
- Ignore node_modules, dist, build, target, .venv, __pycache__, and .git.
- Run relevant install/build/test/static checks when possible.
- Extract concrete build errors instead of generic failure wording.
- Check project scale, modules, business keywords, interaction entry points, state feedback, exception handling, and data flow.
- Identify code issues such as empty handlers, TODO logic, alert/confirm, swallowed exceptions, missing API calls, and route/state mismatch.
- Keep evidence specific and concise with file path and line number when available.
- For each blocking issue, prefer this evidence shape internally: current requirement, file/page/API/function, code-level problem, objective symptom, unmet requirement, root cause, and impact.
- Do not copy the user's prompt as the review result. Do not copy another AI review verbatim. Rewrite into concrete review facts.
- Product review results are internal evidence and must never be appended to 日志轨迹.
