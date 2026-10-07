"""gh#116: the ref sweep, and the several ways it could delete something alive.

Every test drives REAL git against a real local bare remote - no network, and no mock standing in
for git. A monkeypatch stands only where a seam cannot be driven honestly - the project config,
the `gh` merged-PR reader (no test may reach the network) and a failing worktree list - and says
why at the call site. The property a monkeypatch once hid (`live_remote_heads` returning None
rather than an empty set) is asserted directly against a genuinely unreachable remote, because a test that replaces the function under
test cannot hold that function's contract.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from agentflow import refs


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def _init(path: Path, bare: bool = False) -> Path:
    subprocess.run(
        ["git", "init", *(["--bare"] if bare else []), "-b", "master", str(path)],
        check=True, capture_output=True,
    )
    if not bare:
        _configure(path)
    return path


def _configure(path: Path) -> None:
    """Identity, and signing pinned off so a machine with global commit signing still passes."""
    for k, v in (("user.email", "t@e.st"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(path, "config", k, v)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A clone of a bare remote carrying master plus one feature branch."""
    bare, seed = _init(tmp_path / "remote.git", bare=True), _init(tmp_path / "seed")
    (seed / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "master")
    _git(seed, "checkout", "-b", "feature/live")
    _git(seed, "push", "-u", "origin", "feature/live")

    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    _configure(work)
    _git(work, "fetch", "origin")
    return work


def _drop_remote_branch(checkout: Path, name: str) -> None:
    bare = _git(checkout, "remote", "get-url", "origin").strip()
    subprocess.run(["git", "-C", bare, "update-ref", "-d", f"refs/heads/{name}"],
                   check=True, capture_output=True)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def test_a_ref_the_remote_still_has_is_not_stale(checkout):
    assert refs.plan(checkout).names == []


def test_a_ref_the_remote_deleted_is_stale_and_prunable(checkout):
    """The gh#116 shape: the remote branch goes away on merge, the tracking ref survives."""
    _drop_remote_branch(checkout, "feature/live")
    p = refs.plan(checkout)
    assert p.names == ["feature/live"], p.prunable
    assert all(len(sha) == 40 for _, sha in p.prunable), "the plan carries the sha it saw"
    assert not p.is_clean


# --------------------------------------------------------------------------- #
# The three ways an impoverished view of the remote could delete everything
# --------------------------------------------------------------------------- #
def test_a_down_remote_yields_none_not_an_empty_set(checkout):
    """`live_remote_heads`'s OWN contract, held against a real unreachable remote.

    This is the assertion that was missing: the unreachable-remote test below monkeypatches this
    function, so it proved `plan()` handles None and proved nothing about who produces None.
    Mutating the `return None` to `return set()` left that suite fully green while every ref -
    master included - became prunable.
    """
    _git(checkout, "remote", "set-url", "origin", str(checkout / "does-not-exist"))
    assert refs.live_remote_heads(checkout) is None


def test_an_unreachable_remote_refuses_and_condemns_nothing(checkout):
    """A transport failure must refuse, not classify. Driven by a real dead url, not a mock."""
    _git(checkout, "remote", "set-url", "origin", str(checkout / "does-not-exist"))
    p = refs.plan(checkout)
    assert p.prunable == [], "a failed remote lookup must condemn nothing"
    assert any("refusing to classify" in r for r in p.refused)
    assert not p.is_clean


def test_a_reachable_but_empty_remote_condemns_nothing(checkout, tmp_path):
    """`ls-remote --heads` EXITS 0 AND PRINTS NOTHING for a remote with no branches.

    So the returncode check alone cannot separate "reachable and empty" from "reachable and
    populated", and without this arm a repointed or freshly-created origin makes every tracking
    ref stale at once - master included. Reachable via a `remote set-url`, a repo rename, or a
    fresh cut of the repository.
    """
    empty = _init(tmp_path / "empty.git", bare=True)
    _git(checkout, "remote", "set-url", "origin", str(empty))

    assert refs.live_remote_heads(checkout) == set(), "reachable, and genuinely empty"
    p = refs.plan(checkout)
    assert p.prunable == [], "an empty remote is not evidence that every branch was deleted"
    assert any("ZERO branches" in r for r in p.refused)


def test_a_nonstandard_fetch_refspec_refuses(checkout):
    """Refs mapped under refs/remotes/origin/ that are not branches are invisible to ls-remote.

    The usual example is `+refs/pull/*/head:refs/remotes/origin/pr/*`. Those refs are alive and
    would be classified stale, so the sweep refuses to classify anything under that config.
    """
    _git(checkout, "config", "--add", "remote.origin.fetch",
         "+refs/pull/*/head:refs/remotes/origin/pr/*")
    assert refs.nonstandard_refspecs(checkout)
    p = refs.plan(checkout)
    assert p.prunable == []
    assert any("non-standard fetch refspec" in r for r in p.refused)


