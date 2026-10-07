"""The skill-size ratchet: every SKILL.md stays under its line ceiling, and a raise is declared.

`skills/_shared/skill_budgets.json` maps each budgeted skill to a line ceiling; the logic lives in
`.github/scripts/skill_budget_gate.py`, which this file loads by path (it is repo tooling, not part
of the `agentflow` package).

WHAT IT CAN PROVE
* The current tree: every budgeted SKILL.md is at or under its ceiling, every budgeted SKILL.md has
  an entry and every entry names one. A failure names the file, the budget, the actual count and
  the exact edit that would raise it. A skill more than 50 lines under its ceiling raises a warning
  ("budget for X can drop to Y"), never a failure.
* The budgeted set comes from the GIT INDEX: tracked `skills/<dir>/SKILL.md` one level down plus
  `install/SKILL.md`. An untracked SKILL.md is not budgeted. Vendored skills (`skills/_vendored/**`)
  are EXCLUDED: they are third-party copies, and their size is not this repo's decision. Both the
  real tree and a fixture holding vendored SKILL.md files pin that.
* The PR-body escape hatch, on fixtures and throwaway git repos: a ceiling raised over the base
  with no `skill-budget: <skill> +N because <reason>` line fails and names the skill; with the line
  (plain, bulleted or quoted) it passes. A new or renamed key above its file's line count is a raise
  too. A wrong N, a malformed line, a duplicate line and a line naming an unbudgeted skill fail; a
  quoted template and a malformed line inside a fenced block do not. No error echoes body text. A
  base with no budget file, a missing base revision, a malformed base budget file and an unset
  PR_BODY behave as documented.
* Wiring: the declaration step sits in ci.yml's `sanitize-gate` job (a required context), runs on
  pull requests only, reads the body and base sha through env, and compares against the merge
  commit's first parent with the event's base sha as the fallback.

WHAT IT CANNOT PROVE
* That GitHub serves the merge commit's parents to a depth-2 fetch. The PR's own run is that proof.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import warnings
from pathlib import Path

import pytest
import yaml

from agentflow.tracked_paths import tracked_files

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "skill_budget_gate.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JOB = "sanitize-gate"
STEP = "Raised skill budgets are declared"
PROSE_STEP = "Instruction prose carries no history"


def _load_gate():
    spec = importlib.util.spec_from_file_location("skill_budget_gate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="the budgeted set is read from git")


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return res.stdout.strip()


def _write(root: Path, skills: dict[str, int], budgets: dict | None, extra: dict[str, str] | None = None) -> None:
    for name, lines in skills.items():
        rel = "install/SKILL.md" if name == "install" else f"skills/{name}/SKILL.md"
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(b"line %d\n" % i for i in range(lines)))
    for rel, text in (extra or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if budgets is not None:
        _write_budgets(root, budgets)


def _tree(root: Path, skills: dict[str, int], budgets: dict | None, extra: dict[str, str] | None = None) -> Path:
    """A fixture checkout with every file staged: one SKILL.md of N lines per skill, plus budgets."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, skills, budgets, extra)
    _git(root, "add", "-A")
    return root


def _write_budgets(root: Path, budgets: dict) -> None:
    path = root / gate.BUDGET_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(budgets), encoding="utf-8")


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# The current tree
# ---------------------------------------------------------------------------


def test_every_skill_is_within_its_budget():
    errors, advisories = gate.check_ceilings(REPO_ROOT, gate.load_budgets(REPO_ROOT))
    for advisory in advisories:
        warnings.warn(advisory, stacklevel=1)
    assert not errors, "\n".join(errors)


def test_budget_file_covers_exactly_the_budgeted_set():
    skills = gate.discover(REPO_ROOT)
    budgets = gate.load_budgets(REPO_ROOT)
    assert set(budgets) == set(skills)
    assert skills["install"] == "install/SKILL.md"
    first_party = tracked_files(
        REPO_ROOT,
        lambda r: r.startswith("skills/") and r.endswith("/SKILL.md") and r.count("/") == 2
        and not r.startswith("skills/_vendored/"),
    )
    assert len(skills) == len(first_party) + 1


def test_vendored_skills_are_excluded_from_the_real_budget():
    """Vendored skills (skills/_vendored/**) are excluded: their size is not this repo's call."""
    vendored = tracked_files(REPO_ROOT, lambda r: r.startswith("skills/_vendored/") and r.endswith("/SKILL.md"))
    assert vendored, "expected tracked vendored SKILL.md files to exclude"
    paths = set(gate.discover(REPO_ROOT).values())
    for p in vendored:
        assert p.relative_to(REPO_ROOT).as_posix() not in paths
    assert not [p for p in paths if "_vendored" in p]
    assert "_vendored" not in gate.load_budgets(REPO_ROOT)


