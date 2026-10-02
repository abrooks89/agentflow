"""C3 and C4 must OBSERVE something before they report clean (gh#139).

Both assertions state a negative — "the failing parse created no status board",
"no scenario=C wake was scheduled" — and both used to derive that negative from
a collection that was empty because its SOURCE was absent, not because a search
came up clean:

- C3 globbed `harness/records/scenarioC/**/status.md`. `records/` is gitignored
  (`.gitignore:2`) and `harness/README.md` instructs that Scenario C records
  nothing there on any machine, so the glob returned `[]` on every run. The
  parse C3 is about happens IN-PROCESS on a tracked fixture, so a board written
  by a warn-and-continue parser would land beside that fixture and never under
  `records/` at all — C3 was watching a place the fixture path cannot reach.
- C4 read `harness/stubs/schedulewakeup_log.md`, also gitignored, through
  `_parse_wake_log`, which returns `[]` for a missing file. Absence of the log
  is not absence of the wake.

Neither could fail on the CI path (`ci.yml`, `--only C,F,G`), which is the only
path they run on for anyone but the one machine that records scenarios. This
module gates the repair: C3 now diffs the tree the parse can write to and C4
reports SKIP until a recording has populated the log, and each way of breaking
that goes red here. The PASS controls are load-bearing — a C3 or C4 that simply
refuses to pass would satisfy the red cases and prove nothing.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from datetime import timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = REPO_ROOT / "harness"
BAD_PLAN = HARNESS_DIR / "fixtures" / "plan-bad-task-type.md"
FIXTURE_SLUG = "toy-calc-bad-task-type"


def _load_check_module():
    """Import harness/check.py by path — it is a script, not a package module."""
    path = HARNESS_DIR / "check.py"
    # FAIL, never skip: if the harness moves, this gate must break loudly rather
    # than quietly stop covering the checker.
    assert path.is_file(), f"harness/check.py not found at {path} - moved, or this path is stale"
    spec = importlib.util.spec_from_file_location("agentflow_harness_check_c", path)
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: check.py defines dataclasses, and @dataclass resolves
    # its own module out of sys.modules at class-creation time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check():
    return _load_check_module()


def _ctx(check, *, parse_plan=None, config=None, import_error=None):
    """A Context stand-in. `wake_rows` is borrowed from the real Context rather
    than reimplemented, so C4's read path here is the shipped one."""

    class Ctx:
        pass

    Ctx.wake_rows = check.Context.wake_rows
    ctx = Ctx()
    ctx.parse_plan = parse_plan
    ctx.config = config if config is not None else {}
    ctx.import_error = import_error
    return ctx


def _real_parse_plan():
    import agentflow.plan_parser as pp

    return pp.parse_plan


