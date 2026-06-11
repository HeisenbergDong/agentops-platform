from __future__ import annotations

from pathlib import Path
from typing import Any

from worker.safety.path_guard import assert_within_root


def ensure_project_workspace(root: Path, workspace_path: Path, payload: dict[str, Any]) -> dict:
    assert_within_root(workspace_path, root)
    workspace_path.mkdir(parents=True, exist_ok=True)
    project_name = str(payload.get("project_name") or payload.get("project_slug") or workspace_path.name).strip()
    directions = payload.get("directions") if isinstance(payload.get("directions"), list) else []
    topic = "\n".join(str(item).strip() for item in directions if str(item).strip())
    readme = workspace_path / "README.md"
    if not readme.exists():
        title = project_name or workspace_path.name
        body = topic or str(payload.get("prompt") or "").strip()[:600]
        readme.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    return {
        "project_name": project_name or workspace_path.name,
        "workspace_path": str(workspace_path),
        "readme_created": readme.exists(),
    }
