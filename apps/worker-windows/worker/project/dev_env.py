import json
import os
import re
from pathlib import Path


TOOL_KEYS = ("node", "npm", "python", "maven", "go", "jdk")
PATH_KEYS = ("path", "home", "java_home", "maven_home", "goroot", "gopath")


def command_environment(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    config = load_dev_env(root)
    path_entries = _tool_path_entries(config)
    if path_entries:
        current_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join([*path_entries, current_path]) if current_path else os.pathsep.join(path_entries)
    _set_if_present(env, "JAVA_HOME", config, "jdk", ("java_home", "home"))
    _set_if_present(env, "MAVEN_HOME", config, "maven", ("maven_home", "home"))
    _set_if_present(env, "GOROOT", config, "go", ("goroot", "home"))
    _set_if_present(env, "GOPATH", config, "go", ("gopath",))
    return env


def resolve_tool(root: Path, name: str) -> str:
    config = load_dev_env(root)
    tool = config.get(name)
    if isinstance(tool, dict):
        path = str(tool.get("path") or "").strip()
        if path and Path(path).exists():
            return path
    return name


def load_dev_env(root: Path) -> dict:
    path = _find_dev_env(root)
    if not path:
        return {}
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return _parse_partial_dev_env(text)


def _find_dev_env(root: Path) -> Path | None:
    current = root if root.is_dir() else root.parent
    for candidate in (current, *current.parents):
        path = candidate / ".dev-env.json"
        if path.exists():
            return path
    return None


def _parse_partial_dev_env(text: str) -> dict:
    result: dict[str, dict[str, str]] = {}
    for tool in TOOL_KEYS:
        body_match = re.search(rf'"{re.escape(tool)}"\s*:\s*\{{(?P<body>.*?)\n\s*\}}', text, re.DOTALL)
        if not body_match:
            continue
        values: dict[str, str] = {}
        body = body_match.group("body")
        for key in PATH_KEYS:
            value_match = re.search(rf'"{re.escape(key)}"\s*:\s*"(?P<value>[^"]+)"', body)
            if value_match:
                values[key] = value_match.group("value").replace("\\\\", "\\")
        if values:
            result[tool] = values
    return result


def _tool_path_entries(config: dict) -> list[str]:
    entries: list[str] = []
    for tool_name in TOOL_KEYS:
        tool = config.get(tool_name)
        if not isinstance(tool, dict):
            continue
        for key in PATH_KEYS:
            raw_value = str(tool.get(key) or "").strip()
            if not raw_value:
                continue
            path = Path(raw_value)
            candidate = path.parent if path.suffix else path / "bin"
            if candidate.exists():
                entries.append(str(candidate))
            if path.exists() and path.is_dir():
                entries.append(str(path))
    return _dedupe(entries)


def _set_if_present(env: dict[str, str], env_key: str, config: dict, tool_name: str, keys: tuple[str, ...]) -> None:
    tool = config.get(tool_name)
    if not isinstance(tool, dict):
        return
    for key in keys:
        value = str(tool.get(key) or "").strip()
        if value:
            env[env_key] = value
            return


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result
