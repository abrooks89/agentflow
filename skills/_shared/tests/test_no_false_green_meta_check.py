"""`--self-test` must catch an assertion that goes green having seen nothing.

The fifth finding of gh#139. `--self-test`'s step 4 runs every assertion against
absent records and asserts none of them CRASHES. That is a no-crash check, never
a no-false-green one, so it could not see the four defects the issue is about:
C3, C4 and C7 all returned PASS on the records-absent world — the only world CI
ever runs them in — while examining nothing at all.

Step 4b closes the empty-input half of that. With records absent, a PASS is a
claim to have observed something, so each one must point at a present, non-empty
input through `check.ABSENCE_PASS_EVIDENCE`; deny-by-default, so a NEW assertion
that goes green on nothing is caught without anyone remembering to register it.

Hermetic over the machine-local config (gh#220). Repointing RECORDS_DIR and
WAKE_LOG built a guaranteed records-absent world but left `harness.config.json`
ambient, and that file is gitignored — so it exists on every configured developer
machine and on no fresh clone. Three tests here were red on the former and green
on the latter for exactly as long as the file has existed, and CI, always a fresh
clone, was structurally incapable of seeing it. Every run below therefore pins
the config itself, and does so across BOTH worlds the harness ships into:
the tracked example config (empty `preport_ip_literals`, what CI sees) and a
synthesized configured one (a non-empty needle list, what an operator who followed
C7's own SKIP message sees). The configured world had no coverage at all before.

Hermetic over the machine-local RECORDS too (gh#302). The same shape of defect one
axis over: step 4b used to SKIP itself whenever `harness/records/` or
`harness/stubs/schedulewakeup_log.md` existed, and both are gitignored, so on every
machine that had ever recorded a scenario the one gate against a vacuous PASS did
not run — while CI, always a fresh clone, ran it and stayed green. The skip said so
out loud; visible and not running is still not running. check.py now synthesizes the
records-absent world for steps 4 and 4b instead of reading whatever is on disk, and
the tests near the bottom of this file assert that ACROSS records worlds — the config
tests above them vary only the config, so nothing pinned this axis before.

The command, not just the tests (gh#340). gh#220 was closed by pinning the config
in THIS file, while `check.py --self-test` itself kept reading the machine-local
config whenever one existed, so the shipped verdict still moved with it. The command
now always grades against the tracked example config; the configured world is handed
to `run_self_test` explicitly here, and the records tests cross both axes.

Crash coverage over the recordings, returned (gh#341). gh#302 moved step 4 into the
synthesized world, so a recording that made an assertion raise no longer surfaced in
`--self-test`. Step 4d runs every assertion over the ambient recordings for crashes
only, verdicts discarded; the tests at the very bottom pin that split.

What it does NOT cover, asserted here so a green is not read as more than it is:
an assertion that examines a present input through a lossy key (B4's basename
comparison) has a truthy probe and passes this check. Only the empty-input world
is covered.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = REPO_ROOT / "harness"
EXAMPLE_CONFIG = HARNESS_DIR / "harness.config.example.json"

MARKER = "self-test.no-false-green"

#: RFC 5737 TEST-NET-3, reserved for documentation. The configured world's
#: mechanism depends only on the needle list being NON-EMPTY, never on its value,
#: and a real infra literal is the exact thing C7 exists to keep out of this tree.
#:
#: Assembled from two pieces so the whole string never appears verbatim in any file
#: C7 READS — including this one. Not "in any file under skills/": CPython constant-folds
#: the two pieces back into one string in the .pyc, so the literal does exist on disk under
#: skills/_shared/tests/__pycache__/. That is harmless because __pycache__ is in
#: _GREP_SKIP_DIRS and .pyc is not in _GREP_EXTS, and the CI sanitize gate excludes both -
#: but the reach of the split is what C7 greps, not the whole tree, and a future reader
#: should not inherit the stronger claim. Written as one literal it plants its own
#: needle in the tree C7 greps, C7 correctly reports a hit and returns FAIL, and the
#: configured world below then never exercises the PASS branch this test exists to
#: cover. A self-fulfilling fixture is worse than no fixture: it is green for a
#: reason unrelated to the thing under test.
PLACEHOLDER_LITERAL = "203.0.113." + "9"


def _load_check_module():
    """Import harness/check.py by path — it is a script, not a package module."""
    path = HARNESS_DIR / "check.py"
    assert path.is_file(), f"harness/check.py not found at {path} - moved, or this path is stale"
    spec = importlib.util.spec_from_file_location("agentflow_harness_check_meta", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check():
    return _load_check_module()


def _write_config(tmp_path, *, literals, skills_root=None, name="harness.config.json"):
    """A machine-local-shaped config in tmp_path, built from the tracked example.

    `skills_root` is written ABSOLUTE on purpose: the shipped resolution rule makes
    a relative value relative to the config file's own directory, and this file does
    not live in harness/, so "../skills" would silently point at tmp_path's parent.
    """
    assert EXAMPLE_CONFIG.is_file(), f"tracked example config missing at {EXAMPLE_CONFIG}"
    cfg = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    cfg["preport_ip_literals"] = list(literals)
    cfg["skills_root"] = str(skills_root if skills_root is not None else REPO_ROOT / "skills")
    path = tmp_path / name
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


@pytest.fixture(params=["example-config", "configured-config"])
def config_path(request, tmp_path) -> Path:
    """Both worlds the harness ships into, so neither can be green alone.

    "example-config" is what CI and a fresh clone see: `preport_ip_literals` empty,
    C7 SKIPs. "configured-config" is what a developer who followed the documented
    setup sees: needles present, C7 asserts over the real skills tree and PASSes.
    gh#220 was the second world going unmeasured.
    """
    if request.param == "example-config":
        return EXAMPLE_CONFIG
    return _write_config(tmp_path, literals=[PLACEHOLDER_LITERAL])


def _run_self_test(check, tmp_path, config_path, extra_rows=()):
    """Run `--self-test` in a guaranteed records-absent world, against `config_path`.

    RECORDS_DIR/WAKE_LOG are repointed at paths that do not exist, so the check runs
    identically here and on a machine that has recorded scenarios. Since gh#302
    check.py synthesizes that world itself, so this is belt-and-braces for the tests
    that are ABOUT something else; the records axis is pinned deliberately, by the
    tests at the bottom of this file, which hand check.py a populated world instead.
    The config is passed explicitly: since gh#340 the command ignores DEFAULT_CONFIG,
    so repointing it, as gh#220's fix did, would now select nothing.
    """
    registry = check.build_registry() + list(extra_rows)
    orig = (check.RECORDS_DIR, check.WAKE_LOG, check.build_registry)
    check.RECORDS_DIR = tmp_path / "no-records"
    check.WAKE_LOG = tmp_path / "no-stubs" / "schedulewakeup_log.md"
    check.build_registry = lambda: list(registry)
    try:
        return check.run_self_test(config_path)
    finally:
        check.RECORDS_DIR, check.WAKE_LOG, check.build_registry = orig


def _marker_line(out: str) -> str:
    """The step-4b verdict line alone. Step 3 also echoes a synthetic id in its
    `extra=[...]` registry-completeness finding, so a bare substring search over the
    whole transcript would pass for the wrong reason."""
    lines = [ln for ln in out.splitlines() if MARKER in ln]
    assert len(lines) == 1, f"expected exactly one {MARKER} line, got {lines}"
    return lines[0]


def _row(check, aid, status):
    """A synthetic registry row whose verdict is fixed, with no input at all."""
    return check.Assertion(
        id=aid,
        group="C",
        behavioral=True,
        fn=lambda ctx: check.Result(status, "synthetic row - examined nothing"),
    )


def test_the_shipped_registry_is_clean(check, tmp_path, config_path, capsys) -> None:
    """Control. If this were already red, every case below would prove nothing —
    and it is also the live assertion that the table covers today's registry."""
    _run_self_test(check, tmp_path, config_path)
    line = _marker_line(capsys.readouterr().out)
    assert line.startswith("PASS"), line


