from pathlib import Path

from fastapi import UploadFile
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import workers
from app.db.models import Attachment, User, Worker
from app.db.session import Base


def test_worker_upload_attachment_creates_server_file(monkeypatch, tmp_path: Path):
    db = _test_session()
    worker = Worker(
        worker_id="worker1",
        user_id="user1",
        machine_name="host1",
        token_hash="hash",
    )
    db.add(worker)
    db.commit()
    source = tmp_path / "screen.png"
    source.write_bytes(b"png")
    monkeypatch.setattr(workers.settings, "attachment_root", tmp_path / "storage")

    with source.open("rb") as file_obj:
        response = workers.upload_worker_attachment(
            worker_id="worker1",
            kind="screenshot",
            job_id="job1",
            round_id="round1",
            file=UploadFile(file=file_obj, filename="screen.png"),
            worker=worker,
            db=db,
        )

    attachment = db.scalar(select(Attachment))
    assert response["status"] == "uploaded"
    assert attachment is not None
    assert attachment.user_id == "user1"
    assert attachment.job_id == "job1"
    assert attachment.round_id == "round1"
    assert Path(attachment.path).read_bytes() == b"png"
    assert str(attachment.path).startswith(str(tmp_path / "storage"))


def test_download_worker_package_uses_configured_zip(monkeypatch, tmp_path: Path):
    package = tmp_path / "agentops-worker-windows.zip"
    package.write_bytes(b"zip")
    monkeypatch.setattr(workers.settings, "worker_package_path", package)

    response = workers.download_worker_package(user=User(id="user1", email="u@example.com", display_name="User"))

    assert Path(response.path) == package.resolve()
    assert response.filename == "agentops-worker-windows.zip"


def test_download_worker_package_reports_missing(monkeypatch):
    monkeypatch.setattr(workers, "_worker_package_path", lambda: None)

    try:
        workers.download_worker_package(user=User(id="user1", email="u@example.com", display_name="User"))
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("Expected missing worker package to return 404")


def _test_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()
