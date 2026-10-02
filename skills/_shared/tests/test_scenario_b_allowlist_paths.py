"""B4's allowlist must distinguish two files with the same name (gh#139).

B3 and B4 compared recorded commit paths to the plan's `file_allowlist` through
a `_basename` helper applied to BOTH sides. That made the comparison lossy in
the exact direction that matters:

- the shipped fixture allowlists `calc.py` and `tests/test_calc.py`, and the
  second collapsed to `test_calc.py`;
- a committed `vendor/calc.py` collapsed to `calc.py` and matched.

So B4 reported an out-of-allowlist path as "all committed files within
allowlist" — not a missed catch, a wrong statement — and B3 accepted any file
named `calc.py` as proof the wave touched the toy repo's own.

This is gated here rather than by running the harness, because `check.py` never
runs B1-B9 in CI: `--only C,F,G` is the CI invocation and `records/scenarioB` is
gitignored, so the recording machine is the only place check.py's OWN B rows
execute. This module is what closes that gap rather than a note about it: it
calls B2, B3 and B4 directly against synthetic records, and this directory runs
on ubuntu and windows in CI, so every shape enumerated below is executed on both
legs before any recording is made. (That also makes the separator control below
meaningful, which is why the two legs matter.)

The four controls are load-bearing. A B4 that simply refuses to pass satisfies
both red cases; and the absolute-path control is what keeps the fix from buying
strictness by turning every absolute recording red.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = REPO_ROOT / "harness"
SINGLE_TASK = HARNESS_DIR / "fixtures" / "plan-single-task.md"


def _load_check_module():
    """Import harness/check.py by path — it is a script, not a package module."""
    path = HARNESS_DIR / "check.py"
    assert path.is_file(), f"harness/check.py not found at {path} - moved, or this path is stale"
    spec = importlib.util.spec_from_file_location("agentflow_harness_check_b", path)
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: check.py defines dataclasses, and @dataclass resolves
    # its own module out of sys.modules at class-creation time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check():
    return _load_check_module()


class _Ctx:
    """Context stand-in: B3/B4 reach only for parse_plan."""

    def __init__(self):
        import agentflow.plan_parser as pp

        self.parse_plan = pp.parse_plan
        self.config = {}


@pytest.fixture()
def ctx():
    return _Ctx()


def _plant(check, tmp_path: Path, files, worktree=None) -> Path:
    """Write a scenarioB record whose only variable is the committed file list."""
    from agentflow import status_writer as sw

    recs = tmp_path / "records" / "scenarioB"
    recs.mkdir(parents=True, exist_ok=True)
    # B4 goes through the real `_load_scenario_b`, which needs a readable
    # status.md; write one with the real writer rather than hand-rolling the
    # format this suite exists to check.
    sw.write_status(
        recs / "status.md",
        sw.StatusState(
            epic="toy",
            plan="plan-single-task.md",
            started="2026-01-01T00:00:00+00:00",
            status="in_progress",
            current_phase="phase-1",
        ),
    )
    evidence = {
        "commits": [{"sha": "d00dfee", "files": list(files)}],
        "pytest_head": {"result": "pass"},
    }
    if worktree is not None:
        evidence["worktree_path"] = worktree
    (recs / "wave-evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    return tmp_path / "records"


def _run(check, ctx, tmp_path, files, worktree=None, fn="b4_diff_in_allowlist"):
    records = _plant(check, tmp_path, files, worktree)
    original = check.RECORDS_DIR
    check.RECORDS_DIR = records
    try:
        return getattr(check, fn)(ctx)
    finally:
        check.RECORDS_DIR = original


# --- the fixture still has the shape that makes these cases meaningful -----


def test_fixture_allowlist_still_has_the_two_level_shape(check, ctx) -> None:
    """If the fixture drifts to a flat allowlist, every case below tests nothing."""
    assert SINGLE_TASK.is_file(), f"{SINGLE_TASK} missing - B4 reads its allowlist here"
    assert check._scenario_b_allowlist(ctx) == ["calc.py", "tests/test_calc.py"]


# --- red cases -------------------------------------------------------------


def test_b4_refuses_a_same_named_file_in_another_directory(check, ctx, tmp_path) -> None:
    """Two files named calc.py, one allowlisted and one not."""
    result = _run(check, ctx, tmp_path, ["calc.py", "vendor/calc.py"])
    assert result.status == check.FAIL, f"B4 accepted an out-of-allowlist path: {result.detail}"
    assert "vendor/calc.py" in result.detail


def test_b4_fails_a_path_it_cannot_place(check, ctx, tmp_path) -> None:
    """An absolute path outside the recorded worktree cannot be judged against the
    allowlist. Today B4 does not merely mis-place it - it reports it as within."""
    result = _run(
        check,
        ctx,
        tmp_path,
        ["/elsewhere/toy-repo/calc.py"],
        worktree="/srv/records/toy-repo",
    )
    assert result.status == check.FAIL, f"unplaceable path silently accepted: {result.detail}"
    assert "not resolvable" in result.detail


def test_b3_refuses_a_same_named_file_in_another_directory(check, ctx, tmp_path) -> None:
    """B3's half of the same defect."""
    result = _run(check, ctx, tmp_path, ["vendor/calc.py"], fn="b3_commit_touches_calc")
    assert result.status == check.FAIL, f"B3 accepted vendor/calc.py as the toy repo's: {result.detail}"


