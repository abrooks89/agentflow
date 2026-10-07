"""Stale remote-tracking refs and merged local branches in the canonical checkout.

GitHub deletes the remote branch when a PR merges, but a checkout that does not prune on fetch
keeps the `refs/remotes/origin/*` ref pointing at nothing. Every `git branch -a` and every ref
enumeration then reads a deleted branch as a live one. The steward hand-pruned the same debris on
three consecutive rotations - 21 refs - before this was filed (gh#116).

THE OTHER CAUSE IS MERGED LOCAL BRANCHES, AND `--apply` DELETES THEM UNDER THREE GUARDS. `gh pr
merge --delete-branch` removes the REMOTE branch only, so a merged branch leaves `<name>` behind in
the local list. There is no recovery window: a branch whose tip is its merged PR's `headRefOid`
loses nothing when deleted, because the squash is on the base branch and the host keeps the PR head
commit. A local branch is deleted only when ALL THREE hold:

1. it has no head on origin, asked of `ls-remote`, never of the tracking refs;
2. its tip equals the `headRefOid` of a merged PR with the same head name - an exact sha match,
   never ancestry, because a squash merge makes every merged branch read as unmerged to
   `--merged` and `is-ancestor`, and a branch pushed to after its merge holds unmerged work;
3. no worktree has it checked out, the current branch included, and the worktree list was read.

The base branch is never a candidate. The deletion is `update-ref -d refs/heads/<name> <sha>`, so a
branch that moved between plan and apply refuses loudly instead of being deleted. With no merged-PR
reader for the project's host, nothing local is deleted and the reason is reported; a reader that
FAILED is a refusal. Everything else - merged-but-moved, never merged, checked out - is reported
with the reason it was kept, and never deleted.

`fetch.prune` IS THE ACTUAL CURE FOR THE TRACKING-REF HALF, AND THIS TOOL DELIBERATELY WILL NOT
APPLY IT. Setting it removes only refs git already knows are gone, and it ends the accumulation
permanently instead of sweeping it forever. But it changes the behaviour of a checkout six lanes
share, and the steward escalated gh#116 rather than setting it unilaterally for exactly that reason. So an unset
`fetch.prune` is a REFUSAL here: reported every run, never applied, and it keeps the exit code
non-zero in BOTH modes - the same shape `backups.reap` uses for a group holding the only copy of a
file. A sweep that can quietly finish the job is a sweep that has stopped asking.

THE FAILURE MODE THIS MODULE IS BUILT AROUND is answering "which refs are dead?" from an
impoverished view of the remote. Every such answer is refused rather than guessed, because the
consequence is deleting every tracking ref at once: a transport failure, a remote that reports NO
branches at all, and a fetch refspec that maps non-branch refs into `refs/remotes/origin/*` are all
treated as "I cannot classify", never as "nothing is live".
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

#: The remote every lane pushes to. Named once so a second remote cannot be swept by accident.
REMOTE = "origin"

#: The only fetch refspec under which "a ref under refs/remotes/origin/ is a branch" holds.
STANDARD_REFSPEC = f"+refs/heads/*:refs/remotes/{REMOTE}/*"

#: Merged pull requests read per sweep. A truncated list can only KEEP a branch - one whose PR fell
#: off the end has no head to match - and never delete one.
MERGED_PR_LIMIT = 1000


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run git in `repo` by EXPLICIT path.

    `-C` is not decoration: a fleet lane's working directory is the launch directory, which is
    not a repository, so a bare `git` here fails in the direction that looks like a broken
    machine rather than a wrong repo.
    """
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


