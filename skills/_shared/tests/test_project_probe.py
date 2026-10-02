#!/usr/bin/env python3
"""Tests for the install-interview project probe.

The probe exists so "I don't know" is a usable answer to the verification
question: it reads the checkout and proposes a strategy WITH its reasons, so the
interviewer never silently picks one.

Its contract:
- evidence-based — an unreadable or unknown tree yields `none` and says why
- a UI project is never recommended a suite-only gate
- a project with BOTH a suite and browser tooling is pointed at a SKILL.md path,
  because the enum values are single-mode
- every recommendation carries at least one reason

Run: `python -m pytest skills/_shared/tests/test_project_probe.py -v`
"""

from __future__ import annotations

import json

from agentflow.project_probe import Evidence, format_report, probe, recommend


def _write(root, rel, content=""):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _pkg(scripts=None, deps=None):
    return json.dumps({"scripts": scripts or {}, "dependencies": deps or {}})


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def test_missing_checkout_is_reported_not_raised(tmp_path):
    ev = probe(tmp_path / "nope")
    assert ev.exists is False
    rec = recommend(ev)
    assert rec.strategy == "none"
    assert "no checkout found" in rec.reasons[0]


def test_detects_node_ui_browser_tooling_and_commands(tmp_path):
    _write(tmp_path, "package.json", _pkg(
        scripts={"test": "vitest run", "e2e": "playwright test", "dev": "next dev"},
        deps={"react": "^19", "@playwright/test": "^1"},
    ))
    _write(tmp_path, "next.config.ts")
    _write(tmp_path, "e2e/full-flow.spec.ts")

    ev = probe(tmp_path)
    assert "javascript/typescript" in ev.languages
    assert "next.js" in ev.ui_frameworks and "react" in ev.ui_frameworks
    assert "playwright" in ev.browser_tools
    assert "e2e" in ev.e2e_dirs
    assert "npm run test" in ev.test_commands and "npm run e2e" in ev.test_commands
    assert "npm run dev" in ev.dev_commands
    assert ev.has_ui and ev.has_suite and ev.has_browser_tooling


def test_malformed_package_json_degrades_instead_of_crashing(tmp_path):
    _write(tmp_path, "package.json", "{ this is not json")
    ev = probe(tmp_path)
    assert ev.exists is True
    assert "javascript/typescript" in ev.languages  # the manifest still counts
    assert ev.test_commands == []                   # but yielded no commands


def test_python_library_detection(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path, "tests/test_x.py")
    ev = probe(tmp_path)
    assert ev.languages == ["python"]
    assert ev.test_commands == ["pytest"]
    assert not ev.has_ui


def test_containerization_is_noticed(tmp_path):
    _write(tmp_path, "Dockerfile", "FROM scratch\n")
    assert probe(tmp_path).containerized is True


# --------------------------------------------------------------------------- #
# recommend
# --------------------------------------------------------------------------- #
def test_ui_plus_suite_plus_browser_is_pointed_at_a_skill_path(tmp_path):
    _write(tmp_path, "package.json", _pkg(scripts={"test": "vitest run"}, deps={"react": "^19"}))
    _write(tmp_path, "playwright.config.ts")
    rec = recommend(probe(tmp_path))
    assert rec.strategy == "<SKILL.md path>"
    assert any("single-mode" in f for f in rec.follow_ups)
    assert any("staging.url" in f for f in rec.follow_ups)


def test_ui_without_browser_tooling_still_never_recommends_a_suite_gate(tmp_path):
    _write(tmp_path, "package.json", _pkg(scripts={"test": "vitest run"}, deps={"vue": "^3"}))
    rec = recommend(probe(tmp_path))
    assert rec.strategy == "web-journey"
    assert any("no browser tooling found" in f for f in rec.follow_ups)
    # the suite is demoted, not discarded
    assert any("regression floor" in f for f in rec.follow_ups)


def test_library_with_tests_gets_test_suite_plus_a_caveat(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path, "tests/test_x.py")
    rec = recommend(probe(tmp_path))
    assert rec.strategy == "test-suite"
    assert any("UI the probe missed" in f for f in rec.follow_ups)


def test_code_with_no_tooling_recommends_none_and_says_why(tmp_path):
    _write(tmp_path, "go.mod", "module x\n")
    ev = probe(tmp_path)
    ev.test_commands = []  # a go.mod with no suite convention applied
    rec = recommend(ev)
    assert rec.strategy == "none"
    assert any("no test tooling" in r for r in rec.reasons)
    assert any("smoke-cli" in f for f in rec.follow_ups)


def test_empty_directory_yields_no_evidence_and_admits_it(tmp_path):
    rec = recommend(probe(tmp_path))
    assert rec.strategy == "none"
    assert "no evidence" in rec.reasons[0] or "probe has no evidence" in rec.reasons[0]


def test_every_recommendation_carries_at_least_one_reason(tmp_path):
    cases = [
        Evidence(checkout="x", exists=True, ui_frameworks=["react"]),
        Evidence(checkout="x", exists=True, languages=["python"], test_commands=["pytest"]),
        Evidence(checkout="x", exists=True, languages=["go"]),
        Evidence(checkout="x", exists=False),
    ]
    for ev in cases:
        assert recommend(ev).reasons, f"no reason given for {ev}"


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def test_report_states_it_is_a_proposal_not_a_decision(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    ev = probe(tmp_path)
    text = format_report(ev, recommend(ev))
    assert "RECOMMENDED verification.strategy: test-suite" in text
    assert "because:" in text
    assert "The user decides." in text
