from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WorkerCommandType(StrEnum):
    OPEN_TRAE = "open_trae"
    OPEN_WORKSPACE = "open_workspace"
    FOCUS_TRAE = "focus_trae"
    SEND_PROMPT = "send_prompt"
    WAIT_COMPLETION = "wait_completion"
    DIAGNOSE_UI = "diagnose_ui"
    CLICK_CONTINUE = "click_continue"
    CLICK_CONFIRM = "click_confirm"
    COPY_LATEST_REPLY = "copy_latest_reply"
    CAPTURE_SCREENSHOT = "capture_screenshot"
    SCAN_PROJECT = "scan_project"
    RUN_COMMAND = "run_command"
    BROWSER_ACCEPTANCE = "browser_acceptance"
    GIT_SUBMIT = "git_submit"
    STOP_CURRENT_TASK = "stop_current_task"


class WorkerCommand(BaseModel):
    command_id: str
    job_id: str | None = None
    round_id: str | None = None
    type: WorkerCommandType
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkerResult(BaseModel):
    command_id: str
    worker_id: str
    status: str
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class WorkerHeartbeat(BaseModel):
    worker_id: str
    machine_name: str
    display_name: str = ""
    worker_type: str = "windows_trae"
    machine_fingerprint: str = ""
    version: str = ""
    supported_apps: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    current_stage: str = "idle"
    current_window_title: str = ""
    busy: bool = False


class WorkerRegisterRequest(BaseModel):
    registration_code: str
    worker_id: str = ""
    display_name: str = ""
    worker_type: str = "windows_trae"
    machine_name: str
    machine_fingerprint: str = ""
    version: str = ""
    supported_apps: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


class CreateWorkerCommandRequest(BaseModel):
    type: WorkerCommandType = WorkerCommandType.DIAGNOSE_UI
    payload: dict[str, Any] = Field(default_factory=dict)
    job_id: str | None = None
    round_id: str | None = None


class WorkerLogEntry(BaseModel):
    command_id: str | None = None
    job_id: str | None = None
    round_id: str | None = None
    level: str = "info"
    stage: str = "worker"
    message: str
    extra: dict[str, Any] = Field(default_factory=dict)
