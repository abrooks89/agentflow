"""gh#498: a worktree whose lane is live, or whose tree changed since it was measured, reports
as refused. Report-only (Q-10093): no test here expects anything to be deleted.

Every test drives REAL git: a bare remote, a canonical clone, and a per-epic worktree registered
under `<worktree_root>/<project>/`, all in tmp_path. The registry is the real template with rows
edited in, parsed by the real parser - the liveness check is only as good as its read of that file.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentflow import registry as _registry
from agentflow import worktree_remove as wr

PROJECT = "demo"
EPIC_DIR = "e0787-a-new-human-surface"
EPIC_ID = "E-0787-a-new-human-surface"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def _identity(repo: Path) -> None:
    for k, v in (("user.email", "t@e.st"), ("user.name", "t"), ("core.autocrlf", "false")):
        _git(repo, "config", k, v)


@pytest.fixture
def fleet(tmp_path: Path):
    """(checkout, worktree_root, worktree, registry_path) for one merged, clean epic tree."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "master", str(bare)], check=True, capture_output=True
    )
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", "master", str(seed)], check=True, capture_output=True)
    _identity(seed)
    (seed / "f.txt").write_text("hello\n", encoding="utf-8")
    # This repo's own ignore rules, in miniature: a nested harness worktree and a `.bak` copy are
    # ignored, so `git worktree remove` would delete them without a word.
    (seed / ".gitignore").write_text(
        ".claude/worktrees/\n*.bak*\n__pycache__/\n.pytest_cache/\n", encoding="utf-8"
    )
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "master")

    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", str(bare), str(checkout)], check=True, capture_output=True)
    _identity(checkout)

    worktree_root = tmp_path / "wt"
    (worktree_root / PROJECT).mkdir(parents=True)
    worktree = worktree_root / PROJECT / EPIC_DIR
    # The shape gh#498 measured: branch at master, no commits of its own, nothing written yet.
    _git(checkout, "worktree", "add", str(worktree), "-b", EPIC_DIR, "master")
    _identity(worktree)

    registry_path = tmp_path / "registry.md"
    registry_path.write_text(_registry._template_text(PROJECT), encoding="utf-8")
    return checkout, worktree_root, worktree, registry_path


def _set_agent(
    registry_path: Path, agent: str, status: str, work: str, phase: str = "build"
) -> None:
    text = registry_path.read_text(encoding="utf-8")
    old = f"| {agent} | idle | - | - | - | - | - |"
    assert text.count(old) == 1, f"template row for {agent} not found exactly once"
    new = f"| {agent} | {status} | {work} | {phase} | 2026-09-22T19:57:00Z | - | - |"
    registry_path.write_text(text.replace(old, new), encoding="utf-8")


def _add_queue(registry_path: Path, qid: str, lane: str, claimed_by: str, state: str) -> None:
    text = registry_path.read_text(encoding="utf-8")
    header = "| id | work item | lane | claimed_by | state | waiting_on | notes |\n|---|---|---|---|---|---|---|\n"
    assert text.count(header) == 1, "Queue table header not found exactly once"
    row = f"| {qid} | a-new-human-surface | {lane} | {claimed_by} | {state} | - | - |\n"
    registry_path.write_text(text.replace(header, header + row), encoding="utf-8")


def _keys(v: wr.Verdict) -> list[str]:
    return [r.key for r in v.refusals]


def _check(fleet, worktree=None) -> wr.Verdict:
    checkout, root, wt, reg = fleet
    return wr.check(checkout, root, PROJECT, worktree or wt, reg)


def _report(fleet, worktree=None) -> tuple[wr.Verdict, list[str]]:
    """The verdict and the manual removal command the report would print for it."""
    checkout, root, wt, reg = fleet
    v = wr.check(checkout, root, PROJECT, worktree or wt, reg)
    return v, wr.removal_command(checkout, v)


def _names_the_tree(cmd: list[str], wt: Path) -> bool:
    """*cmd* is the manual pair: `worktree remove` on this tree, then `worktree prune`."""
    flat = [c.replace("\\", "/").lower() for c in cmd]
    want = str(wt).replace("\\", "/").lower()
    return (
        len(flat) == 2
        and "worktree remove" in flat[0]
        and want in flat[0]
        and flat[1].endswith("worktree prune")
    )


# --------------------------------------------------------------------------------------------- #
# The allowed case - without it every refusal test below could pass against a helper that
# refuses everything.
# --------------------------------------------------------------------------------------------- #
def test_a_genuinely_dead_tree_is_removable_and_left_in_place(fleet):
    checkout, _, wt, _ = fleet
    v, cmd = _report(fleet)
    assert v.removable, [str(r) for r in v.refusals]
    assert v.epic == "E-0787" and v.branch == EPIC_DIR
    assert _names_the_tree(cmd, wt), cmd
    assert wt.is_dir(), "report-only: the tree stays"
    assert (
        str(wt).replace("\\", "/").lower()
        in _git(checkout, "worktree", "list").replace("\\", "/").lower()
    )


def test_an_idle_or_reaped_row_and_a_done_queue_row_do_not_hold_the_tree(fleet):
    """A reaped row as `reap` writes it - idle, with the reaped phase - does not hold the tree."""
    _, _, _, reg = fleet
    _set_agent(reg, "jr-dev-1", "idle", EPIC_ID, phase="-")
    _set_agent(reg, "jr-dev-2", "idle", EPIC_ID, phase=_registry.REAPED_PHASE)
    _add_queue(reg, "E-0787", "jr-dev-2", "jr-dev-2", "done")
    _add_queue(reg, "E-0788", "jr-dev-2", "-", "ready")
    assert _check(fleet).removable, [str(r) for r in _check(fleet).refusals]


def test_a_reaped_phase_on_a_non_idle_row_still_holds_the_tree(fleet):
    """The phase is a label anyone can type; only the status says whether the lane let go."""
    _set_agent(fleet[3], "jr-dev-2", "building", EPIC_ID, phase=_registry.REAPED_PHASE)
    assert _keys(_check(fleet)) == ["worktree_lane_live"]


def test_a_live_lane_on_a_different_epic_does_not_hold_the_tree(fleet):
    _set_agent(fleet[3], "lead-dev", "building", "E-0786-domain-c-deployed-vs")
    assert _check(fleet).removable


# --------------------------------------------------------------------------------------------- #
# Guard 1: an ## Agents row live on the epic. This is gh#498's exact board.
# --------------------------------------------------------------------------------------------- #
def test_a_live_lane_building_on_the_epic_refuses_and_leaves_the_tree(fleet):
    _, _, wt, reg = fleet
    _set_agent(reg, "jr-dev-2", "building", EPIC_ID)
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_lane_live"]
    assert "jr-dev-2" in str(v.refusals[0])
    assert wt.is_dir() and cmd == []


@pytest.mark.parametrize("status", ["waiting", "halted"])
def test_a_parked_lane_still_holds_its_tree(fleet, status):
    _set_agent(fleet[3], "jr-dev-1", status, EPIC_ID)
    assert _keys(_check(fleet)) == ["worktree_lane_live"]


@pytest.mark.parametrize("status", ["Building", "`building`", "blocked", "idle-ish", "`Idle`"])
def test_a_status_the_validator_rejects_refuses_as_unreadable(fleet, status):
    """A capitalised, backticked or unknown status fails `registry validate`, so nothing is read."""
    _set_agent(fleet[3], "jr-dev-1", status, EPIC_ID)
    v = _check(fleet)
    assert _keys(v) == ["worktree_registry_unreadable"], [str(r) for r in v.refusals]
    assert "registry validate fails" in str(v.refusals[0])


# --------------------------------------------------------------------------------------------- #
# Guard 2: a ## Queue row claimed or in flight on the epic.
# --------------------------------------------------------------------------------------------- #
def test_a_claimed_queue_row_refuses_and_leaves_the_tree(fleet):
    _, _, wt, reg = fleet
    _add_queue(reg, "E-0787", "jr-dev-2", "jr-dev-2", "building")
    v, _ = _report(fleet)
    assert _keys(v) == ["worktree_queue_live"]
    assert wt.is_dir()


def test_a_ready_row_with_a_claimant_refuses(fleet):
    _add_queue(fleet[3], "E-0787", "jr-dev-2", "jr-dev-2", "ready")
    assert _keys(_check(fleet)) == ["worktree_queue_live"]


@pytest.mark.parametrize("state", ["Done", "shipped", "", "Ready"])
def test_an_unrecognised_queue_state_on_the_epic_refuses_even_unclaimed(fleet, state):
    _add_queue(fleet[3], "E-0787", "jr-dev-2", "-", state)
    assert _keys(_check(fleet)) == ["worktree_registry_unreadable"]


def test_an_unclaimed_ready_row_on_the_epic_holds_the_tree(fleet):
    """Only a terminal state releases the tree: a `ready` row is an epic about to be claimed."""
    _add_queue(fleet[3], "E-0787", "jr-dev-2", "-", "ready")
    assert _keys(_check(fleet)) == ["worktree_queue_live"]


# --------------------------------------------------------------------------------------------- #
# A registry the parser could not fully read is unreadable, not empty (fail closed).
# --------------------------------------------------------------------------------------------- #
def test_an_agents_table_missing_a_lane_refuses_as_unreadable(fleet):
    reg = fleet[3]
    text = reg.read_text(encoding="utf-8")
    old = "| jr-dev-2 | idle | - | - | - | - | - |\n"
    assert text.count(old) == 1
    reg.write_text(text.replace(old, ""), encoding="utf-8")
    v = _check(fleet)
    assert _keys(v) == ["worktree_registry_unreadable"] and "jr-dev-2" in str(v.refusals[0])


