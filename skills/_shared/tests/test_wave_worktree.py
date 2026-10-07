"""gh#503: crash recovery must see best-of-N candidate worktrees.

wave_group.js creates each best-of-N candidate with `--task "<id>-c<i>"`, so the candidate's
worktree lands at `<repo>-<epic>-w<n>-t<id>-c<i>`. The orphan scan used to anchor on `-t<id>$`,
so after a crash `prune_orphan_worktrees` never saw a single candidate and every one leaked.

Every test drives REAL git in pytest's tmp_path - no mock stands in for `git worktree`, because
the defect lived in how a real porcelain path meets the pattern.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentflow import wave_worktree as ww


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A one-commit repo; wave worktrees land as its siblings inside tmp_path."""
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "master", str(path)], check=True, capture_output=True)
    for k, v in (("user.email", "t@e.st"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(path, "config", k, v)
    (path / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "seed")
    return path


def _listed(repo: Path, epic: str) -> dict[str, ww.OrphanRecord]:
    return {Path(o.path).name: o for o in ww.list_orphan_wave_worktrees(repo, epic)}


def _raw_worktree(repo: Path, name: str, branch: str) -> Path:
    """A worktree at an arbitrary sibling name - for paths create_wave_worktree would never emit."""
    path = repo.parent / name
    _git(repo, "worktree", "add", str(path), "-b", branch, "master")
    return path


def _branches(repo: Path) -> set[str]:
    return set(_git(repo, "branch", "--format=%(refname:short)").split())


def test_a_plain_wave_worktree_is_listed(repo):
    """Control: the shape the scan always handled still matches."""
    path = ww.create_wave_worktree(repo, "ep", 2, 5, "master")
    listed = _listed(repo, "ep")
    assert list(listed) == ["repo-ep-w2-t5"]
    assert listed["repo-ep-w2-t5"].branch == "ep-w2-t5"
    assert Path(listed["repo-ep-w2-t5"].path).resolve() == path.resolve()


def test_a_best_of_n_candidate_worktree_is_listed(repo):
    """The regression: the CLI passes task "5-c0" verbatim, exactly as wave_group.js does."""
    assert ww.main(["create", "--repo", str(repo), "--epic", "ep", "--wave", "2",
                    "--task", "5-c0", "--base", "master"]) == 0
    ww.create_wave_worktree(repo, "ep", 2, "5-c1", "master")
    listed = _listed(repo, "ep")
    assert set(listed) == {"repo-ep-w2-t5-c0", "repo-ep-w2-t5-c1"}
    assert listed["repo-ep-w2-t5-c0"].branch == "ep-w2-t5-c0"
    assert listed["repo-ep-w2-t5-c0"].has_unmerged_commits is False


def test_prune_removes_clean_candidates_and_preserves_unmerged_ones(repo):
    clean = ww.create_wave_worktree(repo, "ep", 2, "5-c0", "master")
    dirty = ww.create_wave_worktree(repo, "ep", 2, "5-c1", "master")
    (dirty / "work.txt").write_text("candidate work\n", encoding="utf-8")
    _git(dirty, "add", "-A")
    _git(dirty, "commit", "-m", "candidate commit")

    summary = ww.prune_orphan_worktrees(repo, "ep")

    assert summary.worktrees_removed == 2
    assert summary.branches_deleted == ["ep-w2-t5-c0"]
    assert summary.branches_preserved == ["ep-w2-t5-c1"]
    assert not clean.exists()
    assert not dirty.exists()
    branches = _branches(repo)
    assert "ep-w2-t5-c0" not in branches
    assert "ep-w2-t5-c1" in branches
    assert ww.list_orphan_wave_worktrees(repo, "ep") == []


def test_a_different_epic_is_not_listed(repo):
    ww.create_wave_worktree(repo, "other", 2, "5-c0", "master")
    ww.create_wave_worktree(repo, "other", 2, 5, "master")
    assert _listed(repo, "ep") == {}


@pytest.mark.parametrize("suffix", ["-c0x", "-cand", "-c", "-c0-c1", "x"])
def test_trailing_text_after_the_pattern_is_not_listed(repo, suffix):
    """The suffix is optional, but the anchor still holds: nothing may follow it."""
    _raw_worktree(repo, f"repo-ep-w2-t5{suffix}", f"ep-w2-t5{suffix}")
    assert _listed(repo, "ep") == {}


@pytest.mark.parametrize("name", ["repo-ep-w2-t", "repo-ep-w2-t-c0", "repo-ep-w-t5-c0"])
def test_an_empty_wave_or_task_number_is_not_listed(repo, name):
    """The digit groups are mandatory: a bare "-t" or "-w" is not a wave worktree."""
    _raw_worktree(repo, name, name.removeprefix("repo-"))
    assert _listed(repo, "ep") == {}


def test_a_slug_that_prefixes_another_does_not_cross_match(repo):
    ww.create_wave_worktree(repo, "ep", 1, "3-c0", "master")
    ww.create_wave_worktree(repo, "ep-2", 1, "3-c0", "master")
    assert set(_listed(repo, "ep")) == {"repo-ep-w1-t3-c0"}
    assert set(_listed(repo, "ep-2")) == {"repo-ep-2-w1-t3-c0"}


def test_a_slug_that_is_a_bare_tail_of_another_does_not_cross_match(repo):
    """The leading hyphen is load-bearing: slug "p" must not claim epic "ep"'s worktrees."""
    ww.create_wave_worktree(repo, "ep", 1, "3-c0", "master")
    assert _listed(repo, "p") == {}


def test_slug_metacharacters_are_matched_literally(repo):
    """re.escape is load-bearing: slug "ep.1" must not claim epic "ep-1"'s worktrees."""
    ww.create_wave_worktree(repo, "ep-1", 1, "3-c0", "master")
    assert _listed(repo, "ep.1") == {}


# gh#514: `git worktree list` returns every worktree sharing the git dir, so the match must pin
# the whole basename (repo name included) and the sibling directory, not just the path's tail.


def _two_epics_one_dirty(repo: Path) -> tuple[list[Path], list[Path], Path]:
    fix = [ww.create_wave_worktree(repo, "fix", 1, 3, "master"),
           ww.create_wave_worktree(repo, "fix", 1, "4-c0", "master")]
    login = [ww.create_wave_worktree(repo, "login-fix", 1, 3, "master"),
             ww.create_wave_worktree(repo, "login-fix", 1, "4-c0", "master")]
    uncommitted = login[0] / "in-flight.txt"
    uncommitted.write_text("uncommitted sibling work\n", encoding="utf-8")
    return fix, login, uncommitted


def test_pruning_a_hyphen_suffix_slug_leaves_the_longer_epic_intact(repo):
    """The reported defect: pruning "fix" deleted epic "login-fix"'s worktrees and branches."""
    fix, login, uncommitted = _two_epics_one_dirty(repo)
    fix_uncommitted = fix[0] / "fix-work.txt"
    fix_uncommitted.write_text("owned work\n", encoding="utf-8")

    summary = ww.prune_orphan_worktrees(repo, "fix")

    assert summary.worktrees_removed == 2
    assert sorted(summary.branches_deleted) == ["fix-w1-t3", "fix-w1-t4-c0"]
    assert not any(p.exists() for p in fix)
    assert all(p.exists() for p in login)
    assert uncommitted.read_text(encoding="utf-8") == "uncommitted sibling work\n"
    branches = _branches(repo)
    assert {"login-fix-w1-t3", "login-fix-w1-t4-c0"} <= branches
    assert not {"fix-w1-t3", "fix-w1-t4-c0"} & branches
    assert set(_listed(repo, "login-fix")) == {"repo-login-fix-w1-t3", "repo-login-fix-w1-t4-c0"}


def test_pruning_the_longer_slug_leaves_the_hyphen_suffix_epic_intact(repo):
    """Symmetry control, not a regression test: no tail-anchored pattern for "login-fix" could
    ever match "repo-fix-*", so this stays green on the pre-gh#514 code. It pins that the fix did
    not overcorrect into the reverse direction."""
    fix, login, uncommitted = _two_epics_one_dirty(repo)

    summary = ww.prune_orphan_worktrees(repo, "login-fix")

    assert summary.worktrees_removed == 2
    assert sorted(summary.branches_deleted) == ["login-fix-w1-t3", "login-fix-w1-t4-c0"]
    assert not any(p.exists() for p in login)
    assert all(p.exists() for p in fix)
    assert {"fix-w1-t3", "fix-w1-t4-c0"} <= _branches(repo)
    assert set(_listed(repo, "fix")) == {"repo-fix-w1-t3", "repo-fix-w1-t4-c0"}


def test_a_repo_whose_name_is_a_suffix_of_another_does_not_cross_prune(tmp_path):
    """"my-app" and its linked checkout "app" share one git dir and one parent directory."""
    my_app = tmp_path / "my-app"
    subprocess.run(["git", "init", "-b", "master", str(my_app)], check=True, capture_output=True)
    for k, v in (("user.email", "t@e.st"), ("user.name", "t")):
        _git(my_app, "config", k, v)
    (my_app / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(my_app, "add", "-A")
    _git(my_app, "commit", "-m", "seed")
    app = tmp_path / "app"
    _git(my_app, "worktree", "add", "--detach", str(app), "master")

    app_wt = ww.create_wave_worktree(app, "ep", 1, 1, "master")
    my_app_wt = ww.create_wave_worktree(my_app, "ep", 1, 2, "master")
    (my_app_wt / "in-flight.txt").write_text("uncommitted\n", encoding="utf-8")

    assert set(_listed(app, "ep")) == {"app-ep-w1-t1"}
    assert set(_listed(my_app, "ep")) == {"my-app-ep-w1-t2"}

    ww.prune_orphan_worktrees(app, "ep")
    assert not app_wt.exists()
    assert (my_app_wt / "in-flight.txt").exists()
    assert "ep-w1-t2" in _branches(my_app)

    app_wt = ww.create_wave_worktree(app, "ep", 1, 1, "master")
    ww.prune_orphan_worktrees(my_app, "ep")
    assert not my_app_wt.exists()
    assert app_wt.exists()
    assert "ep-w1-t1" in _branches(app)


def test_a_same_named_worktree_outside_the_sibling_dir_is_not_listed(repo):
    """Only _wave_path's directory counts: an exact basename elsewhere is someone else's tree."""
    elsewhere = repo.parent / "elsewhere"
    elsewhere.mkdir()
    _git(repo, "worktree", "add", str(elsewhere / "repo-ep-w1-t3"), "-b", "ep-w1-t3", "master")
    assert _listed(repo, "ep") == {}


# gh#514 round 2: repo names and slugs both hold hyphens, so the basename alone is ambiguous and
# the checked-out branch has to agree with it.


def test_a_hyphenated_linked_checkout_does_not_claim_a_hyphenated_epic_of_its_main_repo(tmp_path):
    """"app-x" + slug "ep" and "app" + slug "x-ep" both spell "app-x-ep-w1-t1": only the branch
    tells them apart. Round 1 force-removed app's dirty x-ep worktree and deleted its branch."""
    app = tmp_path / "app"
    subprocess.run(["git", "init", "-b", "master", str(app)], check=True, capture_output=True)
    for k, v in (("user.email", "t@e.st"), ("user.name", "t")):
        _git(app, "config", k, v)
    (app / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(app, "add", "-A")
    _git(app, "commit", "-m", "seed")
    app_x = tmp_path / "app-x"
    _git(app, "worktree", "add", "--detach", str(app_x), "master")

    theirs = ww.create_wave_worktree(app, "x-ep", 1, 1, "master")
    (theirs / "in-flight.txt").write_text("uncommitted\n", encoding="utf-8")
    ours = ww.create_wave_worktree(app_x, "ep", 1, 2, "master")
    assert theirs.name == "app-x-ep-w1-t1"

    assert set(_listed(app_x, "ep")) == {"app-x-ep-w1-t2"}
    summary = ww.prune_orphan_worktrees(app_x, "ep")

    assert summary.branches_deleted == ["ep-w1-t2"]
    assert not ours.exists()
    assert (theirs / "in-flight.txt").read_text(encoding="utf-8") == "uncommitted\n"
    assert "x-ep-w1-t1" in _branches(app)
    assert set(_listed(app, "x-ep")) == {"app-x-ep-w1-t1"}


def test_a_matching_basename_on_an_unrelated_branch_is_not_listed(repo):
    """The path alone never proves ownership: pruning must not `git branch -D` someone's branch."""
    wt = _raw_worktree(repo, "repo-ep-w1-t1", "someone-else")
    assert _listed(repo, "ep") == {}
    ww.prune_orphan_worktrees(repo, "ep")
    assert wt.exists()
    assert "someone-else" in _branches(repo)


def test_a_relative_dot_repo_path_still_finds_a_genuine_orphan(repo, monkeypatch):
    """Path(".").name is "": the repo must be resolved before its name is used."""
    ww.create_wave_worktree(repo, "ep", 1, 1, "master")
    monkeypatch.chdir(repo)
    assert set(_listed(Path("."), "ep")) == {"repo-ep-w1-t1"}


def test_a_case_variant_repo_path_still_finds_a_genuine_orphan(repo):
    """Windows paths are case-insensitive; the caller's spelling need not match git's."""
    variant = repo.parent / repo.name.upper()
    if not variant.exists():
        pytest.skip("case-sensitive filesystem: a case variant is a different directory")
    ww.create_wave_worktree(repo, "ep", 1, 1, "master")
    assert set(_listed(variant, "ep")) == {"repo-ep-w1-t1"}


# One failed removal must not abort the prune: the rest still go, and the failure carries git's
# own reason so the caller can report which tree is stuck and why.


def test_prune_keeps_going_past_a_failed_removal_and_reports_gits_reason(repo, monkeypatch):
    first = ww.create_wave_worktree(repo, "ep", 3, 1, "master")
    stuck = ww.create_wave_worktree(repo, "ep", 3, 2, "master")
    last = ww.create_wave_worktree(repo, "ep", 3, 3, "master")
    reason = f"error: failed to delete '{stuck}': Permission denied"
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "worktree", "remove"] and Path(cmd[-1]) == stuck:
            raise subprocess.CalledProcessError(255, cmd, output="", stderr=reason + "\n")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ww.subprocess, "run", run)
    summary = ww.prune_orphan_worktrees(repo, "ep")
    monkeypatch.setattr(ww.subprocess, "run", real_run)

    assert not first.exists()
    assert not last.exists()
    assert stuck.exists()
    assert summary.worktrees_removed == 2
    assert sorted(summary.branches_deleted) == ["ep-w3-t1", "ep-w3-t3"]
    assert summary.branches_preserved == []
    assert len(summary.failed) == 1
    failure = summary.failed[0]
    assert failure.branch == "ep-w3-t2"
    assert Path(failure.path).resolve() == stuck.resolve()
    assert failure.stderr == reason
    assert failure.has_unmerged_commits is False
    assert "ep-w3-t2" in _branches(repo)


def _fail_removal_of(stuck: Path, monkeypatch, stderr):
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "worktree", "remove"] and Path(cmd[-1]) == stuck:
            raise subprocess.CalledProcessError(255, cmd, output="", stderr=stderr)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ww.subprocess, "run", run)


