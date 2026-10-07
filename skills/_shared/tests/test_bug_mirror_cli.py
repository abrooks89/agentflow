"""The mirror has an ENTRY POINT, and it agrees with the other rendering of the same fact.

E-0212 slice 4. `test_bug_mirror.py` gates the renderers; this module gates the command the
steward actually types (`issue_intake mirror`) and the closed-issue lookup behind it. A
renderer with no reachable caller is a capability nobody can use - the same shape as a flag
that ships with zero prose.

Two behaviours here are load-bearing and neither is obvious from the renderers:

  IN-FLIGHT COMES FROM THE SAME TWO SURFACES `fleet_report` USES - Agents rows and CLAIMED
  Queue rows. `in_flight` greps for `gh#N`, and the registry mentions issue numbers in half a
  dozen places that assert nothing about who is building what. Two renderings of one fact
  disagreeing is the drift the mirror exists to prevent.

  THE CLOSED LOOKUP DEGRADES, IT DOES NOT ABORT. The open index is written before the tracker
  is asked for closed issues, so a failure there costs the newest half of the mirror and not
  all of it - and it SAYS so, because a silent half is worse than a loud one.

WHAT THIS GATE CAN AND CANNOT DO:

* It CAN prove the command writes the right files, that month filtering and PR provenance
  survive the parse, that a bad registry degrades loudly, and that a failed closed lookup
  still leaves the open index on disk.
* It CANNOT prove the mirror's CONTENT is right - that is `test_bug_mirror.py`, which gates
  the renderers this module only drives.

The last section gates the steward's INVOCATION, which is prose. A command nothing calls is
the same defect as a renderer nothing reaches, one layer up.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS_ROOT))

from agentflow import issue_intake as ii  # noqa: E402
from agentflow.config import WorkSource  # noqa: E402
from agentflow.issue_intake import (  # noqa: E402
    Issue,
    IssueIntakeError,
    list_closed_issues,
)

HOST_REF = "owner/repo"


class FakeRunner:
    def __init__(self, stdout="[]", returncode=0, stderr=""):
        self.calls = []
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.returncode, self.stdout, self.stderr


def _closed_raw(number, closed_at, prs=()):
    return {
        "number": number,
        "title": f"issue {number}",
        "labels": [{"name": "bug"}],
        "closedAt": closed_at,
        "closedByPullRequestsReferences": [{"number": n, "url": u} for n, u in prs],
    }


# --------------------------------------------------------------------------- #
# list_closed_issues
# --------------------------------------------------------------------------- #
def test_only_the_requested_month_comes_back():
    """Months outside the window are dropped here, so the caller cannot rewrite a frozen one."""
    runner = FakeRunner(stdout=json.dumps([
        _closed_raw(1, "2026-09-02T10:00:00Z"),
        _closed_raw(2, "2026-08-30T10:00:00Z"),
    ]))
    got = list_closed_issues(HOST_REF, kind="github-issues", cli="gh", month="2026-09",
                             runner=runner)
    assert [c.number for c in got] == [1]


def test_the_closing_prs_are_carried_across_as_provenance():
    runner = FakeRunner(stdout=json.dumps([
        _closed_raw(1, "2026-09-02T10:00:00Z", prs=((10, "u/10"), (9, "u/9"))),
    ]))
    got = list_closed_issues(HOST_REF, kind="github-issues", cli="gh", month="2026-09",
                             runner=runner)
    # Sorted, so the rendered month is stable no matter what order the tracker answers in.
    assert got[0].closing_prs == ((9, "u/9"), (10, "u/10"))
    assert got[0].outcome == "shipped"


def test_an_issue_closed_with_no_pr_is_not_reported_as_shipped():
    runner = FakeRunner(stdout=json.dumps([_closed_raw(1, "2026-09-02T10:00:00Z")]))
    got = list_closed_issues(HOST_REF, kind="github-issues", cli="gh", month="2026-09",
                             runner=runner)
    assert got[0].outcome == "closed without a PR"


def test_it_asks_for_the_field_the_provenance_depends_on():
    """`closedByPullRequestsReferences` is the whole reason this is not a plain issue list."""
    runner = FakeRunner()
    list_closed_issues(HOST_REF, kind="github-issues", cli="gh", month="2026-09", runner=runner)
    argv = runner.calls[0]
    assert "--state" in argv and argv[argv.index("--state") + 1] == "closed"
    assert "closedByPullRequestsReferences" in argv[argv.index("--json") + 1]


def test_a_tracker_that_is_not_a_tracker_spawns_no_process():
    """Same no-op rule the rest of this module follows: no `kind`, no subprocess."""
    runner = FakeRunner()
    assert list_closed_issues(HOST_REF, kind="none", cli="gh", month="2026-09", runner=runner) == []
    assert runner.calls == []


def test_malformed_json_fails_loudly():
    runner = FakeRunner(stdout="{not json")
    with pytest.raises(IssueIntakeError):
        list_closed_issues(HOST_REF, kind="github-issues", cli="gh", month="2026-09",
                           runner=runner)


# --------------------------------------------------------------------------- #
# _claimed_refs - the surface agreement with fleet_report
# --------------------------------------------------------------------------- #
_REGISTRY = """# Fleet Registry

