from __future__ import annotations

import ctypes


STD_INPUT_HANDLE = -10
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080


def disable_quick_edit_mode() -> None:
    try:
        kernel32 = ctypes.windll.kernel32
    except AttributeError:
        return

    handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    if handle in (0, -1):
        return

    mode = ctypes.c_uint()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return

    new_mode = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE
    kernel32.SetConsoleMode(handle, new_mode)
