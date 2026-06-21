import time

from app.core.secrets import seal_secret
from app.services.feishu import auth as feishu_auth
from app.services.feishu.auth import exchange_authorization_code, get_feishu_access_token
from app.services.user_settings import safe_open_secret


def test_cached_user_token_is_preferred(monkeypatch):
    def fail_tenant(*_args, **_kwargs):
        raise AssertionError("tenant token should not be requested")

    monkeypatch.setattr(feishu_auth, "tenant_access_token", fail_tenant)

    token, refreshed_cache, auth_mode = get_feishu_access_token(
        {
            "app_id": "cli_test",
            "app_secret": seal_secret("secret"),
            "token_cache": {
                "user_access_token": seal_secret("cached-user-token"),
                "user_expires_at": int(time.time()) + 3600,
                "refresh_token": seal_secret("refresh-token"),
            },
        }
    )

    assert token == "cached-user-token"
    assert refreshed_cache is None
    assert auth_mode == "user_oauth"


def test_refresh_token_updates_user_cache(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "code": 0,
                "data": {
                    "access_token": "fresh-user-token",
                    "expires_in": 7200,
                    "refresh_token": "fresh-refresh-token",
                    "refresh_token_expires_in": 604800,
                    "scope": "offline_access base:record:create",
                },
            }

    monkeypatch.setattr(feishu_auth.httpx, "post", lambda *_args, **_kwargs: Response())

    token, refreshed_cache, auth_mode = get_feishu_access_token(
        {
            "app_id": "cli_test",
            "app_secret": seal_secret("secret"),
            "token_cache": {
                "refresh_token": seal_secret("old-refresh-token"),
                "refresh_expires_at": int(time.time()) + 604800,
            },
        }
    )

    assert token == "fresh-user-token"
    assert refreshed_cache is not None
    assert safe_open_secret(refreshed_cache["user_access_token"]) == "fresh-user-token"
    assert safe_open_secret(refreshed_cache["refresh_token"]) == "fresh-refresh-token"
    assert refreshed_cache["scope"] == "offline_access base:record:create"
    assert auth_mode == "user_oauth"


def test_failed_user_refresh_falls_back_to_tenant_token(monkeypatch):
    def fail_refresh(*_args, **_kwargs):
        raise feishu_auth.FeishuAuthError("refresh token invalid")

    def tenant_token(app_id, app_secret):
        assert app_id == "cli_test"
        assert app_secret == "secret"
        return {"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}

    monkeypatch.setattr(feishu_auth, "refresh_user_token", fail_refresh)
    monkeypatch.setattr(feishu_auth, "tenant_access_token", tenant_token)

    token, refreshed_cache, auth_mode = get_feishu_access_token(
        {
            "app_id": "cli_test",
            "app_secret": seal_secret("secret"),
            "token_cache": {
                "refresh_token": seal_secret("bad-refresh-token"),
                "refresh_expires_at": int(time.time()) + 604800,
            },
        }
    )

    assert token == "tenant-token"
    assert refreshed_cache is not None
    assert safe_open_secret(refreshed_cache["tenant_access_token"]) == "tenant-token"
    assert auth_mode == "tenant"


def test_require_user_oauth_does_not_fall_back_to_tenant(monkeypatch):
    def fail_tenant(*_args, **_kwargs):
        raise AssertionError("tenant token should not be requested")

    monkeypatch.setattr(feishu_auth, "tenant_access_token", fail_tenant)

    try:
        get_feishu_access_token(
            {
                "app_id": "cli_test",
                "app_secret": seal_secret("secret"),
                "token_cache": {},
            },
            require_user_oauth=True,
        )
    except feishu_auth.FeishuAuthError as exc:
        assert "user OAuth is required" in str(exc)
    else:
        raise AssertionError("FeishuAuthError was not raised")


def test_exchange_authorization_code_uses_oauth_token_endpoint(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "code": 0,
                "data": {
                    "access_token": "fresh-user-token",
                    "expires_in": 7200,
                    "refresh_token": "fresh-refresh-token",
                    "refresh_token_expires_in": 604800,
                },
            }

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(feishu_auth.httpx, "post", fake_post)

    data = exchange_authorization_code(
        {"app_id": "cli_test", "app_secret": seal_secret("secret")},
        "auth-code",
        "https://agentops.example/api/settings/feishu/oauth/callback",
    )

    assert captured["url"].endswith("/authen/v2/oauth/token")
    assert captured["json"]["grant_type"] == "authorization_code"
    assert captured["json"]["client_id"] == "cli_test"
    assert captured["json"]["client_secret"] == "secret"
    assert captured["json"]["code"] == "auth-code"
    assert data["access_token"] == "fresh-user-token"
