import re
from urllib.parse import urlparse

import httpx

from app.core.secrets import open_secret


GITHUB_API = "https://api.github.com"
MAX_REVIEW_DIFF_CHARS = 60000
MAX_REVIEW_FILE_CHARS = 20000
MAX_REVIEW_FILES = 12


def ensure_github_repository(github_config: dict, project_name: str = "") -> dict:
    token = _open_token(github_config.get("token"))
    if not token:
        remote_url = build_project_remote_url(github_config, project_name, owner="")
        owner, repo = github_owner_repo(remote_url)
        return {
            "ok": False,
            "reason": "missing_github_token",
            "remote_url": remote_url,
            "owner": owner,
            "repo": repo,
            "credential_preflight": github_credential_preflight(github_config, remote_url),
        }

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
            credential_preflight = github_credential_preflight(github_config, remote_url)
            if not owner or not repo:
                return {"ok": False, "reason": "not_github_remote", "remote_url": remote_url, "credential_preflight": credential_preflight}

            repo_response = client.get(f"{GITHUB_API}/repos/{owner}/{repo}")
            if repo_response.status_code == 200:
                return _with_deploy_key(
                    client,
                    {"ok": True, "existed": True, "owner": owner, "repo": repo, "remote_url": remote_url, "credential_preflight": credential_preflight},
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
                    {"ok": True, "created": True, "owner": owner, "repo": repo, "remote_url": remote_url, "credential_preflight": credential_preflight},
                    github_config,
                )
            if create_response.status_code == 422:
                return _with_deploy_key(
                    client,
                    {"ok": True, "existed": True, "owner": owner, "repo": repo, "remote_url": remote_url, "credential_preflight": credential_preflight},
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


def github_credential_preflight(github_config: dict, remote_url: str = "") -> dict:
    url = str(remote_url or build_project_remote_url(github_config)).strip()
    protocol = "ssh" if url.startswith(("git@github.com:", "ssh://git@github.com/")) else "https" if url.startswith("https://") else "unknown"
    has_token = bool(_open_token(github_config.get("token")))
    has_pubkey = bool(str(github_config.get("pubkey") or github_config.get("deploy_key") or "").strip())
    if protocol == "ssh":
        return {
            "protocol": "ssh",
            "has_api_token": has_token,
            "has_deploy_key": has_pubkey,
            "ok": has_token and has_pubkey,
            "warning": "" if has_pubkey else "SSH remote selected but no deploy public key is configured.",
        }
    if protocol == "https":
        return {
            "protocol": "https",
            "has_api_token": has_token,
            "has_deploy_key": has_pubkey,
            "ok": has_token,
            "warning": "" if has_token else "HTTPS remote selected but no GitHub token is configured.",
        }
    return {
        "protocol": protocol,
        "has_api_token": has_token,
        "has_deploy_key": has_pubkey,
        "ok": False,
        "warning": "Remote URL is not a supported GitHub SSH or HTTPS remote.",
    }


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


def fetch_github_review_snapshot(github_config: dict, git_data: dict, prompt: str = "") -> dict:
    token = _open_token(github_config.get("token"))
    commit_sha = str(git_data.get("commit_sha") or "").strip()
    remote_url = str(git_data.get("remote_url") or git_data.get("github_url") or git_data.get("repository_url") or "").strip()
    owner, repo = github_owner_repo(remote_url)
    if not token:
        return {"ok": False, "reason": "missing_github_token", "commit_sha": commit_sha, "remote_url": remote_url}
    if not owner or not repo or not commit_sha:
        return {
            "ok": False,
            "reason": "missing_github_commit_context",
            "owner": owner,
            "repo": repo,
            "commit_sha": commit_sha,
            "remote_url": remote_url,
        }

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agentops-platform",
    }
    try:
        with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as client:
            commit_response = client.get(f"{GITHUB_API}/repos/{owner}/{repo}/commits/{commit_sha}")
            if commit_response.status_code >= 400:
                return _github_error("github_commit_fetch_failed", commit_response, remote_url, owner, repo)
            commit = commit_response.json()
            files = commit.get("files") if isinstance(commit.get("files"), list) else []
            selected = _select_review_files(files, prompt)
            contents = []
            for file_item in selected:
                path = str(file_item.get("filename") or "").strip()
                if not path or str(file_item.get("status") or "") == "removed":
                    continue
                content = _fetch_file_at_commit(client, owner, repo, path, commit_sha)
                if content:
                    contents.append({"path": path, "content": content[:MAX_REVIEW_FILE_CHARS]})
            return {
                "ok": True,
                "owner": owner,
                "repo": repo,
                "remote_url": remote_url,
                "commit_sha": commit_sha,
                "commit_url": str(commit.get("html_url") or _commit_url(remote_url, commit_sha)),
                "message": str((commit.get("commit") or {}).get("message") or ""),
                "changed_files": [str(item.get("filename") or "") for item in files if isinstance(item, dict) and item.get("filename")],
                "diff": _commit_diff(files),
                "selected_files": contents,
            }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "reason": "github_review_snapshot_request_failed",
            "detail": str(exc)[:500],
            "owner": owner,
            "repo": repo,
            "commit_sha": commit_sha,
            "remote_url": remote_url,
        }


def _select_review_files(files: list, prompt: str) -> list[dict]:
    if not files:
        return []
    keywords = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", str(prompt or ""))
        if len(token) >= 2
    }

    def score(item: dict) -> tuple[int, int, str]:
        path = str(item.get("filename") or "")
        suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        source_score = 3 if suffix in {"tsx", "jsx", "ts", "js", "vue", "py", "go", "java", "html", "css"} else 1
        keyword_score = sum(1 for keyword in keywords if keyword and keyword in path.lower())
        patch_size = len(str(item.get("patch") or ""))
        return (keyword_score, source_score, -patch_size, path)

    clean = [item for item in files if isinstance(item, dict) and str(item.get("filename") or "").strip()]
    return sorted(clean, key=score, reverse=True)[:MAX_REVIEW_FILES]


def _fetch_file_at_commit(client: httpx.Client, owner: str, repo: str, path: str, commit_sha: str) -> str:
    response = client.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
        params={"ref": commit_sha},
    )
    if response.status_code >= 400:
        return ""
    data = response.json()
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return ""
    try:
        import base64

        return base64.b64decode(str(data.get("content") or ""), validate=False).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _commit_diff(files: list) -> str:
    chunks: list[str] = []
    budget = MAX_REVIEW_DIFF_CHARS
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        patch = str(item.get("patch") or "").strip()
        if not filename or not patch:
            continue
        chunk = f"\n--- {filename} ({item.get('status') or 'modified'}) ---\n{patch}\n"
        if len(chunk) > budget:
            chunks.append(chunk[:budget])
            break
        chunks.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    return "".join(chunks).strip()


def _commit_url(remote_url: str, commit_sha: str) -> str:
    normalized = _web_repo_url(remote_url)
    return f"{normalized}/commit/{commit_sha}" if normalized and commit_sha else normalized


def _web_repo_url(remote_url: str) -> str:
    owner, repo = github_owner_repo(remote_url)
    return f"https://github.com/{owner}/{repo}" if owner and repo else ""


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
