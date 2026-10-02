"""Tests for `agentflow.reconcile` - the materialized view of `current_state` (E-0200 item 5).

The pass MOVES FILES, so the interesting assertions are all about what it refuses to do.
"""

from __future__ import annotations

import pytest

from agentflow import reconcile as rc


def _spec(root, project, bucket, name, state=None, partition="", body="# spec\n"):
    """Write a spec into <root>/projects/<project>/design/<bucket>[/<partition>]/<name>."""
    d = root / "projects" / project / "design" / bucket
    if partition:
        d = d / partition
    d.mkdir(parents=True, exist_ok=True)
    fm = "---\nslug: " + name.removesuffix(".md") + "\n"
    if state is not None:
        fm += f"current_state: {state}\n"
    fm += "---\n\n"
    path = d / name
    path.write_text(fm + body, encoding="utf-8")
    return path


def _plan(tmp_path, project=None):
    return rc.plan_reconcile(tmp_path, project)


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "state,expected",
    [("done", "done"), ("shipped", "done"), ("archived", "archived"),
     ("superseded", "archived")],
    ids=["done", "shipped", "archived", "superseded"],
)
def test_a_terminal_state_is_planned_into_its_partition(tmp_path, state, expected):
    _spec(tmp_path, "alpha", "specs", "a.md", state)
    plan = _plan(tmp_path)
    assert [m.dest for m in plan.moves] == [expected]


@pytest.mark.parametrize(
    "state",
    ["drafted", "planning", "speccing", "approved-for-autonomous", "in-progress", "pinned"],
)
def test_an_ACTIVE_state_stays_in_the_bucket_root(tmp_path, state):
    """Clicking `specs/` shows what is live - that is the whole point of the partition, so
    anything not terminal must stay where a human will see it."""
    _spec(tmp_path, "alpha", "specs", "a.md", state)
    plan = _plan(tmp_path)
    assert plan.moves == []
    assert plan.already_correct == 1


def test_a_closed_file_already_in_its_partition_is_left_alone(tmp_path):
    _spec(tmp_path, "alpha", "specs", "a.md", "done", partition="done")
    plan = _plan(tmp_path)
    assert plan.moves == []
    assert plan.already_correct == 1


def test_a_REOPENED_file_moves_back_out_to_active(tmp_path):
    """The view is derived in BOTH directions. A spec reopened from done to drafted has to
    come back where it can be seen, or `done/` silently becomes a graveyard that swallows
    live work."""
    _spec(tmp_path, "alpha", "specs", "a.md", "drafted", partition="done")
    plan = _plan(tmp_path)
    assert [(m.source, m.dest) for m in plan.moves] == [("done", "")]
    assert plan.moves[0].target.parent.name == "specs"


def test_a_file_moves_between_partitions_without_passing_through_active(tmp_path):
    _spec(tmp_path, "alpha", "specs", "a.md", "superseded", partition="done")
    plan = _plan(tmp_path)
    assert [(m.source, m.dest) for m in plan.moves] == [("done", "archived")]
    assert plan.moves[0].target.parent.name == "archived"


# --------------------------------------------------------------------------- #
# Refusals - the load-bearing half
# --------------------------------------------------------------------------- #
def test_a_file_with_NO_current_state_is_unplaceable_not_active(tmp_path):
    """THE THIRD OUTCOME, and 170 files on the live tree are in it. Treating a missing state
    as "active" would be a guess, and a materialized view that guesses has become a second
    source of truth - the exact thing the design forbids."""
    _spec(tmp_path, "alpha", "specs", "a.md", state=None)
    plan = _plan(tmp_path)
    assert plan.moves == []
    assert [u.reason for u in plan.unplaceable] == ["no readable current_state"]


def test_an_unparseable_file_is_unplaceable_and_names_the_error(tmp_path):
    d = tmp_path / "projects" / "alpha" / "design" / "specs"
    d.mkdir(parents=True)
    (d / "broken.md").write_text("no frontmatter at all\n", encoding="utf-8")
    plan = _plan(tmp_path)
    assert plan.moves == []
    assert "SpecFormatError" in plan.unplaceable[0].reason


