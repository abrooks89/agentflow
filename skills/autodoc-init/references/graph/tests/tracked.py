"""A test corpus comes from the GIT INDEX, never from a walk of the working tree (gh#410).

A corpus from `glob` is a verdict about whatever is lying in one checkout, not about the commit:
one untracked file can redden the suite while `git status` explains nothing, and CI (which checks
out tracked files only) cannot reproduce it.

Stdlib-only on purpose. This mirrors `agentflow.tracked_paths.tracked_files` -- same signature
minus the `git` override, same semantics -- rather than importing it, because the graph package
keeps its own dependency policy and its CI job never installs agentflow.

The predicate is a Python callable over the root-relative POSIX string, never a git pathspec:
git's pathspec `*` crosses `/`, so `eval/*.py` would also match `eval/sub/x.py`.

No fallback: every way git can fail to answer raises, because a silent fall back to a tree walk
reinstates the defect exactly where it is hardest to see.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path


def tracked_files(root: Path, predicate: Callable[[str], bool] | None = None) -> list[Path]:
    """Absolute, sorted paths of the indexed files under `root` whose relative path passes.

    `git -C <root> ls-files` answers relative to `root`, not to the repository root.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False,
        )
    except OSError as exc:
        raise AssertionError(
            f"could not run git to read the index at {root}: {exc}. This corpus comes from the "
            "git index and deliberately has no working-tree fallback."
        ) from exc
    if proc.returncode != 0:
        raise AssertionError(
            f"`git ls-files` failed in {root} (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()!r}"
        )
    rels = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
    if not rels:
        raise AssertionError(
            f"`git ls-files` reported an EMPTY index under {root} - an empty corpus passes by "
            "scanning nothing, so it fails here instead"
        )
    keep = rels if predicate is None else [r for r in rels if predicate(r)]
    return sorted(root / r for r in keep)
