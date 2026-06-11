from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TraeAutomationError(RuntimeError):
    pass


SW_RESTORE = 9
TREE_SCOPE_DESCENDANTS = 4
UIA_CONTROL_TYPE_PROPERTY_ID = 30003
UIA_INVOKE_PATTERN_ID = 10000
CUIAUTOMATION_CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
CONTROL_TYPES = {
    "Button": 50000,
    "Edit": 50004,
    "Text": 50020,
}
TRAE_EXECUTABLE_NAMES = ("Trae CN.exe", "Trae.exe", "trae.exe")


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int


class TraeWindow:
    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self._element: Any | None = None

    def window_text(self) -> str:
        return _window_title(self.hwnd)

    def restore(self) -> None:
        ctypes.windll.user32.ShowWindow(self.hwnd, SW_RESTORE)

    def set_focus(self) -> None:
        _set_foreground_window(self.hwnd)

    def descendants(self, control_type: str) -> list["AutomationElement"]:
        element = self._uia_element()
        control_type_id = CONTROL_TYPES.get(control_type)
        if not control_type_id:
            return []
        try:
            uia = _uia_client()
            condition = uia.CreatePropertyCondition(UIA_CONTROL_TYPE_PROPERTY_ID, control_type_id)
            collection = element.FindAll(TREE_SCOPE_DESCENDANTS, condition)
        except Exception as exc:
            raise TraeAutomationError(f"Could not inspect Trae UI Automation tree: {exc}") from exc
        return [AutomationElement(collection.GetElement(index)) for index in range(collection.Length)]

    def _uia_element(self) -> Any:
        if self._element is None:
            try:
                self._element = _uia_client().ElementFromHandle(self.hwnd)
            except Exception as exc:
                raise TraeAutomationError(f"Could not attach to Trae UI Automation element: {exc}") from exc
        return self._element


class AutomationElement:
    def __init__(self, element: Any) -> None:
        self.element = element

    def window_text(self) -> str:
        try:
            return str(self.element.CurrentName or "")
        except Exception:
            return ""

    def rectangle(self) -> Rect:
        try:
            raw = self.element.CurrentBoundingRectangle
            return Rect(
                left=int(raw.left),
                top=int(raw.top),
                right=int(raw.right),
                bottom=int(raw.bottom),
            )
        except Exception:
            return Rect(0, 0, 0, 0)

    def click_input(self) -> None:
        try:
            pattern = self.element.GetCurrentPattern(UIA_INVOKE_PATTERN_ID)
            pattern.Invoke()
            return
        except Exception:
            pass
        rect = self.rectangle()
        x = (rect.left + rect.right) // 2
        y = (rect.top + rect.bottom) // 2
        if x <= 0 and y <= 0:
            raise TraeAutomationError(f"Could not click UI element: {self.window_text() or '<unnamed>'}")
        _mouse_click(x, y)


def resolve_trae_executable(trae_exe_path: Path) -> Path:
    candidates = _candidate_trae_paths(trae_exe_path)
    for candidate in candidates:
        if candidate.exists():
            return candidate

    tried = ", ".join(str(candidate) for candidate in candidates[:8])
    if len(candidates) > 8:
        tried += ", ..."
    raise TraeAutomationError(
        "Trae executable not found. Configure --trae-exe-path or AGENTOPS_WORKER_TRAE_EXE_PATH. "
        f"Tried: {tried}"
    )


def open_trae(trae_exe_path: Path, workspace_path: Path | None = None) -> dict:
    exe_path = resolve_trae_executable(trae_exe_path)

    args = [str(exe_path)]
    if workspace_path:
        args.append(str(workspace_path))
    subprocess.Popen(args)
    return {
        "status": "launched",
        "trae_exe_path": str(exe_path),
        "workspace_path": str(workspace_path) if workspace_path else "",
    }


def ensure_trae_running(
    trae_exe_path: Path,
    workspace_path: Path | None = None,
    launch_timeout_seconds: float = 30.0,
    force_open_workspace: bool = False,
) -> dict:
    existing = _try_find_trae_window()
    if existing and not (force_open_workspace and workspace_path):
        title = _focus_window(existing)
        return {
            "status": "already_running",
            "window_title": title,
            "workspace_path": str(workspace_path) if workspace_path else "",
        }

    launch_result = open_trae(trae_exe_path, workspace_path)
    if existing and force_open_workspace:
        time.sleep(2.0)
    window = find_trae_window(timeout_seconds=launch_timeout_seconds)
    title = _focus_window(window)
    return {
        **launch_result,
        "window_title": title,
    }


