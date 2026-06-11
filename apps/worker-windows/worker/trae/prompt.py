import ctypes
import time

from worker.system.clipboard import ClipboardError, set_clipboard_text
from worker.trae.window import focus_trae


class PromptSendError(RuntimeError):
    pass


def send_prompt(prompt: str, submit: bool = True, submit_hotkey: str = "{ENTER}") -> dict:
    prompt = prompt.strip()
    if not prompt:
        raise PromptSendError("Prompt is empty")

    focus_result = focus_trae()
    try:
        set_clipboard_text(prompt)
    except ClipboardError as exc:
        raise PromptSendError(str(exc)) from exc
    _send_keys("^v")
    if submit:
        _send_keys(submit_hotkey)
    return {
        "status": "sent",
        "chars": len(prompt),
        "submitted": submit,
        "submit_hotkey": submit_hotkey if submit else "",
        "window_title": focus_result.get("window_title", ""),
    }

def _send_keys(keys: str) -> None:
    normalized = keys.strip()
    try:
        if normalized.lower() == "^v":
            _hotkey(0x11, 0x56)
        elif normalized in {"{ENTER}", "ENTER", "Enter"}:
            _press_key(0x0D)
        elif normalized in {"^{ENTER}", "^ENTER", "^Enter", "CTRL+ENTER", "Ctrl+Enter"}:
            _hotkey(0x11, 0x0D)
        else:
            raise PromptSendError(f"Unsupported key sequence: {keys}")
    except PromptSendError:
        raise
    except Exception as exc:
        raise PromptSendError(f"Could not send keys to Trae: {exc}") from exc
    time.sleep(0.05)


def _hotkey(modifier_vk: int, key_vk: int) -> None:
    _key_down(modifier_vk)
    try:
        _press_key(key_vk)
    finally:
        _key_up(modifier_vk)


def _press_key(vk: int) -> None:
    _key_down(vk)
    _key_up(vk)


def _key_down(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)


def _key_up(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)