def test_an_UNKNOWN_state_stays_active_rather_than_being_guessed_into_a_partition(tmp_path):
    """`ops-cutover-pending` and `blocked-on-stale-premise` are real values on the live tree.
    They are not terminal, so the file is live and belongs where a human looks. What must NOT
    happen is inventing a partition for them."""
    _spec(tmp_path, "alpha", "specs", "a.md", "ops-cutover-pending")
    plan = _plan(tmp_path)
    assert plan.moves == []
    assert plan.unplaceable == []


def test_a_name_collision_is_reported_and_NEVER_overwritten(tmp_path):
    """Two files with one name is a fact somebody has to look at. Clobbering one destroys the
    evidence that it ever happened, and this pass runs unattended."""
    _spec(tmp_path, "alpha", "specs", "dup.md", "done")
    _spec(tmp_path, "alpha", "specs", "dup.md", "done", partition="done", body="# the other\n")
    plan = _plan(tmp_path)
    assert plan.moves == []
    assert "target exists" in plan.collisions[0].reason


def test_apply_never_touches_an_unplaceable_or_colliding_file(tmp_path):
    """The refusals are only worth anything if `apply_plan` cannot reach them - which it
    cannot, because they were never put on the move list in the first place."""
    keep = _spec(tmp_path, "alpha", "specs", "nostate.md", state=None)
    _spec(tmp_path, "alpha", "specs", "dup.md", "done")
    collided = _spec(tmp_path, "alpha", "specs", "dup.md", "done",
                     partition="done", body="# other\n")
    moves = _spec(tmp_path, "alpha", "specs", "closed.md", "done")

    plan = _plan(tmp_path)
    assert rc.apply_plan(plan) == 1
    assert keep.exists(), "an unplaceable file was moved"
    assert collided.exists(), "a collision target was overwritten"
    assert not moves.exists() and (moves.parent / "done" / "closed.md").exists()


# --------------------------------------------------------------------------- #
# Scope, vocabulary and reporting
# --------------------------------------------------------------------------- #
def test_the_vocabulary_is_BORROWED_from_the_survey_not_restated(tmp_path):
    """A second list here would be a second definition of "done". The union of the two
    partitions must be exactly what the suite already calls terminal - so adding a state
    there cannot leave this pass silently unaware of it."""
    from agentflow.epic_survey import TERMINAL_CURRENT_STATES

    assert rc.DONE_STATES | rc.ARCHIVED_STATES == TERMINAL_CURRENT_STATES
    assert rc.DONE_STATES & rc.ARCHIVED_STATES == frozenset()


def test_a_state_named_only_in_the_SPEC_PROSE_is_not_silently_adopted(tmp_path):
    """The layout spec says `archived/` collects "superseded, `abandoned` and stale specs".
    `abandoned` is not in the survey's terminal set and does not occur anywhere in the live
    corpus, so this pass does NOT invent it - the file stays active.

    That is the borrowing rule doing its job in the uncomfortable direction. If `abandoned`
    ever needs to be terminal, it belongs in `TERMINAL_CURRENT_STATES`, where the DISPATCH
    gate reads it - and this pass then inherits it for free. Adding it here instead would
    make a file archived for the steward and dispatchable for the builders at the same time.
    """
    _spec(tmp_path, "alpha", "specs", "a.md", "abandoned")
    plan = _plan(tmp_path)
    assert plan.moves == [], "abandoned was placed without being in the shared vocabulary"


def test_project_scoping_never_reaches_a_sibling(tmp_path):
    _spec(tmp_path, "alpha", "specs", "a.md", "done")
    _spec(tmp_path, "beta", "specs", "b.md", "done")
    assert {m.project for m in _plan(tmp_path, "alpha").moves} == {"alpha"}
    assert {m.project for m in _plan(tmp_path).moves} == {"alpha", "beta"}


def test_every_design_bucket_is_walked_not_just_specs(tmp_path):
    for bucket in ("specs", "plans", "use-cases", "suggestions"):
        _spec(tmp_path, "alpha", bucket, "a.md", "done")
    assert {m.bucket for m in _plan(tmp_path).moves} == {
        "specs", "plans", "use-cases", "suggestions"
    }