def test_a_failed_unmerged_orphan_says_its_branch_holds_unmerged_work(repo, monkeypatch):
    """Once git deregisters a stuck tree a later prune cannot see it, so the failure is the only
    record left - it must say whether deleting the branch would lose commits."""
    stuck = ww.create_wave_worktree(repo, "ep", 3, 1, "master")
    (stuck / "g.txt").write_text("wave work\n", encoding="utf-8")
    _git(stuck, "add", "-A")
    _git(stuck, "commit", "-m", "wave work")
    _fail_removal_of(stuck, monkeypatch, "error: Permission denied\n")
    summary = ww.prune_orphan_worktrees(repo, "ep")
    monkeypatch.undo()

    assert [f.branch for f in summary.failed] == ["ep-w3-t1"]
    assert summary.failed[0].has_unmerged_commits is True
    assert summary.branches_preserved == []


@pytest.mark.parametrize("stderr", ["", None, "  \n"])
def test_a_failure_with_no_git_stderr_falls_back_to_the_exception_summary(
    repo, monkeypatch, stderr
):
    stuck = ww.create_wave_worktree(repo, "ep", 3, 1, "master")
    _fail_removal_of(stuck, monkeypatch, stderr)
    summary = ww.prune_orphan_worktrees(repo, "ep")
    monkeypatch.undo()

    reason = summary.failed[0].stderr
    assert "returned non-zero exit status 255" in reason


