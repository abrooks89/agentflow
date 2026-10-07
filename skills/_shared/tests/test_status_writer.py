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

import os
import re
import subprocess
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


# --------------------------------------------------------------------------- #
# mark-done truth: a foreign spec is refused (gh#888), the duplicate WARNING fires exactly when
# another board keyed as the slug exists, so the survey refuses the epic (gh#889, gh#1072), and a
# fresh record carries the spec title (gh#892).
# --------------------------------------------------------------------------- #
_MD_CUTOFF = "2020-01-01T00:00:00+00:00"
_MD_PROJECT = "toy"
_MD_SLUG = "add-widget"
_MD_TITLE = "Add the widget"


def _md_spec(root: Path, *, project: str | None = _MD_PROJECT, title: str | None = _MD_TITLE) -> Path:
    """A spec under `toy`, approved, optionally without its `project:` or `title:` field."""
    path = root / "projects" / _MD_PROJECT / "design" / "specs" / f"2026-10-01-{_MD_SLUG}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = "current_state: approved-for-autonomous\n"
    if title is not None:
        fm = f"title: {title}\n" + fm
    if project is not None:
        fm += f"project: {project}\n"
    path.write_text(f"---\n{fm}---\n\nbody\n", encoding="utf-8")
    for proj in (_MD_PROJECT, "other"):
        (root / "projects" / proj / "status").mkdir(parents=True, exist_ok=True)
    return path


def _md_cli(root: Path, spec: Path, project: str = _MD_PROJECT) -> int:
    from agentflow.status_writer import main

    return main(
        [
            "mark-done", "--data-root", str(root), "--project", project, "--slug", _MD_SLUG,
            "--plan", str(spec), "--shipped", "ddd (PR 4)", "--actor", "jr-dev-1",
        ]
    )


def _md_survey(root: Path, project: str = _MD_PROJECT) -> dict:
    from agentflow.epic_survey import survey_epics

    recs = survey_epics(root, _MD_CUTOFF, include_archived=True, project=project)
    return {r.slug: r for r in recs}


