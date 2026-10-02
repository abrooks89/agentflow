#!/usr/bin/env python3
"""Reconstruct the harness toy-repo fixture from tracked seed files.

The toy-repo is a tiny git repo (its own ``.git``) used as the controlled
target for orchestrator/builder scenario recordings. It is gitignored from the
outer repo, so a fresh clone does not have it — this script rebuilds it
byte-identically from ``fixtures/toy-repo-seed/``:

    python harness/seed_toy_repo.py            # create harness/toy-repo/
    python harness/seed_toy_repo.py --force    # wipe + recreate

The seed commit is DETERMINISTIC: file contents are LF-normalized and the
commit uses a fixed author/committer/date, so every machine produces the same
seed sha. The script prints the sha and fails loudly if it drifts from
EXPECTED_SEED_SHA (meaning a seed fixture changed — update the constant and
harness/README.md together, deliberately).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SEED_DIR = SCRIPT_DIR / "fixtures" / "toy-repo-seed"
TOY_DIR = SCRIPT_DIR / "toy-repo"

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="reconstruct harness/toy-repo from tracked seed fixtures")
    parser.add_argument("--force", action="store_true", help="delete an existing toy-repo first")
    args = parser.parse_args(argv)

    if not SEED_DIR.is_dir():
        print(f"FATAL: seed fixtures missing: {SEED_DIR}", file=sys.stderr)
        return 2
    if TOY_DIR.exists():
        if not args.force:
            print(
                f"FATAL: {TOY_DIR} already exists. Rerun with --force to wipe and "
                f"reseed (any recorded state in it will be lost).",
                file=sys.stderr,
            )
            return 2
        shutil.rmtree(TOY_DIR, onerror=_chmod_retry)

    for src in sorted(SEED_DIR.rglob("*")):
        if src.is_file():
            _copy_lf_normalized(src, TOY_DIR / src.relative_to(SEED_DIR))

    _run_git(["init", "-q", "-b", "main"], cwd=TOY_DIR)
    _run_git(["config", "core.autocrlf", "false"], cwd=TOY_DIR)
    _run_git(["add", "-A"], cwd=TOY_DIR)
    _run_git(["commit", "-q", "-m", COMMIT_MESSAGE], cwd=TOY_DIR)
    sha = _run_git(["rev-parse", "HEAD"], cwd=TOY_DIR)

    print(f"seeded {TOY_DIR}")
    print(f"seed HEAD sha: {sha}")
    if EXPECTED_SEED_SHA and sha != EXPECTED_SEED_SHA:
        print(
            f"FATAL: seed sha drifted (expected {EXPECTED_SEED_SHA}) - a seed "
            f"fixture changed; update EXPECTED_SEED_SHA + harness/README.md deliberately",
            file=sys.stderr,
        )
        return 1
    print(f"reset to clean state any time:  cd harness/toy-repo && git reset --hard {sha} && git clean -fd")
    return 0


def _chmod_retry(func, path, exc_info):  # noqa: ANN001 - shutil onerror signature
    """Windows: .git objects are read-only; chmod +w then retry the removal."""
    import os
    import stat

    os.chmod(path, stat.S_IWRITE)
    func(path)


if __name__ == "__main__":
    sys.exit(main())
