"""refine hands an approved spec to `solution`, and plans land where the survey reads them.

refine used to end by invoking raw writing-plans and forbid every other skill. A plan made that
way never got the DAG frontmatter `solution` adds, and writing-plans saved it to the retired
`solution/` bucket at the top of the data root, so the epic survey never saw it. The plan
template also documented a `scaffold` task type and a `ledger` block that the plan parser rejects
or ignores, so a plan authored from the template could fail to parse.

WHAT IT CAN PROVE: refine's handoff names `solution` and no longer names raw writing-plans as its
next step; the vendored plan-writing and plan-reading skills name the plans directory the survey
reads; the template documents exactly the task types the parser accepts.
WHAT IT CANNOT PROVE: that a live session follows the handoff.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
import yaml

from agentflow.plan_parser import DAGError, parse_plan

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = REPO_ROOT / "skills"
TEMPLATE = SKILLS / "orchestrator" / "templates" / "plan-frontmatter.yaml"
PLANS_DIR = "<data_root>/projects/<name>/design/plans/"
#: Assembled rather than written out: spelled contiguously, this module would trip the
#: pre-migration bucket gate in test_skill_call_sites.py, which scans tests too.
STALE_PLANS_DIR = "<data_root>/" + "solution" + "/"

#: The vendored skills that write or read a plan file. `brainstorming` is deliberately absent:
#: it is left unpatched and refine is its fork (see test_brainstorm_route.py).
PLAN_SKILLS = ("writing-plans", "requesting-code-review", "subagent-driven-development")


def _skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def test_refine_terminal_step_invokes_solution_not_raw_writing_plans():
    text = _skill("refine")
    assert "**The terminal state is invoking `solution`.**" in text
    assert '"Invoke solution skill" [shape=doublecircle]' in text
    assert "12. **Transition to implementation** — invoke `../solution/SKILL.md`" in text
    assert "writing-plans is the next step" not in text
    assert "The terminal state is invoking writing-plans" not in text
    assert "Invoke writing-plans skill" not in text
    assert _skill("solution"), "refine hands off to a solution skill that does not ship"


def test_project_intercepts_refines_solution_handoff_so_solution_runs_once():
    text = _skill("project")
    assert "refine's terminal step invokes `solution` itself" in text
    assert "brainstorming's Step 9 will normally invoke" not in text


@pytest.mark.parametrize("skill", PLAN_SKILLS)
def test_vendored_plan_skills_name_the_plans_dir_the_survey_reads(skill):
    text = (SKILLS / "_vendored" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert STALE_PLANS_DIR not in text, f"{skill} names the retired pre-migration plans bucket"
    assert PLANS_DIR in text, f"{skill} names no plan under {PLANS_DIR}"


def test_writing_plans_does_not_offer_execution_when_solution_invokes_it():
    text = (SKILLS / "_vendored" / "writing-plans" / "SKILL.md").read_text(encoding="utf-8")
    handoff = text.split("## Execution Handoff", 1)[1]
    first_offer = handoff.index("Two execution options")
    guard = handoff.index("**When invoked by the `solution` skill**")
    assert guard < first_offer, "the solution-invoked branch must come before the execution offer"
    assert "do NOT offer an execution choice" in handoff[guard:first_offer]


def _documented_types(template_text: str) -> set[str]:
    return set(re.findall(r'^\s*#\s+- "(\w+)"\s+—', template_text, flags=re.MULTILINE))


def test_template_documents_only_the_task_types_the_parser_accepts(tmp_path):
    text = TEMPLATE.read_text(encoding="utf-8")
    assert _documented_types(text) == {"standard", "tdd"}
    assert "scaffold" not in text
    assert "ledger" not in yaml.safe_load(text), "template still carries the ledger block"

    def plan(task_type: str) -> Path:
        p = tmp_path / f"plan-{task_type}.md"
        p.write_text(textwrap.dedent(f"""\
            ---
            epic: "e"
            spec: "s.md"
            phases: [p1]
            tasks:
              1:
                phase: p1
                deps: []
                type: {task_type}
            test_commands:
              regression: "pytest -q"
            ---
            # body
            """), encoding="utf-8")
        return p

    for t in sorted(_documented_types(text)):
        assert parse_plan(plan(t)).tasks[1] is not None, t
    with pytest.raises(DAGError):
        parse_plan(plan("scaffold"))