def test_prune_does_not_swallow_a_non_git_exception(repo, monkeypatch):
    """Only git's refusal is per-orphan; any other exception is a bug and must surface."""
    ww.create_wave_worktree(repo, "ep", 3, 1, "master")

    def boom(**_kwargs):
        raise RuntimeError("not a git failure")

    monkeypatch.setattr(ww, "cleanup_wave_worktree", boom)
    with pytest.raises(RuntimeError, match="not a git failure"):
        ww.prune_orphan_worktrees(repo, "ep")


def test_a_clean_prune_reports_no_failures(repo):
    ww.create_wave_worktree(repo, "ep", 1, 1, "master")
    assert ww.prune_orphan_worktrees(repo, "ep").failed == []


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to delete a busy cwd")
def test_a_tree_held_open_on_windows_is_reported_not_raised(repo):
    """The real shape: a process whose cwd is inside the tree makes `git worktree remove` fail."""
    held = ww.create_wave_worktree(repo, "ep", 3, 1, "master")
    other = ww.create_wave_worktree(repo, "ep", 3, 2, "master")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=held)
    try:
        summary = ww.prune_orphan_worktrees(repo, "ep")
    finally:
        holder.kill()
        holder.wait()
    assert not other.exists()
    assert [f.branch for f in summary.failed] == ["ep-w3-t1"]
    assert summary.failed[0].stderr
    assert summary.failed[0].has_unmerged_commits is False
    assert summary.branches_deleted == ["ep-w3-t2"]


# The CLI is how wave agents create and clean up trees: a git failure must say git's reason on
# stderr and exit 2, not dump a Python traceback.


def _create_args(repo: Path, base: str = "master") -> list[str]:
    return ["create", "--repo", str(repo), "--epic", "ep", "--wave", "3", "--task", "1",
            "--base", base]