@dataclass
class PrunePlan:
    """What a sweep found, split by who may act on it.

    Buckets by who may act on them:

    * ``prunable`` - stale tracking refs as ``(name, sha)``. This tool removes them under
      ``--apply``, and carries the sha so the removal can refuse a ref that moved since.
    * ``deletable`` - merged local branches as ``(name, sha)`` that passed all three guards.
      Deleted under ``--apply`` against the same sha.
    * ``kept`` - one line per deletion candidate that was NOT deletable, saying why.
    * ``refused`` - findings only a human may act on. Never applied, never cleared by a run.
    * ``reported`` - evidence, no action implied and no effect on the exit code.
    """

    prunable: list[tuple[str, str]] = field(default_factory=list)
    deletable: list[tuple[str, str]] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    reported: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self.prunable]

    @property
    def is_clean(self) -> bool:
        """Nothing pending AND nothing refused.

        `kept` is excluded for the same reason as `reported`: a merged-but-moved or never-merged
        branch is one this sweep may never delete, so it cannot be what keeps the exit non-zero.

        `reported` is excluded on purpose: it carries evidence the sweep is not entitled to act
        on, so letting it redden the exit would make a clean checkout permanently non-zero and
        train the reader to ignore the code.
        """
        return not self.prunable and not self.deletable and not self.refused


@dataclass(frozen=True)
class MergedHeads:
    """The merged-PR head set, or why it is missing.

    Two kinds of missing, kept apart because they differ in who owns them. ``unavailable`` is a
    standing fact of the configuration (no reader for this host), reported and not a finding.
    ``failed`` means a reader exists and its read did not complete, which is a blind sweep and is
    refused. Either way ``heads`` is None and nothing local is deleted.
    """

    heads: Optional[dict[str, frozenset[str]]] = None
    unavailable: str = ""
    failed: str = ""


#: Called only when a deletion candidate exists, so a sweep with nothing to delete makes no call.
PrReader = Callable[[], MergedHeads]


def merged_pr_heads(host_ref: str, git_host_kind: str, cli: str, runner=None) -> MergedHeads:
    """``headRefName -> {headRefOid, ...}`` for merged PRs, read with `gh pr list`.

    Gated by the same `_pr_reader_refusal` the fleet report uses, so a project on a host `gh`
    cannot read gets a stated reason instead of whatever GitHub repository shares its name.
    ``runner`` is the fleet report's argv seam: ``argv -> (rc, out, err)``.

    A name maps to a SET because head names are reused: two merged PRs from one branch name each
    leave a head, and a tip matching either is merged. An item with no oid, or from a fork, is
    skipped, which can only keep a branch.
    """
    from .fleet_report import _default_git_runner, _pr_reader_refusal

    refusal = _pr_reader_refusal(
        SimpleNamespace(tracker_host_ref=host_ref, git_host_kind=git_host_kind, tracker_cli=cli)
    )
    if refusal:
        return MergedHeads(unavailable=refusal)
    active = runner if runner is not None else _default_git_runner
    rc, out, err = active([
        "gh", "pr", "list",
        "--repo", host_ref,
        "--state", "merged",
        "--limit", str(MERGED_PR_LIMIT),
        "--json", "headRefName,headRefOid,isCrossRepository",
    ])
    if rc != 0:
        first = (err or "").strip().splitlines()
        return MergedHeads(failed=(first[0] if first else "gh pr list failed")[:200])
    try:
        raw = json.loads(out or "[]")
    except ValueError as exc:
        return MergedHeads(failed=f"gh returned unreadable JSON: {exc}")
    if not isinstance(raw, list):
        return MergedHeads(failed="gh returned JSON that is not a list of pull requests")
    heads: dict[str, set[str]] = {}
    for item in raw:
        # A fork PR's head lives in ANOTHER repository, so a local branch sharing its name is a
        # different branch and its merge says nothing about ours. A missing field reads as
        # cross-repo, which can only keep a branch.
        if not isinstance(item, dict) or item.get("isCrossRepository", True) is not False:
            continue
        name, oid = str(item.get("headRefName") or ""), str(item.get("headRefOid") or "")
        if name and oid:
            heads.setdefault(name, set()).add(oid)
    return MergedHeads(heads={k: frozenset(v) for k, v in heads.items()})


