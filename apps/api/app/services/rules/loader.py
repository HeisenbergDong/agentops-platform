from pathlib import Path

from app.core.config import settings


class RuleLoader:
    def __init__(self, rules_dir: Path | None = None):
        self.rules_dir = rules_dir or settings.rules_dir

    def list_rules(self) -> list[dict]:
        return [
            {"name": path.name, "path": str(path), "size": path.stat().st_size}
            for path in sorted(self.rules_dir.glob("*"))
            if path.is_file()
        ]

    def read_rule(self, name: str) -> str:
        path = self.rules_dir / name
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(name)
        return path.read_text(encoding="utf-8")

    def read_many(self, names: list[str]) -> dict[str, str]:
        return {name: self.read_rule(name) for name in names}