def _run_c3(check, tmp_path, *, parse_plan, config=None, seed_fixture=True):
    """Point FIXTURES_DIR/RECORDS_DIR at a temp tree and run C3."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (tmp_path / "records").mkdir(parents=True, exist_ok=True)
    if seed_fixture:
        # Copy the SHIPPED fixture, so the slug C3 resolves is the real one.
        shutil.copy2(BAD_PLAN, fixtures / BAD_PLAN.name)
    orig_f, orig_r = check.FIXTURES_DIR, check.RECORDS_DIR
    check.FIXTURES_DIR, check.RECORDS_DIR = fixtures, tmp_path / "records"
    try:
        return check.c3_no_status_created(_ctx(check, parse_plan=parse_plan, config=config))
    finally:
        check.FIXTURES_DIR, check.RECORDS_DIR = orig_f, orig_r


def _run_c4(check, tmp_path, log_text):
    """Point WAKE_LOG at a temp file (or a nonexistent path) and run C4."""
    log = tmp_path / "stubs" / "schedulewakeup_log.md"
    if log_text is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(log_text, encoding="utf-8")
    orig = check.WAKE_LOG
    check.WAKE_LOG = log
    try:
        return check.c4_no_wake_scheduled(_ctx(check))
    finally:
        check.WAKE_LOG = orig


_WAKE_ROW = "2026-01-01T00:00:00+00:00 | scenario={s} | wake_at=2026-01-01T01:00:00+00:00 | prompt=p\n"


# --- the shipped fixture still has the shape C3 resolves -------------------


def test_bad_plan_fixture_still_declares_the_slug_c3_reads() -> None:
    """If the fixture drifts, every C3 case below silently tests nothing."""
    assert BAD_PLAN.is_file(), f"{BAD_PLAN} missing - C3 watches what parsing this file creates"
    assert f"slug: {FIXTURE_SLUG}" in BAD_PLAN.read_text(encoding="utf-8")


# --- C3: red cases ---------------------------------------------------------


def test_c3_fails_when_the_parse_creates_a_status_board(check, tmp_path) -> None:
    """The case C3 exists for: a parser that hard-fails but leaves a board behind."""

    def writing_parser(path):
        board = Path(path).parent / FIXTURE_SLUG / "status.md"
        board.parent.mkdir(parents=True, exist_ok=True)
        board.write_text("# board\n", encoding="utf-8")
        raise ValueError("task 1.type must be 'standard' or 'tdd', got 'scaffold'")

    result = _run_c3(check, tmp_path, parse_plan=writing_parser)
    assert result.status == check.FAIL, f"C3 missed a board on the fixture path: {result.detail}"
    assert "status.md" in result.detail


def test_c3_fails_when_a_board_exists_for_the_slug_under_status_roots(check, tmp_path) -> None:
    root = tmp_path / "roots"
    (root / FIXTURE_SLUG).mkdir(parents=True)
    (root / FIXTURE_SLUG / "status.md").write_text("# board\n", encoding="utf-8")
    result = _run_c3(
        check,
        tmp_path,
        parse_plan=_real_parse_plan(),
        config={"status_roots": [str(root)]},
    )
    assert result.status == check.FAIL, f"C3 ignored the configured status roots: {result.detail}"
    assert FIXTURE_SLUG in result.detail


def test_c3_fails_when_the_tracked_fixture_is_absent(check, tmp_path) -> None:
    """A missing fixture is a broken harness, and must be loud rather than clean."""
    result = _run_c3(check, tmp_path, parse_plan=_real_parse_plan(), seed_fixture=False)
    assert result.status == check.FAIL, f"C3 passed with nothing to parse: {result.detail}"


def test_c3_skips_when_the_parser_is_unimportable(check, tmp_path) -> None:
    result = _run_c3(check, tmp_path, parse_plan=None)
    assert result.status == check.SKIP, f"C3 claimed clean with no parse: {result.detail}"
    assert "pip install -e skills/_shared" in result.detail


# --- C3: control -----------------------------------------------------------


def test_c3_passes_the_shipped_fixture_through_the_real_parser(check, tmp_path) -> None:
    """Without this the red cases are satisfied by a C3 that never passes."""
    result = _run_c3(
        check,
        tmp_path,
        parse_plan=_real_parse_plan(),
        config={"status_roots": [str(tmp_path / "roots")]},
    )
    assert result.status == check.PASS, f"C3 rejected a clean parse: {result.detail}"
    assert "watched" in result.detail, "PASS must name what it examined"


# --- C4 --------------------------------------------------------------------


def test_c4_skips_when_no_wake_log_exists(check, tmp_path) -> None:
    result = _run_c4(check, tmp_path, None)
    assert result.status == check.SKIP, f"C4 read an absent log as proof: {result.detail}"
    assert "schedulewakeup_log.md" in result.detail
    assert "run-scenario.md" in result.detail


def test_c4_skips_when_the_wake_log_is_empty(check, tmp_path) -> None:
    result = _run_c4(check, tmp_path, "# no rows yet\n")
    assert result.status == check.SKIP, f"C4 read an empty log as proof: {result.detail}"


def test_c4_passes_a_populated_log_with_no_C_row(check, tmp_path) -> None:
    """Control: a live recording that scheduled A and B wakes and no C wake."""
    result = _run_c4(check, tmp_path, _WAKE_ROW.format(s="A") + _WAKE_ROW.format(s="B"))
    assert result.status == check.PASS, f"C4 rejected a clean recording: {result.detail}"
    assert "2 wake row(s)" in result.detail


def test_c4_fails_a_logged_C_row(check, tmp_path) -> None:
    """Control: the violation still goes red, from the same entry point."""
    result = _run_c4(check, tmp_path, _WAKE_ROW.format(s="A") + _WAKE_ROW.format(s="C"))
    assert result.status == check.FAIL, f"C4 accepted a scenario=C wake: {result.detail}"


def test_c4_skip_does_not_soften_a_recorded_violation(check, tmp_path) -> None:
    """A later change that flattens C4 to SKIP would erase the violation quietly."""
    absent = _run_c4(check, tmp_path / "a", None)
    violating = _run_c4(check, tmp_path / "b", _WAKE_ROW.format(s="C"))
    assert absent.status != violating.status
    assert violating.status == check.FAIL


# --- wiring ----------------------------------------------------------------


def test_c3_c4_are_wired_behavioral_in_the_shipped_registry(check) -> None:
    """A flip to structural would turn an observed violation into a warning."""
    rows = {a.id: a for a in check.build_registry()}
    for aid in ("C3", "C4"):
        assert aid in rows, f"{aid} is no longer wired"
        assert rows[aid].behavioral is True, f"{aid} demoted to structural"


# --- C7: a grep that greps for nothing ------------------------------------
#
# Same defect, third instance. C7 sums `_grep_count` over
# `config["preport_ip_literals"]`, and the tracked example config - the one CI
# runs - ships that key as `[]`. A sum over an empty list is 0, so the PASS was
# unconditional. `_grep_count` also returns 0 for a root that does not exist,
# which turns a mistyped `skills_root` into a clean bill of health.
#
# The needle below is 203.0.113.7 (RFC 5737 TEST-NET-3), never a real infra
# literal: the repo's sanitize gate greps the whole tree and would fail the
# build on one of those, in a test fixture the same as in shipped code.

_SYNTHETIC_NEEDLE = "203.0.113.7"


def _run_c7(check, *, literals, root):
    config = {
        "skills_root": str(root),
        "preport_ip_literals": literals,
        "orchestrator_skill": "orchestrator",
        "solution_skill": "solution",
    }
    ctx = _ctx(check, config=config)
    ctx.skills_root = lambda: Path(config["skills_root"])
    return check.c7_no_ip_literal_in_ported(ctx)


def test_c7_skips_when_no_literals_are_configured(check, tmp_path) -> None:
    """The CI config's own shape: an empty needle list examines nothing."""
    root = tmp_path / "skills"
    root.mkdir()
    result = _run_c7(check, literals=[], root=root)
    assert result.status == check.SKIP, f"C7 passed an empty grep: {result.detail}"
    assert "preport_ip_literals" in result.detail