# ---------------------------------------------------------------------------
# The budgeted set and ceilings, on fixtures
# ---------------------------------------------------------------------------


def test_vendored_skill_files_are_never_budgeted(tmp_path):
    root = _tree(
        tmp_path,
        {"demo": 3},
        None,
        {"skills/_vendored/SKILL.md": "x\n", "skills/_vendored/other/SKILL.md": "x\n"},
    )
    assert gate.discover(root) == {"demo": "skills/demo/SKILL.md"}


def test_an_untracked_skill_is_not_budgeted(tmp_path):
    root = _tree(tmp_path, {"demo": 3}, None)
    _write(root, {"scratch": 3}, None)  # on disk, never added
    assert gate.discover(root) == {"demo": "skills/demo/SKILL.md"}


def test_nested_skill_files_are_not_budgeted(tmp_path):
    root = _tree(tmp_path, {"demo": 3}, None, {"skills/demo/references/SKILL.md": "x\n"})
    assert gate.discover(root) == {"demo": "skills/demo/SKILL.md"}


def test_two_files_sharing_a_budget_key_do_not_run(tmp_path):
    root = _tree(tmp_path, {"install": 3}, None, {"skills/install/SKILL.md": "x\n"})
    with pytest.raises(gate.GateDidNotRun, match="share the budget key 'install'"):
        gate.discover(root)


def test_a_skill_over_its_ceiling_fails_with_the_exact_remedy(tmp_path):
    root = _tree(tmp_path, {"auditor": 15, "install": 4}, {"auditor": 10, "install": 4})
    errors, _ = gate.check_ceilings(root, gate.load_budgets(root))
    assert errors == [
        "skills/auditor/SKILL.md is 15 lines, over its budget of 10 by 5: cut it, or set "
        "\"auditor\": 15 in skills/_shared/skill_budgets.json and add "
        "'skill-budget: auditor +5 because <reason>' to the PR body"
    ]


def test_a_skill_at_its_ceiling_passes(tmp_path):
    root = _tree(tmp_path, {"auditor": 10, "install": 4}, {"auditor": 10, "install": 4})
    assert gate.check_ceilings(root, gate.load_budgets(root)) == ([], [])


@pytest.mark.parametrize(
    "data, lines",
    [(b"a\r\nb\r\nc", 2), (b"a\nb\nc", 2), (b"x", 0), (b"a\nb", 1), (b"a\nb\n", 2), (b"", 0)],
)
def test_lines_are_counted_like_wc(tmp_path, data, lines):
    path = tmp_path / "f.md"
    path.write_bytes(data)
    assert gate.line_count(path) == lines


def test_a_skill_missing_from_the_budget_file_fails(tmp_path):
    root = _tree(tmp_path, {"auditor": 3, "steward": 3}, {"auditor": 3})
    errors, _ = gate.check_ceilings(root, gate.load_budgets(root))
    assert len(errors) == 1
    assert "skills/steward/SKILL.md has no budget" in errors[0]


def test_a_budget_entry_naming_no_skill_fails(tmp_path):
    root = _tree(tmp_path, {"auditor": 3}, {"auditor": 3, "retired": 9})
    errors, _ = gate.check_ceilings(root, gate.load_budgets(root))
    assert len(errors) == 1
    assert "'retired'" in errors[0]


@pytest.mark.parametrize(
    "text",
    ["{not json", "[1, 2]", '{"auditor": "10"}', '{"auditor": 0}', '{"auditor": true}', '{"Bad Name": 3}'],
)
def test_a_malformed_budget_file_does_not_run(text):
    with pytest.raises(gate.GateDidNotRun):
        gate.parse_budgets(text, "fixture")


def test_slack_over_fifty_lines_is_an_advisory_not_a_failure(tmp_path):
    root = _tree(tmp_path, {"demo": 10}, {"demo": 61})
    assert gate.check_ceilings(root, gate.load_budgets(root)) == (
        [],
        ["budget for demo can drop to 10 (skills/demo/SKILL.md is 10 lines, budget 61)"],
    )
    _write_budgets(root, {"demo": 60})
    assert gate.check_ceilings(root, gate.load_budgets(root)) == ([], [])


# ---------------------------------------------------------------------------
# The PR-body declaration
# ---------------------------------------------------------------------------

BASE = {"auditor": 10, "steward": 20}
RAISED = {"auditor": 11, "steward": 20}
COUNTS = {"auditor": 11, "steward": 20}
INJECTION = "::warning::planted"


