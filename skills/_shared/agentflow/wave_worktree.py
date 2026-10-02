"""Per-wave git worktree helpers.

Wraps `git worktree` CLI to give each orchestrator wave subagent
its own working tree on a throwaway branch. See the per-wave-worktrees
design spec under `<data_root>/projects/<name>/design/specs/`.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess


@dataclass
class MergeResult:
    success: bool
    stderr: str


@dataclass
class OrphanRecord:
    path: Path
    branch: str
    has_unmerged_commits: bool


@dataclass
class PruneSummary:
    worktrees_removed: int
    branches_preserved: list[str]
    branches_deleted: list[str]


def _wave_branch(epic_slug: str, wave_n: int, task_id: int | str) -> str:
    return f"{epic_slug}-w{wave_n}-t{task_id}"


def _wave_path(repo_path: Path, epic_slug: str, wave_n: int, task_id: int | str) -> Path:
    return repo_path.parent / f"{repo_path.name}-{_wave_branch(epic_slug, wave_n, task_id)}"


def create_wave_worktree(
    repo_path: Path,
    epic_slug: str,
    wave_n: int,
    task_id: int | str,
    base_branch: str,
) -> Path:
    """Create a per-wave worktree at a sibling path on a throwaway branch.

    Returns the absolute Path to the new worktree's root.
    """
    branch = _wave_branch(epic_slug, wave_n, task_id)
    path = _wave_path(repo_path, epic_slug, wave_n, task_id)
    subprocess.run(
        ["git", "worktree", "add", str(path), "-b", branch, base_branch],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def merge_back(wave_worktree: Path, main_worktree: Path, branch: str) -> MergeResult:
    """Fast-forward-merge a wave's branch back into the main worktree's HEAD.

    Uses `git fetch <wave-path> <branch>` + `git merge --ff-only FETCH_HEAD`.
    Returns MergeResult(success=False, stderr=...) on FF failure — no partial
    state mutation on the main worktree.
    """
    fetch = subprocess.run(
        ["git", "fetch", str(wave_worktree), branch],
        cwd=main_worktree,
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        return MergeResult(success=False, stderr=fetch.stderr)

    merge = subprocess.run(
        ["git", "merge", "--ff-only", "FETCH_HEAD"],
        cwd=main_worktree,
        capture_output=True,
        text=True,
    )
    if merge.returncode != 0:
        return MergeResult(success=False, stderr=merge.stderr)

    return MergeResult(success=True, stderr="")


def cleanup_wave_worktree(
    path: Path,
    branch: str,
    main_worktree: Path,
    preserve_branch: bool = False,
) -> None:
    """Remove a wave worktree; optionally preserve its branch.

    Called from two contexts:
    1. After successful merge_back — preserve_branch=False (delete branch too).
    2. Crash-recovery bootstrap with unmerged commits — preserve_branch=True.
    """
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=main_worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    if not preserve_branch:
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=main_worktree,
            check=True,
            capture_output=True,
            text=True,
        )


def _parse_worktree_porcelain(output: str) -> list[dict]:
    """Parse `git worktree list --porcelain` output into a list of dicts."""
    entries = []
    current: dict = {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["worktree"] = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].strip().removeprefix("refs/heads/")
        elif line == "detached":
            current["branch"] = None
    if current:
        entries.append(current)
    return entries


def _has_unmerged_commits(repo_path: Path, branch: str, base: str = "HEAD") -> bool:
    """Return True if `branch` has commits not reachable from `base` (the main HEAD)."""
    result = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..{branch}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip()) > 0


def list_orphan_wave_worktrees(repo_path: Path, epic_slug: str) -> list[OrphanRecord]:
    """Scan `git worktree list --porcelain` for worktrees matching the epic's wave pattern."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    # Best-of-N candidates are created with task "<id>-c<i>" (wave_group.js), so the branch ends
    # "-t<id>-c<i>"; without the optional suffix a crash leaks every candidate worktree (gh#503).
    branch_pattern = re.compile(rf"{re.escape(epic_slug)}-w(\d+)-t(\d+(?:-c\d+)?)")
    # `git worktree list` covers every worktree sharing this git dir, and repo names and slugs both
    # hold hyphens, so no basename pattern alone is unambiguous: "app-x" + "ep" and "app" + "x-ep"
    # spell the same directory (gh#514). Ownership therefore needs BOTH halves create_wave_worktree
    # wrote: the checked-out branch is exactly _wave_branch(epic_slug, ...), and the path is exactly
    # _wave_path(repo, ...) for those same numbers. The expected path is built from the caller's
    # ABSOLUTE spelling, not its resolved one: create_wave_worktree names the wave off the spelling
    # it was given, so a repo reached through a junction or symlink would otherwise expect the
    # link target's name and miss its own orphans. Both sides are then resolved and normcased,
    # which also covers Path(".") and a Windows case variant. The branch check above, not the path,
    # is what keeps "app-x" + "ep" off "app" + "x-ep".
    resolved_repo = Path(os.path.abspath(repo_path))
    orphans = []
    for entry in _parse_worktree_porcelain(result.stdout):
        wt = entry.get("worktree", "")
        branch = entry.get("branch")
        if not branch:
            continue  # detached HEAD: create_wave_worktree always checks out a branch
        m = branch_pattern.fullmatch(branch)
        if not m:
            continue
        expected = _wave_path(resolved_repo, epic_slug, int(m.group(1)), m.group(2))
        if os.path.normcase(Path(wt).resolve()) != os.path.normcase(expected.resolve()):
            continue
        unmerged = _has_unmerged_commits(repo_path, branch)
        orphans.append(OrphanRecord(path=Path(wt), branch=branch, has_unmerged_commits=unmerged))
    return orphans


