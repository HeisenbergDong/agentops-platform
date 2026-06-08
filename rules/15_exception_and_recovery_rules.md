# 15 Exception And Recovery Rules

Automation exceptions include:

- Trae stuck.
- Trace missing or incomplete.
- Worker offline.
- GitHub network/credential/permission failure.
- Feishu token/API/empty-row failure.
- Local project exists but no verified completed trace.
- Duplicate submission risk.

Rules:

- Business table writes are forbidden during automation exceptions.
- Save pending drafts locally and in the server database when possible.
- Notify through configured alert channel.
- Allow retry, skip, stop, or manual takeover from the exception center.
- Local recovery without real trace cannot be converted into 日志轨迹.
