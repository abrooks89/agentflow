"""Fleet skills route design work to `refine`, never to the vendored brainstorming skill (gh#585).

The vendored `brainstorming` skill writes its design doc to the retired pre-migration `refine`
bucket at the top of the data root. The epic survey reads only `projects/<name>/design/specs/`, so a brainstorm-pin that
dev-manager step 6 sent there produced a spec no lane could ever see. `refine` is the fleet's fork
and writes where the survey reads. The vendored copy is left unpatched on purpose.

WHAT IT CAN PROVE: the named first-party skills route to `refine`, and `refine` writes to
design/specs. WHAT IT CANNOT PROVE: that a live session follows the route.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agentflow.tracked_paths import tracked_files

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDORED_ROUTE = "../_vendored/brainstorming/SKILL.md"


def _skill_text(name: str) -> str:
    return (REPO_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", ["dev-manager", "tdd-loop"])
def test_design_work_routes_to_refine_not_vendored_brainstorming(skill):
    text = _skill_text(skill)
    assert VENDORED_ROUTE not in text, f"{skill}/SKILL.md still routes design work to vendored brainstorming"
    assert "../refine/SKILL.md" in text, f"{skill}/SKILL.md names no route to refine"


def test_no_first_party_skill_or_command_routes_to_vendored_brainstorming():
    """Corpus-wide, from the index, so a third skill regressing is caught too."""
    files = tracked_files(
        REPO_ROOT,
        lambda rel: "_vendored" not in rel
        and ((rel.startswith("skills/") and rel.endswith("/SKILL.md"))
             or (rel.startswith("commands/") and rel.endswith(".md"))),
    )
    assert files, "found no first-party skills or commands; the check would be vacuous"
    offenders = [str(f.relative_to(REPO_ROOT)) for f in files
                 if VENDORED_ROUTE in f.read_text(encoding="utf-8")]
    assert not offenders, offenders


def test_step_6_leaves_one_record_per_epic():
    text = _skill_text("dev-manager")
    assert "Leave one record per epic" in text
    assert "do not follow its writing-plans handoff" in text


def test_refine_writes_where_the_survey_reads():
    assert "design/specs/YYYY-MM-DD-<topic>-design.md" in _skill_text("refine")
