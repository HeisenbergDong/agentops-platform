from pathlib import Path

from worker.connection.client import WorkerClient


class AttachmentUploadError(RuntimeError):
    pass


class AttachmentUploader:
    def __init__(self, client: WorkerClient, worker_id: str) -> None:
        self.client = client
        self.worker_id = worker_id

    def upload(
        self,
        path: Path,
        kind: str,
        *,
        job_id: str | None = None,
        round_id: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> dict:
        if not path.exists() or not path.is_file():
            raise AttachmentUploadError(f"Attachment file does not exist: {path}")
        response = self.client.upload_attachment(
            self.worker_id,
            path,
            kind=kind,
            job_id=job_id,
            round_id=round_id,
            content_type=content_type,
        )
        attachment = response.get("attachment") if isinstance(response, dict) else None
        if not isinstance(attachment, dict) or not attachment.get("id"):
            raise AttachmentUploadError("Server attachment upload response did not include an attachment id.")
        return attachment
