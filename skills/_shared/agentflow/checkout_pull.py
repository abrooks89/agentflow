"""Domain C's one bounded fix: fast-forward a link-deployed checkout onto its base branch.

WHY A PULL AND NOT A DEPLOY. Where the deployed skill directories are junctions (Windows) or
symlinks (Linux) onto the checkout, the deployed text IS the checkout's text, so "deployed is
stale" means exactly "the checkout is behind its remote". The cure is a pull. A provision run
cures nothing there and rewrites hooks and settings besides, so it stays a human-typed step.

THE FOUR CONDITIONS, all measured in this call, every one of them required:

1. ``not_link_deployed`` - the deployed path is a link whose target belongs to the checkout's own
   work tree (git's ``--show-toplevel`` of the target is the checkout, so a worktree nested under
   the checkout does not count). On a copy-deployed surface a pull does not change what any seat
   reads, so it would only move the checkout under a human who may be mid-provision.
2. ``not_on_base_branch`` - the checkout's HEAD is the base branch, not a side branch and not
   detached. Pulling the base branch into anything else is a merge, not a sync.
3. ``tracked_changes`` - no tracked file differs from HEAD. A hand edit in the canonical checkout
   is someone's work; a pull must never be the thing that collides with it. Untracked files do
   not refuse, because ``pull --ff-only`` refuses on its own before overwriting one.
4. ``not_fast_forward`` - HEAD is an ancestor of the freshly fetched ``origin/<base>``. Anything
   else needs a decision about whose commit wins.

The remote ref is fetched first and every comparison is against it, never against the working
tree: a checkout and its link-deployed surface go stale TOGETHER, so a comparison between the
two cannot see the drift. ``fetch_failed`` refuses too - a checkout that cannot fetch looks
identical to a current one.

EVERY failing condition is reported, not only the first, so one report says everything a human
must change. Report-only unless ``--apply`` is passed; with it, the pull runs only when nothing
refused, and the sha before and after is printed so the move is in the record.

Refusals print as ``refused [<key>]: <rule>`` with the detail indented beneath, the same shape
`agentflow.registry` and `agentflow.worktree_remove` use.
"""

from __future__ import annotations

import argparse
import errno
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REMOTE = "origin"

#: Every refusal this helper can raise, keyed the way `registry.RULES` is.
RULES = {
    "not_link_deployed": (
        "the deployed skill directory must be a junction or symlink into the checkout's own work tree"
    ),
    "not_on_base_branch": "the checkout must have the base branch checked out",
    "tracked_changes": "the checkout must have no tracked changes",
    "not_fast_forward": "the checkout's HEAD must be an ancestor of the fetched remote base branch",
    "fetch_failed": "the remote base branch must be fetchable",
    "pull_failed": "git refused the fast-forward pull",
}


@dataclass
class Refusal:
    key: str
    detail: str

    def __str__(self) -> str:
        return f"refused [{self.key}]: {RULES[self.key]}\n    {self.detail}"


@dataclass
class Verdict:
    refusals: list = field(default_factory=list)
    before: Optional[str] = None
    after: Optional[str] = None
    behind: Optional[int] = None
    pulled: bool = False


GIT_TIMEOUT_S = 120


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # a credential or host-key prompt would hang the steward's wake; refuse instead
    argv = ["git", "-C", str(repo), *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, check=False, env=env, timeout=GIT_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv, 124, "", f"git {args[0]} timed out after {GIT_TIMEOUT_S}s"
        )