# --------------------------------------------------------------------------- #
# A GENERATED FILE DECLARES ITSELF (gh#176)
#
# The bug mirror writes `design/bugs/index.md` and `design/bugs/closed-<YYYY-MM>.md`.
# They are a rendering of the tracker and correctly carry no frontmatter, so this pass
# refused them - correctly, and forever - and gained one more permanent refusal every
# month as the closed-month file rolled over. Two of three unplaceables were the mirror
# the day it shipped.
#
# The fix keys on the file's own banner rather than on a bucket list, so none of the
# fixtures below name a directory as authored or derived. Each writes its files RAW:
# `_spec` cannot express the shape at issue, because it always emits the `---` fence, so
# a `_spec`-built file PARSES and lands on the other unplaceable path entirely.
# --------------------------------------------------------------------------- #
def _generated(root, project, bucket, name, heading="# Open bugs"):
    """A file in the mirror's own shape: a heading, the banner, no frontmatter.

    The banner is IMPORTED from the writer, never retyped. A literal here would pass while
    the real mirror was refused the moment either side reworded it.
    """
    from agentflow.issue_intake import _DO_NOT_EDIT

    d = root / "projects" / project / "design" / bucket
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(f"{heading}\n\n{_DO_NOT_EDIT}\n\nNone.\n", encoding="utf-8")
    return path


def test_the_generated_banner_is_BORROWED_from_the_writer_not_retyped():
    """One definition of "generated", in the module that emits it. A second copy here is how
    the reader and the writer drift apart on the next rewording - the same failure the state
    and partition vocabularies are borrowed to avoid.

    WHAT THIS DOES NOT COVER, stated so nobody reads it as wider than it is: it pins the
    NAME BINDING, not the comparison. A future edit that retyped the banner text inside
    `_is_generated` and stopped consulting this constant would pass every case in this
    file, including this one. Guarding that would mean asserting on the comparison itself,
    which is a different test than the one written here.
    """
    from agentflow.issue_intake import _DO_NOT_EDIT

    assert rc.GENERATED_BANNER is _DO_NOT_EDIT


def test_a_generated_file_is_SKIPPED_rather_than_reported_unplaceable(tmp_path):
    """(a) The live shape, exactly. No frontmatter, so it cannot parse; a banner, so it does
    not need to. It is neither moved nor named as a refusal."""
    index = _generated(tmp_path, "alpha", "bugs", "index.md")
    plan = _plan(tmp_path)
    assert plan.unplaceable == [], [u.reason for u in plan.unplaceable]
    assert plan.moves == []
    assert index.is_file(), "a skipped file must be left exactly where it is"


def test_the_refusal_count_does_NOT_grow_with_the_MONTH_COUNT(tmp_path):
    """(b) The half of the bug that made it worth fixing rather than tolerating.

    `closed-<YYYY-MM>.md` is written once per month and earlier months are frozen, so the
    mirror's file count rises forever. One file proves the skip; two with different stamps
    prove the refusal list does not scale with the calendar."""
    _generated(tmp_path, "alpha", "bugs", "index.md")
    _generated(tmp_path, "alpha", "bugs", "closed-2026-09.md", heading="# Bugs closed 2026-09")
    one_month = _plan(tmp_path)
    _generated(tmp_path, "alpha", "bugs", "closed-2026-10.md", heading="# Bugs closed 2026-10")
    next_month = _plan(tmp_path)

    assert one_month.unplaceable == [] and next_month.unplaceable == []
    assert (one_month.generated_skipped, next_month.generated_skipped) == (2, 3), (
        "the skip must be per FILE - a count that ignores the new month is not measuring it"
    )