def test_an_agents_row_the_parser_dropped_refuses_as_unreadable(fleet):
    """A mangled row naming the epic - here too few cells - is silently dropped by the parser."""
    reg = fleet[3]
    text = reg.read_text(encoding="utf-8")
    anchor = "| dev-manager | idle | - | - | - | - | - |\n"
    assert text.count(anchor) == 1
    reg.write_text(
        text.replace(anchor, anchor + f"| jr-dev-2 building | {EPIC_ID} | build |\n"),
        encoding="utf-8",
    )
    assert _keys(_check(fleet)) == ["worktree_registry_unreadable"]


# --------------------------------------------------------------------------------------------- #
# Guard 3: re-measured at report time. An earlier "clean" is not an input.
# --------------------------------------------------------------------------------------------- #
def test_a_tree_that_changed_after_an_earlier_clean_measurement_refuses(fleet):
    """gh#498's four minutes: measured clean, then a lane staged a brand-new file."""
    _, _, wt, _ = fleet
    assert _check(fleet).removable, "precondition: the earlier measurement says clean"
    (wt / "only-copy.md").write_text("exists nowhere else\n", encoding="utf-8")
    _git(wt, "add", "only-copy.md")
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_dirty_at_removal"]
    assert (wt / "only-copy.md").is_file() and cmd == []


def test_an_untracked_file_refuses(fleet):
    (fleet[2] / "scratch.txt").write_text("x\n", encoding="utf-8")
    assert _keys(_check(fleet)) == ["worktree_dirty_at_removal"]


def test_a_tree_that_committed_after_an_earlier_clean_measurement_refuses(fleet):
    _, _, wt, _ = fleet
    assert _check(fleet).removable, "precondition: the earlier measurement says clean"
    (wt / "g.txt").write_text("new\n", encoding="utf-8")
    _git(wt, "add", "g.txt")
    _git(wt, "commit", "-m", "local only")
    v, _ = _report(fleet)
    assert _keys(v) == ["worktree_unpushed_at_removal"], (
        "status is clean; only the log guard sees it"
    )
    assert wt.is_dir()


# --------------------------------------------------------------------------------------------- #
# The fail-closed preconditions.
# --------------------------------------------------------------------------------------------- #
def test_the_canonical_checkout_is_outside_the_root(fleet):
    assert _keys(_check(fleet, worktree=fleet[0])) == ["worktree_outside_root"]


def test_an_unregistered_directory_refuses(fleet):
    stray = fleet[1] / PROJECT / "e0999-never-added"
    stray.mkdir()
    assert _keys(_check(fleet, worktree=stray)) == ["worktree_not_registered"]


def _deregister(fleet) -> Path:
    """`git worktree remove` on the fixture's tree, as the printed command would run it."""
    checkout, _, wt, _ = fleet
    _git(checkout, "worktree", "remove", str(wt))
    assert not wt.exists(), "precondition: an unheld tree is removed whole"
    return wt


#: Recursive or forcing deletes. The hint must never print one: each can take content, and
#: `Remove-Item` on a junction is how Windows PowerShell 5.1 empties the junction's target.
_DANGEROUS = ("rm -r", "rm -f", "rmdir /s", "rd /s", "Remove-Item", "-Recurse", "-Force", "rmtree")

#: Shown only beside a removal step.
_EMPTY_STEP = ('rmdir "', "[IO.Directory]::Delete(")


def _not_registered_text(fleet, worktree=None) -> str:
    v, cmd = _report(fleet, worktree=worktree)
    assert _keys(v) == ["worktree_not_registered"], [str(r) for r in v.refusals]
    assert cmd == []
    text = str(v.refusals[0])
    assert text.startswith(
        f"refused [worktree_not_registered]: {wr.RULES['worktree_not_registered']}"
    )
    for bad in _DANGEROUS:
        assert bad not in text, f"the hint prints a delete that can take content: {bad!r}"
    return text


def test_a_deregistered_empty_directory_still_on_disk_is_named_as_residue(fleet):
    """The Windows shape: git deregistered the tree, but a process held the directory, so the
    delete failed and an empty directory stayed. The refusal says what it is and how to clear it."""
    wt = _deregister(fleet)
    wt.mkdir()
    text = _not_registered_text(fleet)
    # Measured facts as facts; the history as a condition the guard never checked.
    assert "Git does not list it; if it was a worktree of this checkout" in text
    assert "It is empty" in text and "already deregistered" not in text
    # Addressed to a human: the steward reads this and must not take it as its own step.
    assert "A human clears it by closing any shell or process" in text
    p = str(wt.absolute())
    assert f'`rmdir "{p}"` in bash' in text
    assert f'`[IO.Directory]::Delete("{p}")` in PowerShell' in text
    assert wt.is_dir(), "report-only: the residue stays"


def test_the_residue_step_never_claims_rmdir_refuses_in_every_shell(fleet):
    """PowerShell's `rmdir` is `Remove-Item`: it prompts and, on a yes, deletes the contents. The
    refuse-when-not-empty claim may stand only beside forms for which it is true."""
    wt = _deregister(fleet)
    wt.mkdir()
    text = _flat(_not_registered_text(fleet))
    assert "rmdir" not in text.split("in bash", 1)[1].split("(not PowerShell's `rmdir`", 1)[0]
    assert "not PowerShell's `rmdir`" in text
    assert "Both refuse a directory that is not empty" in text


@pytest.mark.skipif(sys.platform != "win32", reason="checks Windows PowerShell's own rmdir")
def test_powershell_rmdir_is_the_recursive_delete_the_hint_steers_away_from():
    """The fact the hint's wording rests on, measured on this host rather than remembered."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "(Get-Alias rmdir).Definition"],
        capture_output=True, text=True, check=True,
    )
    assert proc.stdout.strip() == "Remove-Item"


def test_the_powershell_form_refuses_a_directory_that_is_not_empty(tmp_path):
    """`[IO.Directory]::Delete(path)` is the non-recursive overload, as `os.rmdir` is."""
    d = tmp_path / "left"
    (d / "nested").mkdir(parents=True)
    with pytest.raises(OSError):
        os.rmdir(d)
    assert (d / "nested").is_dir()
    if sys.platform == "win32":
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"[IO.Directory]::Delete('{d}')"],
            capture_output=True, text=True,
        )
        assert proc.returncode != 0 and "not empty" in (proc.stderr + proc.stdout)
        assert (d / "nested").is_dir()


def test_a_deregistered_directory_that_is_gone_gets_no_residue_hint(fleet):
    _deregister(fleet)
    text = _not_registered_text(fleet)
    assert "is not in `git -C" in text
    assert "a directory is at that path" not in text
    assert not any(s in text for s in _EMPTY_STEP)


def test_an_unregistered_directory_that_is_not_empty_gets_no_removal_step(fleet):
    """A leftover with nested directories (the holding shell sat in `wt/src`), or a full tree
    copied in: not residue, so no `rmdir`, and what is inside is named for the human."""
    wt = _deregister(fleet)
    (wt / "src" / "deep").mkdir(parents=True)
    (wt / "notes.txt").write_text("keep me\n", encoding="utf-8")
    text = _not_registered_text(fleet)
    assert "It is not empty: it holds 2 entries ('notes.txt', 'src')" in text
    assert "residue" not in text.replace(str(wt), "").replace(str(wt.absolute()), "")
    assert "It is empty" not in text
    assert not any(s in text for s in _EMPTY_STEP)
    assert "a human checks what is inside before deleting anything by hand" in text
    assert (wt / "notes.txt").is_file()


def test_a_long_leftover_listing_is_elided(fleet):
    wt = _deregister(fleet)
    wt.mkdir()
    for i in range(wr._RESIDUE_PREVIEW + 2):
        (wt / f"f{i}").write_text("x", encoding="utf-8")
    text = _not_registered_text(fleet)
    assert f"holds {wr._RESIDUE_PREVIEW + 2} entries" in text and ", ...)" in text
    assert f"'f{wr._RESIDUE_PREVIEW}'" not in text


def test_a_live_tree_of_another_repository_is_named_and_not_called_residue(fleet, tmp_path):
    """A worktree of a DIFFERENT checkout under the root: git here does not list it, but its
    `.git` file says what it is, and it gets no removal step."""
    _, root, _, _ = fleet
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-b", "master", str(other)], check=True, capture_output=True)
    _identity(other)
    (other / "g.txt").write_text("g\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "g")
    theirs = root / PROJECT / "e0999-another-repos-tree"
    _git(other, "worktree", "add", str(theirs), "-b", "theirs", "master")
    text = _not_registered_text(fleet, worktree=theirs)
    assert "A `.git` entry is there" in text and "another repository" in text
    assert not any(s in text for s in _EMPTY_STEP)
    assert (theirs / "g.txt").is_file()


def test_a_link_leaf_onto_an_unregistered_directory_gets_no_command(fleet, tmp_path):
    """`is_dir()` follows the link, so the hint fires; deleting through a junction is what can
    empty its target, so the hint names the link and prints nothing to run."""
    _, root, _, _ = fleet
    target = tmp_path / "elsewhere"
    target.mkdir()
    leaf = root / PROJECT / "e0999-a-link"
    _link(leaf, target)
    text = _not_registered_text(fleet, worktree=leaf)
    assert "symlink or junction" in text
    assert "It is empty" not in text and not any(s in text for s in _EMPTY_STEP)
    assert target.is_dir() and leaf.is_dir()


def test_a_residue_path_the_shell_would_expand_gets_no_command(fleet):
    """The hint's commands are double-quoted like the removal pair, so the same paths go without."""
    _, root, _, _ = fleet
    stray = root / PROJECT / "e0787-a$HOME-left"
    stray.mkdir()
    text = _not_registered_text(fleet, worktree=stray)
    assert "It is empty" in text and f"quoting {str(stray.absolute())!r} for the shell" in text
    assert f'rmdir "{stray.absolute()}"' not in text and 'rmdir "<path>"' in text
    assert '[IO.Directory]::Delete("<path>")' in text


