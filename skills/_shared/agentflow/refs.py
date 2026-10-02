"""Stale remote-tracking refs in the canonical checkout, and the one-line cure nobody applied.

GitHub deletes the remote branch when a PR merges, but a checkout that does not prune on fetch
keeps the `refs/remotes/origin/*` ref pointing at nothing. Every `git branch -a` and every ref
enumeration then reads a deleted branch as a live one. The steward hand-pruned the same debris on
three consecutive rotations - 21 refs - before this was filed (gh#116).

THIS SWEEP FIXES ONE OF gh#116's TWO CAUSES. The other - merged LOCAL branches nobody deletes - is
STILL LIVE and is deliberately not acted on here. `gh pr merge --delete-branch` removes the REMOTE
branch only, which is precisely why a merged branch shows up twice in one run: `origin/<name>` as
stale, and `<name>` sitting in the local list. Whether a post-merge local branch is worth keeping
briefly for recovery is gh#116's open question, and deleting local branches is a wider destructive
reach than removing refs git already knows are dead. So local branches are REPORTED, with the ones
whose remote is gone called out as the likely debris, and never removed.

`fetch.prune` IS THE ACTUAL CURE FOR THIS HALF, AND THIS TOOL DELIBERATELY WILL NOT APPLY IT.
Setting it removes only refs git already knows are gone, and it ends the accumulation permanently
instead of sweeping it forever. But it changes the behaviour of a checkout six lanes share, and the
steward escalated gh#116 rather than setting it unilaterally for exactly that reason. So an unset
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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: The remote every lane pushes to. Named once so a second remote cannot be swept by accident.
REMOTE = "origin"

#: The only fetch refspec under which "a ref under refs/remotes/origin/ is a branch" holds.
STANDARD_REFSPEC = f"+refs/heads/*:refs/remotes/{REMOTE}/*"


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

    Three buckets, because they have three different owners:

    * ``prunable`` - stale tracking refs as ``(name, sha)``. This tool removes them under
      ``--apply``, and carries the sha so the removal can refuse a ref that moved since.
    * ``refused`` - findings only a human may act on. Never applied, never cleared by a run.
    * ``reported`` - evidence, no action implied and no effect on the exit code.
    """

    prunable: list[tuple[str, str]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    reported: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self.prunable]

    @property
    def is_clean(self) -> bool:
        """Nothing pending AND nothing refused.

        `reported` is excluded on purpose: it carries evidence the sweep is not entitled to act
        on, so letting it redden the exit would make a clean checkout permanently non-zero and
        train the reader to ignore the code.
        """
        return not self.prunable and not self.refused


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
    proc = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return proc.stdout.split() if proc.returncode == 0 else []


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
    proc = _git(repo, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return set()
    return {
        line[len("branch refs/heads/") :]
        for line in proc.stdout.splitlines()
        if line.startswith("branch refs/heads/")
    }


def plan(repo: Path) -> PrunePlan:
    """Pure: decide what a sweep WOULD do. No side effects, so the report and the apply agree."""
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

    locals_ = [b for b in local_branches(repo) if b != "master"]
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
        live = sorted(set(gone) & checked_out_branches(repo))
        abandoned = [b for b in gone if b not in set(live)]
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
                "the likely merged-and-abandoned debris, since `--delete-branch` removes the "
                "remote branch and leaves the local one."
                if abandoned
                else "."
            )
            if gone
            else ""
        )
        p.reported.append(
            f"{len(locals_)} local branch(es) besides master: {', '.join(sorted(locals_))}."
            f"{detail} Evidence only - never deleted here. Whether a post-merge local branch is "
            "worth keeping briefly for recovery is gh#116's open question"
        )
    return p


def apply(repo: Path, p: PrunePlan) -> tuple[int, list[str]]:
    """Delete the planned tracking refs. Returns (failures, lines).

    Each deletion passes the sha the PLAN saw, so `update-ref -d <ref> <sha>` refuses if the ref
    moved in between. Without it a branch that came back between plan and apply is deleted while
    live, and reported as a clean `pruned` line - a wrong deletion invisible in the output.

    Only `prunable` is touched. Refusals are never applied; that is what makes them refusals
    rather than a slower kind of automation.
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
    return failures, lines


def main(argv=None) -> int:
    """The CLI. Report-only unless `--apply` is typed.

    EXIT CODE, matching `agentflow.backups` so the steward reads one contract:

    * ``0`` - nothing pending and nothing refused.
    * ``1`` - refs are pending, an apply failed, or a refusal is standing. A refusal is
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
        help="delete the stale tracking refs (default: report only). Never applies a refusal.",
    )
    args = ap.parse_args(argv)

    from .config import ConfigError, load_project

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

    p = plan(repo)
    print(f"# agentflow refs sweep - {repo}")

    if p.prunable:
        print(f"stale tracking refs ({len(p.prunable)}):")
        for name, sha in p.prunable:
            print(f"  {REMOTE}/{name} @ {sha[:8]}")
    else:
        print("stale tracking refs: none")

    failures = 0
    if args.apply and p.prunable:
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
        return 0 if plan(repo).is_clean else 1
    return 0 if p.is_clean else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