def find_trae_window(timeout_seconds: float = 10.0) -> TraeWindow:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        hwnd = _find_top_level_window("Trae")
        if hwnd:
            return TraeWindow(hwnd)
        time.sleep(0.5)
    raise TraeAutomationError("Trae window was not found")


def focus_trae(timeout_seconds: float = 10.0) -> dict:
    window = find_trae_window(timeout_seconds)
    title = _focus_window(window)
    return {"status": "focused", "window_title": title}


def window_text_snapshot(window: Any, limit: int = 300) -> str:
    texts: list[str] = []
    for control_type in ("Text", "Button", "Edit"):
        try:
            controls = window.descendants(control_type=control_type)
        except Exception:
            controls = []
        for control in controls:
            if len(texts) >= limit:
                break
            text = control.window_text().strip()
            if text:
                texts.append(text)
    if texts:
        return "\n".join(texts)
    if isinstance(window, TraeWindow):
        return _child_window_text_snapshot(window.hwnd, limit=limit)
    return ""


def _find_top_level_window(title_marker: str) -> int:
    user32 = ctypes.windll.user32
    found = ctypes.c_void_p(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if title_marker.lower() in title.lower():
            found.value = int(hwnd)
            return False
        return True

    user32.EnumWindows(enum_proc, 0)
    return int(found.value or 0)


def _try_find_trae_window() -> TraeWindow | None:
    hwnd = _find_top_level_window("Trae")
    if not hwnd:
        return None
    return TraeWindow(hwnd)


def _focus_window(window: TraeWindow) -> str:
    title = window.window_text()
    window.restore()
    window.set_focus()
    return title


def _window_title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _child_window_text_snapshot(hwnd: int, limit: int = 300) -> str:
    user32 = ctypes.windll.user32
    texts: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(child_hwnd: int, _lparam: int) -> bool:
        if len(texts) >= limit:
            return False
        title = _window_title(child_hwnd).strip()
        if title:
            texts.append(title)
        return True

    user32.EnumChildWindows(hwnd, enum_proc, 0)
    return "\n".join(texts)


def _set_foreground_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    foreground = user32.GetForegroundWindow()
    current_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    if foreground_thread:
        user32.AttachThreadInput(current_thread, foreground_thread, True)
    if target_thread:
        user32.AttachThreadInput(current_thread, target_thread, True)
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if target_thread:
            user32.AttachThreadInput(current_thread, target_thread, False)
        if foreground_thread:
            user32.AttachThreadInput(current_thread, foreground_thread, False)


def _mouse_click(x: int, y: int) -> None:
    user32 = ctypes.windll.user32
    user32.SetCursorPos(x, y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def _uia_client() -> Any:
    try:
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient
    except ImportError as exc:
        raise TraeAutomationError("comtypes is required for Windows UI Automation") from exc
    except Exception as exc:
        raise TraeAutomationError(f"Could not load Windows UI Automation interfaces: {exc}") from exc
    return comtypes.client.CreateObject(CUIAUTOMATION_CLSID, interface=UIAutomationClient.IUIAutomation)


def _candidate_trae_paths(configured_path: Path) -> list[Path]:
    candidates: list[Path] = []

    def add(path: str | Path | None) -> None:
        if not path:
            return
        candidate = Path(path).expanduser()
        if candidate not in candidates:
            candidates.append(candidate)

    add(os.environ.get("AGENTOPS_WORKER_TRAE_EXE_PATH"))
    add(os.environ.get("TRAE_EXE_PATH"))
    add(configured_path)

    for executable_name in TRAE_EXECUTABLE_NAMES:
        found = shutil.which(executable_name)
        add(found)

    local_appdata = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("PROGRAMFILES")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    userprofile = os.environ.get("USERPROFILE")
    roots = [
        local_appdata,
        Path(local_appdata) / "Programs" if local_appdata else None,
        program_files,
        program_files_x86,
        Path(userprofile) / "AppData" / "Local" / "Programs" if userprofile else None,
        Path("D:/app"),
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
    ]
    app_dirs = ("Trae CN", "Trae")
    for root in roots:
        if not root:
            continue
        for app_dir in app_dirs:
            for executable_name in TRAE_EXECUTABLE_NAMES:
                add(Path(root) / app_dir / executable_name)
    return candidates