def test_a_genuine_unplaceable_is_STILL_reported_alongside_the_mirror(tmp_path):
    """(c) The property the list exists for, asserted in the same fixture as the fix.

    A change that quietened ALL unplaceables would pass every test above. This one fails
    against it: the malformed authored file has no banner, so it is still parsed and still
    named, while the mirror beside it is not."""
    _generated(tmp_path, "alpha", "bugs", "index.md")
    _generated(tmp_path, "alpha", "bugs", "closed-2026-09.md")
    d = tmp_path / "projects" / "alpha" / "design" / "specs"
    d.mkdir(parents=True)
    (d / "broken.md").write_text("no frontmatter at all\n", encoding="utf-8")

    plan = _plan(tmp_path)
    assert [u.path.name for u in plan.unplaceable] == ["broken.md"]
    assert plan.generated_skipped == 2
    assert plan.is_clean is False, "a real refusal must still make the tree unclean"


def test_an_UNKNOWN_bucket_is_still_walked_file_by_file(tmp_path):
    """(d) The fails-closed property, and the proof this is not an allow-list in disguise.

    `notebooks/` is a bucket no list anywhere knows about. Both files in it are treated on
    their own evidence: the banner one is skipped, the authored one is walked and reported.
    An allow-list would ignore both; a `bugs/` exclusion would report both."""
    _generated(tmp_path, "alpha", "notebooks", "derived.md")
    d = tmp_path / "projects" / "alpha" / "design" / "notebooks"
    (d / "authored.md").write_text("no frontmatter at all\n", encoding="utf-8")

    plan = _plan(tmp_path)
    assert [u.path.name for u in plan.unplaceable] == ["authored.md"]
    assert plan.generated_skipped == 1


def test_an_AUTHORED_file_inside_a_derived_bucket_is_reconciled_normally(tmp_path):
    """The unit is the FILE, not the folder. A spec that lands in `bugs/` carries no banner,
    so it is parsed and moved like any other - which a directory exclusion could not do, and
    is the concrete behaviour the docstring promises.

    Both assertions are load-bearing, and they fail in opposite directions: the move fails
    under an exclusion of `bugs/`, which would skip the spec along with the mirror, and the
    empty refusal list fails on the unfixed tree, where the mirror in this same bucket is
    reported. Without the second the test passed before the change and guarded only the fork
    not taken."""
    spec = _spec(tmp_path, "alpha", "bugs", "a.md", "done")
    _generated(tmp_path, "alpha", "bugs", "index.md")

    plan = _plan(tmp_path)
    assert [(m.bucket, m.dest) for m in plan.moves] == [("bugs", "done")]
    assert plan.unplaceable == [], "the mirror beside it was still refused"
    assert rc.apply_plan(plan) == 1
    assert (spec.parent / "done" / "a.md").is_file()


def test_the_report_COUNTS_the_skipped_generated_files(tmp_path):
    """Silence would make "this tree has no mirror" and "the mirror was skipped" the same
    output - the absence-vs-success confusion that keeps costing this repository. The count
    is unconditional so the zero case says the first and the non-zero case says the second."""
    empty = rc.render(_plan(tmp_path), apply=False)
    assert "0 generated (skipped)" in empty

    _generated(tmp_path, "alpha", "bugs", "index.md")
    _generated(tmp_path, "alpha", "bugs", "closed-2026-09.md")
    text = rc.render(_plan(tmp_path), apply=False)
    assert "2 generated (skipped)" in text
    assert "index.md" not in text, (
        "naming each generated file every sweep rebuilds exactly the noise gh#176 removed"
    )


def test_a_skipped_generated_file_does_not_make_a_clean_tree_UNCLEAN(tmp_path):
    """The mirror is the steady state, not a pending decision. Counting it as dirt would keep
    a correctly-mirrored tree permanently unclean - gh#176 in a different costume."""
    _spec(tmp_path, "alpha", "specs", "a.md", "drafted")
    _generated(tmp_path, "alpha", "bugs", "index.md")
    plan = _plan(tmp_path)
    assert plan.generated_skipped == 1
    assert plan.is_clean is True


def test_status_is_NEVER_walked(tmp_path):
    """status/ is lock-protected fleet coordination. It does not move, mirror or sync, and a
    pass that relocated a registry file would be the worst possible version of this bug."""
    d = tmp_path / "projects" / "alpha" / "status"
    d.mkdir(parents=True)
    (d / "registry.md").write_text("---\ncurrent_state: done\n---\n", encoding="utf-8")
    plan = _plan(tmp_path)
    assert plan.moves == [] and plan.unplaceable == []