def test_a_vacuous_pass_is_caught(check, tmp_path, config_path, capsys) -> None:
    """An unregistered assertion that returns PASS having examined nothing."""
    _run_self_test(check, tmp_path, config_path, extra_rows=[_row(check, "ZZ1", check.PASS)])
    line = _marker_line(capsys.readouterr().out)
    assert line.startswith("FAIL"), line
    assert "ZZ1" in line


@pytest.mark.parametrize("status_name", ["FAIL", "SKIP"])
def test_an_honest_verdict_with_no_input_is_not_flagged(
    check, tmp_path, config_path, capsys, status_name
) -> None:
    """Control: only PASS is a claim to have observed something. C4 SKIPs on this
    world, and must not be dragged red by its own repair."""
    status = getattr(check, status_name)
    _run_self_test(check, tmp_path, config_path, extra_rows=[_row(check, "ZZ2", status)])
    line = _marker_line(capsys.readouterr().out)
    assert line.startswith("PASS"), line


def test_a_registered_row_with_an_empty_probe_is_still_caught(
    check, tmp_path, config_path, capsys
) -> None:
    """Registering an id is not enough — C7's whole defect was a needle list that
    had quietly become empty, so an entry whose probe returns nothing is a finding."""
    check.ABSENCE_PASS_EVIDENCE["ZZ3"] = lambda ctx: []
    try:
        _run_self_test(check, tmp_path, config_path, extra_rows=[_row(check, "ZZ3", check.PASS)])
    finally:
        del check.ABSENCE_PASS_EVIDENCE["ZZ3"]
    line = _marker_line(capsys.readouterr().out)
    assert line.startswith("FAIL"), line
    assert "ZZ3" in line