def test_cli_create_twice_exits_2_with_gits_reason(repo, capsys):
    assert ww.main(_create_args(repo)) == 0
    capsys.readouterr()
    assert ww.main(_create_args(repo)) == 2
    err = capsys.readouterr().err
    assert err.startswith("wave-worktree error: ")
    assert "already exists" in err
    assert "Traceback" not in err


def test_cli_create_on_a_bad_base_exits_2_with_gits_reason(repo, capsys):
    assert ww.main(_create_args(repo, base="nosuchbranch")) == 2
    err = capsys.readouterr().err
    assert err.startswith("wave-worktree error: ")
    assert "nosuchbranch" in err
    assert "Traceback" not in err


def test_cli_cleanup_of_a_missing_tree_exits_2_with_gits_reason(repo, capsys):
    missing = repo.parent / "repo-ep-w9-t9"
    rc = ww.main(["cleanup", "--path", str(missing), "--branch", "ep-w9-t9", "--main", str(repo)])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("wave-worktree error: ")
    assert "is not a working tree" in err


def test_cli_does_not_swallow_a_non_git_exception(repo, monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("not a git failure")

    monkeypatch.setattr(ww, "create_wave_worktree", boom)
    with pytest.raises(RuntimeError, match="not a git failure"):
        ww.main(_create_args(repo))


# The wake-time prune runs on every wake, so it meets the in-flight task's own tree. Every tree a
# task still records in `wave_worktree_path` is live: the prune must keep it, or merge-back on the
# reconcile wake fetches from a directory that no longer exists.


def _committed_wave(repo: Path, wave: int, task: int) -> Path:
    path = ww.create_wave_worktree(repo, "ep", wave, task, "master")
    (path / f"t{task}.txt").write_text("wave work\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", f"feat(T{task}): wave work")
    return path


def test_a_live_tree_survives_the_prune_and_merges_back(repo):
    """The reported sequence: create, commit, wake-time prune, then merge-back on approval."""
    live = _committed_wave(repo, 4, 7)

    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={str(live)})

    assert live.exists()
    assert summary.worktrees_removed == 0
    assert summary.branches_kept == ["ep-w4-t7"]
    assert summary.branches_preserved == [] and summary.branches_deleted == []
    result = ww.merge_back(live, repo, "ep-w4-t7")
    assert result.success, result.stderr
    assert (repo / "t7.txt").read_text(encoding="utf-8") == "wave work\n"


def test_an_unkept_orphan_is_still_removed_beside_a_kept_tree(repo):
    live = _committed_wave(repo, 4, 7)
    orphan = ww.create_wave_worktree(repo, "ep", 3, 2, "master")

    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths=[live])

    assert live.exists()
    assert not orphan.exists()
    assert summary.worktrees_removed == 1
    assert summary.branches_deleted == ["ep-w3-t2"]
    assert summary.branches_kept == ["ep-w4-t7"]
    assert set(_listed(repo, "ep")) == {"repo-ep-w4-t7"}


def test_a_live_path_that_names_no_orphan_keeps_nothing(repo):
    """A kept path only spares the tree it names; a stale or foreign entry spares nothing."""
    orphan = ww.create_wave_worktree(repo, "ep", 3, 2, "master")
    summary = ww.prune_orphan_worktrees(
        repo, "ep", live_paths={str(repo.parent / "repo-ep-w9-t9"), str(repo), None, ""})
    assert not orphan.exists()
    assert summary.branches_kept == []
    assert summary.branches_deleted == ["ep-w3-t2"]


def test_no_live_paths_prunes_every_orphan_as_before(repo):
    """Backward compatible: existing callers pass nothing and every orphan still goes."""
    orphan = ww.create_wave_worktree(repo, "ep", 3, 2, "master")
    summary = ww.prune_orphan_worktrees(repo, "ep")
    assert not orphan.exists()
    assert summary.branches_kept == []


def test_a_bare_string_is_refused_rather_than_read_as_characters(repo):
    live = ww.create_wave_worktree(repo, "ep", 3, 2, "master")
    with pytest.raises(TypeError, match="collection of paths"):
        ww.prune_orphan_worktrees(repo, "ep", live_paths=str(live))
    assert live.exists()


def _spellings(path: Path) -> dict[str, str]:
    text = str(path)
    spelled = {
        "trailing separator": text + os.sep,
        "forward slashes": text.replace("\\", "/"),
        "forward slashes with trailing slash": text.replace("\\", "/") + "/",
        "dot segment": str(path.parent / "." / path.name),
        "parent segment": str(path.parent / "repo" / ".." / path.name),
    }
    if sys.platform == "win32":
        spelled["backslashes"] = text.replace("/", "\\")
        spelled["case variant"] = text.upper()
    return spelled


@pytest.mark.parametrize("form", [
    "trailing separator", "forward slashes", "forward slashes with trailing slash",
    "dot segment", "parent segment", "backslashes", "case variant"])
def test_a_live_path_in_another_spelling_still_keeps_its_tree(repo, form):
    live = _committed_wave(repo, 4, 7)
    spelled = _spellings(live)
    if form not in spelled:
        pytest.skip(f"{form} spells a different path off Windows")
    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={spelled[form]})
    assert live.exists(), spelled[form]
    assert summary.branches_kept == ["ep-w4-t7"]


def test_a_relative_live_path_is_read_against_the_cwd(repo, monkeypatch):
    live = _committed_wave(repo, 4, 7)
    monkeypatch.chdir(repo.parent)
    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={live.name})
    assert live.exists()
    assert summary.branches_kept == ["ep-w4-t7"]


# A task that goes back to ready is re-dispatched into a NEW tree (the wave number moves on), so
# its old tree must be released when it is requeued, not left recorded as live forever.


def test_releasing_a_clean_tree_removes_it_and_its_branch(repo):
    tree = ww.create_wave_worktree(repo, "ep", 1, 3, "master")
    summary = ww.release_wave_worktree(tree, "ep-w1-t3", repo)
    assert not tree.exists()
    assert summary.worktrees_removed == 1
    assert summary.branches_deleted == ["ep-w1-t3"]
    assert "ep-w1-t3" not in _branches(repo)


def test_releasing_a_tree_with_commits_keeps_its_branch(repo):
    """A requeued task may have committed before it wedged: the branch is the only copy."""
    tree = _committed_wave(repo, 1, 3)
    summary = ww.release_wave_worktree(tree, "ep-w1-t3", repo)
    assert not tree.exists()
    assert summary.branches_preserved == ["ep-w1-t3"]
    assert "ep-w1-t3" in _branches(repo)


