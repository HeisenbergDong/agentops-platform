import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Header, HTTPException

from app.core.config import settings

PASSWORD_ITERATIONS = 210_000


def require_worker_token(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {settings.worker_token_dev}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid worker token")


def generate_worker_registration_code() -> str:
    return f"wrc_{secrets.token_urlsafe(24)}"


def generate_worker_token() -> str:
    return f"wkt_{secrets.token_urlsafe(40)}"


def hash_worker_secret(value: str) -> str:
    return hmac.new(settings.app_secret_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    )
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${encoded}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        algorithm, iterations_text, salt, expected = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations_text),
        )
        actual = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def create_access_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": int(time.time()) + settings.access_token_ttl_seconds,
    }
    body = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign(body)
    return f"v1.{body}.{signature}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        version, body, signature = token.split(".", 2)
        if version != "v1" or not hmac.compare_digest(signature, _sign(body)):
            raise ValueError("Invalid token signature")
        payload = json.loads(_urlsafe_b64decode(body).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("Token expired")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc


def _sign(body: str) -> str:
    digest = hmac.new(settings.app_secret_key.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return _urlsafe_b64encode(digest)


def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _urlsafe_b64decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(f"{encoded}{padding}")
