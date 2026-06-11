import ctypes
import time


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
OPEN_CLIPBOARD_RETRIES = 10
OPEN_CLIPBOARD_RETRY_SECONDS = 0.05


class ClipboardError(RuntimeError):
    pass


def set_clipboard_text(text: str) -> None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    _configure_kernel32(kernel32)
    _configure_user32(user32)
    data = str(text) + "\0"
    raw = data.encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
    if not handle:
        raise ClipboardError("Could not allocate clipboard memory")

    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        raise ClipboardError("Could not lock clipboard memory")
    try:
        ctypes.memmove(locked, raw, len(raw))
    finally:
        kernel32.GlobalUnlock(handle)

    if not _open_clipboard(user32):
        kernel32.GlobalFree(handle)
        raise ClipboardError("Could not open clipboard")
    try:
        if not user32.EmptyClipboard():
            raise ClipboardError("Could not clear clipboard")
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise ClipboardError("Could not set clipboard text")
        handle = None
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


def get_clipboard_text() -> str:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    _configure_kernel32(kernel32)
    _configure_user32(user32)
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""
    if not _open_clipboard(user32):
        raise ClipboardError("Could not open clipboard")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return ""
        try:
            return ctypes.wstring_at(locked)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _configure_kernel32(kernel32) -> None:
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p


def _configure_user32(user32) -> None:
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_int
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_int


def _open_clipboard(user32) -> bool:
    for _attempt in range(OPEN_CLIPBOARD_RETRIES):
        if user32.OpenClipboard(None):
            return True
        time.sleep(OPEN_CLIPBOARD_RETRY_SECONDS)
    return False