def test_releasing_a_missing_tree_reports_gits_reason_instead_of_raising(repo):
    missing = repo.parent / "repo-ep-w9-t9"
    summary = ww.release_wave_worktree(missing, "ep-w9-t9", repo)
    assert summary.worktrees_removed == 0
    assert len(summary.failed) == 1
    assert summary.failed[0].branch == "ep-w9-t9"
    assert summary.failed[0].stderr


def test_a_requeued_task_leaves_only_its_new_tree_behind(repo):
    """Requeue releases the old tree; the next prune keeps the re-dispatched one and finds no
    other tree to remove."""
    old = ww.create_wave_worktree(repo, "ep", 1, 3, "master")
    ww.release_wave_worktree(old, "ep-w1-t3", repo)
    new = ww.create_wave_worktree(repo, "ep", 2, 3, "master")

    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={str(new)})

    assert new.exists() and not old.exists()
    assert summary.branches_kept == ["ep-w2-t3"]
    assert summary.worktrees_removed == 0
    assert set(_listed(repo, "ep")) == {"repo-ep-w2-t3"}


# The orchestrator skill is what calls these on a wake; the prose is the caller, so pin it.

_SKILL = Path(__file__).resolve().parents[2] / "orchestrator" / "SKILL.md"


def _skill_section(start: str, end: str) -> str:
    text = _SKILL.read_text(encoding="utf-8")
    return text[text.index(start):text.index(end)]


def test_the_wake_time_prune_passes_every_recorded_tree_as_live():
    step = _skill_section("**Step 1.2.5", "### Step 2:")
    call = step[step.index("summary = prune_orphan_worktrees("):]
    call = call[:call.index("\n)\n")]  # the closing paren on its own line
    assert "live_paths=live_paths" in call, call
    assert ("live_paths = {t.wave_worktree_path for t in state.tasks.values() "
            "if t.wave_worktree_path}") in step


@pytest.mark.parametrize("start,end,anchor", [
    ("**Step 1.2 —", "**Step 1.2.5", "wave abandoned at restart"),
    ("**Sub-step 4.0", "### Step 5:", "`requeue` (first timeout"),
    ("**Sub-step 4.0", "### Step 5:", "A human who resets this task to `ready`"),
    ("**Step 7.0", "### Step 8", 'For `actions[tid] == "requeue"`'),
    ("### Step 6:", "### Step 7:", "frontmatter for each task to run again"),
])
def test_every_return_to_ready_releases_the_wave_tree(start, end, anchor):
    section = _skill_section(start, end)
    line = next(ln for ln in section.splitlines() if anchor in ln)
    assert "**Releasing a wave tree**" in line, line


def test_a_best_of_n_task_marked_done_releases_its_unused_tree():
    """The live set keys on a set path, not on task state. Best-of-N builds in candidate trees
    and only the single-shot merge-back clears the path, so without a release on `done` the
    per-task tree would stay live, never pruned, for the rest of the epic."""
    section = _skill_section("**Step 7.0", "### Step 8")
    line = next(ln for ln in section.splitlines()
                if "For best-of-N/adversarial tasks" in ln and "mark done on approved" in ln)
    tail = line[line.index("mark done on approved"):]
    assert "**Releasing a wave tree**" in tail, line
    assert "best-of-N task marked `done`" in _skill_section(
        "**Releasing a wave tree (every return to `ready`).**", "### Step 2:")


def test_a_released_best_of_n_tree_is_gone_and_the_next_prune_keeps_nothing(repo):
    """Behaviour behind the pin above: candidate trees come and go, the per-task tree is
    released on done, and a later wake's live set (built from cleared paths) is empty."""
    task_tree = ww.create_wave_worktree(repo, "ep", 1, 4, "master")
    cand = ww.create_wave_worktree(repo, "ep", 1, "4-c0", "master")
    ww.cleanup_wave_worktree(cand, "ep-w1-t4-c0", repo, preserve_branch=False)

    released = ww.release_wave_worktree(task_tree, "ep-w1-t4", repo)
    recorded = [None]  # the task's cleared wave_worktree_path
    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths=recorded)

    assert released.worktrees_removed == 1 and not task_tree.exists()
    assert summary.branches_kept == [] and summary.worktrees_removed == 0
    assert not _listed(repo, "ep")


def test_releasing_a_path_with_no_branch_is_recorded_not_raised(repo):
    """A hand-edited state with a path and no branch must not crash the wake: the tree is left
    for the prune, which reads its branch from git once the path is cleared."""
    tree = ww.create_wave_worktree(repo, "ep", 1, 5, "master")
    summary = ww.release_wave_worktree(tree, None, repo)
    assert summary.worktrees_removed == 0
    assert len(summary.failed) == 1 and "no wave_branch" in summary.failed[0].stderr
    assert tree.exists()

    after = ww.prune_orphan_worktrees(repo, "ep", live_paths=[None])
    assert after.worktrees_removed == 1 and not tree.exists()


def test_the_release_procedure_clears_the_path_even_when_the_release_failed():
    proc = _skill_section("**Releasing a wave tree (every return to `ready`).**", "### Step 2:")
    assert "release_wave_worktree(" in proc
    assert "state.tasks[tid].wave_worktree_path = None" in proc
    assert "state.tasks[tid].wave_branch = None" in proc
    assert "Clear the fields even when the release failed" in proc


def test_a_live_path_reached_through_a_link_still_keeps_its_tree(repo):
    """A repo reached through a junction (Windows) or symlink records its tree under the link."""
    live = _committed_wave(repo, 4, 7)
    link = repo.parent.parent / "linked-parent"
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(repo.parent)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(repo.parent, target_is_directory=True)
    try:
        summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={str(link / live.name)})
    finally:
        os.rmdir(link) if sys.platform == "win32" else link.unlink()
    assert live.exists()
    assert summary.branches_kept == ["ep-w4-t7"]


