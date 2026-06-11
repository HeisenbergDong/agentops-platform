import re
from urllib.parse import urlparse

import httpx

from app.core.secrets import open_secret


GITHUB_API = "https://api.github.com"


def ensure_github_repository(github_config: dict, project_name: str = "") -> dict:
    token = _open_token(github_config.get("token"))
    if not token:
        remote_url = build_project_remote_url(github_config, project_name, owner="")
        owner, repo = github_owner_repo(remote_url)
        return {"ok": False, "reason": "missing_github_token", "remote_url": remote_url, "owner": owner, "repo": repo}

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agentops-platform",
    }
    try:
        with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as client:
            viewer_response = client.get(f"{GITHUB_API}/user")
            if viewer_response.status_code >= 400:
                remote_url = build_project_remote_url(github_config, project_name, owner="")
                owner, repo = github_owner_repo(remote_url)
                return _github_error("github_viewer_failed", viewer_response, remote_url, owner, repo)
            login = str(viewer_response.json().get("login") or "")

            remote_url = build_project_remote_url(github_config, project_name, owner=login)
            owner, repo = github_owner_repo(remote_url)
            if not owner or not repo:
                return {"ok": False, "reason": "not_github_remote", "remote_url": remote_url}

            repo_response = client.get(f"{GITHUB_API}/repos/{owner}/{repo}")
            if repo_response.status_code == 200:
                return _with_deploy_key(
                    client,
                    {"ok": True, "existed": True, "owner": owner, "repo": repo, "remote_url": remote_url},
                    github_config,
                )
            if repo_response.status_code != 404:
                return _github_error("github_repo_check_failed", repo_response, remote_url, owner, repo)

            create_path = "/user/repos" if login.lower() == owner.lower() else f"/orgs/{owner}/repos"
            create_response = client.post(
                f"{GITHUB_API}{create_path}",
                json={"name": repo, "private": False, "auto_init": False},
            )
            if create_response.status_code in {200, 201}:
                return _with_deploy_key(
                    client,
                    {"ok": True, "created": True, "owner": owner, "repo": repo, "remote_url": remote_url},
                    github_config,
                )
            if create_response.status_code == 422:
                return _with_deploy_key(
                    client,
                    {"ok": True, "existed": True, "owner": owner, "repo": repo, "remote_url": remote_url},
                    github_config,
                )
            return _github_error("github_repo_create_failed", create_response, remote_url, owner, repo)
    except httpx.HTTPError as exc:
        remote_url = build_project_remote_url(github_config, project_name, owner="")
        owner, repo = github_owner_repo(remote_url)
        return {
            "ok": False,
            "reason": "github_request_failed",
            "detail": str(exc)[:500],
            "remote_url": remote_url,
            "owner": owner,
            "repo": repo,
        }


def build_project_remote_url(github_config: dict, project_name: str = "", owner: str = "") -> str:
    configured_remote = str(
        github_config.get("remote_url")
        or github_config.get("github_url")
        or github_config.get("default_remote_url")
        or ""
    ).strip()
    if configured_remote and not project_name:
        return configured_remote

    repo_name = _safe_repo_name(project_name or github_config.get("repo") or github_config.get("repository") or "")
    if not repo_name:
        return configured_remote

    configured_owner = str(github_config.get("owner") or github_config.get("repo_owner") or "").strip()
    base_owner = configured_owner or owner or github_owner_repo(configured_remote)[0]
    if not base_owner:
        return configured_remote

    protocol = str(github_config.get("remote_protocol") or "ssh").strip().lower()
    if protocol == "https":
        return f"https://github.com/{base_owner}/{repo_name}.git"
    return f"git@github.com:{base_owner}/{repo_name}.git"


def github_owner_repo(remote_url: str) -> tuple[str, str]:
    text = str(remote_url or "").strip()
    if text.startswith("git@github.com:"):
        path = text.split(":", 1)[1]
        owner, _, repo = path.partition("/")
    elif text.startswith("ssh://git@github.com/"):
        path = text.split("ssh://git@github.com/", 1)[1]
        owner, _, repo = path.partition("/")
    else:
        parsed = urlparse(text)
        if parsed.netloc.lower() != "github.com":
            return "", ""
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2:
            return "", ""
        owner, repo = parts[0], parts[1]
    repo = repo.removesuffix(".git")
    return owner.strip(), repo.strip()


def _open_token(value) -> str:
    try:
        return open_secret(str(value)) if value else ""
    except Exception:
        return ""


def _safe_repo_name(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip(".-_")
    return text[:80] or ""


def _with_deploy_key(client: httpx.Client, result: dict, github_config: dict) -> dict:
    pubkey = str(github_config.get("pubkey") or github_config.get("deploy_key") or "").strip()
    if not pubkey:
        return {**result, "deploy_key": {"skipped": True, "reason": "missing_pubkey"}}
    owner = str(result.get("owner") or "")
    repo = str(result.get("repo") or "")
    if not owner or not repo:
        return result
    title = str(github_config.get("deploy_key_title") or f"agentops-worker-{repo}").strip()
    response = client.post(
        f"{GITHUB_API}/repos/{owner}/{repo}/keys",
        json={"title": title, "key": pubkey, "read_only": False},
    )
    if response.status_code in {200, 201}:
        return {**result, "deploy_key": {"ok": True, "created": True, "title": title}}
    if response.status_code == 422:
        return {**result, "deploy_key": {"ok": True, "existed": True, "title": title}}
    key_error = _github_error("github_deploy_key_failed", response, str(result.get("remote_url") or ""), owner, repo)
    return {**result, "deploy_key": key_error}


def _github_error(reason: str, response: httpx.Response, remote_url: str, owner: str, repo: str) -> dict:
    detail = ""
    try:
        data = response.json()
        detail = str(data.get("message") or data)
    except Exception:
        detail = response.text
    return {
        "ok": False,
        "reason": f"{reason}:{response.status_code}",
        "detail": detail[:500],
        "remote_url": remote_url,
        "owner": owner,
        "repo": repo,
    }
