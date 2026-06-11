from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import workers
from app.db.models import Attachment, Worker
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


def _test_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()
