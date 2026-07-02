from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import workers as workers_api
from app.db.models import Job, RuntimeLog, TaskRound, Worker, WorkerCommand
from app.db.models.base import now_utc
from app.db.repositories import workers as worker_repo
from app.db.session import Base
from app.worker_gateway.contracts import (
    CreateWorkerCommandRequest,
    WorkerCommandType,
    WorkerHeartbeat,
    WorkerLogEntry,
    WorkerRegisterRequest,
    WorkerResult,
)


def test_poll_assigns_lease_and_requeues_expired_claim(monkeypatch):
    db = _test_session()
    command = _create_command(db)
    monkeypatch.setattr(worker_repo.settings, "worker_command_claim_lease_seconds", 1)

    polled = worker_repo.poll_worker_commands(db, "worker1")
    lease_id = polled[0].lease_id
    polled[0].lease_expires_at = now_utc() - timedelta(seconds=1)
    db.commit()

    result = worker_repo.expire_worker_command_leases(db, worker_id="worker1")
    db.refresh(command)

    assert lease_id
    assert result["requeued"] == 1
    assert command.status == "queued"
    assert command.lease_id == ""
    assert command.attempts == 1


def test_claim_expires_to_failed_after_max_attempts(monkeypatch):
    db = _test_session()
    _job, _round, command = _create_job_round_command(db)
    monkeypatch.setattr(worker_repo.settings, "worker_command_max_claim_attempts", 2)
    command.status = "claimed"
    command.attempts = 2
    command.lease_id = "claim-1"
    command.lease_expires_at = now_utc() - timedelta(seconds=1)
    db.commit()

    result = worker_repo.expire_worker_command_leases(db, worker_id="worker1")
    db.refresh(command)
    log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == "worker_command_lease_expired"))

    assert result["failed"] == 1
    assert command.status == "failed"
    assert command.finished_at is not None
    assert log is not None


def test_running_lease_expiration_cancels_command_and_marks_job_manual_required():
    db = _test_session()
    job, round_, command = _create_job_round_command(db)
    command.status = "running"
    command.lease_id = "run-1"
    command.lease_expires_at = now_utc() - timedelta(seconds=1)
    db.commit()

    result = worker_repo.expire_worker_command_leases(db, worker_id="worker1")
    db.refresh(job)
    db.refresh(round_)
    db.refresh(command)

    assert result["cancelled"] == 1
    assert command.status == "cancelled"
    assert command.finished_at is not None
    assert job.status == "manual_required"
    assert round_.status == "manual_required"


def test_ack_rejects_stale_claim_lease():
    db = _test_session()
    command = _create_command(db)
    polled = worker_repo.poll_worker_commands(db, "worker1")[0]

    acked, status = worker_repo.ack_worker_command(db, "worker1", polled.id, lease_id="wrong-lease")

    assert acked.id == command.id
    assert status == "stale_lease"
    assert acked.status == "claimed"


def test_poll_prioritizes_stop_command_before_older_work():
    db = _test_session()
    wait_command = _create_command(db)
    stop_command = worker_repo.create_worker_command(
        db,
        worker_id="worker1",
        user_id="user1",
        payload=CreateWorkerCommandRequest(
            type=WorkerCommandType.STOP_CURRENT_TASK,
            job_id=None,
            round_id=None,
            payload={"reason": "user_stop"},
        ),
    )

    polled = worker_repo.poll_worker_commands(db, "worker1", limit=2)

    assert [item.id for item in polled] == [stop_command.id, wait_command.id]


def test_ack_rotates_running_lease_and_renew_extends_it(monkeypatch):
    db = _test_session()
    _create_command(db)
    monkeypatch.setattr(worker_repo.settings, "worker_command_run_lease_seconds", 120)
    polled = worker_repo.poll_worker_commands(db, "worker1")[0]
    claim_lease_id = polled.lease_id

    acked, status = worker_repo.ack_worker_command(db, "worker1", polled.id, lease_id=claim_lease_id)
    original_expiry = acked.lease_expires_at
    acked.lease_expires_at = now_utc() + timedelta(seconds=1)
    db.commit()
    renewed, read_status = worker_repo.read_worker_command_for_worker(
        db,
        "worker1",
        acked.id,
        lease_id=acked.lease_id,
        renew=True,
    )

    assert status == "ok"
    assert acked.status == "running"
    assert acked.lease_id and acked.lease_id != claim_lease_id
    assert read_status == "ok"
    assert renewed.lease_expires_at > original_expiry - timedelta(seconds=5)