def _is_link(path: Path) -> bool:
    """True for a symlink OR a Windows junction.

    `Path.is_symlink()` is False for a junction on Python 3.11; `os.readlink` answers for both.
    Kept local rather than imported because the installer's copy lives outside this package.
    """
    if path.is_symlink():
        return True
    try:
        os.readlink(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        # EINVAL (POSIX) and 4390 ERROR_NOT_A_REPARSE_POINT (Windows) mean "not a link".
        # Anything else is not an answer and must not read as one.
        if exc.errno == errno.EINVAL or getattr(exc, "winerror", None) == 4390:
            return False
        raise
    return True


def _norm(p: Path) -> str:
    return os.path.normcase(str(p.resolve()))


def _link_detail(checkout: Path, deployed: Path) -> Optional[str]:
    """None when link-deployed onto *checkout*; otherwise why not."""
    if not _is_link(deployed):
        if deployed.exists():
            return f"{deployed} is a real directory (copy-deployed), not a link onto the checkout"
        return f"{deployed} does not exist"
    target = deployed.resolve()
    if not target.exists():
        return f"{deployed} is a dangling link"
    root = _norm(checkout)
    resolved = _norm(target)
    if resolved != root and not resolved.startswith(root.rstrip(os.sep) + os.sep):
        return f"{deployed} links to {target}, which is outside the checkout {checkout}"
    # A nested path is not enough: a git worktree created under the checkout lives inside its
    # directory but has its own HEAD, so pulling the checkout would not change what the link
    # serves. Ask git which work tree owns the target.
    top = _git(target, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return f"{deployed} links to {target}, which is not inside any git work tree"
    owner = Path(top.stdout.strip())
    if _norm(owner) != root:
        return f"{deployed} links to {target}, which belongs to the work tree {owner}, not {checkout}"
    return None


def evaluate(checkout: Path, base_branch: str, deployed: Path) -> Verdict:
    """Measure all four conditions (fetching the remote ref first). Never moves HEAD."""
    v = Verdict()
    head = _git(checkout, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise RuntimeError(f"not a git checkout: {checkout}: {head.stderr.strip()}")
    v.before = v.after = head.stdout.strip()

    detail = _link_detail(checkout, deployed)
    if detail:
        v.refusals.append(Refusal("not_link_deployed", detail))

    branch = _git(checkout, "symbolic-ref", "--short", "-q", "HEAD")
    current = branch.stdout.strip() if branch.returncode == 0 else ""
    if current != base_branch:
        v.refusals.append(Refusal(
            "not_on_base_branch",
            f"HEAD is {current or 'detached'}, base branch is {base_branch}",
        ))

    status = _git(checkout, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        raise RuntimeError(f"git status failed in {checkout}: {status.stderr.strip()}")
    if status.stdout.strip():
        changed = ", ".join(line[3:] for line in status.stdout.splitlines()[:5])
        v.refusals.append(Refusal("tracked_changes", f"tracked changes: {changed}"))

    fetch = _git(checkout, "fetch", REMOTE, base_branch)
    if fetch.returncode != 0:
        v.refusals.append(Refusal(
            "fetch_failed", f"git fetch {REMOTE} {base_branch}: {fetch.stderr.strip()}"
        ))
        return v

    # The full ref, because a local branch literally named `origin/<base>` outranks the
    # remote-tracking ref when git resolves the short name.
    remote_ref = f"refs/remotes/{REMOTE}/{base_branch}"
    count = _git(checkout, "rev-list", "--count", f"HEAD..{remote_ref}")
    if count.returncode != 0:
        raise RuntimeError(f"git rev-list failed in {checkout}: {count.stderr.strip()}")
    v.behind = int(count.stdout.strip())
    ancestor = _git(checkout, "merge-base", "--is-ancestor", "HEAD", remote_ref)
    if ancestor.returncode == 1:
        ahead = _git(checkout, "rev-list", "--count", f"{remote_ref}..HEAD").stdout.strip()
        v.refusals.append(Refusal(
            "not_fast_forward",
            f"HEAD has {ahead} commit(s) not on {remote_ref}; {v.behind} behind",
        ))
    elif ancestor.returncode != 0:
        raise RuntimeError(f"git merge-base failed in {checkout}: {ancestor.stderr.strip()}")
    return v


def run(checkout: Path, base_branch: str, deployed: Path, apply: bool) -> Verdict:
    """Evaluate, and pull only when *apply* is set, nothing refused, and the checkout is behind."""
    v = evaluate(checkout, base_branch, deployed)
    if v.refusals or not apply or not v.behind:
        return v
    pull = _git(checkout, "pull", "--ff-only", REMOTE, base_branch)
    v.after = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    if pull.returncode != 0:
        v.refusals.append(Refusal("pull_failed", pull.stderr.strip() or pull.stdout.strip()))
        return v
    v.pulled = v.after != v.before
    return v


def _claude_root() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def main(argv=None) -> int:
    """The CLI. Report-only unless ``--apply``.

    EXIT CODE, the same contract as `agentflow.refs` and `agentflow.worktree_remove`:

    * ``0`` - pulled, already current, or (report-only) a pull would be allowed.
    * ``1`` - refused; the checkout was not touched.
    * ``2`` - malformed call, or the project could not be resolved.
    """
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    ap = argparse.ArgumentParser(prog="python -m agentflow.checkout_pull", description=__doc__)
    ap.add_argument("--project", help="project whose canonical_repo to use (default: resolved)")
    ap.add_argument("--checkout", help="override canonical_repo.local_checkout")
    ap.add_argument("--base-branch", help="override canonical_repo.base_branch")
    ap.add_argument(
        "--deployed",
        help="deployed skill directory to judge (default: <claude config dir>/skills/steward)",
    )
    ap.add_argument("--apply", action="store_true", help="pull when every condition holds")
    args = ap.parse_args(argv)

    if args.checkout:
        if not args.base_branch:
            print("--checkout needs --base-branch")
            return 2
        checkout, base_branch = Path(args.checkout).expanduser(), args.base_branch
    else:
        from . import config as _config

        try:
            name = _config.resolve_project_name(args.project)
            if not name:
                print("could not resolve project: pass --project <name>")
                return 2
            proj = _config.load_project(name)
        except _config.ConfigError as exc:
            print(f"could not resolve project: {exc}")
            return 2
        checkout = Path(proj.canonical_repo.local_checkout).expanduser()
        base_branch = args.base_branch or proj.canonical_repo.base_branch
    deployed = (
        Path(args.deployed).expanduser() if args.deployed else _claude_root() / "skills" / "steward"
    )

    try:
        v = run(checkout, base_branch, deployed, args.apply)
    except RuntimeError as exc:
        print(f"could not measure: {exc}")
        return 2

    print(f"# agentflow checkout_pull - {checkout} ({base_branch})")
    print(f"deployed: {deployed}")
    print(f"behind {REMOTE}/{base_branch}: {'-' if v.behind is None else v.behind}")
    print(f"before: {v.before}")
    for r in v.refusals:
        print(r)
    if v.refusals:
        print(f"after: {v.after}")
        print("NOT PULLED.")
        return 1
    print(f"after: {v.after}")
    if v.pulled:
        print("pulled: fast-forwarded.")
    elif not v.behind:
        print("current: nothing to pull.")
    else:
        print("pull allowed: every condition holds. Re-run with --apply to pull.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
