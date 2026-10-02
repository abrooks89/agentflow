"""Tests for the additive optional Task.use_cases field (Phase 4 T4).

Additive-only: absence yields [] and every other field parses identically, so
existing plans and golden Scenario A stay green. use_cases is a flat opaque
scalar list validated for list-ness like deps, then coerced to strings.
"""

import textwrap
from pathlib import Path

import pytest

from agentflow.plan_parser import DAGError, Task, parse_plan


def _write_plan(tmp_path: Path, tasks_yaml: str) -> Path:
    tasks_block = textwrap.indent(textwrap.dedent(tasks_yaml).strip(), "  ")
    body = (
        '---\n'
        'epic: "Test Epic"\n'
        'spec: "spec.md"\n'
        'phases: ["P1"]\n'
        'tasks:\n'
        f"{tasks_block}\n"
        'test_commands:\n'
        '  P1: "pytest -q"\n'
        '---\n\n'
        '# body\n'
    )
    p = tmp_path / "plan.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_default_empty_when_absent(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        1:
          phase: "P1"
          deps: []
        """,
    )
    task = parse_plan(plan).tasks[1]
    assert task.use_cases == []


def test_dataclass_default_is_empty_list() -> None:
    assert Task(id=1, phase="P1", deps=[]).use_cases == []


def test_parses_scalar_list(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        1:
          phase: "P1"
          deps: []
          use_cases: ["UC-0147", "UC-0148"]
        """,
    )
    assert parse_plan(plan).tasks[1].use_cases == ["UC-0147", "UC-0148"]


def test_coerces_entries_to_strings(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        1:
          phase: "P1"
          deps: []
          use_cases: [147]
        """,
    )
    assert parse_plan(plan).tasks[1].use_cases == ["147"]


def test_null_use_cases_yields_empty(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        1:
          phase: "P1"
          deps: []
          use_cases: null
        """,
    )
    assert parse_plan(plan).tasks[1].use_cases == []


def test_non_list_use_cases_raises(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path,
        """
        1:
          phase: "P1"
          deps: []
          use_cases: "UC-0147"
        """,
    )
    with pytest.raises(DAGError, match="use_cases must be a list"):
        parse_plan(plan)


def test_additive_does_not_disturb_other_fields(tmp_path: Path) -> None:
    """A task carrying use_cases still parses every existing field identically."""
    plan = _write_plan(
        tmp_path,
        """
        1:
          phase: "P1"
          deps: []
          complexity: "high"
          best_of_n: 3
          use_cases: ["UC-0200"]
        """,
    )
    task = parse_plan(plan).tasks[1]
    assert task.complexity == "high"
    assert task.best_of_n == 3
    assert task.deps == []
    assert task.use_cases == ["UC-0200"]
# --------------------------------------------------------------------------- #
# files_section - the promotion precondition (E-0200 item 13)
# --------------------------------------------------------------------------- #
from agentflow.plan_parser import files_section  # noqa: E402

NL = chr(10)
BT = chr(96)


def _spec(*body: str) -> str:
    return NL.join(("# A spec", "") + body) + NL


def test_a_spec_with_no_Files_section_fails_the_gate():
    """The measured failure: an epic reaches approved-for-autonomous, looks like
    progress on every board, and is claimable in principle forever, because
    lead-dev cannot prove two lanes are disjoint and correctly refuses to publish.
    Nothing anywhere reports why it never moves."""
    fs = files_section(_spec("## Scope", "do the thing"))
    assert fs.present is False
    assert fs.seam_checkable is False


def test_a_Files_section_naming_real_paths_passes():
    fs = files_section(_spec("## Files", "", f"- {BT}skills/_shared/agentflow/config.py{BT} - the resolver",
                             f"- {BT}docs/REGISTRY.md{BT}"))
    assert fs.seam_checkable is True
    assert fs.paths == ("skills/_shared/agentflow/config.py", "docs/REGISTRY.md")


def test_a_Files_section_gesturing_at_an_AREA_is_present_but_not_seam_checkable():
    """THE SECOND HALF, AND THE REASON present IS NOT THE WHOLE GATE.

    "The loop skills" satisfies a section-exists check and tells a seam check
    nothing. A machine can decide part one; part two needs the promoting seat to
    read the list, which is why the SKILL.md prose has to say both.
    """
    fs = files_section(_spec("## Files", "", "- the loop skills", "- the vault side"))
    assert fs.present is True
    assert fs.seam_checkable is False
    assert fs.paths == ()


def test_a_bare_identifier_in_backticks_is_not_a_path():
    """`lead-dev` and `drafted` are backticked all over these specs. Counting them
    as paths would make the unspecific case above pass."""
    fs = files_section(_spec("## Files", "", f"- the {BT}lead-dev{BT} and {BT}steward{BT} skills"))
    assert fs.seam_checkable is False


def test_the_section_stops_at_the_next_h2():
    """A following section's backticked paths must not be counted as this one's."""
    fs = files_section(_spec("## Files", "", "- the loop skills", "", "## Verification",
                             f"- run {BT}skills/_shared/tests/test_x.py{BT}"))
    assert fs.paths == ()
    assert fs.seam_checkable is False


def test_a_deeper_heading_still_counts_as_the_section():
    """Specs write it as `## Files` and occasionally `### Files`."""
    assert files_section(_spec("### Files", "", f"- {BT}a/b.py{BT}")).seam_checkable is True


