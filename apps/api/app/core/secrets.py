def mask_secret(value: str | None, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return f"{value[:visible]}{'*' * 8}"


def assert_no_secret_text(text: str) -> None:
    suspicious = ["sk-", "FEISHU_APP_SECRET", "token=", "password="]
    lowered = text.lower()
    if any(item.lower() in lowered for item in suspicious):
        raise ValueError("Text appears to contain a secret")