def test_mark_done_refuses_a_spec_whose_project_field_names_another_project(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """gh#888 repro: a `toy` spec marked done under `other` wrote a phantom record and exit 0."""
    from agentflow.status_writer import merge_record_path

    spec = _md_spec(tmp_path)
    capsys.readouterr()
    assert _md_cli(tmp_path, spec, project="other") == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: ") and "'toy'" in err and "'other'" in err, err
    assert not merge_record_path(tmp_path, "other", _MD_SLUG).exists()
    assert _MD_SLUG not in _md_survey(tmp_path, "other")
    # Control: the same spec under its own project is accepted.
    assert _md_cli(tmp_path, spec) == 0
    assert _md_survey(tmp_path)[_MD_SLUG].lifecycle_state == "done"


def test_mark_done_refuses_a_spec_with_no_project_field_from_another_projects_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """gh#888 remainder: with no `project:` field the spec's location decides ownership, so a
    `toy` spec marked done under `other` is refused before any write instead of leaving a
    phantom done record on `other`'s board."""
    from agentflow.status_writer import merge_record_path

    spec = _md_spec(tmp_path, project=None)
    capsys.readouterr()
    assert _md_cli(tmp_path, spec, project="other") == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: ") and "no project: field" in err and "'toy'" in err, err
    assert list((tmp_path / "projects" / "other" / "status").iterdir()) == []
    assert not merge_record_path(tmp_path, "other", _MD_SLUG).exists()
    assert _MD_SLUG not in _md_survey(tmp_path, "other")


def _md_cli_no_data_root(spec: Path) -> int:
    from agentflow.status_writer import main

    return main(
        [
            "mark-done", "--project", _MD_PROJECT, "--slug", _MD_SLUG,
            "--plan", str(spec), "--shipped", "ddd (PR 4)",
        ]
    )


def test_mark_done_without_data_root_refuses_an_unresolvable_config(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh#1161: with no --data-root, a config that does not load refuses like every other
    mark-done refusal - one stderr line that keeps the remediation, exit 2, nothing on stdout."""
    import agentflow.config as config_mod

    exc = config_mod.ConfigError("environment.data_root", "environment.md", "x", "set it")

    def _raise() -> None:
        raise exc

    monkeypatch.setattr(config_mod, "load_environment", _raise)
    spec = _md_spec(tmp_path)
    capsys.readouterr()
    assert _md_cli_no_data_root(spec) == 2
    out, err = capsys.readouterr()
    assert err == (
        "refused: cannot resolve data_root: environment.md: [environment.data_root] x "
        "remediation: set it\n"
    ), err
    assert out == ""


def test_mark_done_without_data_root_refuses_an_unreadable_environment_file(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh#1161: an environment.md that is not UTF-8 fails before any ConfigError; it refuses the
    same way and names the file, since a bare UnicodeDecodeError names none."""
    import agentflow.config as config_mod

    def _raise() -> None:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(config_mod, "load_environment", _raise)
    monkeypatch.setattr(config_mod, "environment_path", lambda *a, **k: Path("environment.md"))
    spec = _md_spec(tmp_path)
    capsys.readouterr()
    assert _md_cli_no_data_root(spec) == 2
    out, err = capsys.readouterr()
    assert err == (
        "refused: cannot resolve data_root: environment.md: not valid UTF-8 "
        "(invalid start byte at byte 0)\n"
    ), err
    assert out == ""


def test_mark_done_accepts_a_spec_with_no_project_field_under_its_own_design_tree(
    tmp_path: Path,
) -> None:
    """Control: absence of `project:` alone is not a refusal - a pre-field spec that lives in
    the project's own `design/` tree is still marked done there."""
    from agentflow.status_writer import merge_record_path, read_status

    spec = _md_spec(tmp_path, project=None)
    assert _md_cli(tmp_path, spec) == 0
    assert read_status(merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)).status == "done"
    assert _md_survey(tmp_path)[_MD_SLUG].lifecycle_state == "done"


@pytest.mark.parametrize("pass_old_path", [True, False])
def test_mark_done_accepts_a_no_project_spec_in_a_terminal_partition(
    tmp_path: Path, pass_old_path: bool
) -> None:
    """`done/` is still inside `projects/<project>/design/`, whether `resolve_plan` followed the
    spec there from its old path or the lane passed the partition path directly."""
    from agentflow.status_writer import merge_record_path, read_status

    spec = _md_spec(tmp_path, project=None)
    filed = spec.parent / "done" / spec.name
    filed.parent.mkdir()
    spec.rename(filed)
    assert _md_cli(tmp_path, spec if pass_old_path else filed) == 0
    assert read_status(merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)).status == "done"


def test_mark_done_refuses_a_no_project_spec_outside_the_data_root(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Strict rule: a spec with no `project:` that is not under the project's `design/` tree is
    refused even when it is in no other project's tree either - nothing says it is `toy`'s."""
    from agentflow.status_writer import merge_record_path

    root = tmp_path / "root"
    inside = _md_spec(root, project=None)
    stray = tmp_path / "elsewhere" / "specs" / inside.name
    stray.parent.mkdir(parents=True)
    inside.rename(stray)
    capsys.readouterr()
    assert _md_cli(root, stray) == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: ") and "is outside the data root" in err, err
    assert not merge_record_path(root, _MD_PROJECT, _MD_SLUG).exists()


def test_mark_done_refuses_a_no_project_spec_under_a_different_case_project(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Case-sensitive like the `project:` check: on a case-insensitive filesystem `Toy` reaches
    `projects/toy`, but the name on disk is `toy`, so `--project Toy` does not own the spec."""
    from agentflow.status_writer import merge_record_path

    spec = _md_spec(tmp_path, project=None)
    capsys.readouterr()
    assert _md_cli(tmp_path, spec, project=_MD_PROJECT.upper()) == 2
    # A case-sensitive filesystem refuses earlier (no `TOY/status`); a case-insensitive one
    # reaches the ownership check. Either way nothing is written.
    err = capsys.readouterr().err
    assert "no project: field" in err or "status dir does not exist" in err, err
    assert not merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG).exists()