## Agents

| agent | status | work | phase | heartbeat | eta | waiting_on |
|---|---|---|---|---|---|---|
| lead-dev | building | E-0100 | fixing gh#11 | 2026-09-03T06:00:00Z | 2026-09-03T07:00:00Z | - |
| jr-dev-1 | idle | - | - | 2026-09-03T06:00:00Z | 2026-09-03T07:00:00Z | - |

## Queue

deploy lock: holder: none

| id | work_item | lane | claimed_by | state | waiting_on | notes |
|---|---|---|---|---|---|---|
| E-0101 | gh#12 mid-build | jr-dev-2 | jr-dev-2 | building | - | see gh#17 |
| E-0102 | gh#13 not started | jr-dev-2 | - | ready | - | - |
| E-0103 | gh#15 shipped | jr-dev-1 | jr-dev-1 | done | - | - |
| E-0104 | gh#16 pulled back | jr-dev-1 | - | withdrawn | - | - |

## Questions

## Blockers

## Directives

- gh#14 was discussed here and is not being built by anyone
"""


def _write_registry(dirpath, text=_REGISTRY):
    p = Path(dirpath) / "registry.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_held_counts_an_agents_row_a_claimed_row_and_an_unclaimed_ready_row(tmp_path):
    refs, note = ii._claimed_refs(_write_registry(tmp_path))
    blob = " ".join(refs)
    assert "gh#11" in blob        # Agents row phase
    assert "gh#12" in blob        # claimed Queue row
    assert "gh#13" in blob        # published, unclaimed: already handed out
    assert note == ""


@pytest.mark.parametrize("ref,where", [("gh#14", "a Directive"),
                                       ("gh#15", "a claimed DONE queue row"),
                                       ("gh#16", "a WITHDRAWN queue row"),
                                       ("gh#17", "a live row's notes cell")])
def test_a_number_mentioned_where_nobody_holds_it_is_not_in_flight(tmp_path, ref, where):
    """The over-broad version of this read the whole file and called all of these in flight."""
    refs, _ = ii._claimed_refs(_write_registry(tmp_path))
    assert ref not in " ".join(refs), where


def test_a_missing_registry_says_so_rather_than_reporting_an_idle_fleet(tmp_path):
    """No claims, and 'we could not read the claims', render identically. The note separates them."""
    _, note = ii._claimed_refs(tmp_path / "does-not-exist.md")
    assert "in-flight grouping skipped" in note


def test_a_half_written_registry_degrades_instead_of_killing_the_mirror(tmp_path):
    refs, note = ii._claimed_refs(_write_registry(tmp_path, "## Agents\n\n| broken"))
    assert refs == []
    assert note != ""


def test_a_queue_section_whose_table_is_gone_degrades_with_a_note_like_selectable_refuses(tmp_path):
    """The heading survives, the table does not. The parser alone reads that as zero rows - an
    idle board - so the mirror listed every held bug as selectable with no note, while the
    `selectable` CLI refused the same file. Both now read the board through one checked path."""
    damaged = _REGISTRY.replace(
        "| id | work_item | lane | claimed_by | state | waiting_on | notes |\n"
        "|---|---|---|---|---|---|---|\n", "")
    path = _write_registry(tmp_path, damaged)

    refs, note = ii._claimed_refs(path)

    assert refs == []
    assert "file_damaged" in note and "in-flight grouping skipped" in note
    with pytest.raises(ii.IssueIntakeError):
        ii.read_held_set(path)


# --------------------------------------------------------------------------- #
# _run_mirror - the command itself
# --------------------------------------------------------------------------- #
@pytest.fixture
def env(tmp_path):
    (tmp_path / "projects" / "p" / "status").mkdir(parents=True)
    (tmp_path / "projects" / "p" / "design" / "specs").mkdir(parents=True)
    _write_registry(tmp_path / "projects" / "p" / "status")
    return SimpleNamespace(data_root=tmp_path, git_host=SimpleNamespace(cli="gh"))


@pytest.fixture
def proj():
    return SimpleNamespace(
        canonical_repo=SimpleNamespace(host_ref=HOST_REF),
        work_source=WorkSource(kind="github-issues"),
    )


def _run(env, proj, issues=(), closed=None, error=None):
    def fake_closed(*a, **k):
        if error:
            raise IssueIntakeError(error)
        return list(closed or [])

    orig, ii.list_closed_issues = ii.list_closed_issues, fake_closed
    try:
        specs = Path(env.data_root) / "projects" / "p" / "design" / "specs"
        return ii._run_mirror(env, proj, "p", list(issues), specs)
    finally:
        ii.list_closed_issues = orig


def _bugs(env):
    return Path(env.data_root) / "projects" / "p" / "design" / "bugs"


def test_the_command_writes_the_index_and_the_current_month(env, proj):
    assert _run(env, proj, issues=[Issue(number=3, title="t", labels=("bug", "triaged"))]) == 0
    assert (_bugs(env) / "index.md").exists()
    assert len(list(_bugs(env).glob("closed-*.md"))) == 1


def test_a_held_bug_lands_in_the_in_flight_group_via_the_registry(env, proj):
    """End to end: the held rows on gh#12 and gh#13 reach the rendered grouping; the done
    row on gh#15 does not."""
    _run(env, proj, issues=[Issue(number=12, title="mid-build", labels=("bug", "triaged")),
                            Issue(number=13, title="not started", labels=("bug", "triaged")),
                            Issue(number=15, title="shipped", labels=("bug", "triaged"))])
    body = (_bugs(env) / "index.md").read_text(encoding="utf-8")
    flight, rest = body.split("## Triaged")
    assert "#12" in flight
    assert "#13" in flight
    assert "#15" not in flight
    assert "#15" in rest