def test_origin_head_is_excluded_by_full_refname_not_by_its_short_name(checkout):
    """Git shortens `refs/remotes/origin/HEAD` to `origin`, NEVER to `origin/HEAD`.

    So a short-name exclusion for "origin/HEAD" can never fire, and a guard written that way
    only looks like it protects the symref. This asserts the shortening AND the exclusion, so
    the test cannot pass for a different reason than the one it names.
    """
    _git(checkout, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
    shorts = _git(checkout, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    assert "origin\n" in shorts, "git really does shorten the symref to a bare 'origin'"

    names = [n for n, _ in refs.local_tracking_refs(checkout)]
    assert "HEAD" not in names and "origin" not in names
    assert "master" in names, "the exclusion is narrow, not a filter that drops everything"


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #
def test_apply_removes_only_the_prunable_refs(checkout):
    _drop_remote_branch(checkout, "feature/live")
    before = {n for n, _ in refs.local_tracking_refs(checkout)}
    failures, _ = refs.apply(checkout, refs.plan(checkout))
    after = {n for n, _ in refs.local_tracking_refs(checkout)}

    assert failures == 0
    assert before - after == {"feature/live"}, "exactly the stale ref, nothing else"
    assert "master" in after, "a live ref survived the sweep"
    assert refs.plan(checkout).names == []


def test_apply_refuses_a_ref_that_moved_since_the_plan(checkout):
    """The plan carries a sha, so a branch that came back between plan and apply is not deleted.

    Without the sha this reported a clean `pruned` line while deleting a ref the remote had
    again - a wrong deletion invisible in the output.
    """
    _drop_remote_branch(checkout, "feature/live")
    p = refs.plan(checkout)
    assert p.names == ["feature/live"]

    # The ref comes back at a DIFFERENT commit, exactly as a re-push would leave it.
    (checkout / "again.txt").write_text("x\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-m", "moved")
    _git(checkout, "update-ref", "refs/remotes/origin/feature/live", "HEAD")

    failures, lines = refs.apply(checkout, p)
    assert failures == 1, lines
    assert "feature/live" in {n for n, _ in refs.local_tracking_refs(checkout)}


def test_a_standing_refusal_keeps_the_run_non_clean_even_after_a_successful_apply(checkout):
    """The property that makes a refusal a refusal rather than slower automation.

    `fetch.prune` is still unset here, so the sweep cannot report a clean tree however many refs
    it removed. If this ever passes, an unattended run has quietly finished a job a human owns.
    """
    _drop_remote_branch(checkout, "feature/live")
    refs.apply(checkout, refs.plan(checkout))
    after = refs.plan(checkout)
    assert after.prunable == []
    assert after.refused, "fetch.prune is still unset"
    assert not after.is_clean


# --------------------------------------------------------------------------- #
# fetch.prune and the evidence bucket
# --------------------------------------------------------------------------- #
def test_fetch_prune_unset_is_a_standing_refusal(checkout):
    assert refs.fetch_prune_enabled(checkout) is False
    assert any("fetch.prune" in r for r in refs.plan(checkout).refused)


def test_fetch_prune_set_clears_that_refusal(checkout):
    _git(checkout, "config", "fetch.prune", "true")
    assert refs.fetch_prune_enabled(checkout) is True
    assert not any("fetch.prune" in r for r in refs.plan(checkout).refused)


def test_reported_evidence_does_not_redden_a_clean_tree(checkout):
    """`reported` carries things the sweep may not act on, so it must not drive the exit code."""
    _git(checkout, "config", "fetch.prune", "true")
    _git(checkout, "branch", "some/local-work")

    p = refs.plan(checkout)
    assert p.reported, "the local branch is reported"
    assert p.is_clean, "but evidence alone is not a finding"


def test_a_local_branch_whose_remote_is_gone_is_named_as_likely_debris(checkout):
    """gh#116's SECOND cause, reported accurately rather than assumed away.

    `gh pr merge --delete-branch` removes the REMOTE branch only, so a merged branch appears
    twice in one run: `origin/<name>` stale, and `<name>` still local. An earlier draft of this
    module claimed lanes delete their own local branch and that the cause had self-corrected;
    two of the four local branches in the live checkout had merged PRs, so it had not.
    """
    _git(checkout, "branch", "feature/live", "origin/feature/live")
    _drop_remote_branch(checkout, "feature/live")

    p = refs.plan(checkout)
    assert p.names == ["feature/live"]
    note = " ".join(p.reported)
    # "any more" is gone from the wording, deliberately: it asserts the branch ONCE had a
    # remote, and a never-pushed branch never did. gh#306's whole point is that the two are
    # indistinguishable by this predicate, so the line no longer claims to know which.
    assert "no branch on origin" in note and "feature/live" in note
    assert "feature/live" in refs.local_branches(checkout), "reported, never deleted"


# --------------------------------------------------------------------------- #
# gh#306 - the orphan count is asked of the remote, not of the debris
#
# It read `set(locals_) & set(p.names)`: local branches intersected with STALE TRACKING REFS,
# the artifact this sweep exists to prune. Measured on the live checkout 2026-09-08: 41 local
# branches with no branch on origin, ZERO stale tracking refs, so the old derivation reported
# 0 of 41 - the under-count becomes total silence exactly once the sweep succeeds at its other
# job.
# --------------------------------------------------------------------------- #
def _detail(p) -> str:
    return " ".join(p.reported)


def test_the_orphan_count_is_asked_of_the_remote_not_of_the_tracking_refs(checkout):
    """THE DEFECT ITSELF. This branch has no remote-tracking ref at all, so the old derivation
    could not see it however many there were: `p.names` is empty here and stays empty."""
    _git(checkout, "branch", "never-pushed", "master")

    p = refs.plan(checkout)

    assert p.names == [], "no tracking ref is stale - the old source of the count is empty"
    assert "never-pushed" in _detail(p)
    assert "1 has no branch on origin" in _detail(p)


def test_an_unreachable_remote_names_no_branch_as_orphaned(checkout):
    """The inversion this fix could have introduced, pinned so it cannot.

    `live_remote_heads` returns None for an unreachable remote precisely so nobody reads silence
    as "they were all deleted". Subtracting a None-shaped answer from the local branches would
    condemn EVERY branch at once - a louder version of the bug being fixed.
    """
    _git(checkout, "branch", "never-pushed", "master")
    _git(checkout, "remote", "set-url", "origin", str(checkout / "does-not-exist.git"))

    p = refs.plan(checkout)

    assert refs.live_remote_heads(checkout) is None
    assert "no branch on origin" not in _detail(p)
    assert any("could not reach" in r for r in p.refused)


def test_a_reachable_but_empty_remote_names_no_branch_as_orphaned(checkout, tmp_path):
    """The same trap one step in: a repointed url or a fresh repo answers 0 branches, which is
    indistinguishable from a remote that lost every branch. Either would make every local branch
    look orphaned, so an empty answer classifies nothing here just as it prunes nothing."""
    _git(checkout, "branch", "never-pushed", "master")
    empty = _init(tmp_path / "empty2.git", bare=True)
    _git(checkout, "remote", "set-url", "origin", str(empty))

    p = refs.plan(checkout)

    assert refs.live_remote_heads(checkout) == set()
    assert "no branch on origin" not in _detail(p)


def test_a_branch_checked_out_in_a_live_worktree_is_not_called_debris(checkout, tmp_path):
    """"No branch on origin" is not evidence of abandonment, and this is the one class where the
    sweep can prove it for free.

    NOT AN EDGE CASE: the lane running the sweep is itself in a worktree on its own unpushed
    branch, so without this the report names the reader's live work as debris on every run. On
    the live checkout that was exactly 1 of 41, and it was the branch of the seat running it.
    """
    _git(checkout, "worktree", "add", str(tmp_path / "wt"), "-b", "being-built-now")

    p = refs.plan(checkout)
    note = _detail(p)

    assert "being-built-now" in refs.checked_out_branches(checkout)
    assert "CHECKED OUT in a live worktree" in note
    assert "being-built-now" in note
    # ...and it is kept out of the sentence that calls branches abandoned debris.
    debris = note.split("The remaining")[1] if "The remaining" in note else ""
    assert "being-built-now" not in debris


def test_with_nothing_checked_out_the_debris_count_is_not_called_remaining(checkout):
    """gh#436: "remaining" asserts a subtraction that only happened when something was live.

    The live-worktree clause renders only when `live` is non-empty, so with nothing checked out
    the sentence went straight from "65 local branches... 54 have no branch on origin" to "The
    remaining 54", naming the same fifty-four it had just counted. The arithmetic a reader does
    (65 - 54 = 11) has no set behind it. The census was right both times; only the label lied.
    """
    _git(checkout, "branch", "feature/live", "origin/feature/live")
    _drop_remote_branch(checkout, "feature/live")

    p = refs.plan(checkout)
    note = _detail(p)

    assert refs.checked_out_branches(checkout) & {"feature/live"} == set(), "nothing live here"
    assert "CHECKED OUT in a live worktree" not in note, "the subtracting clause did not render"
    assert "The remaining" not in note, "so nothing was subtracted and nothing may claim it was"
    assert "All 1 (feature/live)" in note


def test_a_nonstandard_refspec_does_not_suppress_the_orphan_count(checkout):
    """A deliberate ASYMMETRY, stated so it is not read as an oversight.

    That refusal exists because refs mapped into `refs/remotes/origin/` by a non-standard
    refspec are not branches and would be classified stale while alive. A LOCAL branch name is
    not such a ref, so the doubt does not reach it. Suppressing this count there would repeat
    gh#306's own mistake - deriving one answer from an artifact of a different question.
    """
    _git(checkout, "branch", "never-pushed", "master")
    _git(checkout, "config", "--add", "remote.origin.fetch",
         "+refs/pull/*/head:refs/remotes/origin/pr/*")

    p = refs.plan(checkout)

    assert p.prunable == [], "tracking refs stay unclassified, as before"
    assert any("non-standard fetch refspec" in r for r in p.refused)
    # ASSERT THE COUNT SENTENCE, not the branch name. The name appears in the always-present
    # "N local branch(es) besides master: ..." list either way, so `"never-pushed" in note` was
    # vacuous - it passed with the count suppressed. A mutation run found that; the earlier
    # version of this test proved nothing.
    assert "1 has no branch on origin" in _detail(p), "the count is unaffected by that doubt"


# --------------------------------------------------------------------------------------------- #
# gh#492: the refusal STRUCTURE, and the steward paragraph pinned to it.
#
# The paragraph said "three cases refuse" while `p.refused.append` appears four times, and nothing
# anywhere read that paragraph - so a reader grepping the code could not reconcile the two and
# filed the doc as off by one. BOTH numbers were right under their own predicate: three
# impoverished-view cases in one exclusive chain, plus one independently guarded refusal. What was
# missing was never the right number, it was a guard that moves the prose when the code moves.
# --------------------------------------------------------------------------------------------- #
REFS_MODULE = Path(__file__).resolve().parents[1] / "agentflow" / "refs.py"
STEWARD_SKILL = Path(__file__).resolve().parents[2] / "steward" / "SKILL.md"

#: Number words the prose may spell the count with, indexed by the count itself.
_NUMBER_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six"}


def _flat(text: str) -> str:
    """Collapse whitespace, so a prose needle survives re-wrapping.

    Asserting on a needle that spans a line break pins the LINE WIDTH as well as the words, and
    the next editor who reflows the paragraph gets a failure that says nothing about what broke.
    """
    return " ".join(text.split())


def _refusal_sites() -> tuple:
    """Partition `plan()`'s refusal sites into (chain, independent, total), read from source.

    DERIVED, never retyped. A test comparing two literals cannot prove derivation, so these come
    out of refs.py's own AST: add a fifth refusal, or fold `fetch.prune` into the chain, and the
    numbers move with it.
    """
    tree = ast.parse(REFS_MODULE.read_text(encoding="utf-8"))
    plan = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "plan"
    )

    def appends(node) -> int:
        """Count `<anything>.refused.append(...)` calls anywhere beneath `node`."""
        total = 0
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Attribute):
                continue
            if sub.func.attr != "append":
                continue
            inner = sub.func.value
            if isinstance(inner, ast.Attribute) and inner.attr == "refused":
                total += 1
        return total

    # Top-level `if` statements in plan(). The impoverished-remote cases share ONE of them as an
    # if/elif chain; `fetch.prune` is a separate statement. Ranked rather than compared by value,
    # so two statements holding equal counts cannot make the partition silently vanish.
    counts = sorted(
        (appends(n) for n in plan.body if isinstance(n, ast.If)), reverse=True
    )
    counts = [c for c in counts if c]
    chain = counts[0] if counts else 0
    independent = sum(counts[1:])
    return chain, independent, appends(plan)