def test_mark_done_accepts_a_no_project_spec_under_a_junctioned_design_tree(
    tmp_path: Path,
) -> None:
    """The project's `design/` may itself be a link to elsewhere; the spec is still its own."""
    from agentflow.status_writer import merge_record_path, read_status

    spec = _md_spec(tmp_path, project=None)
    design = tmp_path / "projects" / _MD_PROJECT / "design"
    real = tmp_path / "real-design"
    design.rename(real)
    try:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(design), str(real)],
                           check=True, capture_output=True)
        else:
            design.symlink_to(real, target_is_directory=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"cannot create a directory link here: {exc}")
    assert _md_cli(tmp_path, spec) == 0
    assert read_status(merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)).status == "done"


def _md_board(root: Path, rel: str, status: str = "running") -> Path:
    path = root / "projects" / _MD_PROJECT / "status" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nepic: {_MD_SLUG}\nplan: p\nstarted: s\nstatus: {status}\ncurrent_phase: p1\n---\n",
        encoding="utf-8",
    )
    return path


def _md_set_mtime(path: Path, mtime_ns: int) -> None:
    os.utime(path, ns=(mtime_ns, mtime_ns))


_MD_HOUR_NS = 3600 * 1_000_000_000


@pytest.mark.parametrize(
    "rel",
    [
        f"{_MD_SLUG}/status.md",           # Convention 1
        f"aaa/{_MD_SLUG}.status.md",       # Convention 2, sorts before the record
        f"legacy/{_MD_SLUG}.status.md",    # Convention 2, sorts after the record
        f"zzz/{_MD_SLUG}/status.md",       # Convention 1, sorting last
    ],
)
@pytest.mark.parametrize("other_is_newer", [True, False], ids=["other-newer", "record-newest"])
def test_duplicate_warning_fires_for_any_other_board_and_the_survey_refuses_the_epic(
    tmp_path: Path, capsys: pytest.CaptureFixture, rel: str, other_is_newer: bool
) -> None:
    """gh#889, under the refuse rule: any second board keyed as the slug makes it a duplicate.

    Neither path, filename convention nor mtime picks a winner, so every case warns, names the
    other board, and the survey reads the epic as `DUPLICATE_STATUS_BOARDS` - not `done` -
    however recently mark-done wrote its record.
    """
    import time

    from agentflow.epic_survey import DUPLICATE_STATUS_BOARDS

    spec = _md_spec(tmp_path)
    other = _md_board(tmp_path, rel)
    _md_set_mtime(other, time.time_ns() + (_MD_HOUR_NS if other_is_newer else -_MD_HOUR_NS))
    capsys.readouterr()
    assert _md_cli(tmp_path, spec) == 0  # a warning never changes the exit code
    err = capsys.readouterr().err
    assert err.startswith("WARNING: ") and str(other) in err, err
    assert "duplicate status boards" in err, err
    rec = _md_survey(tmp_path)[_MD_SLUG]
    assert rec.lifecycle_state == DUPLICATE_STATUS_BOARDS


def test_no_duplicate_warning_when_the_record_is_the_only_board(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from agentflow.status_writer import merge_record_path

    spec = _md_spec(tmp_path)
    capsys.readouterr()
    assert _md_cli(tmp_path, spec) == 0
    assert "WARNING: " not in capsys.readouterr().err
    rec = _md_survey(tmp_path)[_MD_SLUG]
    assert rec.lifecycle_state == "done"
    assert Path(rec.source_path) == merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)