def test_running_twice_against_unchanged_state_leaves_the_files_alone(env, proj):
    """The churn gate, at the level the steward actually invokes - not just the renderer."""
    issues = [Issue(number=3, title="t", labels=("bug", "triaged"))]
    _run(env, proj, issues=issues)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in _bugs(env).glob("*.md")}
    _run(env, proj, issues=issues)
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in _bugs(env).glob("*.md")}
    assert after == before


def test_a_failed_closed_lookup_still_leaves_the_open_index_written(env, proj, capsys):
    """Degrade, do not abort: half a mirror beats none, provided it says which half."""
    assert _run(env, proj, issues=[Issue(number=3, title="t", labels=("bug",))],
                error="tracker exploded") == 0
    assert (_bugs(env) / "index.md").exists()
    assert not list(_bugs(env).glob("closed-*.md"))
    assert "tracker exploded" in capsys.readouterr().err


def test_a_run_that_changed_nothing_still_says_it_ran(env, proj, capsys):
    """'ran and found nothing' must not read as 'did not run' - the steward reports this line."""
    issues = [Issue(number=3, title="t", labels=("bug", "triaged"))]
    _run(env, proj, issues=issues)
    capsys.readouterr()
    _run(env, proj, issues=issues)
    out = capsys.readouterr().out
    assert "wrote nothing" in out
    assert "unchanged" in out


def test_mirror_is_a_real_mode_of_the_cli(env, proj, monkeypatch):
    """The steward types a COMMAND. An unreachable renderer is the defect this closes."""
    from agentflow import config as cfg

    monkeypatch.setattr(cfg, "load_environment", lambda: env)
    monkeypatch.setattr(cfg, "load_project", lambda name: proj)
    monkeypatch.setattr(ii, "list_open_issues", lambda *a, **k: [])
    monkeypatch.setattr(ii, "list_closed_issues", lambda *a, **k: [])
    assert ii.main(["mirror", "--project", "p"]) == 0
    assert (_bugs(env) / "index.md").exists()