def test_the_refusal_sites_are_an_exclusive_chain_plus_an_independent_guard():
    """The shape the paragraph now describes, asserted against the code that has it.

    Three mutually exclusive impoverished-remote cases in one chain, plus TWO independently
    guarded refusals (`fetch.prune`, and the local-branch deletion refusal) that can stand beside
    any of them. Not cosmetic: it is the
    difference between "at most one refusal per run" and "up to two", which is what a reader
    needs in order to interpret a report at all.
    """
    chain, independent, total = _refusal_sites()

    assert (chain, independent, total) == (3, 2, 5), (
        f"refusal shape is now chain={chain} independent={independent} total={total}, not "
        "(3, 2, 5). The steward refs paragraph states this shape in prose and its count sentence "
        "is scoped to the chain, so update skills/steward/SKILL.md in the same commit - a count "
        "nothing checks is exactly how gh#492 happened."
    )


def test_the_fetch_prune_refusal_can_stand_beside_an_impoverished_view(checkout):
    """The independence claim, proved by making BOTH fire at once.

    Behavioural rather than only structural: a non-standard refspec refusal and an unset
    `fetch.prune` refusal in the SAME plan. Were `fetch.prune` ever folded into that if/elif
    chain, the total would still read four by grep, every existing refusal test would stay green,
    and only this one would red.
    """
    assert refs.fetch_prune_enabled(checkout) is False, (
        "this fixture is expected to start with fetch.prune unset - a fresh clone does not "
        "inherit the canonical checkout's repo-local setting. If that changed, this test is no "
        "longer exercising two simultaneous refusals and must be re-grounded rather than fixed."
    )
    _git(checkout, "config", "--add", "remote.origin.fetch",
         "+refs/pull/*/head:refs/remotes/origin/pr/*")

    p = refs.plan(checkout)

    assert any("non-standard fetch refspec" in r for r in p.refused), (
        f"expected the refspec refusal; got {p.refused!r}"
    )
    assert any("fetch.prune" in r for r in p.refused), (
        f"expected the fetch.prune refusal alongside it; got {p.refused!r}"
    )
    assert len(p.refused) == 2, (
        f"expected exactly 2 refusals standing together, got {len(p.refused)}: {p.refused!r}. "
        "One run printing two refusal lines is the whole point of the fourth guard being "
        "independent of the chain."
    )