def test_stale_result_is_ignored_by_worker_api():
    db = _test_session()
    worker = Worker(worker_id="worker1", user_id="user1", machine_name="host", token_hash="hash")
    db.add(worker)
    command = _create_command(db)
    command.status = "running"
    command.lease_id = "run-current"
    command.lease_expires_at = now_utc() + timedelta(minutes=5)
    db.commit()

    response = workers_api.post_result(
        "worker1",
        WorkerResult(
            command_id=command.id,
            worker_id="worker1",
            lease_id="old-run",
            status="success",
            data={"text_chars": 1000},
        ),
        worker=worker,
        db=db,
    )
    db.refresh(command)

    assert response["status"] == "ignored"
    assert response["reason"] == "stale_lease"
    assert command.status == "running"
    assert command.result == {}


def test_finished_log_recovers_result_when_results_callback_is_missing():
    db = _test_session()
    worker = Worker(worker_id="worker1", user_id="user1", machine_name="host", token_hash="hash")
    job, round_, command = _create_job_round_command(db)
    command.status = "running"
    command.lease_id = "run-current"
    command.lease_expires_at = now_utc() + timedelta(minutes=5)
    db.add(worker)
    db.commit()

    response = workers_api.post_log(
        "worker1",
        WorkerLogEntry(
            command_id=command.id,
            job_id=job.id,
            round_id=round_.id,
            level="info",
            stage="worker_command_finished",
            message="wait_completion worker_command_finished",
            extra={
                "command_type": WorkerCommandType.WAIT_COMPLETION.value,
                "result_status": "manual_required",
                "error": "Trae output did not become stable before wait_completion timeout",
                "result": {
                    "output_probe": {"complete_like": False, "reason": "service_interrupted", "marker": "任务中断"},
                    "trae_turn": {"status": "missing", "reason": "awaiting_current_continuation"},
                    "completion_gate": {"passed": False, "reason": "pending_intervention_visible", "recoverable": True},
                    "supervisor_decision": {"action": "recover_service_interruption", "reason": "service_interrupted"},
                },
            },
        ),
        worker=worker,
        db=db,
    )

    db.refresh(command)
    db.refresh(job)
    recovery_log = db.scalar(select(RuntimeLog).where(RuntimeLog.stage == "worker_result_recovered_from_log"))

    assert response["status"] == "received"
    assert command.status == "manual_required"
    assert command.lease_id == ""
    assert command.result["output_probe"]["reason"] == "service_interrupted"
    assert job.status != "manual_required"
    assert recovery_log is not None


def test_cancelled_result_with_stop_report_is_accepted_after_server_cancelled_command():
    db = _test_session()
    worker = Worker(worker_id="worker1", user_id="user1", machine_name="host", token_hash="hash")
    db.add(worker)
    command = _create_command(db)
    command.status = "cancelled"
    command.lease_id = "run-current"
    command.lease_expires_at = now_utc() + timedelta(minutes=5)
    db.commit()

    response = workers_api.post_result(
        "worker1",
        WorkerResult(
            command_id=command.id,
            worker_id="worker1",
            lease_id="old-run",
            status="cancelled",
            data={
                "stopped": True,
                "stop_report": {
                    "worker_command_cancelled": True,
                    "stop_confirmed": True,
                    "cleanup_status": "no_matching_processes",
                },
            },
        ),
        worker=worker,
        db=db,
    )
    db.refresh(command)

    assert response["status"] == "received"
    assert command.status == "completed"
    assert command.result["stop_report"]["stop_confirmed"] is True


def test_registered_worker_is_not_reported_online_until_heartbeat():
    db = _test_session()
    code, _row = worker_repo.create_registration_code(db, created_by="admin1", assigned_user_id="user1")

    worker, _token = worker_repo.register_worker(
        db,
        WorkerRegisterRequest(
            registration_code=code,
            worker_id="local-windows-worker",
            machine_name="host",
            display_name="Local Worker",
            version="test",
        ),
    )

    registered = workers_api.serialize_worker(worker)
    assert registered["registered"] is True
    assert registered["online"] is False
    assert registered["status"] == "offline"
    assert registered["busy"] is False

    worker_repo.update_worker_heartbeat(
        db,
        worker,
        WorkerHeartbeat(
            worker_id="local-windows-worker",
            machine_name="host",
            display_name="Local Worker",
            version="test",
            busy=False,
        ),
    )
    online = workers_api.serialize_worker(worker)
    assert online["online"] is True
    assert online["status"] == "online"
    assert online["busy"] is False