def test_c7_fails_when_the_skills_root_does_not_exist(check, tmp_path) -> None:
    """Needles configured, nowhere to look: loud, not clean."""
    result = _run_c7(
        check, literals=[_SYNTHETIC_NEEDLE], root=tmp_path / "not-a-real-root"
    )
    assert result.status == check.FAIL, f"C7 passed an unexamined root: {result.detail}"
    assert "skills_root does not exist" in result.detail


def test_c7_fails_a_planted_literal(check, tmp_path) -> None:
    """Control: with a real needle in a real tree, C7 still catches it."""
    root = tmp_path / "skills"
    root.mkdir()
    (root / "probe.md").write_text(f"host {_SYNTHETIC_NEEDLE}\n", encoding="utf-8")
    result = _run_c7(check, literals=[_SYNTHETIC_NEEDLE], root=root)
    assert result.status == check.FAIL, f"C7 missed a planted literal: {result.detail}"


def test_c7_passes_a_clean_tree_with_needles_configured(check, tmp_path) -> None:
    """Control: the one shape where PASS is a real claim."""
    root = tmp_path / "skills"
    root.mkdir()
    (root / "probe.md").write_text("nothing to see\n", encoding="utf-8")
    result = _run_c7(check, literals=[_SYNTHETIC_NEEDLE], root=root)
    assert result.status == check.PASS, f"C7 rejected a clean tree: {result.detail}"
    assert "1 configured literal(s)" in result.detail, "PASS must name what it examined"


# --- C7: a grep with a needle but no haystack ------------------------------
#
# The first repair guarded on `root.exists()`, which moved the defect one step
# in rather than closing it: an existing `skills_root` holding no file the grep
# reads still produced `sum(...) == 0` and a PASS naming the root, over a search
# space of zero files. `_grep_space` already existed for the meta-check and
# reports exactly the number of files `_grep_count` WOULD read; C7 now gates on
# it and prints it, so the PASS states what it examined.


def test_c7_fails_when_the_skills_root_holds_no_greppable_file(check, tmp_path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    assert check._grep_space(root) == 0, "the empty-haystack premise stopped holding"
    result = _run_c7(check, literals=[_SYNTHETIC_NEEDLE], root=root)
    assert result.status == check.FAIL, f"C7 passed over an empty search space: {result.detail}"
    assert "no file this grep reads" in result.detail


def test_c7_fails_a_root_holding_only_unreadable_extensions(check, tmp_path) -> None:
    """The same hole reached by a populated directory. A tree of files the grep
    skips is a zero search space too, and must not read as a clean tree."""
    root = tmp_path / "skills"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "image.png").write_bytes(b"\x89PNG")
    (root / "archive.tar.gz").write_bytes(b"\x1f\x8b")
    result = _run_c7(check, literals=[_SYNTHETIC_NEEDLE], root=root)
    assert result.status == check.FAIL, f"C7 passed over unread files: {result.detail}"


def test_c7_pass_states_the_size_of_the_search_space(check, tmp_path) -> None:
    """A PASS that names only the root cannot be told apart from one over nothing."""
    root = tmp_path / "skills"
    root.mkdir()
    for name in ("a.md", "b.py", "c.json"):
        (root / name).write_text("clean\n", encoding="utf-8")
    result = _run_c7(check, literals=[_SYNTHETIC_NEEDLE], root=root)
    assert result.status == check.PASS, f"C7 rejected a clean populated tree: {result.detail}"
    assert "3 file(s) read" in result.detail, f"PASS does not state its coverage: {result.detail}"


def test_c7_still_catches_a_literal_in_a_populated_tree(check, tmp_path) -> None:
    """Control: the space guard must not become the only thing C7 checks."""
    root = tmp_path / "skills"
    root.mkdir()
    (root / "clean.md").write_text("nothing\n", encoding="utf-8")
    (root / "dirty.md").write_text(f"host {_SYNTHETIC_NEEDLE}\n", encoding="utf-8")
    result = _run_c7(check, literals=[_SYNTHETIC_NEEDLE], root=root)
    assert result.status == check.FAIL, f"C7 missed a planted literal: {result.detail}"
    assert "surviving" in result.detail