def test_a_missing_data_root_returns_an_empty_plan_rather_than_raising(tmp_path):
    plan = rc.plan_reconcile(tmp_path / "nothing-here")
    assert plan.is_clean and plan.already_correct == 0


def test_is_clean_is_false_while_anything_is_unreadable(tmp_path):
    """A tree with nothing to move and 170 unreadable files is not reconciled; it is
    unexamined. Reporting it clean is how the unreadable set stays invisible."""
    _spec(tmp_path, "alpha", "specs", "a.md", state=None)
    plan = _plan(tmp_path)
    assert plan.moves == []
    assert plan.is_clean is False


def test_the_report_names_the_unplaceable_files_and_explains_the_refusal(tmp_path):
    _spec(tmp_path, "alpha", "specs", "nostate.md", state=None)
    text = rc.render(_plan(tmp_path), apply=False)
    assert "nostate.md" in text
    assert "1 unplaceable" in text
    assert "guessing" in text, "the refusal must say why, not just count"


def test_dry_run_and_apply_are_distinguishable_in_the_output(tmp_path):
    _spec(tmp_path, "alpha", "specs", "a.md", "done")
    plan = _plan(tmp_path)
    assert "[DRY-RUN]" in rc.render(plan, apply=False)
    assert "[APPLY]" in rc.render(plan, apply=True)


# --------------------------------------------------------------------------- #
# The shared contract with the survey (gh#122)
#
# This pass WRITES the partition directories and `epic_survey` READS them, and for
# a while only one half knew where they were: the globs named the bucket roots, so
# every record this pass filed left the board - and with it `_done_slugs`, so a
# finished prerequisite read as unsatisfied and stranded every dependent naming it.
#
# Neither test below writes a directory name. Both are derived from the two modules'
# own constants, so no future edit can satisfy them by editing a literal.
# --------------------------------------------------------------------------- #
_SURVEY_CUTOFF = "2020-01-01T00:00:00+00:00"


def test_the_PARTITION_vocabulary_is_BORROWED_from_the_survey_not_restated():
    """The DIRECTORY half of the rule the STATE half has had all along. A local copy
    here is exactly how the two halves drifted apart in the first place."""
    from agentflow.epic_survey import TERMINAL_PARTITIONS

    assert rc.TERMINAL_PARTITIONS is TERMINAL_PARTITIONS


def test_the_survey_REACHES_every_partition_reconcile_can_write_to(tmp_path):
    """Structural guard: enumerate this pass's codomain and check each destination against
    the survey's patterns. It names the defect precisely - a glob that cannot reach a
    partition - where the round-trip below can only report a lost record.

    Matched with `Path.glob`, the mechanism the survey itself uses, NOT `fnmatch`: an
    fnmatch `*` crosses path separators, so `specs/*.md` appears to match `specs/done/x.md`
    and this guard passes on the broken tree it exists to catch. Measured, not reasoned -
    the first draft of this test did exactly that.
    """
    from agentflow.epic_survey import DEFAULT_SURVEY_PATTERNS, TERMINAL_CURRENT_STATES

    dests = {rc._partition_for(s) for s in TERMINAL_CURRENT_STATES} | {""}
    assert dests - {""}, "reconcile has no partitions - the fixture, not the contract, is wrong"
    # Both buckets: plan_reconcile walks EVERY directory under design/, so a plan is moved
    # on the same rule as a spec and a specs-only reach leaves the mirror-image hole.
    for bucket, globs in (("specs", DEFAULT_SURVEY_PATTERNS.spec_globs),
                          ("plans", DEFAULT_SURVEY_PATTERNS.plan_globs)):
        for dest in sorted(dests):
            d = tmp_path / "projects" / "p" / "design" / bucket / dest
            d.mkdir(parents=True, exist_ok=True)
            (d / "x.md").write_text("---\nslug: x\n---\n", encoding="utf-8")
            reached = {p for g in globs for p in tmp_path.glob(g)}
            assert d / "x.md" in reached, (
                f"reconcile can move a {bucket} file to {dest or '(active)'}/, "
                f"which no {bucket} glob reaches"
            )
            (d / "x.md").unlink()


