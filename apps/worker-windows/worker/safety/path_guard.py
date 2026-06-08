from pathlib import Path


def assert_within_root(candidate: Path, root: Path) -> None:
    resolved_candidate = candidate.resolve()
    resolved_root = root.resolve()
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        raise ValueError(f"Path is outside allowed root: {resolved_candidate}")
