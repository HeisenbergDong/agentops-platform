from pathlib import Path

import pytest

from worker.project import browser_acceptance
from worker.project.browser_acceptance import run_browser_acceptance
from worker.project.diagnostics import summarize_command_result
from worker.project.git_submit import _classify_push_failure, _push_args
from worker.project.scanner import scan_project


def test_scan_project_detects_nested_node_project(tmp_path: Path):
    workspace = tmp_path / "workspace"
    app = workspace / "nested-app"
    app.mkdir(parents=True)
    (app / "package.json").write_text(
        '{"scripts":{"build":"vite build","dev":"vite"}}',
        encoding="utf-8",
    )
    src = app / "src"
    src.mkdir()
    (src / "main.js").write_text(
        "document.querySelector('button')?.addEventListener('click', () => document.body.dataset.clicked = '1')",
        encoding="utf-8",
    )

    result = scan_project(workspace)

    assert result["root"] == str(workspace)
    assert result["project_root"] == str(app)
    assert result["recommended_command_cwd"] == str(app)
    assert result["recommended_commands"] == [["npm", "run", "build"]]
    assert result["product_review"]["ok"] is True


def test_scan_project_reports_static_product_review_issues(tmp_path: Path):
    workspace = tmp_path / "workspace"
    app = workspace / "app"
    src = app / "src"
    src.mkdir(parents=True)
    (app / "package.json").write_text('{"scripts":{"build":"vite build"}}', encoding="utf-8")
    (src / "App.vue").write_text(
        """
<template>
  <button @click="">保存</button>
</template>
<script setup>
function save() {}
</script>
""",
        encoding="utf-8",
    )

    result = scan_project(workspace, prompt="做一个能保存备注并刷新列表的页面")

    review = result["product_review"]
    assert review["ok"] is False
    assert review["blocking_issue_count"] >= 1
    assert any(item["code"] == "empty_event_handler" for item in review["file_findings"])
    assert any(item["code"] == "empty_function" for item in review["file_findings"])
    assert any("事件绑定为空" in item for item in review["issues"])
    assert any("函数体为空" in item for item in review["issues"])


def test_browser_acceptance_starts_nested_vite_dev_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    app = workspace / "apps" / "web"
    (app / "node_modules").mkdir(parents=True)
    (app / "package.json").write_text(
        '{"scripts":{"dev":"vite","build":"vite build"}}',
        encoding="utf-8",
    )
    fetch_results = [
        {"status": "failed", "project_path": str(workspace), "url": "http://localhost:5173", "message": "refused"},
        {"status": "passed", "project_path": str(workspace), "url": "http://localhost:5173", "http_status": 200},
    ]
    launched = {}

    def fake_fetch(project_path: str, normalized_url: str, timeout_seconds: float, cancellation_check=None):
        return fetch_results.pop(0)

    class FakeProcess:
        pid = 1234

    def fake_popen(command, cwd, env, stdin, stdout, stderr, creationflags):
        launched["command"] = command
        launched["cwd"] = cwd
        launched["env"] = env
        return FakeProcess()

    monkeypatch.setattr(browser_acceptance, "_fetch_url", fake_fetch)
    monkeypatch.setattr(browser_acceptance.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(browser_acceptance.time, "sleep", lambda seconds: None)

    result = run_browser_acceptance(str(workspace), "localhost:5173", timeout_seconds=0.1)

    assert result["status"] == "passed"
    assert result["auto_start"]["cwd"] == str(app)
    assert launched["command"] == ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173"]


def test_browser_acceptance_html_inspection_rejects_blank_page():
    result = browser_acceptance._inspect_html("<html><body><div id='app'></div></body></html>", "text/html")

    assert result["issues"]
    assert "接近空白" in result["issues"][0]


def test_browser_acceptance_html_inspection_keeps_interaction_evidence():
    result = browser_acceptance._inspect_html(
        "<html><head><title>Demo</title></head><body><button>保存</button><input value='demo'>订单看板</body></html>",
        "text/html",
    )

    assert result["issues"] == []
    assert result["title"] == "Demo"
    assert result["interactive_count"] == 2
    assert result["interaction"]["buttons"] == 1
    assert result["interaction"]["inputs"] == 1
    assert result["interaction"]["button_labels"] == ["保存"]


def test_git_push_args_set_upstream_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    calls = []

    class Result:
        returncode = 1
        stdout = ""
        stderr = "no upstream"

    def fake_git(cwd: Path, args: list[str], timeout: int, cancellation_check=None):
        calls.append(args)
        return Result()

    monkeypatch.setattr("worker.project.git_submit._git", fake_git)

    args = _push_args(tmp_path, "origin", "", "main", 120)

    assert calls == [["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]]
    assert args == ["push", "--set-upstream", "origin", "main"]


def test_git_push_failure_classifies_credentials():
    assert _classify_push_failure("Permission denied (publickey). fatal: Could not read from remote repository.") == (
        "ssh_key_or_deploy_key_failed"
    )
    assert _classify_push_failure("fatal: Authentication failed for 'https://github.com/acme/repo.git'") == (
        "https_token_or_credential_failed"
    )


def test_command_diagnostics_classify_build_error_with_source_location():
    result = summarize_command_result(
        ["npm", "run", "build"],
        1,
        stdout="",
        stderr="src/App.tsx:12:5: error TS2339: Property 'name' does not exist",
    )

    assert result["passed"] is False
    assert result["error_type"] == "type_error"
    assert result["primary_location"] == {"path": "src/App.tsx", "line": 12, "column": 5}
    assert "npm run build" in result["summary"]
