from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


MAX_RESULT_OUTBOX_ATTEMPTS = 5
RESULT_POST_RETRY_DELAYS_SECONDS = (1.0, 2.0, 5.0)


def default_result_outbox_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "AgentOps" / "Worker" / "result-outbox"
    return Path.home() / ".agentops" / "worker" / "result-outbox"


class ResultOutbox:
    def __init__(
        self,
        root: Path | str | None = None,
        retry_delays: tuple[float, ...] = RESULT_POST_RETRY_DELAYS_SECONDS,
    ) -> None:
        self.root = Path(root).expanduser() if root else default_result_outbox_dir()
        self.retry_delays = retry_delays

    def flush(self, client: Any, worker_id: str) -> None:
        for path in sorted(self.root.glob("*.json")):
            envelope = self._read(path)
            if not isinstance(envelope, dict):
                self._discard(path)
                continue
            payload = envelope.get("payload")
            if not isinstance(payload, dict):
                self._discard(path)
                continue
            attempts = int(envelope.get("attempts") or 0)
            try:
                client.post_result(worker_id, payload)
            except Exception:
                attempts += 1
                if attempts >= MAX_RESULT_OUTBOX_ATTEMPTS:
                    self._discard(path)
                else:
                    envelope["attempts"] = attempts
                    envelope["last_attempt_at"] = time.time()
                    self._write(path, envelope)
                continue
            self._discard(path)

    def post_or_save(self, client: Any, worker_id: str, payload: dict[str, Any]) -> dict:
        last_exc: Exception | None = None
        for delay in (0.0, *self.retry_delays):
            if delay:
                time.sleep(delay)
            try:
                result = client.post_result(worker_id, payload)
            except Exception as exc:
                last_exc = exc
                continue
            return result
        self.save(worker_id, payload)
        if last_exc:
            raise last_exc
        return {"status": "saved_to_outbox"}

    def save(self, worker_id: str, payload: dict[str, Any]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        command_id = _safe_name(str(payload.get("command_id") or "command"))
        created = int(time.time() * 1000)
        path = self.root / f"{created}-{_safe_name(worker_id)}-{command_id}.json"
        self._write(
            path,
            {
                "worker_id": worker_id,
                "command_id": str(payload.get("command_id") or ""),
                "created_at": time.time(),
                "attempts": 0,
                "payload": payload,
            },
        )
        return path

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _discard(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    return cleaned[:96] or "item"
