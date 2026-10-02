"""gh#720: the auditor's blocker return is defined by the subagent contract, and the gate holds
its canned blocker to that definition.

A blocker ("I could not verify") and a `missed` verdict ("the intent fell short") route to
different places: a blocker becomes a dev-manager `## Blockers` row with no ledger row, so the
epic is judged again; a `missed` becomes a gap epic plus a ledger row, which retires the epic.
If the contract has no blocker shape, the subagent can only say `missed`, and the gate's F2 row
proves routing for a shape nothing real emits. These tests pin the shape to the contract text.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = REPO_ROOT / "harness" / "auditor-scenario" / "auditor_scenario.py"
CONTRACT = REPO_ROOT / "skills" / "auditor" / "references" / "audit-agent.md"


def _load_driver():
    # FAIL, never skip: a moved driver must break this test, not retire it.
    assert DRIVER.is_file(), f"auditor driver not found at {DRIVER}"
    spec = importlib.util.spec_from_file_location("agentflow_auditor_scenario_gh720", DRIVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _canned_blockers(drv) -> dict:
    canned = drv.canned_subagent_output({"uc_sat": "UC-0001", "uc_half": "UC-0002"})
    return {slug: r for slug, r in canned.items() if "blocker" in r}


def test_contract_declares_a_parseable_blocker_return():
    shape = _load_driver().contract_blocker_return()
    assert set(shape["blocker"]) == {"blocked_on", "tried"}
    # A blocker replaces the verdicts; a result carrying both could be routed either way.
    assert "verdicts" not in shape
    assert {"epic", "strategy_used"} <= set(shape)


def test_the_canned_blocker_has_the_contracts_shape():
    drv = _load_driver()
    blockers = _canned_blockers(drv)
    assert blockers, "the scenario has no canned blocker, so F2 would prove nothing about the shape"
    for slug, result in blockers.items():
        assert drv.blocker_shape_mismatches(result) == [], slug


def test_a_drifted_blocker_shape_is_reported():
    """The comparison must be able to fail, or F2's shape half is vacuous."""
    drv = _load_driver()
    drifted = {"epic": "e", "strategy_used": None, "blocker": {"reason": "staging down"}}
    assert drv.blocker_shape_mismatches(drifted)
    with_verdicts = {"epic": "e", "strategy_used": None, "verdicts": [],
                     "blocker": {"blocked_on": "x", "tried": "y"}}
    assert drv.blocker_shape_mismatches(with_verdicts)


def test_f2_fails_when_a_canned_blocker_drifts_from_the_contract():
    """F2 itself, not just its helpers: a drifted canned blocker must turn the row red even
    when every routing half of it passes."""
    drv = _load_driver()
    canned = drv.canned_subagent_output({"uc_sat": "UC-0001", "uc_half": "UC-0002"})
    ok, blockers, problems = drv.f2_passes(True, True, True, canned)
    assert ok and blockers and not problems

    slug = blockers[0]
    drifted = dict(canned)
    drifted[slug] = {**canned[slug], "blocker": {"reason": "staging down"}}
    ok, _, problems = drv.f2_passes(True, True, True, drifted)
    assert not ok and slug in problems

    no_blocker = {k: v for k, v in canned.items() if "blocker" not in v}
    ok, _, _ = drv.f2_passes(True, True, True, no_blocker)
    assert not ok, "F2 must not pass with no canned blocker to hold to the contract"


def test_contract_routes_an_unverifiable_epic_to_the_blocker_not_to_missed():
    text = CONTRACT.read_text(encoding="utf-8")
    for case in ("No `verification.strategy`", "Staging unreachable",
                 "Spec or plan doc missing", "A probe cannot run"):
        assert case in text, f"the contract no longer names the blocker case {case!r}"
    assert "unreachable), return `verdict: missed`" not in text


def test_the_products_own_import_failure_is_a_verdict_not_a_blocker():
    """A broken product module is a regression to report, not an environment to wait out."""
    text = CONTRACT.read_text(encoding="utf-8")
    probe_case = text[text.index("**A probe cannot run**"):text.index("A blocker covers the whole epic")]
    assert "third-party dependency" in probe_case
    assert "OWN" in probe_case and "regressed" in probe_case
    assert "import error in the harness" not in text