def test_every_table_entry_names_a_wired_assertion(check) -> None:
    """A probe for a retired id is dead weight that reads as coverage."""
    wired = {a.id for a in check.build_registry()}
    stale = sorted(set(check.ABSENCE_PASS_EVIDENCE) - wired)
    assert not stale, f"ABSENCE_PASS_EVIDENCE names assertions that no longer exist: {stale}"


def test_c4_is_absent_from_the_table(check) -> None:
    """It SKIPs on this world now. Keeping an entry for it would re-admit the exact
    PASS this epic removed if the fix were reverted."""
    assert "C4" not in check.ABSENCE_PASS_EVIDENCE


@pytest.mark.parametrize(
    "world, expected_status, expect_probe",
    [("example-config", "SKIP", False), ("configured-config", "PASS", True)],
)
def test_c7s_probe_tracks_c7s_verdict_in_both_worlds(
    check, tmp_path, world, expected_status, expect_probe
) -> None:
    """C7's absence guard, REPLACED by a stronger claim rather than dropped (gh#220).

    Absence only ever guarded the world where C7 does nothing, and said nothing
    about the one where it runs. Each world is named explicitly rather than derived
    from whatever C7 happens to return, so a C7 that quietly stops asserting in the
    configured world is a failure here instead of a tautology.
    """
    probe = check.ABSENCE_PASS_EVIDENCE.get("C7")
    assert probe is not None, "C7 must be registered - absence made it a false green when configured"
    cfg = (
        EXAMPLE_CONFIG
        if world == "example-config"
        else _write_config(tmp_path, literals=[PLACEHOLDER_LITERAL])
    )
    ctx = check.Context(cfg)
    verdict = check.c7_no_ip_literal_in_ported(ctx)
    assert verdict.status == expected_status, f"{world}: {verdict.status} - {verdict.detail}"
    assert bool(probe(ctx)) is expect_probe, f"{world}: probe={probe(ctx)!r}, {verdict.detail}"


@pytest.mark.parametrize("reverted", ["empty-needle-list", "empty-search-space"])
def test_c7s_probe_returns_zero_under_a_revert_of_either_gh139_repair(
    check, tmp_path, reverted
) -> None:
    """The `min` is load-bearing, and this is what makes registering C7 stronger
    than omitting it. C7's predicate is conjunctive — needles to look for AND files
    to look in — so a probe built from either half alone would vouch for the other
    half being empty, which is precisely the pair of zeroes gh#139 was about."""
    if reverted == "empty-needle-list":
        cfg = _write_config(tmp_path, literals=[])
    else:
        empty = tmp_path / "empty-skills"
        empty.mkdir()
        cfg = _write_config(tmp_path, literals=[PLACEHOLDER_LITERAL], skills_root=empty)
    value = check.ABSENCE_PASS_EVIDENCE["C7"](check.Context(cfg))
    assert not value, f"probe vouched for input under a reverted {reverted}: {value}"


