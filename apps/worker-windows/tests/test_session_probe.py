import json
import sqlite3

from worker.trae.session_probe import probe_latest_trae_turn


def test_probe_latest_trae_turn_reads_recent_completed_log(monkeypatch, tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    log_dir = appdata / "Trae CN" / "logs" / "20260611"
    log_dir.mkdir(parents=True)
    session_id = "41867a07a0b34471bd185ecc93ebf73b"
    user_message_id = "6a26227be4db5ceccde3e54e"
    task_id = "7a26227be4db5ceccde3e54f"
    trace_id = "8f0bce1835c9654e637c14de711bd35b"
    (log_dir / "ai-agent_1_stdout.log").write_text(
        "\n".join(
            [
                (
                    "2026-06-08T10:01:31.685492+08:00 INFO starting new task from idle, "
                    f"new_user_message_id: {user_message_id} trace_id=\"{trace_id}\" "
                    f"session_id={session_id} task_id={task_id}"
                ),
                (
                    "2026-06-08T10:02:01.000000+08:00 INFO plan tool call finish cost: 12ms, "
                    f"toolcall_name: \"edit\", status: success, user_message_id: {user_message_id}, task_id: {task_id}"
                ),
                (
                    "2026-06-08T10:03:01.000000+08:00 INFO chat_turn_finish completed: "
                    f"session_id={session_id}, message_id=\"{user_message_id}\" trace_id=\"{trace_id}\""
                ),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(appdata))

    result = probe_latest_trae_turn()

    assert result["status"] == "found"
    assert result["session_id"] == session_id
    assert result["user_message_id"] == user_message_id
    assert result["task_id"] == task_id
    assert result["trace_id"] == trace_id
    assert result["turn_status"] == "completed"


def test_probe_latest_trae_turn_prefers_matching_prompt_and_workspace(monkeypatch, tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    trae_root = appdata / "Trae CN"
    log_dir = trae_root / "logs" / "20260611"
    storage = trae_root / "User" / "workspaceStorage"
    log_dir.mkdir(parents=True)
    target_workspace = tmp_path / "projects" / "target"
    other_workspace = tmp_path / "projects" / "other"
    target_workspace.mkdir(parents=True)
    other_workspace.mkdir(parents=True)
    target_session = "41867a07a0b34471bd185ecc93ebf73b"
    target_message = "6a26227be4db5ceccde3e54e"
    target_task = "7a26227be4db5ceccde3e54f"
    other_session = "51867a07a0b34471bd185ecc93ebf73b"
    other_message = "7a26227be4db5ceccde3e54e"
    other_task = "8a26227be4db5ceccde3e54f"
    _write_workspace_db(storage / "target", target_workspace, target_session, "Build matching feature")
    _write_workspace_db(storage / "other", other_workspace, other_session, "Unrelated newer prompt")
    (log_dir / "ai-agent_1_stdout.log").write_text(
        "\n".join(
            [
                _start_line("2026-06-08T10:01:00+08:00", target_session, target_message, target_task, "8f0bce1835c9654e637c14de711bd35b"),
                _finish_line("2026-06-08T10:02:00+08:00", target_session, target_message, "8f0bce1835c9654e637c14de711bd35b"),
                _start_line("2026-06-08T11:01:00+08:00", other_session, other_message, other_task, "9f0bce1835c9654e637c14de711bd35b"),
                _finish_line("2026-06-08T11:02:00+08:00", other_session, other_message, "9f0bce1835c9654e637c14de711bd35b"),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(appdata))

    result = probe_latest_trae_turn(prompt="Build matching feature", workspace_path=str(target_workspace))

    assert result["status"] == "found"
    assert result["session_id"] == target_session
    assert result["user_message_id"] == target_message
    assert result["workspace_folder"].replace("\\", "/").endswith("/target")
    assert result["match_score"] > 20


def test_probe_latest_trae_turn_rejects_context_mismatch(monkeypatch, tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    trae_root = appdata / "Trae CN"
    log_dir = trae_root / "logs" / "20260611"
    storage = trae_root / "User" / "workspaceStorage"
    log_dir.mkdir(parents=True)
    old_workspace = tmp_path / "projects" / "old-project"
    requested_workspace = tmp_path / "projects" / "requested-project"
    old_workspace.mkdir(parents=True)
    requested_workspace.mkdir(parents=True)
    old_session = "41867a07a0b34471bd185ecc93ebf73b"
    old_message = "6a26227be4db5ceccde3e54e"
    old_task = "7a26227be4db5ceccde3e54f"
    _write_workspace_db(storage / "old", old_workspace, old_session, "Unrelated old prompt")
    (log_dir / "ai-agent_1_stdout.log").write_text(
        "\n".join(
            [
                _start_line("2026-06-08T11:01:00+08:00", old_session, old_message, old_task, "8f0bce1835c9654e637c14de711bd35b"),
                _finish_line("2026-06-08T11:02:00+08:00", old_session, old_message, "8f0bce1835c9654e637c14de711bd35b"),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(appdata))

    result = probe_latest_trae_turn(prompt="Build current requested feature", workspace_path=str(requested_workspace))

    assert result["status"] == "missing"
    assert result["reason"] in {"workspace_mismatch", "low_confidence_context_match"}
    assert result["candidate"]["session_id"] == old_session


def test_probe_latest_trae_turn_ignores_turns_before_prompt_send(monkeypatch, tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    log_dir = appdata / "Trae CN" / "logs" / "20260611"
    log_dir.mkdir(parents=True)
    old_session = "41867a07a0b34471bd185ecc93ebf73b"
    old_message = "6a26227be4db5ceccde3e54e"
    old_task = "7a26227be4db5ceccde3e54f"
    new_session = "51867a07a0b34471bd185ecc93ebf73b"
    new_message = "7a26227be4db5ceccde3e54e"
    new_task = "8a26227be4db5ceccde3e54f"
    (log_dir / "ai-agent_1_stdout.log").write_text(
        "\n".join(
            [
                _start_line("2026-06-08T10:01:00+08:00", old_session, old_message, old_task, "8f0bce1835c9654e637c14de711bd35b"),
                _finish_line("2026-06-08T10:02:00+08:00", old_session, old_message, "8f0bce1835c9654e637c14de711bd35b"),
                _start_line("2026-06-08T10:05:00+08:00", new_session, new_message, new_task, "9f0bce1835c9654e637c14de711bd35b"),
                _finish_line("2026-06-08T10:06:00+08:00", new_session, new_message, "9f0bce1835c9654e637c14de711bd35b"),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(appdata))
    sent_after_epoch = _epoch("2026-06-08T10:03:00+08:00")

    result = probe_latest_trae_turn(sent_after_epoch=sent_after_epoch)

    assert result["status"] == "found"
    assert result["session_id"] == new_session


def test_probe_latest_trae_turn_blocks_when_only_old_turn_exists(monkeypatch, tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    log_dir = appdata / "Trae CN" / "logs" / "20260611"
    log_dir.mkdir(parents=True)
    old_session = "41867a07a0b34471bd185ecc93ebf73b"
    old_message = "6a26227be4db5ceccde3e54e"
    old_task = "7a26227be4db5ceccde3e54f"
    (log_dir / "ai-agent_1_stdout.log").write_text(
        "\n".join(
            [
                _start_line("2026-06-08T10:01:00+08:00", old_session, old_message, old_task, "8f0bce1835c9654e637c14de711bd35b"),
                _finish_line("2026-06-08T10:02:00+08:00", old_session, old_message, "8f0bce1835c9654e637c14de711bd35b"),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(appdata))
    sent_after_epoch = _epoch("2026-06-08T10:03:00+08:00")

    result = probe_latest_trae_turn(sent_after_epoch=sent_after_epoch)

    assert result["status"] == "missing"
    assert result["reason"] == "no_completed_turn_after_prompt_send"
    assert result["candidate"]["session_id"] == old_session


def _write_workspace_db(path, folder, session_id: str, prompt: str) -> None:
    path.mkdir(parents=True)
    (path / "workspace.json").write_text(json.dumps({"folder": folder.as_uri()}), encoding="utf-8")
    with sqlite3.connect(path / "state.vscdb") as conn:
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        values = {
            "memento/icube-ai-agent-storage": json.dumps(
                {"currentSessionId": session_id, "list": [{"sessionId": session_id}]},
                ensure_ascii=False,
            ),
            "icube_session_agent_map": json.dumps({session_id: "default"}, ensure_ascii=False),
            "icube-ai-agent-storage-input-history": json.dumps([{"inputText": prompt}], ensure_ascii=False),
        }
        conn.executemany("INSERT INTO ItemTable (key, value) VALUES (?, ?)", values.items())


def _start_line(timestamp: str, session_id: str, message_id: str, task_id: str, trace_id: str) -> str:
    return (
        f"{timestamp} INFO starting new task from idle, new_user_message_id: {message_id} "
        f"trace_id=\"{trace_id}\" session_id={session_id} task_id={task_id}"
    )


def _finish_line(timestamp: str, session_id: str, message_id: str, trace_id: str) -> str:
    return f"{timestamp} INFO chat_turn_finish completed: session_id={session_id}, message_id=\"{message_id}\" trace_id=\"{trace_id}\""


def _epoch(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value).timestamp()