def test_an_unsafe_checkout_path_does_not_withhold_the_residue_step(fleet, tmp_path):
    """The hint's commands name only the leftover directory, so only its path is judged."""
    checkout, root, _, reg = fleet
    odd = tmp_path / "a$odd-checkout"
    subprocess.run(["git", "clone", str(checkout), str(odd)], check=True, capture_output=True)
    stray = root / PROJECT / "e0999-never-added"
    stray.mkdir()
    v = wr.check(odd, root, PROJECT, stray, reg)
    assert _keys(v) == ["worktree_not_registered"]
    text = str(v.refusals[0])
    assert f'`rmdir "{stray.absolute()}"` in bash' in text and "<path>" not in text


def test_a_tree_naming_no_epic_refuses(fleet):
    checkout, root, _, _ = fleet
    wt = root / PROJECT / "scratch"
    _git(checkout, "worktree", "add", str(wt), "-b", "scratch", "master")
    assert _keys(_check(fleet, worktree=wt)) == ["worktree_epic_unresolved"]


def test_an_unreadable_registry_refuses(fleet):
    fleet[3].unlink()
    assert _keys(_check(fleet)) == ["worktree_registry_unreadable"]


@pytest.mark.parametrize("header", ["| agent |", "| id | work item |"])
def test_a_template_missing_an_owner_header_names_the_restore_remedy(fleet, monkeypatch, header):
    """`file_damaged`'s rule text no longer carries the restore advice (row damage names its row
    instead), so a missing-structure detail must carry it itself. The damaged file is the shipped
    template, not the live board, so the detail names the template and never tells the lane to
    re-add a heading by hand, which on an intact live registry would be a wrong edit."""
    real = _registry._template_text
    monkeypatch.setattr(
        _registry, "_template_text", lambda project: real(project).replace(header, "| gone |", 1)
    )
    v = _check(fleet)
    assert _keys(v) == ["worktree_registry_unreadable"], [str(r) for r in v.refusals]
    detail = v.refusals[0].detail
    assert "file_damaged" in detail, detail
    assert "templates/registry.md" in detail and "git history" in detail, detail
    assert "live registry is not the damaged file" in detail, detail
    assert _registry._MISSING_STRUCTURE_ADVICE not in detail, detail


def test_refusals_print_in_the_suites_shape():
    r = wr.Refusal("worktree_lane_live", "detail here")
    assert (
        str(r) == f"refused [worktree_lane_live]: {wr.RULES['worktree_lane_live']}\n  detail here"
    )


# --------------------------------------------------------------------------------------------- #
# The CLI the steward runs.
# --------------------------------------------------------------------------------------------- #
class _Repo:
    def __init__(self, checkout):
        self.local_checkout = str(checkout)


class _Proj:
    def __init__(self, checkout):
        self.canonical_repo = _Repo(checkout)


class _Env:
    def __init__(self, root, data_root):
        self.worktree_root = str(root)
        self.data_root = str(data_root)


def _patch_config(monkeypatch, fleet):
    from agentflow import config

    checkout, root, _, _ = fleet
    monkeypatch.setattr(config, "load_environment", lambda: _Env(root, root.parent))
    monkeypatch.setattr(config, "resolve_project_name", lambda explicit=None: explicit)
    monkeypatch.setattr(config, "load_project", lambda name: _Proj(checkout))


def _cli(reg: Path, wt: Path, *extra: str) -> int:
    return wr.main(["--project", PROJECT, "--registry", str(reg), str(wt), *extra])


def test_cli_refuses_with_exit_1_and_reports_removable_with_exit_0(fleet, monkeypatch, capsys):
    _, _, wt, reg = fleet
    _patch_config(monkeypatch, fleet)
    _set_agent(reg, "jr-dev-2", "building", EPIC_ID)
    assert _cli(reg, wt) == 1
    assert "refused [worktree_lane_live]" in capsys.readouterr().out and wt.is_dir()

    reg.write_text(_registry._template_text(PROJECT), encoding="utf-8")
    assert _cli(reg, wt) == 0
    assert wt.is_dir(), "report-only must not remove"


def test_cli_rejects_apply(fleet, monkeypatch, capsys):
    """Q-10093: automated delete is not shipped, so the flag is not there to type."""
    _, _, wt, reg = fleet
    _patch_config(monkeypatch, fleet)
    with pytest.raises(SystemExit) as exc:
        _cli(reg, wt, "--apply")
    assert exc.value.code == 2
    assert "--apply" in capsys.readouterr().err
    assert wt.is_dir()


def test_cli_leaves_a_removable_tree_fully_intact(fleet, monkeypatch):
    checkout, _, wt, reg = fleet
    _patch_config(monkeypatch, fleet)
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "m.cpython-311.pyc").write_bytes(b"\0")
    before = sorted(p.relative_to(wt).as_posix() for p in wt.rglob("*"))
    head = _git(wt, "rev-parse", "HEAD")
    assert _cli(reg, wt) == 0
    assert sorted(p.relative_to(wt).as_posix() for p in wt.rglob("*")) == before
    assert _git(wt, "rev-parse", "HEAD") == head
    registered = [
        _norm(ln[len("worktree ") :])
        for ln in _git(checkout, "worktree", "list", "--porcelain").splitlines()
        if ln.startswith("worktree ")
    ]
    assert _norm(wt) in registered


def test_cli_prints_the_manual_command_only_for_a_removable_tree(fleet, monkeypatch, capsys):
    _, _, wt, reg = fleet
    _patch_config(monkeypatch, fleet)
    assert _cli(reg, wt) == 0
    out = capsys.readouterr().out
    assert "worktree remove" in out and "worktree prune" in out
    assert _norm(wt) in _norm_text(out)

    _set_agent(reg, "jr-dev-2", "building", EPIC_ID)
    assert _cli(reg, wt) == 1
    out = capsys.readouterr().out
    assert "worktree remove" not in out and "worktree prune" not in out


def test_cli_tells_the_human_to_free_the_tree_and_then_check_it_is_gone(fleet, monkeypatch, capsys):
    """Git deregisters a tree even when a process holding it makes the delete fail, so the pair
    can half-succeed silently: the report says so around the command, and only around it."""
    _, _, wt, reg = fleet
    _patch_config(monkeypatch, fleet)
    assert _cli(reg, wt) == 0
    out = capsys.readouterr().out
    flat = _flat(out)
    assert "close any shell or process whose working directory is inside the tree" in flat
    assert "check the directory itself is gone" in flat
    assert "git worktree list" in flat and "deregisters" in flat
    # Before the command, and the check after it.
    assert out.index("close any shell") < out.index("worktree remove") < out.index("itself is gone")

    _set_agent(reg, "jr-dev-2", "building", EPIC_ID)
    assert _cli(reg, wt) == 1
    out = capsys.readouterr().out
    assert "close any shell or process whose working directory" not in _flat(out)
    assert "itself is gone" not in out


def test_cli_refuses_residue_with_exit_1_and_the_hint(fleet, monkeypatch, capsys):
    _, _, wt, reg = fleet
    _patch_config(monkeypatch, fleet)
    _deregister(fleet)
    wt.mkdir()
    assert _cli(reg, wt) == 1
    out = capsys.readouterr().out
    # Not "residue" alone: pytest's tmp_path carries this test's name, which holds that word.
    assert "refused [worktree_not_registered]" in out and "It is empty" in out
    assert "A human clears it" in _flat(out)
    # The hint names the earlier `git worktree remove`; the removal pair itself is not printed.
    assert "worktree prune" not in out and "itself is gone" not in out
    assert wt.is_dir()


def _norm_text(text: str) -> str:
    return text.replace("\\", "/").lower()


# --------------------------------------------------------------------------------------------- #
# The skill text routes removal through the guard.
# --------------------------------------------------------------------------------------------- #
STEWARD_SKILL = Path(__file__).resolve().parents[2] / "steward" / "SKILL.md"


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_steward_charter_reports_worktrees_and_never_removes_them():
    text = STEWARD_SKILL.read_text(encoding="utf-8")
    assert "python -m agentflow.worktree_remove --project <name>" in text
    # The rule and its reason, not the registry row that decided it (Q-10093): a stranger's
    # fleet reads the skill, and the row is not theirs to open.
    assert "so the guard only reports" in _flat(text)
    assert "There is no `--apply`" in text
    # Other tools' `--apply` (refs, backups) are theirs; only the guard's own command line counts.
    applied = re.compile(r"worktree_remove[^\n`]*--apply")
    assert not applied.search(text), "Q-10093: the steward runs the guard report-only"
    # The raw removal the charter used to authorise on git state alone (gh#498) must not come
    # back as an instruction. Matched on the command's shape so rewrapping cannot hide it.
    raw = re.compile(r"worktree\s+remove\s+<worktree_root>")
    assert not raw.search(text), (
        "steward/SKILL.md tells the steward to run `git worktree remove` directly again; the "
        "steward only reports, and a human runs the removal (Q-10093)"
    )


