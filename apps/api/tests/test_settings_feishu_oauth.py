from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import settings as settings_api
from app.api.settings import FeishuOAuthBeginRequest, begin_feishu_oauth, feishu_oauth_callback
from app.db.models import User, UserConfig
from app.db.session import Base
from app.services.user_settings import load_user_settings, save_user_settings, safe_open_secret


def test_feishu_oauth_begin_and_callback_cache_user_token(monkeypatch):
    db = _test_session()
    user = _create_user(db, "user1")
    save_user_settings(
        db,
        user.id,
        {
            "feishu": {
                "app_id": "cli_test",
                "app_secret": "secret",
                "write_url": "https://bcnrsnl3m9wk.feishu.cn/base/app_token?table=tbl1&view=vew1",
            }
        },
    )
    db.commit()

    request = SimpleNamespace(url_for=lambda name: "https://agentops.example/api/settings/feishu/oauth/callback")
    begin = begin_feishu_oauth(
        FeishuOAuthBeginRequest(redirect_uri="https://agentops.example/api/settings/feishu/oauth/callback"),
        request,
        user,
        db,
    )

    assert begin["status"] == "authorization_required"
    assert "app_id=cli_test" in begin["authorize_url"]
    assert "redirect_uri=" in begin["authorize_url"]

    config = db.scalar(select(UserConfig).where(UserConfig.user_id == user.id, UserConfig.category == "feishu"))
    state = config.data["token_cache"]["oauth_state"]

    monkeypatch.setattr(
        settings_api,
        "exchange_authorization_code",
        lambda feishu, code, redirect_uri: {
            "access_token": "fresh-user-token",
            "expires_in": 7200,
            "refresh_token": "fresh-refresh-token",
            "refresh_token_expires_in": 604800,
            "scope": "offline_access bitable:app",
        },
    )
    monkeypatch.setattr(
        settings_api,
        "discover_feishu_resources",
        lambda feishu, require_user_oauth=False: {
            "discovered_resources": {"auth_mode": "user_oauth", "message": "ok"},
            "token_cache": feishu["token_cache"],
        },
    )

    response = feishu_oauth_callback(code="auth-code", state=state, db=db)

    assert response.status_code == 200
    settings = load_user_settings(db, user.id)
    cache = settings["feishu"]["token_cache"]
    assert safe_open_secret(cache["user_access_token"]) == "fresh-user-token"
    assert safe_open_secret(cache["refresh_token"]) == "fresh-refresh-token"
    assert "oauth_state" not in cache
    assert settings["feishu"]["discovered_resources"]["auth_mode"] == "user_oauth"


def _test_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    return Session()


def _create_user(db, user_id: str) -> User:
    user = User(id=user_id, email=f"{user_id}@example.com", display_name=user_id, password_hash="hash")
    db.add(user)
    db.commit()
    return user
