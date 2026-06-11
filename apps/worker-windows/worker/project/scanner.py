import json
from pathlib import Path

from worker.project.product_review import review_project_static


IGNORED_DIRS = {"node_modules", "dist", "build", "target", ".venv", "__pycache__", ".git", ".npm-cache"}


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


def scan_project(root: Path, prompt: str = "", changed_file_list=None) -> dict:
    files = changed_files(root)
    project_root = _primary_project_root(root)
    product_review = review_project_static(project_root, prompt=prompt, changed_files=changed_file_list)
    return {
        "status": "scanned",
        "root": str(root),
        "project_root": str(project_root),
        "recommended_command_cwd": str(project_root),
        "file_count": len(files),
        "files": files,
        "detected_stack": _detected_stack(project_root),
        "recommended_commands": _recommended_commands(project_root),
        "product_review": product_review,
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


def _primary_project_root(root: Path) -> Path:
    if (root / "package.json").exists() or _detected_stack(root):
        return root
    candidates = _package_roots(root)
    if candidates:
        return sorted(
            candidates,
            key=lambda item: (-_project_activity_time(item), -_package_score(item), len(item.relative_to(root).parts), str(item)),
        )[0]
    return root


def _package_roots(root: Path) -> list[Path]:
    if not root.exists():
        return []
    results: list[Path] = []
    for path in root.rglob("package.json"):
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            relative_parts = path.parts
        if any(part in IGNORED_DIRS for part in relative_parts):
            continue
        results.append(path.parent)
    return results


def _package_score(root: Path) -> int:
    scripts = _package_scripts(root)
    score = 0
    if "test" in scripts:
        score += 30
    if "build" in scripts:
        score += 40
    if "dev" in scripts:
        score += 15
    if (root / "package-lock.json").exists():
        score += 20
    if (root / "node_modules").exists():
        score += 20
    return score


def _package_scripts(root: Path) -> dict:
    package_json = root / "package.json"
    if not package_json.exists():
        return {}
    try:
        scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
    except Exception:
        return {}
    return scripts if isinstance(scripts, dict) else {}


def _project_activity_time(root: Path) -> float:
    latest = 0.0
    for candidate in (root, root / "package.json"):
        try:
            latest = max(latest, candidate.stat().st_mtime)
        except OSError:
            pass
    try:
        children = list(root.iterdir())
    except OSError:
        return latest
    for child in children:
        if child.name in IGNORED_DIRS:
            continue
        try:
            latest = max(latest, child.stat().st_mtime)
        except OSError:
            pass
    return latest