# --- controls --------------------------------------------------------------


def test_b4_still_passes_the_honest_record(check, ctx, tmp_path) -> None:
    result = _run(check, ctx, tmp_path, ["calc.py", "tests/test_calc.py"])
    assert result.status == check.PASS, f"B4 rejected an honest record: {result.detail}"


def test_b4_accepts_windows_separators(check, ctx, tmp_path) -> None:
    """Guards the one behavior `_basename` had that the fix must not lose."""
    result = _run(check, ctx, tmp_path, ["calc.py", "tests\\test_calc.py"])
    assert result.status == check.PASS, f"B4 red on a Windows-recorded path: {result.detail}"


def test_b4_relativises_absolute_paths_against_the_recorded_worktree(check, ctx, tmp_path) -> None:
    """Passes today for the wrong reason (basename ignored the root), so it is a
    control, not a red-proof: it fails a fix that forgets `worktree_path` and turns
    every absolute recording red."""
    result = _run(
        check,
        ctx,
        tmp_path,
        ["/srv/records/toy-repo/calc.py", "/srv/records/toy-repo/tests/test_calc.py"],
        worktree="/srv/records/toy-repo",
    )
    assert result.status == check.PASS, f"B4 red on an absolute recording: {result.detail}"


def test_b3_still_passes_the_honest_record(check, ctx, tmp_path) -> None:
    result = _run(check, ctx, tmp_path, ["calc.py"], fn="b3_commit_touches_calc")
    assert result.status == check.PASS, f"B3 rejected an honest record: {result.detail}"


# --- B4 must refuse evidence that resolves to ZERO committed paths ---------
#
# The same defect one step further in, and the epic's own property: with no
# committed path recorded, `touched - allow` is empty for the same reason C7's
# `sum(...)` over an empty needle list was zero, and B4's PASS detail then states
# a fact about "all committed files" having examined none. Four shapes reach it,
# and none of them is exotic - `commits: []` is what a wave that reviewed clean
# but committed nothing records, and `{}` is what a truncated write leaves.
#
# B3 already goes FAIL on all four, so the suite as a whole notices. That is not
# a substitute: B4's own verdict is the false statement, and B3 is asserting
# something else entirely.


