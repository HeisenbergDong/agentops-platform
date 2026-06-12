from __future__ import annotations

import re


BUILD_ERROR_PATTERNS = (
    ("missing_module", r"(Module not found|Cannot find module|Failed to resolve import|ERR_MODULE_NOT_FOUND)"),
    ("syntax_error", r"(SyntaxError|Unexpected token|Unexpected end of input|Transform failed)"),
    ("type_error", r"(TypeError|TS\d{4}:|Property .+ does not exist|Type .+ is not assignable)"),
    ("lint_error", r"(ESLint|eslint|prettier|ruff|flake8|mypy)"),
    ("test_failure", r"(FAIL|FAILED|AssertionError|Expected .+ Received|Tests? failed)"),
    ("dependency_install", r"(npm ERR!|pnpm ERR!|yarn error|Could not resolve dependency|ERESOLVE|ENOTFOUND)"),
    ("port_in_use", r"(EADDRINUSE|address already in use|port .* already in use)"),
    ("permission", r"(EACCES|EPERM|Permission denied|Access is denied)"),
)

SOURCE_LOCATION_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[^:\n\r\t ]+\.(?:vue|tsx?|jsx?|css|html|py|go|java|json|yaml|yml))"
    r"(?::(?P<line>\d+))?(?::(?P<column>\d+))?",
    re.IGNORECASE,
)


def summarize_command_result(command: list[str], returncode: int | None, stdout: str = "", stderr: str = "") -> dict:
    combined = "\n".join(part for part in [stderr, stdout] if part)
    error_type = classify_build_error(combined) if returncode not in {0, None} else ""
    locations = extract_source_locations(combined)
    return {
        "command": command,
        "passed": returncode == 0,
        "returncode": returncode,
        "error_type": error_type,
        "primary_location": locations[0] if locations else {},
        "locations": locations[:8],
        "summary": command_summary_message(command, returncode, error_type, locations, combined),
    }


def classify_build_error(text: str) -> str:
    haystack = str(text or "")
    for error_type, pattern in BUILD_ERROR_PATTERNS:
        if re.search(pattern, haystack, flags=re.IGNORECASE):
            return error_type
    return "command_failed" if haystack.strip() else "command_failed_no_output"


def extract_source_locations(text: str) -> list[dict[str, object]]:
    seen: set[tuple[str, int, int]] = set()
    locations: list[dict[str, object]] = []
    for match in SOURCE_LOCATION_RE.finditer(str(text or "")):
        path = _clean_path(match.group("path"))
        if not path or _looks_like_package_path(path):
            continue
        line = _int_or_zero(match.group("line"))
        column = _int_or_zero(match.group("column"))
        key = (path, line, column)
        if key in seen:
            continue
        seen.add(key)
        locations.append({"path": path, "line": line, "column": column})
        if len(locations) >= 20:
            break
    return locations


def command_summary_message(
    command: list[str],
    returncode: int | None,
    error_type: str,
    locations: list[dict[str, object]],
    output: str,
) -> str:
    command_text = " ".join(command)
    if returncode == 0:
        return f"Command passed: {command_text}"
    location = locations[0] if locations else {}
    location_text = ""
    if location:
        location_text = f" at {location.get('path')}"
        if location.get("line"):
            location_text += f":{location.get('line')}"
    first_line = first_relevant_error_line(output)
    detail = f" ({first_line})" if first_line else ""
    return f"Command failed ({error_type or 'command_failed'}){location_text}: {command_text}{detail}"


def first_relevant_error_line(text: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.search(
            r"(error|failed|fail|exception|traceback|cannot|module not found|syntax|typeerror|referenceerror)",
            stripped,
            flags=re.IGNORECASE,
        ):
            return stripped[:240]
    return ""


def _clean_path(value: str) -> str:
    text = str(value or "").strip().strip("'\"`()[]{}")
    return text.replace("\\", "/")


def _looks_like_package_path(path: str) -> bool:
    lowered = path.lower()
    return "node_modules/" in lowered or "/site-packages/" in lowered or lowered.startswith("http")


def _int_or_zero(value: str | None) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