def test_a_raise_with_no_budget_line_fails_naming_the_skill():
    errors, _ = gate.check_raises(BASE, RAISED, COUNTS, "Agentflow [lead-dev]\n\nSome change.\n")
    assert len(errors) == 1
    assert "budget for auditor rose by +1 (10 -> 11)" in errors[0]
    assert "skill-budget: auditor +1 because <reason>" in errors[0]


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["", "- ", "* ", "> ", "> - "])
def test_a_raise_with_its_budget_line_passes(eol, prefix):
    body = eol.join(["Agentflow [lead-dev]", "", f"{prefix}skill-budget: auditor +1 because the step needs it", ""])
    errors, notes = gate.check_raises(BASE, RAISED, COUNTS, body)
    assert errors == []
    assert notes == ["budget for auditor raised by +1, declared"]


def test_a_budget_line_with_the_wrong_amount_fails():
    errors, _ = gate.check_raises(BASE, RAISED, COUNTS, "skill-budget: auditor +5 because reasons")
    assert errors == ["the budget for auditor rose by +1 (10 -> 11), but PR body line 1 declares +5"]


@pytest.mark.parametrize(
    "line",
    [
        "skill-budget: auditor 1 because no plus sign",
        "skill-budget: auditor +1",
        "skill-budget: auditor +1 because",
        "skill-budget: +1 because no skill",
        "skill-budget: auditor +0 because zero",
        "- skill-budget: auditor 1 because a bulleted one",
    ],
)
def test_a_malformed_budget_line_fails_by_line_number(line):
    errors, _ = gate.check_raises(BASE, BASE, COUNTS, f"Agentflow [lead-dev]\n\n{line}")
    assert errors == [
        "PR body line 3 is a malformed skill-budget line; expected "
        "`skill-budget: <skill> +<N> because <reason>`"
    ]


def test_no_error_echoes_pr_body_text():
    body = "\n".join([
        f"skill-budget: {INJECTION} +1 because x",
        f"skill-budget: unknownskill +1 because {INJECTION}",
        f"skill-budget: auditor +1 because {INJECTION}",
        f"skill-budget: auditor +1 because again {INJECTION}",
    ])
    errors, notes = gate.check_raises(BASE, RAISED, COUNTS, body)
    assert len(errors) == 3, errors
    for text in errors + notes:
        assert INJECTION not in text and "unknownskill" not in text and "again" not in text, text


def test_a_quoted_template_is_not_a_declaration():
    body = "Raise with:\n\nskill-budget: <skill> +<N> because <reason>\n"
    assert gate.check_raises(BASE, BASE, COUNTS, body) == ([], [])


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_a_malformed_line_inside_a_fence_is_an_example(fence):
    body = f"Example:\n{fence}\nskill-budget: auditor 5 because shape\n{fence}\n"
    assert gate.check_raises(BASE, BASE, COUNTS, body) == ([], [])
    outside = f"Example:\n{fence}\n{fence}\nskill-budget: auditor 5 because shape\n"
    errors, _ = gate.check_raises(BASE, BASE, COUNTS, outside)
    assert errors == [
        "PR body line 4 is a malformed skill-budget line; expected "
        "`skill-budget: <skill> +<N> because <reason>`"
    ]


def test_two_lines_for_one_skill_fail():
    body = "skill-budget: auditor +1 because a\nskill-budget: auditor +1 because b"
    errors, _ = gate.check_raises(BASE, RAISED, COUNTS, body)
    assert errors == ["PR body lines 1 and 2 declare the same skill; keep one"]


def test_a_line_naming_an_unbudgeted_skill_fails():
    errors, _ = gate.check_raises(BASE, BASE, COUNTS, "skill-budget: audtior +1 because typo")
    assert errors == ["PR body line 1 names a skill with no budget in skills/_shared/skill_budgets.json"]


def test_a_line_for_a_skill_that_did_not_rise_is_noted_and_passes():
    errors, notes = gate.check_raises(BASE, BASE, COUNTS, "skill-budget: steward +3 because stale line")
    assert errors == []
    assert "steward" in notes[0]


def test_a_lowered_ceiling_and_a_new_key_at_its_count_need_no_line():
    head = {"auditor": 9, "steward": 20, "newskill": 40}
    assert gate.check_raises(BASE, head, {**COUNTS, "newskill": 40}, "") == ([], [])


def test_a_new_key_above_its_count_is_a_raise():
    head = {"auditor": 10, "steward": 20, "newskill": 45}
    errors, _ = gate.check_raises(BASE, head, {**COUNTS, "newskill": 40}, "")
    assert errors == [
        "the budget for newskill rose by +5 (new key at 45, its file is 40 lines) and the PR body "
        "declares no raise: add the line 'skill-budget: newskill +5 because <reason>'"
    ]
    errors, _ = gate.check_raises(BASE, head, {**COUNTS, "newskill": 40}, "skill-budget: newskill +5 because it is new")
    assert errors == []


