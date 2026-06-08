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
    RUN_COMMAND = "run_command"
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
    supported_apps: list[str] = Field(default_factory=list)
    current_stage: str = "idle"
    current_window_title: str = ""
    busy: bool = False