def live_remote_heads(repo: Path) -> set[str] | None:
    """Branch names the remote actually has. `None` when the remote could not be asked.

    Returning None rather than an empty set is the whole safety of this function, and the
    distinction is only meaningful if callers can trust it - so it is asserted directly by
    `test_a_down_remote_yields_none_not_an_empty_set`, against a real unreachable remote rather
    than a stand-in. An empty set means "the remote has no branches", under which EVERY tracking
    ref is stale; a failed lookup must never be able to author that.
    """
    proc = _git(repo, "ls-remote", "--heads", REMOTE)
    if proc.returncode != 0:
        return None
    heads = set()
    for line in proc.stdout.splitlines():
        _, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            heads.add(ref[len("refs/heads/") :])
    return heads


def local_tracking_refs(repo: Path) -> list[tuple[str, str]]:
    """``(branch, sha)`` for each ref this checkout believes `origin` has.

    Reads the FULL refname, not `%(refname:short)`. Git shortens `refs/remotes/origin/HEAD` to
    plain ``origin`` - not ``origin/HEAD`` - so a short-name exclusion for ``origin/HEAD`` never
    fires and only looks like it protects the symref. Excluding the full path is the check that
    actually holds, and it matters: `origin/HEAD` is a symref into another ref, and deleting it
    breaks bare `git log origin` for every lane sharing the checkout.
    """
    proc = _git(repo, "for-each-ref", "--format=%(refname)%09%(objectname)", f"refs/remotes/{REMOTE}")
    if proc.returncode != 0:
        return []
    prefix = f"refs/remotes/{REMOTE}/"
    out = []
    for line in proc.stdout.splitlines():
        refname, _, sha = line.partition("\t")
        if refname == f"{prefix}HEAD" or not refname.startswith(prefix):
            continue
        out.append((refname[len(prefix) :], sha.strip()))
    return out


def fetch_prune_enabled(repo: Path) -> bool:
    proc = _git(repo, "config", "--get", "fetch.prune")
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def nonstandard_refspecs(repo: Path) -> list[str]:
    """Fetch refspecs beyond the standard branch mapping.

    A refspec like `+refs/pull/*/head:refs/remotes/origin/pr/*` puts refs that are NOT branches
    under `refs/remotes/origin/`, where `ls-remote --heads` can never see them - so they would be
    classified stale and deleted while perfectly alive.
    """
    proc = _git(repo, "config", "--get-all", f"remote.{REMOTE}.fetch")
    if proc.returncode != 0:
        return []
    return [s.strip() for s in proc.stdout.splitlines() if s.strip() and s.strip() != STANDARD_REFSPEC]


def local_branches(repo: Path) -> list[str]:
    """Local branch names, from the FULL refname.

    `%(refname:short)` disambiguates against tags: with a tag `t` present, branch `t` shortens to
    `heads/t`, a name that matches no merged PR head and no worktree line.
    """
    return list(local_branch_tips(repo))


def local_branch_tips(repo: Path) -> dict[str, str]:
    """``branch -> full sha`` for every local branch. Empty when unreadable, which deletes nothing."""
    proc = _git(repo, "for-each-ref", "--format=%(refname)%09%(objectname)", "refs/heads")
    if proc.returncode != 0:
        return {}
    out = {}
    for line in proc.stdout.splitlines():
        refname, _, sha = line.partition("\t")
        if refname.startswith("refs/heads/"):
            out[refname[len("refs/heads/") :]] = sha.strip()
    return out


