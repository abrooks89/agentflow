"""check.py must say which tree it tested, and treat a missing recording one way (gh#628).

Two findings:

- `--self-test` printed `parse_plan resolved via agentflow.plan_parser` and no path.
  parse_plan imports whatever `agentflow` is installed, while the registry CLI runs
  from this clone's own `skills/_shared`, so one run could mix two trees silently
  (the gh#24 hazard). It now prints both and WARNs when they disagree.
- A missing recording was FAIL in A1-A9, B1-B9, D1-D3 and E2 but SKIP in C4 and D4,
  so on a fresh clone a missing recording could not be told apart from a regression.
  Every absent-record return now goes through `_unrecorded`, and the run exits 3
  (not 1) when the only thing holding it red is unrecorded scenarios in scope.

The FAIL controls are load-bearing: a checker that SKIPs everything would satisfy the
SKIP cases and hide every recorded regression.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = REPO_ROOT / "harness"
EXAMPLE_CONFIG = HARNESS_DIR / "harness.config.example.json"

#: Every assertion whose only evidence is a live recording. Written out here rather
#: than read from check.py, so the test is an independent statement of the contract.
RECORD_BACKED = (
    [f"A{i}" for i in range(1, 10)]
    + [f"B{i}" for i in range(1, 10)]
    + ["E2", "C4", "D1", "D2", "D3", "D4"]
)


def _load_check_module():
    """Import harness/check.py by path - it is a script, not a package module."""
    path = HARNESS_DIR / "check.py"
    assert path.is_file(), f"harness/check.py not found at {path} - moved, or this path is stale"
    spec = importlib.util.spec_from_file_location("agentflow_harness_check_gh628", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check():
    return _load_check_module()


@pytest.fixture
def ctx(check):
    c = check.Context(EXAMPLE_CONFIG)
    assert c.parse_plan is not None, f"agentflow not importable: {c.import_error}"
    return c


@contextlib.contextmanager
def _world(check, records: Path, wake_log: Path):
    orig = (check.RECORDS_DIR, check.WAKE_LOG)
    check.RECORDS_DIR, check.WAKE_LOG = records, wake_log
    try:
        yield
    finally:
        check.RECORDS_DIR, check.WAKE_LOG = orig


def _by_id(check):
    return {a.id: a for a in check.build_registry()}


# --- the helper ------------------------------------------------------------


def test_unrecorded_helper_returns_a_marked_skip(check) -> None:
    res = check._unrecorded("A", "records/scenarioA/plan.md")
    assert res.status == check.SKIP
    assert res.unrecorded == ("A", "records/scenarioA/plan.md")
    assert "records/scenarioA/plan.md" in res.detail
    assert "run-scenario.md" in res.detail


# --- absent records are SKIP, uniformly --------------------------------------


@pytest.mark.parametrize("aid", RECORD_BACKED)
def test_absent_record_is_an_unrecorded_skip(check, ctx, tmp_path, aid) -> None:
    with _world(check, tmp_path / "no-records", tmp_path / "no-stubs" / "log.md"):
        res = _by_id(check)[aid].fn(ctx)
    assert res.status == check.SKIP, f"{aid} reported a missing recording as {res.status}: {res.detail}"
    assert res.unrecorded is not None, f"{aid} SKIPped without going through _unrecorded: {res.detail}"


def test_no_other_assertion_claims_unrecorded(check, ctx, tmp_path) -> None:
    """The marker drives the exit code, so it must not leak onto fixture-backed rows."""
    with _world(check, tmp_path / "no-records", tmp_path / "no-stubs" / "log.md"):
        marked = {
            a.id for a in check.build_registry() if getattr(a.fn(ctx), "unrecorded", None)
        }
    assert marked == set(RECORD_BACKED)


# --- present-but-wrong records still FAIL -----------------------------------


def test_a_present_unparseable_plan_still_fails(check, ctx, tmp_path) -> None:
    records = tmp_path / "records"
    (records / "scenarioA").mkdir(parents=True)
    (records / "scenarioA" / "plan.md").write_text("---\nnot: [a plan\n---\n", encoding="utf-8")
    with _world(check, records, tmp_path / "log.md"):
        for aid in ("A1", "A2", "A9"):
            res = _by_id(check)[aid].fn(ctx)
            assert res.status == check.FAIL, f"{aid} hid a broken recording: {res.detail}"
            assert res.unrecorded is None


def test_a_present_plan_passes(check, ctx, tmp_path) -> None:
    """Control: the SKIP arm must not swallow a good recording."""
    records = tmp_path / "records"
    (records / "scenarioA").mkdir(parents=True)
    shutil.copyfile(HARNESS_DIR / "fixtures" / "plan-single-task.md", records / "scenarioA" / "plan.md")
    with _world(check, records, tmp_path / "log.md"):
        res = _by_id(check)["A2"].fn(ctx)
    assert res.status == check.PASS, res.detail


def test_b9_fails_when_scenario_b_was_recorded_without_a_wake_row(check, ctx, tmp_path) -> None:
    """B's status board is the proof B was recorded; a missing B wake row is then a
    regression in the recording, not an absent one."""
    records = tmp_path / "records"
    (records / "scenarioB").mkdir(parents=True)
    (records / "scenarioB" / "status.md").write_text("# status\n", encoding="utf-8")
    log = tmp_path / "log.md"
    log.write_text(
        "2026-01-01T00:00:00+00:00 | scenario=A | wake_at=2026-01-01T01:00:00+00:00 | prompt=/x\n",
        encoding="utf-8",
    )
    with _world(check, records, log):
        res = check.b9_one_future_wake(ctx)
    assert res.status == check.FAIL, res.detail
    assert res.unrecorded is None


def test_d3_still_fails_a_second_wake(check, ctx, tmp_path) -> None:
    row = "2026-01-01T00:00:00+00:00 | scenario=D | wake_at=2026-01-01T01:00:00+00:00 | prompt=/lead-dev E-1\n"
    log = tmp_path / "log.md"
    log.write_text(row + row, encoding="utf-8")
    with _world(check, tmp_path / "no-records", log):
        res = check.d3_no_second_wake(ctx)
    assert res.status == check.FAIL, res.detail


# --- exit-code contract ------------------------------------------------------


def _registry_of(check, *rows):
    def _registry():
        return [
            check.Assertion(id=i, group=g, behavioral=True, fn=(lambda ctx, r=r: r))
            for i, g, r in rows
        ]

    return _registry


def _run_suite(check, monkeypatch, only, *rows):
    monkeypatch.setattr(check, "build_registry", _registry_of(check, *rows))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = check.run_suite(EXAMPLE_CONFIG, only)
    return rc, buf.getvalue()


def test_only_unrecorded_exits_3_and_says_so(check, monkeypatch) -> None:
    rc, out = _run_suite(
        check, monkeypatch, None,
        ("A1", "A", check._unrecorded("A", "records/scenarioA/plan.md")),
        ("C1", "C", check.Result(check.PASS, "ok")),
    )
    assert rc == check.EXIT_UNRECORDED == 3
    lines = [ln for ln in out.splitlines() if ln.startswith("# unrecorded:")]
    assert lines and "records/scenarioA/plan.md" in lines[0] and "A1" in lines[0], out


def test_behavioral_fail_keeps_exit_1_over_unrecorded(check, monkeypatch) -> None:
    rc, _ = _run_suite(
        check, monkeypatch, None,
        ("A1", "A", check._unrecorded("A", "records/scenarioA/plan.md")),
        ("C1", "C", check.Result(check.FAIL, "regressed")),
    )
    assert rc == 1


def test_all_green_exits_0(check, monkeypatch) -> None:
    rc, out = _run_suite(check, monkeypatch, None, ("C1", "C", check.Result(check.PASS, "ok")))
    assert rc == 0
    assert "# unrecorded:" not in out


def test_unrecorded_scenario_outside_only_does_not_hold_the_exit(check, monkeypatch) -> None:
    """CI runs `--only C,F,G`: C4's evidence is the wake log that A/B/D write, and
    those scenarios were not selected, so the run must stay 0 (and still say so)."""
    rc, out = _run_suite(
        check, monkeypatch, "C,F,G",
        ("C4", "C", check._unrecorded("A/B/D", "stubs/schedulewakeup_log.md")),
        ("C1", "C", check.Result(check.PASS, "ok")),
    )
    assert rc == 0, out
    assert "# unrecorded:" in out


def test_the_same_c4_skip_holds_a_full_run(check, monkeypatch) -> None:
    rc, _ = _run_suite(
        check, monkeypatch, None,
        ("C4", "C", check._unrecorded("A/B/D", "stubs/schedulewakeup_log.md")),
    )
    assert rc == 3


def test_ci_command_stays_green_on_this_tree(check, capsys) -> None:
    """The exact CI invocation, against the real registry, in a records-absent world."""
    with check._records_absent_world():
        rc = check.run_suite(EXAMPLE_CONFIG, "C,F,G")
    out = capsys.readouterr().out
    assert rc == 0, out


# --- which tree was tested ---------------------------------------------------


def test_tree_lines_name_both_paths(check, ctx, monkeypatch) -> None:
    # Pinned in-clone, so the verdict does not depend on which agentflow the venv holds.
    in_clone = str(check.SCRIPT_DIR.parent / "skills" / "_shared" / "agentflow" / "plan_parser.py")
    monkeypatch.setattr(check, "_plan_parser_file", lambda c: in_clone)
    lines = check._tree_lines(ctx)
    text = "\n".join(lines)
    parser_file = check._plan_parser_file(ctx)
    root, _ = check._registry_cli_root(ctx)
    assert parser_file and parser_file in text
    assert root is not None and str(root) in text
    assert not any(ln.startswith("WARN") for ln in lines), text


def test_a_foreign_plan_parser_warns_naming_both(check, ctx, tmp_path, monkeypatch) -> None:
    foreign = tmp_path / "other-clone" / "skills" / "_shared" / "agentflow" / "plan_parser.py"
    monkeypatch.setattr(check, "_plan_parser_file", lambda c: str(foreign))
    lines = check._tree_lines(ctx)
    warns = [ln for ln in lines if ln.startswith("WARN self-test.tree")]
    assert len(warns) == 1, lines
    root, _ = check._registry_cli_root(ctx)
    assert str(foreign) in warns[0] and str(root) in warns[0]
    assert "pip install -e skills/_shared" in warns[0]



# --- gh#944: C13 under the default skills_root ---------------------------------


def _default_root_ctx(check, tmp_path, monkeypatch):
    """A Context with no skills_root in config or env, so it takes the documented default
    (~/.claude/skills) - here under a temp home that, like a real install, has no _shared."""
    import json

    cfg = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    del cfg["skills_root"]
    path = tmp_path / "harness.config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.delenv("AGENTFLOW_SKILLS_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    ctx = check.Context(path)
    assert ctx.skills_root() == home / ".claude" / "skills", ctx.skills_root()
    return ctx


def test_c13_executes_against_the_clone_under_the_default_skills_root(check, tmp_path, monkeypatch) -> None:
    """It used to SKIP here blaming a 'pre-port layout', with no unrecorded marker, so the
    run exited 0 with the precedence check never executed."""
    ctx = _default_root_ctx(check, tmp_path, monkeypatch)
    res = check.c13_slash_argument_beats_injected_default(ctx)
    assert res.status in (check.PASS, check.FAIL), f"C13 did not execute: {res.status} {res.detail}"
    assert "pre-port" not in res.detail, res.detail
    assert check.ABSENCE_PASS_EVIDENCE["C13"](ctx), "C13's absence probe still keys on skills_root alone"


def test_c13_with_no_package_anywhere_fails_naming_the_cause(check, tmp_path, monkeypatch) -> None:
    ctx = _default_root_ctx(check, tmp_path, monkeypatch)
    monkeypatch.setattr(check, "SCRIPT_DIR", tmp_path / "not-a-clone" / "harness")
    res = check.c13_slash_argument_beats_injected_default(ctx)
    assert res.status == check.FAIL, f"no package anywhere must not pass clean: {res.status} {res.detail}"
    assert "skills_root" in res.detail and "_shared" in res.detail, res.detail
    assert str(ctx.skills_root() / "_shared") in res.detail, res.detail
    assert "pre-port" not in res.detail, res.detail

# --- the self-test grades the records-absent case -----------------------------


def _no_ambient_records(check, monkeypatch, tmp_path) -> None:
    """Point RECORDS_DIR/WAKE_LOG at paths that do not exist.

    Step 4d (gh#341) reads whatever recordings the machine holds, deliberately, so a
    self-test run left ambient here would pass or fail by machine: a recording 4d
    trips over is a finding for `--self-test`, not for these tests.
    """
    monkeypatch.setattr(check, "RECORDS_DIR", tmp_path / "no-records")
    monkeypatch.setattr(check, "WAKE_LOG", tmp_path / "no-stubs" / "schedulewakeup_log.md")


def _self_test_out(check, monkeypatch, tmp_path, registry=None) -> tuple[int, str]:
    monkeypatch.setattr(check, "run_auditor_scenario", lambda: 0)
    _no_ambient_records(check, monkeypatch, tmp_path)
    if registry is not None:
        monkeypatch.setattr(check, "build_registry", lambda: list(registry))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = check.run_self_test()
    return rc, buf.getvalue()


def _unrecorded_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if "self-test.unrecorded" in ln]
    assert len(lines) == 1, out
    return lines[0]


def test_self_test_passes_the_unrecorded_check_and_prints_the_tree(check, monkeypatch, tmp_path) -> None:
    rc, out = _self_test_out(check, monkeypatch, tmp_path)
    assert rc == 0, out
    assert _unrecorded_line(out).startswith("PASS"), out
    assert "plan_parser" in out and "registry CLI root" in out


def test_self_test_catches_a_record_backed_row_that_fails_on_absence(check, monkeypatch, tmp_path) -> None:
    registry = check.build_registry()
    for a in registry:
        if a.id == "A3":
            a.fn = lambda ctx: check.Result(check.FAIL, "no records/scenarioA plan file recorded")
    rc, out = _self_test_out(check, monkeypatch, tmp_path, registry)
    line = _unrecorded_line(out)
    assert line.startswith("FAIL") and "A3" in line, line
    assert rc == 1


# --- a stale resolver_cmd key (gh#627) ----------------------------------------
#
# gh#627 retired `resolver_cmd`: it only ever chose the pre-port import. A local
# config that still carries it must load, WARN, and change no verdict; and the key
# must not creep back into REQUIRED_CONFIG_KEYS, which would turn every config
# regenerated from the example (it no longer ships the key) red at self-test step 1.


def _stale_config(tmp_path, value) -> Path:
    import json

    cfg = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    cfg["resolver_cmd"] = value
    # The example's relative "../skills" resolves against the config's own dir.
    cfg["skills_root"] = str(REPO_ROOT / "skills")
    path = tmp_path / "harness.config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "value, advice",
    [("python -m agentflow.config", "Delete the key"), (None, "regenerate from harness.config.example.json")],
)
def test_run_suite_warns_on_a_stale_resolver_cmd_and_fails_nothing(check, tmp_path, capsys, value, advice) -> None:
    config = _stale_config(tmp_path, value)
    with check._records_absent_world():
        rc = check.run_suite(config, "C,F,G")
    out = capsys.readouterr().out
    warns = [ln for ln in out.splitlines() if ln.startswith("# WARN config:")]
    assert len(warns) == 1 and "'resolver_cmd'" in warns[0] and advice in warns[0], out
    assert rc == 0, out
    assert not any(ln.startswith("FAIL") for ln in out.splitlines()), out


def test_self_test_warns_on_a_stale_resolver_cmd_and_stays_ok(check, monkeypatch, tmp_path) -> None:
    config = _stale_config(tmp_path, "python -m agentflow.config")
    monkeypatch.setattr(check, "run_auditor_scenario", lambda: 0)
    _no_ambient_records(check, monkeypatch, tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        # Passed explicitly: the command grades the tracked example config and no
        # longer reads a local one (gh#340), so a stale key there surfaces through
        # `check.py <config>`, pinned above; this pins the self-test's own WARN path.
        rc = check.run_self_test(config)
    out = buf.getvalue()
    assert any(ln.startswith("# WARN config:") and "'resolver_cmd'" in ln for ln in out.splitlines()), out
    assert rc == 0, out


def test_a_config_without_resolver_cmd_passes_self_test(check, monkeypatch, tmp_path) -> None:
    import json

    assert "resolver_cmd" not in json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8")), (
        "the example config ships resolver_cmd again - gh#627 retired it"
    )
    rc, out = _self_test_out(check, monkeypatch, tmp_path)
    assert "PASS self-test.config" in out, out
    assert "# WARN config:" not in out, out
    assert rc == 0, out


# --- review round (gh#628 follow-ups) ---------------------------------------

_ROW = "2026-01-01T00:00:00+00:00 | scenario={s} | wake_at=2026-01-01T01:00:00+00:00 | prompt=/x\n"


@pytest.mark.parametrize("fn", ["d1_continuation_reinvokes_builder", "d2_verbatim_baseline", "d3_no_second_wake"])
def test_d1_d3_fail_when_scenario_d_was_recorded_without_a_wake_row(check, ctx, tmp_path, fn) -> None:
    """registry-after.md is written by the consume-first run of the base D procedure, so
    D WAS recorded; a missing D wake row is then a regression, as B9 treats B."""
    records = tmp_path / "records"
    (records / "scenarioD").mkdir(parents=True)
    (records / "scenarioD" / "registry-after.md").write_text("# registry\n", encoding="utf-8")
    log = tmp_path / "log.md"
    log.write_text(_ROW.format(s="B"), encoding="utf-8")
    with _world(check, records, log):
        res = getattr(check, fn)(ctx)
    assert res.status == check.FAIL, f"{fn} hid a half-recorded D: {res.detail}"
    assert res.unrecorded is None


def test_c4_is_owned_by_b_and_d_only(check, ctx, tmp_path) -> None:
    """Scenario A schedules no wake (run-scenario.md), so it cannot supply C4's log."""
    with _world(check, tmp_path / "no-records", tmp_path / "no-stubs" / "log.md"):
        res = check.c4_no_wake_scheduled(ctx)
    assert res.unrecorded is not None and res.unrecorded[0] == "B/D", res
    assert "Scenarios A" not in res.detail and "A/B" not in res.detail, res.detail


def test_only_a_c_with_a_recorded_exits_0(check, tmp_path, capsys) -> None:
    records = tmp_path / "records"
    (records / "scenarioA").mkdir(parents=True)
    shutil.copyfile(HARNESS_DIR / "fixtures" / "plan-single-task.md", records / "scenarioA" / "plan.md")
    with _world(check, records, tmp_path / "no-stubs" / "log.md"):
        rc = check.run_suite(EXAMPLE_CONFIG, "A,C")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "# EXIT 3" not in out


def test_exit_line_names_only_selected_scenarios(check, monkeypatch) -> None:
    rc, out = _run_suite(
        check, monkeypatch, "B,C",
        ("C4", "C", check._unrecorded("B/D", "harness/stubs/schedulewakeup_log.md")),
    )
    assert rc == 3
    exit_line = [ln for ln in out.splitlines() if ln.startswith("# EXIT 3")]
    assert len(exit_line) == 1, out
    assert "scenario(s) B are unrecorded" in exit_line[0], exit_line[0]


def _main(check, argv, capsys):
    try:
        rc = check.main(argv)
    except SystemExit as exc:
        rc = exc.code
    return rc, capsys.readouterr()


def test_unknown_only_group_is_a_usage_error_exit_2(check, capsys) -> None:
    rc, cap = _main(check, [str(EXAMPLE_CONFIG), "--only", "X"], capsys)
    assert rc == 2, (rc, cap)
    assert "--only" in cap.err and "Traceback" not in cap.err


def test_malformed_config_json_exits_2_with_a_message(check, tmp_path, capsys) -> None:
    bad = tmp_path / "harness.config.json"
    bad.write_text("{not json", encoding="utf-8")
    rc, cap = _main(check, [str(bad)], capsys)
    assert rc == 2, (rc, cap)
    assert "harness.config.json" in cap.err and "Traceback" not in cap.err


@pytest.mark.parametrize("key", ["orchestrator_skill", "builder_entry"])
def test_a_config_missing_a_required_key_exits_2_naming_it(check, tmp_path, capsys, key) -> None:
    """gh#943: a missing key used to crash C6 with KeyError and exit 1 - a config typo
    reading as a behavioral regression."""
    import json

    cfg = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    del cfg[key]
    cfg["skills_root"] = str(REPO_ROOT / "skills")
    bad = tmp_path / "harness.config.json"
    bad.write_text(json.dumps(cfg), encoding="utf-8")
    rc, cap = _main(check, [str(bad), "--only", "C,F,G"], capsys)
    assert rc == 2, (rc, cap)
    assert key in cap.err and "missing required key" in cap.err, cap.err
    assert "Traceback" not in cap.err


@pytest.mark.parametrize("body, kind", [("[]", "list"), ('"x"', "str")])
def test_a_non_object_config_exits_2_naming_its_type(check, tmp_path, capsys, body, kind) -> None:
    bad = tmp_path / "harness.config.json"
    bad.write_text(body, encoding="utf-8")
    rc, cap = _main(check, [str(bad)], capsys)
    assert rc == 2, (rc, cap)
    assert "must be a JSON object" in cap.err and kind in cap.err, cap.err
    assert "Traceback" not in cap.err


def test_the_example_config_passes_the_shape_check(check) -> None:
    """Control: the refusal keys on shape, not on every config."""
    check._check_config_shape(EXAMPLE_CONFIG)


@pytest.mark.parametrize(
    "key, value, expected",
    [
        ("status_roots", 5, "list of strings"),
        # A str is iterable: it used to pass C3 by iterating characters.
        ("status_roots", "/path/a", "list of strings"),
        ("status_roots", ["/path/a", 5], "list of strings"),
        ("builder_entry", ["/lead-dev"], "string"),
        ("orchestrator_skill", 1, "string"),
    ],
)
def test_a_wrong_typed_required_key_exits_2_naming_key_and_type(check, tmp_path, capsys, key, value, expected) -> None:
    """gh#989: `"status_roots": 5` crashed C3 with TypeError and exit 1 - a config typo
    reading as a behavioral regression, the gh#943 shape for a wrong type."""
    import json

    cfg = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    cfg[key] = value
    cfg["skills_root"] = str(REPO_ROOT / "skills")
    bad = tmp_path / "harness.config.json"
    bad.write_text(json.dumps(cfg), encoding="utf-8")
    rc, cap = _main(check, [str(bad), "--only", "C,F,G"], capsys)
    assert rc == 2, (rc, cap)
    assert key in cap.err and expected in cap.err and "wrong type" in cap.err, cap.err
    assert "Traceback" not in cap.err


def test_self_test_fails_a_wrong_typed_required_key(check, monkeypatch, tmp_path) -> None:
    """The self-test's config step uses the same type check as `check.py <config>`."""
    import json

    cfg = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    cfg["status_roots"] = 5
    cfg["skills_root"] = str(REPO_ROOT / "skills")
    bad = tmp_path / "harness.config.json"
    bad.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(check, "run_auditor_scenario", lambda: 0)
    _no_ambient_records(check, monkeypatch, tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = check.run_self_test(bad)
    out = buf.getvalue()
    lines = [ln for ln in out.splitlines() if "self-test.config" in ln]
    assert lines and lines[0].startswith("FAIL") and "status_roots" in lines[0], out
    assert "list of strings" in lines[0], out
    assert rc == 1, out


def _seed_parseable_b_status(records: Path) -> None:
    from agentflow import status_writer as sw

    (records / "scenarioB").mkdir(parents=True)
    sw.write_status(
        records / "scenarioB" / "status.md",
        sw.StatusState(
            epic="toy",
            plan="plan-single-task.md",
            started="2026-01-01T00:00:00+00:00",
            status="in_progress",
            current_phase="phase-1",
        ),
    )


@pytest.mark.parametrize("aid", ["B1", "B3", "B4", "B8"])
def test_b_status_without_wave_evidence_fails(check, ctx, tmp_path, aid) -> None:
    records = tmp_path / "records"
    _seed_parseable_b_status(records)
    with _world(check, records, tmp_path / "no-stubs" / "log.md"):
        res = _by_id(check)[aid].fn(ctx)
    assert res.status == check.FAIL, f"{aid} hid a half-recorded B: {res.detail}"
    assert res.unrecorded is None


def test_self_test_fails_when_a_record_backed_id_is_not_wired(check, monkeypatch, tmp_path) -> None:
    """A rename must not silently shrink the set step 4c grades."""
    registry = [a for a in check.build_registry() if a.id != "D2"]
    rc, out = _self_test_out(check, monkeypatch, tmp_path, registry)
    line = _unrecorded_line(out)
    assert line.startswith("FAIL") and "D2" in line, line
    assert rc == 1