def test_the_steward_paragraph_scopes_its_count_to_the_derived_number():
    """The prose number must track the code in the one place that states a number.

    The count comes from `_refusal_sites`, so this cannot pass by agreeing with a literal someone
    retyped. Add a fourth impoverished-remote case and this demands the paragraph say "Four".
    """
    chain, _, _ = _refusal_sites()
    word = _NUMBER_WORDS[chain]
    text = _flat(STEWARD_SKILL.read_text(encoding="utf-8"))

    assert f"{word} cases refuse" in text, (
        f"the refs paragraph must say '{word} cases refuse' to match the {chain} mutually "
        "exclusive impoverished-remote refusals in refs.py; it does not. That is gh#492's "
        "defect - prose and code counting differently with nothing comparing them."
    )
    assert "FOR THAT REASON" in text, (
        "the count sentence must state WHAT it counts. Unscoped it reads as a count of every "
        "refusal in the module, which is four, and that reading is what got the paragraph filed "
        "as wrong when it was right under its own predicate."
    )
    _, independent, _ = _refusal_sites()
    more = f"{_NUMBER_WORDS[independent].upper()} MORE refusal"
    assert more in text, (
        f"the paragraph must name the {independent} independently-guarded refusal(s) beside the "
        f"count ('{more}'), so a reader who greps the code for refusal sites can reconcile the "
        "total with the chain instead of concluding the document is stale."
    )


