"""Per-wave git worktree helpers.

Wraps `git worktree` CLI to give each orchestrator wave subagent
its own working tree on a throwaway branch. See the per-wave-worktrees
design spec under `<data_root>/projects/<name>/design/specs/`.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
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
class PruneFailure:
    path: Path
    branch: str
    stderr: str
    # Git can deregister a stuck tree while keeping its branch, so a later prune no longer sees the
    # orphan and this failure is the only record that deleting the branch would lose commits.
    has_unmerged_commits: bool


@dataclass
class PruneSummary:
    worktrees_removed: int
    branches_preserved: list[str]
    branches_deleted: list[str]
    # Orphans whose cleanup git refused, each with git's own reason. A failed orphan is in none of
    # the lists above: its tree, its branch, or both are still there for the caller to report.
    failed: list[PruneFailure] = field(default_factory=list)
    # Wave trees the caller named as live (a task still records them), and the best-of-N candidate
    # trees of such a task, left untouched with their branches. A kept tree is in none of the
    # lists above.
    branches_kept: list[str] = field(default_factory=list)


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
    """Merge a wave's branch back into the main worktree's HEAD.

    Fetches `<branch>` from the wave tree, then fast-forwards when the main HEAD has not moved
    since the wave was created. Sibling waves are all created from the same epic HEAD, so once one
    lands the others have diverged; those land through a merge commit (`--no-ff`) instead. The
    task commit is never rebased: its sha must survive the landing, because the orchestrator
    matches task commits by signature in the log.

    The fetch lands in a ref private to this branch, never in FETCH_HEAD: the main worktree has one
    FETCH_HEAD, and two merge-backs running at once into it would each merge whichever tip was
    fetched last, both report success, and leave one task's commit on no branch.

    Returns MergeResult(success=False, stderr=...) when the fetch fails or the merge does. A merge
    that stopped part-way (a conflict) is aborted so the main worktree is left exactly as it was;
    if the abort itself fails, its stderr is appended to the merge's.
    """
    land_ref = f"refs/wave-land/{branch}"
    fetch = _git_in(main_worktree, "fetch", "--no-write-fetch-head", str(wave_worktree),
                    f"+refs/heads/{branch}:{land_ref}")
    if fetch.returncode != 0:
        return MergeResult(success=False, stderr=fetch.stderr)
    result = _merge_landed(main_worktree, branch, land_ref)
    # A leftover ref is harmless (the next fetch force-updates it) but is reported, not dropped.
    drop = _git_in(main_worktree, "update-ref", "-d", land_ref)
    if drop.returncode != 0:
        note = f"could not delete {land_ref}: {drop.stderr.strip()}"
        result.stderr = f"{result.stderr}\n{note}" if result.stderr else note
    return result


def _merge_landed(main_worktree: Path, branch: str, land_ref: str) -> MergeResult:
    tip = _git_in(main_worktree, "rev-parse", "--verify", f"{land_ref}^{{commit}}")
    if tip.returncode != 0:
        return MergeResult(success=False, stderr=tip.stderr)
    sha = tip.stdout.strip()

    if _git_in(main_worktree, "merge", "--ff-only", sha).returncode != 0:
        merge = _git_in(main_worktree, "merge", "--no-ff", "--no-edit",
                        "-m", f"Merge wave branch '{branch}'", sha)
        if merge.returncode != 0:
            return MergeResult(success=False, stderr=_refused(main_worktree, merge, sha))

    # A success is only a success if this wave's own tip is now in HEAD.
    if _git_in(main_worktree, "merge-base", "--is-ancestor", sha, "HEAD").returncode != 0:
        return MergeResult(success=False,
                           stderr=f"merge reported success but {sha} is not in HEAD")
    return MergeResult(success=True, stderr="")


def _refused(main_worktree: Path, merge: subprocess.CompletedProcess, sha: str) -> str:
    # git prints conflict lines on stdout and its verdict on stderr; the caller needs both.
    stderr = "\n".join(s.strip() for s in (merge.stdout, merge.stderr) if s.strip())
    # A merge git refused before starting leaves no MERGE_HEAD and nothing to abort. A MERGE_HEAD
    # naming another tip belongs to a concurrent merge-back, whose own abort is its business.
    merge_head = _git_in(main_worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    if merge_head.returncode == 0 and merge_head.stdout.strip() == sha:
        abort = _git_in(main_worktree, "merge", "--abort")
        if abort.returncode != 0:
            stderr += f"\nmerge --abort also failed: {abort.stderr.strip()}"
    return stderr


def _git_in(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


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


def _path_key(path: str | os.PathLike) -> str:
    """One spelling per directory: resolved (made absolute against the cwd, links followed) and
    normcased, so a trailing separator, forward or back slashes, dot segments, a junction or
    symlink, and a Windows case variant all compare equal."""
    return os.path.normcase(str(Path(path).resolve()))


def _live_keys(live_paths: Iterable[str | os.PathLike | None]) -> set[str]:
    if isinstance(live_paths, (str, bytes, os.PathLike)):
        # A bare path iterates as its characters, which would silently keep nothing.
        raise TypeError("live_paths must be a collection of paths, not a single path")
    # An unset `wave_worktree_path` is None (or empty): such a task holds no tree to keep.
    return {_path_key(p) for p in live_paths if p}


def _candidate_parent_key(o: OrphanRecord, repo_path: Path, epic_slug: str) -> str | None:
    """The `_path_key` of the task tree a best-of-N candidate belongs to; None for any other tree.

    State records only the task's own "-t<id>" tree, never its "-t<id>-c<i>" candidates, so a
    candidate is live exactly when its parent is. The parent is rebuilt with `_wave_path` from the
    branch's own numbers (as `list_orphan_wave_worktrees` built the candidate's path), so "-t1"
    never claims "-t10-c0" and another wave's candidates stay orphans.
    """
    m = re.fullmatch(rf"{re.escape(epic_slug)}-w(\d+)-t(\d+)-c\d+", o.branch)
    if not m:
        return None
    return _path_key(_wave_path(Path(os.path.abspath(repo_path)), epic_slug,
                                int(m.group(1)), m.group(2)))


def _release(o: OrphanRecord, repo_path: Path, summary: PruneSummary) -> None:
    """Remove one wave tree into `summary`; git's refusal is recorded, never raised."""
    try:
        cleanup_wave_worktree(
            path=o.path,
            branch=o.branch,
            main_worktree=repo_path,
            preserve_branch=o.has_unmerged_commits,
        )
    except subprocess.CalledProcessError as exc:
        summary.failed.append(PruneFailure(
            path=o.path, branch=o.branch, stderr=_git_reason(exc),
            has_unmerged_commits=o.has_unmerged_commits))
        return
    summary.worktrees_removed += 1
    if o.has_unmerged_commits:
        summary.branches_preserved.append(o.branch)
    else:
        summary.branches_deleted.append(o.branch)


