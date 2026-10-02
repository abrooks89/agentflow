"""Tests for agentflow.stabilization_tests path resolution (AF-B3).

The stabilization-tests directory must resolve from the installed suite root —
no hardcoded legacy `~/.claude/skills/<orchestrator skill dir>/` literal anywhere
in the module, and resolution must work against an arbitrary (fake) suite root.
"""

from pathlib import Path

from agentflow import stabilization_tests
from agentflow.stabilization_tests import (
    IntegrationTestSuggestion,
    append_suggestion,
    suggestions_dir,
)


def _suggestion() -> IntegrationTestSuggestion:
    return IntegrationTestSuggestion(
        task_id=1,
        task_name="wire the frobnicator",
        feature="frobnicator end-to-end",
        why_deferred="needs a real container",
        suggested_approach="compose-based integration test",
    )


def test_module_has_no_legacy_skill_path_literal():
    src = Path(stabilization_tests.__file__).read_text(encoding="utf-8")
    # Concatenated so this test file itself never carries the greppable legacy name.
    legacy_dir = ".claude/skills/long" + "form-orchestrator"
    assert legacy_dir not in src


def test_default_dir_resolves_under_installed_suite_root():
    suite_root = Path(stabilization_tests.__file__).resolve().parents[3]
    assert suggestions_dir() == (
        suite_root / "skills" / "_shared" / "stabilization-tests"
    )


def test_resolves_under_fake_suite_root(tmp_path):
    d = suggestions_dir(tmp_path)
    assert d == tmp_path / "skills" / "_shared" / "stabilization-tests"

    written = append_suggestion("epic-x", "Epic X", _suggestion(), base_dir=d)
    assert written == d / "epic-x.md"
    assert written.exists()
    assert "frobnicator end-to-end" in written.read_text(encoding="utf-8")