def test_find_duplicate_boards_names_every_other_board_and_agrees_with_the_survey(
    tmp_path: Path,
) -> None:
    """Several same-key boards: every one but the record is named, and the survey refuses."""
    from agentflow.epic_survey import DUPLICATE_STATUS_BOARDS
    from agentflow.status_writer import find_duplicate_boards

    spec = _md_spec(tmp_path)
    assert _md_cli(tmp_path, spec) == 0
    others = [
        _md_board(tmp_path, f"{_MD_SLUG}/status.md"),
        _md_board(tmp_path, f"aaa/{_MD_SLUG}.status.md"),
        _md_board(tmp_path, f"legacy/{_MD_SLUG}.status.md"),
    ]
    assert find_duplicate_boards(tmp_path, _MD_PROJECT, _MD_SLUG) == sorted(others)
    assert _md_survey(tmp_path)[_MD_SLUG].lifecycle_state == DUPLICATE_STATUS_BOARDS
    for b in others[1:]:
        b.unlink()
    assert find_duplicate_boards(tmp_path, _MD_PROJECT, _MD_SLUG) == [others[0]]
    others[0].unlink()
    assert find_duplicate_boards(tmp_path, _MD_PROJECT, _MD_SLUG) == []
    assert _md_survey(tmp_path)[_MD_SLUG].lifecycle_state == "done"


def test_a_fresh_record_carries_the_spec_title_and_the_survey_keeps_it(tmp_path: Path) -> None:
    """gh#892: the survey title flipped from the spec title to the slug once marked done."""
    from agentflow.status_writer import merge_record_path, read_status

    spec = _md_spec(tmp_path)
    assert _md_survey(tmp_path)[_MD_SLUG].title == _MD_TITLE  # control: before the record
    assert _md_cli(tmp_path, spec) == 0
    path = merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)
    assert read_status(path).epic == _MD_TITLE
    assert f"# {_MD_TITLE}: done\n" in path.read_text(encoding="utf-8")
    rec = _md_survey(tmp_path)[_MD_SLUG]
    assert (rec.title, rec.lifecycle_state) == (_MD_TITLE, "done")
    # The ownership cross-check accepts its own title-keyed record: a re-run is a no-op.
    before = path.read_bytes()
    assert _md_cli(tmp_path, spec) == 0
    assert path.read_bytes() == before


def test_a_fresh_record_for_an_untitled_spec_falls_back_to_the_slug(tmp_path: Path) -> None:
    from agentflow.status_writer import merge_record_path, read_status

    spec = _md_spec(tmp_path, title=None)
    assert _md_cli(tmp_path, spec) == 0
    path = merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)
    assert read_status(path).epic == _MD_SLUG
    assert f"# {_MD_SLUG}: done\n" in path.read_text(encoding="utf-8")


def _md_named_spec(root: Path, slug: str, title: str) -> Path:
    path = root / "projects" / _MD_PROJECT / "design" / "specs" / f"2026-10-01-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ncurrent_state: approved-for-autonomous\nproject: {_MD_PROJECT}\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    (root / "projects" / _MD_PROJECT / "status").mkdir(parents=True, exist_ok=True)
    return path


def _md_cli_slug(root: Path, spec: Path, slug: str) -> int:
    from agentflow.status_writer import main

    return main(
        [
            "mark-done", "--data-root", str(root), "--project", _MD_PROJECT, "--slug", slug,
            "--plan", str(spec), "--shipped", "eee (PR 5)",
        ]
    )


