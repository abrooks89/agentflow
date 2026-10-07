"""The status board follows the plan on every wake.

The ready set comes from `plan.tasks`; every state write goes through `state.tasks`, which
bootstrap fills once. A plan edited after bootstrap used to desync from its board: an added task
crashed the ready-task write with `KeyError`, and a removed task deadlocked the epic behind an
escalation describing dependency edges that did not exist. `reconcile_board` is the one helper
both the orc-bootstrap path and the inline fallback call before any ready set is computed.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from agentflow import wave_event
from agentflow.plan_parser import (
    ORPHAN_ESCALATION_TEMPLATE,
    BoardReconcile,
    compute_ready_set,
    orphan_escalation_text,
    parse_plan,
    reconcile_board,
)
from agentflow.status_writer import (
    ORPHAN_RESOLVED_ANSWER,
    StatusState,
    TaskState,
    open_escalation,
    read_status,
    sync_orphan_escalations,
    write_status,
)

SKILLS_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = SKILLS_ROOT / "orchestrator" / "SKILL.md"
BOOTSTRAP = SKILLS_ROOT / "orchestrator" / "agents" / "orc-bootstrap.md"

EXACT = (
    "task 3 is on the board but no longer in the plan: "
    "remove it from status.md or restore it to the plan"
)

_PLAN_HEAD = """---
epic: notes-api
spec: specs/notes-api.md
slug: notes-api
phases: [schema, api]
test_commands:
  unit: pytest -q
tasks:
"""


def _plan_file(tmp_path: Path, name: str, tasks: str) -> Path:
    path = tmp_path / name
    path.write_text(_PLAN_HEAD + tasks + "---\n\n# Plan\n", encoding="utf-8")
    return path


THREE = """  1: {phase: schema, deps: []}
  2: {phase: api, deps: [1]}
  3: {phase: api, deps: [2]}
"""


def _bootstrapped_board(tmp_path: Path, plan_path: Path) -> Path:
    """A board built the way SKILL bootstrap step 5 builds it from the plan."""
    plan = parse_plan(plan_path)
    tasks = {
        tid: TaskState(state="ready" if not t.deps else "pending", phase=t.phase)
        for tid, t in plan.tasks.items()
    }
    path = tmp_path / "status" / "notes-api.status.md"
    path.parent.mkdir()
    write_status(
        path,
        StatusState(
            epic="notes-api",
            plan=str(plan_path),
            started="2026-01-01T00:00:00+00:00",
            status="running",
            current_phase="schema",
            tasks=tasks,
        ),
    )
    return path


def _sets(state: StatusState) -> tuple[set[int], set[int]]:
    return (
        {t for t, ts in state.tasks.items() if ts.state == "done"},
        {t for t, ts in state.tasks.items() if ts.state == "blocked"},
    )


# --------------------------------------------------------------------------- #
# Repro 1: the plan gains a task after bootstrap
# --------------------------------------------------------------------------- #
def test_repro_added_task_joins_the_board_pending_with_its_phase_and_a_decision(tmp_path):
    status_path = _bootstrapped_board(tmp_path, _plan_file(tmp_path, "plan.md", THREE))
    edited = parse_plan(
        _plan_file(tmp_path, "edited.md", THREE + "  4: {phase: api, deps: []}\n")
    )
    state = read_status(status_path)
    before = len(state.decisions)

    result = reconcile_board(edited, state)

    assert result == BoardReconcile(added=(4,))
    assert state.tasks[4].state == "pending"
    assert state.tasks[4].phase == "api"
    new = state.decisions[before:]
    assert len(new) == 1 and new[0].task == 4 and "task 4" in new[0].summary


def test_repro_added_task_is_ready_and_writable_after_reconcile(tmp_path):
    status_path = _bootstrapped_board(tmp_path, _plan_file(tmp_path, "plan.md", THREE))
    edited = parse_plan(
        _plan_file(tmp_path, "edited.md", THREE + "  4: {phase: api, deps: []}\n")
    )
    state = read_status(status_path)
    if reconcile_board(edited, state).added:
        write_status(status_path, state)

    done, blocked = _sets(state)
    ready = compute_ready_set(edited, done, blocked=blocked)
    assert ready == [1, 4]
    for tid in ready:  # Step 2's ready-task write: raised KeyError: 4 before reconcile existed
        state.tasks[tid].state = "ready"
    write_status(status_path, state)

    # The wave-event CLI path refused "unknown task 4" and exited 2.
    argv = ["task-state", "--status", str(status_path), "--task", "4", "--state", "in_flight"]
    assert wave_event.main(argv) == 0
    assert read_status(status_path).tasks[4].state == "in_flight"


# --------------------------------------------------------------------------- #
# Repro 2: the plan loses a task after bootstrap
# --------------------------------------------------------------------------- #
def test_repro_removed_open_task_is_a_named_escalation_not_a_deadlock(tmp_path):
    status_path = _bootstrapped_board(tmp_path, _plan_file(tmp_path, "plan.md", THREE))
    removed = parse_plan(_plan_file(tmp_path, "removed.md", THREE.rsplit("  3:", 1)[0]))
    state = read_status(status_path)
    state.tasks[1].state = "done"
    state.tasks[2].state = "done"
    before = len(state.decisions)

    result = reconcile_board(removed, state)

    assert result == BoardReconcile(orphaned_open=(3,))
    assert state.decisions[before:] == []
    assert state.tasks[3].state == "pending"  # reported, never removed
    text = orphan_escalation_text(3)
    assert text == EXACT
    assert "dependency" not in text.lower() and "deadlock" not in text.lower()

    assert sync_orphan_escalations(state, result) is True
    assert [(e.task, e.question, e.answer) for e in state.escalations] == [(3, EXACT, None)]
    assert state.status == "escalation"


# --------------------------------------------------------------------------- #
# Orphan escalations across wakes: one per task, closed once the operator fixes it
# --------------------------------------------------------------------------- #
def _wake(plan, state: StatusState) -> BoardReconcile:
    """Step 2.0's reconcile-then-sync sequence, as the SKILL runs it."""
    board = reconcile_board(plan, state)
    sync_orphan_escalations(state, board)
    return board


