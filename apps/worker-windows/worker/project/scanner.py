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
