"""One epic-worktree command, held by `/project` Step 5; every other copy matches it (gh#625).

THE DEFECT. Three skills each taught their own `git worktree add` for the epic tree, and none
defined its branch names:

* `/project` Step 5: `-b <branch>` with no start point, so the branch forked from whatever HEAD
  the shared checkout happened to hold.
* solution: `<base-branch>` with no `-b`, which git refuses ("already checked out") whenever the
  base branch is checked out in the shared checkout -- credited to a "Step 6 of solution" that
  does not exist, and promising an existing directory is reused where `/project` says it fails.
* orchestrator recovery: a third form, `-b <epic-branch> <base-branch>`, off a local ref.
* lead-dev and jr-dev-1/2/3 restated the old `-b <branch>` recipe in their worktree sections.

Separately, the orchestrator's `{epic_start_sha}` fallback read `rev-parse <branch>^{N}`: `^{...}`
is git's PEEL syntax (`^{commit}`, `^{tree}`), not an ancestor count, so the line could never
name the commit it describes. `~N` is the N-th first-parent ancestor.

WHAT THIS PINS, and only this. Every `git -C ... worktree add` COMMAND line in these seven skills
is one of the two forms `/project` Step 5 defines (create, or re-attach a resumed epic's existing
branch), the create form is preceded by a fetch so `origin/<base-branch>` is current and carries
`--no-track` (a branch tracking `origin/<base-branch>` would defeat Step 7's first
`push -u origin <epic-branch>`), every `--head` in the orchestrator's Step 7 is `<epic-branch>`,
the peel form is gone, and solution and the orchestrator's recovery point at `/project` Step 5. It CANNOT prove an operator follows the command, or that
`<epic-branch>` is spelled by the claim contract in a given run -- only that the prose now says one
thing in one place.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentflow.tracked_paths import only_tracked

SKILLS_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = SKILLS_ROOT.parent

# `joinpath`, not the `/` operator: the pre-migration-bucket gate in test_skill_call_sites reads
# an operator join onto the solution directory as a stale data-root bucket; here it is a skill.
PROJECT = SKILLS_ROOT.joinpath("project", "SKILL.md")
SOLUTION = SKILLS_ROOT.joinpath("solution", "SKILL.md")
ORCHESTRATOR = SKILLS_ROOT.joinpath("orchestrator", "SKILL.md")
#: The loop skills restate the create form in their worktree sections; they must match it too.
LOOPS = tuple(SKILLS_ROOT.joinpath(n, "SKILL.md") for n in ("lead-dev", "jr-dev-1", "jr-dev-2",
                                                            "jr-dev-3"))
SKILLS = (PROJECT, SOLUTION, ORCHESTRATOR, *LOOPS)

#: The path token is the layout `/project` Step 5 creates: the directory is the epic's branch, which
#: in a fleet run is the claim branch name and so carries the row's number. A hand run is covered
#: too, because its `<epic-branch>` is its slug; admitting `<epic-slug>` here would let a reverted
#: command stay green. The orchestrator's recovery names the tree its Step 1.1 resolved
#: (`<worktree>`). Nothing else is a legal path here.
_PATH = r"(?:<worktree_root>/<project>/<epic-branch>|<worktree>)"
_GIT = r"git -C <canonical_repo\.local_checkout> "
CREATE = re.compile(_GIT + r"worktree add " + _PATH + r" -b <epic-branch> --no-track origin/<base-branch>$")
REATTACH = re.compile(_GIT + r"worktree add " + _PATH + r" <epic-branch>$")
FETCH = _GIT.replace("\\", "") + "fetch"
_COMMAND = re.compile(r"git\s+-C\s+\S+\s+worktree\s+add\b[^`]*")
#: Removal commands wrap across lines in some skills, hence matching over the whole text.
_REMOVE = re.compile(r"git\s+-C\s+<canonical_repo\.local_checkout>\s+worktree\s+remove\s+([^`,\s]+)")


def _tracked() -> list[Path]:
    got = only_tracked(SUITE_ROOT, SKILLS)
    # Floor: a renamed skill must red here, not drop out of coverage with a green run.
    assert got == sorted(p.resolve() for p in SKILLS), got
    return got


def _commands(path: Path) -> list[tuple[int, str]]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        for m in _COMMAND.finditer(line):
            out.append((n, m.group(0).strip()))
    return out


def test_skills_are_tracked():
    _tracked()


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_every_worktree_add_command_is_a_defined_form(path: Path):
    _tracked()
    bad = [(n + 1, c) for n, c in _commands(path) if not (CREATE.match(c) or REATTACH.match(c))]
    assert not bad, f"{path.parent.name}: worktree add not in the /project Step 5 form: {bad}"


@pytest.mark.parametrize("path", (PROJECT, ORCHESTRATOR, *LOOPS), ids=lambda p: p.parent.name)
def test_create_form_is_present_and_fetched_first(path: Path):
    _tracked()
    lines = path.read_text(encoding="utf-8").splitlines()
    creates = [n for n, c in _commands(path) if CREATE.match(c)]
    assert creates, f"{path.parent.name} has no create-form worktree add"
    for n in creates:
        window = "\n".join(lines[max(0, n - 3):n + 1])
        assert FETCH in window, f"{path.parent.name}:{n + 1} creates off origin without a fetch"


def test_project_step5_defines_both_forms_and_every_placeholder():
    _tracked()
    text = PROJECT.read_text(encoding="utf-8")
    step5 = text.split("### Step 5:", 1)[1].split("\n### ", 1)[0]
    cmds = [c for _, c in _commands(PROJECT)]
    assert any(CREATE.match(c) and "<worktree_root>" in c for c in cmds)
    assert any(REATTACH.match(c) and "<worktree_root>" in c for c in cmds)
    for ph in ("<canonical_repo.local_checkout>", "<worktree_root>", "<project>", "<epic-slug>",
               "<epic-branch>", "<base-branch>"):
        assert f"`{ph}`" in step5 and re.search(re.escape(f"`{ph}`") + r"\s+(?:is|—|-)", step5), (
            f"/project Step 5 uses {ph} without defining it"
        )
    assert "resumed epic" in step5, "the existing-path rule (reuse only a resumed epic's tree)"
    # A merged tree is a detached HEAD, so only a numbered directory still ties it to its row.
    # Per command, not per section: create, re-attach and remove must each name the branch.
    paths = re.findall(r"worktree (?:add|remove) (<worktree_root>/<project>/[^`,\s]+)", step5)
    assert len(paths) == 3 and set(paths) == {"<worktree_root>/<project>/<epic-branch>"}, paths
    assert re.search(r"directory is the claim branch name", step5), (
        "/project Step 5 must define the fleet worktree directory as the claim branch name"
    )


@pytest.mark.parametrize("path", (PROJECT, *LOOPS), ids=lambda p: p.parent.name)
def test_worktree_remove_names_the_tree_create_made(path: Path):
    """A removal on another path leaves the created tree behind and the next create collides."""
    _tracked()
    got = _REMOVE.findall(path.read_text(encoding="utf-8"))
    assert got, f"{path.parent.name} has no explicit epic-end worktree remove"
    assert set(got) == {"<worktree_root>/<project>/<epic-branch>"}, (path.parent.name, got)


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_no_undefined_bare_branch_placeholder(path: Path):
    """`<branch>` was the undefined name every site used; the epic's branch is `<epic-branch>`."""
    _tracked()
    assert "<branch>" not in path.read_text(encoding="utf-8")


