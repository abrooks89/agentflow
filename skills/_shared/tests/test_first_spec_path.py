"""dev-manager gives a first session a way to create a spec (gh#577).

A stranger's first dev-manager session met an empty survey, nothing to promote, and a "When NOT to
use" line pointing at a `pin` skill that does not ship (it was renamed `backlog`). This pins the
two halves of the fix to the file that reader actually opens.

WHAT IT CAN PROVE: the dead pointer is gone, the pointed-at skills exist, and the empty-board
branch names both spec-creating verbs and where the spec lands.
WHAT IT CANNOT PROVE: that a live session takes the branch.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEV_MANAGER = REPO_ROOT / "skills" / "dev-manager" / "SKILL.md"


def _text() -> str:
    return DEV_MANAGER.read_text(encoding="utf-8")


def _skill_text(name: str) -> str:
    return (REPO_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def test_no_pointer_to_a_pin_skill_that_does_not_ship():
    assert not (REPO_ROOT / "skills" / "pin").exists()
    assert not re.search(r"→ `pin`", _text()), "dev-manager still routes ideas to a `pin` skill"


def test_the_skills_it_points_at_ship():
    for skill in ("backlog", "refine"):
        assert (REPO_ROOT / "skills" / skill / "SKILL.md").is_file(), skill


def test_empty_board_branch_names_both_verbs_and_the_spec_folder():
    text = _text()
    assert "An empty board is a first session, not inbox-zero" in text
    branch = text[text.index("An empty board is a first session"):text.index("### 3.4 Issue intake")]
    for needle in ("/refine <idea>", "/backlog <idea>", "design/specs/", "project=<name>", "## Files"):
        assert needle in branch, needle


def test_the_spec_paths_it_names_match_the_skills_that_write_them():
    """The branch tells a stranger where the spec lands; that must stay true of the writers."""
    branch = _text()
    refine = _skill_text("refine")
    backlog = _skill_text("backlog")
    assert "design/specs/YYYY-MM-DD-<topic>-design.md" in refine
    assert "design/specs/YYYY-MM-DD-<topic>-design.md" in branch
    assert "design/specs/YYYY-MM-DD-<slug>.md" in backlog