def test_every_directory_reconcile_can_MOVE_a_file_into_is_still_surveyed(tmp_path):
    """The behavioural round-trip: survey, reconcile for real, survey again, same records.

    The one test that would have caught gh#122, because it is the only one exercising the
    two modules' shared contract rather than each module's half of it. Add a partition
    without widening the globs and `after` loses a slug; narrow a glob and it loses one;
    add a terminal state and the fixture grows itself to cover it.
    """
    from agentflow.epic_survey import TERMINAL_CURRENT_STATES, survey_epics

    for state in sorted(TERMINAL_CURRENT_STATES):
        _spec(tmp_path, "alpha", "specs", f"{state}.md", state)

    before = {r.slug for r in survey_epics(tmp_path, _SURVEY_CUTOFF, include_archived=True)}
    assert before, "the fixture surveyed nothing - a vacuous pass, not a green contract"

    plan = _plan(tmp_path)
    # Pin the move count to the vocabulary size: a pass that plans nothing would otherwise
    # satisfy `after == before` trivially.
    assert rc.apply_plan(plan) == len(TERMINAL_CURRENT_STATES)
    moved = sorted((tmp_path / "projects/alpha/design/specs").rglob("*.md"))
    # ...and prove the files actually left the bucket root, so a rename-in-place cannot pass.
    assert {p.parent.name for p in moved} == set(rc.TERMINAL_PARTITIONS)

    after = {r.slug for r in survey_epics(tmp_path, _SURVEY_CUTOFF, include_archived=True)}
    assert after == before, f"reconcile moved these out of the survey's reach: {before - after}"


# --------------------------------------------------------------------------- #
# The backup group travels with the primary (gh#288)
#
# `done/` and `archived/` are a materialized view built by MOVING the primary file.
# The `.bak` files other loops wrote BESIDE it are part of that file, not part of the
# path, and a move that leaves them behind is an INCOMPLETE MOVE. `backups reap` then
# sees a group whose primary is absent from that directory and refuses it on the rule
# that its backups are "the only surviving copy" - a sentence that is FALSE for a moved
# spec, whose primary is one directory over and alive.
#
# Measured before the fix: 66 refused groups, 50 of them manufactured this way, climbing
# by one per CORRECT reconcile sweep. The sixteen real refusals - the ones a human has to
# decide - were buried in them.
# --------------------------------------------------------------------------- #
from agentflow import backups as bk  # noqa: E402 - grouped with the tests that use it


def _bak(path, firing="steward-f1", when=None, seq=1, text="copy"):
    """A CANONICAL backup of `path`, written beside it."""
    from datetime import datetime, timezone

    when = when or datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    p = path.with_name(bk.backup_name(path.name, firing, when, seq))
    p.write_text(text, encoding="utf-8")
    return p


def test_a_specs_BACKUPS_move_with_it(tmp_path):
    """The whole group or nothing. A backup left at the old path is orphaned from a file
    that is alive, and every downstream reader of that group now reasons about a primary
    that is missing only from where it is looking."""
    spec = _spec(tmp_path, "alpha", "specs", "a.md", "done")
    baks = [_bak(spec, seq=1), _bak(spec, seq=2)]

    assert rc.apply_plan(_plan(tmp_path)) == 1
    moved = spec.parent / "done"
    assert (moved / "a.md").is_file()
    assert {p.name for p in moved.iterdir()} == {"a.md"} | {b.name for b in baks}
    assert not any(b.exists() for b in baks), "a backup was left behind at the old path"


def test_a_LEGACY_shaped_backup_moves_too(tmp_path):
    """Legacy is the majority of the live tree. A fix that only carries canonical names
    leaves exactly the files this bug is made of."""
    spec = _spec(tmp_path, "alpha", "specs", "a.md", "archived")
    legacy = [spec.with_name("a.md" + s) for s in (".bak", ".bak-flip3", ".bak2")]
    for p in legacy:
        p.write_text("old", encoding="utf-8")

    rc.apply_plan(_plan(tmp_path))
    moved = spec.parent / "archived"
    assert {p.name for p in moved.iterdir()} == {"a.md"} | {p.name for p in legacy}


