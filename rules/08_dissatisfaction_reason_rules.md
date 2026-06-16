# 08 Dissatisfaction Reason Rules

When dissatisfaction is selected, the reason must contain both:

- `产物不满意：`
- `过程不满意：`

When satisfaction is selected, dissatisfaction reason must be empty.

## Style

- Use `我` instead of `用户`.
- Use `模型` instead of `Trae`.
- Make definite judgments. Do not write `可能`.
- Do not write `判定依据`.
- Do not mention tool-call statistics.
- Do not reuse fixed boilerplate.
- Do not cross business domains.

## Evidence

- Reasons must come from code review, build/test output, browser acceptance, Trae trace, or current changed files.
- Do not invent broken buttons, login failures, or flow failures without evidence.
- If build fails, include 1-4 key error lines.
- Use one representative file path/line example for similar code issues.
- If automation stopped before trace collection, the process dissatisfaction must say the chain did not yet produce verified Trae trace/evidence; do not pretend GitHub or Feishu evidence exists.
- If test mode continues after incomplete trace, label it as a test exception and do not describe it as formal business acceptance.
- If the Worker stop report shows local cleanup succeeded but Trae was not safely stopped, distinguish that from product quality. It is process evidence, not a product bug by itself.