def test_every_APPROVED_spec_on_THIS_machine_passes(monkeypatch):
    """The control that says this gate can ship: measured against the real board
    rather than fixtures. Derives the corpus from the resolved config instead of a
    hardcoded path, so it skips cleanly anywhere the config is absent - a test that
    only runs on one machine is a test nobody else's CI can trust.

    Two approved specs in a SIBLING project do not pass, which is the gate finding
    exactly what it was built to find. Those are reported upward, not edited here,
    and this assertion is scoped to the project being built for that reason.
    """
    import re

    try:
        from agentflow.config import load_environment

        data_root = Path(load_environment().data_root)
    except Exception:  # noqa: BLE001 - no config on this box, nothing to measure
        pytest.skip("no resolved environment config")

    root = data_root / "projects" / "agentflow" / "design" / "specs"
    if not root.is_dir():
        pytest.skip("agentflow spec corpus not present")

    state_re = re.compile("^current_state:" + chr(92) + "s*(" + chr(92) + "S+)", re.M)
    failures = []
    for f in sorted(root.glob("*.md")):
        if ".bak" in f.name:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        m = state_re.search(text)
        if not m or m.group(1) != "approved-for-autonomous":
            continue
        if not files_section(text).seam_checkable:
            failures.append(f.name)
    assert not failures, f"approved specs that would fail the promotion gate: {failures}"
# --------------------------------------------------------------------------- #
# Document-shaped plans (E-0074 / gh#74) - solution/ holds two kinds of file
# --------------------------------------------------------------------------- #
def _plan_file(tmp_path, body: str, name: str = "doc.md"):
    f = tmp_path / name
    f.write_text("---" + NL + body.strip() + NL + "---" + NL + NL + "prose" + NL, encoding="utf-8")
    return f


ORCHESTRATOR = """
epic: a real epic
spec: /specs/a.md
slug: a
phases: [p1]
tasks:
  1:
    phase: p1
    deps: []
test_commands: {}
"""


def test_a_human_document_with_NO_tasks_parses(tmp_path):
    """THE HEADLINE. `solution/` holds orchestrator plans AND human-authored operator
    documents - a waterfall programme, a beta-test runbook. Requiring orchestrator
    frontmatter of both dropped every document, forever, with a warning no caller
    could see. 50 files on the live corpus, all the same class."""
    plan = parse_plan(_plan_file(tmp_path, "slug: waterfall" + NL + "title: a programme plan"))
    assert plan.tasks == {}
    assert plan.is_document is True


def test_a_document_needs_no_blank_spec_workaround(tmp_path):
    """The issue names this directly: "a plan legitimately without a spec should not
    need a blank key to parse". `spec: ""` was a workaround, not a fix."""
    plan = parse_plan(_plan_file(tmp_path, "slug: runbook"))
    assert plan.spec == ""
    assert plan.epic == ""
    assert plan.phases == []


def test_an_ORCHESTRATOR_plan_still_parses_in_full(tmp_path):
    """The control. A change that admits documents by loosening everything would
    pass every test above and quietly stop validating real plans."""
    plan = parse_plan(_plan_file(tmp_path, ORCHESTRATOR))
    assert plan.is_document is False
    assert plan.epic == "a real epic"
    assert plan.spec == "/specs/a.md"
    assert list(plan.tasks) == [1]


def test_a_BAD_TASK_TYPE_still_raises(tmp_path):
    """The narrowness proof. Two files in the live corpus carry `type: scaffold`, and
    they are genuinely malformed - admitting documents must not admit those."""
    body = ORCHESTRATOR.replace("    deps: []", "    deps: []" + NL + "    type: scaffold")
    with pytest.raises(DAGError) as e:
        parse_plan(_plan_file(tmp_path, body))
    assert "standard" in str(e.value)


def test_a_BROKEN_DAG_still_raises(tmp_path):
    """One live file declares a task in a phase the plan does not list. Still wrong."""
    body = ORCHESTRATOR.replace("phases: [p1]", "phases: [p2]")
    with pytest.raises(DAGError):
        parse_plan(_plan_file(tmp_path, body))


def test_a_tasks_key_that_is_not_a_mapping_raises(tmp_path):
    """`tasks: [1, 2]` is a malformed plan, not a document. Treating any unusable
    `tasks` as "no tasks" would swallow a real error into a silent empty plan."""
    with pytest.raises(DAGError) as e:
        parse_plan(_plan_file(tmp_path, "slug: x" + NL + "tasks: [1, 2]"))
    assert "mapping" in str(e.value)


def test_an_explicitly_null_tasks_is_a_document_not_an_error(tmp_path):
    """`tasks:` with nothing after it is how a human writes "none", and YAML reads
    it as None rather than an empty dict."""
    assert parse_plan(_plan_file(tmp_path, "slug: x" + NL + "tasks:")).is_document is True


def test_the_SURVEY_now_counts_a_document_instead_of_skipping_it(tmp_path):
    """End to end, which is where the defect actually showed: the file was dropped
    from every survey-derived count and the only evidence was a skip line."""
    from agentflow.fleet_report import survey_with_skips

    root = tmp_path / "data"
    (root / "projects" / "p" / "design" / "plans").mkdir(parents=True)
    (root / "projects" / "p" / "design" / "specs").mkdir(parents=True)
    doc = root / "projects" / "p" / "design" / "plans" / "programme.md"
    doc.write_text("---" + NL + "slug: programme" + NL + "---" + NL + NL + "prose" + NL,
                   encoding="utf-8")

    records, scan = survey_with_skips(root, "2020-01-01T00:00:00+00:00")
    assert scan.skipped_count == 0, f"still skipped: {scan}"
    assert "programme" in {r.slug for r in records}
