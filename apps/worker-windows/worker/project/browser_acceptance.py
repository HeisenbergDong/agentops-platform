import re
from html import unescape
from urllib.parse import urlparse

import httpx


LOCAL_BROWSER_HOSTS = {"localhost", "127.0.0.1", "::1"}


def run_browser_acceptance(project_path: str, url: str = "", timeout_seconds: float = 10.0) -> dict:
    normalized_url = _normalize_url(url)
    if not normalized_url:
        return {
            "status": "no_browser_evidence",
            "project_path": project_path,
            "message": "No browser acceptance URL was provided.",
        }

    parsed = urlparse(normalized_url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or host not in LOCAL_BROWSER_HOSTS:
        return {
            "status": "unsupported_url",
            "project_path": project_path,
            "url": normalized_url,
            "message": "Only local HTTP URLs are supported for automated browser acceptance.",
        }

    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.get(
                normalized_url,
                headers={"User-Agent": "agentops-worker-browser-acceptance/0.1"},
            )
    except httpx.HTTPError as exc:
        return {
            "status": "failed",
            "project_path": project_path,
            "url": normalized_url,
            "message": str(exc),
        }

    content = response.content or b""
    body = response.text or ""
    passed = 200 <= response.status_code < 400 and len(content) > 0
    return {
        "status": "passed" if passed else "failed",
        "project_path": project_path,
        "requested_url": normalized_url,
        "url": str(response.url),
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "content_length": len(content),
        "title": _extract_title(body),
        "message": "Browser acceptance URL responded with content." if passed else "Browser acceptance URL did not return usable content.",
    }


def _normalize_url(raw_url: str) -> str:
    url = str(raw_url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = f"http://{url}"
    return url


def _extract_title(body: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return unescape(title)[:200]