def _open_orphans(state: StatusState) -> list[int]:
    return [
        e.task
        for e in state.escalations
        if e.answer is None and e.question == orphan_escalation_text(e.task)
    ]


def test_a_second_wake_over_the_same_orphan_does_not_stack_a_duplicate():
    plan = _plan_obj({1: "schema"})
    state = _state({1: "done", 3: "pending"})
    _wake(plan, state)
    board = reconcile_board(plan, state)
    assert sync_orphan_escalations(state, board) is False
    assert _open_orphans(state) == [3]
    assert len(state.escalations) == 1


def test_dedup_holds_across_a_write_and_reread_of_the_board(tmp_path):
    # Each wake reads the board from disk, so the escalation's task id must
    # round-trip as the same type the reconcile compares against.
    plan = _plan_obj({1: "schema"})
    state = _state({1: "done", 3: "pending"})
    _wake(plan, state)
    path = tmp_path / "board.status.md"
    write_status(path, state)

    reread = read_status(path)
    board = reconcile_board(plan, reread)
    assert sync_orphan_escalations(reread, board) is False
    assert _open_orphans(reread) == [3]
    assert len(reread.escalations) == 1


def test_fixing_the_board_closes_the_orphan_escalation_and_resumes_the_epic():
    plan = _plan_obj({1: "schema"})
    state = _state({1: "done", 3: "pending"})
    _wake(plan, state)
    assert state.status == "escalation"

    del state.tasks[3]  # the operator removes the task from status.md
    board = reconcile_board(plan, state)
    assert board == BoardReconcile()
    assert sync_orphan_escalations(state, board) is True

    assert _open_orphans(state) == []
    assert state.escalations[0].answer == ORPHAN_RESOLVED_ANSWER
    assert state.status == "running"


def test_restoring_the_task_to_the_plan_also_closes_it():
    state = _state({1: "done", 3: "pending"})
    _wake(_plan_obj({1: "schema"}), state)
    _wake(_plan_obj({1: "schema", 3: "api"}), state)
    assert _open_orphans(state) == [] and state.status == "running"


def test_closing_an_orphan_leaves_an_unrelated_open_escalation_holding_the_epic():
    plan = _plan_obj({1: "schema"})
    state = _state({1: "done", 3: "pending"})
    open_escalation(state, task=1, question="an unrelated question")
    _wake(plan, state)
    del state.tasks[3]
    _wake(plan, state)
    assert [e.question for e in state.escalations if e.answer is None] == ["an unrelated question"]
    assert state.status == "escalation"


