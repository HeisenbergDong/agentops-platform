from __future__ import annotations

import time
from typing import Callable

from worker.runtime.state import WorkerRuntimeState


class CommandCancelled(Exception):
    """Raised when the server or local runtime asks the current command to stop."""


class CancellationToken:
    def __init__(
        self,
        state: WorkerRuntimeState,
        command_id: str,
        checker: Callable[[str], bool] | None = None,
        check_interval_seconds: float = 2.0,
    ) -> None:
        self.state = state
        self.command_id = command_id
        self.checker = checker
        self.check_interval_seconds = max(0.5, check_interval_seconds)
        self._next_check_at = 0.0

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise CommandCancelled("Command cancelled by server stop request.")

    def cancelled(self) -> bool:
        if self.state.stop_requested:
            return True
        if not self.checker or not self.command_id:
            return False
        now = time.monotonic()
        if now < self._next_check_at:
            return False
        self._next_check_at = now + self.check_interval_seconds
        if self.checker(self.command_id):
            self.state.stop_requested = True
            return True
        return False
