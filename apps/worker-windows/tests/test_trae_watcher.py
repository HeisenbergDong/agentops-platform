import os
import time

from worker.trae import watcher


def test_newest_mtime_under_ignores_build_artifacts(tmp_path):
    project = tmp_path / "project"
    src = project / "src"
    dist = project / "dist"
    src.mkdir(parents=True)
    dist.mkdir(parents=True)
    src_file = src / "app.py"
    dist_file = dist / "bundle.js"
    src_file.write_text("print('ok')", encoding="utf-8")
    dist_file.write_text("bundle", encoding="utf-8")
    old = time.time() - 100
    new = time.time()
    os.utime(src_file, (old, old))
    os.utime(dist_file, (new, new))

    mtime, path = watcher.newest_mtime_under(project)

    assert mtime == old
    assert path.endswith("app.py")


def test_activity_snapshot_uses_recent_agent_log(monkeypatch, tmp_path):
    log = tmp_path / "ai-agent_1_stdout.log"
    log.write_text("main_routine completed", encoding="utf-8")
    now = 2000.0
    log_mtime = now - 4.0
    os.utime(log, (log_mtime, log_mtime))

    monkeypatch.setattr(watcher.time, "time", lambda: now)
    monkeypatch.setattr(watcher, "latest_agent_log_path", lambda: log)

    result = watcher.activity_snapshot(None, started_at_epoch=now - 30.0, quiet_seconds=10)

    assert result["recent"] is True
    assert result["source"] == "agent_log"
    assert result["quiet_seconds"] == 4.0
    assert result["path"] == str(log)


def test_filtered_agent_log_tail_keeps_meaningful_lines(monkeypatch, tmp_path):
    log = tmp_path / "ai-agent_1_stdout.log"
    log.write_text(
        "\n".join(
            [
                "checkRunCommandStatus noisy",
                "toolcall_name: edit status: success",
                "plain heartbeat",
                "chat_turn_finish completed",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(watcher, "latest_agent_log_path", lambda: log)

    result = watcher.filtered_agent_log_tail(max_lines=10)

    assert result["tail_hash"]
    assert result["lines"] == ["toolcall_name: edit status: success", "chat_turn_finish completed"]
