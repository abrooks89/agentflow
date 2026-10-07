"""Domain C's one bounded fix: a fast-forward pull of a link-deployed checkout.

On a link-deployed surface the deployed skill directory IS a directory of the checkout, so
"deployed is stale" means "the checkout is behind", and the cure is a pull, not a provision run.
The steward may run that pull only when all four conditions hold - link-deployed, on the base
branch, no tracked changes, fast-forward - and must name the failing condition and leave the
checkout untouched otherwise.

Every behaviour test drives REAL git: a bare remote, a checkout cloned from it, a second clone
that advances the remote, and a deployed skill directory that is a junction (Windows) or symlink
onto the checkout. The prose tests at the bottom pin the wiring in `skills/steward/SKILL.md`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentflow import checkout_pull as cp

BASE = "master"
REPO_ROOT = Path(__file__).resolve().parents[3]
STEWARD_SKILL = REPO_ROOT / "skills" / "steward" / "SKILL.md"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _identity(repo: Path) -> None:
    for k, v in (("user.email", "t@e.st"), ("user.name", "t"), ("core.autocrlf", "false")):
        _git(repo, "config", k, v)


def _link(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"touch {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def fleet(tmp_path: Path):
    """(checkout, upstream, deployed): checkout one commit behind origin, deployed linked onto it."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", BASE, str(bare)], check=True, capture_output=True
    )
    upstream = tmp_path / "upstream"
    subprocess.run(["git", "init", "-b", BASE, str(upstream)], check=True, capture_output=True)
    _identity(upstream)
    (upstream / "skills" / "steward").mkdir(parents=True)
    _commit(upstream, "skills/steward/SKILL.md", "rule one\n")
    _git(upstream, "remote", "add", "origin", str(bare))
    _git(upstream, "push", "-u", "origin", BASE)

    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", str(bare), str(checkout)], check=True, capture_output=True)
    _identity(checkout)

    # A merge lands upstream; the checkout has not pulled it.
    _commit(upstream, "skills/steward/SKILL.md", "rule one\nrule two\n")
    _git(upstream, "push", "origin", BASE)

    claude_skills = tmp_path / "claude" / "skills"
    claude_skills.mkdir(parents=True)
    deployed = claude_skills / "steward"
    _link(deployed, checkout / "skills" / "steward")
    return checkout, upstream, deployed


def _keys(v: cp.Verdict) -> list[str]:
    return [r.key for r in v.refusals]


# --------------------------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------------------------- #
def test_happy_path_fast_forwards_and_logs_both_shas(fleet):
    checkout, upstream, deployed = fleet
    before = _git(checkout, "rev-parse", "HEAD")
    target = _git(upstream, "rev-parse", "HEAD")

    v = cp.run(checkout, BASE, deployed, apply=True)

    assert _keys(v) == []
    assert v.pulled is True
    assert v.before == before
    assert v.after == target
    assert _git(checkout, "rev-parse", "HEAD") == target
    # deployed == checkout by construction: the new rule is live through the link.
    assert "rule two" in (deployed / "SKILL.md").read_text(encoding="utf-8")


def test_report_only_without_apply_never_moves_head(fleet):
    checkout, _, deployed = fleet
    before = _git(checkout, "rev-parse", "HEAD")

    v = cp.run(checkout, BASE, deployed, apply=False)

    assert _keys(v) == []
    assert v.pulled is False
    assert v.behind == 1
    assert _git(checkout, "rev-parse", "HEAD") == before


def test_already_current_is_not_a_refusal_and_does_not_pull(fleet):
    checkout, _, deployed = fleet
    _git(checkout, "pull", "--ff-only")
    before = _git(checkout, "rev-parse", "HEAD")

    v = cp.run(checkout, BASE, deployed, apply=True)

    assert _keys(v) == []
    assert v.behind == 0
    assert v.pulled is False
    assert v.before == v.after == before


def test_behind_is_measured_against_the_fetched_remote_ref(fleet):
    """The working tree and the deployed copy are stale TOGETHER; only the remote ref sees it."""
    checkout, _, deployed = fleet
    v = cp.run(checkout, BASE, deployed, apply=False)
    assert v.behind == 1


