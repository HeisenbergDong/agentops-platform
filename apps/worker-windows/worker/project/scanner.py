import json
from pathlib import Path


IGNORED_DIRS = {"node_modules", "dist", "build", "target", ".venv", "__pycache__", ".git"}


def changed_files(root: Path) -> list[str]:
    if not root.exists():
        return []
    files: list[str] = []
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        files.append(str(path.relative_to(root)))
    return files[:500]


def scan_project(root: Path) -> dict:
    files = changed_files(root)
    return {
        "status": "scanned",
        "root": str(root),
        "file_count": len(files),
        "files": files,
        "detected_stack": _detected_stack(root),
        "recommended_commands": _recommended_commands(root),
    }


def _detected_stack(root: Path) -> list[str]:
    stack: list[str] = []
    if (root / "package.json").exists():
        stack.append("node")
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "tests").exists():
        stack.append("python")
    if (root / "pom.xml").exists():
        stack.append("maven")
    if (root / "go.mod").exists():
        stack.append("go")
    return stack


def _recommended_commands(root: Path) -> list[list[str]]:
    commands: list[list[str]] = []
    package_json = root / "package.json"
    if package_json.exists():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
        except Exception:
            scripts = {}
        if isinstance(scripts, dict):
            if "test" in scripts:
                commands.append(["npm", "test"])
            if "build" in scripts:
                commands.append(["npm", "run", "build"])
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "tests").exists():
        commands.append(["python", "-m", "pytest"])
    if (root / "pom.xml").exists():
        commands.append(["mvn", "test"])
    if (root / "go.mod").exists():
        commands.append(["go", "test", "./..."])
    return commands[:4]