def test_the_verdict_does_not_depend_on_a_machine_local_config(check, tmp_path) -> None:
    """gh#220 and gh#340 together, against the SHIPPED command rather than a helper.

    gh#220 was a configured machine reporting C7 as a false green while CI stayed
    green; its fix pinned the config in this file only. gh#340 measured what was
    left: `check.py --self-test` on a machine holding `harness.config.json` still
    graded one more PASS than a fresh clone did. So this calls `run_self_test()` the
    way `main` does - no argument - with DEFAULT_CONFIG absent and then present, and
    compares the WHOLE step-4b line. A status-word comparison, which is what this test
    used to make, was green through gh#340: both worlds said PASS, over different rows.
    """
    fresh_clone = tmp_path / "never-created" / "harness.config.json"
    assert not fresh_clone.exists()
    configured = _write_config(tmp_path, literals=[PLACEHOLDER_LITERAL])
    records = tmp_path / "no-records"
    wake_log = tmp_path / "no-stubs" / "schedulewakeup_log.md"

    outputs = {}
    for label, path in (("fresh-clone", fresh_clone), ("configured", configured)):
        orig = check.DEFAULT_CONFIG
        check.DEFAULT_CONFIG = path
        try:
            outputs[label] = _self_test_output(check, None, records, wake_log)
        finally:
            check.DEFAULT_CONFIG = orig

    lines = {label: _marker_line(out) for label, out in outputs.items()}
    assert lines["fresh-clone"] == lines["configured"], lines
    assert lines["configured"].startswith("PASS"), lines
    # Which config decided is printed, so a reader never has to infer it.
    for label, out in outputs.items():
        assert f"# config: {check.EXAMPLE_CONFIG}" in out.splitlines(), f"{label}: {out[:400]}"


def test_self_test_refuses_a_config_argument(check, tmp_path, capsys) -> None:
    """`check.py --self-test <config>` used to ignore the config silently, which reads
    as that config having been checked. Since gh#340 it cannot be, so it is refused
    as a usage error, before any step runs."""
    configured = _write_config(tmp_path, literals=[PLACEHOLDER_LITERAL])
    rc = check.main(["--self-test", str(configured)])
    captured = capsys.readouterr()
    assert rc == 2, captured
    assert "--self-test takes no config" in captured.err, captured.err
    assert "SELF-TEST" not in captured.out, captured.out


# ---------------------------------------------------------------------------
# The records axis (gh#302)
# ---------------------------------------------------------------------------

#: One parseable wake row, in the shape check.py's `_WAKE_RE` matches. scenario=D,
#: because what matters is that SOME row parses: C4 SKIPs on an empty log and PASSes
#: on any log with rows and no scenario=C row.
WAKE_ROW = (
    "2026-01-01T00:00:00Z | scenario=D | wake_at=2026-01-01T00:05:00Z | "
    "prompt=synthetic row written by the harness test suite\n"
)

#: The ambient worlds a developer machine can be in. `harness/records/` and
#: `harness/stubs/schedulewakeup_log.md` are gitignored, so each of these is a shape a
#: developer machine can be in - bare existence was enough to switch step 4b off - and
#: none of them occurs in CI, which is why nothing caught the skip for as long as it
#: shipped. Only the latter two are OBSERVED states; the empty directory is a shape the
#: removed guard responded to.
RECORDS_WORLDS = ("empty-records-dir", "wake-log-with-rows", "scenarioA-plan-recorded")


def _seed_records_world(tmp_path, world):
    """Build one ambient on-disk records world; return (records_dir, wake_log).

    The two halves of the removed guard's disjunction are seeded SEPARATELY: it read
    `RECORDS_DIR.exists() or WAKE_LOG.exists()`, so a test that only ever varied them
    together could not say which half was load-bearing.

    "empty-records-dir" is the weakest of the three on purpose — the guard tested bare
    existence and never content, so an empty directory was enough to switch step 4b
    off, while changing no assertion's verdict. It pins the condition and nothing else.
    The other two additionally change a verdict, which is what makes them leak
    detectors; see `test_each_seeded_world_would_be_caught_if_the_absent_world_leaked`.
    """
    root = tmp_path / world
    records = root / "records"
    wake_log = root / "stubs" / "schedulewakeup_log.md"
    if world == "empty-records-dir":
        records.mkdir(parents=True)
    elif world == "wake-log-with-rows":
        wake_log.parent.mkdir(parents=True)
        wake_log.write_text(WAKE_ROW, encoding="utf-8")
    elif world == "scenarioA-plan-recorded":
        (records / "scenarioA").mkdir(parents=True)
        shutil.copyfile(
            HARNESS_DIR / "fixtures" / "plan-single-task.md", records / "scenarioA" / "plan.md"
        )
    else:  # pragma: no cover - a typo in the parametrize list, not a runtime path
        raise AssertionError(f"unknown records world {world!r}")
    return records, wake_log