def test_register_worker_generates_id_when_default_local_id_is_sent():
    db = _test_session()
    code, _row = worker_repo.create_registration_code(db, created_by="admin1", assigned_user_id="user1")

    worker, _token = worker_repo.register_worker(
        db,
        WorkerRegisterRequest(
            registration_code=code,
            worker_id="local-windows-worker",
            machine_name="host",
            display_name="Local Worker",
            version="test",
        ),
    )

    assert worker.worker_id.startswith("worker_")
    assert worker.worker_id != "local-windows-worker"
    assert worker.user_id == "user1"


def test_register_worker_rejects_rebinding_existing_worker_to_another_user():
    db = _test_session()
    db.add(Worker(worker_id="shared-worker", user_id="user1", machine_name="host", token_hash="hash"))
    db.commit()
    code, _row = worker_repo.create_registration_code(db, created_by="admin1", assigned_user_id="user2")

    try:
        worker_repo.register_worker(
            db,
            WorkerRegisterRequest(
                registration_code=code,
                worker_id="shared-worker",
                machine_name="host-2",
                display_name="Local Worker",
                version="test",
            ),
        )
    except ValueError as exc:
        assert "already bound to another user" in str(exc)
    else:
        raise AssertionError("Expected duplicate worker registration to fail")

    worker = db.scalar(select(Worker).where(Worker.worker_id == "shared-worker"))
    assert worker is not None
    assert worker.user_id == "user1"
    assert worker.machine_name == "host"


def test_register_worker_allows_same_user_to_refresh_existing_worker_token():
    db = _test_session()
    db.add(Worker(worker_id="user-worker", user_id="user1", machine_name="old-host", token_hash="old"))
    db.commit()
    code, _row = worker_repo.create_registration_code(db, created_by="admin1", assigned_user_id="user1")

    worker, token = worker_repo.register_worker(
        db,
        WorkerRegisterRequest(
            registration_code=code,
            worker_id="user-worker",
            machine_name="new-host",
            display_name="Local Worker",
            version="test",
        ),
    )

    assert token
    assert worker.worker_id == "user-worker"
    assert worker.user_id == "user1"
    assert worker.machine_name == "new-host"


def test_delete_worker_revokes_and_hides_offline_worker():
    db = _test_session()
    worker = Worker(worker_id="old-worker", user_id="user1", machine_name="host", token_hash="hash")
    db.add(worker)
    command = worker_repo.create_worker_command(
        db,
        worker_id="old-worker",
        user_id="user1",
        payload=CreateWorkerCommandRequest(
            type=WorkerCommandType.WAIT_COMPLETION,
            job_id=None,
            round_id=None,
            payload={},
        ),
    )

    deleted = worker_repo.delete_worker(db, "old-worker")
    db.refresh(command)

    assert deleted is not None
    assert deleted.revoked_at is not None
    assert deleted.user_id is None
    assert deleted.token_hash == ""
    assert command.status == "cancelled"
    assert worker_repo.list_workers(db) == []
    assert worker_repo.get_worker_by_worker_id(db, "old-worker") is None
    assert worker_repo.get_worker_by_worker_id(db, "old-worker", include_revoked=True) is not None


def test_delete_worker_rejects_online_worker():
    db = _test_session()
    worker = Worker(
        worker_id="online-worker",
        user_id="user1",
        machine_name="host",
        token_hash="hash",
        status="online",
        last_seen_at=now_utc(),
    )
    db.add(worker)
    db.commit()

    try:
        worker_repo.delete_worker(db, "online-worker")
    except ValueError as exc:
        assert "online Worker" in str(exc)
    else:
        raise AssertionError("Expected online Worker deletion to fail")

    db.refresh(worker)
    assert worker.revoked_at is None
    assert worker_repo.get_worker_by_worker_id(db, "online-worker") is not None


def _test_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _create_command(db) -> WorkerCommand:
    command = worker_repo.create_worker_command(
        db,
        worker_id="worker1",
        user_id="user1",
        payload=CreateWorkerCommandRequest(
            type=WorkerCommandType.WAIT_COMPLETION,
            job_id=None,
            round_id=None,
            payload={},
        ),
    )
    return command


def _create_job_round_command(db) -> tuple[Job, TaskRound, WorkerCommand]:
    job = Job(user_id="user1", status="waiting_trae", directions=["demo"])
    db.add(job)
    db.flush()
    round_ = TaskRound(job_id=job.id, round_index=1, status="waiting_trae")
    db.add(round_)
    db.flush()
    command = worker_repo.create_worker_command(
        db,
        worker_id="worker1",
        user_id="user1",
        payload=CreateWorkerCommandRequest(
            type=WorkerCommandType.WAIT_COMPLETION,
            job_id=job.id,
            round_id=round_.id,
            payload={},
        ),
    )
    return job, round_, command