def test_a_backup_belonging_to_a_DIFFERENT_primary_is_left_alone(tmp_path):
    """`a.md.notes.bak` is a backup of `a.md.notes`, not of `a.md`. Dragging it along is
    the mirror-image defect: a move that splits a group nobody asked it to touch."""
    spec = _spec(tmp_path, "alpha", "specs", "a.md", "done")
    other = spec.with_name("a.md.notes.bak")
    other.write_text("not mine", encoding="utf-8")
    neighbour = _spec(tmp_path, "alpha", "specs", "b.md", "drafted")
    neighbour_bak = _bak(neighbour)

    rc.apply_plan(_plan(tmp_path))
    assert other.is_file(), "a backup of a DIFFERENT primary was dragged along"
    assert neighbour_bak.is_file(), "an unrelated file's backup was moved"
    assert not (spec.parent / "done" / other.name).exists()


def test_a_backup_OF_a_backup_travels_with_the_whole_chain(tmp_path):
    """The one shape where the two parsers disagree. `a.md.bak-flip3` groups to `a.md`, and
    its OWN canonical backup groups to `a.md.bak-flip3` - so carrying only the first level
    moves a file and leaves its backup behind, re-creating the bug one level down."""
    spec = _spec(tmp_path, "alpha", "specs", "a.md", "done")
    legacy = spec.with_name("a.md.bak-flip3")
    legacy.write_text("old", encoding="utf-8")
    nested = _bak(legacy)

    rc.apply_plan(_plan(tmp_path))
    moved = spec.parent / "done"
    assert {p.name for p in moved.iterdir()} == {"a.md", legacy.name, nested.name}


def test_a_backup_that_is_ALSO_a_spec_refuses_the_move_instead_of_aborting_mid_sweep(tmp_path):
    """A path may not be planned twice, and the sweep may not die finding out.

    `plan_reconcile` globs `*.md`, so a backup whose own name ENDS in `.md` -
    `a.md.bak.<stamp>.<firing>.01.md`, or a legacy `cp a.md a.md.bak-flip3.md` - is BOTH a
    move candidate in its own right (it carries a copy of the spec's frontmatter, so it
    parses to the same destination) AND a member of `backup_group(a.md)`.

    Before this guard the plan carried the same path twice and `apply_plan` aborted with
    `FileNotFoundError` on the second rename, PART WAY THROUGH a tree-wide move. That is a
    hard abort in place of a reported refusal, and it leaves exactly the half-reconciled
    tree this epic exists to stop producing - introduced BY the fix for it. Master handled
    the shape benignly, so the regression is ours and the guard belongs with the change.

    Both reviewers found this independently on a fixture, and both proposed the same
    remedy, which is also the one the module already uses one rule up: **a group that
    cannot move cleanly does not move at all**, and the ambiguity is reported for a human
    rather than resolved by guessing which role the file is playing.

    Latent rather than live: 0 of the ~900 backup-shaped files on the tree today end in
    `.md`, and `backups.py` documents the tail-last format that keeps it that way. A guard
    for an out-of-contract shape is still worth its four lines when the alternative is an
    aborted sweep.
    """
    spec = _spec(tmp_path, "alpha", "specs", "a.md", "done")
    # A backup of `a.md` that is itself named `*.md`, carrying the same frontmatter.
    twice = spec.parent / "a.md.bak.20260903T100000Z.steward-f1.01.md"
    twice.write_text(spec.read_text(encoding="utf-8"), encoding="utf-8")

    plan = _plan(tmp_path)
    assert plan.moves == [], "a path planned twice must not reach apply_plan"
    assert plan.collisions, "the ambiguity must be REPORTED, not silently dropped"
    reason = plan.collisions[0].reason
    assert twice.name in reason and "backup" in reason, reason

    # Nothing moved, nothing crashed, and both files are exactly where they were.
    assert rc.apply_plan(plan) == 0
    assert spec.is_file() and twice.is_file()


