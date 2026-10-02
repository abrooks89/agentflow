"""WORK says how much of itself a free lane could actually start (gh#121).

The disjointness rule is right and nothing here touches it: three builders in one seam produce
merge conflicts even with worktrees. What was missing is a CHANNEL. On 2026-09-02 every lane
behaved correctly while the lead lane could claim nothing — one healthy `building` row, three
selectable bugs, non-zero WORK — and the only way to see it was to cross-reference a 33-file
``## Files`` section against three bug fixes by hand.

**READ THIS BEFORE ADDING A TEST HERE.** The first version of this gate was 13 green tests over
hand-built ``EpicRecord``s, and it certified a feature that could not do the thing the issue
asked for. ``survey_epics`` overwrites ``source_path`` with the epic's ``status.md`` for anything
``running``, and status bodies never carry a ``## Files`` heading — so in the real pipeline no
in-flight EPIC could ever contribute paths, and epic-blocks-epic was unreportable. Every fixture
had quietly asserted a record shape the survey does not produce. The class-closing test is
``test_a_running_epic_resolves_its_SPEC_not_its_status_file``, which drives the real
``survey_epics`` over a real tree; the hand-built cases below are unit coverage layered on top of
it, never a substitute for it.

WHAT THIS GATE CAN AND CANNOT DO:

* It CAN prove that an overlap is reported with its holder and the offending paths, that an
  unreadable file set is counted separately rather than as clear, that an item never blocks
  itself, that a capped input is declared rather than silently trusted, and that the holder
  resolves through the real survey.
* It CANNOT prove the rendered markdown looks right — that is the artifact renderer's. It also
  cannot prove the signal is COMPLETE: paths match as exact strings, so a real overlap spelled
  two ways is missed by construction, and two tests at the bottom pin that under-reporting as
  the chosen direction rather than leaving it for a later reader to call a bug.
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS_ROOT))

from agentflow.epic_survey import EpicRecord, survey_epics  # noqa: E402
from agentflow.fleet_report import (  # noqa: E402
    ReportInputs,
    Rows,
    WorkSection,
    _attach_seam_signal,
    _seam_paths,
    _spec_index,
    _work_section,
)
from agentflow.issue_intake import Issue  # noqa: E402

BT = chr(96)
CUTOFF = "2000-01-01T00:00:00Z"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _tree(tmp_path, project="demo"):
    """A real data_root laid out the way ``SurveyPatterns`` expects to find one."""
    specs = tmp_path / "projects" / project / "design" / "specs"
    status = tmp_path / "projects" / project / "status"
    specs.mkdir(parents=True)
    status.mkdir(parents=True)
    return specs, status


def _write_spec(specs, slug, *paths, state="approved-for-autonomous"):
    body = [
        "---", f"slug: {slug}", f"current_state: {state}", "priority: 100", "---",
        f"# {slug}", "", "## Files", "",
    ]
    body += [f"- {BT}{p}{BT} - because" for p in paths] or ["- the loop skills"]
    p = specs / f"2026-09-04-{slug}.md"
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return p


def _write_running_status(status, slug):
    """An orchestrator status.md — the ``epic:`` signature is what the survey gates on."""
    p = status / f"{slug}.status.md"
    p.write_text(
        "---\n"
        f"epic: {slug}\n"
        f"plan: plans/{slug}.md\n"
        "started: '2026-09-04T00:00:00Z'\n"
        "paused: false\n"
        "status: running\n"
        "current_phase: build\n"
        "tasks:\n"
        "  1:\n"
        "    state: done\n"
        "    phase: build\n"
        "  2:\n"
        "    state: pending\n"
        "    phase: build\n"
        "---\n"
        "# Status\n\n## Progress\n\n- [x] one\n- [ ] two\n\n## Decisions log\n\n- none\n",
        encoding="utf-8",
    )
    return p


def _inputs(tmp_path, project="demo", rows=10):
    return ReportInputs(
        data_root=tmp_path, project=project, mtime_cutoff_iso=CUTOFF, rows_per_section=rows
    )


def _rec(tmp_path, slug, *paths):
    """A record whose source_path IS its spec — the shape a non-running epic really has."""
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    body = [f"# {slug}", "", "## Files", ""]
    body += [f"- {BT}{p}{BT}" for p in paths] or ["- the loop skills"]
    f = specs / f"{slug}.md"
    f.write_text("\n".join(body), encoding="utf-8")
    return EpicRecord(slug=slug, title=slug, lifecycle_state="ready", source_path=str(f))


def _no_files_rec(tmp_path, slug):
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    f = specs / f"{slug}.md"
    f.write_text(f"# {slug}\n\n## Scope\n\ndo the thing\n", encoding="utf-8")
    return EpicRecord(slug=slug, title=slug, lifecycle_state="ready", source_path=str(f))


class _EpicRow:
    def __init__(self, slug):
        self.slug = slug


def _bug(n, *paths, body=None):
    if body is None:
        body = "\n".join(["## Files", ""] + [f"- {BT}{p}{BT}" for p in paths])
    return Issue(number=n, title=f"bug {n}", labels=("bug", "triaged"), body=body)


def _work(*, claimable=(), running=(), bug_claimable=(), bug_running=(), cap=None):
    def rows(items):
        items = list(items)
        shown = items if cap is None else items[:cap]
        return Rows(items=tuple(shown), total=len(items))

    return WorkSection(
        approved_claimable=rows(claimable),
        in_progress=rows(running),
        bug_claimable=rows(bug_claimable),
        bug_in_progress=rows(bug_running),
    )


# --------------------------------------------------------------------------- #
# The class-closing test: the real pipeline, not a hand-built record
# --------------------------------------------------------------------------- #
def test_a_running_epic_resolves_its_SPEC_not_its_status_file(tmp_path):
    """The defect that made the first draft useless for the case gh#121 is about.

    A running epic's ``source_path`` is its status.md, and no status.md has a ``## Files``
    section — so reading the record's own path yields ``None`` every time and the holder set
    stays permanently empty. This drives ``survey_epics`` over a real tree so the shape under
    test is the one the fleet actually produces.
    """
    specs, status = _tree(tmp_path)
    _write_spec(specs, "wide-migration", "skills/_shared/agentflow/config.py")
    _write_running_status(status, "wide-migration")
    _write_spec(specs, "small-fix", "skills/_shared/agentflow/config.py")

    records = survey_epics(tmp_path, CUTOFF, include_archived=True, project="demo")
    running = [r for r in records if r.lifecycle_state == "running"]

    assert [r.slug for r in running] == ["wide-migration"], [r.slug for r in records]
    assert running[0].source_path.endswith(".status.md"), (
        "premise of this test: the survey really does repoint source_path at the status file"
    )

    work = _work(claimable=[_EpicRow("small-fix")], running=[_EpicRow("wide-migration")])
    out = _attach_seam_signal(work, records, _inputs(tmp_path))

    assert out.seam_blocked.total == 1, "the holder must resolve through its SPEC"
    assert out.seam_blocked.items[0].blocked_by == ("wide-migration",)
    assert out.seam_unknown_holders == 0


def test_the_spec_index_is_built_from_the_surveys_own_globs(tmp_path):
    """Non-vacuity for the test above: if the index were empty everything would read unknown."""
    specs, _ = _tree(tmp_path)
    _write_spec(specs, "wide-migration", "docs/A.md")
    index = _spec_index(_inputs(tmp_path))
    assert "wide-migration" in index, index
    assert _spec_index(_inputs(tmp_path, project="no-such-project")) == {}


def test_the_real_work_section_and_the_seam_signal_agree_on_one_tree(tmp_path):
    """End to end through ``_work_section``, so the two columns are the real ones."""
    specs, status = _tree(tmp_path)
    _write_spec(specs, "wide-migration", "install/provision.py")
    _write_running_status(status, "wide-migration")
    _write_spec(specs, "small-fix", "install/provision.py")

    records = survey_epics(tmp_path, CUTOFF, include_archived=True, project="demo")
    inputs = _inputs(tmp_path)
    work = _work_section(None, records, _NoChange(), inputs=inputs, cap=10, scan=None)
    out = _attach_seam_signal(work, records, inputs)

    assert "wide-migration" in [getattr(r, "slug", "") for r in work.in_progress.items]
    assert out.seam_blocked.total >= 1
    assert all(b.name != b.blocked_by[0] for b in out.seam_blocked.items), "never self-blocked"


class _NoChange:
    """The minimum ``diff_since`` result ``_work_section`` reads."""

    no_prior = True
    epic_moves = ()


# --------------------------------------------------------------------------- #
# The reported overlap
# --------------------------------------------------------------------------- #
def test_an_overlap_names_the_holder_and_the_paths(tmp_path):
    """Either half alone is unactionable: a holder with no paths says "wait for something"."""
    held = _rec(tmp_path, "wide-migration", "skills/_shared/agentflow/config.py", "docs/A.md")
    want = _rec(tmp_path, "small-fix", "docs/A.md")

    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("small-fix")], running=[_EpicRow("wide-migration")]),
        [held, want], _inputs(tmp_path),
    )

    block = out.seam_blocked.items[0]
    assert (block.name, block.blocked_by) == ("small-fix", ("wide-migration",))
    assert block.paths == ("docs/A.md",), "the offending path, not the whole Files list"
    assert out.seam_unknown_count == 0 and out.seam_note == ""


def test_a_disjoint_claimable_is_not_blocked_and_not_unknown(tmp_path):
    """The discriminating control: the same machinery must produce a clean verdict too."""
    held = _rec(tmp_path, "wide-migration", "skills/_shared/agentflow/config.py")
    want = _rec(tmp_path, "elsewhere", "docs/UNRELATED.md")

    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("elsewhere")], running=[_EpicRow("wide-migration")]),
        [held, want], _inputs(tmp_path),
    )
    assert (out.seam_blocked.total, out.seam_unknown_count, out.seam_unknown_holders) == (0, 0, 0)


def test_no_in_flight_work_means_nothing_is_blocked(tmp_path):
    """Control against a check that reports on shape rather than on overlap."""
    want = _rec(tmp_path, "small-fix", "docs/A.md")
    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("small-fix")]), [want], _inputs(tmp_path))
    assert out.seam_blocked.total == 0 and out.seam_unknown_count == 0


def test_two_holders_of_one_path_are_both_named(tmp_path):
    """`blocked_by` is a tuple because waiting on the wrong one of two is still waiting."""
    a = _rec(tmp_path, "aaa-epic", "docs/A.md")
    b = _rec(tmp_path, "bbb-epic", "docs/A.md")
    want = _rec(tmp_path, "small-fix", "docs/A.md")

    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("small-fix")],
              running=[_EpicRow("aaa-epic"), _EpicRow("bbb-epic")]),
        [a, b, want], _inputs(tmp_path),
    )
    assert out.seam_blocked.items[0].blocked_by == ("aaa-epic", "bbb-epic")


# --------------------------------------------------------------------------- #
# Nothing blocks itself
# --------------------------------------------------------------------------- #
def test_an_epic_in_both_columns_never_blocks_itself(tmp_path):
    """A running epic whose frontmatter still says approved appears in BOTH columns.

    That lag is the fleet's documented normal, so intersecting the item with itself would
    manufacture a block on most boards — and "X blocked by X" reads as real to a renderer.
    """
    both = _rec(tmp_path, "wide-migration", "docs/A.md")
    other = _rec(tmp_path, "small-fix", "docs/A.md")

    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("wide-migration"), _EpicRow("small-fix")],
              running=[_EpicRow("wide-migration")]),
        [both, other], _inputs(tmp_path),
    )

    assert [b.name for b in out.seam_blocked.items] == ["small-fix"]
    assert out.seam_unknown_count == 0, "the self row is skipped, not counted unknown"


def test_a_dual_listed_item_with_no_files_is_not_counted_as_an_unknown_CLAIMABLE(tmp_path):
    """The property the self-exclusion actually owns, found by a surviving mutant.

    Removing the ``held_names`` skip left every block assertion green, because the ``h != name``
    guard inside the holder comprehension catches the same case a second time — two guards that
    each suffice, hiding which one is load-bearing. The one thing only ``held_names`` decides is
    what happens BEFORE the unknown branch: an item already being built is not waiting on
    anything, so counting it as an unknown claimable inflates the number a reader uses to judge
    how much of the board is unreadable.
    """
    both = _no_files_rec(tmp_path, "opaque-and-running")
    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("opaque-and-running")],
              running=[_EpicRow("opaque-and-running")]),
        [both], _inputs(tmp_path),
    )
    assert out.seam_unknown_count == 0, "it is being built, not waiting"
    assert out.seam_unknown_holders == 1, "as a HOLDER it is still unreadable, and that counts"


def test_a_bug_in_both_columns_never_blocks_itself(tmp_path):
    """Same rule on the bug side, where in-flight is decided by a registry claim."""
    out = _attach_seam_signal(
        _work(bug_claimable=[_bug(59, "install/provision.py")],
              bug_running=[_bug(59, "install/provision.py")]),
        [], _inputs(tmp_path),
    )
    assert out.seam_blocked.total == 0


# --------------------------------------------------------------------------- #
# Unknown is a third state, on both sides
# --------------------------------------------------------------------------- #
def test_a_claimable_with_no_files_section_is_unknown_not_clear(tmp_path):
    """An absent ``## Files`` is not evidence of no overlap.

    Folding it into the clear column is how a blocked lane reads as an available one, which is
    the exact failure this signal exists to remove.
    """
    held = _rec(tmp_path, "wide-migration", "docs/A.md")
    want = _no_files_rec(tmp_path, "silent-spec")

    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("silent-spec")], running=[_EpicRow("wide-migration")]),
        [held, want], _inputs(tmp_path),
    )
    assert out.seam_unknown_count == 1
    assert out.seam_blocked.total == 0, "unknown is not blocked either - it is unknown"


def test_a_present_but_vague_files_section_is_also_unknown(tmp_path):
    """"the loop skills" is present and unusable; ``files_section`` already refuses it."""
    held = _rec(tmp_path, "wide-migration", "docs/A.md")
    want = _rec(tmp_path, "vague-spec")

    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("vague-spec")], running=[_EpicRow("wide-migration")]),
        [held, want], _inputs(tmp_path),
    )
    assert out.seam_unknown_count == 1


def test_a_bug_with_no_files_section_is_unknown_not_clear(tmp_path):
    """The bug half of the same branch — untested in the first draft."""
    held = _rec(tmp_path, "wide-migration", "docs/A.md")
    out = _attach_seam_signal(
        _work(bug_claimable=[_bug(7, body="No Files section here, just prose.")],
              running=[_EpicRow("wide-migration")]),
        [held], _inputs(tmp_path),
    )
    assert out.seam_unknown_count == 1 and out.seam_blocked.total == 0


def test_an_unreadable_spec_is_unknown_rather_than_a_crash(tmp_path):
    """A record can name a path that is gone; the report must survive it."""
    held = _rec(tmp_path, "wide-migration", "docs/A.md")
    ghost = EpicRecord(slug="ghost", title="ghost", lifecycle_state="ready",
                       source_path=str(tmp_path / "does-not-exist.md"))
    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("ghost")], running=[_EpicRow("wide-migration")]),
        [held, ghost], _inputs(tmp_path),
    )
    assert out.seam_unknown_count == 1


def test_a_holder_that_declares_no_files_weakens_every_clear_verdict_and_says_so(tmp_path):
    """The dangerous unknown, and the reason it gets its own counter.

    An unreadable CLAIMABLE costs one item's verdict. An unreadable HOLDER costs every clear
    verdict in the section, because work nobody can locate cannot be found to overlap anything.
    """
    held = _no_files_rec(tmp_path, "opaque-migration")
    want = _rec(tmp_path, "small-fix", "docs/A.md")

    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("small-fix")], running=[_EpicRow("opaque-migration")]),
        [held, want], _inputs(tmp_path),
    )
    assert out.seam_blocked.total == 0, "it genuinely cannot be shown to overlap"
    assert out.seam_unknown_holders == 1
    assert "cannot be found to overlap" in out.seam_note, out.seam_note


# --------------------------------------------------------------------------- #
# A capped view is declared, never trusted
# --------------------------------------------------------------------------- #
def test_a_capped_input_makes_the_verdict_partial_and_says_so(tmp_path):
    """`rows_per_section` defaults to 10, so on a busy board the inputs arrive truncated.

    A clear answer over a truncated view is the same false confidence as a clear answer over an
    unreadable holder, so it gets the same treatment: declared, not assumed away.
    """
    held = _rec(tmp_path, "wide-migration", "docs/A.md")
    wants = [_rec(tmp_path, f"fix-{i}", f"docs/{i}.md") for i in range(4)]

    out = _attach_seam_signal(
        _work(claimable=[_EpicRow(r.slug) for r in wants],
              running=[_EpicRow("wide-migration")], cap=2),
        [held, *wants], _inputs(tmp_path),
    )
    assert out.seam_partial is True
    assert "capped" in out.seam_note


def test_an_uncapped_view_is_not_partial(tmp_path):
    """Control: the flag must be able to be False, or it says nothing when it is True."""
    held = _rec(tmp_path, "wide-migration", "docs/A.md")
    want = _rec(tmp_path, "small-fix", "docs/A.md")
    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("small-fix")], running=[_EpicRow("wide-migration")]),
        [held, want], _inputs(tmp_path),
    )
    assert out.seam_partial is False and out.seam_note == ""


# --------------------------------------------------------------------------- #
# Both directions across the two columns
# --------------------------------------------------------------------------- #
def test_a_bug_can_be_blocked_by_an_in_flight_epic(tmp_path):
    held = _rec(tmp_path, "wide-migration", "install/provision.py")
    out = _attach_seam_signal(
        _work(running=[_EpicRow("wide-migration")],
              bug_claimable=[_bug(59, "install/provision.py")]),
        [held], _inputs(tmp_path),
    )
    assert out.seam_blocked.items[0].name == "gh#59"
    assert out.seam_blocked.items[0].blocked_by == ("wide-migration",)


def test_an_epic_can_be_blocked_by_an_in_flight_bug(tmp_path):
    """The reverse direction, which a one-way check would silently miss."""
    want = _rec(tmp_path, "small-fix", "skills/_shared/agentflow/registry.py")
    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("small-fix")],
              bug_running=[_bug(134, "skills/_shared/agentflow/registry.py")]),
        [want], _inputs(tmp_path),
    )
    assert out.seam_blocked.items[0].blocked_by == ("gh#134",)


# --------------------------------------------------------------------------- #
# What the matcher does and does not equate
# --------------------------------------------------------------------------- #
def test_separator_and_dot_slash_spellings_of_one_path_do_match(tmp_path):
    held = _rec(tmp_path, "wide-migration", "./skills/_shared/agentflow/config.py")
    want = _rec(tmp_path, "small-fix", "skills\\_shared\\agentflow\\config.py")
    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("small-fix")], running=[_EpicRow("wide-migration")]),
        [held, want], _inputs(tmp_path),
    )
    assert out.seam_blocked.total == 1


def test_a_parent_relative_path_is_not_flattened_into_a_repo_relative_one(tmp_path):
    """``lstrip("./")`` would strip a character SET and equate these two. It does not."""
    assert _seam_paths(f"## Files\n- {BT}../shared/x.py{BT}") == frozenset({"../shared/x.py"})
    assert _seam_paths(f"## Files\n- {BT}./shared/x.py{BT}") == frozenset({"shared/x.py"})


def test_a_directory_named_on_one_side_does_NOT_match_a_file_inside_it(tmp_path):
    """Pinned as a deliberate limit, not left for a later reader to find and call a bug.

    Under-reporting costs a lane one bounced claim. Over-reporting parks real work behind an
    overlap that does not exist, and nothing would ever contradict it.
    """
    held = _rec(tmp_path, "wide-migration", "skills/_shared/")
    want = _rec(tmp_path, "small-fix", "skills/_shared/agentflow/config.py")
    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("small-fix")], running=[_EpicRow("wide-migration")]),
        [held, want], _inputs(tmp_path),
    )
    assert out.seam_blocked.total == 0, "exact-string matching, and the limit is stated"
    assert out.seam_unknown_count == 0, "both sides named paths; the miss is the matcher's"


def test_a_glob_entry_matches_only_an_identical_glob(tmp_path):
    """The other documented under-reporting shape, pinned for the same reason."""
    held = _rec(tmp_path, "wide-migration", "skills/*/SKILL.md")
    want = _rec(tmp_path, "small-fix", "skills/steward/SKILL.md")
    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("small-fix")], running=[_EpicRow("wide-migration")]),
        [held, want], _inputs(tmp_path),
    )
    assert out.seam_blocked.total == 0


# --------------------------------------------------------------------------- #
# Entries the section parser promoted that cannot name a file (gh#257)
# --------------------------------------------------------------------------- #
def _spec_with_adjacent_code_spans(specs, slug):
    """A spec that writes two adjacent inline-code spans, which is all it takes.

    The section parser fires on any backticked token carrying a separator, so the ``/``
    BETWEEN the spans is returned as if it were a declared path. Nothing here is contrived:
    this is how a spec names a pair of sibling commands.
    """
    body = [
        "---", f"slug: {slug}", "current_state: approved-for-autonomous", "priority: 100",
        "---", f"# {slug}", "", "## Files", "",
        f"- {BT}reap{BT}/{BT}stand-down{BT} - the verb pair this epic touches",
        f"- {BT}skills/{slug}/only.py{BT} - the file it actually edits",
    ]
    p = specs / f"2026-09-04-{slug}.md"
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return p


def test_a_separator_between_two_code_spans_is_not_a_declared_path(tmp_path):
    """The single measured defect, and it is spelled identically wherever it occurs.

    gh#257 predicted junk would rarely collide with junk. This junk is a one-character
    string that every occurrence spells the same way, so it collides with itself perfectly:
    20 specs in the live corpus declared it.
    """
    section = f"## Files\n- {BT}reap{BT}/{BT}stand-down{BT}\n- {BT}a/b.py{BT}"
    assert _seam_paths(section) == frozenset({"a/b.py"})


def test_two_unrelated_specs_are_no_longer_blocked_by_a_shared_slash(tmp_path):
    """The consequence end to end, and it is the direction this signal calls unsafe.

    ``_attach_seam_signal``'s own docstring says a missed overlap costs a lane one bounced
    claim while a fabricated one parks real work behind nothing. Two specs that share no
    file, each merely naming a pair of sibling commands, produced exactly that fabrication.
    """
    specs, _ = _tree(tmp_path)
    _spec_with_adjacent_code_spans(specs, "holder")
    _spec_with_adjacent_code_spans(specs, "candidate")
    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("candidate")], running=[_EpicRow("holder")]),
        [], _inputs(tmp_path),
    )
    assert out.seam_blocked.total == 0, [
        (b.name, b.blocked_by, b.paths) for b in out.seam_blocked.items
    ]
    assert out.seam_unknown_count == 0, "both specs still declare one real path each"


def test_a_real_overlap_still_reports_and_its_paths_list_is_now_true(tmp_path):
    """The other half: the block survives, and what it says it compared is now true.

    Dropping noise must not cost a true block. It also must not leave ``/`` in the reported
    paths tuple of one that stands - that tuple is what a human reads to decide whether the
    overlap is real, and gh#257's weaker claim was exactly that it overstates itself. Thirty
    real blocks in the live corpus were reporting more than they compared.
    """
    specs, _ = _tree(tmp_path)
    for slug in ("holder", "candidate"):
        body = [
            "---", f"slug: {slug}", "current_state: approved-for-autonomous", "priority: 100",
            "---", f"# {slug}", "", "## Files", "",
            f"- {BT}reap{BT}/{BT}stand-down{BT}",
            f"- {BT}skills/_shared/agentflow/registry.py{BT}",
        ]
        (specs / f"2026-09-04-{slug}.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    out = _attach_seam_signal(
        _work(claimable=[_EpicRow("candidate")], running=[_EpicRow("holder")]),
        [], _inputs(tmp_path),
    )
    assert out.seam_blocked.total == 1, "the shared registry.py is a real overlap"
    assert out.seam_blocked.items[0].paths == ("skills/_shared/agentflow/registry.py",)


def test_a_prose_sentence_promoted_by_the_parser_is_not_a_declared_path(tmp_path):
    """Zero collisions in the corpus today, and included anyway - deliberately.

    Five live specs carry one of these in their ``paths`` tuple. That none has yet collided
    is a fact about which sentences happened to get quoted, not a property of the shape: any
    two specs quoting one sentence would block each other on it.
    """
    prose = " passes arbitrary flat frontmatter through. The word "
    assert _seam_paths(f"## Files\n- {BT}{prose}{BT}\n- {BT}a/b.py{BT}") == frozenset({"a/b.py"})


def test_a_spaced_entry_that_carries_a_separator_is_left_alone(tmp_path):
    """The prose branch's guard, pinned on its own so a tightening cannot quietly drop it.

    Requiring no ``/`` is what keeps a real path out of that branch. ``files_vs_diff.classify``
    has a second, tree-grounded guard for the same branch; this function has no tree, so this
    is the whole of its protection and it is the weaker of the two by construction.
    """
    spaced = "src/x.py and docs/y.md"
    assert _seam_paths(f"## Files\n- {BT}{spaced}{BT}") == frozenset({spaced})


def test_a_section_whose_every_entry_is_noise_is_unknown_rather_than_clear(tmp_path):
    """The empty-set case must reach ``None``, not ``frozenset()``.

    A spec that said only ``/`` has told you nothing locatable. Returning an empty set would
    make it match nothing and read as CLEAR; ``None`` counts it as unknown, which is the
    third state this function already keeps for "the loop skills".
    """
    assert _seam_paths(f"## Files\n- {BT}reap{BT}/{BT}stand-down{BT}") is None


def test_an_out_of_repo_path_is_kept_because_two_specs_can_share_one(tmp_path):
    """The deliberate non-drop, pinned so a later tightening has to argue with it.

    An absolute or parent-relative entry is a REAL file that two specs can both edit - four
    live specs share one such path. It is not repo-relative, but a seam check asks who
    touches the same file, not whose paths are well-formed.
    """
    outside = "~/.agentflow/config/environment.md"
    assert _seam_paths(f"## Files\n- {BT}{outside}{BT}") == frozenset({outside})


# --------------------------------------------------------------------------- #
# Non-vacuity — the fixtures must actually parse
# --------------------------------------------------------------------------- #
def test_the_fixtures_really_produce_paths(tmp_path):
    """Guards every hand-built assertion above.

    If ``_rec`` or ``_bug`` wrote a section ``files_section`` cannot read, those tests would
    pass by returning None everywhere and prove nothing.
    """
    rec = _rec(tmp_path, "probe", "docs/A.md", "install/provision.py")
    assert _seam_paths(Path(rec.source_path).read_text(encoding="utf-8")) == frozenset(
        {"docs/A.md", "install/provision.py"})
    assert _seam_paths(_bug(1, "docs/A.md").body) == frozenset({"docs/A.md"})
    assert _seam_paths(_no_files_rec(tmp_path, "quiet").source_path and
                       Path(_no_files_rec(tmp_path, "quiet").source_path).read_text(
                           encoding="utf-8")) is None
