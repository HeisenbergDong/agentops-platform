from pathlib import Path

import pytest

from worker.safety.path_guard import assert_within_root


def test_path_guard_allows_child(tmp_path: Path):
    child = tmp_path / "project"
    child.mkdir()
    assert_within_root(child, tmp_path)


def test_path_guard_rejects_outside(tmp_path: Path):
    with pytest.raises(ValueError):
        assert_within_root(Path("C:/Windows"), tmp_path)
