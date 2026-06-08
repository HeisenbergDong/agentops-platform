from pathlib import Path


class AttachmentUploader:
    def upload(self, path: Path, kind: str) -> dict:
        return {
            "status": "pending",
            "kind": kind,
            "path": str(path),
            "message": "Attachment upload will be implemented after server storage API.",
        }