# --- C6 and C9: the same grep with the same hole ---------------------------
#
# C7's search-space guard was written as a repair for C7. It is a rule, and the
# other two `_grep_count` assertions sat one and two functions away carrying the
# identical defect: C9 guarded `root.exists()` and stopped exactly where C7's
# first (insufficient) repair stopped, and C6 had no guard at all - both ported
# skill dirs missing summed to zero hits and read as a clean port.
#
# The harness already knew: `_grep_space` is the ABSENCE_PASS_EVIDENCE probe for
# both, so `--self-test` step 4b named them false-green in the same run in which
# they returned PASS. That meta-check is not cover - it self-skips on any machine
# that has recorded a scenario, and `--only C,F,G` never invokes it - so the
# predicate belongs in the assertions, which is where it now is.
#
# C6 is guarded PER ROOT rather than on the sum because its claim is conjunctive.
# The sum case below is the one a total-space guard would let through.


def _example_skill_names():
    """The skill names C6 resolves, read from the shipped example config.

    Typed as literals these are two bare bucket words, and the first-party path
    gate in `test_skill_call_sites.py` reads a path-join onto either of them as a
    pre-migration data-root path - correctly, since it cannot tell a skill
    directory from a bucket by the name alone. Reading them here removes the
    literal and makes these tests track the config CI actually runs.
    """
    import json

    cfg = json.loads((HARNESS_DIR / "harness.config.example.json").read_text(encoding="utf-8"))
    return cfg["orchestrator_skill"], cfg["solution_skill"]


_ORCH_SKILL, _SOL_SKILL = _example_skill_names()


def _c6_ctx(check, root, *, orch=_ORCH_SKILL, sol=_SOL_SKILL):
    """A Context stand-in for C6, borrowing the real skill-dir resolution."""
    ctx = _ctx(
        check,
        config={
            "skills_root": str(root),
            "orchestrator_skill": orch,
            "solution_skill": sol,
        },
    )
    ctx.config_path = Path(root)
    # Bound rather than reimplemented, so C6's read path here is the shipped one.
    ctx.skills_root = check.Context.skills_root.__get__(ctx)
    ctx.orchestrator_dir = check.Context.orchestrator_dir.__get__(ctx)
    ctx.solution_dir = check.Context.solution_dir.__get__(ctx)
    return ctx


def _seed_skill(root: Path, name: str, *, text="clean\n") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d


def test_c6_fails_when_a_ported_skill_dir_does_not_exist(check, tmp_path) -> None:
    """No existence guard at all was C6's starting state: both dirs absent read
    as `0 + 0` hits and PASSed as a clean port."""
    root = tmp_path / "skills"
    root.mkdir()
    result = check.c6_no_syspath_in_ported(_c6_ctx(check, root))
    assert result.status == check.FAIL, f"C6 passed two absent dirs: {result.detail}"
    assert "do not exist" in result.detail
    assert _ORCH_SKILL in result.detail and _SOL_SKILL in result.detail


def test_c6_fails_when_a_ported_skill_dir_holds_no_greppable_file(check, tmp_path) -> None:
    """Present but empty is the same zero, one step in - C7's second lesson."""
    root = tmp_path / "skills"
    (root / _ORCH_SKILL).mkdir(parents=True)
    (root / _SOL_SKILL).mkdir(parents=True)
    result = check.c6_no_syspath_in_ported(_c6_ctx(check, root))
    assert result.status == check.FAIL, f"C6 passed an empty search space: {result.detail}"
    assert "no file this grep reads" in result.detail


def test_c6_fails_when_only_one_ported_root_is_readable(check, tmp_path) -> None:
    """The case a total-space guard would miss, and the reason the guard is per
    root: C6's claim covers BOTH skills, so a populated orchestrator tree must
    not vouch for an empty one whose tree the grep never reads."""
    root = tmp_path / "skills"
    _seed_skill(root, _ORCH_SKILL)
    (root / _SOL_SKILL).mkdir(parents=True)
    result = check.c6_no_syspath_in_ported(_c6_ctx(check, root))
    assert result.status == check.FAIL, f"one populated root vouched for an empty one: {result.detail}"
    assert repr([_SOL_SKILL]) in result.detail, (
        f"the FAIL does not name the unread root: {result.detail}"
    )


def test_c6_pass_states_the_size_of_what_it_examined(check, tmp_path) -> None:
    """The old PASS - `zero sys.path.insert in ported skills` - stated no
    coverage at all: no file count, no root."""
    root = tmp_path / "skills"
    _seed_skill(root, _ORCH_SKILL)
    _seed_skill(root, _SOL_SKILL)
    result = check.c6_no_syspath_in_ported(_c6_ctx(check, root))
    assert result.status == check.PASS, f"C6 rejected a clean port: {result.detail}"
    assert "2 file(s) read" in result.detail, f"PASS does not state its coverage: {result.detail}"
    assert f"{_ORCH_SKILL}=1" in result.detail and f"{_SOL_SKILL}=1" in result.detail


def test_c6_still_catches_a_surviving_syspath_insert(check, tmp_path) -> None:
    """Control: the space guard must not become the only thing C6 checks."""
    root = tmp_path / "skills"
    _seed_skill(root, _ORCH_SKILL)
    _seed_skill(root, _SOL_SKILL, text="sys.path.insert(0, str(here))\n")
    result = check.c6_no_syspath_in_ported(_c6_ctx(check, root))
    assert result.status == check.FAIL, f"C6 missed a planted insert: {result.detail}"
    assert "surviving" in result.detail


