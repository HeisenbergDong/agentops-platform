import base64
import hashlib
import hmac

from app.core.config import settings


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


def seal_secret(value: str | None) -> str:
    if not value:
        return ""
    raw = value.encode("utf-8")
    key_stream = _keystream(len(raw))
    cipher = bytes(item ^ key_stream[index] for index, item in enumerate(raw))
    tag = hmac.new(_secret_key(), raw, hashlib.sha256).digest()[:12]
    return "enc:v1:" + base64.urlsafe_b64encode(tag + cipher).decode("ascii").rstrip("=")


def open_secret(value: str | None) -> str:
    if not value:
        return ""
    if not value.startswith("enc:v1:"):
        return value
    encoded = value.removeprefix("enc:v1:")
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    tag, cipher = raw[:12], raw[12:]
    key_stream = _keystream(len(cipher))
    plain = bytes(item ^ key_stream[index] for index, item in enumerate(cipher))
    expected = hmac.new(_secret_key(), plain, hashlib.sha256).digest()[:12]
    if not hmac.compare_digest(tag, expected):
        raise ValueError("Secret payload failed integrity check")
    return plain.decode("utf-8")


def _secret_key() -> bytes:
    return hashlib.sha256(settings.app_secret_key.encode("utf-8")).digest()


def _keystream(length: int) -> bytes:
    key = _secret_key()
    chunks: list[bytes] = []
    counter = 0
    while sum(len(item) for item in chunks) < length:
        chunks.append(hashlib.sha256(key + counter.to_bytes(4, "big")).digest())
        counter += 1
    return b"".join(chunks)[:length]
