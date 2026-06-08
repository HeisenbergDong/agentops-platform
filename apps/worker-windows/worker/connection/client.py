import httpx


class WorkerClient:
    def __init__(self, server_url: str, token: str) -> None:
        self.server_url = server_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def heartbeat(self, payload: dict) -> dict:
        response = httpx.post(
            f"{self.server_url}/api/workers/heartbeat",
            headers=self.headers,
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def poll_commands(self, worker_id: str) -> list[dict]:
        response = httpx.get(
            f"{self.server_url}/api/workers/{worker_id}/commands",
            headers=self.headers,
            timeout=20,
        )
        response.raise_for_status()
        return response.json().get("commands", [])

    def post_result(self, worker_id: str, payload: dict) -> dict:
        response = httpx.post(
            f"{self.server_url}/api/workers/{worker_id}/results",
            headers=self.headers,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