# --------------------------------------------------------------------------- #
# The invocation - prose, because that is what a loop actually executes
# --------------------------------------------------------------------------- #
STEWARD = SKILLS_ROOT.parent / "steward" / "SKILL.md"


@pytest.fixture(scope="module")
def steward():
    return STEWARD.read_text(encoding="utf-8")


def test_the_steward_is_told_the_exact_command(steward):
    """Not "regenerate the mirror" - the line a loop can run without inventing arguments."""
    assert "python -m agentflow.issue_intake mirror --project" in steward


def test_the_mirror_is_a_cheap_every_wake_domain_not_a_rotated_one(steward):
    """Rotated domains run on a schedule; a mirror that lags the tracker by six wakes is stale."""
    block = steward.split("**M. The bug mirror")[1].split("**P.")[0]
    assert "every wake" in block.lower()


def test_the_steward_is_told_the_files_are_derived_and_must_not_be_hand_edited(steward):
    """The one rule that keeps this from becoming a second source of truth for every bug."""
    block = steward.split("**M. The bug mirror")[1].split("**P.")[0]
    assert "DERIVED" in block
    assert "Never hand-edit" in block


def test_the_sweep_record_distinguishes_ran_and_changed_nothing_from_did_not_run(steward):
    """The command prints the distinction; the skill has to make the steward report it."""
    assert "ran and changed nothing" in steward


def test_the_domain_is_on_the_end_of_wake_checklist_not_only_in_prose(steward):
    """A duty described but never checked off is the failure mode E-0220 already paid for."""
    checklist = steward.split("- [ ]")
    assert any("Domain M ran" in item for item in checklist)


# --------------------------------------------------------------------------- #
# RUN AS A MODULE - the way the steward actually invokes it
# --------------------------------------------------------------------------- #
# `ii.main([...])` runs against a fully imported module, where every name is bound. A real
# `python -m` run executes the file top to bottom and stops at the `__main__` guard, so any
# definition BELOW that guard does not exist yet when main() calls it. Slices 2 and 4 appended
# to the end of the module, putting the mirror's whole implementation below the guard: the
# import-level test passed, CI was green on 14 checks, and the first real invocation raised
# `NameError: _run_mirror`. The distinction is invisible to every test that imports.
#
# The STRUCTURAL test is the one that gates it - verified by mutation, which the subprocess
# test below does not survive on its own. Structure is a weaker kind of evidence than
# behaviour and is used here only because the behaviour needs a resolvable project, and the
# one on this machine is live data.
MODULE_PATH = SKILLS_ROOT / "agentflow" / "issue_intake.py"


def test_the_main_guard_is_the_last_thing_in_the_module():
    """Anything appended after it is dead to a `python -m` run, silently."""
    lines = [ln for ln in MODULE_PATH.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    guard = max(i for i, ln in enumerate(lines) if ln.startswith("if __name__"))
    assert guard == len(lines) - 2, (
        "definitions appear after the __main__ guard; they will not exist when main() runs")


def test_a_module_run_fails_through_the_modules_own_handler_not_a_traceback():
    """A real process, so import-time breakage shows up here and not at a steward's first sweep.

    HONEST SCOPE, because the obvious reading is wrong: an unknown project dies in
    `load_project` BEFORE `_run_mirror` is reached, so this does NOT gate the guard position -
    the structural test above is what does. What this gates is that a `python -m` run gets as
    far as the module's own error path at all, which any import-order or syntax breakage in the
    appended half would break. Reaching `_run_mirror` from a subprocess needs a resolvable
    project, and the only one on this machine is live data the suite must not write to.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "agentflow.issue_intake", "mirror",
         "--project", "no-such-project-exists"],
        cwd=str(SKILLS_ROOT), capture_output=True, text=True,
    )
    assert "NameError" not in proc.stderr, proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    assert proc.returncode == 1


def test_every_mode_is_offered_by_the_real_command_line():
    """The steward and lead-dev read mode names off this help text, not off the source, so a
    mode that exists in the module but never reaches argparse is invisible to them."""
    proc = subprocess.run(
        [sys.executable, "-m", "agentflow.issue_intake", "--help"],
        cwd=str(SKILLS_ROOT), capture_output=True, text=True,
    )
    assert proc.returncode == 0
    for mode in ("untriaged", "selectable", "awaiting", "mirror", "claim", "release"):
        assert mode in proc.stdout, f"`{mode}` is not offered by the CLI"
    assert "--issue" in proc.stdout