# --------------------------------------------------------------------------------------------- #
# Each condition, failing alone, refuses by name and leaves the checkout untouched
# --------------------------------------------------------------------------------------------- #
def test_not_on_base_branch_refuses(fleet):
    checkout, _, deployed = fleet
    _git(checkout, "checkout", "-b", "side")
    before = _git(checkout, "rev-parse", "HEAD")

    v = cp.run(checkout, BASE, deployed, apply=True)

    assert _keys(v) == ["not_on_base_branch"]
    assert v.pulled is False
    assert _git(checkout, "rev-parse", "HEAD") == before


def test_dirty_tracked_file_refuses(fleet):
    checkout, _, deployed = fleet
    (checkout / "skills" / "steward" / "SKILL.md").write_text("hand edit\n", encoding="utf-8")
    before = _git(checkout, "rev-parse", "HEAD")

    v = cp.run(checkout, BASE, deployed, apply=True)

    assert _keys(v) == ["tracked_changes"]
    assert v.pulled is False
    assert _git(checkout, "rev-parse", "HEAD") == before
    assert (checkout / "skills" / "steward" / "SKILL.md").read_text(encoding="utf-8") == "hand edit\n"


def test_untracked_file_alone_does_not_refuse(fleet):
    checkout, _, deployed = fleet
    (checkout / "scratch.txt").write_text("x\n", encoding="utf-8")
    v = cp.run(checkout, BASE, deployed, apply=False)
    assert _keys(v) == []


def test_diverged_checkout_refuses_as_not_fast_forward(fleet):
    checkout, _, deployed = fleet
    local = _commit(checkout, "local.txt", "a local commit nobody pushed\n")

    v = cp.run(checkout, BASE, deployed, apply=True)

    assert _keys(v) == ["not_fast_forward"]
    assert v.pulled is False
    assert _git(checkout, "rev-parse", "HEAD") == local


def test_not_link_deployed_refuses(fleet, tmp_path):
    checkout, _, _ = fleet
    copied = tmp_path / "copy-deployed" / "steward"
    copied.mkdir(parents=True)
    (copied / "SKILL.md").write_text("rule one\n", encoding="utf-8")
    before = _git(checkout, "rev-parse", "HEAD")

    v = cp.run(checkout, BASE, copied, apply=True)

    assert _keys(v) == ["not_link_deployed"]
    assert v.pulled is False
    assert _git(checkout, "rev-parse", "HEAD") == before


def test_link_onto_a_different_tree_is_not_link_deployed(fleet, tmp_path):
    """A link that points somewhere other than this checkout is not cured by pulling it."""
    checkout, _, _ = fleet
    elsewhere = tmp_path / "elsewhere" / "steward"
    elsewhere.mkdir(parents=True)
    link = tmp_path / "claude2" / "steward"
    link.parent.mkdir()
    _link(link, elsewhere)

    v = cp.run(checkout, BASE, link, apply=True)

    assert _keys(v) == ["not_link_deployed"]
    assert v.pulled is False


def test_missing_deployed_path_is_not_link_deployed(fleet, tmp_path):
    checkout, _, _ = fleet
    v = cp.run(checkout, BASE, tmp_path / "nope" / "steward", apply=True)
    assert _keys(v) == ["not_link_deployed"]


def test_every_failing_condition_is_reported_not_just_the_first(fleet, tmp_path):
    checkout, _, _ = fleet
    copied = tmp_path / "copy" / "steward"
    copied.mkdir(parents=True)
    _git(checkout, "checkout", "-b", "side")
    (checkout / "skills" / "steward" / "SKILL.md").write_text("hand edit\n", encoding="utf-8")

    v = cp.run(checkout, BASE, copied, apply=True)

    assert set(_keys(v)) == {"not_link_deployed", "not_on_base_branch", "tracked_changes"}
    assert v.pulled is False


def test_unfetchable_remote_refuses_and_does_not_pull(fleet):
    checkout, _, deployed = fleet
    _git(checkout, "remote", "set-url", "origin", str(checkout.parent / "gone.git"))
    before = _git(checkout, "rev-parse", "HEAD")

    v = cp.run(checkout, BASE, deployed, apply=True)

    assert "fetch_failed" in _keys(v)
    assert v.pulled is False
    assert _git(checkout, "rev-parse", "HEAD") == before


