import json
import re
import subprocess
import time
from html import unescape
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse, urlunparse

import httpx

from worker.project.dev_env import command_environment, resolve_tool


LOCAL_BROWSER_HOSTS = {"localhost", "127.0.0.1", "::1"}
IGNORED_DIRS = {"node_modules", "dist", "build", "target", ".venv", "__pycache__", ".git", ".npm-cache"}
DEFAULT_START_TIMEOUT_SECONDS = 45.0
COMMON_SPA_PATHS = ("/", "/jobs", "/dashboard", "/candidates", "/interviews", "/login")
LOCAL_URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|\[::1\]):(?P<port>\d{2,5})(?P<path>/[^\s'\"<>)]*)?")
LOCAL_PORT_HINT_RE = re.compile(r"(?:localhost|127\.0\.0\.1|port|端口|运行在|running).{0,30}?(?P<port>51\d{2})", re.IGNORECASE)


def run_browser_acceptance(
    project_path: str,
    url: str = "",
    timeout_seconds: float = 10.0,
    cancellation_check: Callable[[], None] | None = None,
) -> dict:
    if cancellation_check:
        cancellation_check()
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

    root = Path(project_path)
    initial = _fetch_best_url(
        project_path,
        _candidate_urls(root, normalized_url, include_neighbor_ports=False),
        timeout_seconds,
        cancellation_check,
    )
    if initial["status"] == "passed":
        return initial

    if cancellation_check:
        cancellation_check()
    start_result = _start_local_dev_server(root, parsed)
    if start_result["status"] not in {"started", "already_running"}:
        initial["auto_start"] = start_result
        return initial

    deadline = time.monotonic() + max(DEFAULT_START_TIMEOUT_SECONDS, timeout_seconds)
    latest = initial
    while time.monotonic() < deadline:
        _sleep_with_cancellation(1.5, cancellation_check)
        latest = _fetch_best_url(
            project_path,
            _candidate_urls(root, normalized_url, start_result, include_neighbor_ports=True),
            timeout_seconds,
            cancellation_check,
        )
        if latest["status"] == "passed":
            latest["auto_start"] = start_result
            latest["message"] = "Browser acceptance URL responded with content after starting the local dev server."
            return latest

    latest["auto_start"] = start_result
    latest["message"] = "Browser acceptance URL did not become usable after starting the local dev server."
    return latest


def _fetch_best_url(
    project_path: str,
    candidate_urls: list[str],
    timeout_seconds: float,
    cancellation_check: Callable[[], None] | None = None,
) -> dict:
    best: dict | None = None
    best_score = -1
    tried: list[str] = []
    for candidate in candidate_urls:
        if cancellation_check:
            cancellation_check()
        if candidate in tried:
            continue
        tried.append(candidate)
        result = _fetch_url(project_path, candidate, timeout_seconds, cancellation_check)
        result["candidate_urls_tried"] = list(tried)
        if result["status"] == "passed":
            return result
        score = _acceptance_score(result)
        if best is None or score > best_score:
            best = result
            best_score = score
    if best is not None:
        best["candidate_urls_tried"] = tried
        return best
    return {
        "status": "failed",
        "project_path": project_path,
        "url": "",
        "candidate_urls_tried": tried,
        "message": "No browser acceptance candidate URL could be checked.",
    }


def _fetch_url(
    project_path: str,
    normalized_url: str,
    timeout_seconds: float,
    cancellation_check: Callable[[], None] | None = None,
) -> dict:
    if cancellation_check:
        cancellation_check()
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, trust_env=False) as client:
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
    inspection = _inspect_html(body, response.headers.get("content-type", ""))
    passed = 200 <= response.status_code < 400 and len(content) > 0 and not inspection["issues"]
    runtime_diagnostics = {
        "passed": passed,
        "http_ok": 200 <= response.status_code < 400,
        "page_has_content": len(content) > 0,
        "blocking_issues": inspection["issues"],
        "warnings": inspection["warnings"],
        "error_markers": inspection.get("error_markers", []),
        "interaction": inspection.get("interaction", {}),
    }
    return {
        "status": "passed" if passed else "failed",
        "project_path": project_path,
        "requested_url": normalized_url,
        "url": str(response.url),
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "content_length": len(content),
        "title": inspection["title"],
        "inspection": inspection,
        "runtime_diagnostics": runtime_diagnostics,
        "message": "Browser acceptance URL responded with usable content." if passed else _acceptance_failure_message(response.status_code, inspection),
    }


def _sleep_with_cancellation(seconds: float, cancellation_check: Callable[[], None] | None) -> None:
    if not cancellation_check:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        cancellation_check()
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    cancellation_check()


def _start_local_dev_server(root: Path, parsed_url) -> dict:
    package_root, script = _find_dev_server_script(root, parsed_url.port)
    if not package_root or not script:
        return {
            "status": "not_available",
            "message": "No local package.json dev script was found for browser acceptance.",
            "project_path": str(root),
        }

    command = [resolve_tool(root, "npm"), "run", script]
    script_text = _package_scripts(package_root).get(script, "")
    if parsed_url.port and "vite" in script_text.lower():
        command.extend(["--", "--host", "127.0.0.1", "--port", str(parsed_url.port)])

    log_dir = package_root / ".agentops"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "browser-acceptance-server.log"
    creationflags = 0
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW

    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            cwd=package_root,
            env=command_environment(root),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    _write_started_process_metadata(log_dir, process.pid, package_root, command)

    return {
        "status": "started",
        "pid": process.pid,
        "cwd": str(package_root),
        "command": [str(item) for item in command],
        "log_path": str(log_path),
    }


def _write_started_process_metadata(log_dir: Path, pid: int, cwd: Path, command: list[str]) -> None:
    try:
        (log_dir / "browser-acceptance-server.json").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "cwd": str(cwd),
                    "command": [str(item) for item in command],
                    "created_at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        return


def _candidate_urls(
    root: Path,
    normalized_url: str,
    start_result: dict | None = None,
    *,
    include_neighbor_ports: bool = True,
) -> list[str]:
    parsed = urlparse(normalized_url)
    urls: list[str] = []
    log_urls = _urls_from_project_logs(root)
    if isinstance(start_result, dict):
        log_path = str(start_result.get("log_path") or "").strip()
        if log_path:
            log_urls = _urls_from_log_file(Path(log_path)) + log_urls
    urls.extend(_expand_urls_with_common_paths(log_urls))
    urls.append(normalized_url)
    if include_neighbor_ports and parsed.port:
        for port in range(parsed.port, parsed.port + 8):
            for path in _candidate_paths(parsed.path):
                urls.append(_replace_port_path(parsed, port, path))
    return list(dict.fromkeys(url for url in urls if url))


def _candidate_paths(path: str) -> list[str]:
    values = [path or "/"]
    values.extend(COMMON_SPA_PATHS)
    normalized: list[str] = []
    for value in values:
        clean = "/" + str(value or "/").lstrip("/")
        if clean not in normalized:
            normalized.append(clean)
    return normalized


def _replace_port_path(parsed, port: int, path: str) -> str:
    host = parsed.hostname or "localhost"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}"
    return urlunparse((parsed.scheme or "http", netloc, path or "/", "", "", ""))