@pytest.mark.skipif(sys.platform != "win32", reason="a case variant is a different path off Windows")
def test_a_case_variant_live_path_keeps_a_tree_whose_directory_is_gone(repo):
    """resolve() restores on-disk case only for a path that exists. A tree deleted under git is
    still registered and still recorded by its task; the match must not hinge on that directory."""
    live = ww.create_wave_worktree(repo, "ep", 4, 7, "master")
    shutil.rmtree(live)
    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={str(live).upper()})
    assert summary.branches_kept == ["ep-w4-t7"]
    assert summary.failed == [] and summary.branches_deleted == []


# A best-of-N task records only its own "-t<id>" tree; its candidates build in "-t<id>-c<i>"
# trees that state never names. While the task's tree is live its candidates may still have
# agents working in them, so a candidate whose parent tree is live is kept, tree and branch.


def _best_of_n(repo: Path, wave: int, task: int, n: int = 2) -> tuple[Path, list[Path]]:
    parent = ww.create_wave_worktree(repo, "ep", wave, task, "master")
    cands = [ww.create_wave_worktree(repo, "ep", wave, f"{task}-c{i}", "master")
             for i in range(n)]
    (cands[1] / "cand.txt").write_text("candidate work\n", encoding="utf-8")
    _git(cands[1], "add", "-A")
    _git(cands[1], "commit", "-m", "candidate commit")
    return parent, cands


def test_a_live_task_keeps_its_candidate_trees_and_branches(repo):
    parent, cands = _best_of_n(repo, 3, 5)

    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={str(parent)})

    assert parent.exists() and all(c.exists() for c in cands)
    assert sorted(summary.branches_kept) == ["ep-w3-t5", "ep-w3-t5-c0", "ep-w3-t5-c1"]
    assert summary.worktrees_removed == 0
    assert summary.branches_deleted == [] and summary.branches_preserved == []
    assert {"ep-w3-t5-c0", "ep-w3-t5-c1"} <= _branches(repo)


def test_candidates_of_a_task_that_is_not_live_are_still_pruned(repo):
    live, live_cands = _best_of_n(repo, 3, 5)
    dead, dead_cands = _best_of_n(repo, 2, 4)

    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={str(live)})

    assert all(c.exists() for c in live_cands)
    assert not dead.exists() and not any(c.exists() for c in dead_cands)
    assert sorted(summary.branches_kept) == ["ep-w3-t5", "ep-w3-t5-c0", "ep-w3-t5-c1"]
    assert sorted(summary.branches_deleted) == ["ep-w2-t4", "ep-w2-t4-c0"]
    assert summary.branches_preserved == ["ep-w2-t4-c1"]
    assert "ep-w2-t4-c1" in _branches(repo) and "ep-w2-t4-c0" not in _branches(repo)


def test_candidates_are_kept_only_by_their_own_wave(repo):
    """The wave number is part of the parent: a live "-w3-t5" does not keep "-w2-t5-c0"."""
    live = ww.create_wave_worktree(repo, "ep", 3, 5, "master")
    other = ww.create_wave_worktree(repo, "ep", 2, "5-c0", "master")
    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={str(live)})
    assert not other.exists()
    assert summary.branches_kept == ["ep-w3-t5"]
    assert summary.branches_deleted == ["ep-w2-t5-c0"]


def test_a_live_task_does_not_keep_the_candidates_of_a_longer_task_id(repo):
    """"-t1" is a string prefix of "-t10": a live task 1 must not keep task 10's candidates."""
    live = ww.create_wave_worktree(repo, "ep", 3, 1, "master")
    t10 = ww.create_wave_worktree(repo, "ep", 3, "10-c0", "master")
    own = ww.create_wave_worktree(repo, "ep", 3, "1-c0", "master")

    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={str(live)})

    assert not t10.exists() and own.exists()
    assert sorted(summary.branches_kept) == ["ep-w3-t1", "ep-w3-t1-c0"]
    assert summary.branches_deleted == ["ep-w3-t10-c0"]


def test_a_live_candidate_path_does_not_keep_its_sibling_candidates(repo):
    """Only a live "-t<id>" parent keeps candidates; naming one candidate keeps that one alone."""
    parent, cands = _best_of_n(repo, 3, 5)
    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={str(cands[0])})
    assert cands[0].exists() and not cands[1].exists() and not parent.exists()
    assert summary.branches_kept == ["ep-w3-t5-c0"]


@pytest.mark.parametrize("suffix", ["-cx", "-c", "-c0x"])
def test_a_non_digit_suffix_is_not_kept_as_a_candidate(repo, suffix):
    """A "-c" suffix without exactly digits is no candidate: nothing claims it, kept or pruned."""
    live = ww.create_wave_worktree(repo, "ep", 3, 5, "master")
    odd = _raw_worktree(repo, f"repo-ep-w3-t5{suffix}", f"ep-w3-t5{suffix}")
    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={str(live)})
    assert odd.exists()
    assert summary.branches_kept == ["ep-w3-t5"]
    assert summary.worktrees_removed == 0


@pytest.mark.parametrize("form", [
    "trailing separator", "forward slashes", "forward slashes with trailing slash",
    "dot segment", "parent segment", "backslashes", "case variant"])
def test_a_parent_in_another_spelling_still_keeps_its_candidates(repo, form):
    parent, cands = _best_of_n(repo, 3, 5)
    spelled = _spellings(parent)
    if form not in spelled:
        pytest.skip(f"{form} spells a different path off Windows")
    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={spelled[form]})
    assert all(c.exists() for c in cands), spelled[form]
    assert sorted(summary.branches_kept) == ["ep-w3-t5", "ep-w3-t5-c0", "ep-w3-t5-c1"]


def test_a_live_parent_whose_tree_is_gone_still_keeps_its_candidates(repo):
    """The parent tree is never built in under best-of-N; its directory going missing must not
    expose the candidates while the task still records the path."""
    parent, cands = _best_of_n(repo, 3, 5)
    shutil.rmtree(parent)
    summary = ww.prune_orphan_worktrees(repo, "ep", live_paths={str(parent)})
    assert all(c.exists() for c in cands)
    assert {"ep-w3-t5-c0", "ep-w3-t5-c1"} <= set(summary.branches_kept)


