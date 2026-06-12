from pathlib import Path

from fastapi import UploadFile
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import workers
from app.db.models import Attachment, User, UserConfig, Worker
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


def test_worker_trae_ui_analyze_uses_bound_user_model_settings(monkeypatch, tmp_path: Path):
    db = _test_session()
    worker = Worker(worker_id="worker1", user_id="user1", machine_name="host1", token_hash="hash")
    db.add(worker)
    db.add(UserConfig(user_id="user1", category="model", data={"api_key": "enc", "model_name": "vision"}))
    db.commit()
    source = tmp_path / "screen.png"
    source.write_bytes(b"png")
    captured = {}

    def fake_analyze(configs, image_bytes, mime_type, context):
        captured["configs"] = configs
        captured["image_bytes"] = image_bytes
        captured["mime_type"] = mime_type
        captured["context"] = context
        return {"status": "found", "targets": [{"action": "send_button"}]}

    monkeypatch.setattr(workers, "analyze_trae_ui", fake_analyze)

    with source.open("rb") as file_obj:
        response = workers.analyze_worker_trae_ui(
            worker_id="worker1",
            context='{"task":"find_prompt_input_and_send_button"}',
            file=UploadFile(file=file_obj, filename="screen.png"),
            worker=worker,
            db=db,
        )

    assert response["status"] == "analyzed"
    assert response["analysis"]["status"] == "found"
    assert captured["image_bytes"] == b"png"
    assert captured["context"]["task"] == "find_prompt_input_and_send_button"
    assert captured["configs"]["model"]["model_name"] == "vision"


def test_worker_package_path_checks_repo_storage_for_relative_attachment_root(monkeypatch, tmp_path: Path):
    package = tmp_path / "storage" / "worker-packages" / "agentops-worker-windows.zip"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"zip")
    monkeypatch.setattr(workers.settings, "worker_package_path", None)
    monkeypatch.setattr(workers.settings, "attachment_root", Path("storage"))
    monkeypatch.setattr(workers.settings, "repo_root", tmp_path)

    assert workers._worker_package_path() == package.resolve()


def _test_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()