def test_renaming_a_key_cannot_carry_a_raise():
    errors, _ = gate.check_raises({"auditor": 10}, {"auditor2": 5000}, {"auditor2": 5}, "")
    assert len(errors) == 1
    assert "budget for auditor2 rose by +4995" in errors[0]


# ---------------------------------------------------------------------------
# The CLI against a base commit, as the CI step runs it
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    """A git repo whose base commit budgets auditor at 10; the working tree raises it to 11."""
    root = _tree(tmp_path / "repo", {"auditor": 10, "install": 2}, {"auditor": 10, "install": 2})
    base = _commit(root, "base")
    _write(root, {"auditor": 11}, {"auditor": 11, "install": 2})
    return root, base


def test_cli_fails_an_undeclared_raise(repo, capsys):
    root, base = repo
    assert gate.run(["--base", base], root=root, env={"PR_BODY": "no budget line"}) == 1
    out = capsys.readouterr().out
    assert "::error::skill budget gate: the budget for auditor rose by +1 (10 -> 11)" in out


def test_cli_passes_a_declared_raise(repo, capsys):
    root, base = repo
    body = "skill-budget: auditor +1 because the new step needs it"
    assert gate.run(["--base", base], root=root, env={"PR_BODY": body}) == 0
    assert "budget for auditor raised by +1, declared" in capsys.readouterr().out


def test_cli_never_prints_pr_body_text(repo, capsys):
    root, base = repo
    body = f"skill-budget: {INJECTION}\nskill-budget: nosuch +1 because {INJECTION}"
    assert gate.run(["--base", base], root=root, env={"PR_BODY": body}) == 1
    out = capsys.readouterr().out
    assert INJECTION not in out and "nosuch" not in out
    assert "PR body line 1 is a malformed skill-budget line" in out


def test_cli_checks_every_key_against_its_count_when_the_base_has_no_budget_file(tmp_path, capsys):
    root = _tree(tmp_path / "repo", {"auditor": 3}, None)
    base = _commit(root, "base")
    _write_budgets(root, {"auditor": 3})
    assert gate.run(["--base", base], root=root, env={"PR_BODY": ""}) == 0
    assert "is new at this PR" in capsys.readouterr().out
    _write_budgets(root, {"auditor": 9})
    assert gate.run(["--base", base], root=root, env={"PR_BODY": ""}) == 1
    assert "budget for auditor rose by +6" in capsys.readouterr().out


def test_cli_fails_closed_on_a_base_it_cannot_read(repo, capsys):
    root, _ = repo
    assert gate.run(["--base", "0" * 40], root=root, env={"PR_BODY": ""}) == 2
    assert "the check did not run" in capsys.readouterr().out


def test_cli_fails_closed_on_a_malformed_base_budget_file(tmp_path, capsys):
    root = _tree(tmp_path / "repo", {"auditor": 3}, None)
    (root / gate.BUDGET_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / gate.BUDGET_FILE).write_text('{"auditor": "three"}', encoding="utf-8")
    base = _commit(root, "base")
    _write_budgets(root, {"auditor": 3})
    assert gate.run(["--base", base], root=root, env={"PR_BODY": ""}) == 2
    out = capsys.readouterr().out
    assert "the check did not run" in out and f"at {base}" in out


def test_cli_fails_closed_without_a_pr_body(repo, capsys):
    root, base = repo
    assert gate.run(["--base", base], root=root, env={}) == 2
    assert "PR_BODY is not set" in capsys.readouterr().out


def test_regenerate_sets_every_ceiling_to_its_count(repo):
    root, _ = repo
    _write_budgets(root, {"auditor": 500})
    assert gate.run(["--regenerate"], root=root, env={}) == 0
    assert gate.load_budgets(root) == {"auditor": 11, "install": 2}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_declaration_step_runs_in_the_required_sanitize_job_on_prs():
    wf = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    steps = wf["jobs"][JOB]["steps"]
    names = [s.get("name") for s in steps]
    assert names.count(STEP) == 1, names
    assert names.index(STEP) > names.index(PROSE_STEP), names
    step = steps[names.index(STEP)]
    assert "!cancelled()" in step["if"] and "github.event_name == 'pull_request'" in step["if"]
    assert step["env"]["PR_BODY"] == "${{ github.event.pull_request.body }}"
    assert step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    run = step["run"]
    assert "${{" not in run, "the body and base sha must arrive through env"
    first_parent = run.index("git rev-parse --verify -q 'HEAD^1'")
    assert run.index('git fetch --no-tags --depth=2 origin "$head_sha"') < first_parent
    fallback = run.index('git fetch --no-tags --depth=1 origin "$BASE_SHA"')
    assert first_parent < fallback < run.index('python3 .github/scripts/skill_budget_gate.py --base "$base"')