def test_no_title_warning_for_another_real_epic_that_shares_the_title(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Review of gh#892: records now carry spec titles, so two epics titled alike made the second
    mark-done tell a lane to fold or delete the first epic's correct record."""
    alpha = _md_named_spec(tmp_path, "alpha", "Polish pass")
    beta = _md_named_spec(tmp_path, "beta", "Polish pass")
    assert _md_cli_slug(tmp_path, alpha, "alpha") == 0
    capsys.readouterr()
    assert _md_cli_slug(tmp_path, beta, "beta") == 0
    assert "WARNING" not in capsys.readouterr().err
    # Control: a title-keyed board with no spec of its own is still a duplicate and still flagged.
    dup = tmp_path / "projects" / _MD_PROJECT / "status" / "polish-pass.status.md"
    dup.write_text(
        "---\nepic: Polish pass\nplan: p\nstarted: s\nstatus: running\ncurrent_phase: p1\n---\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert _md_cli_slug(tmp_path, beta, "beta") == 0
    err = capsys.readouterr().err
    assert "WARNING: " in err and str(dup) in err and "alpha" not in err, err


def test_a_rerun_after_the_spec_is_retitled_refreshes_the_record_not_refuses(
    tmp_path: Path,
) -> None:
    """Review of gh#892: the record's old title made a re-run refuse its own output."""
    from agentflow.status_writer import merge_record_path, read_status

    spec = _md_named_spec(tmp_path, _MD_SLUG, "Old title")
    assert _md_cli_slug(tmp_path, spec, _MD_SLUG) == 0
    spec.write_text(spec.read_text(encoding="utf-8").replace("Old title", "New title"),
                    encoding="utf-8")
    assert _md_cli_slug(tmp_path, spec, _MD_SLUG) == 0
    path = merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)
    assert read_status(path).epic == "New title"
    text = path.read_text(encoding="utf-8")
    assert "# New title: done\n" in text and "Old title" not in text
    before = path.read_bytes()
    assert _md_cli_slug(tmp_path, spec, _MD_SLUG) == 0
    assert path.read_bytes() == before


def test_a_retitled_rerun_still_refuses_a_running_board_for_another_epic(tmp_path: Path) -> None:
    """The ownership relaxation is for this writer's own done records only."""
    from agentflow.status_writer import merge_record_path

    spec = _md_named_spec(tmp_path, _MD_SLUG, "New title")
    path = merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)
    board = (
        "---\nepic: Old title\nplan: p\nstarted: s\nstatus: running\ncurrent_phase: p1\n---\n\nx\n"
    ).encode("utf-8")
    path.write_bytes(board)
    assert _md_cli_slug(tmp_path, spec, _MD_SLUG) == 2
    assert path.read_bytes() == board


@pytest.mark.parametrize(
    "epic_line", ["", "epic:\n", 'epic: ""\n'], ids=["absent", "null", "blank"]
)
def test_a_record_with_no_epic_is_filled_from_the_spec_title(tmp_path: Path, epic_line: str) -> None:
    """gh#1162: a record at this slug's path with no `epic:` names no other epic, so mark-done
    writes it and fills `epic:` from the spec title instead of refusing. A key with no value, or
    a blank one, counts as no `epic:`. The rest of the record is kept, not rebuilt."""
    import yaml

    from agentflow.status_writer import merge_record_path

    spec = _md_named_spec(tmp_path, _MD_SLUG, "New title")
    path = merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)
    path.write_text(
        f"---\n{epic_line}plan: p\nstarted: s\nstatus: running\ncurrent_phase: p1\n---\n\nx\n",
        encoding="utf-8",
    )
    assert _md_cli_slug(tmp_path, spec, _MD_SLUG) == 0
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text.split("---")[1])
    assert data["epic"] == "New title"
    assert (data["status"], data["shipped"]) == ("done", "eee (PR 5)")
    assert (data["plan"], data["started"]) == ("p", "s")
    assert text.rstrip().endswith("x"), text


# --------------------------------------------------------------------------- #
# mark-done finishes the orchestrator's running board (gh#890): no next wake, `ended` set, open
# escalations closed through `answer_escalation`, the AUTO block re-rendered - and a re-run is
# still byte-identical, the promise the mark_done docstring makes.
# --------------------------------------------------------------------------- #
_MD_SHIPPED_CLOSE = "closed by mark-done at merge: ddd (PR 4)"
_MD_NOW = "2026-10-03T12:00:00Z"
_MD_NOTES = "## My notes\n\nkept byte for byte\n"


def _md_running_board(root: Path, *, answered_first: bool = False) -> Path:
    """The orchestrator's running board for the epic, written by `write_status` itself."""
    from agentflow.status_writer import TaskState, merge_record_path, write_status

    state = StatusState(
        epic=_MD_TITLE,
        plan="solution/2026-10-01-add-widget.md",
        started="2026-10-01T00:00:00Z",
        status="running",
        current_phase="build",
        next_wake="2026-10-03T13:00:00Z",
        project=_MD_PROJECT,
        tasks={
            1: TaskState(state="done", phase="build", commit="c40a237a"),
            2: TaskState(
                state="in_flight", phase="build", role="builder",
                since="2026-10-03T11:00:00Z", model="opus",
            ),
            3: TaskState(state="pending", phase="build"),
        },
    )
    if answered_first:
        open_escalation(state, task=1, question="Which schema wins?")
        answer_escalation(state, "e1", "use A")
    open_escalation(state, task=2, question="Ship the flag on or off?")
    state.status = "running"
    path = merge_record_path(root, _MD_PROJECT, _MD_SLUG)
    write_status(path, state)
    path.write_text(path.read_text(encoding="utf-8") + _MD_NOTES, encoding="utf-8")
    return path