def test_a_stale_resolver_cmd_null_no_longer_selects_a_pre_port_skip(check, tmp_path) -> None:
    """gh#627 replaced the pre-port SKIP control that stood here.

    `resolver_cmd: null` used to select a `lib.plan_parser` import from a skill dir
    that no longer exists and SKIP C5-C8 and E1 as "pre-port". The port is done, so
    that branch is gone. A machine-local config that still carries the key must
    neither crash nor keep switching anything: the Context loads, parse_plan comes
    from the installed package, the key is named in a WARN, and C6 ASSERTS - here
    over two absent ported dirs, which is a FAIL rather than the old SKIP.
    """
    import json

    root = tmp_path / "skills"
    root.mkdir()
    config_path = tmp_path / "harness.config.json"
    config_path.write_text(
        json.dumps({"resolver_cmd": None, "skills_root": str(root),
                    "orchestrator_skill": _ORCH_SKILL, "solution_skill": _SOL_SKILL}),
        encoding="utf-8",
    )
    ctx = check.Context(config_path)
    assert ctx.parse_plan is not None, f"stale key broke the import: {ctx.import_error}"
    warnings = ctx.ignored_key_warnings()
    assert len(warnings) == 1 and "'resolver_cmd'" in warnings[0] and "ignored" in warnings[0]
    assert "regenerate from harness.config.example.json" in warnings[0], warnings[0]
    result = check.c6_no_syspath_in_ported(ctx)
    assert result.status == check.FAIL, f"C6 still skips on a stale resolver_cmd: {result.detail}"
    assert "do not exist" in result.detail


def _run_c9(check, root):
    ctx = _ctx(check, config={"skills_root": str(root)})
    ctx.skills_root = lambda: Path(root)
    return check.c9_no_typed_timestamp_placeholder(ctx)


# C9's needle is BUILT from `check._TYPED_TS_NEEDLE`, never typed as a literal
# below - the same discipline `_SYNTHETIC_NEEDLE` follows above, and for a
# sharper reason: this file lives UNDER the skills tree C9 greps, so a literal
# placeholder in a test fixture turns C9 red against the real repo exactly as a
# planted one in shipped code would. It did, on the first draft of these tests.
# Reading the constant also keeps the tests honest if the needle ever changes.


def test_c9_fails_when_the_skills_root_holds_no_greppable_file(check, tmp_path) -> None:
    """C9 refused a missing root but passed an empty one - exactly where C7's
    first repair stopped."""
    root = tmp_path / "skills"
    root.mkdir()
    assert check._grep_space(root) == 0, "the empty-haystack premise stopped holding"
    result = _run_c9(check, root)
    assert result.status == check.FAIL, f"C9 passed over an empty search space: {result.detail}"
    assert "no file this grep reads" in result.detail


def test_c9_fails_a_root_holding_only_unreadable_extensions(check, tmp_path) -> None:
    """The same zero reached by a POPULATED tree, and the sharper case: the only
    file present carries the needle, in an extension `_grep_count` never opens.
    The space guard cannot read it either - but it can refuse to call it clean."""
    root = tmp_path / "skills"
    root.mkdir()
    (root / "lane-state.env").write_text(f"eta: {check._TYPED_TS_NEEDLE}\n", encoding="utf-8")
    result = _run_c9(check, root)
    assert result.status == check.FAIL, f"C9 passed over unread files: {result.detail}"
    assert check._TYPED_TS_NEEDLE in result.detail


def test_c9_still_fails_a_missing_root(check, tmp_path) -> None:
    """Control: the pre-existing existence guard survives the new one."""
    result = _run_c9(check, tmp_path / "not-a-real-root")
    assert result.status == check.FAIL, f"C9 passed an absent root: {result.detail}"
    assert "skills_root does not exist" in result.detail


