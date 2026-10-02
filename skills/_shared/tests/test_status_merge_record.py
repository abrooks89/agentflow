"""The merge record a builder lane writes at merge is READ by the survey (gh#319 route 2).

Registry Q-10081 decided that lanes write the epic's status.md at merge so the completion-truth
cross-check has something independent of spec frontmatter to read. The first hand-written records
existed on disk and were skipped: `read_status` raised `KeyError('plan')` and `epic_survey` logged
"malformed status, skipping". A file that exists is not the check running, so the load-bearing test
here goes all the way from the CLI to `survey_epics(..., project=...)` and `selectable()`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentflow.epic_survey import selectable, survey_epics
from agentflow.status_writer import (
    MergeRecordRefused,
    main,
    mark_done,
    merge_record_path,
    read_status,
)

PAST_CUTOFF = "2020-01-01T00:00:00+00:00"
PROJECT = "proj"
SLUG = "ship-the-thing"


def _spec(root: Path, slug: str = SLUG) -> Path:
    path = root / "projects" / PROJECT / "design" / "specs" / f"2026-09-01-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Still approved: the lane has merged but not yet reconciled the spec frontmatter, which is
    # exactly the window the status board exists to cover.
    path.write_text(
        f"---\ntitle: {slug}\ncurrent_state: approved-for-autonomous\nproject: {PROJECT}\n---\n\nbody\n",
        encoding="utf-8",
    )
    (root / "projects" / PROJECT / "status").mkdir(parents=True, exist_ok=True)
    return path


def _cli(root: Path, spec: Path, *extra: str) -> int:
    return main(
        [
            "mark-done",
            "--data-root",
            str(root),
            "--project",
            PROJECT,
            "--slug",
            SLUG,
            "--plan",
            str(spec),
            "--shipped",
            "abc1234 (PR 42)",
            "--queue-id",
            "E-0001-ship-the",
            "--actor",
            "jr-dev-1",
            *extra,
        ]
    )


def test_cli_record_round_trips_through_read_status(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    assert _cli(tmp_path, spec) == 0
    path = merge_record_path(tmp_path, PROJECT, SLUG)
    state = read_status(path)
    assert (state.epic, state.status, state.current_phase) == (SLUG, "done", "done")
    assert state.plan == str(spec) and state.project == PROJECT
    raw = path.read_bytes()
    assert b"\r" not in raw, "merge record must be LF-only on every platform"
    fm = yaml.safe_load(raw.decode("utf-8").split("---\n")[1])
    assert fm["shipped"] == "abc1234 (PR 42)"
    assert fm["queue_id"] == "E-0001-ship-the" and fm["written_by"] == "jr-dev-1"


def test_survey_reads_the_merge_record_as_done_for_the_builder_cross_check(tmp_path: Path) -> None:
    """The steward's pin: the record is READ, not merely present.

    `selectable()` keys approval on spec frontmatter, so it still hands back a record whose spec
    says approved (its documented stale-approval caveat; reconciling the frontmatter is the other
    half of the merge duty). What the builder's completion-truth cross-check reads is the record's
    `lifecycle_state` and `source_path` - and those must now come from the merge record.
    """
    spec = _spec(tmp_path)

    def picked():
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True, project=PROJECT)
        return {r.slug: r for r in selectable(recs, project=PROJECT)}[SLUG]

    # Control: before the merge record, the cross-check has nothing but frontmatter to read.
    assert picked().lifecycle_state != "done"

    assert _cli(tmp_path, spec) == 0
    rec = picked()
    assert rec.lifecycle_state == "done"
    assert Path(rec.source_path) == merge_record_path(tmp_path, PROJECT, SLUG)
    # The cross-check the builder SKILL.md prescribes refuses it.
    assert SLUG not in {
        r.slug
        for r in selectable(
            survey_epics(tmp_path, PAST_CUTOFF, include_archived=True, project=PROJECT),
            project=PROJECT,
        )
        if r.lifecycle_state != "done"
    }


def test_mark_done_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    spec = _spec(tmp_path)
    assert _cli(tmp_path, spec) == 0
    path = merge_record_path(tmp_path, PROJECT, SLUG)
    first = path.read_bytes()
    capsys.readouterr()
    assert _cli(tmp_path, spec) == 0
    assert capsys.readouterr().out.startswith("unchanged: ")
    assert path.read_bytes() == first
    _, changed = mark_done(
        tmp_path,
        project=PROJECT,
        slug=SLUG,
        plan=spec,
        shipped="abc1234 (PR 42)",
        queue_id="E-0001-ship-the",
        actor="jr-dev-1",
    )
    assert changed is False


def test_refuses_to_overwrite_a_record_for_a_different_epic(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    path = merge_record_path(tmp_path, PROJECT, SLUG)
    foreign = (
        "---\nepic: some-other-epic\nplan: p.md\nstarted: '2026-01-01T00:00:00+00:00'\n"
        "status: running\ncurrent_phase: p1\n---\n\nnot yours\n"
    )
    path.write_bytes(foreign.encode("utf-8"))
    assert _cli(tmp_path, spec) == 2
    assert path.read_bytes() == foreign.encode("utf-8")
    with pytest.raises(MergeRecordRefused, match="some-other-epic"):
        mark_done(tmp_path, project=PROJECT, slug=SLUG, plan=spec, shipped="abc1234 (PR 42)")


@pytest.mark.parametrize("value", ["[a, b]", "{a: 1}"])
def test_refuses_a_board_whose_epic_is_not_text(tmp_path: Path, value: str) -> None:
    """A list or mapping `epic:` is unhashable, so it once escaped as a traceback (exit 1)."""
    spec = _spec(tmp_path)
    path = merge_record_path(tmp_path, PROJECT, SLUG)
    board = (
        f"---\nepic: {value}\nplan: p.md\nstarted: '2026-01-01T00:00:00+00:00'\n"
        "status: running\ncurrent_phase: p1\n---\n\nbody\n"
    )
    path.write_bytes(board.encode("utf-8"))
    assert _cli(tmp_path, spec) == 2
    assert path.read_bytes() == board.encode("utf-8")


def test_refuses_a_plan_whose_spec_slug_is_not_the_slug(tmp_path: Path) -> None:
    other = _spec(tmp_path, "a-different-spec")
    assert _cli(tmp_path, other) == 2
    assert not merge_record_path(tmp_path, PROJECT, SLUG).exists()


def test_completes_a_hand_written_record_the_survey_was_skipping(tmp_path: Path) -> None:
    """The live shape that failed: `status: done` plus merge keys, no `plan`/`started`."""
    spec = _spec(tmp_path)
    path = merge_record_path(tmp_path, PROJECT, SLUG)
    path.write_text(
        f"---\nepic: {SLUG}\nstatus: done\nscope: report-only\n---\n\n# notes kept\n",
        encoding="utf-8",
    )
    with pytest.raises(KeyError):
        read_status(path)
    assert _cli(tmp_path, spec) == 0
    assert read_status(path).status == "done"
    text = path.read_text(encoding="utf-8")
    assert "scope: report-only" in text and "# notes kept" in text


def _cli_slug(root: Path, slug: str, spec: Path) -> int:
    return main(
        [
            "mark-done",
            "--data-root",
            str(root),
            "--project",
            PROJECT,
            "--slug",
            slug,
            "--plan",
            str(spec),
            "--shipped",
            "abc1234 (PR 42)",
        ]
    )


def test_a_board_the_parser_rejects_is_a_refusal_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """An unknown task field reaches `TaskState(**td)` as a TypeError; the lane must see exit 2."""
    spec = _spec(tmp_path)
    path = merge_record_path(tmp_path, PROJECT, SLUG)
    board = (
        f"---\nepic: {SLUG}\nplan: p.md\nstarted: s\nstatus: running\ncurrent_phase: p1\n"
        "tasks:\n  1:\n    state: done\n    bogus: 1\n---\n\nbody\n"
    )
    path.write_bytes(board.encode("utf-8"))
    capsys.readouterr()
    assert _cli(tmp_path, spec) == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: ") and "TypeError" in err and "Traceback" not in err
    assert path.read_bytes() == board.encode("utf-8")


@pytest.mark.parametrize("slug", ["a<b", "a:b", 'a"b', "a|b", "a?b", "a\x01b", "a\\b", "a/b"])
def test_a_slug_that_is_not_a_windows_filename_is_refused_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture, slug: str
) -> None:
    spec = _spec(tmp_path)
    capsys.readouterr()
    assert _cli_slug(tmp_path, slug, spec) == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: slug must be a single literal path segment"), err
    assert list((tmp_path / "projects" / PROJECT / "status").iterdir()) == []


PLAN_TITLE = "Ship the thing - one sentence in, a shipped thing out (epic 7)"


def _orchestrator_board(epic: str) -> str:
    """The shape the orchestrator writes at this same path: its `epic:` is a human title."""
    return (
        f"---\nepic: '{epic}'\nplan: /gone/solution/2026-09-01-{SLUG}.md\n"
        "started: '2026-09-01T00:00:00Z'\npaused: false\nstatus: running\n"
        f"current_phase: deploy-and-gate\nlast_green_commit: 7a995ae2\nproject: {PROJECT}\n"
        "total_waves: 3\ntasks:\n"
        "  1:\n    state: done\n    phase: build\n    commit: c40a237a\n    verifier_iterations: 0\n"
        "  2:\n    state: in_flight\n    phase: deploy-and-gate\n"
        "---\n\n<!-- AUTO:start -->\nrendered\n<!-- AUTO:end -->\n\nuser notes kept\n"
    )


def _plan(root: Path, epic: str = PLAN_TITLE) -> Path:
    path = root / "projects" / PROJECT / "design" / "plans" / f"2026-09-02-{SLUG}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\nepic: "{epic}"\nslug: {SLUG}\nproject: {PROJECT}\n---\n\n# {epic}\n',
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("owner", ["plan-epic", "spec-title"])
def test_completes_the_orchestrator_board_that_names_the_epic_by_title(
    tmp_path: Path, owner: str
) -> None:
    """Review round 1 HIGH: the orchestrator's running board was refused as 'a different epic'."""
    spec = _spec(tmp_path)
    if owner == "plan-epic":
        _plan(tmp_path)
        epic = PLAN_TITLE
    else:
        spec.write_text(
            spec.read_text(encoding="utf-8").replace(f"title: {SLUG}", "title: The spec title"),
            encoding="utf-8",
        )
        epic = "The spec title"
    path = merge_record_path(tmp_path, PROJECT, SLUG)
    path.write_bytes(_orchestrator_board(epic).encode("utf-8"))
    before = read_status(path)
    assert before.status == "running" and len(before.tasks) == 2

    assert _cli(tmp_path, spec) == 0
    after = read_status(path)
    assert (after.status, after.current_phase, after.epic) == ("done", "done", epic)
    assert after.tasks == before.tasks
    assert (after.plan, after.last_green_commit, after.total_waves) == (
        before.plan,
        "7a995ae2",
        3,
    )
    text = path.read_text(encoding="utf-8")
    assert "<!-- AUTO:end -->\n\nuser notes kept\n" in text
    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True, project=PROJECT)
    rec = {r.slug: r for r in recs}[SLUG]
    assert rec.lifecycle_state == "done"
    assert Path(rec.source_path) == path