def test_the_paragraph_no_longer_predicts_that_the_refusal_cannot_clear():
    """The falsified prediction, kept out by name - and the policy kept in.

    `fetch.prune` was set on the canonical checkout from outside this sweep, so "its fetch.prune
    refusal will not clear" became false. The POLICY it was attached to did not become false, and
    that is the trap: the correct repair drops only the prediction, so this pins the two apart.
    """
    text = _flat(STEWARD_SKILL.read_text(encoding="utf-8"))

    assert "refusal will not clear" not in text, (
        "the refs paragraph still predicts the fetch.prune refusal will not clear. It has "
        "cleared - the setting is true on the canonical checkout - so the sentence is false "
        "(gh#492)."
    )
    assert "do not set it on an unattended wake" in text, (
        "the POLICY sentence must survive the repair: this sweep still never applies "
        "fetch.prune and gh#116's escalation still stands. Only the prediction was wrong, and "
        "deleting the policy with it would licence an unattended lane to change a shared "
        "checkout."
    )
    assert "not in global config" in text, (
        "the paragraph must say the setting is repo-local, because that is what makes the new "
        "state conditional rather than permanent: a fresh clone starts unset and the refusal "
        "returns. Replacing one absolute prediction with the opposite absolute would reproduce "
        "gh#492 in the other direction."
    )



