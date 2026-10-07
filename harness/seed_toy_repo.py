#!/usr/bin/env python3
"""Reconstruct the harness toy-repo fixture from tracked seed files.

The toy-repo is a tiny git repo (its own ``.git``) used as the controlled
target for orchestrator/builder scenario recordings. It is gitignored from the
outer repo, so a fresh clone does not have it — this script rebuilds it
byte-identically from ``fixtures/toy-repo-seed/``:

    python harness/seed_toy_repo.py                         # create harness/toy-repo/
    python harness/seed_toy_repo.py --force                 # wipe + recreate
    python harness/seed_toy_repo.py --worktree-root <dir>   # name the worktree root

The seed commit is DETERMINISTIC: file contents are LF-normalized and the
commit uses a fixed author/committer/date, so every machine produces the same
seed sha. The script prints the sha and fails loudly if it drifts from
EXPECTED_SEED_SHA (meaning a seed fixture changed — update the constant and
harness/README.md together, deliberately).

Scenario B's orchestrator stops at Step 1.1 unless two more things exist (gh#1105), so the
seed makes both; neither adds a commit, so the seed sha does not move:

- an ``origin`` that resolves to the toy profile's ``host_ref``. It is a LOCAL bare repo at
  ``harness/toy-origin/<host_ref>.git`` (gitignored), reached by a ``file://`` URL, so the
  identity check passes with no network traffic and nothing is ever pushed anywhere real;
- the epic worktree ``<worktree_root>/<project>/<epic-branch>``, created with the one
  epic-worktree command (/project Step 5). ``<project>`` and ``<epic-branch>`` (the slug,
  as no registry row names a claim branch) come from ``fixtures/plan-single-task.md``.

``<worktree_root>`` is ``--worktree-root`` when given, else the machine's
``environment.md``. With neither (a fresh clone, CI) the repo and origin are still seeded
and the script says, on stderr, that the worktree was not.

Requires the ``agentflow`` package from ``skills/_shared`` to be importable: the
seed file list comes from the git index via ``agentflow.tracked_paths``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from agentflow.config import ConfigError, _load_first_yaml_block, load_environment
from agentflow.plan_parser import parse_plan
from agentflow.tracked_paths import tracked_files

SCRIPT_DIR = Path(__file__).resolve().parent
SEED_DIR = SCRIPT_DIR / "fixtures" / "toy-repo-seed"
TOY_DIR = SCRIPT_DIR / "toy-repo"
ORIGIN_ROOT = SCRIPT_DIR / "toy-origin"
PROFILE = SCRIPT_DIR / "toy-profile" / "harness-toy.md"
PLAN = SCRIPT_DIR / "fixtures" / "plan-single-task.md"

COMMIT_MESSAGE = "Seed toy-repo calculator fixture"
FIXED_DATE = "2026-01-01T00:00:00 +0000"
FIXED_NAME = "agentflow-harness"
FIXED_EMAIL = "harness@agentflow.invalid"

# Deterministic seed sha for the current toy-repo-seed fixtures. If you change
# a seed file, rerun with --force, then update this constant AND the sha noted
# in harness/README.md in the same commit.
EXPECTED_SEED_SHA = "1292c9ae2ff279c45ea0a026785c9f075bc1fe6b"


def _run_git(args: list[str], cwd: Path) -> str:
    null_cfg = "NUL" if sys.platform == "win32" else "/dev/null"
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": FIXED_NAME,
            "GIT_AUTHOR_EMAIL": FIXED_EMAIL,
            "GIT_AUTHOR_DATE": FIXED_DATE,
            "GIT_COMMITTER_NAME": FIXED_NAME,
            "GIT_COMMITTER_EMAIL": FIXED_EMAIL,
            "GIT_COMMITTER_DATE": FIXED_DATE,
            # isolate from user/system git config (identity, hooks, autocrlf)
            "GIT_CONFIG_GLOBAL": null_cfg,
            "GIT_CONFIG_SYSTEM": null_cfg,
        }
    )
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode}):\n{proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _copy_lf_normalized(src: Path, dst: Path) -> None:
    """Copy text files with LF endings so the seed blob shas are identical on
    every platform regardless of how the outer checkout normalized them."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    with dst.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def toy_identity() -> tuple[str, str, str, str]:
    """(host_ref, base_branch, project, epic_branch), read from the tracked profile and plan.

    Read rather than restated, so the origin, the branch and the worktree path cannot drift
    from what the orchestrator will compute for Scenario B.
    """
    repo = _load_first_yaml_block(PROFILE)["canonical_repo"]
    plan = parse_plan(PLAN)
    if not plan.project or not plan.slug:
        raise RuntimeError(f"{PLAN} must declare project: and slug: - Scenario B resolves its tree from them")
    return repo["host_ref"], repo["base_branch"], plan.project, plan.slug