def prune_orphan_worktrees(repo_path: Path, epic_slug: str) -> PruneSummary:
    """Remove orphan wave worktrees for this epic; preserve branches that still hold unmerged commits."""
    orphans = list_orphan_wave_worktrees(repo_path, epic_slug)
    preserved: list[str] = []
    deleted: list[str] = []
    for o in orphans:
        cleanup_wave_worktree(
            path=o.path,
            branch=o.branch,
            main_worktree=repo_path,
            preserve_branch=o.has_unmerged_commits,
        )
        if o.has_unmerged_commits:
            preserved.append(o.branch)
        else:
            deleted.append(o.branch)
    # Final cleanup of dangling .git/worktrees/ entries
    subprocess.run(["git", "worktree", "prune"], cwd=repo_path, check=True, capture_output=True)
    return PruneSummary(
        worktrees_removed=len(orphans),
        branches_preserved=preserved,
        branches_deleted=deleted,
    )


def main(argv: list[str] | None = None) -> int:
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    import argparse
    import json
    from pathlib import Path

    p = argparse.ArgumentParser(prog="python -m agentflow.wave_worktree")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--repo", required=True)
    c.add_argument("--epic", required=True)
    c.add_argument("--wave", type=int, required=True)
    c.add_argument("--task", required=True)
    c.add_argument("--base", required=True)
    c.add_argument("--json", action="store_true")

    m = sub.add_parser("merge-back")
    m.add_argument("--wave-worktree", required=True)
    m.add_argument("--main", required=True)
    m.add_argument("--branch", required=True)

    cl = sub.add_parser("cleanup")
    cl.add_argument("--path", required=True)
    cl.add_argument("--branch", required=True)
    cl.add_argument("--main", required=True)
    cl.add_argument("--preserve-branch", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "create":
        path = create_wave_worktree(
            repo_path=Path(args.repo), epic_slug=args.epic,
            wave_n=args.wave, task_id=args.task, base_branch=args.base)
        print(json.dumps({"path": str(path)}) if args.json else str(path))
        return 0
    if args.cmd == "merge-back":
        result = merge_back(
            wave_worktree=Path(args.wave_worktree),
            main_worktree=Path(args.main), branch=args.branch)
        print(json.dumps({"success": bool(result.success),
                          "stderr": getattr(result, "stderr", "")}))
        return 0 if result.success else 2
    if args.cmd == "cleanup":
        cleanup_wave_worktree(Path(args.path), args.branch, Path(args.main),
                              preserve_branch=args.preserve_branch)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
