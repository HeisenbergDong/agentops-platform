from dataclasses import dataclass


@dataclass
class WorkerRuntimeState:
    stage: str = "idle"
    current_window_title: str = ""
    busy: bool = False
    stop_requested: bool = False
    current_command_id: str = ""
    current_lease_id: str = ""