def test_c9_pass_states_the_size_of_the_search_space(check, tmp_path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    for name in ("a.md", "b.py", "c.json"):
        (root / name).write_text("clean\n", encoding="utf-8")
    result = _run_c9(check, root)
    assert result.status == check.PASS, f"C9 rejected a clean populated tree: {result.detail}"
    assert "3 file(s) read" in result.detail, f"PASS does not state its coverage: {result.detail}"


def test_c9_still_catches_a_planted_placeholder(check, tmp_path) -> None:
    """Control: the space guard must not become the only thing C9 checks."""
    root = tmp_path / "skills"
    root.mkdir()
    (root / "clean.md").write_text("nothing\n", encoding="utf-8")
    (root / "lane.md").write_text(f"eta: {check._TYPED_TS_NEEDLE}\n", encoding="utf-8")
    result = _run_c9(check, root)
    assert result.status == check.FAIL, f"C9 missed a planted placeholder: {result.detail}"
    assert "timestamps come from the clock" in result.detail


def _names_reached(code) -> set:
    """Every global name a code object references, nested code objects included.

    `co_names` alone stops at the function body: C6 measures its two roots inside
    a dict comprehension, which is its own code object, so a flat check reported
    the guard missing while it was there.
    """
    import types

    names = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _names_reached(const)
    return names


def test_c6_and_c9_probes_mirror_their_new_predicates(check) -> None:
    """The ABSENCE_PASS_EVIDENCE probes had the answer before the assertions did.
    Pin that they still measure the same thing the assertions now gate on - a
    C6 probe that SUMS the two roots grades a different assertion than the one
    that guards each."""
    import inspect

    src = inspect.getsource(check).split("ABSENCE_PASS_EVIDENCE = {")[1]
    c6 = next(line for line in src.splitlines() if line.strip().startswith('"C6"'))
    assert "min(" in c6, f"C6's probe no longer mirrors its per-root guard: {c6.strip()!r}"
    for fn in (check.c6_no_syspath_in_ported, check.c9_no_typed_timestamp_placeholder):
        assert "_grep_space" in _names_reached(fn.__code__), (
            f"{fn.__name__} no longer measures its own search space - the gh#139 defect"
        )


# --- C3: `created` as a predicate, and a board written to the cwd ----------
#
# C3's FAIL arm was a state check over the whole fixtures tree while `created`
# was computed and never read - so a board that had ALWAYS been tracked turned
# C3 red under a message claiming the malformed parse created it. The diff is
# the precise form. A pre-existing board is still refused, but as a broken
# harness (it would mask an overwrite from the diff), which is a different
# statement.
#
# Separately, a parser regressed into writing `status.md` relative to the
# working directory was missed entirely. C3 parses from an empty throwaway cwd
# rather than watching the real one, so there is no unrelated-status.md-in-the-
# repo-root false red to trade against.


def _cwd_writing_parser(exc=True):
    """A parser that writes a board into the CURRENT DIRECTORY, then hard-fails."""

    def parser(path):
        Path("status.md").write_text("# board\n", encoding="utf-8")
        if exc:
            raise ValueError("task 1.type must be 'standard' or 'tdd', got 'scaffold'")

    return parser


def test_c3_fails_a_board_written_relative_to_the_working_directory(check, tmp_path) -> None:
    result = _run_c3(check, tmp_path, parse_plan=_cwd_writing_parser())
    assert result.status == check.FAIL, f"C3 missed a cwd-written board: {result.detail}"
    assert "status.md" in result.detail


def test_c3_leaves_the_working_directory_where_it_found_it(check, tmp_path) -> None:
    """The cwd probe is a global side effect; a leak would move every later
    relative path in the run, including the other assertions' subprocesses."""
    import os

    before = os.getcwd()
    _run_c3(check, tmp_path, parse_plan=_cwd_writing_parser())
    assert os.getcwd() == before
    exploding = _run_c3(check, tmp_path / "boom", parse_plan=_real_parse_plan())
    assert os.getcwd() == before, "cwd leaked on the raising path"
    assert exploding.status == check.PASS


def test_c3_does_not_write_a_board_into_the_repo_working_directory(check, tmp_path) -> None:
    """The rejected alternative, pinned: C3 must not become sensitive to an
    unrelated status.md sitting in the directory the suite happens to run from.

    Run from a TEMP cwd rather than the real one. Writing the decoy board into
    `os.getcwd()` made this test both fragile and contagious: under CI that
    directory is `skills/_shared`, so the test hard-failed if a board was already
    there, and a regressed parser that drops a board into the run tree left it
    behind for the NEXT test to trip over - observed as exactly that cascade
    during a red-proof. The temp cwd is a faithful surface for the claim, since
    "the directory the suite happens to run from" is what is being stood in for,
    and it can neither inherit nor leave repo-tree state.
    """
    import os

    ambient = tmp_path / "ambient-cwd"
    ambient.mkdir()
    planted = ambient / "status.md"
    planted.write_text("# unrelated board\n", encoding="utf-8")
    before = os.getcwd()
    try:
        os.chdir(ambient)
        result = _run_c3(check, tmp_path, parse_plan=_real_parse_plan())
    finally:
        os.chdir(before)
    assert planted.exists(), "the decoy board vanished; this test proved nothing"
    assert result.status == check.PASS, (
        f"C3 went red for a status.md it did not create: {result.detail}"
    )


def test_c3_calls_a_preexisting_fixture_board_a_broken_harness(check, tmp_path) -> None:
    """A board the parse did not create must not be reported as one it did."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(parents=True)
    shutil.copy2(BAD_PLAN, fixtures / BAD_PLAN.name)
    (fixtures / "already-tracked.status.md").write_text("# board\n", encoding="utf-8")
    orig_f, orig_r = check.FIXTURES_DIR, check.RECORDS_DIR
    (tmp_path / "records").mkdir(parents=True, exist_ok=True)
    check.FIXTURES_DIR, check.RECORDS_DIR = fixtures, tmp_path / "records"
    try:
        result = check.c3_no_status_created(_ctx(check, parse_plan=_real_parse_plan()))
    finally:
        check.FIXTURES_DIR, check.RECORDS_DIR = orig_f, orig_r
    assert result.status == check.FAIL
    assert "BEFORE the parse" in result.detail, (
        f"a pre-existing board was reported as created by the parse: {result.detail}"
    )


# --- C3: all three surfaces must agree on what a board IS ------------------
#
# C3's two new surfaces (the fixtures diff and the cwd probe) go through
# `_status_board_paths`, which matches on SHAPE - both `status.md` and
# `<slug>.status.md` - because "a board written by a parser that should have
# raised would be named after the plan, not after us". The retained
# records/scenarioC net was left name-based (`glob("**/status.md")`), so it
# missed the very filename that reasoning predicts:
#
#     records/scenarioC/status.md                        -> FAIL
#     records/scenarioC/toy-calc-bad-task-type.status.md -> PASS
#
# It now uses the same helper as the other two.


def _plant_under_scenario_c(tmp_path: Path, name: str) -> None:
    d = tmp_path / "records" / "scenarioC"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("# board\n", encoding="utf-8")


def test_c3_fails_a_slug_named_board_under_records_scenario_c(check, tmp_path) -> None:
    """The shape the retained net was blind to, and the likelier of the two: a
    warn-and-continue parser names the board after the plan it parsed."""
    _plant_under_scenario_c(tmp_path, f"{FIXTURE_SLUG}.status.md")
    result = _run_c3(check, tmp_path, parse_plan=_real_parse_plan())
    assert result.status == check.FAIL, (
        f"a slug-named board under records/scenarioC read as clean: {result.detail}"
    )
    assert f"{FIXTURE_SLUG}.status.md" in result.detail


def test_c3_fails_a_plain_status_md_under_records_scenario_c(check, tmp_path) -> None:
    """Control: the shape the retained net already caught must stay caught."""
    _plant_under_scenario_c(tmp_path, "status.md")
    result = _run_c3(check, tmp_path, parse_plan=_real_parse_plan())
    assert result.status == check.FAIL, f"C3 stopped watching records/scenarioC: {result.detail}"
    assert "status.md" in result.detail


def test_c3_still_passes_with_a_non_board_file_under_records_scenario_c(check, tmp_path) -> None:
    """Control: shape-matching must not widen into "any file under the dir"."""
    _plant_under_scenario_c(tmp_path, "notes.md")
    result = _run_c3(check, tmp_path, parse_plan=_real_parse_plan())
    assert result.status == check.PASS, f"C3 red on a file that is not a board: {result.detail}"


def test_c3_pass_counts_only_status_roots_that_exist(check, tmp_path) -> None:
    """`2 status_root(s) probed` read as coverage the CI config does not have:
    both its roots are `/path/to/your/...` placeholders that do not exist."""
    live = tmp_path / "live-root"
    live.mkdir()
    result = _run_c3(
        check,
        tmp_path,
        parse_plan=_real_parse_plan(),
        config={"status_roots": [str(live), str(tmp_path / "nope"), "/path/to/your/specs"]},
    )
    assert result.status == check.PASS, f"C3 rejected a clean parse: {result.detail}"
    assert "1 of 3 configured status_root(s) exist" in result.detail, (
        f"PASS overstates its status-root coverage: {result.detail}"
    )


# --- the shipped config docs must not deny that C3 reads status_roots ------
#
# `harness.config.example.json`'s `_comment` said status_roots was "used by live
# recording sessions, not by check.py assertions" while C3 probed it in the same
# run. That is worse than stale prose: it is a machine-local CONFIGURATION
# INSTRUCTION, and an operator who believes it points status_roots at their real
# spec/plan tree without knowing an assertion now watches that tree.
#
# Pinned against C3's EXECUTED read (the status-roots red case above), so this
# cannot drift into asserting a sentence nobody honours.


def _config_prose() -> str:
    """The example config's `_comment` - its documentation, not its keys.

    Read as JSON rather than grepped: the raw file also contains the literal
    `"status_roots":` key, which is not prose and can carry no explanation.
    """
    import json

    return json.loads(
        (HARNESS_DIR / "harness.config.example.json").read_text(encoding="utf-8")
    )["_comment"]


@pytest.mark.parametrize("doc", ["harness.config.example.json", "README.md"])
def test_docs_state_that_status_roots_is_read_by_an_assertion(doc) -> None:
    text = _config_prose() if doc.endswith(".json") else (HARNESS_DIR / doc).read_text(encoding="utf-8")
    hits = [i for i in range(len(text)) if text.startswith("status_roots", i)]
    assert hits, f"{doc} no longer documents status_roots at all"
    for start in hits:
        window = text[start:start + 400]
        assert "C3" in window, (
            f"{doc} documents status_roots without saying C3 reads it "
            f"(near: {window[:160]!r})"
        )


# --- C4: format-example rows are not wakes (gh#604) ------------------------

# The seeded header of a fresh schedulewakeup_log.md: the stub README's record
# format inside a fence. Before gh#604 this parsed as one row and C4 PASSed.
_TEMPLATE_ONLY_LOG = (
    "# ScheduleWakeup stub log\n\n"
    "## Record format\n\n"
    "```\n"
    "<now-UTC> | scenario=<A|B|C|D> | wake_at=<future-UTC> | prompt=<verbatim prompt>\n"
    "```\n\n"
    "<!-- RECORDS-BELOW -->\n"
)


def test_c4_skips_a_template_only_log(check, tmp_path) -> None:
    result = _run_c4(check, tmp_path, _TEMPLATE_ONLY_LOG)
    assert result.status == check.SKIP, f"C4 counted the format example as a wake: {result.detail}"


def test_c4_skips_an_unfenced_placeholder_row(check, tmp_path) -> None:
    """Fence tracking alone is not the guard: an unfenced placeholder is still not a row."""
    row = "<now-UTC> | scenario=<A|B|C|D> | wake_at=<future-UTC> | prompt=<verbatim prompt>\n"
    result = _run_c4(check, tmp_path, row)
    assert result.status == check.SKIP, f"C4 counted a placeholder row: {result.detail}"


def test_c4_counts_a_real_row_below_the_template(check, tmp_path) -> None:
    """Control: the template is ignored, a real row after it still counts."""
    result = _run_c4(check, tmp_path, _TEMPLATE_ONLY_LOG + _WAKE_ROW.format(s="B"))
    assert result.status == check.PASS, f"C4 dropped a real row: {result.detail}"
    assert "1 wake row(s)" in result.detail
    assert "['B']" in result.detail


def test_c4_fails_a_C_row_with_a_bad_timestamp(check, tmp_path) -> None:
    """Review B2: a hand-typed C row is a recorded C wake whatever its timestamps say.
    Filtering on ISO timestamps dropped it and C4 PASSed 'none scenario=C'."""
    log = (
        "2026-01-01 | scenario=C | wake_at=soon | prompt=/lead-dev x\n"
        + _WAKE_ROW.format(s="B")
    )
    result = _run_c4(check, tmp_path, log)
    assert result.status == check.FAIL, f"C4 dropped a malformed C row: {result.detail}"


def test_c4_inner_different_fence_does_not_close_the_outer(check, tmp_path) -> None:
    """A ~~~ line inside a ``` fence is content, not a closer: the template after it
    is still fenced and must not count."""
    log = (
        "```\n"
        "~~~\n"
        "<now-UTC> | scenario=C | wake_at=<future-UTC> | prompt=<verbatim prompt>\n"
        "```\n"
    )
    result = _run_c4(check, tmp_path, log)
    assert result.status == check.SKIP, f"C4 read a still-fenced row: {result.detail}"