def test_the_wake_time_prune_says_candidates_of_a_live_task_are_kept():
    step = _skill_section("**Step 1.2.5", "### Step 2:")
    assert "best-of-N candidate" in step and "-c<i>" in step, step
    # Pin the keep rule itself, not just the topic: a paragraph inverted to "pruned" must fail.
    flat = " ".join(step.split())
    assert (
        "The prune keeps it, tree and branch, whenever its task's own `<slug>-w<n>-t<id>` tree "
        "is in `live_paths`, and lists it in `summary.branches_kept`"
    ) in flat, flat
    assert "A candidate whose task tree is not live is an orphan like any other." in flat, flat
    assert "ends `blocked` keeps its tree recorded, so its candidate trees stay too" in flat, flat


# merge_back lands a wave branch on the epic branch. Step 7.0 creates every task's tree from the
# same epic HEAD and disjoint tasks run together, so once the first sibling lands every other one
# has diverged: a fast-forward-only merge refused them all. A fast-forward is still taken when
# possible; otherwise a merge commit lands the branch, and a real conflict is aborted so the epic
# tree is left exactly as it was. The task commit is never rewritten: Step 7 matches task commits
# by signature in the log, so its sha must survive the landing.


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _commit_in(tree: Path, name: str, body: str, message: str) -> str:
    (tree / name).write_text(body, encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-m", message)
    return _head(tree)


def _sibling(repo: Path, task: int, name: str, body: str) -> tuple[Path, str]:
    tree = ww.create_wave_worktree(repo, "ep", 1, task, "master")
    return tree, _commit_in(tree, name, body, f"feat(T{task}): wave work")


def _is_ancestor(repo: Path, commit: str, of: str) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, of],
                          capture_output=True, text=True)
    return proc.returncode == 0


def _parents(repo: Path, commit: str) -> list[str]:
    return _git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:]


def _merge_in_progress(repo: Path) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          capture_output=True, text=True)
    return proc.returncode == 0


def test_a_single_wave_ahead_of_the_epic_fast_forwards(repo):
    tree, sha = _sibling(repo, 1, "t1.txt", "one\n")

    result = ww.merge_back(tree, repo, "ep-w1-t1")

    assert result.success, result.stderr
    assert result.stderr == ""
    assert _head(repo) == sha  # a fast-forward: no merge commit on top of the task commit


def test_disjoint_siblings_from_one_base_both_land(repo):
    """The reported defect: the second sibling had diverged and the ff-only merge refused it."""
    t1, sha1 = _sibling(repo, 1, "t1.txt", "one\n")
    t2, sha2 = _sibling(repo, 2, "t2.txt", "two\n")

    first = ww.merge_back(t1, repo, "ep-w1-t1")
    assert first.success, first.stderr
    assert _head(repo) == sha1
    second = ww.merge_back(t2, repo, "ep-w1-t2")
    assert second.success, second.stderr

    head = _head(repo)
    assert (repo / "t1.txt").read_text(encoding="utf-8") == "one\n"
    assert (repo / "t2.txt").read_text(encoding="utf-8") == "two\n"
    # Neither task commit was rewritten: both shas are still what the wave trees committed.
    assert _head(t1) == sha1 and _head(t2) == sha2
    assert _is_ancestor(repo, sha1, head) and _is_ancestor(repo, sha2, head)
    assert _parents(repo, head) == [sha1, sha2]
    assert "ep-w1-t2" in _git(repo, "log", "-1", "--format=%s", head)
    assert _git(repo, "status", "--porcelain") == ""


def test_a_real_conflict_is_refused_and_leaves_the_epic_tree_untouched(repo):
    t1, sha1 = _sibling(repo, 1, "f.txt", "from task one\n")
    t2, _ = _sibling(repo, 2, "f.txt", "from task two\n")
    assert ww.merge_back(t1, repo, "ep-w1-t1").success

    result = ww.merge_back(t2, repo, "ep-w1-t2")

    assert result.success is False
    assert result.stderr.strip()
    assert "conflict" in result.stderr.lower()
    assert _head(repo) == sha1
    assert not _merge_in_progress(repo)
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / "f.txt").read_text(encoding="utf-8") == "from task one\n"


def test_a_conflict_abort_failure_is_reported_alongside_the_conflict(repo, monkeypatch):
    t1, _ = _sibling(repo, 1, "f.txt", "from task one\n")
    t2, _ = _sibling(repo, 2, "f.txt", "from task two\n")
    assert ww.merge_back(t1, repo, "ep-w1-t1").success
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "merge", "--abort"]:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: abort broke\n")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ww.subprocess, "run", run)
    result = ww.merge_back(t2, repo, "ep-w1-t2")
    monkeypatch.undo()

    assert result.success is False
    assert "conflict" in result.stderr.lower()
    assert "abort broke" in result.stderr
    _git(repo, "merge", "--abort")  # leave the tmp repo tidy


def test_a_refused_merge_that_started_no_merge_does_not_run_an_abort(repo, monkeypatch):
    """A merge git refuses before starting (here: a dirty tree it would overwrite) leaves no
    MERGE_HEAD, so there is nothing to abort; an unconditional `merge --abort` would fail and
    bury git's real reason under its own."""
    t1, _ = _sibling(repo, 1, "t1.txt", "one\n")
    t2, _ = _sibling(repo, 2, "t2.txt", "two\n")
    assert ww.merge_back(t1, repo, "ep-w1-t1").success
    (repo / "t2.txt").write_text("untracked local\n", encoding="utf-8")
    calls: list[list[str]] = []
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ww.subprocess, "run", run)
    result = ww.merge_back(t2, repo, "ep-w1-t2")
    monkeypatch.undo()

    assert result.success is False
    assert "t2.txt" in result.stderr
    assert ["git", "merge", "--abort"] not in calls
    assert "abort" not in result.stderr.lower().replace("aborting", "")
    assert (repo / "t2.txt").read_text(encoding="utf-8") == "untracked local\n"


def test_a_fetch_from_a_missing_wave_tree_fails(repo):
    before = _head(repo)

    result = ww.merge_back(repo.parent / "repo-ep-w9-t9", repo, "ep-w9-t9")

    assert result.success is False
    assert result.stderr.strip()
    assert _head(repo) == before