def _md_tail(text: str) -> str:
    end = "<!-- AUTO:end -->"
    return text[text.index(end) + len(end):]


def test_mark_done_finishes_the_running_orchestrator_board(tmp_path: Path) -> None:
    """gh#890 repro: the board flipped to done but kept its next wake, an open escalation and an
    AUTO block still showing `Escalations awaiting you` and `Next wake:`."""
    from agentflow.status_writer import read_status

    spec = _md_spec(tmp_path)
    path = _md_running_board(tmp_path)
    before_text = path.read_text(encoding="utf-8")
    before = read_status(path)
    # Control: the repro board really is in the shape the issue describes.
    assert before.next_wake and before.ended is None
    assert [(e.id, e.answer) for e in before.escalations] == [("e1", None)]
    assert "## Escalations awaiting you" in before_text and "## Next wake:" in before_text

    assert _md_cli(tmp_path, spec) == 0
    text = path.read_text(encoding="utf-8")
    after = read_status(path)
    assert (after.status, after.current_phase) == ("done", "done")
    assert after.next_wake is None
    assert after.ended
    assert [(e.id, e.answer) for e in after.escalations] == [("e1", _MD_SHIPPED_CLOSE)]
    # The close is on the decision log, through the same event answer_escalation always writes.
    assert after.decisions[-1].summary == "session_escalation_answered: e1 answered"
    assert len(after.decisions) == len(before.decisions) + 1
    assert "## Escalations awaiting you" not in text
    assert "## Next wake:" not in text
    assert after.tasks == before.tasks  # task 2 stays in_flight and task 3 pending
    assert "## No next wake (status: done)" in text
    assert _md_tail(text) == _md_tail(before_text)
    assert text.endswith(_MD_NOTES)


def test_mark_done_rerun_on_a_finished_board_changes_no_byte(tmp_path: Path) -> None:
    """Idempotence holds through the new work: `ended` is not moved, no escalation is re-closed,
    no decision event is added and the AUTO block renders the same."""
    from agentflow.status_writer import mark_done, read_status

    spec = _md_spec(tmp_path)
    path = _md_running_board(tmp_path)
    kwargs = dict(
        project=_MD_PROJECT, slug=_MD_SLUG, plan=spec, shipped="ddd (PR 4)", actor="jr-dev-1"
    )
    _, changed = mark_done(tmp_path, now=_MD_NOW, **kwargs)
    assert changed
    first = path.read_bytes()
    state = read_status(path)
    assert state.ended == _MD_NOW and state.next_wake is None
    assert state.escalations[0].answer == _MD_SHIPPED_CLOSE
    # The close is stamped with the board's end, not the wall clock, so a fixed `now` gives fixed
    # output and the log never shows a question closed after the board ended.
    assert state.decisions[-1].at == _MD_NOW

    _, changed = mark_done(tmp_path, now="2026-10-04T00:00:00Z", **kwargs)
    assert not changed
    assert path.read_bytes() == first
    assert _md_cli(tmp_path, spec) == 0  # the CLI passes no `now`; still nothing to change
    assert path.read_bytes() == first


