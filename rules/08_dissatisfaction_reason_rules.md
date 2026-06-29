# 08 Dissatisfaction Reason Rules

When dissatisfaction is selected, the reason must contain both:

- `产物不满意：`
- `过程不满意：`

When satisfaction is selected, dissatisfaction reason must be empty.

## Specificity

- Current task comes first. Do not write only historical legacy problems. Write the current evaluation first, then optional historical issues.
- Product dissatisfaction must be locatable: name the file, page, API, route, function, or feature when available; explain the code-level problem, objective symptom, unmet current requirement, and root cause.
- Process dissatisfaction must include trigger node, actual model behavior, and business impact.
- If product and process are connected, write the causal chain naturally: where the model acted, what it did, what product issue resulted, how it appeared, which requirement failed, and what the correct approach should have been.
- If the model created a todo list before understanding the prompt or project, explain which todo items were unreasonable and why.
- Page problems should be grounded in rendering, route, empty page, 404, missing entry, missing state change, or visible interaction failure.
- API problems should distinguish HTTP 400/500 and identify whether the issue is front-end parameters, backend route/handler, validation, permissions, or response handling.
- If the whole project cannot run, identify the concrete compile, dependency, syntax, startup, or command failure.
- Business logic gaps must name the exact business operation that is incomplete and include one example.
- Do not paste large error blocks. Extract 1-4 key lines and explain the cause.
- Reasons should read like human review, not a fixed template. Keep wording varied across rounds.

## Style

- Use `我` instead of `用户`.
- Use `模型` instead of `Trae`.
- Make definite judgments. Do not write `可能`.
- Do not write `判定依据`.
- Do not mention tool-call statistics.
- Do not reuse fixed boilerplate.
- Do not cross business domains.
- Do not use internal round labels such as `本轮`, `第x轮`, or `0-1代码生成` in user-visible reasons.
- Do not pile up internal field names or tool names such as `日志轨迹`, `日志`, `轨迹`, `edit_file_search_replace`, `Write`, `changes`, or `npm install undefined`.
- Do not copy the prompt, prior dissatisfaction reason, or AI review text verbatim.
- Do not mention 3003/model failure unless the trace or screen evidence explicitly shows that failure.
- Avoid batches of similar reasons. If recent reasons are similar, rewrite around the current file, function, action, and evidence.

## Evidence

- Reasons must come from code review, build/test output, browser acceptance, Trae trace, or current changed files.
- Do not invent broken buttons, login failures, or flow failures without evidence.
- If build fails, include 1-4 key error lines.
- Use one representative file path/line example for similar code issues.
- If automation stopped before trace collection, the process dissatisfaction must say the chain did not yet produce verified Trae trace/evidence; do not pretend GitHub or Feishu evidence exists.
- If test mode continues after incomplete trace, label it as a test exception and do not describe it as formal business acceptance.
- If the Worker stop report shows local cleanup succeeded but Trae was not safely stopped, distinguish that from product quality. It is process evidence, not a product bug by itself.