# --------------------------------------------------------------------------------------------- #
# Merged LOCAL branches: deleted under --apply only when all three guards hold.
#
# (1) no head on origin, (2) tip == the headRefOid of a merged PR with the same head name,
# (3) checked out in no worktree. The merged-PR set is always a stub here: no test calls `gh`.
# --------------------------------------------------------------------------------------------- #
def _reader(heads: dict):
    return lambda: refs.MergedHeads(heads={k: frozenset(v) for k, v in heads.items()})


def _never_called():
    raise AssertionError("the merged-PR reader must not be called here")


def _merged_local(checkout: Path, name: str = "feature/done") -> str:
    """A local branch with its own commit and no head on origin - what a merged lane leaves."""
    _git(checkout, "checkout", "-q", "-b", name)
    (checkout / f"{name.replace('/', '_')}.txt").write_text("work\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", f"work on {name}")
    sha = _git(checkout, "rev-parse", "HEAD").strip()
    _git(checkout, "checkout", "-q", "master")
    return sha


def _has_branch(checkout: Path, name: str) -> bool:
    return name in refs.local_branch_tips(checkout)


def test_an_exact_merged_pr_head_with_no_origin_head_is_deleted_under_apply(checkout):
    sha = _merged_local(checkout)
    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {sha}}))

    assert p.deletable == [("feature/done", sha)]
    assert _has_branch(checkout, "feature/done"), "planning deletes nothing"

    failures, lines = refs.apply(checkout, p)
    assert failures == 0, lines
    assert not _has_branch(checkout, "feature/done")
    assert _has_branch(checkout, "master"), "nothing else went with it"
    assert any("deleted feature/done" in line for line in lines)


def test_guard_1_a_branch_still_on_origin_is_never_a_candidate(checkout):
    """Its tip IS the merged PR head, but origin still has the branch: not deleted."""
    _git(checkout, "branch", "feature/live", "origin/feature/live")
    sha = refs.local_branch_tips(checkout)["feature/live"]

    p = refs.plan(checkout, pr_reader=_reader({"feature/live": {sha}}))

    assert p.deletable == []


def test_guard_2_a_merged_but_moved_branch_is_kept_with_its_reason(checkout):
    """The tip is not the merged head: someone committed after the merge. Kept, and said why."""
    sha = _merged_local(checkout)
    other = _git(checkout, "rev-parse", "master").strip()
    assert other != sha

    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {other}}))

    assert p.deletable == []
    assert any("feature/done" in k and "merged but moved" in k for k in p.kept), p.kept


def test_guard_2_a_never_merged_branch_is_kept_with_its_reason(checkout):
    _merged_local(checkout)
    p = refs.plan(checkout, pr_reader=_reader({}))

    assert p.deletable == []
    assert any("feature/done" in k and "never merged" in k for k in p.kept), p.kept


def test_guard_2_matches_the_sha_under_the_same_head_name_only(checkout):
    """A sha-only match would delete a branch whose tip happens to be ANOTHER PR's head."""
    sha = _merged_local(checkout)
    p = refs.plan(checkout, pr_reader=_reader({"some/other-branch": {sha}}))

    assert p.deletable == []


def test_guard_2_a_reused_head_name_matches_any_of_its_merged_heads(checkout):
    sha = _merged_local(checkout)
    older = _git(checkout, "rev-parse", "master").strip()
    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {older, sha}}))

    assert p.deletable == [("feature/done", sha)]


def test_guard_3_a_branch_checked_out_in_a_worktree_is_kept(checkout, tmp_path):
    sha = _merged_local(checkout)
    _git(checkout, "worktree", "add", "-q", str(tmp_path / "wt"), "feature/done")

    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {sha}}))

    assert p.deletable == []
    assert any("feature/done" in k and "checked out" in k for k in p.kept), p.kept


def test_the_current_branch_is_never_deleted(checkout):
    sha = _merged_local(checkout)
    _git(checkout, "checkout", "-q", "feature/done")

    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {sha}}))

    assert p.deletable == []


def test_the_base_branch_is_never_a_candidate(checkout):
    sha = _merged_local(checkout)
    p = refs.plan(checkout, base="feature/done", pr_reader=_reader({"feature/done": {sha}}))

    assert p.deletable == []


def test_an_unreadable_worktree_list_refuses_and_deletes_nothing(checkout, monkeypatch):
    """Monkeypatched because a repo where `worktree list` fails while every other read works
    cannot be built honestly. The property: an unknown set of held branches deletes nothing,
    instead of reading as an empty set."""
    sha = _merged_local(checkout)
    monkeypatch.setattr(refs, "worktree_branches", lambda repo: None)

    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {sha}}))

    assert p.deletable == []
    assert any("could not list worktrees" in r for r in p.refused)


