from worker.trae.window import focus_trae


class PromptSendError(RuntimeError):
    pass


def send_prompt(prompt: str, submit: bool = True, submit_hotkey: str = "{ENTER}") -> dict:
    prompt = prompt.strip()
    if not prompt:
        raise PromptSendError("Prompt is empty")

    focus_result = focus_trae()
    _set_clipboard_text(prompt)
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


def _set_clipboard_text(text: str) -> None:
    root = None
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
    except Exception as exc:
        raise PromptSendError(f"Could not write prompt to clipboard: {exc}") from exc
    finally:
        if root:
            root.destroy()


def _send_keys(keys: str) -> None:
    try:
        from pywinauto.keyboard import send_keys as pywinauto_send_keys
    except ImportError as exc:
        raise PromptSendError("pywinauto is required to send keys to Trae") from exc
    try:
        pywinauto_send_keys(keys, pause=0.05)
    except Exception as exc:
        raise PromptSendError(f"Could not send keys to Trae: {exc}") from exc
