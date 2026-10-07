"""The graph suite's `tracked_files` helper excludes untracked files without losing reach."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.tracked import tracked_files


def _is_py(rel: str) -> bool:
    return rel.endswith(".py")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)


def test_an_untracked_file_is_excluded_and_git_add_brings_it_back(tmp_path) -> None:
    """Three steps, in order: the stray is reachable by a tree walk (else the fixture is inert),
    absent while untracked, and present after `git add` (else "absent" could mean the helper
    stopped seeing the directory at all)."""
    repo = tmp_path / "repo"
    (repo / "eval").mkdir(parents=True)
    kept = repo / "eval" / "kept.py"
    kept.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "eval/kept.py")

    stray = repo / "eval" / "stray.py"
    stray.write_text("x = 2\n", encoding="utf-8")

    assert stray in set((repo / "eval").glob("*.py"))
    assert tracked_files(repo, _is_py) == [kept]

    _git(repo, "add", "eval/stray.py")
    assert tracked_files(repo, _is_py) == [kept, stray]