def _self_test_output(check, config_path, records_dir, wake_log, extra_rows=()) -> str:
    """`--self-test` against an EXPLICIT records world, returning what it printed.

    The mirror image of `_run_self_test`: that one hands check.py a world which is
    already absent, so it cannot tell whether check.py would have synthesized one.
    This one hands it whatever `_seed_records_world` built and asserts on what
    check.py does with it. stdout is captured here rather than through `capsys` so a
    module-scoped baseline can be computed once. `config_path=None` is the command's
    own call, which selects the config itself.
    """
    registry = check.build_registry() + list(extra_rows)
    orig = (check.RECORDS_DIR, check.WAKE_LOG, check.build_registry)
    check.RECORDS_DIR = records_dir
    check.WAKE_LOG = wake_log
    check.build_registry = lambda: list(registry)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            check.run_self_test(config_path)
    finally:
        check.RECORDS_DIR, check.WAKE_LOG, check.build_registry = orig
    return buf.getvalue()


#: The config worlds, crossed with RECORDS_WORLDS in one parametrization (gh#340):
#: gh#220 and gh#302 each pinned one axis while holding the other still, so no test
#: saw a configured machine that had also recorded scenarios - which is the ordinary
#: operator machine.
CONFIG_WORLDS = ("example-config", "configured-config")


def _config_for(world, tmp_path) -> Path:
    if world == "example-config":
        return EXAMPLE_CONFIG
    return _write_config(tmp_path, literals=[PLACEHOLDER_LITERAL])


@pytest.fixture(scope="module")
def records_absent_baseline(check, tmp_path_factory) -> dict:
    """The step-4b verdict line per config world with no records: what every records
    world must match. Per config, because the graded count legitimately differs
    between config worlds (C7 SKIPs in one and PASSes in the other) and the comparison
    is of the whole line, not its first word."""
    tmp = tmp_path_factory.mktemp("records-absent")
    lines = {}
    for world in CONFIG_WORLDS:
        line = _marker_line(
            _self_test_output(
                check, _config_for(world, tmp), tmp / "no-records", tmp / "no-stubs" / "wake.md"
            )
        )
        assert line.startswith("PASS"), f"{world}: {line}"
        lines[world] = line
    return lines


@pytest.mark.parametrize("config_world", CONFIG_WORLDS)
@pytest.mark.parametrize("world", RECORDS_WORLDS)
def test_step_4b_grades_identically_whatever_records_are_on_disk(
    check, tmp_path, world, config_world, records_absent_baseline
) -> None:
    """gh#302 itself. The whole line, not just the status word: it names how many
    PASSes were graded, so an equal line says the same rows were vetted and not
    merely that both worlds ended up green. Against the shipped guard this was red in
    all three worlds — the line read `SKIP ... not reproducible on this machine`."""
    records, wake_log = _seed_records_world(tmp_path, world)
    out = _self_test_output(check, _config_for(config_world, tmp_path), records, wake_log)
    line = _marker_line(out)
    assert line == records_absent_baseline[config_world], f"{config_world}/{world}: {line}"
    # 4d's control: the shipped registry does not raise on any seeded world, so the
    # FAIL cases below are about the injected row and nothing else.
    assert _ambient_line(out).startswith("PASS"), f"{config_world}/{world}: {_ambient_line(out)}"


@pytest.mark.parametrize("config_world", CONFIG_WORLDS)
@pytest.mark.parametrize("world", RECORDS_WORLDS)
def test_a_vacuous_pass_is_still_caught_with_records_on_disk(
    check, tmp_path, world, config_world
) -> None:
    """The consequence, stated separately from the verdict line: a machine with
    recordings must still CATCH the defect this whole check exists for. Equal verdicts
    alone would be satisfied by a check that graded nothing anywhere."""
    records, wake_log = _seed_records_world(tmp_path, world)
    out = _self_test_output(
        check,
        _config_for(config_world, tmp_path),
        records,
        wake_log,
        extra_rows=[_row(check, "ZZ4", check.PASS)],
    )
    line = _marker_line(out)
    assert line.startswith("FAIL"), line
    assert "ZZ4" in line


