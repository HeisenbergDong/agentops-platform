from app.worker_gateway.contracts import WorkerHeartbeat


class InMemoryWorkerRegistry:
    def __init__(self) -> None:
        self._heartbeats: dict[str, WorkerHeartbeat] = {}

    def update(self, heartbeat: WorkerHeartbeat) -> WorkerHeartbeat:
        self._heartbeats[heartbeat.worker_id] = heartbeat
        return heartbeat

    def list_workers(self) -> list[WorkerHeartbeat]:
        return list(self._heartbeats.values())


worker_registry = InMemoryWorkerRegistry()