def test_cli_merge_back_lands_disjoint_siblings_with_exit_0(repo, capsys):
    """The reported repro through the CLI: create twice, commit disjoint files, merge both."""
    import json

    for t in (1, 2):
        assert ww.main(["create", "--repo", str(repo), "--epic", "omega", "--wave", "1",
                        "--task", str(t), "--base", "master", "--json"]) == 0
    capsys.readouterr()
    for t in (1, 2):
        _commit_in(repo.parent / f"repo-omega-w1-t{t}", f"f{t}.txt", f"{t}\n", f"T{t}")

    for t in (1, 2):
        rc = ww.main(["merge-back", "--wave-worktree", str(repo.parent / f"repo-omega-w1-t{t}"),
                      "--main", str(repo), "--branch", f"omega-w1-t{t}"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0, out
        assert out == {"success": True, "stderr": ""}
    assert (repo / "f1.txt").exists() and (repo / "f2.txt").exists()


def test_a_sibling_fetch_landing_between_fetch_and_merge_cannot_swap_the_tip(repo, monkeypatch):
    """Best-of-N group tasks land concurrently into one main worktree. When the fetch went to the
    shared FETCH_HEAD, a sibling's fetch landing between this one's fetch and its merge made this
    merge-back merge the sibling's tip and report success while its own commit landed nowhere."""
    t1, sha1 = _sibling(repo, 1, "f.txt", "from task one\n")
    t2, sha2 = _sibling(repo, 2, "t2.txt", "two\n")
    real = ww._git_in
    interleaved = []

    def git_in(cwd, *args):
        proc = real(cwd, *args)
        if args[0] == "fetch" and not interleaved:
            # Run B's fetch exactly as merge_back would build it, right after A's.
            interleaved.append(
                real(cwd, *[a.replace("ep-w1-t1", "ep-w1-t2") for a in args]).returncode)
        return proc

    monkeypatch.setattr(ww, "_git_in", git_in)
    first = ww.merge_back(t1, repo, "ep-w1-t1")
    monkeypatch.undo()
    second = ww.merge_back(t2, repo, "ep-w1-t2")

    assert interleaved == [0]
    assert first.success, first.stderr
    assert second.success, second.stderr
    head = _head(repo)
    assert _is_ancestor(repo, sha1, head) and _is_ancestor(repo, sha2, head)
    assert (repo / "f.txt").read_text(encoding="utf-8") == "from task one\n"


def test_merge_back_leaves_no_landing_ref_behind(repo):
    t1, _ = _sibling(repo, 1, "t1.txt", "one\n")
    t2, _ = _sibling(repo, 2, "f.txt", "conflicting\n")
    _commit_in(repo, "f.txt", "epic moved\n", "epic work")

    assert ww.merge_back(t1, repo, "ep-w1-t1").success
    assert ww.merge_back(t2, repo, "ep-w1-t2").success is False

    assert _git(repo, "for-each-ref", "refs/wave-land/") == ""


def test_a_failing_merge_hook_refuses_the_merge_commit_and_leaves_the_tree_clean(repo):
    """A fast-forward never ran merge hooks; a merge commit does. A hook refusal must fail the
    land and leave the epic tree as it was, with the hook's own words in stderr."""
    t1, sha1 = _sibling(repo, 1, "t1.txt", "one\n")
    t2, _ = _sibling(repo, 2, "t2.txt", "two\n")
    assert ww.merge_back(t1, repo, "ep-w1-t1").success
    hooks = Path(_git(repo, "rev-parse", "--git-path", "hooks").strip())
    if not hooks.is_absolute():
        hooks = repo / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-merge-commit"
    hook.write_text("#!/bin/sh\necho 'fake hook says no' >&2\nexit 1\n", encoding="utf-8",
                    newline="\n")
    hook.chmod(0o755)

    result = ww.merge_back(t2, repo, "ep-w1-t2")

    assert result.success is False
    assert "fake hook says no" in result.stderr
    assert not _merge_in_progress(repo)
    assert _git(repo, "status", "--porcelain") == ""
    assert _head(repo) == sha1


def test_a_refused_merge_does_not_abort_a_concurrent_merge_in_progress(repo):
    """A merge-back that fails because another one is stopped mid-merge must not abort that
    other merge: the MERGE_HEAD is not its own."""
    t1, _ = _sibling(repo, 1, "f.txt", "from task one\n")
    t2, _ = _sibling(repo, 2, "f.txt", "from task two\n")
    t3, _ = _sibling(repo, 3, "t3.txt", "three\n")
    assert ww.merge_back(t1, repo, "ep-w1-t1").success
    other = subprocess.run(["git", "-C", str(repo), "merge", "--no-ff", "--no-edit", "ep-w1-t2"],
                           capture_output=True, text=True)
    assert other.returncode != 0 and _merge_in_progress(repo)
    theirs = _git(repo, "rev-parse", "MERGE_HEAD").strip()

    result = ww.merge_back(t3, repo, "ep-w1-t3")

    assert result.success is False
    assert _merge_in_progress(repo)
    assert _git(repo, "rev-parse", "MERGE_HEAD").strip() == theirs
    _git(repo, "merge", "--abort")


def _ff_that_lands_nothing(monkeypatch) -> None:
    """A `merge --ff-only` that exits 0 without moving HEAD: git's word with no land behind it."""
    real = ww._git_in

    def git_in(cwd, *args):
        if args[:2] == ("merge", "--ff-only"):
            return subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")
        return real(cwd, *args)

    monkeypatch.setattr(ww, "_git_in", git_in)


def test_a_merge_that_reports_success_without_landing_the_tip_fails(repo, monkeypatch):
    """gh#1049: merge_back's success is the tip being in HEAD, not git's exit code."""
    tree, sha = _sibling(repo, 1, "t1.txt", "one\n")
    before = _head(repo)
    _ff_that_lands_nothing(monkeypatch)

    result = ww.merge_back(tree, repo, "ep-w1-t1")

    assert result.success is False
    assert f"{sha} is not in HEAD" in result.stderr
    assert _head(repo) == before


def test_cli_merge_back_exits_2_when_the_tip_is_not_in_head(repo, monkeypatch, capsys):
    """gh#1049: the CLI the workflow calls carries the same verdict as its exit code."""
    import json

    tree, sha = _sibling(repo, 1, "t1.txt", "one\n")
    _ff_that_lands_nothing(monkeypatch)

    rc = ww.main(["merge-back", "--wave-worktree", str(tree), "--main", str(repo),
                  "--branch", "ep-w1-t1"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 2
    assert out["success"] is False
    assert f"{sha} is not in HEAD" in out["stderr"]