def test_no_pr_reader_deletes_nothing_and_says_why_without_refusing(checkout):
    _merged_local(checkout)
    p = refs.plan(checkout)

    assert p.deletable == []
    assert any("no merged-PR reader" in k for k in p.kept), p.kept
    assert not any("merged-PR" in r for r in p.refused), "a missing reader is not a finding"


def test_a_failed_pr_read_refuses_and_deletes_nothing(checkout):
    _merged_local(checkout)
    p = refs.plan(checkout, pr_reader=lambda: refs.MergedHeads(failed="HTTP 502"))

    assert p.deletable == []
    assert any("merged-PR read failed (HTTP 502)" in r for r in p.refused)
    assert not p.is_clean, "a blind sweep must not exit 0"


def test_an_unreachable_remote_deletes_no_local_branch(checkout):
    sha = _merged_local(checkout)
    _git(checkout, "remote", "set-url", "origin", str(checkout / "does-not-exist.git"))

    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {sha}}))

    assert p.deletable == []


def test_the_pr_reader_is_not_called_without_a_candidate(checkout):
    """No local branch lacks an origin head here, so there is nothing to ask the host about."""
    p = refs.plan(checkout, pr_reader=_never_called)
    assert p.deletable == []


def test_apply_refuses_a_branch_that_moved_between_plan_and_apply(checkout):
    sha = _merged_local(checkout)
    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {sha}}))
    assert p.deletable == [("feature/done", sha)]

    master = _git(checkout, "rev-parse", "master").strip()
    _git(checkout, "update-ref", "refs/heads/feature/done", master)

    failures, lines = refs.apply(checkout, p)
    assert failures == 1, lines
    assert refs.local_branch_tips(checkout)["feature/done"] == master, "the moved branch survives"


def test_apply_keeps_a_branch_checked_out_between_plan_and_apply(checkout, tmp_path):
    """`update-ref -d` does not protect a checked-out branch, so apply re-reads the worktrees."""
    sha = _merged_local(checkout)
    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {sha}}))
    _git(checkout, "worktree", "add", "-q", str(tmp_path / "late"), "feature/done")

    failures, lines = refs.apply(checkout, p)
    assert failures == 1, lines
    assert _has_branch(checkout, "feature/done")
    assert any("KEPT feature/done" in line for line in lines)


def test_a_pending_deletion_is_not_clean_and_the_apply_makes_it_clean(checkout):
    _git(checkout, "config", "fetch.prune", "true")
    sha = _merged_local(checkout)
    reader = _reader({"feature/done": {sha}})

    assert not refs.plan(checkout, pr_reader=reader).is_clean
    refs.apply(checkout, refs.plan(checkout, pr_reader=reader))
    assert refs.plan(checkout, pr_reader=reader).is_clean


# --------------------------------------------------------------------------------------------- #
# The merged-PR reader, driven through its runner seam with fake output only.
# --------------------------------------------------------------------------------------------- #
def test_merged_pr_heads_parses_names_to_sets_of_oids():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, (
            '[{"headRefName": "a", "headRefOid": "111", "isCrossRepository": false},'
            ' {"headRefName": "a", "headRefOid": "222", "isCrossRepository": false},'
            ' {"headRefName": "b", "headRefOid": "", "isCrossRepository": false}]'
        ), ""

    got = refs.merged_pr_heads("o/r", "github", "gh", runner=runner)

    assert got.heads == {"a": frozenset({"111", "222"})}, "an item with no oid is skipped"
    assert calls[0][:3] == ["gh", "pr", "list"] and "merged" in calls[0]
    assert "headRefName,headRefOid,isCrossRepository" in calls[0]


def test_merged_pr_heads_skips_fork_prs_and_items_that_do_not_say():
    """A fork PR's head is a branch in another repository, so its name says nothing about ours."""
    out = (
        '[{"headRefName": "fork", "headRefOid": "111", "isCrossRepository": true},'
        ' {"headRefName": "unsaid", "headRefOid": "222"},'
        ' {"headRefName": "ours", "headRefOid": "333", "isCrossRepository": false}]'
    )
    got = refs.merged_pr_heads("o/r", "github", "gh", runner=lambda a: (0, out, ""))
    assert got.heads == {"ours": frozenset({"333"})}


