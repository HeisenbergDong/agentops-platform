from app.services.feishu import writer
import pytest


def test_write_feishu_record_updates_explicit_uid_with_allowed_overwrite(monkeypatch):
    _patch_feishu_dependencies(
        monkeypatch,
        records=[
            {
                "record_id": "rec-10",
                "fields": {
                    "UID": "10",
                    writer.SESSION_FIELD: "existing-session",
                    "User Prompt": "old prompt",
                    "不满意原因": "old reason",
                },
            }
        ],
    )
    requested = {}

    def fake_request_json(method, url, access_token, params=None, json=None):
        requested["method"] = method
        requested["url"] = url
        requested["json"] = json
        return {"code": 0, "data": {"record": {"record_id": "rec-10"}}}

    monkeypatch.setattr(writer, "_request_json", fake_request_json)

    result = writer.write_feishu_record(
        {
            "app_token": "app-token",
            "table_id": "table-id",
            "target_uid": "10",
            "overwrite_fields": ["不满意原因"],
            "token_cache": {"tenant_access_token": "cached-token", "expires_at": 4102444800},
        },
        {
            writer.SESSION_FIELD: "new-session",
            "User Prompt": "new prompt",
            "不满意原因": "new reason",
        },
    )

    assert result["status"] == "written"
    assert requested["method"] == "PUT"
    assert requested["json"]["fields"] == {"不满意原因": "new reason"}


def test_write_feishu_record_skips_duplicate_by_prompt_round(monkeypatch):
    calls = []
    monkeypatch.setattr(writer, "get_feishu_access_token", lambda _config: ("cached-token", None, "tenant"))
    monkeypatch.setattr(
        writer,
        "_list_fields",
        lambda _token, _app, _table: [
            {"field_name": writer.SESSION_FIELD},
            {"field_name": "轮次"},
            {"field_name": "User Prompt"},
            {"field_name": "任务类型"},
            {"field_name": "业务领域"},
        ],
    )

    def fake_request_json(method, url, access_token, params=None, json=None):
        calls.append((method, url, json))
        if method == "POST" and url.endswith("/records/search"):
            conditions = (((json or {}).get("filter") or {}).get("conditions") or [])
            field_names = [condition.get("field_name") for condition in conditions]
            if field_names == [writer.SESSION_FIELD]:
                return {"code": 0, "data": {"items": []}}
            if field_names == ["User Prompt", "轮次"]:
                return {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "record_id": "rec-1",
                                "fields": {
                                    "UID": "1",
                                    writer.SESSION_FIELD: "existing-session",
                                    "User Prompt": "订单系统补筛选",
                                    "轮次": "第二轮",
                                    "任务类型": "Bug修复",
                                    "业务领域": "全栈Web应用",
                                },
                            }
                        ]
                    },
                }
            return {"code": 0, "data": {"items": [{"record_id": "rec-2", "fields": {"UID": "2", writer.SESSION_FIELD: ""}}]}}
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(writer, "_request_json", fake_request_json)
    result = writer.write_feishu_record(
        {
            "app_token": "app-token",
            "table_id": "table-id",
            "token_cache": {"tenant_access_token": "cached-token", "expires_at": 4102444800},
        },
        {
            writer.SESSION_FIELD: "new-session",
            "User Prompt": "订单系统补筛选",
            "轮次": "第二轮",
            "任务类型": "Bug修复",
            "业务领域": "全栈Web应用",
        },
    )

    assert result["status"] == "skipped_duplicate"
    assert result["record_id"] == "rec-1"
    assert result["duplicate_existing_uid"] == "1"
    assert len([body for method, url, body in calls if method == "POST" and url.endswith("/records/search")]) >= 3


def test_task_type_test_prefix_normalizes_to_allowed_option():
    assert writer._normalize_option(
        "任务类型",
        "测试-Bug修复",
        {"任务类型": {"Bug修复", "Feature迭代", "0-1代码生成"}},
    ) == "Bug修复"
    assert writer._normalize_option(
        "任务类型",
        "测试-0-1代码生成",
        {"任务类型": {"Bug修复", "Feature迭代", "0-1代码生成"}},
    ) == "0-1代码生成"


def test_write_feishu_record_fails_when_required_payload_field_is_missing_from_table(monkeypatch):
    _patch_feishu_dependencies(
        monkeypatch,
        records=[{"record_id": "rec-2", "fields": {"UID": "2", writer.SESSION_FIELD: ""}}],
    )
    monkeypatch.setattr(
        writer,
        "_list_fields",
        lambda _token, _app, _table: [
            {"field_name": writer.SESSION_FIELD},
            {"field_name": "User Prompt"},
            {"field_name": "轮次"},
        ],
    )

    with pytest.raises(writer.FeishuWriteError, match="missing required writable fields"):
        writer.write_feishu_record(
            {
                "app_token": "app-token",
                "table_id": "table-id",
                "token_cache": {"tenant_access_token": "cached-token", "expires_at": 4102444800},
            },
            {
                writer.SESSION_FIELD: "new-session",
                "User Prompt": "prompt",
                "github地址": "https://github.com/acme/repo.git",
            },
        )