def test_a_hung_fetch_refuses_instead_of_hanging_the_wake(fleet, monkeypatch):
    checkout, _, deployed = fleet
    before = _git(checkout, "rev-parse", "HEAD")
    real_run = subprocess.run

    def run(argv, *a, **kw):
        assert kw.get("env", {}).get("GIT_TERMINAL_PROMPT") == "0"
        assert kw.get("timeout") == cp.GIT_TIMEOUT_S
        if "fetch" in argv:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(cp.subprocess, "run", run)
    v = cp.run(checkout, BASE, deployed, apply=True)

    assert "fetch_failed" in _keys(v)
    assert v.pulled is False
    monkeypatch.undo()
    assert _git(checkout, "rev-parse", "HEAD") == before


def test_every_refusal_key_is_a_documented_rule():
    for key in ("not_link_deployed", "not_on_base_branch", "tracked_changes",
                "not_fast_forward", "fetch_failed"):
        assert key in cp.RULES


# --------------------------------------------------------------------------------------------- #
# Adversarial cases: nested worktree, detached HEAD, shadowing branch, the exact pull argv
# --------------------------------------------------------------------------------------------- #
def test_link_onto_a_worktree_nested_inside_the_checkout_is_not_link_deployed(fleet):
    """A path inside the checkout's directory can belong to a different work tree.

    A git worktree created under the checkout (an in-repo worktree directory) sits inside the
    checkout's path but has its own HEAD, so pulling the checkout does not change what the link
    serves. The link check asks git which tree owns the target, not whether the path is nested.
    """
    checkout, _, deployed = fleet
    nested = checkout / ".claude" / "worktrees" / "w"
    _git(checkout, "worktree", "add", "-b", "side", str(nested))
    deployed.rmdir() if sys.platform == "win32" else deployed.unlink()
    _link(deployed, nested / "skills" / "steward")
    before = _git(checkout, "rev-parse", "HEAD")
    served = (deployed / "SKILL.md").read_text(encoding="utf-8")

    v = cp.run(checkout, BASE, deployed, apply=True)

    assert _keys(v) == ["not_link_deployed"]
    assert v.pulled is False
    assert _git(checkout, "rev-parse", "HEAD") == before
    assert (deployed / "SKILL.md").read_text(encoding="utf-8") == served


def test_detached_head_refuses_as_not_on_base_branch(fleet):
    checkout, _, deployed = fleet
    _git(checkout, "checkout", "--detach")
    before = _git(checkout, "rev-parse", "HEAD")

    v = cp.run(checkout, BASE, deployed, apply=True)

    assert _keys(v) == ["not_on_base_branch"]
    assert v.pulled is False
    assert _git(checkout, "rev-parse", "HEAD") == before


def test_a_local_branch_named_like_the_remote_ref_does_not_hide_the_drift(fleet):
    """A local `origin/<base>` branch outranks the remote-tracking ref when git resolves the
    short name, so the comparison must spell the full `refs/remotes/` ref."""
    checkout, _, deployed = fleet
    _git(checkout, "branch", f"origin/{BASE}", "HEAD")

    v = cp.run(checkout, BASE, deployed, apply=False)

    assert _keys(v) == []
    assert v.behind == 1


def test_the_pull_is_fast_forward_only(fleet, monkeypatch):
    """The bounded exception allows exactly `pull --ff-only`; any other pull mode is a merge."""
    checkout, _, deployed = fleet
    calls = []
    real = cp._git

    def spy(repo, *args):
        calls.append(args)
        return real(repo, *args)

    monkeypatch.setattr(cp, "_git", spy)
    v = cp.run(checkout, BASE, deployed, apply=True)

    assert v.pulled is True
    pulls = [a for a in calls if a and a[0] == "pull"]
    assert pulls == [("pull", "--ff-only", "origin", BASE)]


# --------------------------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------------------------- #
def test_cli_prints_shas_and_exits_zero_on_pull(fleet, capsys):
    checkout, upstream, deployed = fleet
    before = _git(checkout, "rev-parse", "HEAD")
    target = _git(upstream, "rev-parse", "HEAD")

    rc = cp.main(["--checkout", str(checkout), "--base-branch", BASE,
                  "--deployed", str(deployed), "--apply"])

    out = capsys.readouterr().out
    assert rc == 0
    assert f"before: {before}" in out
    assert f"after: {target}" in out