def prune_orphan_worktrees(
    repo_path: Path,
    epic_slug: str,
    live_paths: Iterable[str | os.PathLike | None] = (),
) -> PruneSummary:
    """Remove orphan wave worktrees for this epic; preserve branches that still hold unmerged commits.

    `live_paths` names trees a task still records as its own (every set
    `state.tasks[*].wave_worktree_path`). They are not orphans: the in-flight task's tree is what
    merge-back fetches from on approval, so each is kept, tree and branch, and listed in
    `branches_kept`. A best-of-N candidate tree ("-t<id>-c<i>") is never recorded, so it is kept
    the same way while its task's "-t<id>" tree is live: an agent may still be building in it.
    Omitting `live_paths` keeps the old behaviour of pruning every matching tree.

    One orphan git cannot clean up (on Windows, a tree some process still has as its cwd) is
    recorded in `failed` with git's stderr and the rest are still pruned, so a single stuck tree
    neither aborts the caller nor strands the trees behind it.
    """
    live = _live_keys(live_paths)
    summary = PruneSummary(worktrees_removed=0, branches_preserved=[], branches_deleted=[])
    for o in list_orphan_wave_worktrees(repo_path, epic_slug):
        if _path_key(o.path) in live or _candidate_parent_key(o, repo_path, epic_slug) in live:
            summary.branches_kept.append(o.branch)
            continue
        _release(o, repo_path, summary)
    # Final cleanup of dangling .git/worktrees/ entries
    subprocess.run(["git", "worktree", "prune"], cwd=repo_path, check=True, capture_output=True)
    return summary


def release_wave_worktree(path: Path, branch: str | None, main_worktree: Path) -> PruneSummary:
    """Remove the tree of a task that goes back to ready, before its path is cleared.

    The re-dispatch builds in a new tree (the wave number moves on), so the old one would
    otherwise outlive the task. Its branch is kept when it holds commits the epic does not, as the
    prune does. A tree git cannot remove, or one already gone, lands in `failed` with git's reason
    rather than raising: once the path is cleared the next wake's prune sees the tree as an
    orphan and retries it.
    """
    summary = PruneSummary(worktrees_removed=0, branches_preserved=[], branches_deleted=[])
    if not branch:
        # A hand-edited state can hold a path with no branch. Without the branch nothing can say
        # whether deleting it loses commits, so leave the tree to the prune, which reads the
        # branch from git once the cleared path makes the tree an orphan.
        summary.failed.append(PruneFailure(
            path=Path(path), branch="", has_unmerged_commits=False,
            stderr="no wave_branch recorded for this tree; the next prune retries it as an orphan"))
        return summary
    try:
        unmerged = _has_unmerged_commits(main_worktree, branch)
    except subprocess.CalledProcessError as exc:
        summary.failed.append(PruneFailure(
            path=Path(path), branch=branch, stderr=_git_reason(exc), has_unmerged_commits=False))
        return summary
    _release(OrphanRecord(path=Path(path), branch=branch, has_unmerged_commits=unmerged),
             main_worktree, summary)
    return summary


def _git_reason(exc: subprocess.CalledProcessError) -> str:
    """Git's own words for a failed command; the exception's summary only when git said nothing."""
    stderr = exc.stderr
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return (stderr or "").strip() or str(exc)


def main(argv: list[str] | None = None) -> int:
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    import argparse
    import json
    from pathlib import Path
    import sys

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
    try:
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
    except subprocess.CalledProcessError as exc:
        # Git captured its reason; without this the operator sees only a traceback and an exit code.
        print(f"wave-worktree error: {_git_reason(exc)}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