def test_the_steward_names_every_refusal_key_the_guard_can_raise():
    """Derived from RULES, so a new refusal cannot land without the skill saying what it means."""
    text = _flat(STEWARD_SKILL.read_text(encoding="utf-8"))
    missing = [k for k in wr.RULES if f"`{k}`" not in text]
    assert not missing, f"steward/SKILL.md does not name these worktree_remove refusals: {missing}"


def test_the_steward_runs_no_refusal_named_command_and_verifies_the_directory_is_gone():
    """A refusal's remedy is a human's to run too, and an empty `worktree list` is not proof:
    `remove` deregisters a tree whose directory survived a shell still inside it."""
    text = _flat(STEWARD_SKILL.read_text(encoding="utf-8"))
    assert "run none of it" in text
    assert "DIRECTORY is gone" in text
    assert "deregisters" in text


# --------------------------------------------------------------------------------------------- #
# Squash-merged branches. The fleet merges with `gh pr merge --squash --delete-branch`, so a
# finished branch's own commits are on no remote ref; only its net change landed.
# --------------------------------------------------------------------------------------------- #
def _commit_in_tree(wt: Path, name: str, body: str) -> None:
    (wt / name).write_text(body, encoding="utf-8")
    _git(wt, "add", name)
    _git(wt, "commit", "-m", f"add {name}")


def _squash_merge_and_push(checkout: Path) -> None:
    """What `gh pr merge --squash --delete-branch` leaves: one new commit on origin/master."""
    _git(checkout, "merge", "--squash", EPIC_DIR)
    _git(checkout, "commit", "-m", "Squashed epic (#1)")
    # Upstream moves on after the merge, as it does on a busy fleet.
    (checkout / "later.txt").write_text("someone else's change\n", encoding="utf-8")
    _git(checkout, "add", "later.txt")
    _git(checkout, "commit", "-m", "later work")
    _git(checkout, "push", "origin", "master")


def test_a_squash_merged_branch_is_removable(fleet):
    checkout, _, wt, _ = fleet
    _commit_in_tree(wt, "g.txt", "one\n")
    _commit_in_tree(wt, "h.txt", "two\n")
    _squash_merge_and_push(checkout)
    assert _git(wt, "log", "--oneline", "HEAD", "--not", "--remotes").strip(), (
        "precondition: the branch's own commits are on no remote ref"
    )
    v, cmd = _report(fleet)
    assert v.removable, [str(r) for r in v.refusals]
    assert wt.is_dir() and _names_the_tree(cmd, wt)


def test_a_squash_merged_branch_with_a_later_local_commit_refuses(fleet):
    checkout, _, wt, _ = fleet
    _commit_in_tree(wt, "g.txt", "one\n")
    _squash_merge_and_push(checkout)
    _commit_in_tree(wt, "after.txt", "written after the merge, exists nowhere else\n")
    v, _ = _report(fleet)
    assert _keys(v) == ["worktree_unpushed_at_removal"]
    assert (wt / "after.txt").is_file()


def test_an_unlanded_commit_refuses_through_the_configured_base_branch(fleet):
    """The configured base wins over origin/HEAD, and an unlanded commit still refuses there."""
    checkout, root, wt, reg = fleet
    _commit_in_tree(wt, "g.txt", "one\n")
    v = wr.check(checkout, root, PROJECT, wt, reg, base_branch="master")
    assert _keys(v) == ["worktree_unpushed_at_removal"]


def test_an_unresolvable_base_branch_keeps_the_refusal(fleet):
    checkout, root, wt, reg = fleet
    _commit_in_tree(wt, "g.txt", "one\n")
    _squash_merge_and_push(checkout)
    v = wr.check(checkout, root, PROJECT, wt, reg, base_branch="no-such-base")
    assert _keys(v) == ["worktree_unpushed_at_removal"]
    assert "no-such-base" in str(v.refusals[0])


# --------------------------------------------------------------------------------------------- #
# Ignored content. `git worktree remove` without --force still deletes ignored files.
# --------------------------------------------------------------------------------------------- #
def test_a_nested_registered_worktree_refuses_and_survives(fleet):
    """A harness worktree under `.claude/worktrees/` is ignored, so git would delete it silently."""
    checkout, _, wt, _ = fleet
    nested = wt / ".claude" / "worktrees" / "agent-1"
    _git(wt, "worktree", "add", str(nested), "-b", "agent-1", "master")
    v, cmd = _report(fleet)
    assert "worktree_nested_worktree" in _keys(v), [str(r) for r in v.refusals]
    assert nested.is_dir() and cmd == []


def test_an_ignored_backup_file_refuses_and_survives(fleet):
    _, _, wt, _ = fleet
    (wt / "registry.md.bak-2026").write_text("the only copy\n", encoding="utf-8")
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_ignored_content"]
    assert (wt / "registry.md.bak-2026").is_file() and cmd == []


def test_regenerable_caches_do_not_hold_the_tree(fleet):
    _, _, wt, _ = fleet
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "m.cpython-311.pyc").write_bytes(b"\0")
    (wt / ".pytest_cache").mkdir()
    (wt / ".pytest_cache" / "README.md").write_text("cache\n", encoding="utf-8")
    assert _check(fleet).removable, [str(r) for r in _check(fleet).refusals]


def test_status_show_untracked_no_cannot_hide_an_untracked_file(fleet):
    _, _, wt, _ = fleet
    _git(wt, "config", "status.showUntrackedFiles", "no")
    (wt / "scratch.txt").write_text("x\n", encoding="utf-8")
    assert _git(wt, "status", "--porcelain").strip() == "", "precondition: config hides it"
    assert _keys(_check(fleet)) == ["worktree_dirty_at_removal"]


# --------------------------------------------------------------------------------------------- #
# Path aliases: a link onto a registered tree must not borrow its own name's liveness answer.
# --------------------------------------------------------------------------------------------- #
def _link(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_a_link_onto_a_live_tree_is_refused_and_both_survive(fleet):
    """`e0999-dead-looking` is a link onto the live epic's tree; nothing names E-0999 as live."""
    _, root, wt, reg = fleet
    _set_agent(reg, "jr-dev-2", "building", EPIC_ID)
    alias = root / PROJECT / "e0999-dead-looking"
    _link(alias, wt)
    v, cmd = _report(fleet, worktree=alias)
    assert _keys(v) == ["worktree_path_alias"]
    assert wt.is_dir() and cmd == []


def test_the_registered_path_is_what_is_measured_and_named_in_the_command(fleet):
    _, _, wt, _ = fleet
    v, cmd = _report(fleet)
    assert v.removable
    assert _norm(v.registered) == _norm(wt)
    assert v.registered in cmd[0]


def _norm(p) -> str:
    return str(Path(p).resolve()).replace("\\", "/").lower()


# --------------------------------------------------------------------------------------------- #
# worktree_unmeasurable.
# --------------------------------------------------------------------------------------------- #
def test_a_broken_git_pointer_is_unmeasurable(fleet):
    _, _, wt, _ = fleet
    pointer = wt / ".git"
    # Replaced, not overwritten: Windows marks the pointer hidden, and a hidden file refuses "w".
    pointer.unlink()
    pointer.write_text("gitdir: /nonexistent/pointer\n", encoding="utf-8")
    v = _check(fleet)
    assert set(_keys(v)) == {"worktree_unmeasurable"}, [str(r) for r in v.refusals]
    assert wt.is_dir()


# --------------------------------------------------------------------------------------------- #
# A link inside the tree. `git worktree remove` can follow a junction or symlink and empty its
# target, which lies OUTSIDE the tree - so a link's name (`node_modules`, what `npm link` makes)
# must not pass the regenerable allowlist.
# --------------------------------------------------------------------------------------------- #
def _exclude(checkout: Path, pattern: str) -> None:
    """Ignore *pattern* without touching a tracked file, so the tree stays clean."""
    info = checkout / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    with (info / "exclude").open("a", encoding="utf-8") as fh:
        fh.write(pattern + "\n")


def _sentinel(tmp_path: Path) -> Path:
    target = tmp_path / "outside" / "linked-package"
    target.mkdir(parents=True)
    (target / "only-copy.txt").write_text("lives outside the tree\n", encoding="utf-8")
    return target


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows reparse point")
def test_an_ignored_node_modules_junction_refuses_and_its_target_survives(fleet, tmp_path):
    import _winapi

    checkout, _, wt, _ = fleet
    _exclude(checkout, "node_modules")
    target = _sentinel(tmp_path)
    _winapi.CreateJunction(str(target), str(wt / "node_modules"))
    v, cmd = _report(fleet)
    assert "worktree_link_inside" in _keys(v), [str(r) for r in v.refusals]
    assert "node_modules" in str(v.refusals[_keys(v).index("worktree_link_inside")])
    assert (target / "only-copy.txt").is_file() and cmd == []


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows reparse point")
def test_a_nested_dot_git_junction_refuses_and_its_target_survives(fleet, tmp_path):
    """The name `.git` earns a link nothing: only the tree's own top-level `.git` goes unwalked."""
    import _winapi

    checkout, _, wt, _ = fleet
    _exclude(checkout, "node_modules")
    target = _sentinel(tmp_path)
    (wt / "node_modules" / "pkg").mkdir(parents=True)
    _winapi.CreateJunction(str(target), str(wt / "node_modules" / "pkg" / ".git"))
    v, cmd = _report(fleet)
    assert "worktree_link_inside" in _keys(v), [str(r) for r in v.refusals]
    assert "node_modules/pkg/.git" in str(v.refusals[_keys(v).index("worktree_link_inside")])
    assert (target / "only-copy.txt").is_file() and cmd == []


def test_an_editable_clone_under_venv_refuses_as_a_nested_repo_and_survives(fleet):
    """`pip install -e git+...` clones into `.venv/src/<pkg>`: an allowlisted cache path holding a
    repository whose uncommitted edit and unpushed commit this tree's status cannot see."""
    checkout, _, wt, _ = fleet
    _exclude(checkout, ".venv")
    pkg = wt / ".venv" / "src" / "pkg"
    pkg.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(pkg)], check=True, capture_output=True)
    _identity(pkg)
    (pkg / "m.py").write_text("v = 1\n", encoding="utf-8")
    _git(pkg, "add", "-A")
    _git(pkg, "commit", "-qm", "local only")
    (pkg / "m.py").write_text("v = 2  # an uncommitted fix\n", encoding="utf-8")
    assert _git(wt, "status", "--porcelain").strip() == "", "precondition: this tree sees nothing"
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_nested_repo"], [str(r) for r in v.refusals]
    assert ".venv/src/pkg/.git" in str(v.refusals[0])
    assert (pkg / "m.py").read_text(encoding="utf-8").startswith("v = 2") and cmd == []


