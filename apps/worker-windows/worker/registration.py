import getpass
import hashlib
import platform
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path

from worker.capabilities import CAPABILITIES, SUPPORTED_APPS
from worker.config import WorkerSettings, load_worker_settings, save_worker_settings
from worker.connection.client import WorkerClient


@dataclass(frozen=True)
class RegistrationOptions:
    server_url: str
    registration_code: str
    worker_id: str = ""
    display_name: str = ""
    config_path: Path | None = None
    trae_exe_path: Path | None = None
    workspace_root: Path | None = None
    poll_interval_seconds: float | None = None


def register_worker(options: RegistrationOptions) -> tuple[WorkerSettings, Path, dict]:
    server_url = normalize_server_url(options.server_url)
    registration_code = options.registration_code.strip()
    if not server_url:
        raise ValueError("Server URL is required")
    if not registration_code:
        raise ValueError("Registration code is required")

    current = load_worker_settings(options.config_path)
    machine_name = socket.gethostname()
    display_name = options.display_name.strip() or current.display_name or machine_name
    payload = {
        "registration_code": registration_code,
        "worker_id": options.worker_id.strip() or current.worker_id or "",
        "display_name": display_name,
        "worker_type": current.worker_type,
        "machine_name": machine_name,
        "machine_fingerprint": machine_fingerprint(),
        "version": current.version,
        "supported_apps": SUPPORTED_APPS,
        "capabilities": CAPABILITIES,
    }

    response = WorkerClient(server_url, token="").register_worker(payload)
    worker_id = str(response.get("worker_id") or response.get("worker", {}).get("worker_id") or "").strip()
    token = str(response.get("worker_token") or "").strip()
    if not worker_id or not token:
        raise ValueError("Server registration response did not include worker_id and worker_token")

    updated = WorkerSettings(
        server_url=server_url,
        token=token,
        worker_id=worker_id,
        display_name=display_name,
        worker_type=current.worker_type,
        version=current.version,
        trae_exe_path=options.trae_exe_path or current.trae_exe_path,
        workspace_root=options.workspace_root or current.workspace_root,
        browser_url=current.browser_url,
        poll_interval_seconds=options.poll_interval_seconds or current.poll_interval_seconds,
        auto_launch_trae_on_startup=current.auto_launch_trae_on_startup,
    )
    saved_path = save_worker_settings(updated, options.config_path)
    return updated, saved_path, response


def normalize_server_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = f"http://{url}"
    return url.rstrip("/")


def machine_fingerprint() -> str:
    raw = "|".join(
        [
            platform.system(),
            platform.release(),
            socket.gethostname(),
            getpass.getuser(),
            str(uuid.getnode()),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:32]


def is_registered(worker_settings: WorkerSettings) -> bool:
    return bool(
        worker_settings.server_url.strip()
        and worker_settings.worker_id.strip()
        and worker_settings.token.strip()
        and worker_settings.token != "change-me-worker-token"
    )