def _plant_raw(check, tmp_path: Path, evidence, tasks=None) -> Path:
    """Plant a scenarioB record with an ARBITRARY evidence object.

    `_plant` above always writes one commit with a file list; these cases are
    about evidence shapes that have no file list to vary.
    """
    from agentflow import status_writer as sw

    recs = tmp_path / "records" / "scenarioB"
    recs.mkdir(parents=True, exist_ok=True)
    sw.write_status(
        recs / "status.md",
        sw.StatusState(
            epic="toy",
            plan="plan-single-task.md",
            started="2026-01-01T00:00:00+00:00",
            status="in_progress",
            current_phase="phase-1",
            tasks=tasks or {},
        ),
    )
    (recs / "wave-evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    return tmp_path / "records"


def _run_raw(check, ctx, tmp_path, evidence, tasks=None, fn="b4_diff_in_allowlist"):
    records = _plant_raw(check, tmp_path, evidence, tasks)
    original = check.RECORDS_DIR
    check.RECORDS_DIR = records
    try:
        return getattr(check, fn)(ctx)
    finally:
        check.RECORDS_DIR = original


_EMPTY_EVIDENCE_SHAPES = {
    "commit with an empty file list": {
        "commits": [{"sha": "d00dfee", "files": []}],
        "pytest_head": {"result": "pass"},
    },
    "evidence object is empty": {},
    "worktree_path present, no commits key": {"worktree_path": "/srv/records/toy-repo"},
    "commits list is empty": {"commits": [], "pytest_head": {"result": "pass"}},
}


@pytest.mark.parametrize("shape", sorted(_EMPTY_EVIDENCE_SHAPES), ids=lambda s: s.replace(" ", "-"))
def test_b4_refuses_evidence_with_no_committed_paths(check, ctx, tmp_path, shape) -> None:
    result = _run_raw(check, ctx, tmp_path, _EMPTY_EVIDENCE_SHAPES[shape])
    assert result.status == check.FAIL, (
        f"B4 asserted a fact about committed files having examined none [{shape}]: {result.detail}"
    )
    assert "no committed file path" in result.detail
    assert "within allowlist" not in result.detail, "the false claim survived into the FAIL detail"


def test_b4_zero_path_refusal_is_not_a_blanket_refusal(check, ctx, tmp_path) -> None:
    """Control: the refusal must key on the EMPTY path set, not on evidence shape.

    Without this, a B4 hard-wired to FAIL satisfies every case above.
    """
    honest = _run_raw(
        check,
        ctx,
        tmp_path / "honest",
        {"commits": [{"sha": "d00dfee", "files": ["calc.py"]}], "pytest_head": {"result": "pass"}},
    )
    empty = _run_raw(check, ctx, tmp_path / "empty", {"commits": [{"sha": "d00dfee", "files": []}]})
    assert honest.status == check.PASS, f"B4 red on a one-file record: {honest.detail}"
    assert empty.status == check.FAIL


# --- B2 shares B3/B4's worktree resolution rather than copying it ----------


_TASK_WORKTREE = "/srv/records/toy-repo"


def _task_state(check):
    from agentflow import status_writer as sw

    return {1: sw.TaskState(state="done", phase="phase-1", wave_worktree_path=_TASK_WORKTREE)}


def test_b2_falls_back_to_the_worktree_recorded_on_a_task(check, ctx, tmp_path) -> None:
    """B2's fallback had no coverage at all before this module: check.py's own B
    rows never run in CI, and nothing else called `b2_worktree`. This test is now
    where that fallback is exercised on every push."""
    result = _run_raw(
        check,
        ctx,
        tmp_path,
        {"commits": [{"sha": "d00dfee", "files": ["calc.py"]}]},
        tasks=_task_state(check),
        fn="b2_worktree",
    )
    assert result.status == check.PASS, f"B2 missed the task-state worktree: {result.detail}"
    assert _TASK_WORKTREE in result.detail


def test_b2_and_b4_agree_on_the_worktree_root(check, ctx, tmp_path) -> None:
    """B2 reported one root while B3/B4 relativised against another only because
    the resolution was duplicated. Pin them to the same answer through the same
    record: absolute paths under the TASK-recorded worktree must place."""
    evidence = {"commits": [{"sha": "d00dfee", "files": [_TASK_WORKTREE + "/calc.py"]}]}
    b2 = _run_raw(check, ctx, tmp_path / "b2", evidence, tasks=_task_state(check), fn="b2_worktree")
    b4 = _run_raw(check, ctx, tmp_path / "b4", evidence, tasks=_task_state(check))
    assert b2.status == check.PASS, f"B2: {b2.detail}"
    assert b4.status == check.PASS, (
        f"B4 could not place a path under the worktree B2 reports: {b4.detail}"
    )


def test_b2_fails_when_no_worktree_is_recorded_anywhere(check, ctx, tmp_path) -> None:
    """Control: the fallback must not manufacture a root out of nothing."""
    result = _run_raw(
        check, ctx, tmp_path, {"commits": [{"sha": "d00dfee", "files": ["calc.py"]}]}, fn="b2_worktree"
    )
    assert result.status == check.FAIL, f"B2 claimed a worktree it was never given: {result.detail}"


# --- the recorder contract must state what `files` entries are relative to --
#
# Under the old basename comparison relativity did not matter. It now does: B3
# and B4 place every recorded path. The record-layout block in check.py's module
# docstring calls itself "the contract the P0-T6..T10 recorders follow" and is
# the only thing a recorder author reads, so it has to say what the paths are
# relative to. check.py's own B rows never run in CI - but the red case below
# does, on both legs, so a recorder emitting repo-root-relative paths is caught
# here first, rather than turning B3 and B4 red on the single machine that
# records. That sentence exists to tell a recorder author what will catch them,
# so it must not still say "nothing".


def test_the_recorder_contract_states_path_relativity(check) -> None:
    _, _, contract = check.__doc__.partition("Record layout")
    assert contract, "the record-layout contract block is gone from check.py's docstring"
    assert "worktree-relative" in contract.lower(), (
        "the recorder contract documents wave-evidence.json without saying what "
        "`commits[].files` entries are relative to"
    )


def test_b4_reddens_a_repo_root_relative_recording(check, ctx, tmp_path) -> None:
    """The failure mode the contract line exists to prevent, executed."""
    result = _run(
        check,
        ctx,
        tmp_path,
        ["harness/toy-repo/calc.py"],
        worktree="/srv/records/toy-repo",
    )
    assert result.status == check.FAIL, f"a repo-root-relative recording passed: {result.detail}"
    assert "harness/toy-repo/calc.py" in result.detail, "the FAIL does not name the record"


# --- the contract prose must not deny the coverage that exists -------------
#
# Every sentence above about what catches a mis-relativised recorder used to end
# in "nothing does". That was written before this module existed and stopped
# being true the moment it did - and it is the sentence a maintainer would cite
# to justify not adding B coverage that is already here. It is this epic's own
# property applied to prose: a claim about what was examined, made without
# examining. The anchors below are what keep it honest.
#
# Two of the three are BEHAVIOURAL - they read the workflow CI actually executes,
# and they observe the B assertions being called - because an anchor that only
# asserts a substring is present can be satisfied by prose saying the opposite.
# The third is an explicit string-grep, and is labelled as one; it exists only to
# catch the retired claims coming back by copy-paste.

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The tests that enforce the relativity half of the recorder contract. Named
#: here so the anchors below grade THIS list rather than "some test somewhere".
_RELATIVITY_TESTS = (
    "test_b4_reddens_a_repo_root_relative_recording",
    "test_b4_fails_a_path_it_cannot_place",
    "test_b4_accepts_windows_separators",
    "test_b4_relativises_absolute_paths_against_the_recorded_worktree",
)


def _shared_tests_job():
    """The CI job that runs this directory, read from the workflow itself.

    BEHAVIOURAL anchor input: this is the file GitHub executes, so a change that
    stops running this suite - a narrowed `-k`, a moved working-directory, a
    deleted job - reddens here instead of quietly making the prose false again.
    """
    import yaml

    assert CI_WORKFLOW.is_file(), f"{CI_WORKFLOW} missing - CI moved, or this path is stale"
    jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    assert "shared-tests" in jobs, (
        f"no `shared-tests` job in ci.yml (jobs: {sorted(jobs)}) - this module's "
        "claim to run in CI rests on it"
    )
    return jobs["shared-tests"]


def test_this_module_runs_on_both_ci_legs() -> None:
    """BEHAVIOURAL. The docstring says B2-B4 execute on ubuntu and windows in CI.
    Read the job that would do it and check every link in that chain."""
    job = _shared_tests_job()

    legs = job["strategy"]["matrix"]["os"]
    assert any(leg.startswith("ubuntu") for leg in legs), f"no ubuntu leg: {legs}"
    assert any(leg.startswith("windows") for leg in legs), f"no windows leg: {legs}"

    workdir = job["defaults"]["run"]["working-directory"]
    assert (REPO_ROOT / workdir).resolve() == Path(__file__).resolve().parents[1], (
        f"the shared-tests job runs from {workdir!r}, which is not this module's package root"
    )

    runs = [step["run"] for step in job["steps"] if "run" in step]
    pytest_runs = [r for r in runs if "pytest" in r]
    assert pytest_runs, f"the shared-tests job no longer runs pytest at all: {runs}"
    # A selector is the quiet way this coverage disappears: the job still exists,
    # still runs pytest, and no longer reaches this file.
    for cmd in pytest_runs:
        for selector in (" -k ", " --ignore", " --deselect"):
            assert selector not in cmd, (
                f"the CI pytest invocation carries a selector that may exclude this "
                f"module: {cmd!r}"
            )

    # ... and this file is inside what that bare invocation collects.
    import tomllib

    cfg = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    testpaths = cfg["tool"]["pytest"]["ini_options"]["testpaths"]
    package_root = Path(__file__).resolve().parents[1]
    collected = {(package_root / tp).resolve() for tp in testpaths}
    assert Path(__file__).resolve().parent in collected, (
        f"this module lives outside pytest's testpaths {testpaths}"
    )
    assert Path(__file__).name.startswith("test_"), "this module is not discoverable by pytest"


def test_the_ci_run_reaches_b2_b3_and_b4(check, ctx, tmp_path, monkeypatch) -> None:
    """BEHAVIOURAL. The claim is not that a file exists but that these checker
    functions EXECUTE. Wrap them, run the enumerated tests in-process, and read
    back which ones were reached - the same instrumentation used to disprove the
    original "nothing catches it first" claim, made permanent."""
    reached = set()

    for name, ident in (
        ("b2_worktree", "B2"),
        ("b3_commit_touches_calc", "B3"),
        ("b4_diff_in_allowlist", "B4"),
    ):
        original = getattr(check, name)

        def wrapped(c, _o=original, _i=ident):
            reached.add(_i)
            return _o(c)

        monkeypatch.setattr(check, name, wrapped)

    # Graded PER TEST, not over the union. A set-level assertion is satisfied as
    # long as SOME test still reaches each id, so a named test regressed into a
    # no-op leaves it green - which is this epic's property, one level up: the
    # anchor would be reporting coverage it had not examined.
    module = sys.modules[__name__]
    named = _RELATIVITY_TESTS + (
        "test_b3_still_passes_the_honest_record",
        "test_b2_falls_back_to_the_worktree_recorded_on_a_task",
    )
    per_test = {}
    for i, name in enumerate(named):
        fn = getattr(module, name, None)
        assert fn is not None, f"{name} is named in the contract prose but no longer exists here"
        reached.clear()
        fn(check, ctx, tmp_path / f"reach-{i}")
        per_test[name] = set(reached)

    silent = sorted(name for name, ids in per_test.items() if not ids)
    assert not silent, (
        f"tests this module's prose points a recorder author at reached NO checker "
        f"assertion at all: {silent} - the prose is claiming coverage that does not execute"
    )
    assert set().union(*per_test.values()) == {"B2", "B3", "B4"}, (
        f"the named tests reached {sorted(set().union(*per_test.values()))}, not B2/B3/B4"
    )


def test_the_two_relativity_failure_arms_are_distinct(check, ctx, tmp_path) -> None:
    """BEHAVIOURAL, and the anchor for the contract's grouping (gh#139 follow-up).

    The contract used to group these as "repo-root-relative or otherwise
    unplaceable", which names one mechanism for two. Executed, they are two: a
    repo-root-relative RELATIVE path places fine and fails the allowlist; only an
    absolute path outside `worktree_path` is unplaceable. Both are loud, so the
    operative claim was true and only the grouping was wrong - which is exactly
    the kind of error a substring anchor would never catch."""
    relative = _run(
        check, ctx, tmp_path / "rel", ["harness/toy-repo/calc.py"], worktree="/srv/records/toy-repo"
    )
    absolute = _run(
        check, ctx, tmp_path / "abs", ["/elsewhere/toy-repo/calc.py"], worktree="/srv/records/toy-repo"
    )
    assert relative.status == check.FAIL and absolute.status == check.FAIL
    assert "outside allowlist" in relative.detail, (
        f"a repo-root-relative entry no longer fails through the allowlist arm: {relative.detail}"
    )
    assert "not resolvable" not in relative.detail, (
        f"a repo-root-relative entry is placeable; it must not be reported as unplaceable: "
        f"{relative.detail}"
    )
    assert "not resolvable" in absolute.detail, (
        f"an absolute path outside the worktree no longer fails as unplaceable: {absolute.detail}"
    )


def test_the_contract_no_longer_claims_nothing_catches_a_bad_recorder(check) -> None:
    """STRING-GREP, and labelled as one. The two anchors above are what actually
    hold; this one only stops the retired sentences being pasted back."""
    _, _, contract = check.__doc__.partition("Record layout")
    assert contract, "the record-layout contract block is gone from check.py's docstring"
    assert "test_scenario_b_allowlist_paths.py" in contract, (
        "the recorder contract tells a recorder author what enforces it, and no "
        "longer names the suite that does"
    )
    for retired in (
        "the only place this contract is enforced",
        "None of B1-B9 runs in CI",
    ):
        assert retired not in contract, (
            f"a retired claim is back in the recorder contract: {retired!r} - "
            "check.py's B rows do not run in CI, but this suite does"
        )


# --------------------------------------------------------------------------- #
# gh#218 item 6 - the drive letter was folded, the path body was not
#
# `_posix` upper-cases `c:/` so a root comparison cannot go red on the letter alone, and
# then `_rel_to_root` compared the REST byte-for-byte. On Windows those two halves come
# from different places - the recorder writes the worktree path, git reports the file
# paths - so a record whose halves disagree in case reported its own files as living
# outside its own worktree. A false RED, and the reason it survived is that check.py's B
# rows never run in CI; this module is where they do.
# --------------------------------------------------------------------------- #


def test_a_drive_path_places_when_only_the_case_of_the_body_differs(check) -> None:
    """The bug as gh#218 states it: worktree 'c:/WT', recorded file 'C:/wt/calc.py'."""
    assert check._rel_to_root("C:/wt/calc.py", "c:/WT") == "calc.py"
    assert check._rel_to_root("c:/wt/tests/test_calc.py", "C:/WT") == "tests/test_calc.py"
    # ...and the returned path keeps the RECORD's own casing, since that is the string an
    # allowlist entry and a later reader are matched against.
    assert check._rel_to_root("C:/WT/Vendor/Calc.py", "c:/wt") == "Vendor/Calc.py"


def test_a_drive_path_outside_the_root_is_still_unplaceable(check) -> None:
    """The control. Folding case must not fold two different directories together."""
    assert check._rel_to_root("C:/elsewhere/calc.py", "c:/WT") is None
    assert check._rel_to_root("D:/wt/calc.py", "c:/wt") is None, "a different drive is a different root"


def test_a_posix_root_is_NOT_case_folded(check) -> None:
    """The other control, and the reason the fold is conditional.

    `/Srv` and `/srv` are genuinely different directories on the platform that uses
    them, so folding there would buy this repair by turning a real miss green.
    """
    assert check._rel_to_root("/Srv/wt/calc.py", "/srv/wt") is None
    assert check._rel_to_root("/srv/wt/calc.py", "/srv/wt") == "calc.py"
