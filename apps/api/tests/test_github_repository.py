from app.services.github.repository import build_project_remote_url, github_credential_preflight


def test_github_credential_preflight_flags_ssh_without_deploy_key():
    remote_url = build_project_remote_url(
        {"owner": "acme", "remote_protocol": "ssh", "token": "token"},
        "demo-app",
    )

    result = github_credential_preflight({"owner": "acme", "remote_protocol": "ssh", "token": "token"}, remote_url)

    assert result["protocol"] == "ssh"
    assert result["has_api_token"] is True
    assert result["has_deploy_key"] is False
    assert result["ok"] is False
    assert "deploy public key" in result["warning"]


def test_github_credential_preflight_accepts_https_token():
    remote_url = build_project_remote_url(
        {"owner": "acme", "remote_protocol": "https", "token": "token"},
        "demo-app",
    )

    result = github_credential_preflight({"owner": "acme", "remote_protocol": "https", "token": "token"}, remote_url)

    assert result["protocol"] == "https"
    assert result["has_api_token"] is True
    assert result["ok"] is True
