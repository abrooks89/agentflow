"""Unit coverage for the escalation pair in `agentflow.status_writer` (gh#67).

`answer_escalation` has emitted a `session_escalation_answered` event since it was
written, and documents the side effect. Its sibling `open_escalation` documented the
same contract in the orchestrator skill's event table and emitted nothing, so the
decision log recorded when an escalation was ANSWERED and never when one was OPENED --
the half of the pair that explains why the run stopped.

These tests pin the emission and its summary shape, and bind the shape to the row in
`skills/orchestrator/SKILL.md` that promises it, so the table and the function cannot
drift apart again in either direction.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentflow.status_writer import (
    StatusState,
    answer_escalation,
    open_escalation,
)

SKILLS_ROOT = Path(__file__).resolve().parents[2]


def _state() -> StatusState:
    return StatusState(
        epic="Test Epic",
        plan="plan.md",
        started="2026-01-01T00:00:00Z",
        status="running",
        current_phase="p1",
    )


def _summaries(state: StatusState) -> list[str]:
    return [d.summary for d in state.decisions]


def test_open_escalation_records_the_escalation_and_halts_on_it() -> None:
    state = _state()
    esc = open_escalation(state, task=3, question="Which schema wins?")
    assert state.escalations == [esc]
    assert (esc.id, esc.task, esc.question, esc.answer) == ("e1", 3, "Which schema wins?", None)
    assert state.status == "escalation"


def test_open_escalation_emits_the_documented_event() -> None:
    """The gh#67 second defect. One event, matching `<id> task <N>: <q>`."""
    state = _state()
    open_escalation(state, task=7, question="Which schema wins?")
    assert _summaries(state) == ["session_escalation_opened: e1 task 7: Which schema wins?"]


def test_the_opened_event_carries_the_session_event_shape_its_sibling_uses() -> None:
    """`append_session_event` writes tier 1 and task 0 for every session-level event --
    the escalation's own task number lives in the summary, not in the Decision row. An
    emit that hand-rolled the Decision instead of going through the helper would diverge
    here and the dashboard would sort it differently from every other session event."""
    state = _state()
    open_escalation(state, task=7, question="q")
    (decision,) = state.decisions
    assert (decision.tier, decision.task) == (1, 0)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", decision.at), decision.at


def test_a_caller_supplied_id_is_the_id_in_the_event() -> None:
    """The event is the only place a reader learns which escalation opened, so it must
    carry the id that was actually recorded, not a regenerated one."""
    state = _state()
    open_escalation(state, task=0, question="q", eid="boot-1")
    assert state.escalations[0].id == "boot-1"
    assert _summaries(state) == ["session_escalation_opened: boot-1 task 0: q"]


def test_each_escalation_gets_its_own_event() -> None:
    state = _state()
    open_escalation(state, task=0, question="first")
    open_escalation(state, task=2, question="second")
    assert _summaries(state) == [
        "session_escalation_opened: e1 task 0: first",
        "session_escalation_opened: e2 task 2: second",
    ]


def test_open_then_answer_leaves_a_readable_pair_in_the_decision_log() -> None:
    """The point of emitting at all: an epic that stopped and restarted shows both ends
    of the stop, in order."""
    state = _state()
    esc = open_escalation(state, task=4, question="Which schema wins?")
    answer_escalation(state, esc.id, "the new one")
    assert _summaries(state) == [
        "session_escalation_opened: e1 task 4: Which schema wins?",
        "session_escalation_answered: e1 answered",
    ]
    assert state.status == "running"


def test_a_multi_line_question_collapses_to_one_line_in_the_decision_log() -> None:
    """SKILL.md:294 and :352 both pass a whole multi-line ESCALATION block as the
    question. `render_body` writes one line per decision, so raw newlines would spill
    into the markdown body and a `- ` line inside the block would render as a sibling
    bullet in the Decisions log -- one escalation eating several rows of a 20-row window.
    Every other session event is one line; this one is too."""
    state = _state()
    open_escalation(
        state,
        task=0,
        question="ESCALATION: Target tree unresolvable.\nNo worktree to build in.\n- add `worktree:`",
    )
    (summary,) = _summaries(state)
    assert "\n" not in summary
    assert summary == (
        "session_escalation_opened: e1 task 0: ESCALATION: Target tree unresolvable. "
        "No worktree to build in. - add `worktree:`"
    )


def test_the_escalation_record_keeps_the_question_verbatim() -> None:
    """The collapse is for the one-line decision log ONLY. `escalations[].question` is
    what the operator reads when deciding how to unblock, and what the answer path echoes
    back, so it must keep the block's formatting exactly as the caller wrote it."""
    block = "ESCALATION: pick one.\n  - option A\n  - option B\n"
    state = _state()
    esc = open_escalation(state, task=2, question=block)
    assert esc.question == block
    assert state.escalations[0].question == block


