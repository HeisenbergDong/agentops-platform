from app.services.feishu import writer


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
    _patch_feishu_dependencies(
        monkeypatch,
        records=[
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
            },
            {
                "record_id": "rec-2",
                "fields": {
                    "UID": "2",
                    writer.SESSION_FIELD: "",
                },
            },
        ],
    )

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
    monkeypatch.setattr(writer, "_list_records", lambda _token, _app, _table: records)
    monkeypatch.setattr(
        writer,
        "_request_json",
        lambda _method, _url, _access_token, params=None, json=None: {"code": 0, "data": {"record": {"record_id": "rec"}}},
    )