def test_cli_names_the_condition_and_exits_one_on_refusal(fleet, capsys):
    checkout, _, deployed = fleet
    _git(checkout, "checkout", "-b", "side")

    rc = cp.main(["--checkout", str(checkout), "--base-branch", BASE,
                  "--deployed", str(deployed), "--apply"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "refused [not_on_base_branch]" in out
    assert "NOT PULLED" in out


def test_module_never_spells_a_bare_git_call_without_dash_c():
    src = (Path(cp.__file__)).read_text(encoding="utf-8")
    calls = re.findall(r'\["git",\s*([^\]]*)', src)
    assert calls, "expected git invocations in the module"
    for args in calls:
        assert args.lstrip().startswith('"-C"'), f"git call without -C: {args!r}"


# --------------------------------------------------------------------------------------------- #
# Wiring in the steward's own instructions
# --------------------------------------------------------------------------------------------- #
def _steward() -> str:
    return STEWARD_SKILL.read_text(encoding="utf-8")


def _domain_c(text: str) -> str:
    start = text.index("**C. Deployed ≠ source.**")
    end = text.index("**D. Schema ≠ code.**")
    return text[start:end]


def _may_not_fix(text: str) -> str:
    start = text.index("**YOU MAY NOT FIX**")
    end = text.index("## Three outputs")
    return text[start:end]


def test_domain_c_runs_the_helper_with_apply():
    c = _domain_c(_steward())
    assert "python -m agentflow.checkout_pull --project <name> --apply" in c


def test_domain_c_names_all_four_conditions():
    c = _domain_c(_steward())
    for phrase in ("link-deployed", "<base-branch>", "no tracked changes", "fast-forward"):
        assert phrase in c, phrase


def test_domain_c_states_the_pull_command_with_dash_c_and_ff_only():
    c = _domain_c(_steward())
    assert "git -C <canonical_repo.local_checkout> pull --ff-only" in c


def test_domain_c_logs_sha_before_and_after():
    c = _domain_c(_steward())
    assert "sha before and after" in c


def test_domain_c_reports_failing_condition_and_does_not_pull():
    c = _domain_c(_steward())
    assert re.search(r"names? the (failing )?condition", c)
    assert "does not pull" in c or "do not pull" in c


def test_domain_c_keeps_provision_force_human_and_says_why():
    c = _domain_c(_steward())
    assert "provision --force" in c
    assert "human" in c
    assert "hooks" in c and "settings" in c


def test_domain_c_carries_the_measurement_lessons():
    c = _domain_c(_steward())
    assert "fetch" in c
    assert "origin/<base-branch>" in c
    assert "line endings" in c
    assert "working tree" in c


def test_charter_states_the_one_bounded_exception_to_the_deploy_line():
    nf = _may_not_fix(_steward())
    m = re.search(r"^\* anything requiring a deploy.*?(?=^\* )", nf, re.S | re.M)
    assert m, "the deploy bullet is missing from YOU MAY NOT FIX"
    deploy_line = m.group(0)
    # The exception sits on the deploy line itself: the reap exception further down already
    # says "except", so a file-wide check would pass with no deploy exception at all.
    assert "except" in deploy_line.lower()
    assert "agentflow.checkout_pull" in deploy_line
    assert "pull --ff-only" in deploy_line
    # The exception is stated with its reason, on the same line.
    assert "It is in the charter because git can contradict it" in deploy_line
    assert "already merged upstream" in deploy_line
    assert "undo" in deploy_line
    # Everything else stays forbidden, said on the deploy line and not only implied by it.
    assert "`provision --force` and every other deploy step stay forbidden" in deploy_line
    # `--deploy-only` is a deploy too: the merging builder's, never the steward's.
    assert "`provision.py --deploy-only`" in deploy_line
    assert "merging" in deploy_line and "never runs it" in deploy_line
    assert "migration, restart, or credential change" in nf


def test_domain_c_says_the_pull_rewrites_the_running_stewards_own_skill():
    """On a link-deployed surface deployed == checkout, so the allowed pull rewrites this very
    SKILL.md mid-wake. The prose must say so and say how the wake carries on."""
    c = _domain_c(_steward())
    assert "the pull rewrites your own `SKILL.md`" in c
    assert "domain C's last step" in c
    assert "do not re-read this skill after the pull" in c
    assert "next wake" in c


def test_domain_c_provision_reason_is_not_one_the_allowed_pull_shares():
    """Rewriting the steward's own SKILL.md mid-wake is what the allowed pull does too, so it
    cannot be the reason provision stays human."""
    c = _domain_c(_steward())
    start = c.index("**`provision --force` stays a human-typed step.**")
    para = c[start:c.index("\n\n", start)]
    assert "hooks" in para and "settings" in para
    assert "mid-wake" not in para