def test_a_COLLIDING_backup_name_refuses_the_WHOLE_move(tmp_path):
    """A half-moved group is strictly worse than an unmoved one: the primary would be in
    `done/` and part of its history at the bucket root, which is the very state this fix
    exists to prevent. So a colliding backup is a COLLISION on the same terms as a
    colliding primary - reported for a human, and never reached by `apply_plan`."""
    spec = _spec(tmp_path, "alpha", "specs", "a.md", "done")
    bak = _bak(spec)
    squatter = spec.parent / "done" / bak.name
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("someone else", encoding="utf-8")

    plan = _plan(tmp_path)
    assert plan.moves == []
    assert "backup" in plan.collisions[0].reason and bak.name in plan.collisions[0].reason
    assert rc.apply_plan(plan) == 0
    assert spec.is_file() and bak.is_file(), "a refused move was half-applied anyway"
    assert squatter.read_text(encoding="utf-8") == "someone else"


# --------------------------------------------------------------------------- #
# END-TO-END: the claim the epic actually makes
# --------------------------------------------------------------------------- #
def test_a_reconciled_tree_does_not_manufacture_reap_refusals(tmp_path):
    """BOTH DIRECTIONS IN ONE FIXTURE, because either alone is a green lie.

    * The moved spec's group must NOT be refused - its primary is alive, one directory
      over, and the refusal it used to draw was manufactured by this pass.
    * A group whose primary is GENUINELY gone must still be refused. The refusal rule is
      correct as written; this epic removes the false entries, it does not soften the rule.
    """
    spec = _spec(tmp_path, "alpha", "specs", "a.md", "done")
    moved_baks = [_bak(spec, seq=1), _bak(spec, seq=2)]

    orphan_dir = tmp_path / "projects" / "alpha" / "design" / "specs"
    orphan = orphan_dir / "gone.md"
    orphan.write_text("---\nslug: gone\ncurrent_state: done\n---\n", encoding="utf-8")
    orphan_bak = _bak(orphan)
    orphan.unlink()  # the primary is genuinely gone: this backup IS the last copy

    rc.apply_plan(_plan(tmp_path))

    plan = bk.plan_reap(tmp_path)
    refused = {r.primary for r in plan.refusals}
    assert (spec.parent / "done" / "a.md") not in refused
    assert spec.parent / "a.md" not in refused, "the moved spec still draws a false refusal"
    assert refused == {orphan}, f"unexpected refusals: {refused}"
    assert all(b.exists() is False for b in moved_baks)
    assert orphan_bak.is_file(), "a refused group must be left exactly as it is"


def test_a_backup_that_fails_to_move_leaves_the_VIEW_correct(tmp_path):
    """ATOMICITY, ANSWERED RATHER THAN ASSUMED. Per-file `rename` is not one transaction,
    so the question is not whether a partial failure can happen but which way it falls.

    Primary first means a mid-group failure leaves the view CORRECT - the spec is where its
    `current_state` says - with one straggler orphaned at the old path, which is today's
    steady state for one file instead of all of them, and which the reap reports. Backups
    first would leave the primary behind (the view wrong) AND its already-moved backups
    squatting on the target, which this pass's own collision rule would then refuse for
    good. The error still propagates; it is not counted and continued.
    """
    spec = _spec(tmp_path, "alpha", "specs", "a.md", "done")
    first, second = _bak(spec, seq=1), _bak(spec, seq=2)
    plan = _plan(tmp_path)
    doomed = plan.moves[0].backup_moves[-1][0]

    real = type(spec).rename

    def rename(self, target):
        if self == doomed:
            raise OSError("injected: the filesystem said no")
        return real(self, target)

    import pathlib

    original = pathlib.Path.rename
    pathlib.Path.rename = rename
    try:
        with pytest.raises(OSError, match="injected"):
            rc.apply_plan(plan)
    finally:
        pathlib.Path.rename = original

    done = spec.parent / "done"
    assert (done / "a.md").is_file(), "the primary did not move first"
    assert doomed.is_file(), "the straggler vanished"
    assert sorted(p.name for p in done.iterdir()) == sorted(
        {"a.md"} | ({first.name, second.name} - {doomed.name})
    )
