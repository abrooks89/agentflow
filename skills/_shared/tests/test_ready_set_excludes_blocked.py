"""A blocked task is never ready (gh#703).

`blocked` means a human or a handler owns the task. Every ready-set definition the orchestrator
can reach - the library function, the bootstrap subagent's section 4 and the SKILL's inline
Step 6 fallback - must leave it out, or the next wake re-dispatches escalated work and answers a
tier-3 question without the human.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from agentflow.plan_parser import compute_ready_set

SKILLS_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = SKILLS_ROOT / "orchestrator" / "SKILL.md"
BOOTSTRAP = SKILLS_ROOT / "orchestrator" / "agents" / "orc-bootstrap.md"


def _plan():
    return N(tasks={1: N(deps=[]), 2: N(deps=[1]), 3: N(deps=[])})


def _sets(state: dict[int, str]) -> tuple[set[int], set[int]]:
    return (
        {t for t, s in state.items() if s == "done"},
        {t for t, s in state.items() if s == "blocked"},
    )


def test_issue_repro_blocked_task_is_not_ready():
    done, blocked = _sets({1: "done", 2: "blocked", 3: "in_flight"})
    # in_flight stays in the set; callers skip it. Blocked 2 must not be there.
    assert compute_ready_set(_plan(), done, blocked=blocked) == [3]


def test_unblocked_task_with_deps_met_is_ready():
    done, blocked = _sets({1: "done", 2: "pending", 3: "pending"})
    assert compute_ready_set(_plan(), done, blocked=blocked) == [2, 3]


def test_blocked_is_required_so_a_caller_cannot_forget_it():
    # A defaulted parameter would let an old two-argument call silently keep the bug.
    with pytest.raises(TypeError):
        compute_ready_set(_plan(), {1})  # type: ignore[call-arg]


def _section(text: str, start: str, end_pattern: str) -> str:
    body = text.split(start, 1)[1]
    return re.split(end_pattern, body, maxsplit=1)[0]


def test_bootstrap_section_4_excludes_blocked():
    section = _section(BOOTSTRAP.read_text(encoding="utf-8"), "### 4. Compute ready set", r"\n---\n")
    assert 'blocked = {' in section and '"blocked"' in section
    assert "task_id not in blocked" in section


def test_skill_step6_fallback_passes_blocked():
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    step6 = _section(text, "### Step 6: Compute ready set", r"\n### Step 7")
    assert re.search(r"compute_ready_set\(plan, done, blocked=blocked\)", step6)
    assert 'ts.state == "blocked"' in step6


def test_skill_no_longer_concedes_blocked_tasks_become_ready():
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    assert "marks a `blocked` task ready again" not in text


# Answering an escalation flips the epic back to running; it never touches a task. So a
# deadlock over blocked tasks recurs on every wake until the operator resets each task, and
# both deadlock texts must name that reset step rather than imply the answer suffices.
RESET_STEP = "state.tasks[<tid>].state"


def _names_reset(paragraph: str) -> None:
    flat = " ".join(paragraph.split())
    assert "does not reset" in flat, "must say answering the escalation does not reset the task"
    assert RESET_STEP in flat and "`ready`" in flat, "must name the reset: state.tasks[<tid>].state to ready"
    assert "status.md frontmatter" in flat, "must say where the reset is made"


def test_bootstrap_deadlock_question_names_the_reset_step():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    para = _section(text, "If `ready` is empty AND no task is `in_flight`", r"\n\s*\n")
    _names_reset(para)
    assert "waiting on its escalation" not in para


def test_skill_step6_deadlock_line_names_the_reset_step():
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    step6 = _section(text, "### Step 6: Compute ready set", r"\n### Step 7")
    _names_reset(_section(step6, "Deadlock check:", r"\n\s*\n"))


def test_persistent_failure_handler_sets_in_flight_before_redispatch():
    # The reporter already wrote `blocked`, and blocked is sticky, so a bare "re-dispatch once"
    # never leaves the ready-set filter.
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "verifier_blocker_persistent\"` / `test_failure_persistent" in ln)
    assert re.search(r"state = \"in_flight\".*then re-dispatch", line), line


def test_reconcile_reissue_never_passes_a_raw_commit():
    # gh#706: the re-issued state write must apply wave_group.js's commit rule, not echo the return.
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "re-issue that task's state write" in ln)
    flat = " ".join(line.split())
    for needle in ("trimmed", "7 to 64 hex", "`none`", "`null`", "`n/a`", "never passed raw"):
        assert needle in flat, needle
