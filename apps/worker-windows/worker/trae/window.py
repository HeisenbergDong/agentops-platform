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


SW_MAXIMIZE = 3
SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
TREE_SCOPE_DESCENDANTS = 4
UIA_CONTROL_TYPE_PROPERTY_ID = 30003
UIA_INVOKE_PATTERN_ID = 10000
CUIAUTOMATION_CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
CONTROL_TYPES = {
    "Button": 50000,
    "Edit": 50004,
    "List": 50008,
    "Group": 50026,
    "Text": 50020,
    "Document": 50030,
    "Pane": 50033,
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

    def maximize(self) -> None:
        _show_window(self.hwnd, SW_MAXIMIZE)

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


def open_trae(trae_exe_path: Path, workspace_path: Path | None = None, reuse_window: bool = False) -> dict:
    exe_path = resolve_trae_executable(trae_exe_path)

    args = [str(exe_path)]
    if reuse_window and workspace_path:
        args.append("--reuse-window")
    if workspace_path:
        args.append(str(workspace_path))
    subprocess.Popen(args)
    return {
        "status": "launched",
        "trae_exe_path": str(exe_path),
        "workspace_path": str(workspace_path) if workspace_path else "",
        "reuse_window": bool(reuse_window and workspace_path),
    }


def ensure_trae_running(
    trae_exe_path: Path,
    workspace_path: Path | None = None,
    launch_timeout_seconds: float = 30.0,
    force_open_workspace: bool = False,
) -> dict:
    existing = _try_find_trae_window(
        workspace_path=workspace_path,
        require_workspace_match=bool(workspace_path),
    )
    if existing and not force_open_workspace:
        window = wait_for_stable_trae_window(
            workspace_path=workspace_path,
            timeout_seconds=min(launch_timeout_seconds, 6.0),
            require_workspace_match=bool(workspace_path),
        )
        title = _focus_window(window)
        return {
            "status": "already_running",
            "window_title": title,
            "workspace_path": str(workspace_path) if workspace_path else "",
            "workspace_match": _title_matches_workspace(title, workspace_path),
            "window_diagnostics": trae_window_diagnostics(selected_hwnd=window.hwnd, workspace_path=workspace_path),
        }

    existing_any = _try_find_trae_window()
    reuse_window = bool(existing_any and workspace_path and not force_open_workspace)
    launch_result = open_trae(trae_exe_path, workspace_path, reuse_window=reuse_window)
    if existing_any:
        time.sleep(2.0)
    window = wait_for_stable_trae_window(
        timeout_seconds=launch_timeout_seconds,
        workspace_path=workspace_path,
        require_workspace_match=bool(workspace_path),
    )
    title = _focus_window(window)
    return {
        **launch_result,
        "window_title": title,
        "workspace_match": _title_matches_workspace(title, workspace_path),
        "window_diagnostics": trae_window_diagnostics(selected_hwnd=window.hwnd, workspace_path=workspace_path),
    }


def find_trae_window(
    timeout_seconds: float = 10.0,
    workspace_path: Path | str | None = None,
    require_workspace_match: bool = False,
) -> TraeWindow:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        hwnd = _find_top_level_window(
            "Trae",
            workspace_path=workspace_path,
            require_workspace_match=require_workspace_match,
        )
        if hwnd:
            return TraeWindow(hwnd)
        time.sleep(0.5)
    marker = _workspace_title_marker(workspace_path)
    if require_workspace_match and marker:
        diagnostics = trae_window_diagnostics(workspace_path=workspace_path)
        raise TraeAutomationError(f"Trae window for workspace '{marker}' was not found. diagnostics={diagnostics}")
    raise TraeAutomationError("Trae window was not found")


def wait_for_stable_trae_window(
    timeout_seconds: float = 10.0,
    workspace_path: Path | str | None = None,
    require_workspace_match: bool = False,
    stable_checks: int = 3,
    poll_interval_seconds: float = 0.5,
) -> TraeWindow:
    """Wait until Trae stops swapping startup/workspace windows."""
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    last_signature: tuple[int, str, tuple[int, int, int, int] | None] | None = None
    stable_count = 0
    last_window: TraeWindow | None = None
    while time.monotonic() < deadline:
        try:
            window = find_trae_window(
                timeout_seconds=min(1.0, max(0.1, deadline - time.monotonic())),
                workspace_path=workspace_path,
                require_workspace_match=require_workspace_match,
            )
        except TraeAutomationError:
            time.sleep(poll_interval_seconds)
            continue
        signature = (int(window.hwnd), window.window_text(), _window_rect(window.hwnd))
        if signature == last_signature:
            stable_count += 1
        else:
            stable_count = 1
            last_signature = signature
        last_window = window
        if stable_count >= max(1, stable_checks):
            return window
        time.sleep(poll_interval_seconds)
    if last_window:
        return last_window
    return find_trae_window(
        timeout_seconds=0.5,
        workspace_path=workspace_path,
        require_workspace_match=require_workspace_match,
    )


def focus_trae(
    timeout_seconds: float = 10.0,
    workspace_path: Path | str | None = None,
    require_workspace_match: bool = False,
) -> dict:
    window = wait_for_stable_trae_window(
        timeout_seconds=timeout_seconds,
        workspace_path=workspace_path,
        require_workspace_match=require_workspace_match,
    )
    title = _focus_window(window)
    return {
        "status": "focused",
        "window_title": title,
        "workspace_match": _title_matches_workspace(title, workspace_path),
        "window_diagnostics": trae_window_diagnostics(selected_hwnd=window.hwnd, workspace_path=workspace_path),
    }


def trae_window_diagnostics(selected_hwnd: int | None = None, workspace_path: Path | str | None = None) -> dict:
    windows = _find_top_level_windows("Trae")
    marker = _workspace_title_marker(workspace_path)
    foreground_hwnd = _foreground_window()
    result = {
        "count": len(windows),
        "selected_hwnd": int(selected_hwnd or 0),
        "foreground_hwnd": foreground_hwnd,
        "foreground_pid": _window_process_id(foreground_hwnd) if foreground_hwnd else 0,
        "windows": [
            {
                "hwnd": hwnd,
                "title": title,
                "selected": bool(selected_hwnd and hwnd == selected_hwnd),
                "pid": _window_process_id(hwnd),
                "foreground": bool(foreground_hwnd and hwnd == foreground_hwnd),
                "rect": _rect_dict(_window_rect(hwnd)),
            }
            for hwnd, title in windows
        ],
    }
    if marker:
        result["workspace_marker"] = marker
        result["matching_count"] = len([title for _hwnd, title in windows if _title_matches_workspace(title, workspace_path)])
        for item in result["windows"]:
            item["workspace_match"] = _title_matches_workspace(str(item["title"]), workspace_path)
    return result


def _workspace_title_marker(workspace_path: Path | str | None) -> str:
    if not workspace_path:
        return ""
    text = str(workspace_path).strip().rstrip("\\/")
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    return normalized.rsplit("/", 1)[-1].strip()


def _title_matches_workspace(title: str, workspace_path: Path | str | None) -> bool:
    marker = _workspace_title_marker(workspace_path)
    if not marker:
        return False
    return marker.lower() in str(title or "").lower()


def _select_top_level_window(
    windows: list[tuple[int, str]],
    workspace_path: Path | str | None = None,
    require_workspace_match: bool = False,
) -> int:
    marker = _workspace_title_marker(workspace_path)
    if marker:
        for hwnd, title in windows:
            if marker.lower() in title.lower():
                return hwnd
        if require_workspace_match:
            return 0
    return windows[0][0] if windows else 0


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


def _find_top_level_window(
    title_marker: str,
    workspace_path: Path | str | None = None,
    require_workspace_match: bool = False,
) -> int:
    windows = _find_top_level_windows(title_marker)
    return _select_top_level_window(
        windows,
        workspace_path=workspace_path,
        require_workspace_match=require_workspace_match,
    )


def _find_top_level_windows(title_marker: str) -> list[tuple[int, str]]:
    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if title_marker.lower() in title.lower():
            found.append((int(hwnd), title))
        return True

    user32.EnumWindows(enum_proc, 0)
    return found


def _try_find_trae_window(
    workspace_path: Path | str | None = None,
    require_workspace_match: bool = False,
) -> TraeWindow | None:
    hwnd = _find_top_level_window(
        "Trae",
        workspace_path=workspace_path,
        require_workspace_match=require_workspace_match,
    )
    if not hwnd:
        return None
    return TraeWindow(hwnd)


def _focus_window(window: TraeWindow) -> str:
    title = window.window_text()
    _maximize_and_focus_window(window.hwnd)
    return title


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if not hwnd:
        return None

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = RECT()
    try:
        _set_process_dpi_aware()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
    except Exception:
        return None
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _rect_dict(rect: tuple[int, int, int, int] | None) -> dict[str, int]:
    if not rect:
        return {}
    left, top, right, bottom = rect
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": max(0, right - left),
        "height": max(0, bottom - top),
    }


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