def _expand_urls_with_common_paths(urls: list[str]) -> list[str]:
    result: list[str] = []
    for url in urls:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname or not parsed.port:
            continue
        result.append(url)
        for path in _candidate_paths(parsed.path):
            result.append(_replace_port_path(parsed, parsed.port, path))
    return list(dict.fromkeys(result))


def _urls_from_project_logs(root: Path) -> list[str]:
    if not root.exists():
        return []
    candidates: list[Path] = []
    for relative in (".agentops/browser-acceptance-server.log", "stdout.txt", "stderr.txt"):
        path = root / relative
        if path.exists():
            candidates.append(path)
    try:
        for path in root.rglob("*.log"):
            if any(part in IGNORED_DIRS for part in path.relative_to(root).parts):
                continue
            candidates.append(path)
    except OSError:
        pass
    unique = list({str(path): path for path in candidates}.values())
    unique.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    urls: list[str] = []
    for path in unique[:8]:
        urls.extend(_urls_from_log_file(path))
    return list(dict.fromkeys(urls))


def _urls_from_log_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[-20000:]
    except OSError:
        return []
    urls: list[str] = []
    for match in LOCAL_URL_RE.finditer(text):
        port = match.group("port")
        path_part = match.group("path") or "/"
        urls.append(f"http://localhost:{port}{path_part}")
    for match in LOCAL_PORT_HINT_RE.finditer(text):
        urls.append(f"http://localhost:{match.group('port')}/")
    return list(dict.fromkeys(urls))


def _acceptance_score(result: dict) -> int:
    status = str(result.get("status") or "")
    score = 1000 if status == "passed" else 0
    try:
        http_status = int(result.get("http_status") or 0)
    except (TypeError, ValueError):
        http_status = 0
    if 200 <= http_status < 400:
        score += 100
    inspection = result.get("inspection") if isinstance(result.get("inspection"), dict) else {}
    score += min(int(inspection.get("text_length") or 0), 500)
    score += int(inspection.get("interactive_count") or 0) * 25
    score -= len(inspection.get("issues") or []) * 200
    return score


def _find_dev_server_script(root: Path, target_port: int | None) -> tuple[Path | None, str]:
    candidates: list[tuple[int, Path, str]] = []
    for package_root in _package_roots(root):
        scripts = _package_scripts(package_root)
        for script_name, script_value in scripts.items():
            if not _is_dev_script(script_name, script_value):
                continue
            score = _dev_script_score(package_root, script_name, script_value, target_port)
            candidates.append((score, package_root, script_name))
    if not candidates:
        return None, ""
    _, package_root, script_name = sorted(candidates, key=lambda item: (-item[0], len(item[1].parts), str(item[1])))[0]
    return package_root, script_name


