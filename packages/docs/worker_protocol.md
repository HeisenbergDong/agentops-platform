# Worker Protocol

Worker communication currently uses polling:

1. Worker posts heartbeat to `/api/workers/heartbeat`.
2. Worker polls `/api/workers/{worker_id}/commands`.
3. Worker posts results to `/api/workers/{worker_id}/results`.

The protocol can later move to WebSocket while preserving the command/result schemas.
