"""D4's checker must DISCRIMINATE — gated in CI, where D4 itself can never run.

Assertion D4 (`harness/check.py`) proves a live builder wake applied an answered
Question before claiming fresh work. That proof is only as good as the checker
behind it, and the checker is the one part nobody can see fail:

- `harness/records/` is gitignored (`.gitignore:2`), so D4 reads SKIP on every
  fresh clone and green only on the single machine that recorded the wake;
- CI runs `check.py --self-test` and `--only C` (`ci.yml`), never `--only D`.

So a weakened `d4_consume_first` — one that accepts a violating record — would
be invisible: CI cannot run it, and the one machine that can has a green record
sitting in an ignored directory. This module closes that specific hole. It does
NOT record the wake and does NOT substitute for it (`run-scenario.md`: "Requires
a live builder firing - this record cannot be canned"); a committed record would
be canned for every clone after the first, which is why none is committed here.

What it gates is narrower and real: given the shipped seed fixture, D4 PASSES a
compliant after-state and FAILS each way the contract can actually be broken.
Every after-state below is built by MUTATING the shipped fixture, never by
hand-writing a registry — a fixture the test authored itself would only prove
the test agrees with itself.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = REPO_ROOT / "harness"
SEED = HARNESS_DIR / "fixtures" / "scenarioD-registry-before.md"


def _load_check_module():
    """Import harness/check.py by path — it is a script, not a package module."""
    path = HARNESS_DIR / "check.py"
    # FAIL, never skip: if the harness moves, this gate must break loudly rather
    # than quietly stop covering the checker.
    assert path.is_file(), f"harness/check.py not found at {path} - moved, or this path is stale"
    spec = importlib.util.spec_from_file_location("agentflow_harness_check", path)
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: check.py defines dataclasses, and @dataclass resolves
    # its own module out of sys.modules at class-creation time. Without this the
    # import dies with AttributeError on NoneType.__dict__.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check():
    return _load_check_module()


@pytest.fixture(scope="module")
def seed_text() -> str:
    assert SEED.is_file(), f"seed fixture missing at {SEED} - D4 reads its ids from this file"
    return SEED.read_text(encoding="utf-8")


class _Ctx:
    """Minimal Context stand-in: D4 only reaches for module-level dirs."""


def _run_d4(check, tmp_path: Path, after_text: str | None, seed_text: str | None = "SHIPPED"):
    """Point the checker's FIXTURES_DIR/RECORDS_DIR at a temp tree and run D4.

    The SEED is copied verbatim from the shipped fixture by default, so the ids
    D4 resolves are the real ones, and only the recorded after-state varies.
    `seed_text=None` writes no fixture at all and any other string substitutes
    for it — the two ways the tracked fixture can stop resolving.
    """
    fixtures = tmp_path / "fixtures"
    records = tmp_path / "records" / "scenarioD"
    fixtures.mkdir(parents=True, exist_ok=True)
    records.mkdir(parents=True, exist_ok=True)
    if seed_text is not None:
        (fixtures / "scenarioD-registry-before.md").write_text(
            SEED.read_text(encoding="utf-8") if seed_text == "SHIPPED" else seed_text,
            encoding="utf-8",
        )
    if after_text is not None:
        (records / "registry-after.md").write_text(after_text, encoding="utf-8")

    orig_f, orig_r = check.FIXTURES_DIR, check.RECORDS_DIR
    check.FIXTURES_DIR, check.RECORDS_DIR = fixtures, tmp_path / "records"
    try:
        return check.d4_consume_first(_Ctx())
    finally:
        check.FIXTURES_DIR, check.RECORDS_DIR = orig_f, orig_r


def _compliant_after(seed_text: str) -> str:
    """The seed as a COMPLIANT post-wake registry: answer applied, parked row
    resumed, fresh row still unclaimed. Derived from the shipped seed."""
    after = seed_text.replace("- state: answered", "- state: applied")
    after = after.replace(
        "| E-0101 | toy-epic-d export naming | lead | lead-dev | parked | Q-0101 | parked on answer |",
        "| E-0101 | toy-epic-d export naming | lead | lead-dev | building | - | resumed on answer |",
    )
    return after


def test_seed_fixture_still_has_the_shape_d4_resolves(seed_text: str) -> None:
    """If the seed drifts, every case below silently tests nothing."""
    assert "### Q-0101" in seed_text
    assert "- state: answered" in seed_text
    assert "| E-0101 |" in seed_text and "parked" in seed_text
    assert "| E-0102 |" in seed_text and "ready" in seed_text


def test_d4_passes_a_compliant_after_state(check, seed_text, tmp_path) -> None:
    result = _run_d4(check, tmp_path, _compliant_after(seed_text))
    assert result.status == check.PASS, f"D4 rejected a compliant record: {result.detail}"


def test_d4_skips_when_no_record_exists_and_names_the_recording_procedure(
    check, seed_text, tmp_path
) -> None:
    """The fresh-clone case — the state every clone but one is in.

    A missing record means nobody recorded a wake on this machine, not that a
    builder misbehaved. Reporting FAIL there made a red mean two things a reader
    could not tell apart, so this arm is a SKIP. The SKIP still has to be
    actionable, hence the assertions on the message: it must name the record
    path and the procedure section that clears the debt.
    """
    result = _run_d4(check, tmp_path, None)
    assert result.status == check.SKIP, f"missing record must be a debt, not a violation: {result.detail}"
    assert "Scenario D consume-first" in result.detail
    # Repo-root-relative, because that is the cwd check.py is documented and run
    # from (harness/README.md, ci.yml). A bare `records/...` sends the one reader
    # this SKIP exists for - someone who has never seen this repo - to `ls
    # records/` and "No such file", which is a debt they cannot act on.
    assert "harness/records/scenarioD/registry-after.md" in result.detail
    assert "harness/run-scenario.md" in result.detail


def test_d4_fails_when_the_answer_was_never_applied(check, seed_text, tmp_path) -> None:
    """Question left `answered`: the wake never consumed it."""
    after = _compliant_after(seed_text).replace("- state: applied", "- state: answered")
    result = _run_d4(check, tmp_path, after)
    assert result.status == check.FAIL, "D4 accepted a record whose question was never applied"
    assert "Q-0101" in result.detail


def test_d4_fails_when_the_parked_row_was_never_resumed(check, seed_text, tmp_path) -> None:
    """Answer flipped but the work left parked - consumed on paper only."""
    after = seed_text.replace("- state: answered", "- state: applied")
    result = _run_d4(check, tmp_path, after)
    assert result.status == check.FAIL, "D4 accepted a record whose parked row never resumed"
    assert "E-0101" in result.detail


def test_d4_fails_when_the_builder_grabbed_fresh_work_instead(check, seed_text, tmp_path) -> None:
    """THE violation the assertion exists for: claiming E-0102 while Q-0101 sits
    answered-but-unapplied. run-scenario.md calls this 'a real consume-first
    violation to report, not to paper over'."""
    # Start from the COMPLIANT after-state, not the raw seed. Built from the seed
    # it would still carry an unapplied question, so D4 would fail on THAT check
    # first and this case would pass while proving nothing about the fresh-row
    # rule - verified: with the fresh-row check deleted from D4, the seed-based
    # version stayed green. Only the claimed row may differ here.
    after = _compliant_after(seed_text).replace(
        "| E-0102 | toy-epic-d docs pass | lead | - | ready | - | |",
        "| E-0102 | toy-epic-d docs pass | lead | lead-dev | building | - | |",
    )
    assert after != _compliant_after(seed_text), (
        "E-0102 row shape drifted - this case stopped testing anything"
    )
    result = _run_d4(check, tmp_path, after)
    assert result.status == check.FAIL, "D4 accepted a record where the builder skipped the answer"
    # NOT an `or` with Q-0101: that would let a failure on the question check
    # satisfy the fresh-row case, which is how this test was wrong the first time.
    assert "E-0102" in result.detail, (
        f"D4 failed for the wrong reason - want the fresh-row violation, got: {result.detail}"
    )