def test_b9_names_an_unparseable_timestamp(check, tmp_path) -> None:
    """Review B2: timestamp judgement stays with the consumer - B9 reports the bad
    timestamp rather than 'no row logged'."""
    log = tmp_path / "schedulewakeup_log.md"
    log.write_text("2026-01-01 | scenario=B | wake_at=soon | prompt=/lead-dev x\n", encoding="utf-8")
    orig = check.WAKE_LOG
    check.WAKE_LOG = log
    try:
        result = check.b9_one_future_wake(_ctx(check))
    finally:
        check.WAKE_LOG = orig
    assert result.status == check.FAIL
    assert "unparseable timestamps" in result.detail, result.detail


@pytest.mark.parametrize(
    "ts, wake_at, expected",
    [
        # A naive stamp beside an aware one used to raise TypeError out of the
        # `wake_at <= ts` comparison instead of returning a verdict (gh#612).
        ("2026-01-01T00:00:00Z", "2026-01-01T00:05:00", "PASS"),
        ("2026-01-01T00:00:00", "2026-01-01T00:05:00Z", "PASS"),
        ("2026-01-01T00:05:00Z", "2026-01-01T00:00:00", "FAIL"),
        # Naive is read as UTC, not as the local clock: 10:00+02:00 is 08:00Z, so a
        # naive 09:00 is after it. Under local time this would depend on the machine.
        ("2026-01-01T10:00:00+02:00", "2026-01-01T09:00:00", "PASS"),
    ],
)
def test_b9_compares_mixed_timezone_stamps_as_utc(check, tmp_path, ts, wake_at, expected) -> None:
    """The wake log's record format names both fields UTC (`stubs/README.md`), so a
    missing `Z` is that contract written loosely and B9 still owes a verdict on it."""
    log = tmp_path / "schedulewakeup_log.md"
    log.write_text(f"{ts} | scenario=B | wake_at={wake_at} | prompt=/lead-dev x\n", encoding="utf-8")
    orig = check.WAKE_LOG
    check.WAKE_LOG = log
    try:
        result = check.b9_one_future_wake(_ctx(check))
    finally:
        check.WAKE_LOG = orig
    assert result.status == getattr(check, expected), f"{ts} / {wake_at}: {result.detail}"
    # Stated directly as well: on a machine whose local zone IS UTC, "naive read as
    # local" and "naive read as UTC" give the same verdicts and the same offset, so
    # only the identity of the zone attached to a naive stamp tells them apart there.
    assert check._parse_iso("2026-01-01T09:00:00").tzinfo is timezone.utc