@pytest.mark.parametrize("held", ["blocked", "done_on_hold", "preflight_audit_blocked"])
def test_opening_an_orphan_escalation_keeps_a_blocked_or_held_status(held):
    state = _state({1: "done", 3: "pending"})
    state.status = held
    _wake(_plan_obj({1: "schema"}), state)
    assert _open_orphans(state) == [3]
    assert state.status == held


def test_board_only_done_task_is_left_alone_without_escalation():
    plan = _plan_obj({1: "schema"})
    state = _state({1: "done", 2: "done"})
    result = reconcile_board(plan, state)
    assert result == BoardReconcile(orphaned_done=(2,))
    assert state.tasks[2].state == "done"
    assert state.decisions == [] and state.escalations == []


@pytest.mark.parametrize("board_state", ["blocked", "in_flight", "ready", "code_review"])
def test_every_non_done_board_only_state_is_orphaned_open(board_state):
    result = reconcile_board(_plan_obj({1: "schema"}), _state({1: "done", 2: board_state}))
    assert result == BoardReconcile(orphaned_open=(2,))


def test_identical_sets_are_a_no_op():
    state = _state({1: "done", 2: "pending"})
    result = reconcile_board(_plan_obj({1: "schema", 2: "api"}), state)
    assert result == BoardReconcile()
    assert state.decisions == []
    assert set(state.tasks) == {1, 2}


def _plan_obj(phases: dict[int, str]):
    from agentflow.plan_parser import PlanFrontmatter, Task

    return PlanFrontmatter(
        epic="e",
        spec="s",
        phases=sorted(set(phases.values())),
        tasks={tid: Task(id=tid, phase=p, deps=[]) for tid, p in phases.items()},
        test_commands={},
    )


def _state(tasks: dict[int, str]) -> StatusState:
    return StatusState(
        epic="e",
        plan="p.md",
        started="2026-01-01T00:00:00+00:00",
        status="running",
        current_phase="schema",
        tasks={tid: TaskState(state=s) for tid, s in tasks.items()},
    )


# --------------------------------------------------------------------------- #
# The prose that drives the orchestrator calls the helper where the decision says
# --------------------------------------------------------------------------- #
def _flat(text: str) -> str:
    return " ".join(text.split())


SYNC_CALL = "sync_orphan_escalations(state, board)"


def test_step2_reconciles_the_held_state_before_dispatching_orc_bootstrap():
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    step2 = text.split("### Step 2:", 1)[1].split("## Fallback: inline state-load", 1)[0]
    step20 = step2.split("Dispatch the `orc-bootstrap` named subagent.", 1)[0]
    rec = step20.index("board = reconcile_board(plan, state)")
    assert rec < step20.index(SYNC_CALL) < step20.index("write_status(status_path, state)")
    assert "dispatch_named_subagent(" not in step20
    # Step 1's unpersisted mutations live in `state`; a re-read here would drop them.
    assert "read_status(" not in step20


def test_inline_fallback_reconciles_before_any_ready_set():
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    fb = text.split("def _fallback_inline_state_load", 1)[1].split("**Fallback trigger", 1)[0]
    assert fb.index("board = reconcile_board(plan, state)") < fb.index(SYNC_CALL)


def test_documented_helper_calls_bind_to_the_real_signatures():
    inspect.signature(reconcile_board).bind(object(), object())
    inspect.signature(sync_orphan_escalations).bind(object(), object())


def test_skill_states_the_mid_epic_plan_rule():
    flat = _flat(ORCHESTRATOR.read_text(encoding="utf-8"))
    assert "A plan may gain tasks mid-epic" in flat
    assert "Removing a task from the plan also requires removing it from `status.md`" in flat


def test_bootstrap_reports_board_only_tasks_with_the_exact_text_before_deadlock():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    flat = _flat(text)
    exact = ORPHAN_ESCALATION_TEMPLATE.format(tid="<id>")
    assert exact in flat
    assert flat.index(exact) < flat.index("If `ready` is empty AND no task is `in_flight`")
    para = re.search(r"\*\*Board-only tasks come before the deadlock rule\.\*\*[^\n]*", text)
    assert para and "never described as an unsatisfied dependency edge" in para.group(0)
    assert "five keys" in para.group(0)