def test_step7_push_and_pr_use_the_epic_branch():
    """Per occurrence, not per file: one `--head <epic-branch>` must not vouch for a sibling."""
    _tracked()
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    assert "push -u origin <epic-branch>" in text
    heads = re.findall(r"--head\s+([^\s`]+)", text)
    assert heads and all(h == "<epic-branch>" for h in heads), heads
    for verb in ("gh pr list", "gh pr create"):
        lines = [ln for ln in text.splitlines() if verb in ln and "--head" in ln]
        assert lines, f"orchestrator has no `{verb} ... --head` line"


def test_orchestrator_recovery_points_to_project_step5():
    _tracked()
    lines = ORCHESTRATOR.read_text(encoding="utf-8").splitlines()
    ptr = [n for n, ln in enumerate(lines) if "defined once in /project Step 5" in ln]
    assert ptr, "orchestrator recovery lost its pointer to /project Step 5"
    creates = [n for n, c in _commands(ORCHESTRATOR) if CREATE.match(c)]
    assert any(0 < c - p <= 6 for p in ptr for c in creates), (ptr, creates)


def test_no_peel_syntax_next_to_rev_parse():
    _tracked()
    lines = ORCHESTRATOR.read_text(encoding="utf-8").splitlines()
    peel = [n + 1 for n, ln in enumerate(lines) if "rev-parse" in ln and re.search(r"\^\{\w*\}", ln)]
    assert not peel, f"orchestrator rev-parse uses ^{{...}} peel syntax at lines {peel}"
    assert "rev-parse <epic-branch>~N" in "\n".join(lines)


def test_solution_points_to_project_step5():
    _tracked()
    text = SOLUTION.read_text(encoding="utf-8")
    assert "Step 6 of solution" not in text
    assert "`/project` Step 5" in text
    assert "If the directory already exists, the orchestrator reuses it" not in text
