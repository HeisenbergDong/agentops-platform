from worker.runtime.state import WorkerRuntimeState
from worker.trae.screenshot import capture_screenshot


class CommandRunner:
    def __init__(self) -> None:
        self.state = WorkerRuntimeState()

    def run(self, command: dict) -> dict:
        command_id = command.get("command_id", "")
        command_type = command.get("type", "")
        try:
            if command_type == "capture_screenshot":
                data = capture_screenshot()
            elif command_type == "stop_current_task":
                self.state.stop_requested = True
                data = {"stopped": True}
            else:
                data = {"message": f"Command {command_type} is scaffolded but not implemented yet."}
            return {
                "command_id": command_id,
                "worker_id": "local-windows-worker",
                "status": "success",
                "message": "Command processed",
                "data": data,
            }
        except Exception as exc:
            return {
                "command_id": command_id,
                "worker_id": "local-windows-worker",
                "status": "failed",
                "message": "Command failed",
                "data": {},
                "error": str(exc),
            }
