"""REGISTRY.md's question lifecycle names both seats that answer within delegated authority.

The steward is the primary delegated answerer (its domain P, every wake) and dev-manager the
fallback when a human is in the seat. A step 4 that names only dev-manager reads as a limit
on the steward, and a limit-only duty is satisfied by answering nothing (gh#224).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_DOC = REPO_ROOT / "docs" / "REGISTRY.md"


def _step_4_ask() -> str:
    text = REGISTRY_DOC.read_text(encoding="utf-8")
    match = re.search(r"\*\*4 — ASK\.\*\*(.*?)\n\*\*5 — ", text, flags=re.S)
    assert match, "REGISTRY.md has no '**4 — ASK.**' step followed by step 5"
    return match.group(1)


def test_step_4_names_the_steward_as_the_primary_delegated_answerer() -> None:
    step = _step_4_ask()
    assert "answered_by: steward" in step
    assert re.search(r"\*\*steward\*\*\s+is the\s+primary", step), step


def test_step_4_keeps_dev_manager_as_the_fallback() -> None:
    step = _step_4_ask()
    assert "answered_by: dev-manager" in step
    assert re.search(r"\*\*dev-manager\*\*\s+is the fallback", step), step


def test_step_4_points_at_both_procedures() -> None:
    step = _step_4_ask()
    assert "skills/steward/SKILL.md" in step
    assert "skills/dev-manager/SKILL.md" in step
    for rel in ("skills/steward/SKILL.md", "skills/dev-manager/SKILL.md"):
        assert (REPO_ROOT / rel).is_file(), rel


def test_step_4_says_dev_manager_rechecks_a_steward_decline() -> None:
    # A steward decline is a classification, not a verdict: dev-manager §1c re-checks it and
    # answers the row when the steward was too cautious.
    step = _step_4_ask()
    assert "re-checks" in step, step


def test_no_section_says_only_dev_manager_writes_the_answer_fields() -> None:
    text = REGISTRY_DOC.read_text(encoding="utf-8")
    assert "**only dev-manager** asks the human and writes the answer fields" not in text
    assert "# dev-manager-owned from here down" not in text
    arch = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "answer fields only by dev-manager" not in arch