def origin_dir_for(host_ref: str, origin_root: Path = ORIGIN_ROOT) -> Path:
    """The local bare repo whose path ends in ``<host_ref>.git``, as doctor's rule reads it."""
    return origin_root.joinpath(*host_ref.split("/")).with_suffix(".git")


def epic_worktree_path(worktree_root: Path, project: str, epic_branch: str) -> Path:
    return worktree_root / project / epic_branch


def _resolve_worktree_root(explicit: Optional[str]) -> tuple[Optional[Path], str]:
    """(worktree_root, why-not). An explicit flag wins over environment.md."""
    if explicit:
        # Absolute: the git calls run with cwd=<toy repo>, so a relative root would land the
        # worktree inside the toy repo while the existence checks looked somewhere else.
        return Path(explicit).expanduser().resolve(), ""
    try:
        return Path(load_environment().worktree_root), ""
    except ConfigError as exc:
        return None, f"no --worktree-root and environment.md did not resolve ({exc})"


def _drop_worktree(toy_dir: Path, wt: Path) -> None:
    """Remove the toy repo's epic worktree and prune, so a reseed starts clean.

    Only a tree the toy repo itself registers is removed; any other directory at that path
    is somebody else's and is refused below rather than deleted.
    """
    if not (toy_dir / ".git").is_dir():
        return
    listed = _run_git(["worktree", "list", "--porcelain"], cwd=toy_dir)
    registered = {
        Path(line[len("worktree "):]).resolve()
        for line in listed.splitlines()
        if line.startswith("worktree ")
    }
    if wt.exists() and wt.resolve() in registered:
        _run_git(["worktree", "remove", "--force", str(wt)], cwd=toy_dir)
    _run_git(["worktree", "prune"], cwd=toy_dir)


