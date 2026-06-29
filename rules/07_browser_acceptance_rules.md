# 07 Browser Acceptance Rules

- When GitHub review facts or the current prompt identify specific pages/actions, prioritize those routes and interactions.
- Start local projects only with allowed commands.
- Capture URL, screenshot, console errors, and network failures.
- Visit core pages and click primary business actions when the app is runnable.
- Record what was clicked, expected state change, and actual result.
- For form/list/detail/status flows, record whether DOM text, selected record, list count, status label, statistic, or success/error feedback changed after the action.
- A browser click-through does not override a failed production build.
- If there is no real browser evidence, do not invent UI failures.
- Empty pages, runtime errors, missing navigation, and no feedback after action are valid review signals when observed.