def _package_roots(root: Path) -> list[Path]:
    if not root.exists():
        return []
    roots: list[Path] = []
    if (root / "package.json").exists():
        roots.append(root)
    for path in root.rglob("package.json"):
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            relative_parts = path.parts
        if any(part in IGNORED_DIRS for part in relative_parts):
            continue
        if path.parent not in roots:
            roots.append(path.parent)
    return roots


def _package_scripts(root: Path) -> dict[str, str]:
    try:
        raw_scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {})
    except Exception:
        return {}
    if not isinstance(raw_scripts, dict):
        return {}
    return {str(key): str(value) for key, value in raw_scripts.items()}


def _is_dev_script(name: str, value: str) -> bool:
    lowered_name = name.lower()
    lowered_value = value.lower()
    return lowered_name in {"dev", "start", "serve", "preview"} or lowered_name.startswith("dev:") or "vite" in lowered_value


def _dev_script_score(root: Path, name: str, value: str, target_port: int | None) -> int:
    lowered = value.lower()
    score = 0
    if name == "dev":
        score += 40
    if "vite" in lowered:
        score += 50
    if target_port and str(target_port) in value:
        score += 30
    if (root / "node_modules").exists():
        score += 20
    return score


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


def _inspect_html(body: str, content_type: str = "") -> dict:
    text = str(body or "")
    is_html = "html" in str(content_type or "").lower() or bool(re.search(r"<html|<body|<div|<script", text, re.IGNORECASE))
    visible_text = _visible_text(text) if is_html else text.strip()
    issues: list[str] = []
    warnings: list[str] = []
    title = _extract_title(text)
    interactive_count = _interactive_count(text)
    error_markers = _error_markers(text)
    interaction = _interaction_summary(text)

    if not text.strip():
        issues.append("页面返回内容为空。")
    elif is_html and len(visible_text) < 12 and interactive_count == 0:
        issues.append("页面正文为空或接近空白，且没有可见交互入口。")
    if error_markers:
        issues.append("页面包含运行错误或构建错误信号：" + "；".join(error_markers[:3]))
    if is_html and interactive_count == 0:
        warnings.append("页面里没有检测到 button/input/select/textarea/link 等交互入口。")

    return {
        "title": title,
        "is_html": is_html,
        "text_length": len(visible_text),
        "text_sample": visible_text[:300],
        "interactive_count": interactive_count,
        "interaction": interaction,
        "error_markers": error_markers,
        "issues": issues,
        "warnings": warnings,
    }


def _visible_text(body: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", body, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return unescape(re.sub(r"\s+", " ", text)).strip()


def _interactive_count(body: str) -> int:
    patterns = [
        r"<button\b",
        r"<input\b",
        r"<select\b",
        r"<textarea\b",
        r"<a\b[^>]*\bhref\s*=",
        r"\brole\s*=\s*['\"]button['\"]",
        r"\bonclick\s*=",
    ]
    return sum(len(re.findall(pattern, body, flags=re.IGNORECASE)) for pattern in patterns)


def _interaction_summary(body: str) -> dict:
    tags = {
        "buttons": len(re.findall(r"<button\b", body, flags=re.IGNORECASE)),
        "inputs": len(re.findall(r"<input\b", body, flags=re.IGNORECASE)),
        "selects": len(re.findall(r"<select\b", body, flags=re.IGNORECASE)),
        "textareas": len(re.findall(r"<textarea\b", body, flags=re.IGNORECASE)),
        "links": len(re.findall(r"<a\b[^>]*\bhref\s*=", body, flags=re.IGNORECASE)),
        "click_handlers": len(re.findall(r"(@click|onclick|onClick)\s*=", body, flags=re.IGNORECASE)),
    }
    return {
        **tags,
        "total": sum(tags.values()),
        "button_labels": _button_labels(body)[:12],
        "has_form_control": bool(tags["inputs"] or tags["selects"] or tags["textareas"]),
        "has_click_path": bool(tags["buttons"] or tags["links"] or tags["click_handlers"]),
    }


def _button_labels(body: str) -> list[str]:
    labels: list[str] = []
    for match in re.finditer(r"<button\b[^>]*>([\s\S]*?)</button>", body, flags=re.IGNORECASE):
        label = _visible_text(match.group(1))
        if label:
            labels.append(label[:80])
    return labels


def _error_markers(body: str) -> list[str]:
    markers = []
    for pattern in (
        r"Internal Server Error",
        r"Module not found",
        r"Cannot (?:GET|POST|read|set)",
        r"ReferenceError[:\s]",
        r"TypeError[:\s]",
        r"SyntaxError[:\s]",
        r"Traceback \(most recent call last\)",
        r"Uncaught [A-Za-z]+Error",
        r"Vite Error",
    ):
        match = re.search(pattern, body, flags=re.IGNORECASE)
        if match:
            markers.append(match.group(0)[:120])
    return markers


def _acceptance_failure_message(status_code: int, inspection: dict) -> str:
    if inspection.get("issues"):
        return "Browser acceptance URL responded, but content inspection found blocking issues."
    if not (200 <= status_code < 400):
        return "Browser acceptance URL did not return a successful HTTP status."
    return "Browser acceptance URL did not return usable content."