def worktree_branches(repo: Path) -> Optional[set[str]]:
    """Branches checked out in any worktree of *repo*, or None when the list could not be read.

    The deletion guard uses this rather than `checked_out_branches`, because the two need opposite
    failure directions: for the report an unreadable list safely means "nothing protected", while
    for a deletion it would mean "nothing protected" too - which strands a lane's worktree. None
    lets the deleter refuse.

    THE PORCELAIN `branch` LINE IS NOT THE WHOLE ANSWER. A worktree stopped mid-rebase or
    mid-bisect shows as detached, yet the branch is still in use there: the rebase writes it back
    when it finishes, and git's own `branch -D` refuses it. So each worktree's git dir is read for
    the branch those operations hold. A worktree whose git dir cannot be resolved makes the whole
    answer None, because what it holds is unknown and unknown must keep, not delete.
    """
    proc = _git(repo, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return None
    held: set[str] = set()
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(line[len("worktree ") :])
        elif line.startswith("branch refs/heads/"):
            held.add(line[len("branch refs/heads/") :])
    for path in paths:
        in_use = _branches_held_by_operations(Path(path))
        if in_use is None:
            return None
        held |= in_use
    return held


def _branches_held_by_operations(worktree: Path) -> Optional[set[str]]:
    """Branches an in-progress rebase or bisect in *worktree* holds, or None when unreadable."""
    proc = _git(worktree, "rev-parse", "--git-dir")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    git_dir = Path(proc.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir
    if not git_dir.is_dir():
        return None
    held = set()
    for rel in ("rebase-merge/head-name", "rebase-apply/head-name", "BISECT_START"):
        f = git_dir / rel
        if not f.exists():
            continue
        try:
            name = f.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        # head-name carries a full refname; BISECT_START carries a short branch name or a sha.
        # A sha held as a "branch" matches no local branch, so it protects nothing and harms
        # nothing.
        if name.startswith("refs/heads/"):
            name = name[len("refs/heads/") :]
        if name and name != "detached HEAD":
            held.add(name)
    return held


def checked_out_branches(repo: Path) -> set[str]:
    """Branch names currently checked out in SOME worktree of *repo*, including this one.

    Needed because "no branch on origin" and "abandoned" are different claims, and the sweep
    cannot tell them apart from refs alone: a branch that was never pushed looks identical to
    one pushed and deleted after merge (gh#306). A branch someone has checked out right now is
    the one case where the difference is knowable for free.

    THIS IS ALWAYS NON-EMPTY IN PRACTICE, and that is the point rather than an edge case: the
    lane running this sweep is itself in a worktree on its own unpushed branch, so a report that
    did not exclude these would name the reader's live work as debris every single time.

    An unreadable answer yields the EMPTY set, which is the safe direction here: it can only put
    a branch back into the plain list, never take a live one out of protection.
    """
    held = worktree_branches(repo)
    return held if held is not None else set()


def _classify_local(
    candidates: list[str], tips: dict[str, str], merged: MergedHeads
) -> tuple[list[tuple[str, str]], list[str]]:
    """Split candidates (no head on origin, not checked out) into deletable and kept-with-reason.

    Guard 2 lives here: the tip must equal a merged PR's `headRefOid` under the same head name.
    """
    deletable, kept = [], []
    for name in candidates:
        tip = tips.get(name, "")
        oids = (merged.heads or {}).get(name)
        if not tip:
            kept.append(f"{name}: tip unreadable - kept")
        elif not oids:
            kept.append(f"{name}: no merged PR has this head name - never merged, kept")
        elif tip in oids:
            deletable.append((name, tip))
        else:
            heads = ", ".join(sorted(o[:8] for o in oids))
            kept.append(
                f"{name}: merged but moved - tip {tip[:8]} is not the merged PR head ({heads}), "
                "so it may hold unmerged work; kept"
            )
    return deletable, kept


def plan(repo: Path, base: str = "master", pr_reader: Optional[PrReader] = None) -> PrunePlan:
    """Decide what a sweep WOULD do. No writes, so the report and the apply agree.

    ``base`` is the project's base branch, never a deletion candidate. ``pr_reader`` supplies the
    merged-PR heads; without one, no local branch is deletable and the report says why.
    """
    p = PrunePlan()
    tracking = local_tracking_refs(repo)

    extra = nonstandard_refspecs(repo)
    heads = live_remote_heads(repo)

    if heads is None:
        p.refused.append(
            f"could not reach {REMOTE} (`git ls-remote` failed) - refusing to classify any "
            "tracking ref as stale, because an unanswered remote reads as a remote with no "
            "branches and would condemn every ref at once"
        )
    elif not heads and tracking:
        # ls-remote EXITS 0 AND PRINTS NOTHING for a remote with no branches, so returncode
        # alone does not separate "reachable and empty" from "reachable and populated". This
        # arm is the one an adversarial pass found: without it a repointed or freshly-created
        # origin condemns every ref including master.
        p.refused.append(
            f"{REMOTE} reports ZERO branches while this checkout tracks {len(tracking)} - "
            "refusing to treat that as 'they were all deleted'. A remote that is reachable but "
            "empty (repointed url, fresh repo, wrong remote) looks identical to one that lost "
            "every branch, and only one of those should delete anything"
        )
    elif extra:
        p.refused.append(
            f"{REMOTE} has {len(extra)} non-standard fetch refspec(s): {'; '.join(extra)}. "
            f"Refs mapped into refs/remotes/{REMOTE}/ by anything other than "
            f"'{STANDARD_REFSPEC}' are not branches, so `ls-remote --heads` cannot see them and "
            "they would be classified stale while alive"
        )
    else:
        p.prunable = sorted((n, s) for n, s in tracking if n not in heads)

    if not fetch_prune_enabled(repo):
        p.refused.append(
            "fetch.prune is not set on this checkout, so this debris returns after every merge "
            "and this sweep must keep running forever. The durable cure is one reversible "
            "command - `git -C <checkout> config fetch.prune true` - but it changes a checkout "
            "six lanes share, and gh#116 was escalated rather than applying it unilaterally. "
            "Not applied here by design; a human decides (gh#116)"
        )

    locals_ = [b for b in local_branches(repo) if b != base]
    if locals_:
        # ASKED OF THE REMOTE, NOT OF THE DEBRIS. This read `set(locals_) & set(p.names)` —
        # local branches intersected with STALE TRACKING REFS, which are the artifact this same
        # sweep exists to prune. A branch whose tracking ref was already pruned, or that never
        # had one, was structurally invisible to it (gh#306). Measured 2026-09-08: 41 local
        # branches have no branch on origin while stale tracking refs number ZERO, so the old
        # derivation reported 0 of 41 — the under-count degenerates to total silence exactly
        # once the sweep succeeds at its other job. `heads` is already in hand for the tracking
        # half; this uses the authoritative source for both instead of one via the other.
        #
        # ONLY WHEN THE REMOTE REALLY ANSWERED. `live_remote_heads` returns None for an
        # unreachable remote precisely so no caller can read silence as "they were all deleted",
        # and an EMPTY answer is the same trap one step further in: a repointed url or a fresh
        # repo is indistinguishable from a remote that lost every branch. Either would condemn
        # every local branch at once here, which is the failure this function's neighbour
        # already refuses. A non-standard refspec does NOT suppress it: that doubt is about refs
        # mapped into refs/remotes/, and a local branch name is not one.
        remote_authoritative = bool(heads)
        gone = sorted(set(locals_) - heads) if remote_authoritative else []
        # "No branch on origin" is NOT evidence of abandonment, and the sweep can prove it for
        # one class for free: a branch checked out in a live worktree is being worked on now.
        # Never pushed and pushed-then-deleted are otherwise indistinguishable by this predicate.
        held = worktree_branches(repo)
        live = sorted(set(gone) & (held or set()))
        abandoned = [b for b in gone if b not in set(live)]
        p.kept.extend(f"{b}: checked out in a worktree - kept" for b in live)

        # Guards 1 and 3 are already applied: `abandoned` has no head on origin and is in no
        # worktree. A deletion needs a READ worktree list and a COMPLETED merged-PR read; either
        # missing deletes nothing, and a failed read is a refusal so a blind sweep cannot exit 0.
        deletion_refusal = ""
        if abandoned:
            if held is None:
                deletion_refusal = (
                    "could not list worktrees (`git worktree list` failed) - deleting no local "
                    "branch, because a branch checked out in an unseen worktree would be stranded"
                )
            else:
                merged = pr_reader() if pr_reader is not None else MergedHeads(
                    unavailable="no merged-PR reader was supplied"
                )
                if merged.failed:
                    deletion_refusal = (
                        f"the merged-PR read failed ({merged.failed}) - deleting no local branch, "
                        "because a branch is deletable only when its tip is a merged PR's head"
                    )
                elif merged.heads is None:
                    p.kept.append(
                        f"no local branch is deletable: {merged.unavailable}. "
                        f"{len(abandoned)} candidate(s) kept"
                    )
                else:
                    p.deletable, kept = _classify_local(
                        abandoned, local_branch_tips(repo), merged
                    )
                    p.kept.extend(kept)
        if deletion_refusal:
            p.refused.append(deletion_refusal)
        detail = (
            f" Of these, {len(gone)} {'has' if len(gone) == 1 else 'have'} no branch on {REMOTE}"
            + (
                f", but {len(live)} of those {'is' if len(live) == 1 else 'are'} CHECKED OUT in a "
                f"live worktree and {'is' if len(live) == 1 else 'are'} not debris "
                f"({', '.join(live)})"
                if live
                else ""
            )
            + (
                # "remaining" is relative to the live branches the PRECEDING clause just
                # subtracted - and that clause does not render when nothing is live. Saying it
                # anyway puts an unsubtracted count next to a different number in one sentence,
                # which is how gh#436 sent a reader looking for a set that was never named.
                f". {'The remaining' if live else 'All'} {len(abandoned)} "
                f"({', '.join(abandoned)}) - "
                "the deletion candidates, since `--delete-branch` removes the remote branch and "
                "leaves the local one."
                if abandoned
                else "."
            )
            if gone
            else ""
        )
        p.reported.append(
            f"{len(locals_)} local branch(es) besides {base}: {', '.join(sorted(locals_))}."
            f"{detail} A candidate is deleted under --apply only when its tip is a merged PR's "
            "headRefOid; every other one is kept, with its reason listed"
        )
    return p


def apply(repo: Path, p: PrunePlan) -> tuple[int, list[str]]:
    """Delete the planned tracking refs, then the planned local branches. Returns (failures, lines).

    Each deletion passes the sha the PLAN saw, so `update-ref -d <ref> <sha>` refuses if the ref
    moved in between. Without it a branch that came back between plan and apply is deleted while
    live, and reported as a clean `pruned` line - a wrong deletion invisible in the output.

    A local branch is deleted the same way, against the tip the plan matched to a merged PR head,
    and the worktree list is read AGAIN immediately before each one: a lane that checked the
    branch out after the plan would otherwise lose it from under its worktree, since `update-ref`
    does not protect a checked-out branch the way `branch -d` does.

    Only `prunable` and `deletable` are touched. Refusals are never applied; that is what makes
    them refusals rather than a slower kind of automation.
    """
    failures, lines = 0, []
    for name, sha in p.prunable:
        proc = _git(repo, "update-ref", "-d", f"refs/remotes/{REMOTE}/{name}", sha)
        if proc.returncode == 0:
            lines.append(f"  pruned {REMOTE}/{name}")
        else:
            failures += 1
            lines.append(
                f"  FAILED {REMOTE}/{name}: {(proc.stderr or '').strip()} "
                "(the ref moved since the plan was made - re-run rather than forcing)"
            )
    for name, sha in p.deletable:
        held = worktree_branches(repo)
        if held is None or name in held:
            failures += 1
            why = "the worktree list could not be read" if held is None else (
                "it is now checked out in a worktree"
            )
            lines.append(f"  KEPT {name}: {why} - not deleted")
            continue
        proc = _git(repo, "update-ref", "-d", f"refs/heads/{name}", sha)
        if proc.returncode == 0:
            lines.append(f"  deleted {name} @ {sha[:8]} (merged PR head, no head on {REMOTE})")
        else:
            failures += 1
            lines.append(
                f"  FAILED {name}: {(proc.stderr or '').strip()} "
                "(the branch moved since the plan was made - re-run rather than forcing)"
            )
    return failures, lines


def main(argv=None) -> int:
    """The CLI. Report-only unless `--apply` is typed.

    EXIT CODE, matching `agentflow.backups` so the steward reads one contract:

    * ``0`` - nothing pending and nothing refused.
    * ``1`` - refs or merged local branches are pending, an apply failed, or a refusal is
      standing. A refusal is
      non-zero in BOTH modes: it is the half a human owns, and a sweep that could clear it by
      running would not be refusing anything.
    * ``2`` - the command line was malformed, the project could not be resolved, or the
      checkout does not exist.
    """
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    ap = argparse.ArgumentParser(prog="python -m agentflow.refs", description=__doc__)
    ap.add_argument(
        "--project",
        help="project whose canonical_repo.local_checkout to sweep (default: the resolved "
        "session project, same precedence every other agentflow CLI uses)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="delete the stale tracking refs and the merged local branches that pass every "
        "guard (default: report only). Never applies a refusal.",
    )
    args = ap.parse_args(argv)

    from .config import ConfigError, load_environment, load_project

    try:
        proj = load_project(args.project)
    except ConfigError as exc:
        # Exit 2, not a traceback: an unresolvable project is a malformed call, and the
        # docstring above promises 2 for those.
        print(f"could not resolve project: {exc}")
        return 2

    repo = Path(proj.canonical_repo.local_checkout).expanduser()
    if not (repo / ".git").exists():
        print(f"not a git checkout: {repo}")
        return 2

    base = proj.canonical_repo.base_branch
    try:
        host = load_environment().git_host
        kind, cli = host.kind, host.cli
        env_error = ""
    except ConfigError as exc:
        kind, cli, env_error = "", "", f"environment unreadable: {exc}"

    def pr_reader() -> MergedHeads:
        if env_error:
            return MergedHeads(unavailable=env_error)
        return merged_pr_heads(proj.canonical_repo.host_ref, kind, cli)

    p = plan(repo, base=base, pr_reader=pr_reader)
    print(f"# agentflow refs sweep - {repo}")

    if p.prunable:
        print(f"stale tracking refs ({len(p.prunable)}):")
        for name, sha in p.prunable:
            print(f"  {REMOTE}/{name} @ {sha[:8]}")
    else:
        print("stale tracking refs: none")

    if p.deletable:
        print(f"merged local branches to delete ({len(p.deletable)}):")
        for name, sha in p.deletable:
            print(f"  {name} @ {sha[:8]} - tip is a merged PR head, no head on {REMOTE}, "
                  "in no worktree")
    else:
        print("merged local branches to delete: none")
    for line in p.kept:
        print(f"  kept: {line}")

    failures = 0
    if args.apply and (p.prunable or p.deletable):
        failures, lines = apply(repo, p)
        print("applied:")
        for line in lines:
            print(line)

    for line in p.reported:
        print(f"note: {line}")
    for line in p.refused:
        print(f"REFUSED: {line}")

    if failures:
        return 1
    if args.apply:
        # Re-planning after the write is what makes "clean" a measurement rather than a hope.
        return 0 if plan(repo, base=base, pr_reader=pr_reader).is_clean else 1
    return 0 if p.is_clean else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
