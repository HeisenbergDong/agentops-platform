# 13 User Config And Secret Rules

- Store user configuration per login user.
- Encrypt model keys, GitHub tokens, Feishu secrets, and webhooks.
- Display only masked secret values.
- Never include secrets in runtime logs, prompts, traces, rules, or attachments.
- Each role may have its own provider, model, temperature, token limit, timeout, and fallback model.
- Validate GitHub, Feishu, model, and Worker configuration before starting jobs.
- Token expiration should create an automation error and actionable UI prompt.