def test_write_feishu_record_uses_search_instead_of_listing_all_records(monkeypatch):
    calls = []

    monkeypatch.setattr(
        writer,
        "get_feishu_access_token",
        lambda _config: ("cached-token", None, "tenant"),
    )
    monkeypatch.setattr(
        writer,
        "_list_fields",
        lambda _token, _app, _table: [
            {"field_name": writer.SESSION_FIELD},
            {"field_name": "User Prompt"},
            {"field_name": "轮次"},
        ],
    )

    def fake_request_json(method, url, access_token, params=None, json=None):
        calls.append((method, url, json))
        if method == "POST" and url.endswith("/records/search"):
            return {"code": 0, "data": {"items": [{"record_id": "rec-empty", "fields": {"UID": "1", writer.SESSION_FIELD: ""}}]}}
        if method == "PUT":
            return {"code": 0, "data": {"record": {"record_id": "rec-empty"}}}
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(writer, "_request_json", fake_request_json)

    result = writer.write_feishu_record(
        {
            "app_token": "app-token",
            "table_id": "table-id",
            "token_cache": {"tenant_access_token": "cached-token", "expires_at": 4102444800},
        },
        {writer.SESSION_FIELD: "session-1", "User Prompt": "prompt", "轮次": "第一轮"},
    )

    assert result["status"] == "written"
    assert any(method == "POST" and url.endswith("/records/search") for method, url, _json in calls)
    assert not any(method == "GET" and url.endswith("/records") for method, url, _json in calls)
    search_bodies = [body for method, url, body in calls if method == "POST" and url.endswith("/records/search")]
    assert search_bodies
    assert search_bodies[0].get("field_names") == ["UID", writer.SESSION_FIELD]
    assert any(body.get("field_names") == ["UID", writer.SESSION_FIELD, "User Prompt", "轮次", "任务类型", "业务领域"] for body in search_bodies)


def test_feishu_error_message_identifies_permission_and_field_mapping():
    permission = writer._format_feishu_error(403, {"code": 99991663, "msg": "Forbidden"}, "")
    bitable_permission = writer._format_feishu_error(403, {"code": 91403, "msg": "Forbidden"}, "")
    field_error = writer._format_feishu_error(200, {"code": 1254007, "msg": "field_name is invalid"}, "")

    assert "permission denied" in permission
    assert "reauthorize Feishu user OAuth" in bitable_permission
    assert "field mapping failed" in field_error


def test_request_error_captures_bitable_operation():
    exc = writer._feishu_request_error(
        403,
        {"code": 91403, "msg": "Forbidden"},
        "",
        "GET",
        "https://open.feishu.cn/open-apis/bitable/v1/apps/app-token/tables/table-id/fields",
    )

    assert exc.operation == "list_fields"
    assert exc.status_code == 403
    assert exc.code == 91403

    search_exc = writer._feishu_request_error(
        200,
        {"code": 1254007, "msg": "field_name is invalid"},
        "",
        "POST",
        "https://open.feishu.cn/open-apis/bitable/v1/apps/app-token/tables/table-id/records/search",
    )

    assert search_exc.operation == "search_records"


def test_discovery_uses_permission_help_message(monkeypatch):
    from app.services.feishu import discovery

    class Response:
        status_code = 403
        text = '{"code":91403,"msg":"Forbidden"}'

        def json(self):
            return {"code": 91403, "msg": "Forbidden"}

    monkeypatch.setattr(discovery.httpx, "get", lambda *_args, **_kwargs: Response())

    try:
        discovery._get_items("https://open.feishu.cn/open-apis/bitable/v1/apps/app/tables", "token", {})
    except discovery.FeishuDiscoveryError as exc:
        assert "reauthorize Feishu user OAuth" in str(exc)
    else:
        raise AssertionError("discovery should fail on Feishu 403")


def test_prepare_attachment_field_uploads_local_paths(monkeypatch, tmp_path):
    report = tmp_path / "trace.txt"
    report.write_text("trace", encoding="utf-8")
    uploaded = []

    def fake_upload(access_token: str, app_token: str, file_path: str) -> str:
        uploaded.append((access_token, app_token, file_path))
        return "file-token-trace"

    monkeypatch.setattr(writer, "_upload_bitable_attachment", fake_upload)

    mapped = {
        writer.ATTACHMENT_FIELD: [
            str(report),
            {"file_token": "existing-token"},
            str(report),
        ]
    }

    result = writer._prepare_attachment_field(
        "access-token",
        "app-token",
        mapped,
        {writer.ATTACHMENT_FIELD},
    )

    assert result[writer.ATTACHMENT_FIELD] == [
        {"file_token": "file-token-trace"},
        {"file_token": "existing-token"},
    ]
    assert uploaded == [
        ("access-token", "app-token", str(report)),
        ("access-token", "app-token", str(report)),
    ]


def _patch_feishu_dependencies(monkeypatch, records):
    fields = [
        { "field_name": writer.SESSION_FIELD },
        { "field_name": "轮次" },
        { "field_name": "User Prompt" },
        { "field_name": "任务类型" },
        { "field_name": "业务领域" },
        { "field_name": "不满意原因" },
    ]

    monkeypatch.setattr(
        writer,
        "get_feishu_access_token",
        lambda _config: ("cached-token", None, "tenant"),
    )
    monkeypatch.setattr(writer, "_list_fields", lambda _token, _app, _table: fields)
    monkeypatch.setattr(
        writer,
        "_find_empty_session_record",
        lambda _token, _app, _table: records[-1] if records else {},
    )
    monkeypatch.setattr(
        writer,
        "_find_duplicate_record_for_search",
        lambda _token, _app, _table, target_id, mapped: writer._find_duplicate_record(records, target_id, mapped),
    )
    monkeypatch.setattr(
        writer,
        "_find_explicit_target_record",
        lambda _token, _app, _table, config: writer._find_explicit_target_record_from_records(records, config),
    )
    monkeypatch.setattr(
        writer,
        "_request_json",
        lambda _method, _url, _access_token, params=None, json=None: {"code": 0, "data": {"record": {"record_id": "rec"}}},
    )
