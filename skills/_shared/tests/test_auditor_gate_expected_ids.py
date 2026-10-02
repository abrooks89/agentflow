"""The auditor ship gate grades its rows against a pinned id set (gh#218 item 3).

`run_auditor_scenario` used to refuse only ZERO rows. Its denominator was the number of
rows that arrived, so it could never disagree with them: a driver regressed to emit 1
of its 15 checks printed `1/1 checks PASS` and returned 0. These tests drive the gate
over a stub driver's stdout (no real subprocess) and pin that a missing, an unpinned or
a repeated check id reddens it, with the full set as the control that stays green.
"""
from __future__ import annotations

import importlib.util
import random
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = REPO_ROOT / "harness"
DRIVER = HARNESS_DIR / "auditor-scenario" / "auditor_scenario.py"


def _load_check_module():
    """Import harness/check.py by path — it is a script, not a package module."""
    path = HARNESS_DIR / "check.py"
    assert path.is_file(), f"harness/check.py not found at {path} - moved, or this path is stale"
    spec = importlib.util.spec_from_file_location("agentflow_harness_check_auditorids", path)
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: check.py defines dataclasses, and @dataclass resolves
    # its own module out of sys.modules at class-creation time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check():
    return _load_check_module()


@pytest.fixture
def gate(check, monkeypatch, capsys):
    """Run the gate over canned driver stdout; return (exit code, printed text)."""
    def _go(rows: list[tuple[str, str]], returncode: int = 0) -> tuple[int, str]:
        class _Completed:
            stdout = "".join(f"{check.AUDITOR_ROW_PREFIX}{s}\t{aid}\tdetail\n" for s, aid in rows)
            stderr = ""
        _Completed.returncode = returncode
        monkeypatch.setattr(check.subprocess, "run", lambda *_a, **_k: _Completed())
        rc = check.run_auditor_scenario()
        return rc, capsys.readouterr().out
    return _go


def _all_green(check) -> list[tuple[str, str]]:
    return [("PASS", aid) for aid in check.AUDITOR_EXPECTED_IDS]


def test_the_full_pinned_set_all_green_ships(check, gate):
    rc, out = gate(_all_green(check))
    assert rc == 0, out
    n = len(check.AUDITOR_EXPECTED_IDS)
    assert f"# auditor ship gate: {n}/{n} expected checks PASS" in out
    assert "FAIL" not in out


def test_emission_order_is_not_graded(check, gate):
    rows = _all_green(check)
    random.Random(218).shuffle(rows)
    assert [a for _, a in rows] != list(check.AUDITOR_EXPECTED_IDS), "shuffle was a no-op"
    rc, out = gate(rows)
    assert rc == 0, out


def test_a_driver_regressed_to_one_green_check_is_red(check, gate):
    """The gh#218 repro: one green row used to print `1/1 checks PASS` and exit 0."""
    rc, out = gate([("PASS", "G2-builder-entry")])
    assert rc == 1, out
    n = len(check.AUDITOR_EXPECTED_IDS)
    assert f"# auditor ship gate: 1/{n} expected checks PASS" in out
    assert f"FAIL auditor-gate: {n - 1} expected check(s) never reported" in out


def test_one_missing_check_is_red_and_named(check, gate):
    dropped = check.AUDITOR_EXPECTED_IDS[3]
    rc, out = gate([r for r in _all_green(check) if r[1] != dropped])
    assert rc == 1, out
    assert "never reported" in out and repr(dropped) in out


def test_an_unpinned_check_is_red_even_when_green(check, gate):
    rc, out = gate(_all_green(check) + [("PASS", "Z-nobody-pinned-this")])
    assert rc == 1, out
    assert "unpinned check(s) reported" in out and "'Z-nobody-pinned-this'" in out
    n = len(check.AUDITOR_EXPECTED_IDS)
    # The unpinned row must not inflate the numerator past the pinned denominator.
    assert f"# auditor ship gate: {n}/{n} expected checks PASS" in out


def test_a_repeated_row_cannot_stand_in_for_a_missing_one(check, gate):
    ids = list(check.AUDITOR_EXPECTED_IDS)
    rows = [("PASS", a) for a in ids[:-1]] + [("PASS", ids[0])]
    assert len(rows) == len(ids), "row count must match, so only the set can catch it"
    rc, out = gate(rows)
    assert rc == 1, out
    assert "reported more than once" in out and "never reported" in out


@pytest.mark.parametrize("status", ["SKIP", "pass", ""])
def test_a_pinned_check_that_did_not_pass_is_red(check, gate, status):
    """Only PASS is a pass: a SKIP, a mis-cased or an empty status must not ship."""
    rows = _all_green(check)
    rows[0] = (status, rows[0][1])
    rc, out = gate(rows)
    assert rc == 1, out
    n = len(check.AUDITOR_EXPECTED_IDS)
    assert f"# auditor ship gate: {n - 1}/{n} expected checks PASS" in out


def test_the_pinned_set_matches_every_id_the_driver_can_emit(check):
    """Every `check("<id>", ...)` literal in the driver is pinned, and nothing else is.

    The runtime gate catches a regressed driver in `--self-test`; this catches the edit
    that adds or renames a check without touching the pin, in the fast suite. Every
    call site passes a string literal, and the guard below fails if one ever stops.
    """
    src = DRIVER.read_text(encoding="utf-8")
    # `def check(aid, ...)` is the definition, not a call site.
    call_sites = re.findall(r"(?<!def )\bcheck\(\s*(\S)", src)
    assert call_sites, f"no check( call sites found in {DRIVER} - the pattern is stale"
    assert set(call_sites) == {'"'}, (
        f"a check( call site no longer passes a string literal: {call_sites} - "
        "a computed id would escape this pin")
    literals = set(re.findall(r'\bcheck\(\s*"([^"]+)"', src))
    assert literals == set(check.AUDITOR_EXPECTED_IDS)
    assert len(check.AUDITOR_EXPECTED_IDS) == len(set(check.AUDITOR_EXPECTED_IDS))