@pytest.mark.parametrize(
    "world, aid",
    [("wake-log-with-rows", "C4"), ("scenarioA-plan-recorded", "A1")],
)
def test_each_seeded_world_would_be_caught_if_the_absent_world_leaked(
    check, tmp_path, world, aid
) -> None:
    """These two fixtures are leak detectors only if they really move a verdict.

    Each seeds a row that (a) returns PASS when the globals genuinely point at the
    seeded world and (b) is deliberately absent from ABSENCE_PASS_EVIDENCE, because a
    records-absent PASS is the exact thing it must not return. So if the synthesized
    absent world ever stopped covering one of the two paths, that row would go PASS
    unregistered and step 4b would report it — which is what makes the equality test
    above an assertion about reading, not a coincidence. Asserted rather than assumed:
    a fixture that changes nothing detects nothing, and would keep passing forever.
    """
    records, wake_log = _seed_records_world(tmp_path, world)
    assert aid not in check.ABSENCE_PASS_EVIDENCE, f"{aid} is registered - no longer a leak detector"
    fn = {a.id: a.fn for a in check.build_registry()}[aid]
    orig = (check.RECORDS_DIR, check.WAKE_LOG)
    check.RECORDS_DIR, check.WAKE_LOG = records, wake_log
    try:
        verdict = fn(check.Context(EXAMPLE_CONFIG))
    finally:
        check.RECORDS_DIR, check.WAKE_LOG = orig
    assert verdict.status == check.PASS, f"{world}/{aid}: {verdict.status} - {verdict.detail}"


# ---------------------------------------------------------------------------
# Crash coverage over the ambient recordings (gh#341)
# ---------------------------------------------------------------------------

AMBIENT_MARKER = "self-test.ambient-no-crash"


def _ambient_line(out: str) -> str:
    """The step-4d line alone, for the same reason `_marker_line` exists."""
    lines = [ln for ln in out.splitlines() if AMBIENT_MARKER in ln]
    assert len(lines) == 1, f"expected exactly one {AMBIENT_MARKER} line, got {lines}"
    return lines[0]


def _raises_only_on_recordings(check, aid):
    """A row that raises exactly when the globals point at a world with recordings.

    That is the shape gh#341 is about - a crash reachable only through a real
    recording - and it is invisible to step 4 by construction, because step 4 runs
    inside the synthesized absent world. Read at call time, so it sees whatever world
    `_records_absent_world` has or has not rebound.
    """

    def fn(ctx):
        if check.RECORDS_DIR.exists() or check.WAKE_LOG.exists():
            raise ValueError("synthetic crash on a recording")
        return check.Result(check.SKIP, "synthetic row - no recordings")

    return check.Assertion(id=aid, group="C", behavioral=True, fn=fn)


@pytest.mark.parametrize("world", RECORDS_WORLDS)
def test_step_4d_reports_a_crash_only_the_recordings_reach(check, tmp_path, world) -> None:
    """gh#341 itself. Step 4 stays green - the crash is not reachable from the absent
    world - and step 4d, the only step that reads the recordings, fails the run."""
    records, wake_log = _seed_records_world(tmp_path, world)
    out = _self_test_output(
        check, EXAMPLE_CONFIG, records, wake_log,
        extra_rows=[_raises_only_on_recordings(check, "ZZ5")],
    )
    no_crash = [ln for ln in out.splitlines() if "self-test.no-crash" in ln]
    assert len(no_crash) == 1 and no_crash[0].startswith("PASS"), no_crash
    line = _ambient_line(out)
    assert line.startswith("FAIL"), line
    assert "ZZ5:ValueError" in line, line
    assert "# SELF-TEST FAILED" in out.splitlines()


def test_step_4d_does_not_grade_verdicts(check, tmp_path) -> None:
    """Control: a FAIL over the recordings is a verdict, and grading this world is what
    step 4b cannot do (gh#302). Only an exception is a 4d finding."""
    records, wake_log = _seed_records_world(tmp_path, "wake-log-with-rows")
    out = _self_test_output(
        check, EXAMPLE_CONFIG, records, wake_log, extra_rows=[_row(check, "ZZ6", check.FAIL)]
    )
    assert _ambient_line(out).startswith("PASS"), _ambient_line(out)


def test_step_4d_skips_with_no_recordings(check, tmp_path) -> None:
    """A fresh clone, and CI, has nothing for 4d to read: it says so rather than
    passing over an input it never had."""
    out = _self_test_output(
        check, EXAMPLE_CONFIG, tmp_path / "no-records", tmp_path / "no-stubs" / "wake.md",
        extra_rows=[_raises_only_on_recordings(check, "ZZ7")],
    )
    assert _ambient_line(out).startswith("SKIP"), _ambient_line(out)