def test_an_ignored_symlinked_cache_dir_refuses_and_its_target_survives(fleet, tmp_path):
    checkout, _, wt, _ = fleet
    _exclude(checkout, ".venv")
    target = _sentinel(tmp_path)
    try:
        os.symlink(target, wt / ".venv", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable here: {exc}")
    v, cmd = _report(fleet)
    assert "worktree_link_inside" in _keys(v), [str(r) for r in v.refusals]
    assert (target / "only-copy.txt").is_file() and cmd == []


# --------------------------------------------------------------------------------------------- #
# Squash-merged after upstream drift. Neither a patch-id (it hashes context lines) nor the byte
# comparison (whole-file blob ids) matches a squash made after another PR changed the same file.
# --------------------------------------------------------------------------------------------- #
_DOC = "".join(f"line {i}\n" for i in range(1, 11))


def _seed_doc(checkout: Path, wt: Path) -> None:
    """A shared doc on master, fetched into the tree's fork point."""
    (checkout / "doc.md").write_text(_DOC, encoding="utf-8")
    _git(checkout, "add", "doc.md")
    _git(checkout, "commit", "-m", "doc")
    _git(checkout, "push", "origin", "master")
    _git(wt, "fetch", "origin")
    _git(wt, "reset", "--hard", "origin/master")


def _edit_line(path: Path, n: int, text: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[n - 1] = text + "\n"
    path.write_text("".join(lines), encoding="utf-8")


def _drift_then_squash(checkout: Path, wt: Path) -> None:
    """The branch edits line 5; another PR edits line 3 (inside its diff context) first; squash.

    Line 3, not line 4: an edit on the ADJACENT line conflicts, so a PR carrying it cannot be
    squash-merged until it is rebased, and a rebased branch's patch has the drift in its context.
    """
    _seed_doc(checkout, wt)
    _edit_line(wt / "doc.md", 5, "line 5 - the branch's change")
    _git(wt, "commit", "-am", "branch edit")
    _edit_line(checkout / "doc.md", 3, "line 3 - another PR's change")
    _git(checkout, "commit", "-am", "adjacent-context drift")
    _squash_merge_and_push(checkout)
    _git(wt, "fetch", "origin")
    fork = _git(wt, "merge-base", "origin/master", "HEAD").strip()
    synth = _git(wt, "commit-tree", "HEAD^{tree}", "-p", fork, "-m", "probe").strip()
    assert _git(wt, "cherry", "origin/master", synth).startswith("+"), (
        "precondition: the drift moved the squash's context, so cherry alone cannot see it landed"
    )


def test_a_squash_after_adjacent_context_drift_keeps_the_tree(fleet):
    """The byte comparison is the only landing proof, so a drifted squash fails safe."""
    checkout, _, wt, _ = fleet
    _drift_then_squash(checkout, wt)
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_unpushed_at_removal"], [str(r) for r in v.refusals]
    assert wt.is_dir() and cmd == []


def test_a_squash_after_drift_then_a_same_line_re_edit_upstream_keeps_the_tree(fleet):
    checkout, _, wt, _ = fleet
    _drift_then_squash(checkout, wt)
    _edit_line(checkout / "doc.md", 5, "line 5 - rewritten after the squash")
    _git(checkout, "commit", "-am", "re-edit the branch's line")
    _git(checkout, "push", "origin", "master")
    _git(wt, "fetch", "origin")
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_unpushed_at_removal"], [str(r) for r in v.refusals]
    assert wt.is_dir() and cmd == []


def test_a_squash_after_drift_with_a_later_local_commit_still_refuses(fleet):
    checkout, _, wt, _ = fleet
    _drift_then_squash(checkout, wt)
    _commit_in_tree(wt, "after.txt", "written after the merge, exists nowhere else\n")
    v, _ = _report(fleet)
    assert _keys(v) == ["worktree_unpushed_at_removal"]
    assert (wt / "after.txt").is_file()


def _branch_then_drift_then_squash(checkout: Path, wt: Path, branch_edit) -> None:
    """The branch makes *branch_edit* and commits; another PR edits line 3; squash; fetch."""
    _seed_doc(checkout, wt)
    branch_edit(wt)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "branch edit")
    _edit_line(checkout / "doc.md", 3, "line 3 - another PR's change")
    _git(checkout, "commit", "-am", "adjacent-context drift")
    _squash_merge_and_push(checkout)
    _git(wt, "fetch", "origin")


def test_a_local_partial_revert_after_the_squash_refuses(fleet):
    """HEAD undid one of the branch's two edits; the squash still holds it, so HEAD differs."""
    checkout, _, wt, _ = fleet

    def two_edits(tree: Path) -> None:
        _edit_line(tree / "doc.md", 5, "line 5 - the branch's change")
        _edit_line(tree / "doc.md", 9, "line 9 - the branch's second change")

    _branch_then_drift_then_squash(checkout, wt, two_edits)
    _edit_line(wt / "doc.md", 9, "line 9")
    _git(wt, "commit", "-am", "revert line 9 locally after the merge")
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_unpushed_at_removal"], [str(r) for r in v.refusals]
    assert wt.is_dir() and cmd == []


def test_a_local_delete_of_the_branchs_own_file_after_the_squash_refuses(fleet):
    checkout, _, wt, _ = fleet

    def edit_and_add(tree: Path) -> None:
        _edit_line(tree / "doc.md", 5, "line 5 - the branch's change")
        (tree / "feature.py").write_text("x = 1\n", encoding="utf-8")

    _branch_then_drift_then_squash(checkout, wt, edit_and_add)
    _git(wt, "rm", "-q", "feature.py")
    _git(wt, "commit", "-m", "drop feature.py locally after the merge")
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_unpushed_at_removal"], [str(r) for r in v.refusals]
    assert wt.is_dir() and cmd == []


def test_a_keep_ours_merge_driver_cannot_pass_a_local_re_edit(fleet):
    """Round-4 S6: a `merge=keep` driver made a tree merge absorb local work. No merge runs now."""
    checkout, _, wt, _ = fleet
    (checkout / ".gitattributes").write_text("doc.md merge=keep" + chr(10), encoding="utf-8")
    _git(checkout, "add", ".gitattributes")
    _git(checkout, "commit", "-m", "attrs")
    _git(checkout, "config", "merge.keep.driver", "true")
    _seed_doc(checkout, wt)
    _edit_line(wt / "doc.md", 5, "line 5 - the branch's change")
    _git(wt, "commit", "-am", "branch edit")
    _squash_merge_and_push(checkout)
    _git(wt, "fetch", "origin")
    _edit_line(wt / "doc.md", 5, "line 5 - reworded locally after the merge, never pushed")
    _git(wt, "commit", "-am", "reword")
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_unpushed_at_removal"], [str(r) for r in v.refusals]
    assert wt.is_dir() and cmd == []


def test_a_cherry_picked_upstream_fix_plus_a_local_revert_refuses(fleet):
    """Round-4 bypass: HEAD's tree matched no upstream commit's, but the tree merges passed it."""
    checkout, _, wt, _ = fleet
    _seed_doc(checkout, wt)
    _edit_line(wt / "doc.md", 5, "line 5 - the branch's change")
    _edit_line(wt / "doc.md", 9, "line 9 - the branch's second change")
    _git(wt, "commit", "-am", "branch edits")
    _squash_merge_and_push(checkout)
    _edit_line(checkout / "doc.md", 1, "line 1 - hotfix")
    _git(checkout, "commit", "-am", "hotfix")
    _git(checkout, "push", "origin", "master")
    _git(wt, "fetch", "origin")
    _git(wt, "cherry-pick", "origin/master")
    _edit_line(wt / "doc.md", 9, "line 9")
    _git(wt, "commit", "-am", "revert line 9 locally")
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_unpushed_at_removal"], [str(r) for r in v.refusals]
    assert wt.is_dir() and cmd == []


# --------------------------------------------------------------------------------------------- #
# Index flags that make status lie: an edit under --assume-unchanged / --skip-worktree is invisible
# to `status --porcelain`, and removal deletes it.
# --------------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_an_edit_hidden_by_an_index_flag_refuses(fleet, flag):
    _, _, wt, _ = fleet
    _git(wt, "update-index", flag, "f.txt")
    (wt / "f.txt").write_text("an edit status cannot see\n", encoding="utf-8")
    assert _git(wt, "status", "--porcelain").strip() == "", "precondition: the flag hides it"
    v = _check(fleet)
    assert _keys(v) == ["worktree_dirty_at_removal"], [str(r) for r in v.refusals]
    assert "index flags hide edits" in str(v.refusals[0])


# --------------------------------------------------------------------------------------------- #
# Registry rows the parser keeps, or drops, in the wrong shape.
# --------------------------------------------------------------------------------------------- #
def test_a_shifted_eight_cell_agents_row_refuses_as_unreadable(fleet):
    """An extra cell moves the epic out of the work column, so the live claim reads as unowned."""
    reg = fleet[3]
    text = reg.read_text(encoding="utf-8")
    old = "| jr-dev-2 | idle | - | - | - | - | - |"
    assert text.count(old) == 1
    new = f"| jr-dev-2 | building | stray | {EPIC_ID} | build | 2026-09-22T19:57:00Z | - | - |"
    reg.write_text(text.replace(old, new), encoding="utf-8")
    v = _check(fleet)
    assert _keys(v) == ["worktree_registry_unreadable"], [str(r) for r in v.refusals]
    assert "jr-dev-2" in str(v.refusals[0])


def test_a_queue_row_with_a_malformed_id_refuses_as_unreadable(fleet):
    """`e-0787` fails the E-id pattern, so the parser drops the very row that claims the epic."""
    _add_queue(fleet[3], "e-0787", "jr-dev-2", "jr-dev-2", "building")
    v = _check(fleet)
    assert _keys(v) == ["worktree_registry_unreadable"], [str(r) for r in v.refusals]
    assert "e-0787" in str(v.refusals[0])


def _add_raw_queue_rows(registry_path: Path, rows: str) -> None:
    text = registry_path.read_text(encoding="utf-8")
    header = "| id | work item | lane | claimed_by | state | waiting_on | notes |\n|---|---|---|---|---|---|---|\n"
    assert text.count(header) == 1, "Queue table header not found exactly once"
    registry_path.write_text(text.replace(header, header + rows), encoding="utf-8")


def test_a_short_six_cell_queue_row_refuses_as_unreadable(fleet):
    """The parser needs seven cells, so it drops the parked, claimed row that owns the tree."""
    _add_raw_queue_rows(
        fleet[3], "| E-0787 | a-new-human-surface | jr-dev-1 | jr-dev-1 | parked | - |\n"
    )
    v = _check(fleet)
    assert _keys(v) == ["worktree_registry_unreadable"], [str(r) for r in v.refusals]
    assert "6 cells" in str(v.refusals[0])


def test_a_done_row_repeating_a_live_rows_id_refuses_as_unreadable(fleet):
    """Rows are keyed by id, so the later `done` row replaces the `building` one above it."""
    _add_raw_queue_rows(
        fleet[3],
        "| E-0787 | a-new-human-surface | jr-dev-1 | jr-dev-1 | building | - | - |\n"
        "| E-0787 | a-new-human-surface | jr-dev-1 | jr-dev-1 | done | - | - |\n",
    )
    v = _check(fleet)
    assert _keys(v) == ["worktree_registry_unreadable"], [str(r) for r in v.refusals]
    assert "duplicate id" in str(v.refusals[0])


def test_the_steward_fetches_before_the_guard():
    """The squash check reads the LOCAL origin/<base> ref; a stale one refuses a landed tree."""
    text = _flat(STEWARD_SKILL.read_text(encoding="utf-8"))
    assert "fetch origin <base-branch>" in text
    # Pruning refs is agentflow.refs' job (gh#116); the guard needs only a fresh base ref.
    assert "fetch --prune" not in text


# --------------------------------------------------------------------------------------------- #
# The registry is an ALLOWLIST. One test per round-4 probe (R1-R12), each on a board that is
# removable before the probe's edit: a `done` Queue row for the epic and every lane idle.
# --------------------------------------------------------------------------------------------- #
Q_HDR = "| id | work item | lane | claimed_by | state | waiting_on | notes |\n|---|---|---|---|---|---|---|\n"
A_HDR = "| agent | status | work | phase | heartbeat | eta | waiting_on |\n|---|---|---|---|---|---|---|\n"
DONE_ROW = f"| {EPIC_ID} | a-new-human-surface | jr-dev-1 | jr-dev-1 | done | - | - |\n"
LIVE_ROW = f"| {EPIC_ID} | a-new-human-surface | jr-dev-1 | jr-dev-1 | building | - | - |\n"
A_J1 = "| jr-dev-1 | idle | - | - | - | - | - |\n"
A_J1_LIVE = f"| jr-dev-1 | building | {EPIC_ID} | build | 2026-09-24T10:00:00Z | - | - |\n"


def _board(fleet):
    reg = fleet[3]
    _add_raw_queue_rows(reg, DONE_ROW)
    assert _check(fleet).removable, "precondition: the board before the probe's edit is removable"
    return reg


def _sub(reg: Path, old: str, new: str) -> None:
    text = reg.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"anchor found {text.count(old)} times: {old!r}"
    reg.write_text(text.replace(old, new), encoding="utf-8")


def _append(reg: Path, tail: str) -> None:
    reg.write_text(reg.read_text(encoding="utf-8") + tail, encoding="utf-8")


def _refused(fleet, key: str = "worktree_registry_unreadable") -> None:
    wt = fleet[2]
    v, cmd = _report(fleet)
    assert _keys(v) == [key], [str(r) for r in v.refusals]
    assert wt.is_dir() and cmd == []


def test_r1_a_second_empty_queue_section_after_the_live_one_refuses(fleet):
    reg = _board(fleet)
    _sub(reg, DONE_ROW, LIVE_ROW)
    _append(reg, "\n## Queue\n\n" + Q_HDR)
    _refused(fleet)


def test_r2_a_queue_heading_pasted_into_directives_refuses(fleet):
    reg = _board(fleet)
    _sub(reg, DONE_ROW, LIVE_ROW)
    _append(reg, "\n- pasted from the old board:\n## Queue\n" + Q_HDR + DONE_ROW)
    _refused(fleet)


def test_r3_queue_columns_reordered_refuse(fleet):
    reg = _board(fleet)
    _sub(
        reg,
        Q_HDR + DONE_ROW,
        "| id | work item | lane | claimed_by | notes | waiting_on | state |\n|---|---|---|---|---|---|---|\n"
        f"| {EPIC_ID} | a-new-human-surface | jr-dev-1 | jr-dev-1 | done | - | building |\n",
    )
    _refused(fleet)


def test_r4_agents_columns_reordered_refuse(fleet):
    reg = _board(fleet)
    _sub(
        reg,
        A_HDR,
        "| agent | work | status | phase | heartbeat | eta | waiting_on |\n|---|---|---|---|---|---|---|\n",
    )
    _sub(reg, A_J1, f"| jr-dev-1 | {EPIC_ID} | building | build | now | - | - |\n")
    _refused(fleet)


def test_r5_a_queue_row_without_its_leading_pipe_refuses(fleet):
    reg = _board(fleet)
    _sub(reg, DONE_ROW, LIVE_ROW[2:])
    _refused(fleet)


def test_r6_a_second_idle_agents_section_after_the_live_one_refuses(fleet):
    reg = _board(fleet)
    text = reg.read_text(encoding="utf-8")
    idle_block = text.split("## Agents\n", 1)[1].split("## Queue", 1)[0]
    _sub(reg, A_J1, A_J1_LIVE)
    _append(reg, "\n## Agents\n" + idle_block)
    _refused(fleet)


def test_r7_a_second_row_for_a_lane_without_a_leading_pipe_refuses(fleet):
    reg = _board(fleet)
    _sub(reg, A_J1, A_J1 + A_J1_LIVE[2:])
    _refused(fleet)


def test_r8_a_live_queue_row_with_an_extra_cell_refuses(fleet):
    reg = _board(fleet)
    _sub(reg, DONE_ROW, LIVE_ROW.replace("| - | - |", "| - | a | b |"))
    _refused(fleet)


def test_r9_a_live_row_then_a_withdrawn_duplicate_refuses(fleet):
    reg = _board(fleet)
    _sub(reg, DONE_ROW, LIVE_ROW + DONE_ROW.replace("done", "withdrawn"))
    _refused(fleet)


def test_r10_a_tab_indented_queue_row_without_a_pipe_refuses(fleet):
    reg = _board(fleet)
    _sub(reg, DONE_ROW, "\t" + LIVE_ROW.lstrip("| "))
    _refused(fleet)


def test_r11_a_live_row_naming_the_epic_only_by_slug_refuses(fleet):
    """No line carries E-0787, so the scan passes; `live_owners` still matches the slug."""
    reg = _board(fleet)
    _sub(
        reg,
        DONE_ROW,
        DONE_ROW + "| E-0999 | a-new-human-surface | jr-dev-1 | jr-dev-1 | building | - | - |\n",
    )
    _refused(fleet, "worktree_queue_live")


def test_r12_a_live_agents_row_on_the_epic_refuses(fleet):
    reg = _board(fleet)
    _sub(reg, A_J1, A_J1_LIVE)
    _refused(fleet, "worktree_lane_live")


@pytest.mark.parametrize("variant", ["pipeless", "tab-indented"])
def test_a_pipeless_live_row_below_the_tables_end_is_caught_by_the_line_scan(fleet, variant):
    """Past the blank line that ends the table, only the whole-file scan still sees the row."""
    reg = _board(fleet)
    row = LIVE_ROW[2:] if variant == "pipeless" else "\t" + LIVE_ROW.lstrip("| ")
    _sub(reg, DONE_ROW, DONE_ROW + "\n" + row)
    _refused(fleet)
    assert "is not a well-formed Queue or Agents row" in str(_check(fleet).refusals[0])


def test_a_directive_naming_the_epic_refuses_as_unreadable(fleet):
    """Every line naming the epic must be recognised, wherever in the file it sits."""
    reg = _board(fleet)
    _append(reg, "\n- keep e0787's tree until the rework lands\n")
    _refused(fleet)
    assert "keep e0787" in str(_check(fleet).refusals[0])


def test_a_done_row_and_an_idle_row_on_the_epic_are_removable(fleet):
    reg = _board(fleet)
    _sub(reg, A_J1, f"| jr-dev-1 | idle | {EPIC_ID} | - | 2026-09-24T10:00:00Z | - | - |\n")
    v, cmd = _report(fleet)
    assert v.removable, [str(r) for r in v.refusals]
    assert fleet[2].is_dir() and _names_the_tree(cmd, fleet[2])


def test_notes_that_mention_the_epic_do_not_hold_the_tree(fleet):
    """Notes carry decisions, not claims: a live Note and a struck one on the epic both pass."""
    reg = _board(fleet)
    _sub(
        reg,
        "- **Notes** \u2014 (none)\n",
        "- **Notes** \u2014 (none)\n"
        f"- 2026-09-24T10:00Z jr-dev-1 -> steward: {EPIC_ID} merged, tree is yours\n"
        "- ~~2026-09-23T09:00Z lead-dev -> jr-dev-1: rework E-0787 first~~\n",
    )
    v, cmd = _report(fleet)
    assert v.removable, [str(r) for r in v.refusals]
    assert fleet[2].is_dir() and _names_the_tree(cmd, fleet[2])


# --------------------------------------------------------------------------------------------- #
# Round 6. A patch-id strips whitespace, so a local commit that only re-indents a line the branch
# added still matches by patch-id. The byte comparison is the proof and refuses it; the cherry
# preconditions below show each scenario is patch-id-blind.
# --------------------------------------------------------------------------------------------- #
PY_SRC = "def f(x):\n    if x:\n        a()\n    return 1\n"
YAML_SRC = "deploy:\n  env: staging\nsafety:\n  dry_run: true\n"


def _squash_only_and_push(checkout: Path) -> None:
    _git(checkout, "merge", "--squash", EPIC_DIR)
    _git(checkout, "commit", "-m", "Squashed epic (#1)")
    _git(checkout, "push", "origin", "master")


def _squash_py_and_yaml(checkout: Path, wt: Path, later: bool = True) -> None:
    (wt / "app.py").write_text(PY_SRC, encoding="utf-8")
    (wt / "cfg.yml").write_text(YAML_SRC, encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "add app.py and cfg.yml")
    (_squash_merge_and_push if later else _squash_only_and_push)(checkout)


def _whitespace_commit(wt: Path, name: str, body: str) -> str:
    """Commit *body* to *name* and assert cherry alone would call it landed."""
    (wt / name).write_text(body, encoding="utf-8")
    _git(wt, "commit", "-am", f"whitespace-only change to {name}, never pushed")
    fork = _git(wt, "merge-base", "origin/master", "HEAD").strip()
    synth = _git(wt, "commit-tree", "HEAD^{tree}", "-p", fork, "-m", "probe").strip()
    assert _git(wt, "cherry", "origin/master", synth).startswith("-"), (
        "precondition: patch-id ignores the whitespace, so cherry alone reads the tree as landed"
    )
    return _git(wt, "rev-parse", "HEAD").strip()


def _kept(fleet) -> None:
    wt = fleet[2]
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_unpushed_at_removal"], [str(r) for r in v.refusals]
    assert wt.is_dir() and cmd == []


@pytest.mark.parametrize("later", [False, True], ids=["plain", "later-upstream"])
def test_a_squash_of_python_and_yaml_is_removable(fleet, later):
    """Controls for the refusals below: without these, a guard refusing everything passes them."""
    checkout, _, wt, _ = fleet
    _squash_py_and_yaml(checkout, wt, later=later)
    v, cmd = _report(fleet)
    assert v.removable, [str(r) for r in v.refusals]
    assert wt.is_dir() and _names_the_tree(cmd, wt)


def test_a_python_re_indent_after_the_squash_refuses(fleet):
    checkout, _, wt, _ = fleet
    _squash_py_and_yaml(checkout, wt)
    _whitespace_commit(wt, "app.py", PY_SRC.replace("    return 1", "        return 1"))
    _kept(fleet)


def test_a_yaml_dedent_after_the_squash_refuses(fleet):
    checkout, _, wt, _ = fleet
    _squash_py_and_yaml(checkout, wt)
    _whitespace_commit(wt, "cfg.yml", YAML_SRC.replace("  dry_run", "dry_run"))
    _kept(fleet)


def test_tabs_for_spaces_after_the_squash_refuses(fleet):
    checkout, _, wt, _ = fleet
    _squash_py_and_yaml(checkout, wt)
    _whitespace_commit(wt, "app.py", PY_SRC.replace("        a()", "\t\ta()"))
    _kept(fleet)


def test_a_detached_head_re_indent_after_the_squash_refuses(fleet):
    checkout, _, wt, _ = fleet
    _squash_py_and_yaml(checkout, wt)
    _git(wt, "checkout", "-q", "--detach")
    sha = _whitespace_commit(wt, "app.py", PY_SRC.replace("    return 1", "        return 1"))
    assert not _git(checkout, "branch", "-a", "--contains", sha).strip(), (
        "precondition: the detached commit is on no ref, so the tree is its only copy"
    )
    _kept(fleet)


# --------------------------------------------------------------------------------------------- #
# Round 6-7. An id written with lookalike characters names the epic to a reader and nothing
# to a regex. Folding each class was a list that never ends, so a key cell must be ASCII.
# --------------------------------------------------------------------------------------------- #
def test_an_agents_row_building_on_the_id_with_a_unicode_hyphen_refuses(fleet):
    reg = _board(fleet)
    _sub(reg, A_J1, "| jr-dev-1 | building | E\u20100787-some-epik | build | now | - | - |\n")
    _refused(fleet)


def test_an_agents_row_building_on_the_id_with_a_zero_width_space_refuses(fleet):
    reg = _board(fleet)
    _sub(reg, A_J1, "| jr-dev-1 | building | E-0\u200b787 | build | now | - | - |\n")
    _refused(fleet)


def test_a_queue_row_with_fullwidth_digits_in_the_id_refuses(fleet):
    reg = _board(fleet)
    _sub(
        reg,
        DONE_ROW,
        DONE_ROW + "| E-\uff10\uff17\uff18\uff17 | x | jr-dev-1 | jr-dev-1 | building | - | - |\n",
    )
    _refused(fleet)


@pytest.mark.parametrize(
    "work",
    [
        "\u0415-0787-some-epik",  # Cyrillic capital IE
        "E-\u0966\u096d\u096e\u096d",  # Devanagari digits
        "E\u22120787",  # minus sign, category Sm, not dash punctuation
        "E\u0301-0787",  # combining acute, which NFKC composes into a different letter
    ],
    ids=["cyrillic-E", "devanagari-digits", "minus-sign", "combining-acute"],
)
def test_an_agents_row_building_on_a_lookalike_id_refuses(fleet, work):
    reg = _board(fleet)
    _sub(reg, A_J1, f"| jr-dev-1 | building | {work} | build | now | - | - |\n")
    _refused(fleet)


def test_a_queue_row_with_devanagari_digits_in_the_id_refuses(fleet):
    reg = _board(fleet)
    qid = "E-\u0966\u096d\u096e\u096d"
    _sub(reg, DONE_ROW, DONE_ROW + f"| {qid} | x | jr-dev-1 | jr-dev-1 | building | - | - |\n")
    _refused(fleet)


def test_non_ascii_free_text_cells_on_a_removable_board_are_still_removable(fleet):
    """Control for the key-cell rule: phase, work item and notes are prose, and prose is not ASCII."""
    reg = _board(fleet)
    _sub(
        reg,
        DONE_ROW,
        f"| {EPIC_ID} | a new human surface \u2014 v2 | jr-dev-1 | jr-dev-1 | done | - | "
        "merged \u00b7 tree free |\n",
    )
    _sub(
        reg,
        A_J1,
        "| jr-dev-1 | idle | - | lane empty \u2014 idle-wake 3600s \u00b7 pass 4 | "
        "2026-09-24T10:00:00Z | - | - |\n",
    )
    v, cmd = _report(fleet)
    assert v.removable, [str(r) for r in v.refusals]
    assert fleet[2].is_dir() and _names_the_tree(cmd, fleet[2])


def test_an_id_glued_to_a_word_in_a_live_work_cell_refuses(fleet):
    """`fixE-0787` names the epic; a letter before the E is no boundary."""
    reg = _board(fleet)
    _sub(reg, A_J1, "| jr-dev-1 | building | fixE-0787 | build | now | - | - |\n")
    _refused(fleet, "worktree_lane_live")


def test_a_heading_inside_the_queue_section_refuses(fleet):
    """`## Queue archive` is no section to the parser, so a bullet under it read as a Note."""
    reg = _board(fleet)
    _sub(
        reg,
        DONE_ROW,
        DONE_ROW + "\n## Queue archive\n- E-0787 claimed by jr-dev-1, building\n",
    )
    _refused(fleet)


# --------------------------------------------------------------------------------------------- #
# Round 7. A submodule's `ignore = all` hides a gitlink change from `status` and from a diff, so
# a local commit that only moves the pointer printed the same patch as the landed squash.
# --------------------------------------------------------------------------------------------- #
SHA_A = "1" * 40
SHA_B = "2" * 40


def _squash_a_gitlink(checkout: Path, wt: Path, ignore_in_gitmodules: bool) -> None:
    gm = '[submodule "sub"]\n\tpath = sub\n\turl = https://example.invalid/sub.git\n'
    if ignore_in_gitmodules:
        gm += "\tignore = all\n"
    (wt / ".gitmodules").write_text(gm, encoding="utf-8")
    (wt / "sub").mkdir()
    (wt / "g.txt").write_text("one\n", encoding="utf-8")
    _git(wt, "add", ".gitmodules", "g.txt")
    _git(wt, "update-index", "--add", "--cacheinfo", f"160000,{SHA_A},sub")
    _git(wt, "commit", "-m", "add g.txt and the sub gitlink")
    _squash_only_and_push(checkout)
    assert "160000" in _git(checkout, "ls-tree", "origin/master", "sub"), "gitlink not landed"


def _move_gitlink(wt: Path) -> str:
    _git(wt, "update-index", "--cacheinfo", f"160000,{SHA_B},sub")
    _git(wt, "commit", "-m", "move the sub gitlink, never pushed")
    assert SHA_B in _git(wt, "ls-tree", "HEAD", "sub")
    return _git(wt, "rev-parse", "HEAD").strip()


def test_a_squashed_gitlink_with_ignore_all_is_removable(fleet):
    """Control for the three below: the landed gitlink alone does not hold the tree."""
    checkout, _, wt, _ = fleet
    _squash_a_gitlink(checkout, wt, ignore_in_gitmodules=True)
    v, cmd = _report(fleet)
    assert v.removable, [str(r) for r in v.refusals]
    assert wt.is_dir() and _names_the_tree(cmd, wt)


def test_a_gitlink_moved_under_gitmodules_ignore_all_refuses(fleet):
    checkout, _, wt, _ = fleet
    _squash_a_gitlink(checkout, wt, ignore_in_gitmodules=True)
    _move_gitlink(wt)
    _kept(fleet)


def test_a_gitlink_moved_under_config_ignore_all_refuses(fleet):
    checkout, _, wt, _ = fleet
    _squash_a_gitlink(checkout, wt, ignore_in_gitmodules=False)
    _git(checkout, "config", "submodule.sub.ignore", "all")
    _move_gitlink(wt)
    _kept(fleet)


def test_a_detached_head_gitlink_move_under_ignore_all_refuses(fleet):
    checkout, _, wt, _ = fleet
    _squash_a_gitlink(checkout, wt, ignore_in_gitmodules=True)
    _git(wt, "checkout", "-q", "--detach")
    sha = _move_gitlink(wt)
    assert not _git(checkout, "branch", "-a", "--contains", sha).strip(), (
        "precondition: the detached commit is on no ref, so the tree is its only copy"
    )
    _kept(fleet)


# --------------------------------------------------------------------------------------------- #
# gh#614: the report writes nothing, prints no command it cannot quote, and never hands over a
# command for a tree git has locked.
# --------------------------------------------------------------------------------------------- #
def _object_store(checkout: Path) -> str:
    """Loose and packed object counts of the store the checkout and its worktrees share."""
    return _git(checkout, "count-objects", "-v")


@pytest.mark.parametrize("landed", [True, False], ids=["squash-landed", "unlanded"])
def test_the_landing_probe_writes_no_object(fleet, landed):
    """Both verdicts reach `_landed_upstream`: the tree has commits on no remote either way."""
    checkout, _, wt, _ = fleet
    _commit_in_tree(wt, "g.txt", "one\n")
    if landed:
        _squash_merge_and_push(checkout)
    _git(wt, "fetch", "origin")
    assert _git(wt, "log", "--oneline", "HEAD", "--not", "--remotes").strip(), (
        "precondition: the tree has commits of its own, so the landing probe runs"
    )
    before = _object_store(checkout)
    v = _check(fleet)
    assert v.removable is landed, [str(r) for r in v.refusals]
    assert _object_store(checkout) == before


def _second_tree(fleet, name: str) -> Path:
    """Another per-epic tree under the root, on its own branch, at master like the fixture's."""
    checkout, root, _, _ = fleet
    wt = root / PROJECT / name
    _git(checkout, "worktree", "add", str(wt), "-b", "e0787-second-tree", "master")
    return wt


def test_a_path_with_a_space_still_prints_the_command(fleet):
    wt = _second_tree(fleet, "e0787-a new-human-surface")
    v, cmd = _report(fleet, worktree=wt)
    assert v.removable, [str(r) for r in v.refusals]
    assert _names_the_tree(cmd, wt)
    assert f'"{v.registered}"' in cmd[0]


@pytest.mark.parametrize("char", ["$", "`", "!"], ids=["dollar", "backtick", "bang"])
def test_a_path_the_shell_would_expand_refuses_with_no_command(fleet, char):
    wt = _second_tree(fleet, f"e0787-a{char}HOME-surface")
    v, cmd = _report(fleet, worktree=wt)
    assert _keys(v) == ["worktree_path_unquotable"], [str(r) for r in v.refusals]
    assert cmd == [] and wt.is_dir()


@pytest.mark.parametrize(
    "char", ['"', "$", "`", "\n"], ids=["quote", "dollar", "backtick", "newline"]
)
def test_removal_command_prints_nothing_for_an_unquotable_path(fleet, char):
    """A `"` cannot be in a Windows file name, so this asks the printer directly."""
    checkout, root, _, _ = fleet
    bad = f"{(root / PROJECT).as_posix()}/e0787-a{char}b"
    v = wr.Verdict(worktree=Path(bad), registered=bad)
    assert v.removable, "precondition: nothing else refuses it"
    assert wr.removal_command(checkout, v) == []
    assert [r.key for r in wr._unquotable(checkout, bad)] == ["worktree_path_unquotable"]
    assert wr.removal_command(Path(bad), wr.Verdict(worktree=root, registered=str(root))) == []


@pytest.mark.parametrize("reason", ["kept for a bisect", None], ids=["with-reason", "no-reason"])
def test_a_git_locked_tree_refuses_with_no_command(fleet, reason):
    checkout, _, wt, _ = fleet
    lock = ["worktree", "lock", str(wt)] + (["--reason", reason] if reason else [])
    _git(checkout, *lock)
    v, cmd = _report(fleet)
    assert _keys(v) == ["worktree_locked"], [str(r) for r in v.refusals]
    assert (reason or "(none given)") in str(v.refusals[0])
    assert cmd == [] and wt.is_dir()
    _git(checkout, "worktree", "unlock", str(wt))
    assert _check(fleet).removable, "control: the same tree unlocked is removable"


_BS = "\\"


@pytest.mark.parametrize(
    "path",
    [_BS * 2 + "srv" + _BS + "share" + _BS + "wt" + _BS + "e0787-x", "C:" + _BS],
    ids=["unc-leading-pair", "trailing-backslash"],
)
def test_a_backslash_bash_would_collapse_or_escape_the_quote_with_refuses(fleet, path):
    """In bash double quotes a doubled backslash collapses and a trailing one eats the quote."""
    checkout, _, _, _ = fleet
    v = wr.Verdict(worktree=Path(path), registered=path)
    assert v.removable, "precondition: nothing else refuses it"
    assert wr.removal_command(checkout, v) == []
    assert [r.key for r in wr._unquotable(checkout, path)] == ["worktree_path_unquotable"]
    assert wr.removal_command(Path(path), wr.Verdict(worktree=checkout, registered="D:/x")) == []


def test_an_ordinary_backslashed_windows_path_still_prints(fleet):
    """Control for the refusal above: single inner backslashes are literal in bash quotes."""
    checkout, _, _, _ = fleet
    path = _BS.join(["C:", "Users", "u", "wt", PROJECT, EPIC_DIR])
    v = wr.Verdict(worktree=Path(path), registered=path)
    assert wr._unquotable(checkout, path) == []
    assert f'"{path}"' in wr.removal_command(checkout, v)[0]


def test_a_partial_clone_probe_never_lazily_fetches(tmp_path):
    """A blob:none clone lacks the upstream blobs the byte comparison diffs. Lazy fetch would
    write a promisor pack into the shared store; with it off the diff fails, which reads as not
    landed and keeps the tree, even though the branch's change did land."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "master", str(bare)], check=True, capture_output=True
    )
    _git(bare, "config", "uploadpack.allowFilter", "true")
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-b", "master", str(other)], check=True, capture_output=True)
    _identity(other)
    (other / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "seed")
    _git(other, "remote", "add", "origin", str(bare))
    _git(other, "push", "-u", "origin", "master")

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "--filter=blob:none", bare.as_uri(), str(checkout)],
        check=True,
        capture_output=True,
    )
    _identity(checkout)
    root = tmp_path / "wt"
    (root / PROJECT).mkdir(parents=True)
    wt = root / PROJECT / EPIC_DIR
    _git(checkout, "worktree", "add", str(wt), "-b", EPIC_DIR, "master")
    _identity(wt)
    _commit_in_tree(wt, "g.txt", "one\n")

    # The same change lands upstream as a squash, then someone else's file follows it.
    (other / "g.txt").write_text("one\n", encoding="utf-8")
    _git(other, "add", "g.txt")
    _git(other, "commit", "-m", "Squashed epic (#1)")
    (other / "later.txt").write_text("someone else's change\n", encoding="utf-8")
    _git(other, "add", "later.txt")
    _git(other, "commit", "-m", "later work")
    _git(other, "push", "origin", "master")
    _git(wt, "fetch", "origin")

    reg = tmp_path / "registry.md"
    reg.write_text(_registry._template_text(PROJECT), encoding="utf-8")
    before = _object_store(checkout)
    v = wr.check(checkout, root, PROJECT, wt, reg)
    assert _object_store(checkout) == before
    assert _keys(v) == ["worktree_unpushed_at_removal"], [str(r) for r in v.refusals]
