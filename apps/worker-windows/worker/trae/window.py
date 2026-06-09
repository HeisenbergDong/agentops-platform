import subprocess
import time
from pathlib import Path
from typing import Any


class TraeAutomationError(RuntimeError):
    pass


def open_trae(trae_exe_path: Path, workspace_path: Path | None = None) -> dict:
    exe_path = trae_exe_path.expanduser()
    if not exe_path.exists():
        raise TraeAutomationError(f"Trae executable not found: {exe_path}")

    args = [str(exe_path)]
    if workspace_path:
        args.append(str(workspace_path))
    subprocess.Popen(args)
    return {
        "status": "launched",
        "trae_exe_path": str(exe_path),
        "workspace_path": str(workspace_path) if workspace_path else "",
    }


def find_trae_window(timeout_seconds: float = 10.0) -> Any:
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise TraeAutomationError("pywinauto is required to control Trae on Windows") from exc

    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            for window in Desktop(backend="uia").windows():
                title = window.window_text()
                if "Trae" in title:
                    return window
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)

    message = "Trae window was not found"
    if last_error:
        message = f"{message}: {last_error}"
    raise TraeAutomationError(message)


def focus_trae(timeout_seconds: float = 10.0) -> dict:
    window = find_trae_window(timeout_seconds)
    title = window.window_text()
    try:
        window.restore()
    except Exception:
        pass
    window.set_focus()
    return {"status": "focused", "window_title": title}


def window_text_snapshot(window: Any, limit: int = 300) -> str:
    texts: list[str] = []
    for control_type in ("Text", "Button", "Edit"):
        try:
            controls = window.descendants(control_type=control_type)
        except Exception:
            continue
        for control in controls:
            if len(texts) >= limit:
                break
            try:
                text = control.window_text().strip()
            except Exception:
                continue
            if text:
                texts.append(text)
    return "\n".join(texts)