def test_mark_done_sets_ended_over_a_null_ended(tmp_path: Path) -> None:
    """Real boards carry `ended: null`; that is an absent end, not a set one."""
    from agentflow.status_writer import mark_done, merge_record_path, read_status

    spec = _md_spec(tmp_path)
    path = merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)
    path.write_text(
        f"---\nepic: {_MD_SLUG}\nplan: p\nstarted: s\nstatus: running\ncurrent_phase: p1\n"
        "ended: null\n---\n\n# Hand notes\n",
        encoding="utf-8",
    )
    kwargs = dict(project=_MD_PROJECT, slug=_MD_SLUG, plan=spec, shipped="eee (PR 5)")
    mark_done(tmp_path, now=_MD_NOW, **kwargs)
    first = path.read_bytes()
    assert read_status(path).ended == _MD_NOW
    _, changed = mark_done(tmp_path, now="2026-10-04T00:00:00Z", **kwargs)
    assert not changed and path.read_bytes() == first


def test_mark_done_leaves_a_body_with_no_auto_block_alone(tmp_path: Path) -> None:
    """Only the orchestrator's AUTO block is re-rendered; a body without one is not ours."""
    from agentflow.status_writer import merge_record_path, read_status

    spec = _md_spec(tmp_path)
    path = merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)
    body = "# Hand notes\n\n## Next wake: whenever\n\nfree text\n"
    path.write_text(
        f"---\nepic: {_MD_SLUG}\nplan: p\nstarted: s\nstatus: running\ncurrent_phase: p1\n"
        f"next_wake: '2026-10-03T13:00:00Z'\n---\n\n{body}",
        encoding="utf-8",
    )
    assert _md_cli(tmp_path, spec) == 0
    text = path.read_text(encoding="utf-8")
    state = read_status(path)
    assert state.next_wake is None and state.ended  # the frontmatter is still finished
    assert text.endswith(f"---\n\n{body}")
    assert "AUTO:start" not in text


def test_mark_done_leaves_an_already_answered_escalation_untouched(tmp_path: Path) -> None:
    """Only open escalations are closed; an answered one keeps the operator's answer and gets no
    second event."""
    from agentflow.status_writer import read_status

    spec = _md_spec(tmp_path)
    path = _md_running_board(tmp_path, answered_first=True)
    before = read_status(path)
    assert [(e.id, e.answer) for e in before.escalations] == [("e1", "use A"), ("e2", None)]

    assert _md_cli(tmp_path, spec) == 0
    after = read_status(path)
    assert [(e.id, e.answer) for e in after.escalations] == [
        ("e1", "use A"),
        ("e2", _MD_SHIPPED_CLOSE),
    ]
    answered = [d.summary for d in after.decisions if "escalation_answered" in d.summary]
    assert answered == [
        "session_escalation_answered: e1 answered",
        "session_escalation_answered: e2 answered",
    ]


# --------------------------------------------------------------------------- #
# mark-done follows a spec reconcile filed into done/ or archived/, and refreshes a record's
# dead `plan:` (gh#891). A retry after reconcile used to refuse with a bare ENOENT, and a run
# with the moved path left `plan:` naming the removed file.
# --------------------------------------------------------------------------- #


def _md_file_into(spec: Path, partition: str) -> Path:
    """Move *spec* into ``design/specs/<partition>/`` the way reconcile files a terminal spec."""
    dest = spec.parent / partition / spec.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    spec.rename(dest)
    return dest


@pytest.mark.parametrize("partition", ["done", "archived"])
def test_mark_done_retry_follows_a_spec_filed_into_a_terminal_partition(
    tmp_path: Path, capsys: pytest.CaptureFixture, partition: str
) -> None:
    """gh#891 repro: written, spec moved by reconcile, retry with the original --plan."""
    from agentflow.status_writer import merge_record_path, read_status

    spec = _md_spec(tmp_path)
    assert _md_cli(tmp_path, spec) == 0
    record = merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)
    assert read_status(record).plan == str(spec)
    moved = _md_file_into(spec, partition)
    capsys.readouterr()

    assert _md_cli(tmp_path, spec) == 0
    out, err = capsys.readouterr()
    assert out == f"written: {record}\n"  # stdout stays the one line lanes parse
    assert err.startswith("note: ") and str(moved) in err and "WARNING" not in err, err
    assert read_status(record).plan == str(moved)
    assert _md_survey(tmp_path)[_MD_SLUG].lifecycle_state == "done"


