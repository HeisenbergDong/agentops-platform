import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Job, User, UserConfig
from app.db.session import Base
from app.services.feishu.local_records import list_local_feishu_records
from app.services.orchestrator.states import JobState


def test_local_feishu_records_are_filtered_to_current_user(tmp_path: Path):
    db = _test_session()
    user = User(id="user1", email="user1@example.com", display_name="User 1", role="user", is_active=True)
    other = User(id="user2", email="user2@example.com", display_name="User 2", role="user", is_active=True)
    path = tmp_path / "records.jsonl"
    db.add_all(
        [
            user,
            other,
            UserConfig(user_id=user.id, category="feishu", data={"write_mode": "local_file", "local_file_path": str(path)}),
            Job(id="job1", user_id=user.id, status=JobState.ROUND_COMPLETED, directions=["demo"]),
            Job(id="job2", user_id=other.id, status=JobState.ROUND_COMPLETED, directions=["demo"]),
        ]
    )
    db.commit()
    _write_record(path, "record1", "job1", "第一轮")
    _write_record(path, "record2", "job2", "第二轮")

    result = list_local_feishu_records(db, user)

    assert result["fields"][0] == "Trae Session ID"
    assert [item["record_id"] for item in result["records"]] == ["record1"]
    assert result["records"][0]["fields"]["轮次"] == "第一轮"


def test_admin_can_read_all_local_feishu_records(tmp_path: Path):
    db = _test_session()
    admin = User(id="admin1", email="admin@example.com", display_name="Admin", role="admin", is_active=True)
    user = User(id="user1", email="user1@example.com", display_name="User 1", role="user", is_active=True)
    path = tmp_path / "records.jsonl"
    db.add_all(
        [
            admin,
            user,
            UserConfig(user_id=user.id, category="feishu", data={"write_mode": "local_file", "local_file_path": str(path)}),
            Job(id="job1", user_id=user.id, status=JobState.ROUND_COMPLETED, directions=["demo"]),
        ]
    )
    db.commit()
    _write_record(path, "record1", "job1", "第一轮")

    result = list_local_feishu_records(db, admin)

    assert [item["record_id"] for item in result["records"]] == ["record1"]
    assert result["records"][0]["user_id"] == user.id


def _write_record(path: Path, record_id: str, job_id: str, round_label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "created_at": f"2026-06-24T00:00:0{len(record_id)}+00:00",
                    "record_id": record_id,
                    "fields": {"Trae Session ID": record_id, "轮次": round_label},
                    "metadata": {"job_id": job_id, "round_id": f"{job_id}-round"},
                },
                ensure_ascii=False,
            )
        )
        handle.write("\n")


def _test_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()
