import hashlib
import time

from worker.trae.window import TraeAutomationError, find_trae_window, focus_trae, window_text_snapshot

BUSY_MARKERS = (
    "\u751f\u6210\u4e2d",
    "\u6b63\u5728\u751f\u6210",
    "\u505c\u6b62\u751f\u6210",
    "Stop generating",
    "Generating",
    "Running",
    "Thinking",
)


def wait_completion(
    timeout_seconds: float = 900.0,
    stable_seconds: float = 15.0,
    poll_interval_seconds: float = 2.0,
) -> dict:
    focus_trae(timeout_seconds=min(10.0, timeout_seconds))
    deadline = time.monotonic() + timeout_seconds
    stable_since: float | None = None
    last_signature = ""
    latest_text = ""

    while time.monotonic() < deadline:
        window = find_trae_window(timeout_seconds=2.0)
        latest_text = window_text_snapshot(window)
        signature = hashlib.sha256(latest_text.encode("utf-8", errors="ignore")).hexdigest()
        busy = any(marker in latest_text for marker in BUSY_MARKERS)
        if not latest_text.strip():
            stable_since = None
            last_signature = signature
        elif not busy and signature == last_signature:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= stable_seconds:
                return {
                    "status": "completed",
                    "stable_seconds": stable_seconds,
                    "text_chars": len(latest_text),
                    "text_sample": latest_text[-1000:],
                }
        else:
            stable_since = None
            last_signature = signature
        time.sleep(poll_interval_seconds)

    raise TraeAutomationError("Trae output did not become stable before wait_completion timeout")
