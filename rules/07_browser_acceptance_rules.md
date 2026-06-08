# 07 Browser Acceptance Rules

- Start local projects only with allowed commands.
- Capture URL, screenshot, console errors, and network failures.
- Visit core pages and click primary business actions when the app is runnable.
- Record what was clicked, expected state change, and actual result.
- A browser click-through does not override a failed production build.
- If there is no real browser evidence, do not invent UI failures.
- Empty pages, runtime errors, missing navigation, and no feedback after action are valid review signals when observed.