def test_d4_fails_when_the_seed_fixture_is_absent(check, seed_text, tmp_path) -> None:
    """A missing seed is a broken harness, NOT a missing recording.

    The fixture is tracked, so its absence can only mean the harness itself is
    wrong — the opposite of the gitignored record, whose absence is normal. If
    this branch ever adopted the SKIP this epic introduced for the record, D4
    would go permanently unproven the moment the fixture moved, silently.
    """
    result = _run_d4(check, tmp_path, _compliant_after(seed_text), seed_text=None)
    assert result.status == check.FAIL, (
        f"an absent tracked fixture must be loud, not a debt: {result.detail}"
    )


def test_d4_fails_when_the_seed_fixture_stops_resolving_its_ids(check, seed_text, tmp_path) -> None:
    """The realistic drift: the Queue table's column ORDER changes.

    `_registry_queue_row` reads lane/claimed_by/state/waiting_on by index
    (`cells[2..5]`), so a reordered table still parses as a table and still
    contains every id — it just no longer yields `parked`/`ready` states, and
    D4 can resolve neither id. That must FAIL. As a SKIP it would disable the
    assertion on EVERY machine, including one holding a genuine recording, and
    nothing downstream would go red.

    Note this is not caught by the seed's own shape test, which greps loose
    substrings; only running the checker against the reordered table catches it.
    """
    reordered = seed_text.replace(
        "| id | work item | lane | claimed_by | state | waiting_on | notes |",
        "| id | work item | lane | state | claimed_by | waiting_on | notes |",
    )
    reordered = reordered.replace(
        "| E-0101 | toy-epic-d export naming | lead | lead-dev | parked | Q-0101 |",
        "| E-0101 | toy-epic-d export naming | lead | parked | lead-dev | Q-0101 |",
    ).replace(
        "| E-0102 | toy-epic-d docs pass | lead | - | ready | - |",
        "| E-0102 | toy-epic-d docs pass | lead | ready | - | - |",
    )
    assert reordered != seed_text, "seed row shape drifted - this case stopped testing anything"

    result = _run_d4(check, tmp_path, _compliant_after(seed_text), seed_text=reordered)
    assert result.status == check.FAIL, (
        f"a fixture whose ids no longer resolve must FAIL, not silently disable D4: {result.detail}"
    )