def test_mark_done_rerun_after_the_plan_rewrite_changes_no_byte(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The rewritten `plan:` exists, so neither the followed nor the direct path moves it again."""
    from agentflow.status_writer import merge_record_path

    spec = _md_spec(tmp_path)
    assert _md_cli(tmp_path, spec) == 0
    moved = _md_file_into(spec, "done")
    assert _md_cli(tmp_path, spec) == 0
    record = merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)
    first = record.read_bytes()
    capsys.readouterr()

    assert _md_cli(tmp_path, spec) == 0
    assert capsys.readouterr().out == f"unchanged: {record}\n"
    assert _md_cli(tmp_path, moved) == 0
    out, err = capsys.readouterr()
    assert out == f"unchanged: {record}\n" and "note:" not in err  # nothing was followed
    assert record.read_bytes() == first


def test_mark_done_with_the_moved_path_refreshes_a_dead_plan(tmp_path: Path) -> None:
    """The issue's second half: passing the done/ path directly said `unchanged` and kept the
    removed path in `plan:`, because `setdefault` never refreshed an existing key."""
    from agentflow.status_writer import merge_record_path, read_status

    spec = _md_spec(tmp_path)
    assert _md_cli(tmp_path, spec) == 0
    moved = _md_file_into(spec, "done")
    assert _md_cli(tmp_path, moved) == 0
    assert read_status(merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)).plan == str(moved)


def test_mark_done_refusal_for_a_missing_spec_says_it_looked_in_done_and_archived(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from agentflow.status_writer import merge_record_path

    spec = _md_spec(tmp_path)
    spec.unlink()
    capsys.readouterr()
    assert _md_cli(tmp_path, spec) == 2
    err = capsys.readouterr().err
    specs_dir = spec.parent
    assert err.startswith("refused: ") and "not a readable spec" in err, err
    assert str(specs_dir / "done") in err and str(specs_dir / "archived") in err, err
    assert not merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG).exists()


def test_mark_done_refuses_when_both_partitions_hold_the_spec_name(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Two candidates are two histories; the writer names both and guesses neither."""
    from agentflow.status_writer import merge_record_path

    spec = _md_spec(tmp_path)
    text = spec.read_text(encoding="utf-8")
    moved = _md_file_into(spec, "done")
    other = spec.parent / "archived" / spec.name
    other.parent.mkdir(parents=True)
    other.write_text(text, encoding="utf-8")
    capsys.readouterr()
    assert _md_cli(tmp_path, spec) == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: ") and str(moved) in err and str(other) in err, err
    assert not merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG).exists()


def test_mark_done_keeps_a_stored_plan_that_still_exists(tmp_path: Path) -> None:
    """Only a DEAD `plan:` is refreshed. This one has the spec's name and sits in a spec dir, so
    only its existence keeps it - the orchestrator-board case, whose `plan:` names a live file."""
    from agentflow.status_writer import merge_record_path, read_status

    spec = _md_spec(tmp_path)
    assert _md_cli(tmp_path, spec) == 0
    copy = spec.parent / "done" / spec.name
    copy.parent.mkdir(parents=True)
    copy.write_text(spec.read_text(encoding="utf-8"), encoding="utf-8")
    record = merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG)
    first = record.read_bytes()
    assert _md_cli(tmp_path, copy) == 0
    assert read_status(record).plan == str(spec)
    assert record.read_bytes() == first


def test_mark_done_still_checks_the_slug_of_a_followed_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Following finds a file by NAME; the slug check still decides whether it is this epic."""
    from agentflow.status_writer import merge_record_path

    spec = _md_spec(tmp_path)
    moved = _md_file_into(spec, "done")
    moved.write_text(
        moved.read_text(encoding="utf-8").replace("---\n", "---\nslug: some-other-epic\n", 1),
        encoding="utf-8",
    )
    capsys.readouterr()
    assert _md_cli(tmp_path, spec) == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: ") and str(moved) in err and "'some-other-epic'" in err, err
    assert not merge_record_path(tmp_path, _MD_PROJECT, _MD_SLUG).exists()
