"""gh#503: crash recovery must see best-of-N candidate worktrees.

wave_group.js creates each best-of-N candidate with `--task "<id>-c<i>"`, so the candidate's
worktree lands at `<repo>-<epic>-w<n>-t<id>-c<i>`. The orphan scan used to anchor on `-t<id>$`,
so after a crash `prune_orphan_worktrees` never saw a single candidate and every one leaked.

Every test drives REAL git in pytest's tmp_path - no mock stands in for `git worktree`, because
the defect lived in how a real porcelain path meets the pattern.
"""

from __future__ import annotations

import subprocess
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
    for k, v in (("user.email", "t@e.st"), ("user.name", "t")):
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