def test_a_skip_for_no_record_does_not_soften_a_recorded_violation(
    check, seed_text, tmp_path
) -> None:
    """The failure mode of the SKIP: swallowing a real defect along with the debt.

    Both inputs go through the same entry point, so a change that widened the
    SKIP arm — or flattened the whole assertion to SKIP — shows up here as the
    two cases collapsing onto one status. The recorded violation used is the
    question-applied invariant; the parked-row and fresh-work branches are
    covered by their own cases above and must stay FAIL too.
    """
    absent = _run_d4(check, tmp_path / "absent", None)
    violating_after = _compliant_after(seed_text).replace("- state: applied", "- state: answered")
    present = _run_d4(check, tmp_path / "present", violating_after)

    assert absent.status == check.SKIP, f"no record must SKIP: {absent.detail}"
    assert present.status == check.FAIL, (
        f"a present record that never applied the answer must still FAIL: {present.detail}"
    )
    assert absent.status != present.status, "SKIP is masking a recorded consume-first violation"


def test_d4_is_wired_behavioral_in_the_shipped_registry(check) -> None:
    """The exit-code contract is only worth anything if the SHIPPED D4 carries it.

    `run_suite` counts a FAIL toward the exit code only when the assertion is
    `behavioral`; a structural one prints `FAIL D4 [structural]` and leaves the
    gate green. The exit-code case below builds its own Assertion and so hard-
    codes the flag it is checking — nothing else sees it either: `--self-test`
    only checks that the id is present, CI's `--only C` never touches group D,
    and the cases above call `d4_consume_first` directly. So flipping the one
    word in `D("D4", True, ...)` turns an observed consume-first violation into
    a warning and the whole suite stays green. This is the assertion that bites.
    """
    d4 = [a for a in check.build_registry() if a.id == "D4"]
    assert len(d4) == 1, f"expected exactly one D4 in the registry, got {len(d4)}"
    assert d4[0].behavioral, "a D4 FAIL must fail the exit code, not warn: wire D4 behavioral=True"


def _one_assertion_registry(check, status: str):
    """A build_registry() replacement wiring a single behavioral assertion that
    returns `status`, so run_suite's own summarise/exit path decides the code."""

    def _registry():
        return [check.Assertion(id="D4", group="D", behavioral=True,
                                fn=lambda ctx: check.Result(status, "synthetic"))]

    return _registry


@pytest.mark.parametrize(
    "status_name, expected_exit",
    [("SKIP", 0), ("FAIL", 1), ("PASS", 0)],
)
def test_run_suite_exit_code_treats_skip_as_not_failing(
    check, monkeypatch, status_name, expected_exit
) -> None:
    """Exit-code contract, driven through the checker's real exit path.

    `run_suite` is the function `main` returns through, so this is the actual
    summarise-and-exit logic, not a restatement of it. Only the assertion
    registry is substituted — a SKIP result must leave the gate green, and a
    behavioral FAIL must still turn it red.
    """
    config = HARNESS_DIR / "harness.config.example.json"
    assert config.is_file(), f"tracked example config missing at {config}"
    monkeypatch.setattr(check, "build_registry",
                        _one_assertion_registry(check, getattr(check, status_name)))
    assert check.run_suite(config, None) == expected_exit