def test_merged_pr_heads_reports_a_failed_or_unreadable_read_as_failed():
    assert refs.merged_pr_heads("o/r", "github", "gh", runner=lambda a: (1, "", "boom")).failed
    assert refs.merged_pr_heads("o/r", "github", "gh", runner=lambda a: (0, "{nope", "")).failed
    assert refs.merged_pr_heads("o/r", "github", "gh", runner=lambda a: (0, "{}", "")).failed


def test_merged_pr_heads_has_no_reader_for_a_non_github_host():
    def runner(argv):
        raise AssertionError("gh must not run for a host it cannot read")

    got = refs.merged_pr_heads("o/r", "gitea", "tea", runner=runner)
    assert got.heads is None and got.unavailable and not got.failed


# --------------------------------------------------------------------------------------------- #
# The CLI: dry run by default, deletion only under --apply.
# --------------------------------------------------------------------------------------------- #
def _stub_config(monkeypatch, checkout: Path, sha: str) -> None:
    from types import SimpleNamespace

    from agentflow import config

    monkeypatch.setattr(config, "load_project", lambda name: SimpleNamespace(
        canonical_repo=SimpleNamespace(
            local_checkout=str(checkout), base_branch="master", host_ref="o/r"
        )
    ))
    monkeypatch.setattr(config, "load_environment", lambda: SimpleNamespace(
        git_host=SimpleNamespace(kind="github", cli="gh")
    ))
    # The CLI's only route to `gh`, replaced so no test can reach the network.
    monkeypatch.setattr(refs, "merged_pr_heads", lambda *a, **k: refs.MergedHeads(
        heads={"feature/done": frozenset({sha})}
    ))


def test_the_cli_is_a_dry_run_unless_apply_is_typed(checkout, monkeypatch, capsys):
    sha = _merged_local(checkout)
    _stub_config(monkeypatch, checkout, sha)

    rc = refs.main(["--project", "x"])
    out = capsys.readouterr().out

    assert rc == 1, "a pending deletion is not clean"
    assert "merged local branches to delete (1)" in out and "feature/done" in out
    assert _has_branch(checkout, "feature/done"), "no --apply, no deletion"

    refs.main(["--project", "x", "--apply"])
    out = capsys.readouterr().out
    assert not _has_branch(checkout, "feature/done")
    assert "deleted feature/done" in out


# --------------------------------------------------------------------------------------------- #
# Branches held without a porcelain `branch` line, and names `refname:short` would mangle.
# --------------------------------------------------------------------------------------------- #
def test_a_branch_mid_rebase_in_another_worktree_is_held(checkout, tmp_path):
    """A stopped rebase shows its worktree as DETACHED, yet the branch is still in use there and
    `branch -D` refuses it. Driven by a real rebase stopped part-way in a linked worktree."""
    sha = _merged_local(checkout)
    wt = tmp_path / "rebasing"
    _git(checkout, "worktree", "add", "-q", str(wt), "feature/done")
    root = _git(checkout, "rev-list", "--max-parents=0", "master").strip()
    stopped = subprocess.run(
        ["git", "-C", str(wt), "rebase", "--force-rebase", "--exec", "false", root],
        capture_output=True, text=True,
    )
    assert stopped.returncode != 0, "the rebase must stop part-way for this test to mean anything"
    porcelain = _git(checkout, "worktree", "list", "--porcelain")
    assert "branch refs/heads/feature/done" not in porcelain, "porcelain shows it detached"
    assert refs.local_branch_tips(checkout)["feature/done"] == sha, "the ref has not moved yet"

    assert "feature/done" in refs.worktree_branches(checkout)
    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {sha}}))
    assert p.deletable == []


def test_a_worktree_whose_git_dir_cannot_be_read_refuses_deletion(checkout, tmp_path):
    """What a vanished worktree holds is unknown, and unknown keeps rather than deletes."""
    import shutil

    sha = _merged_local(checkout)
    gone = tmp_path / "vanished"
    _git(checkout, "worktree", "add", "-q", str(gone), "-b", "other-work")
    shutil.rmtree(gone)

    assert refs.worktree_branches(checkout) is None
    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {sha}}))
    assert p.deletable == []
    assert any("could not list worktrees" in r for r in p.refused)


def test_a_branch_sharing_its_name_with_a_tag_is_still_named_correctly(checkout):
    """`%(refname:short)` turns branch `x` into `heads/x` when tag `x` exists, so the branch would
    match no merged PR head and could never be deleted."""
    sha = _merged_local(checkout)
    _git(checkout, "tag", "feature/done", "master")

    assert "feature/done" in refs.local_branches(checkout)
    p = refs.plan(checkout, pr_reader=_reader({"feature/done": {sha}}))
    assert p.deletable == [("feature/done", sha)]