def test_a_plan_for_another_slug_lends_its_title_to_nobody(tmp_path: Path) -> None:
    """A plan file named for this slug but declaring another `slug:` is not this epic's plan."""
    spec = _spec(tmp_path)
    plan = _plan(tmp_path)
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(f"slug: {SLUG}", "slug: someone-else"),
        encoding="utf-8",
    )
    path = merge_record_path(tmp_path, PROJECT, SLUG)
    board = _orchestrator_board(PLAN_TITLE).encode("utf-8")
    path.write_bytes(board)
    assert _cli(tmp_path, spec) == 2
    assert path.read_bytes() == board


@pytest.mark.parametrize(
    "where", ["zzz-legacy/ship-the-thing.status.md", "ship-the-thing/status.md"]
)
def test_warns_naming_a_same_slug_board_elsewhere_in_the_status_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture, where: str
) -> None:
    """Review round 1 LOW: pass 3 keeps one board per slug, and a deeper one can win."""
    spec = _spec(tmp_path)
    other = tmp_path / "projects" / PROJECT / "status" / where
    other.parent.mkdir(parents=True)
    other.write_text(
        f"---\nepic: {SLUG}\nplan: p\nstarted: s\nstatus: running\ncurrent_phase: p1\n---\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert _cli(tmp_path, spec) == 0
    err = capsys.readouterr().err
    assert err.startswith("WARNING: ") and str(other) in err, err
    assert read_status(merge_record_path(tmp_path, PROJECT, SLUG)).status == "done"


def test_no_warning_without_a_shadow(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    spec = _spec(tmp_path)
    notes = tmp_path / "projects" / PROJECT / "status" / "notes" / "another-epic.status.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("---\nepic: another-epic\n---\n", encoding="utf-8")
    capsys.readouterr()
    assert _cli(tmp_path, spec) == 0
    assert "WARNING" not in capsys.readouterr().err


def test_warns_naming_a_board_keyed_by_the_epic_title_instead_of_the_slug(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A board named from the plan's title is a second record the slug join never finds."""
    spec = _spec(tmp_path)
    _plan(tmp_path)
    status_dir = tmp_path / "projects" / PROJECT / "status"
    titled = status_dir / "ship-the-thing-one-sentence-in.status.md"
    titled.write_bytes(_orchestrator_board(PLAN_TITLE).encode("utf-8"))
    capsys.readouterr()
    assert _cli(tmp_path, spec) == 0
    err = capsys.readouterr().err
    assert err.startswith("WARNING: ") and str(titled) in err, err
    assert str(merge_record_path(tmp_path, PROJECT, SLUG)) + " names" not in err
    assert read_status(merge_record_path(tmp_path, PROJECT, SLUG)).status == "done"


def test_no_title_warning_for_the_epics_own_board_or_another_epics(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    spec = _spec(tmp_path)
    _plan(tmp_path)
    status_dir = tmp_path / "projects" / PROJECT / "status"
    merge_record_path(tmp_path, PROJECT, SLUG).write_bytes(
        _orchestrator_board(PLAN_TITLE).encode("utf-8")
    )
    (status_dir / "other-epic.status.md").write_bytes(
        _orchestrator_board("Some other epic entirely").encode("utf-8")
    )
    capsys.readouterr()
    assert _cli(tmp_path, spec) == 0
    assert "WARNING" not in capsys.readouterr().err


def test_a_slug_fallback_title_does_not_count_as_a_title(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A spec with no title reads back as its slug; a board whose epic: is the slug is not flagged."""
    spec = _spec(tmp_path)
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(f"title: {SLUG}\n", ""), encoding="utf-8"
    )
    status_dir = tmp_path / "projects" / PROJECT / "status"
    (status_dir / "some-other-stem.status.md").write_bytes(
        _orchestrator_board(SLUG).encode("utf-8")
    )
    capsys.readouterr()
    assert _cli(tmp_path, spec) == 0
    assert "WARNING" not in capsys.readouterr().err


def test_mark_done_completes_a_BOM_prefixed_board_in_place(tmp_path: Path) -> None:
    """gh#336: a BOM'd existing record was refused as having "no YAML frontmatter". It is now
    completed in place - one frontmatter block, the body kept - and written back without the BOM,
    which the raw-text idempotence compare has to see as a change rather than as no-op."""
    spec = _spec(tmp_path)
    path = merge_record_path(tmp_path, PROJECT, SLUG)
    path.write_bytes(b"\xef\xbb\xbf" + _orchestrator_board(SLUG).encode("utf-8"))

    _, changed = mark_done(
        tmp_path, project=PROJECT, slug=SLUG, plan=spec, shipped="abc1234 (PR 42)"
    )

    assert changed
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert text.count("\nepic:") == 1, text
    assert "user notes kept" in text
    assert read_status(path).status == "done"

    # Converges: the BOM-free rewrite is now the record, so a second call is a no-op.
    _, changed_again = mark_done(
        tmp_path, project=PROJECT, slug=SLUG, plan=spec, shipped="abc1234 (PR 42)"
    )
    assert changed_again is False