def seed(
    *,
    toy_dir: Path = TOY_DIR,
    origin_root: Path = ORIGIN_ROOT,
    worktree_root: Optional[Path],
    force: bool,
) -> tuple[str, Optional[Path]]:
    """Seed the toy repo, its local origin and (with a worktree root) Scenario B's worktree.

    Returns (seed sha, worktree path or None). Raises FileExistsError when something is
    already there and *force* is off, and RuntimeError when a git step fails.
    """
    host_ref, base, project, epic_branch = toy_identity()
    origin = origin_dir_for(host_ref, origin_root)
    if worktree_root is not None:
        worktree_root = Path(worktree_root).expanduser().resolve()
    wt = epic_worktree_path(worktree_root, project, epic_branch) if worktree_root else None

    # The origin is derived from the toy repo, so a leftover one never needs --force; it is
    # rebuilt below whenever the repo is.
    present = [p for p in (toy_dir, wt) if p is not None and p.exists()]
    if present and not force:
        raise FileExistsError(
            f"already exists: {', '.join(str(p) for p in present)}. Rerun with --force to wipe "
            f"and reseed (any recorded state in it will be lost)."
        )
    if wt is not None:
        _drop_worktree(toy_dir, wt)
        if wt.exists():
            raise FileExistsError(
                f"{wt} exists and is not a worktree of {toy_dir}; it is not this script's to "
                f"delete - move it aside, then reseed"
            )
    if toy_dir.exists():
        shutil.rmtree(toy_dir, onerror=_chmod_retry)
    if origin.exists():
        shutil.rmtree(origin, onerror=_chmod_retry)

    # The seed is the git index, not the directory (gh#410): an untracked stray beside the seed
    # would otherwise land in the toy repo and drift the seed sha on one machine only.
    for src in tracked_files(SEED_DIR):
        if src.is_file():
            _copy_lf_normalized(src, toy_dir / src.relative_to(SEED_DIR))

    _run_git(["init", "-q", "-b", base], cwd=toy_dir)
    _run_git(["config", "core.autocrlf", "false"], cwd=toy_dir)
    _run_git(["add", "-A"], cwd=toy_dir)
    _run_git(["commit", "-q", "-m", COMMIT_MESSAGE], cwd=toy_dir)
    sha = _run_git(["rev-parse", "HEAD"], cwd=toy_dir)

    # The origin: a bare repo on this disk, so `origin/<base>` exists for the Step 5 form below
    # and Step 1.1's identity check reads <host_ref> off the URL's last two segments.
    origin.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q", "--bare", "-b", base, str(origin)], cwd=origin.parent)
    _run_git(["remote", "add", "origin", origin.resolve().as_uri()], cwd=toy_dir)
    _run_git(["push", "-q", "origin", base], cwd=toy_dir)

    if wt is not None:
        # /project Step 5 in shape: fetch, then branch off origin/<base> with no upstream.
        _run_git(["fetch", "-q", "origin"], cwd=toy_dir)
        wt.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["worktree", "add", "-q", str(wt), "-b", epic_branch, "--no-track", f"origin/{base}"],
            cwd=toy_dir,
        )
    return sha, wt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="reconstruct harness/toy-repo from tracked seed fixtures")
    parser.add_argument(
        "--force", action="store_true", help="delete an existing toy-repo, origin and worktree first"
    )
    parser.add_argument(
        "--worktree-root",
        help="root to create Scenario B's epic worktree under (default: environment.md worktree_root)",
    )
    args = parser.parse_args(argv)

    if not SEED_DIR.is_dir():
        print(f"FATAL: seed fixtures missing: {SEED_DIR}", file=sys.stderr)
        return 2
    worktree_root, why_not = _resolve_worktree_root(args.worktree_root)
    try:
        sha, wt = seed(worktree_root=worktree_root, force=args.force)
    except FileExistsError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    print(f"seeded {TOY_DIR}")
    print(f"seed HEAD sha: {sha}")
    if EXPECTED_SEED_SHA and sha != EXPECTED_SEED_SHA:
        print(
            f"FATAL: seed sha drifted (expected {EXPECTED_SEED_SHA}) - a seed "
            f"fixture changed; update EXPECTED_SEED_SHA + harness/README.md deliberately",
            file=sys.stderr,
        )
        return 1
    print(f"origin: {origin_dir_for(toy_identity()[0])} (local bare repo, file:// URL)")
    if wt is not None:
        print(f"Scenario B worktree: {wt}")
    else:
        print(
            f"NOTE: Scenario B worktree NOT created: {why_not}. Scenario B stops at the "
            f"orchestrator's Step 1.1 without it; rerun with --worktree-root <dir> to make it.",
            file=sys.stderr,
        )
    print(
        f"reset to clean state any time:  git -C harness/toy-repo reset --hard {sha}"
        " && git -C harness/toy-repo clean -fd"
    )
    return 0


def _chmod_retry(func, path, exc_info):  # noqa: ANN001 - shutil onerror signature
    """Windows: .git objects are read-only; chmod +w then retry the removal."""
    import stat

    os.chmod(path, stat.S_IWRITE)
    func(path)


if __name__ == "__main__":
    sys.exit(main())
