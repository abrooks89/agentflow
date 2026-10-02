"""The corpus of a first-party check is the GIT INDEX, never a walk of the working tree.

WHY THIS IS A MODULE AND NOT A HABIT (gh#175). A check whose corpus comes from `rglob` gives a
verdict about whatever happens to be lying in one developer's checkout, not about the commit.
The signature is the worst one this repo can emit: **`git status --porcelain` prints nothing and
the suite is red at the same time**. There is no git signal to diagnose from, so the natural next
move is to bisect your own work, and CI cannot reproduce any of it because CI checks out tracked
files only. gh#171 hit exactly that -- a gitignored `.egg-info/SOURCES.txt` and an `npm ci` tree,
both sitting under a SHIPPED skill, both invisible to `git status`, both scanned.

gh#171 fixed ONE check. gh#175 is the class, and the class was bigger than the filing: measured
against master, **11 test modules and 25 walk sites** derived a gate corpus from the working tree,
where the issue named five. Two of those modules had already grown a private copy of this
function. A shared module is what stops the third copy, and what gives the enumeration gate
(`skills/_shared/tests/test_corpus_from_index.py`) a single symbol to look for.

**MEMBERSHIP FROM THE INDEX, CONTENT FROM THE WORKING TREE.** That split is deliberate and worth
saying out loud, because it is a hybrid: a locally MODIFIED tracked file still moves a verdict,
which is what you want while editing, while an untracked file cannot enter a corpus at all. What
the callers give up is the file they have written but not yet `git add`ed -- and `add` is enough,
a commit is not needed.

**NO FALLBACK, ANYWHERE.** Every way git can fail to answer raises. A silent fall back to a tree
walk would reinstate the defect on precisely the machines where it is hardest to see, and would do
so with the suite green -- which is how the original stayed latent for months.

**DO NOT ADD A CACHE.** `tracked_paths` is called again after `git add` by tests that prove reach
did not narrow (the index moves inside a single test), so a memoised answer would make those
tests pass while proving nothing.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

#: The git executable, named rather than inlined so a caller can point at one that is not
#: installed and prove the loud-failure path without mocking anything this module owns.
GIT = "git"


def tracked_paths(root: Path, git: str | None = None) -> frozenset[str]:
    """Every path in the GIT INDEX at `root`, `root`-relative POSIX. The ONLY source.

    `git -C <root> ls-files` reports paths relative to `<root>`, NOT to the repository root --
    measured, because the two readings differ for every caller that points at a subdirectory
    (`skills/_shared/agentflow` answers `config.py`, not `skills/_shared/agentflow/config.py`).
    Anchor your own prefixes against the root you passed.
    """
    exe = GIT if git is None else git
    try:
        proc = subprocess.run(
            [exe, "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise AssertionError(
            f"this gate derives its corpus from the git index and could not run {exe!r}: {exc}. "
            "It does NOT fall back to walking the working tree -- a tree walk is what made the "
            "same commit produce different verdicts here and in CI. Install git, or run this "
            "suite from a checkout where it is on PATH."
        ) from exc
    if proc.returncode != 0:
        raise AssertionError(
            f"`{exe} ls-files` failed in {root} (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()!r}. This gate reads its corpus from "
            "the git index, so the root must be inside a git work tree; it deliberately has no "
            "working-tree fallback, because that fallback is the bug it was written to remove."
        )
    paths = frozenset(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p)
    if not paths:
        raise AssertionError(
            f"`{exe} ls-files` reported an EMPTY index under {root}. An empty corpus makes this "
            "gate pass by scanning nothing, which is indistinguishable from a gate that works -- "
            "so it fails here instead. Check that the root is inside the repository and that the "
            "checkout is not bare."
        )
    return paths


def tracked_files(
    root: Path,
    predicate: Callable[[str], bool] | None = None,
    git: str | None = None,
) -> list[Path]:
    """Absolute paths of the indexed files under `root` whose relative POSIX string passes.

    A PREDICATE OVER THE RELATIVE STRING, never a git pathspec. `git ls-files 'skills/*/SKILL.md'`
    looks like the direct translation of `SKILLS_ROOT.glob("*/SKILL.md")` and is not: git's
    pathspec globbing is fnmatch WITHOUT `FNM_PATHNAME`, so `*` crosses `/`. Measured on this
    repo, that pathspec returns 33 paths where the Python glob returns 21, silently pulling in
    every `skills/_vendored/*/SKILL.md`. Callers keep their own filters in Python, where the
    semantics are the ones they already had.

    Sorted, so a caller's failure message enumerates in a stable order.
    """
    rels = tracked_paths(root, git=git)
    keep = rels if predicate is None else (r for r in rels if predicate(r))
    return sorted(root / r for r in keep)


def only_tracked(root: Path, paths: Iterable[Path], git: str | None = None) -> list[Path]:
    """The subset of `paths` that the index at `root` knows about, sorted.

    For a call site whose existing walk is already correct in every respect except that it reads
    the filesystem: wrap the walk and the shape of the check is unchanged. A path outside `root`
    cannot be in that index and is dropped rather than raising, because a caller merging two
    roots would otherwise have to special-case one of them.

    RESIDUAL, AND IT IS THE ONE SILENT PATH HERE: the comparison is byte-exact, while `resolve()`
    canonicalises through the filesystem -- so on Windows it rewrites the caller's spelling to
    whatever the disk holds. Under `core.ignorecase`, a case-only rename leaves `git status`
    empty and drops that path from every corpus built through this function. Measured: renaming
    one skill directory that way narrows the corpus by one skill with no signal. Most callers
    have a floor guard that fires on the narrowing; `test_completion_truth_duty` does not.
    `_index_rel` in `test_deployed_skill_paths.py` carries the same residual and says to fold the
    case at the comparison; it is written here too so the note travels with the shared code.
    """
    rels = tracked_paths(root, git=git)
    out = []
    for p in paths:
        try:
            rel = p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if rel in rels:
            out.append(p)
    return sorted(out)