def _maximize_and_focus_window(hwnd: int, attempts: int = 5) -> dict:
    _set_process_dpi_aware()
    window_pid = _window_process_id(hwnd)
    last_foreground_pid = 0
    last_foreground_hwnd = 0
    for _attempt in range(max(1, attempts)):
        _show_window(hwnd, SW_MAXIMIZE)
        time.sleep(0.25)
        _tap_alt_for_foreground_unlock()
        _set_foreground_window(hwnd, show_window=None)
        time.sleep(0.35)
        last_foreground_hwnd = _foreground_window()
        last_foreground_pid = _foreground_process_id()
        if last_foreground_hwnd == hwnd or (window_pid and last_foreground_pid == window_pid):
            return {
                "status": "focused",
                "hwnd": hwnd,
                "window_pid": window_pid,
                "foreground_hwnd": last_foreground_hwnd,
                "foreground_pid": last_foreground_pid,
                "maximized": True,
            }
    raise TraeAutomationError(
        "Could not bring Trae window to foreground. "
        f"hwnd={hwnd} window_pid={window_pid} "
        f"foreground_hwnd={last_foreground_hwnd} foreground_pid={last_foreground_pid}"
    )


def _set_process_dpi_aware() -> None:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _show_window(hwnd: int, command: int) -> None:
    user32 = ctypes.windll.user32
    try:
        user32.ShowWindowAsync(hwnd, command)
    except Exception:
        user32.ShowWindow(hwnd, command)


def _tap_alt_for_foreground_unlock() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)


def _window_process_id(hwnd: int) -> int:
    if not hwnd:
        return 0
    user32 = ctypes.windll.user32
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _foreground_window() -> int:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _foreground_process_id() -> int:
    foreground = _foreground_window()
    if not foreground:
        return 0
    return _window_process_id(int(foreground))


def _set_foreground_window(hwnd: int, show_window: int | None = SW_RESTORE) -> None:
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
        if show_window is not None:
            _show_window(hwnd, show_window)
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