def test_the_emit_does_not_persist_anything_itself() -> None:
    """Both helpers document that persistence is the caller's job. A write here would
    turn every escalation into a filesystem operation on the path that has already
    failed once."""
    state = _state()
    open_escalation(state, task=0, question="q")
    assert state.decisions and state.escalations  # state changed
    # No path was ever handed to the function, so there is nothing it could have written.
    assert "status_path" not in open_escalation.__code__.co_varnames


def test_the_event_table_and_the_function_agree_on_the_summary_template() -> None:
    """Binds the emit to the row that promises it. The orchestrator skill's
    event-emission table is where a reader learns this event exists; if the table's
    template changes and the function does not, this fails instead of a dashboard
    quietly showing something else."""
    table = (SKILLS_ROOT / "orchestrator" / "SKILL.md").read_text(encoding="utf-8")
    rows = [ln for ln in table.split("\n") if "`escalation_opened:`" in ln]
    assert len(rows) == 1, f"expected exactly one escalation_opened table row, got {rows}"
    assert "inside open_escalation()" in rows[0], rows[0]
    assert "`<id> task <N>: <q>`" in rows[0], rows[0]

    state = _state()
    open_escalation(state, task=9, question="Q?")
    assert _summaries(state) == ["session_escalation_opened: e1 task 9: Q?"]


def test_answer_escalation_still_raises_on_an_unknown_id() -> None:
    """Regression fence around the sibling this change was modelled on: adding an emit to
    `open_escalation` must not have changed how the answer side reports a bad id."""
    state = _state()
    with pytest.raises(KeyError):
        answer_escalation(state, "nope", "answer")


# --------------------------------------------------------------------------- #
# gh#336 - a BOM'd board is now READ, which sends it into every writer. Each read-modify-write
# has to strip the BOM before its anchored frontmatter match, or the old block survives as body.
# --------------------------------------------------------------------------- #
_BOM = b"\xef\xbb\xbf"
_BOARD = (
    "---\nepic: parent epic\nplan: /plans/parent.md\nstarted: 2026-01-01T00:00:00+00:00\n"
    "status: running\ncurrent_phase: phase-1\npaused: false\ntasks:\n  1:\n    state: done\n"
    "---\n\nhuman notes\n"
)


def _bom_board(tmp_path: Path) -> Path:
    path = tmp_path / "parent.status.md"
    path.write_bytes(_BOM + _BOARD.encode("utf-8"))
    return path


def _assert_one_frontmatter_and_no_bom(path: Path) -> None:
    raw = path.read_bytes()
    assert not raw.startswith(_BOM), raw[:20]
    text = raw.decode("utf-8")
    assert len(re.findall(r"^epic:", text, re.MULTILINE)) == 1, text
    assert "\ufeff" not in text
    assert "human notes" in text


def test_pulse_on_a_BOM_prefixed_board_leaves_ONE_frontmatter_block(tmp_path: Path) -> None:
    """The reviewed repro: `pulse` reads (now successfully) and rewrites. Unstripped, the anchored
    sub missed and the old frontmatter was kept as body - `epic:` twice in one board."""
    from agentflow.status_writer import pulse, read_status

    board = _bom_board(tmp_path)
    pulse(board)
    _assert_one_frontmatter_and_no_bom(board)
    assert read_status(board).last_wake is not None


def test_a_wave_event_note_on_a_BOM_prefixed_board_leaves_ONE_frontmatter_block(
    tmp_path: Path,
) -> None:
    """The other live writer that reaches `write_status` through a successful `read_status`."""
    from agentflow.status_writer import read_status
    from agentflow.wave_event import report_note

    board = _bom_board(tmp_path)
    report_note(board, summary="a note")
    _assert_one_frontmatter_and_no_bom(board)
    assert [d.summary for d in read_status(board).decisions][-1].endswith("a note")


def test_lazy_project_backfill_reads_a_BOM_prefixed_plan(tmp_path: Path) -> None:
    from agentflow.status_writer import _lazy_backfill_project

    plan = tmp_path / "plan.md"
    plan.write_bytes(_BOM + b"---\nslug: p\nproject: alpha\n---\n\nplan body\n")
    state = _state()
    state.plan = str(plan)
    _lazy_backfill_project(state)
    assert state.project == "alpha"


def test_read_doc_reads_a_BOM_prefixed_doc(tmp_path: Path) -> None:
    """`mark_done`'s title cross-check reads plans through this; a BOM'd one contributed no name."""
    from agentflow.status_writer import _read_doc

    doc = tmp_path / "plan.md"
    doc.write_bytes(_BOM + b"---\nepic: The Title\n---\n\n# Heading\n")
    result = _read_doc(doc)
    assert result is not None
    fm, body = result
    assert fm == {"epic": "The Title"}
    assert body.lstrip().startswith("# Heading")
