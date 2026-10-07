"""Tests for `agentflow.backups` - the backup filename contract and the reaper.

THE REAPER DELETES FILES UNDER A TREE GIT CANNOT CONTRADICT, so the interesting assertions
here are all about what it refuses to do and what it leaves alone. Every test builds its own
fixture tree under `tmp_path`; nothing in this module reads or writes a real `data_root`.

The legacy fixtures are the suffix families measured on the live tree, not invented ones. A
parser that recognises a made-up shape and misses `.bak-flip3` recognises nothing that
matters.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow import backups as bk

NEWLINE = chr(10)
BASE = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)

SKILLS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SKILLS_ROOT.parent

#: Suffix families observed on the live tree. Normalised digit runs restored to plausible
#: literals; the SHAPES are what were measured.
LEGACY_SUFFIXES = (
    ".bak",
    ".bak-refinement-20260724",
    ".bak-refinement-20260803-144908",
    ".bak-flip3",
    ".bak-f7",
    ".bak-reconcile-2",
    ".bak-tagging-3",
    ".bak-promote-1-2",
    ".bak-projectfield-1",
    ".bak-audit-2",
    ".bak-devmanager-1",
    ".bak-e4c1aa",
    ".bak2",
    ".bak3",
)

#: The two families that put their marker LAST rather than appending after it.
LEGACY_INFIX = (".corrupted.bak", ".pre-reconstruct.bak")


def _write(path: Path, text: str = "x", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))
    return path


def _group(dir_: Path, primary: str, count: int, firing: str = "dev-manager-f592") -> list[Path]:
    """A primary plus `count` canonical backups, oldest first, one second apart."""
    _write(dir_ / primary)
    base = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    made = []
    for i in range(count):
        when = base + timedelta(seconds=i)
        p = dir_ / bk.backup_name(primary, firing, when)
        _write(p, f"copy {i}", mtime=when.timestamp())
        made.append(p)
    return made


# --------------------------------------------------------------------------- #
# The format
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "primary,firing,seq",
    [
        ("2026-08-30-fleet-wake-watchdog.md", "dev-manager-f592", 1),
        ("registry.md", "steward-f0041", 1),
        (".manifest.json", "jr-dev-2-f0007", 2),
        ("no-extension", "auditor-f1", 99),
    ],
    ids=["spec", "registry", "dotfile-manifest", "extensionless"],
)
def test_the_format_round_trips(primary, firing, seq):
    when = datetime(2026, 9, 3, 10, 33, 20, tzinfo=timezone.utc)
    name = bk.backup_name(primary, firing, when, seq)
    parsed = bk.parse_backup_name(name)
    assert parsed is not None
    assert parsed.primary == primary
    assert parsed.firing == firing
    assert parsed.seq == seq
    assert parsed.taken_at == when
    # The grouping key agrees with the contract parser on a canonical name.
    assert bk.primary_for(name) == primary


def test_the_tail_goes_AFTER_the_extension_so_the_survey_never_sees_a_backup():
    """`survey_epics` and `reconcile` both glob `*.md`. A backup that ends in `.md` is a
    phantom epic in the queue, forever, once per backup ever taken."""
    name = bk.backup_name("some-epic-design.md", "dev-manager-f592")
    assert not name.endswith(".md")
    assert not name.endswith(".json")


def test_two_firings_backing_up_the_same_file_in_the_same_second_do_not_collide():
    when = datetime(2026, 9, 3, 10, 33, 20, tzinfo=timezone.utc)
    a = bk.backup_name("registry.md", "lead-dev-f18", when)
    b = bk.backup_name("registry.md", "steward-f18", when)
    assert a != b
    assert bk.parse_backup_name(a).primary == bk.parse_backup_name(b).primary


def test_one_firing_backing_up_the_same_file_twice_in_one_second_does_not_collide(tmp_path):
    """`seq` exists ONLY for this case. Without it the format satisfies the letter of
    "cannot collide between two firings" and still loses data."""
    when = datetime(2026, 9, 3, 10, 33, 20, tzinfo=timezone.utc)
    src = _write(tmp_path / "registry.md")
    first = bk.next_backup_path(src, "dev-manager-f592", when)
    _write(first)
    second = bk.next_backup_path(src, "dev-manager-f592", when)
    assert first != second
    assert bk.parse_backup_name(second.name).seq == 2


def test_next_backup_path_CLAIMS_the_name_so_two_callers_cannot_be_handed_the_same_one(tmp_path):
    """The TOCTOU that `seq` alone could not close. `exists()` then return is a check whose
    answer is stale by the time the caller writes: two writers both saw "free", both got seq
    01, and the second write silently ate the first. `O_CREAT | O_EXCL` is the one step the
    filesystem serialises, so the winner owns the name.

    NOTHING IS WRITTEN BETWEEN THE TWO CALLS HERE - that is the race, reproduced in one
    thread. Reverting to `exists()` returns the same path twice and reds this.
    """
    when = datetime(2026, 9, 3, 10, 33, 20, tzinfo=timezone.utc)
    src = _write(tmp_path / "registry.md")
    first = bk.next_backup_path(src, "dev-manager-f592", when)
    second = bk.next_backup_path(src, "dev-manager-f592", when)
    assert first != second
    assert first.exists() and second.exists(), "the claim IS the file; a path alone is a race"
    assert first.stat().st_size == 0, "the claim is empty - the caller still writes the copy"


def test_lexical_sort_equals_chronological_sort():
    when = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    moments = [when + timedelta(seconds=s) for s in (0, 59, 60, 3600, 86400, 86400 * 400)]
    names = [bk.backup_name("registry.md", "dev-manager-f1", m) for m in moments]
    assert sorted(names) == names
    parsed = [bk.parse_backup_name(n).taken_at for n in sorted(names)]
    assert parsed == sorted(parsed)


def test_a_malformed_canonical_name_is_not_silently_absorbed_as_legacy():
    """`BACKUP_RE` is a contract; `ANY_BACKUP_RE` is a grouping key. A firing that writes a
    stamp with dashes in it must fail the contract loudly rather than pass as legacy."""
    bad = "registry.md.bak.2026-09-03.dev-manager-f592.01"
    assert bk.parse_backup_name(bad) is None
    assert bk.primary_for(bad) == "registry.md"


def test_backup_name_rejects_a_firing_id_it_cannot_parse_back():
    with pytest.raises(ValueError):
        bk.backup_name("registry.md", "Dev_Manager F592")


@pytest.mark.parametrize("firing", ["steward", "dev-manager", "f592", "steward-f", "steward-fx"])
def test_a_firing_WITHOUT_its_number_is_refused(firing):
    """The collision argument rests entirely on this field: two firings differ because
    `(actor, firing-number)` is unique fleet-wide. `BACKUP_RE` accepts a bare `steward`, so
    with no separate check the load-bearing half of that argument lived in caller prose - the
    exact arrangement this module exists to end."""
    with pytest.raises(ValueError):
        bk.backup_name("registry.md", firing)


def test_format_stamp_REFUSES_a_naive_datetime_rather_than_relabelling_it():
    """It used to `.replace(tzinfo=utc)`, which renames the clock without converting it. A
    caller passing `datetime.now()` got a stamp wrong by the local offset, in the name's only
    human-readable field, with nothing downstream able to notice."""
    with pytest.raises(ValueError):
        bk.format_stamp(datetime(2026, 9, 3, 10, 33, 20))
    with pytest.raises(ValueError):
        bk.backup_name("registry.md", "steward-f9", datetime(2026, 9, 3, 10, 33, 20))


def test_format_stamp_CONVERTS_an_aware_non_utc_datetime():
    """The other half: an aware datetime says which clock it came from, so converting it is
    arithmetic rather than a guess."""
    minus_five = timezone(timedelta(hours=-5))
    assert bk.format_stamp(datetime(2026, 9, 3, 5, 33, 20, tzinfo=minus_five)) == (
        bk.format_stamp(datetime(2026, 9, 3, 10, 33, 20, tzinfo=timezone.utc))
    )


# --------------------------------------------------------------------------- #
# Legacy recognition
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("suffix", LEGACY_SUFFIXES, ids=[s[1:] for s in LEGACY_SUFFIXES])
def test_a_legacy_family_is_recognised_and_resolves_to_its_primary(suffix):
    assert bk.primary_for(f"legacy-loop-state.md{suffix}") == "legacy-loop-state.md"


@pytest.mark.parametrize("suffix", LEGACY_INFIX, ids=[s[1:] for s in LEGACY_INFIX])
def test_a_marker_last_legacy_name_resolves_to_a_primary_that_will_not_exist(suffix):
    """`foo.md.corrupted.bak` resolves to `foo.md.corrupted`, which is not a real file - so
    the group lands in the refusal class and the reaper never touches it. A name the parser
    misreads becomes a file the reaper REFUSES, not a file it deletes wrongly."""
    primary = bk.primary_for(f"a-plan.md{suffix}")
    assert primary is not None
    assert primary != "a-plan.md"


@pytest.mark.parametrize("name", ["recipe.baked.md", "plan.md", "registry.md", "backup.txt"])
def test_an_ordinary_file_is_not_mistaken_for_a_backup(name):
    assert bk.primary_for(name) is None


@pytest.mark.parametrize(
    "legacy_primary",
    ["state.md.bak", "state.md.bak-flip3", "state.md.bak-refinement-20260724"],
    ids=["bare-marker", "flip3", "refinement"],
)
def test_a_backup_OF_a_legacy_backup_is_where_the_two_parsers_DISAGREE(legacy_primary):
    """Greedy `primary` in the contract regex: during a sweep a firing may back up a file
    whose own name contains `.bak`.

    THIS IS THE ONE SHAPE WHERE `parse_backup_name` AND `primary_for` GIVE DIFFERENT ANSWERS,
    and the difference decides which file gets deleted. `primary_for` is non-greedy and stops
    at the FIRST marker, so it names the wrong file here - pinned rather than repaired,
    because non-greedy is still the only workable answer for a legacy name, which has no
    second marker to be confused by. `plan_reap` therefore asks the CONTRACT parser first;
    `test_a_backup_OF_a_backup_does_not_get_its_own_primary_reaped` is that rule's teeth.
    """
    name = bk.backup_name(legacy_primary, "steward-f9")
    assert bk.parse_backup_name(name).primary == legacy_primary
    assert bk.primary_for(name) != legacy_primary
    assert bk.primary_for(name) == "state.md"


# --------------------------------------------------------------------------- #
# The grouping rule, as a callable (gh#288)
#
# `reconcile` has to ask the same question this module asks - "which files belong to
# this primary" - when it moves a spec into `done/`. It calls `group_primary` /
# `backup_group` rather than re-deriving the parser order, because a second copy of
# the contract-first rule would eventually disagree about which file a backup protects.
# --------------------------------------------------------------------------- #
def test_group_primary_resolves_the_same_group_the_reaper_does(tmp_path):
    """The teeth on "one definition": whatever `plan_reap` groups a file under, this
    function returns. A drift between them is a drift about what gets deleted."""
    d = tmp_path / "specs"
    _group(d, "a.md", 3)
    _group(d, "a.md.bak-flip3", 3)  # a chain: a legacy backup that is itself backed up
    _group(d, "a.md.notes", 3)
    _write(d / "a.md.notes.bak")

    # keep=1 makes every group over-deep, so each surplus backup arrives with the primary
    # the REAPER filed it under. Those are the pairings that decide deletions.
    plan = bk.plan_reap(d, keep=1)
    assert plan.removals, "the fixture planned nothing - vacuous, not agreeing"
    for item in plan.removals:
        assert bk.group_primary(item.path) == item.primary

    assert bk.group_primary(d / "a.md.notes.bak") == d / "a.md.notes"
    assert bk.group_primary(d / "a.md.bak-flip3") == d / "a.md"
    assert bk.group_primary(d / bk.backup_name("a.md.bak-flip3", "steward-f9")) == (
        d / "a.md.bak-flip3"
    ), "the CONTRACT parser must win over the permissive one"
    assert bk.group_primary(d / "a.md") is None


def test_backup_group_is_TRANSITIVE_so_a_chain_is_never_split(tmp_path):
    """A backup can itself have backups. Taking only the first level moves `a.md` and
    `a.md.bak-flip3` and strands the third file - the same split, one level down."""
    d = tmp_path / "specs"
    canonical = _group(d, "a.md", 2)
    legacy = _write(d / "a.md.bak-flip3")
    nested = _write(d / bk.backup_name("a.md.bak-flip3", "steward-f9"))

    assert bk.backup_group(d / "a.md") == sorted(canonical + [legacy, nested])


def test_backup_group_never_reaches_another_primary_or_another_directory(tmp_path):
    """A group is `(directory, primary name)`. `a.md.notes.bak` belongs to `a.md.notes`,
    and a same-named backup one directory over belongs to that directory's primary."""
    d = tmp_path / "specs"
    mine = _group(d, "a.md", 1)
    _write(d / "a.md.notes.bak")
    _write(d / "b.md.bak-flip3")
    elsewhere = _group(d / "done", "a.md", 1)

    assert bk.backup_group(d / "a.md") == mine
    assert bk.backup_group(d / "done" / "a.md") == elsewhere


def test_backup_group_of_a_file_in_no_directory_is_empty_not_an_error(tmp_path):
    assert bk.backup_group(tmp_path / "nowhere" / "a.md") == []
    assert bk.backup_group(tmp_path / "a.md") == []


# --------------------------------------------------------------------------- #
# The reaper
# --------------------------------------------------------------------------- #
def test_the_reaper_keeps_exactly_the_keep_count_newest_first(tmp_path):
    """The retention fixture at the depth the spec asks for: 40 backups of one file beside
    two shallower neighbours. Depth matters here - at 8-deep an off-by-one, a reversed sort
    and a group that spilled into its neighbour all remove plausible-looking counts; at 40
    with two neighbours they are three different answers."""
    deep = _group(tmp_path, "registry.md", 40)
    middle = _group(tmp_path, "2026-09-01-some-epic-design.md", 5)
    shallow = _group(tmp_path, "feature-suggestions.md", 2)

    plan = bk.plan_reap(tmp_path, keep=3)
    assert plan.groups == 3
    assert plan.kept == 3 + 3 + 2

    removed = {r.path for r in plan.removals}
    assert removed == set(deep[:37]) | set(middle[:2])
    assert removed.isdisjoint(shallow), "a group at or under keep is untouched"
    assert {p for p in deep if p not in removed} == set(deep[-3:])
    assert {p for p in middle if p not in removed} == set(middle[-3:])


@pytest.mark.parametrize("keep", [1, 2, 3, 5, 9])
def test_a_group_no_deeper_than_the_keep_count_is_untouched(tmp_path, keep):
    _group(tmp_path, "registry.md", keep)
    plan = bk.plan_reap(tmp_path, keep=keep)
    assert plan.removals == []
    assert plan.kept == keep


def test_a_backup_OF_a_backup_does_not_get_its_own_primary_reaped(tmp_path):
    """THE GROUPING KEY COMES FROM THE CONTRACT PARSER, not the permissive one.

    `primary_for` stops at the first `.bak`, so it files a canonical backup OF a legacy
    backup under the LEGACY BACKUP'S primary. That merges two groups into one, and the merged
    group's oldest member is `state.md.bak-old0` itself - so the sweep deletes the file the
    newer backups exist to protect, spares its backups, and leaves `state.md` short of keep.
    Invariant 2 ("never eat the last copy") defeated by a parser mismatch, silently.
    """
    _write(tmp_path / "state.md")
    base = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    legacy = _write(tmp_path / "state.md.bak-old0", mtime=base.timestamp())
    of_the_backup = [
        _write(
            tmp_path / bk.backup_name("state.md.bak-old0", "steward-f9", base + timedelta(hours=h)),
            mtime=(base + timedelta(hours=h)).timestamp(),
        )
        for h in range(1, 5)
    ]

    plan = bk.plan_reap(tmp_path, keep=3)
    removed = {r.path for r in plan.removals}

    assert plan.groups == 2, "state.md and state.md.bak-old0 are two primaries, not one"
    assert legacy not in removed, "the reaper deleted the file its own backups protect"
    assert legacy.exists()
    # `state.md` has one backup; only the deepest group is over keep, by exactly one.
    assert removed == {of_the_backup[0]}
    assert {r.primary for r in plan.removals} == {tmp_path / "state.md.bak-old0"}


def test_a_primary_that_is_a_DIRECTORY_is_refused_not_treated_as_live(tmp_path):
    """`exists()` says True for a directory, so the missing-primary guard read one as a
    healthy file and made its backups reapable. `is_file()` is the single first check: it is
    False for absent, directory and broken symlink alike, and the reason names which."""
    (tmp_path / "notes").mkdir()
    base = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    made = [
        _write(
            tmp_path / bk.backup_name("notes", "steward-f9", base + timedelta(seconds=i)),
            mtime=(base + timedelta(seconds=i)).timestamp(),
        )
        for i in range(5)
    ]
    plan = bk.plan_reap(tmp_path, keep=1)
    assert plan.removals == []
    assert [r.reason for r in plan.refusals] == ["primary is a directory"]
    assert all(p.exists() for p in made)
    assert "primary is a directory" in bk.render(plan, root=tmp_path)


def test_the_same_primary_NAME_in_two_directories_is_two_groups(tmp_path):
    """The grouping key is (directory, primary), and dropping the directory kills no fixture
    that puts one name in one place. `registry.md` sits at
    `projects/<name>/status/registry.md` for EVERY project, so this is the live tree's actual
    shape - and the two copies are at different nesting depths, because `rglob` finds both."""
    deep_dir = tmp_path / "projects" / "alpha" / "status"
    shallow_dir = tmp_path / "projects" / "beta" / "design" / "specs" / "nested"
    deep = _group(deep_dir, "registry.md", 7)
    shallow = _group(shallow_dir, "registry.md", 2)

    plan = bk.plan_reap(tmp_path, keep=3)
    removed = {r.path for r in plan.removals}
    assert plan.groups == 2
    assert removed == set(deep[:4])
    assert removed.isdisjoint(shallow)
    assert all(p.exists() for p in shallow)
    assert {r.primary for r in plan.removals} == {deep_dir / "registry.md"}


def test_a_refusal_in_one_directory_does_not_license_removals_in_another(tmp_path):
    """Same primary NAME, opposite verdicts, and neither may leak into the other: beta's
    `registry.md` is gone, so its backups are last copies; alpha's is healthy and reapable.
    Merged into one group, the surviving primary would license deleting beta's last copies."""
    alpha = tmp_path / "projects" / "alpha" / "status"
    beta = tmp_path / "projects" / "beta" / "status"
    healthy = _group(alpha, "registry.md", 6)
    base = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    orphaned = [
        _write(
            beta / bk.backup_name("registry.md", "steward-f1", base + timedelta(seconds=i)),
            mtime=(base + timedelta(seconds=i)).timestamp(),
        )
        for i in range(5)
    ]

    plan = bk.plan_reap(tmp_path, keep=3)
    assert {r.path for r in plan.removals} == set(healthy[:3])
    assert [r.reason for r in plan.refusals] == ["primary is absent"]
    assert plan.refusals[0].primary == beta / "registry.md"
    assert all(p.exists() for p in orphaned)


def test_the_reaper_touches_no_other_files_backups(tmp_path):
    """Two files' backups in ONE directory. Grouping is by (directory, primary), so a deep
    group can never spill its retention onto a shallow neighbour."""
    deep = _group(tmp_path, "legacy-loop-state.md", 8)
    shallow = _group(tmp_path, "registry.md", 2)
    plan = bk.plan_reap(tmp_path, keep=3)
    removed = {r.path for r in plan.removals}
    assert removed == set(deep[:5])
    assert removed.isdisjoint(shallow)


def test_legacy_and_canonical_backups_of_one_file_are_ordered_together_by_mtime(tmp_path):
    _write(tmp_path / "state.md")
    base = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    old_legacy = _write(tmp_path / "state.md.bak-flip3", mtime=base.timestamp())
    mid_canon = _write(
        tmp_path / bk.backup_name("state.md", "steward-f9", base + timedelta(hours=1)),
        mtime=(base + timedelta(hours=1)).timestamp(),
    )
    new_legacy = _write(
        tmp_path / "state.md.bak-refinement-20260903", mtime=(base + timedelta(hours=2)).timestamp()
    )
    plan = bk.plan_reap(tmp_path, keep=2)
    assert [r.path for r in plan.removals] == [old_legacy]
    assert {mid_canon, new_legacy}.isdisjoint({r.path for r in plan.removals})


def test_the_removal_records_whether_each_file_was_canonical_or_legacy(tmp_path):
    _write(tmp_path / "state.md")
    base = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    _write(tmp_path / "state.md.bak-flip3", mtime=base.timestamp())
    _write(tmp_path / "state.md.bak-f7", mtime=(base + timedelta(seconds=1)).timestamp())
    _write(
        tmp_path / bk.backup_name("state.md", "steward-f9", base + timedelta(hours=1)),
        mtime=(base + timedelta(hours=1)).timestamp(),
    )
    plan = bk.plan_reap(tmp_path, keep=1)
    assert sorted(r.canonical for r in plan.removals) == [False, False]


def test_keep_of_zero_is_refused(tmp_path):
    with pytest.raises(ValueError):
        bk.plan_reap(tmp_path, keep=0)


def test_an_empty_tree_plans_nothing(tmp_path):
    plan = bk.plan_reap(tmp_path)
    assert plan.is_clean
    assert plan.groups == 0


def test_the_DEFAULT_keep_is_the_one_the_reaper_actually_uses(tmp_path):
    """The default must be exercised somewhere it can be WRONG.

    `test_an_empty_tree_plans_nothing` is the only other caller that omits `keep`, and an empty
    tree answers identically for every value - so it cannot tell KEEP_DEFAULT from 1 or from 50.
    Every other test passes `keep` explicitly, so nothing else exercises the default path.

    WHAT THIS PINS, STATED NARROWLY BECAUSE THE WIDER CLAIM IS FALSE: it pins that the signature
    default and `KEEP_DEFAULT` are the SAME value. Mutation-proved - repointing the signature at a
    literal while leaving the constant alone reds this test and nothing else.

    It does NOT pin the value. Changing `KEEP_DEFAULT` reds nothing here, deliberately: both sides
    of the assertion read that constant, and `assert KEEP_DEFAULT == 3` would branch on the output
    under test, which is the vacuous shape this suite exists to avoid. The value is a policy
    choice with exactly one home; the agreement between its two readers is the testable part.
    """
    _group(tmp_path, "registry.md", bk.KEEP_DEFAULT + 4)
    plan = bk.plan_reap(tmp_path)
    assert plan.kept == bk.KEEP_DEFAULT, (
        f"plan_reap() with no keep= kept {plan.kept}, not KEEP_DEFAULT ({bk.KEEP_DEFAULT}) - "
        "the signature default and the reaper disagree"
    )
    assert plan.removals, "a group larger than the default must yield removals"


# --------------------------------------------------------------------------- #
# INVARIANTS - the two rules that are not tradeable for a lower file count
# --------------------------------------------------------------------------- #
def test_INVARIANT_a_group_whose_primary_is_absent_is_a_NO_OP(tmp_path):
    """These backups are the only surviving copy of the file. Retention that eats the last
    copy is not retention; it is the incident that started all this."""
    base = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    made = []
    for i in range(9):
        when = base + timedelta(seconds=i)
        made.append(
            _write(
                tmp_path / bk.backup_name("orphan.md", "dev-manager-f592", when),
                mtime=when.timestamp(),
            )
        )
    plan = bk.plan_reap(tmp_path, keep=1)
    assert plan.removals == []
    assert [r.reason for r in plan.refusals] == ["primary is absent"]
    assert plan.refusals[0].count == 9
    assert all(p.exists() for p in made)

    # A refused backup is NOT "kept". `kept` is the retention rule doing its job; folding
    # refusals into it produced a summary reading as healthy retention over exactly the
    # groups nothing had decided anything about.
    assert plan.kept == 0
    assert plan.refused_backups == 9

    # "...and SAYS SO rather than exiting silently" is half the invariant, and the half a
    # human acts on. Deleting the refusal block from `render` must red something.
    text = bk.render(plan, root=tmp_path)
    assert "REFUSE" in text and "orphan.md" in text
    assert "primary is absent" in text
    assert "9 backup(s) left untouched" in text


def test_INVARIANT_the_last_copy_is_never_deleted_when_the_primary_is_zero_length(tmp_path):
    """A truncated primary is the failure mode the backup rule exists for. Zero length is
    NOT "the file is fine and happens to be empty" as far as this pass can tell, and the
    pass does not get to guess on an unrecoverable operation."""
    _write(tmp_path / "state.md", text="")
    base = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    made = [
        _write(
            tmp_path / bk.backup_name("state.md", "dev-manager-f592", base + timedelta(seconds=i)),
            text=f"content {i}",
            mtime=(base + timedelta(seconds=i)).timestamp(),
        )
        for i in range(6)
    ]
    plan = bk.plan_reap(tmp_path, keep=1)
    assert plan.removals == []
    assert [r.reason for r in plan.refusals] == ["primary is zero-length"]
    assert all(p.exists() and p.read_text(encoding="utf-8") for p in made)
    assert plan.kept == 0
    assert plan.refused_backups == 6

    text = bk.render(plan, root=tmp_path)
    assert "REFUSE" in text and "state.md" in text
    assert "primary is zero-length" in text
    assert "6 backup(s) left untouched" in text


def test_a_refused_group_does_not_suppress_a_healthy_neighbour(tmp_path):
    """The refusal is scoped to its own group. A directory holding one orphan must not
    become permanently unreapable."""
    _group(tmp_path, "healthy.md", 6)
    base = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(4):
        when = base + timedelta(seconds=i)
        _write(tmp_path / bk.backup_name("orphan.md", "steward-f1", when), mtime=when.timestamp())
    plan = bk.plan_reap(tmp_path, keep=3)
    assert len(plan.removals) == 3
    assert all(r.primary.name == "healthy.md" for r in plan.removals)
    assert len(plan.refusals) == 1


# --------------------------------------------------------------------------- #
# Dry-run
# --------------------------------------------------------------------------- #
def test_dry_run_deletes_nothing(tmp_path):
    made = _group(tmp_path, "registry.md", 9)
    plan = bk.plan_reap(tmp_path, keep=2)
    text = bk.render(plan, apply=False, root=tmp_path)
    assert "[DRY-RUN]" in text
    assert "[APPLY]" not in text
    assert len(plan.removals) == 7
    assert all(p.exists() for p in made), "planning must not touch the filesystem"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [p.name for p in made] + ["registry.md"]
    )


def test_apply_removes_exactly_what_the_plan_named_and_nothing_else(tmp_path):
    made = _group(tmp_path, "registry.md", 9)
    plan = bk.plan_reap(tmp_path, keep=2)
    planned = {r.path for r in plan.removals}
    result = bk.apply_plan(plan)
    assert result.ok
    assert set(result.removed) == planned
    assert {p for p in made if p.exists()} == set(made) - planned
    assert (tmp_path / "registry.md").exists()


def test_a_failure_partway_through_apply_reports_what_did_and_did_not_go(tmp_path, monkeypatch):
    """B5. A `PermissionError` on file 3 of 7 used to escape as a traceback AFTER the report
    had already printed `[APPLY] remove` for all 7 - against a tree git cannot reconstruct.
    The operator was left with a claim and no record of which removals were real."""
    _group(tmp_path, "registry.md", 9)
    plan = bk.plan_reap(tmp_path, keep=2)
    doomed = sorted(plan.removals, key=lambda r: str(r.path))[2].path

    real_unlink = Path.unlink

    def _explode(self, *a, **kw):
        if self == doomed:
            raise PermissionError(13, "in use by another process")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", _explode)
    result = bk.apply_plan(plan)

    assert not result.ok
    assert [p for p, _ in result.failures] == [doomed]
    assert doomed.exists(), "a failed removal must leave the file alone, not half-remove it"
    assert len(result.removed) == 6, "one failure must not abort the other six"
    assert set(result.removed).isdisjoint({doomed})

    text = bk.render(plan, apply=True, root=tmp_path, result=result)
    assert "[FAILED] NOT removed" in text
    assert doomed.name in text
    assert "applied: 6 removed, 1 FAILED to remove" in text


def test_render_DEFAULTS_to_the_dry_run_banner(tmp_path):
    """`apply=False` is the default and nothing called it that way, so flipping the default
    relabelled every report `[APPLY]` with the suite green. That banner is what a human reads
    to decide whether a sweep has already happened - the one line they act on."""
    _group(tmp_path, "registry.md", 6)
    plan = bk.plan_reap(tmp_path, keep=3)
    text = bk.render(plan)  # no apply=, deliberately
    assert "[DRY-RUN] remove" in text
    assert "[APPLY]" not in text


# --------------------------------------------------------------------------- #
# The CLI - the shipped entry point, and the only place `--apply` can be typed
# --------------------------------------------------------------------------- #
def _fake_data_root(monkeypatch, root: Path) -> None:
    """Point `load_environment` at `root`. `main` imports it at call time, so this lands."""
    from agentflow import config as cfg

    monkeypatch.setattr(
        cfg, "load_environment", lambda: SimpleNamespace(data_root=str(root))
    )


def _files_under(root: Path) -> set:
    return {p for p in root.rglob("*") if p.is_file()}


def test_the_CLI_reap_is_REPORT_ONLY_when_apply_is_not_typed(tmp_path, monkeypatch, capsys):
    """Nothing called `main()`, so the destructive half of the shipped command was unproven:
    defaulting `--apply` to True deleted seeded files while all 63 tests stayed green. This
    is the assertion that the command a steward runs unattended every wake removes nothing."""
    _group(tmp_path / "projects" / "alpha" / "status", "registry.md", 8)
    before = _files_under(tmp_path)
    _fake_data_root(monkeypatch, tmp_path)

    rc = bk.main(["reap"])
    out = capsys.readouterr().out

    assert _files_under(tmp_path) == before, "a bare `reap` must not touch the filesystem"
    assert "[DRY-RUN]" in out
    assert "[APPLY]" not in out
    assert rc == 1, "a tree with pending removals is not bounded, and must not exit 0"


def test_the_CLI_reap_apply_removes_exactly_the_planned_files(tmp_path, monkeypatch, capsys):
    """The mirror. `--apply` is the one path that deletes, so it is proved against the plan
    rather than against a count."""
    status = tmp_path / "projects" / "alpha" / "status"
    made = _group(status, "registry.md", 8)
    planned = {r.path for r in bk.plan_reap(tmp_path, keep=bk.KEEP_DEFAULT).removals}
    assert planned, "the fixture must be deeper than the default keep or this proves nothing"
    _fake_data_root(monkeypatch, tmp_path)

    rc = bk.main(["reap", "--apply"])
    out = capsys.readouterr().out

    assert _files_under(tmp_path) == (set(made) | {status / "registry.md"}) - planned
    assert "[APPLY] removed" in out
    assert rc == 0, "a bounded tree with nothing refused exits 0"


def test_the_CLI_exits_NON_ZERO_while_a_refusal_is_standing(tmp_path, monkeypatch, capsys):
    """`is_clean` was dead code in the shipped CLI - `main` returned 0 unconditionally - so
    the steward had no machine-readable way to tell a clean tree from one where groups hold
    the only copy of a file. A refusal is non-zero in BOTH modes: applying the removals does
    not answer it."""
    base = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    orphan_dir = tmp_path / "projects" / "alpha" / "status"
    for i in range(4):
        _write(
            orphan_dir / bk.backup_name("orphan.md", "steward-f1", base + timedelta(seconds=i)),
            mtime=(base + timedelta(seconds=i)).timestamp(),
        )
    _fake_data_root(monkeypatch, tmp_path)

    assert bk.main(["reap"]) == 1
    assert "REFUSE" in capsys.readouterr().out
    assert bk.main(["reap", "--apply"]) == 1, "an --apply with nothing to remove is not clean"


def test_the_CLI_exits_ZERO_on_a_genuinely_bounded_tree(tmp_path, monkeypatch, capsys):
    """The control for the two tests above: 0 has to be reachable, or `1` means nothing."""
    _group(tmp_path / "projects" / "alpha" / "status", "registry.md", bk.KEEP_DEFAULT)
    _fake_data_root(monkeypatch, tmp_path)
    assert bk.main(["reap"]) == 0
    capsys.readouterr()


def test_a_typod_project_is_LOUD_rather_than_a_clean_looking_zero(tmp_path, monkeypatch, capsys):
    """`reap --project alhpa` used to print "0 to remove ... 0 refused" and exit 0 -
    indistinguishable from a bounded tree, in a command the steward runs unattended and
    routes to a human as evidence. That is the swallowed-failure class this repo forbids."""
    _group(tmp_path / "projects" / "alpha" / "status", "registry.md", 8)
    _fake_data_root(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exc:
        bk.main(["reap", "--project", "alhpa"])
    assert exc.value.code == 2, "a bad invocation must not share an exit code with a finding"
    err = capsys.readouterr().err
    assert "no such directory" in err
    assert "alhpa" in err, "the message must name what was actually resolved"


@pytest.mark.parametrize("project", ["..", ".", "a/b", "*"])
def test_a_project_that_is_not_a_single_segment_is_refused(tmp_path, monkeypatch, capsys, project):
    """`--project ..` resolves to `data_root` itself, which EXISTS - so the directory check
    would wave it through and a flag meant to NARROW the sweep would silently widen it."""
    _group(tmp_path / "projects" / "alpha" / "status", "registry.md", 4)
    _fake_data_root(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exc:
        bk.main(["reap", "--project", project])
    assert exc.value.code == 2
    capsys.readouterr()


@pytest.mark.parametrize("keep", ["0", "-1", "abc"])
def test_a_keep_below_one_is_a_bad_invocation_not_an_unbounded_tree(
    tmp_path, monkeypatch, capsys, keep
):
    """`--keep 0` escaped `plan_reap`'s ValueError as a traceback on exit 1, the code the
    steward routes as "not bounded". A bad flag must exit 2 with a message, before any plan."""
    _group(tmp_path / "projects" / "alpha" / "status", "registry.md", 4)
    _fake_data_root(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exc:
        bk.main(["reap", "--keep", keep])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--keep" in err
    assert "Traceback" not in err


def test_the_CLI_new_subcommand_claims_the_path_it_prints(tmp_path, capsys):
    """`new` is the writer-side entry point named by both SKILL.md files. It prints a path
    AND creates it, because a printed-but-unclaimed path is the race `next_backup_path`
    exists to close."""
    src = _write(tmp_path / "registry.md")
    assert bk.main(["new", str(src), "--firing", "dev-manager-f592"]) == 0
    printed = Path(capsys.readouterr().out.strip())
    assert printed.exists()
    assert bk.parse_backup_name(printed.name).primary == "registry.md"


# --------------------------------------------------------------------------- #
# gh#712 - a claim is not a backup
# --------------------------------------------------------------------------- #
def test_empty_claims_newer_than_the_real_backup_do_not_evict_it(tmp_path):
    """The issue's repro: one real backup, then a run of empty claims taken after it. Ranked
    by mtime alone the empties filled the keep window and the reap deleted the only copy with
    content in it. The control half: with no empties the same group deletes nothing."""
    _write(tmp_path / "spec.md", "live")
    real = tmp_path / bk.backup_name("spec.md", "dev-manager-f1", BASE)
    _write(real, "the real copy", mtime=BASE.timestamp())
    control = bk.plan_reap(tmp_path, keep=3)
    assert control.removals == [] and control.kept == 1

    empties = []
    for i in range(2, 13):
        when = BASE + timedelta(seconds=i)
        empties.append(_write(tmp_path / bk.backup_name("spec.md", f"dev-manager-f{i}", when),
                              "", mtime=when.timestamp()))

    plan = bk.plan_reap(tmp_path, keep=3)
    removed = {r.path for r in plan.removals}
    assert real not in removed, "an empty claim evicted the only real backup"
    assert removed == set(empties), "every empty claim goes once a real backup exists"
    assert plan.kept == 1, "an empty claim is not counted as a kept backup"
    assert all(r.size == 0 for r in plan.removals)


def test_the_keep_window_is_filled_by_real_backups_and_empties_never_displace_them(tmp_path):
    """Interleaved: five real backups, empties between and after them. The newest THREE REAL
    ones survive whatever order the empties landed in."""
    _write(tmp_path / "spec.md", "live")
    real, empty = [], []
    for i in range(10):
        when = BASE + timedelta(seconds=i)
        path = tmp_path / bk.backup_name("spec.md", "dev-manager-f7", when)
        is_real = i % 2 == 0
        _write(path, f"copy {i}" if is_real else "", mtime=when.timestamp())
        (real if is_real else empty).append(path)

    plan = bk.plan_reap(tmp_path, keep=3)
    removed = {r.path for r in plan.removals}
    assert set(real[-3:]).isdisjoint(removed)
    assert removed == set(real[:2]) | set(empty)
    assert plan.kept == 3


def test_a_group_of_only_empty_claims_still_ranks_by_age(tmp_path):
    """Nothing in it to protect, so the ordinary rule applies and the group stays bounded."""
    _write(tmp_path / "spec.md", "live")
    made = []
    for i in range(5):
        when = BASE + timedelta(seconds=i)
        made.append(_write(tmp_path / bk.backup_name("spec.md", "dev-manager-f7", when),
                           "", mtime=when.timestamp()))
    plan = bk.plan_reap(tmp_path, keep=3)
    assert {r.path for r in plan.removals} == set(made[:2])
    assert plan.kept == 3


@pytest.mark.parametrize("firing", ["dev-manager", "Dev-Manager-f1", "dev-manager-fX"])
def test_the_CLI_new_refuses_a_malformed_firing_READABLY_not_with_a_traceback(
        tmp_path, capsys, firing):
    """The issue's repro: the seat has no firing number, so `--firing dev-manager` was the
    natural thing to type, and it surfaced as a ValueError traceback."""
    src = _write(tmp_path / "spec.md", "live")
    assert bk.main(["new", str(src), "--firing", firing]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("backups new: firing must be <actor>-f<N>")
    assert "Traceback" not in captured.err
    assert list(tmp_path.iterdir()) == [src], "a refused firing claims nothing"


def test_the_CLI_new_refuses_an_unclaimable_path_readably(tmp_path, capsys):
    assert bk.main(["new", str(tmp_path / "no-such-dir" / "spec.md"),
                    "--firing", "dev-manager-f1"]) == 2
    assert capsys.readouterr().err.startswith("backups new: ")


def test_the_CLI_new_copy_takes_a_real_verified_backup(tmp_path, capsys):
    src = _write(tmp_path / "spec.md", "the spec body\n")
    assert bk.main(["new", str(src), "--firing", "dev-manager-f1759140000", "--copy"]) == 0
    printed = Path(capsys.readouterr().out.strip())
    assert bk.parse_backup_name(printed.name).primary == "spec.md"
    assert printed.read_bytes() == src.read_bytes()


def test_take_backup_gives_the_copy_its_OWN_mtime_so_it_sorts_as_newest(tmp_path):
    """`copy2` would stamp the backup with the primary's older mtime, and the reaper - which
    orders by the backup's mtime - would then rank it behind backups taken before it."""
    old = BASE.timestamp()
    src = _write(tmp_path / "spec.md", "body", mtime=old)
    taken = bk.take_backup(src, "dev-manager-f1")
    assert taken.stat().st_mtime > old + 60


@pytest.mark.parametrize("make_source", [
    pytest.param(lambda d: d / "absent.md", id="missing"),
    pytest.param(lambda d: _write(d / "empty.md", ""), id="empty"),
    pytest.param(lambda d: (d / "adir.md").mkdir() or d / "adir.md", id="directory"),
])
def test_the_CLI_new_copy_refuses_a_source_with_nothing_to_back_up(tmp_path, capsys, make_source):
    """Refused BEFORE claiming: a claim made first would be the very empty file this mode
    exists to stop producing."""
    src = make_source(tmp_path)
    before = set(tmp_path.iterdir())
    assert bk.main(["new", str(src), "--firing", "dev-manager-f1", "--copy"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"backups new: cannot back up {src}")
    assert set(tmp_path.iterdir()) == before, "a refusal left a claim behind"


def test_take_backup_that_does_not_verify_removes_its_claim_and_raises(tmp_path, monkeypatch):
    """A short copy (a full disk, a file edited mid-copy) must not leave a plausible backup."""
    src = _write(tmp_path / "spec.md", "the whole spec")

    def short_copy(source, dest):
        Path(dest).write_text("the wh", encoding="utf-8")
        return dest

    monkeypatch.setattr(bk.shutil, "copyfile", short_copy)
    with pytest.raises(bk.BackupError, match="did not verify"):
        bk.take_backup(src, "dev-manager-f1")
    assert list(tmp_path.iterdir()) == [src]


def test_take_backup_whose_copy_fails_removes_its_claim_and_raises(tmp_path, monkeypatch):
    src = _write(tmp_path / "spec.md", "the whole spec")

    def failing_copy(source, dest):
        raise PermissionError("denied")

    monkeypatch.setattr(bk.shutil, "copyfile", failing_copy)
    with pytest.raises(bk.BackupError, match="failed: denied"):
        bk.take_backup(src, "dev-manager-f1")
    assert list(tmp_path.iterdir()) == [src]


def test_a_short_copy_whose_claim_cannot_be_UNLINKED_is_left_EMPTY_not_corrupt(tmp_path, monkeypatch):
    """Windows AV holding the file is enough to fail the unlink. A short copy left under a
    canonical name is a non-empty member the reaper would rank as a real backup, so the claim
    is emptied before the unlink is tried and the leftover is at worst zero bytes."""
    src = _write(tmp_path / "spec.md", "the whole spec")

    def short_copy(source, dest):
        Path(dest).write_text("the wh", encoding="utf-8")
        return dest

    real_unlink = Path.unlink

    def held_open(self, missing_ok=False):
        if bk.BACKUP_MARKER in self.name:
            raise PermissionError("held by another process")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(bk.shutil, "copyfile", short_copy)
    monkeypatch.setattr(Path, "unlink", held_open)
    with pytest.raises(bk.BackupError, match="left EMPTY") as exc:
        bk.take_backup(src, "dev-manager-f1")
    assert "did not verify" in str(exc.value)
    leftovers = [p for p in tmp_path.iterdir() if p != src]
    assert len(leftovers) == 1 and leftovers[0].stat().st_size == 0
    monkeypatch.undo()
    assert bk.plan_reap(tmp_path, keep=3).kept == 1, "the leftover still takes no real slot"


@pytest.mark.parametrize("firing,cause", [
    ("dev-manager", ValueError),
    ("dev-manager-f1", OSError),
])
def test_take_backup_raises_ONLY_BackupError_with_the_cause_chained(
        tmp_path, monkeypatch, firing, cause):
    """Python callers catch one type. A malformed firing is ValueError from `backup_name`, and
    an unclaimable directory is OSError from the claim; both surface as BackupError."""
    src = _write(tmp_path / "spec.md", "body")
    if cause is OSError:
        def no_claim(path, *args, **kwargs):
            raise PermissionError("read-only directory")

        monkeypatch.setattr(bk.os, "open", no_claim)
    with pytest.raises(bk.BackupError, match="cannot claim a backup name") as exc:
        bk.take_backup(src, firing)
    assert isinstance(exc.value.__cause__, cause)
    monkeypatch.undo()
    assert list(tmp_path.iterdir()) == [src], "a failed claim leaves nothing behind"


def test_the_dev_manager_rule_takes_the_backup_with_copy_and_defines_its_firing_number():
    """The prose half of gh#712: the rule named only the claim, so a seat following it
    literally held an empty file. The definition block must name the copying mode and say
    what N is for a seat that has no firing counter."""
    definition = next(b for b in _bak_blocks(DEV_MANAGER) if MODULE_REF in b)
    flat = " ".join(definition.split())
    assert "--copy" in flat
    assert "take_backup" in flat
    assert "N for this seat" in flat and "epoch seconds" in flat


def test_the_report_names_the_dominant_directories(tmp_path):
    _group(tmp_path / "specs", "a.md", 9)
    _group(tmp_path / "status", "b.md", 4)
    plan = bk.plan_reap(tmp_path, keep=3)
    text = bk.render(plan, apply=False, root=tmp_path)
    assert "specs" in text
    assert "Directories dominating the removals:" in text


# --------------------------------------------------------------------------- #
# Target 1 - the format is defined ONCE, in code, and pointed at from prose
# --------------------------------------------------------------------------- #
DEV_MANAGER = SKILLS_ROOT / "dev-manager" / "SKILL.md"
DATA_LAYOUT = REPO_ROOT / "docs" / "DATA-LAYOUT.md"

#: The pointer every non-defining mention must carry, so a reader who lands on an imperative
#: has one hop to the rule and the reason.
POINTER = "see Conventions"

#: The module a prose file is allowed to name instead of restating a filename pattern.
MODULE_REF = "agentflow.backups"

#: A prose restatement of the filename format, in BOTH forms it actually takes.
#:
#: The literal form - a real or placeholder stamp - was the obvious one, and it is the form
#: nobody writes. The SCHEMATIC form is the one that happens: a procedure step does not carry a
#: timestamp, it carries the shape, `<file>.bak.<UTC stamp>.<actor>-f<N>.<seq>`. That is a second
#: definition exactly as much as the literal is, and a guard that catches only the literal reads
#: as coverage while leaving the live case green.
#:
#: Every alternative is ANCHORED ON THE MARKER, because `<actor>-f<N>` on its own is the
#: LEGITIMATE pointer form - `--firing <actor>-f<N>` is how the definition block and the CLI help
#: say what to pass. Naming an argument is not restating a filename, and a guard that cannot tell
#: those apart would force the pointer prose out of the files it is there to serve.
#:
#: WHAT IT DETECTS, STATED EXACTLY, because the docstring above it used to advertise "schematic"
#: coverage while matching only marker-adjacent tokens - so the plain-English form (naming the
#: fields, in order, with no filename-shaped token anywhere in the sentence) sailed through, and
#: `DATA-LAYOUT.md` shipped one three lines above the claim that it contained none. Three forms:
#: a stamp (literal or placeholder); a placeholder field hung off the marker; and the marker
#: named as an ORDERED element of a field list ("..., then the `.bak` marker, then a timestamp").
#: It is a prose heuristic over three known files, not a parser. A paraphrase that names no
#: stamp, no placeholder and no ordering word will still get past it; the block-level gate below
#: and review are what cover that.
FORMAT_RESTATEMENT = re.compile(
    r"\d{8}T\d{6}Z"           # a literal stamp
    r"|YYYYMMDDTHHMMSSZ"      # a placeholder stamp
    r"|\.bak[.\-]<"           # the marker followed by a placeholder field: `.bak.<UTC stamp>`
    r"|<[^<>]*>\.bak"         # a placeholder primary carrying the marker: `<file>.bak`
    r"|\.bak[^\s`]*<seq>"     # a `<seq>` field hung off the marker
    # the marker as an ordered field: "keeps the primary intact, THEN the `.bak` marker"
    r"|(?:then|followed by|next)\s+(?:the\s+|a\s+|an\s+)?`?\.bak"
    # the mirror image: "the `.bak` marker, then a fixed-width UTC timestamp"
    r"|`?\.bak`?\s+(?:marker|suffix)\s*,?\s+(?:then|followed by|and\s+then)\b"
)


def _bak_blocks(path: Path) -> list[str]:
    """Markdown blocks mentioning `.bak`: blank-line separated, AND split at every top-level
    `- ` bullet.

    Blocks rather than lines on purpose: this file is hard-wrapped, so a line-granular gate
    would fail the next editor who reflowed a paragraph without changing a word of it. The
    block is also the real unit of reading - a pointer two lines down is still adjacent to
    the imperative.

    THE BULLET SPLIT IS NOT COSMETIC. `## Conventions` is a single blank-line-separated block
    over three thousand characters long, so "the block containing the definition" was the
    whole list: any future `.bak` imperative added to any sibling bullet would have been
    absorbed into the definition and escaped the pointer requirement, with the gate green.
    One bullet is one block.
    """
    text = path.read_text(encoding="utf-8")
    return [b for b in re.split(r"\n[ \t]*\n|\n(?=- )", text) if ".bak" in b]


def test_dev_manager_defines_the_backup_rule_exactly_once():
    """Target 1, made unregressable. Bare `.bak` imperatives scattered through a procedure,
    with the rule and the reason attached to only one of them, is precisely how one firing
    invented `.bak-flip3` while another invented `.bak-f7`."""
    blocks = _bak_blocks(DEV_MANAGER)
    assert blocks, "the backup rule must still be in this file - it is not up for removal"
    definitions = [b for b in blocks if MODULE_REF in b]
    assert len(definitions) == 1, (
        f"expected exactly one block naming {MODULE_REF}; found {len(definitions)}"
    )
    for block in blocks:
        if block in definitions:
            continue
        assert POINTER in block, (
            f"a non-defining `.bak` mention must point at the definition "
            f"({POINTER!r}):\n{block}"
        )


def test_a_bullet_LIST_is_one_block_per_bullet_not_one_block(tmp_path):
    """E1, proved on a fixture rather than asserted about the real file.

    `## Conventions` is a single blank-line-separated block of over three thousand
    characters. With a blank-line-only splitter the definition bullet was that whole block,
    so every sibling bullet counted as part of the definition - and a future `.bak`
    imperative added anywhere in the list would have been exempt from the pointer
    requirement, silently, with the gate reporting one definition and no offenders.

    Reverting the splitter collapses this to a single block and reds the second assertion.
    """
    sample = tmp_path / "SKILL.md"
    sample.write_text(
        "## Conventions\n\n"
        f"- **Backups.** Take a `.bak`; do not invent the name, it lives in `{MODULE_REF}`.\n"
        "  A continuation line, indented, still part of the same bullet.\n"
        "- **Something else.** `.bak` the file first.\n"
        "- **Unrelated.** No marker in this one at all.\n",
        encoding="utf-8",
    )
    blocks = _bak_blocks(sample)
    assert len(blocks) == 2, f"the bullets did not separate: {blocks}"
    assert sum(MODULE_REF in b for b in blocks) == 1
    stray = [b for b in blocks if MODULE_REF not in b]
    assert len(stray) == 1 and POINTER not in stray[0], (
        "the sibling bullet must be visible to the pointer gate as its own block"
    )


def test_dev_manager_defines_the_rule_inside_the_Conventions_section():
    """Where it is stated matters. The other mentions are steps inside a long procedure; a
    reader working the file in order must not meet an imperative before the rule that
    explains it."""
    text = DEV_MANAGER.read_text(encoding="utf-8")
    conventions = text.index("\n## Conventions")
    assert text.index(MODULE_REF) > conventions


def test_no_prose_file_restates_the_filename_format():
    """The format lives in code. A prose copy of it is a second definition that nothing
    keeps honest - which is the failure this epic exists to end, one level up.

    LITERAL, SCHEMATIC AND ENUMERATED ALIKE. `DATA-LAYOUT.md` claims the format is
    "deliberately not written out here"; a guard that caught only a literal stamp enforced a
    third of that claim, and the version that added the schematic shape still let the form
    that actually shipped through - an ordered English enumeration of the fields, three lines
    above the claim itself.

    SCANNED PER BLOCK WITH WHITESPACE NORMALISED, not per line: these files are hard-wrapped,
    so an enumeration is routinely split across a line break, and a line-granular gate would
    be defeated by reflowing a paragraph nobody edited.
    """
    for path in (DEV_MANAGER, DATA_LAYOUT, SKILLS_ROOT / "steward" / "SKILL.md"):
        blocks = re.split(r"\n[ \t]*\n", path.read_text(encoding="utf-8"))
        offenders = [
            flat
            for flat in (" ".join(b.split()) for b in blocks)
            if FORMAT_RESTATEMENT.search(flat)
        ]
        assert not offenders, f"{path.name} restates the format: {offenders}"


@pytest.mark.parametrize(
    "restatement",
    [
        # The reviewer's exact string: a procedure step written the way one really would be.
        "**Backup naming quick reference:** write `<file>.bak.<UTC stamp>.<actor>-f<N>.<seq>` "
        "(see Conventions).",
        "name it `dev-manager-state.md.bak.20260903T104801Z.dev-manager-f592.01`",
        "the tail is `.bak-<stamp>-<firing>`",
        "use `<path>.bak.<stamp>`",
        # The form that ACTUALLY SHIPPED, verbatim: an ordered field enumeration in plain
        # English, carrying no filename-shaped token for the earlier pattern to anchor on -
        # and printed three lines above the sentence claiming the format is not written out.
        "The name keeps the primary filename intact (extension included), then the `.bak` "
        "marker, then a fixed-width UTC timestamp, the originating firing (`<actor>-f<N>`) "
        "and a sequence.",
        "first the primary, followed by the `.bak` marker, then the stamp",
        "the `.bak` marker, then a fixed-width UTC timestamp",
    ],
)
def test_the_restatement_guard_catches_the_schematic_form_not_only_the_literal(restatement):
    """The guard's own contract. A schematic restatement is the form that will actually be
    written, so it has to be the form the guard is proved against - not an inference from the
    literal case passing."""
    assert FORMAT_RESTATEMENT.search(restatement), (
        f"a restatement of the format went undetected: {restatement!r}"
    )


@pytest.mark.parametrize(
    "legitimate",
    [
        "`.bak` before programmatic edits (see Conventions).",
        "`python -m agentflow.backups new <path> --firing <actor>-f<N>`",
        "call `next_backup_path(path, firing)` rather than composing a name",
        "the originating firing (`<actor>-f<N>`) and a sequence",
        # The ordering alternatives must not fire on prose that merely SEQUENCES actions.
        "take the backup first, then edit the file",
        "`.bak` the spec, then write `priority` into frontmatter (see Conventions)",
        "the format lives in `agentflow.backups` and is not restated here",
    ],
)
def test_the_restatement_guard_leaves_the_pointer_forms_alone(legitimate):
    """The other half, and the reason the pattern anchors on the marker: `<actor>-f<N>` names
    an ARGUMENT here. A guard that could not tell that from a filename would evict the pointer
    prose it exists to protect."""
    assert not FORMAT_RESTATEMENT.search(legitimate), (
        f"the guard would evict legitimate pointer prose: {legitimate!r}"
    )


def test_the_backup_rule_itself_is_still_stated_and_still_carries_its_reason():
    """INVARIANT. The take-a-backup-before-programmatic-edit rule is not tradeable. It was
    adopted mid-incident by an agent that had just lost an unbacked-up edit, and the reason
    - `data_root` is not git-managed - is the load-bearing half."""
    text = DEV_MANAGER.read_text(encoding="utf-8")
    assert "NOT git-managed" in text
    assert "before programmatic edits" in text


#: Digits and the English words a small count gets spelled as, so `THREE` in a comment is
#: caught as readily as `3`. Past this range the digit form is the only one anyone writes.
_COUNT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
                "nine", "ten")


def _restatements_of(value: int, text: str) -> list[str]:
    """Every spelling of `value` present in `text` - digits and, in range, the word."""
    forms = [str(value)]
    if value < len(_COUNT_WORDS):
        forms.append(_COUNT_WORDS[value])
    return [f for f in forms if re.search(rf"\b{f}\b", text, re.IGNORECASE)]


def _keep_default_comment() -> str:
    """The `#:` comment block immediately above `KEEP_DEFAULT` in the module source."""
    lines = Path(bk.__file__).read_text(encoding="utf-8").splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.startswith("KEEP_DEFAULT"))
    block: list[str] = []
    while idx and lines[idx - 1].lstrip().startswith("#"):
        idx -= 1
        block.append(lines[idx])
    return "\n".join(block)


def test_the_keep_count_is_named_by_its_constant_and_restated_nowhere():
    """Target 4. The doc carries the scheme, the runner and the retention depth - but the depth
    is named by CONSTANT and never copied as a literal. A number in prose and a number in code
    are two numbers, and the prose copy is the one that goes stale in silence.

    TWO-SIDED, because the first version of this guard watched only the doc: the comment sitting
    one line above the constant spelled the value out in words, so `KEEP_DEFAULT` could be
    retuned and leave a confidently wrong sentence behind it with nothing going red.
    """
    text = DATA_LAYOUT.read_text(encoding="utf-8")
    assert "## Backups" in text
    assert MODULE_REF in text
    section = text.split("## Backups", 1)[1].split("\n## ", 1)[0]
    assert "steward" in section
    assert "keeps the newest `KEEP_DEFAULT` backups" in section, (
        "the doc must name the constant; a literal here is a second number that can drift"
    )
    assert not re.search(r"newest\s+\S*\d", section), "a literal keep-count is back in the doc"
    assert not _restatements_of(bk.KEEP_DEFAULT, _keep_default_comment()), (
        "the comment beside KEEP_DEFAULT restates its value - let the constant carry it"
    )


def _git_ignored(repo: Path, samples: list[str]) -> set:
    """The subset of `samples` REAL GIT ignores in `repo`, via `git check-ignore --stdin`.

    NUL-separated and in BINARY mode, not text: `text=True` translates `\\n` to `\\r\\n` on
    Windows, git then reads the CR as part of the pathname, and the answers come back for
    filenames nobody has. `-z` also removes git's quoting of odd names from the output path.
    """
    payload = b"\0".join(s.encode("utf-8") for s in samples) + b"\0"
    proc = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "--stdin", "-z", "--no-index"],
        input=payload,
        capture_output=True,
    )
    # 0 = some ignored, 1 = none ignored. Anything else is a broken invocation, not a finding.
    if proc.returncode not in (0, 1):
        raise AssertionError(
            f"git check-ignore failed (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return {s for s in proc.stdout.decode("utf-8").split("\0") if s}


def test_gitignore_covers_every_backup_shape_ever_written(tmp_path):
    """Target 6. `*.bak` alone is insufficient: the canonical format and every legacy family
    put text AFTER the marker.

    DRIVEN THROUGH REAL GIT, not `fnmatch`. `fnmatch` is not gitignore: it has no `!`
    negation, no anchoring, no later-pattern-wins precedence, and on Windows it case-folds
    where git does not. Proved: appending `!*.bak-` and the dash-suffix wildcard un-ignores
    every legacy dash family in actual git, and the `fnmatch` version stayed green - a gate
    reporting coverage over rules it cannot read.

    The fixture repo holds ONLY this repository's `.gitignore`, with the user's global
    excludes switched off, so what passes or fails is this file and nothing else.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH; this gate needs real gitignore semantics")

    repo = tmp_path / "fixture"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.excludesFile", ""],
        check=True,
        capture_output=True,
    )
    (repo / ".gitignore").write_text(
        (REPO_ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8"
    )

    samples = [f"state.md{s}" for s in LEGACY_SUFFIXES + LEGACY_INFIX]
    samples.append(bk.backup_name("state.md", "dev-manager-f592"))
    missed = set(samples) - _git_ignored(repo, samples)
    assert not missed, f".gitignore does not cover {sorted(missed)}"

    # The other half: the rules must NOT swallow an ordinary file that merely contains the
    # sequence. A `*.bak*` that covered everything would pass the assertion above.
    innocent = ["recipe.baked.md", "registry.md", "backup.txt", "plan.md"]
    assert not _git_ignored(repo, innocent), "the backup rules ignore ordinary source files"


# --------------------------------------------------------------------------- #
# The MOVED caption (gh#288 residue) - a refusal that can finally say WHY it is
# a refusal, without any of the refusal's consequences moving.
#
# The whole tier is one sentence: `live_elsewhere` changes what the report SAYS
# and never what the reap DOES. Every test below either proves a caption appears
# where a live file genuinely carries the slug, or proves it does NOT appear on
# the ways that question can be answered wrongly - and the exit-code test proves
# a fully captioned tree is still an unbounded one.
# --------------------------------------------------------------------------- #
def _spec(path: Path, slug: str, body: str = "") -> Path:
    """A live markdown document whose FRONTMATTER carries `slug`."""
    return _write(path, text="---\nslug: " + slug + "\nstatus: done\n---\n\n" + body)


def _orphans(
    dir_: Path,
    primary: str,
    count: int,
    text: str,
    firing: str = "dev-manager-f592",
) -> list[Path]:
    """`count` canonical backups of a primary that is NOT on disk, each holding `text`.

    The rename-orphan shape exactly: `reconcile` moved or a human renamed the primary, the
    backups stayed behind at the old name, and nothing at that path exists any more.
    """
    base = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    made = []
    for i in range(count):
        when = base + timedelta(seconds=i)
        made.append(
            _write(
                dir_ / bk.backup_name(primary, firing, when),
                text=text,
                mtime=when.timestamp(),
            )
        )
    return made


#: The three shapes a backup's identity can be UNREADABLE in, each carrying a `slug:` line
#: placed exactly where a looser reader would find it. Keyed by the failure it represents.
UNREADABLE = {
    "no-frontmatter-at-all": "# a heading\n\nslug: rename-target\n",
    "frontmatter-never-closes": "---\ntitle: a spec\nslug: rename-target\n",
    "frontmatter-carries-no-slug": (
        "---\ntitle: a spec\nstatus: done\n---\n\nslug: rename-target\n"
    ),
}

#: The measured false-positive class, in one document: a TypeScript field, the `**Slug:**`
#: prose form, and a bare body line. The frontmatter here deliberately has NO slug key, so any
#: caption this document earns came from its BODY and is therefore wrong.
BODY_SLUG_ONLY = (
    "---\ntitle: connector types\nstatus: done\n---\n\n"
    "```ts\n"
    "interface SpecRow {\n"
    "slug: string;\n"
    "}\n"
    "```\n\n"
    "**Slug:** rename-target\n\n"
    "slug: rename-target\n"
)


def test_a_rename_orphaned_group_is_CAPTIONED_with_the_live_file_carrying_its_slug(tmp_path):
    """The gh#288 residue, and the one thing a refusal could never say before: these backups
    are STALE copies of a document that still exists under a new name, not the LAST copies of
    a document that does not. Without the caption a human triaging the refusal list has to
    re-derive that by hand, once per group, forever - which is how 76% of that list stayed
    unexamined."""
    specs = tmp_path / "projects" / "alpha" / "design" / "specs"
    live = _spec(specs / "2026-09-04-renamed-thing.md", "renamed-thing")
    orphans = _orphans(
        specs,
        "2026-08-30-old-thing.md",
        2,
        text="---\nslug: renamed-thing\nstatus: speccing\n---\n\nold copy\n",
    )

    plan = bk.plan_reap(tmp_path, keep=3, resolve_live=bk._make_live_resolver(tmp_path))

    assert len(plan.refusals) == 1
    refusal = plan.refusals[0]
    assert refusal.primary == specs / "2026-08-30-old-thing.md"
    assert refusal.reason == "primary is absent"
    assert refusal.live_elsewhere == live
    assert refusal.count == 2

    text = bk.render(plan, root=tmp_path)
    assert "REFUSE (moved)" in text
    assert "2026-09-04-renamed-thing.md" in text
    assert "2 backup(s) left untouched" in text
    assert "of those refusals, 1 are MOVED" in text
    assert "0 remain unexplained." in text

    # A caption is not a verdict. Nothing leaves `refusals` and nothing is deleted.
    assert plan.removals == []
    assert plan.kept == 0
    assert all(p.exists() for p in orphans)


def test_a_GENUINE_orphan_is_still_refused_UNCAPTIONED_and_untouched(tmp_path):
    """The control for the caption, and the case the whole module exists for: no live file
    carries this slug, so these backups may well be the only surviving copy. An absent caption
    must read as "unexplained", never as "examined and cleared" - which is why the report
    prints the unexplained count and not only the MOVED one."""
    specs = tmp_path / "projects" / "alpha" / "design" / "specs"
    _spec(specs / "2026-09-04-unrelated.md", "unrelated-slug")
    orphans = _orphans(
        specs,
        "2026-08-30-lost-thing.md",
        5,
        text="---\nslug: lost-thing\nstatus: speccing\n---\n\nlast copy\n",
    )

    plan = bk.plan_reap(tmp_path, keep=1, resolve_live=bk._make_live_resolver(tmp_path))

    assert [r.reason for r in plan.refusals] == ["primary is absent"]
    assert plan.refusals[0].live_elsewhere is None
    assert plan.removals == []
    assert plan.kept == 0, "a refused backup is never folded into the retention count"
    assert plan.refused_backups == 5
    assert all(p.exists() and p.read_text(encoding="utf-8") for p in orphans)

    text = bk.render(plan, root=tmp_path)
    assert "REFUSE " in text
    assert "REFUSE (moved)" not in text
    assert "are MOVED" not in text


def test_a_backup_whose_identity_is_UNREADABLE_is_never_captioned(tmp_path):
    """Three ways a document declines to say what it is, each carrying a `slug:` line a looser
    reader would match. Unreadable, ambiguous and absent all mean ONE thing here - no caption -
    because a caption asserts a lineage a human then deletes on."""
    specs = tmp_path / "projects" / "alpha" / "design" / "specs"
    _spec(specs / "2026-09-04-rename-target.md", "rename-target")
    made: dict = {}
    for name, body in UNREADABLE.items():
        made[name] = _orphans(specs, f"2026-08-30-{name}.md", 2, text=body)

    plan = bk.plan_reap(tmp_path, keep=3, resolve_live=bk._make_live_resolver(tmp_path))

    assert {r.primary.name for r in plan.refusals} == {
        f"2026-08-30-{name}.md" for name in UNREADABLE
    }
    assert {r.live_elsewhere for r in plan.refusals} == {None}
    for name, paths in made.items():
        for path in paths:
            assert bk._explicit_slug(path) is None, f"{name} must not yield a slug"

    text = bk.render(plan, root=tmp_path)
    assert "REFUSE (moved)" not in text
    assert "3 group(s) refused" in text


def test_a_slug_line_in_the_BODY_never_earns_a_caption(tmp_path):
    """THE MEASURED FALSE-POSITIVE CLASS. Eight of the 115 backups on the live refusal list
    carry a `slug:`-shaped line in the body: a TypeScript field (`slug: string;`), a
    documentation example, and `**Slug:**` prose. A whole-file grep matches every one of them,
    and the prose form is positively misleading - it is how a renamed document keeps insisting
    on its old identity. Only the leading frontmatter block counts, and only an explicit key
    inside it."""
    specs = tmp_path / "projects" / "alpha" / "design" / "specs"
    _spec(specs / "2026-09-04-rename-target.md", "rename-target")
    orphans = _orphans(specs, "2026-08-30-connector-types.md", 3, text=BODY_SLUG_ONLY)

    plan = bk.plan_reap(tmp_path, keep=3, resolve_live=bk._make_live_resolver(tmp_path))

    assert [r.primary.name for r in plan.refusals] == ["2026-08-30-connector-types.md"]
    assert plan.refusals[0].live_elsewhere is None
    assert all(bk._explicit_slug(p) is None for p in orphans)
    assert "REFUSE (moved)" not in bk.render(plan, root=tmp_path)


def test_a_backup_captions_on_its_FRONTMATTER_slug_and_never_on_a_stale_body_slug(tmp_path):
    """The other half of the body-slug class, and the half that picks a WRONG live file rather
    than merely failing to pick one. This document was renamed: its frontmatter says
    `current-name`, its body still advertises `previous-name`, and a DIFFERENT live document
    holds `previous-name` today. Reading the body captions this group onto somebody else's
    file - a confident, plausible, wrong lineage on a report a human deletes from."""
    specs = tmp_path / "projects" / "alpha" / "design" / "specs"
    current = _spec(specs / "2026-09-04-current-name.md", "current-name")
    stale = _spec(specs / "2026-08-01-someone-elses.md", "previous-name")
    _orphans(
        specs,
        "2026-08-30-old-path.md",
        2,
        text=(
            "---\nslug: current-name\nstatus: speccing\n---\n\n"
            "**Slug:** previous-name\n\n"
            "slug: previous-name\n"
        ),
    )

    plan = bk.plan_reap(tmp_path, keep=3, resolve_live=bk._make_live_resolver(tmp_path))

    assert [r.primary.name for r in plan.refusals] == ["2026-08-30-old-path.md"]
    assert plan.refusals[0].live_elsewhere == current
    assert plan.refusals[0].live_elsewhere != stale


def test_TWO_live_files_carrying_one_slug_refuse_to_caption_rather_than_pick_one(tmp_path):
    """Slug is NOT a unique key in a data root: the spec and plan pillars deliberately share
    one, and 83 slugs on the live tree are held by more than one file. Uniqueness IS the whole
    safety argument - an arbitrary pick reads on the report exactly like a verified one, and
    the reader cannot tell the two apart."""
    specs = tmp_path / "projects" / "alpha" / "design" / "specs"
    plans = tmp_path / "projects" / "alpha" / "design" / "plans"
    _spec(specs / "2026-09-04-shared.md", "shared-slug")
    _spec(plans / "2026-09-04-shared.md", "shared-slug")
    _orphans(
        specs,
        "2026-08-30-shared.md",
        2,
        text="---\nslug: shared-slug\nstatus: speccing\n---\n\ncopy\n",
    )

    plan = bk.plan_reap(tmp_path, keep=3, resolve_live=bk._make_live_resolver(tmp_path))

    assert [r.primary.name for r in plan.refusals] == ["2026-08-30-shared.md"]
    assert plan.refusals[0].live_elsewhere is None, "an arbitrary pick is not an answer"
    assert "REFUSE (moved)" not in bk.render(plan, root=tmp_path)


def test_a_resolver_that_RAISES_loses_only_the_caption(tmp_path):
    """Retention is the load-bearing behaviour here and it does not need this answer. The
    resolver is the only place in this module where spec knowledge - a file read, a whole-tree
    index - can fail, and taking the sweep down with it would trade the bounded half of the
    tree for a cosmetic one. The correct degradation is EXACTLY the report this module
    produced before the resolver existed."""
    specs = tmp_path / "projects" / "alpha" / "design" / "specs"
    healthy = _group(tmp_path / "projects" / "alpha" / "status", "registry.md", 8)
    _spec(specs / "2026-09-04-renamed-thing.md", "renamed-thing")
    orphans = _orphans(
        specs,
        "2026-08-30-old-thing.md",
        2,
        text="---\nslug: renamed-thing\nstatus: speccing\n---\n\ncopy\n",
    )

    def _explode(primary, members):
        raise RuntimeError("the slug index blew up")

    plan = bk.plan_reap(tmp_path, keep=3, resolve_live=_explode)
    bare = bk.plan_reap(tmp_path, keep=3)

    assert [r.primary.name for r in plan.refusals] == ["2026-08-30-old-thing.md"]
    assert plan.refusals[0].live_elsewhere is None
    assert plan.refusals[0].reason == "primary is absent"
    # Removals elsewhere in the tree are untouched by the failure.
    assert {r.path for r in plan.removals} == {r.path for r in bare.removals} == set(healthy[:5])
    assert plan.kept == bare.kept == 3
    assert all(p.exists() for p in orphans)


def test_the_resolver_is_NOT_consulted_for_a_refusal_that_is_not_an_ABSENT_primary(tmp_path):
    """A zero-length primary and a directory primary are cases where something IS at that
    path. They are not renames, and asking a resolver about them invites it to explain away a
    TRUNCATION - the exact failure the backup rule was improvised mid-incident to catch. The
    absent group in this fixture is the control: without it, a resolver that is never called
    at all would pass this test."""
    calls: list = []

    def _recorder(primary, members):
        calls.append(primary)
        return None

    truncated_dir = tmp_path / "status"
    _write(truncated_dir / "registry.md", text="")
    _orphans(truncated_dir, "registry.md", 3, text="---\nslug: registry\n---\n\ncopy\n")

    (tmp_path / "notes").mkdir()
    _orphans(tmp_path, "notes", 3, text="---\nslug: notes\n---\n\ncopy\n", firing="steward-f9")

    _orphans(tmp_path, "gone.md", 2, text="---\nslug: gone\n---\n\ncopy\n", firing="steward-f9")

    plan = bk.plan_reap(tmp_path, keep=1, resolve_live=_recorder)

    assert {r.reason for r in plan.refusals} == {
        "primary is zero-length",
        "primary is a directory",
        "primary is absent",
    }
    assert calls == [tmp_path / "gone.md"], (
        "the resolver must see the absent primary and NOTHING else; it was handed "
        f"{[str(c) for c in calls]}"
    )


def test_INVARIANT_a_fully_CAPTIONED_tree_is_still_an_UNBOUNDED_tree(tmp_path, monkeypatch, capsys):
    """THE ONE THAT MATTERS. A caption is prose for a human; it decides nothing. If explaining
    a refusal also cleared it, the most common refusal on the live tree - the gh#288 rename
    orphan - would silently stop reaching the steward, and a tree whose every group still
    holds unreconciled backups would report itself bounded and exit 0. The report gets richer;
    the exit code does not move. Both halves are asserted because `is_clean` and `main`'s
    return value are two separate places that could learn to forgive a caption."""
    specs = tmp_path / "projects" / "alpha" / "design" / "specs"
    _spec(specs / "2026-09-04-first.md", "first-thing")
    _spec(specs / "2026-09-04-second.md", "second-thing")
    made = _orphans(
        specs, "2026-08-30-first.md", 2, text="---\nslug: first-thing\n---\n\ncopy\n"
    ) + _orphans(
        specs, "2026-08-30-second.md", 2, text="---\nslug: second-thing\n---\n\ncopy\n"
    )
    before = _files_under(tmp_path)

    plan = bk.plan_reap(tmp_path, resolve_live=bk._make_live_resolver(tmp_path))
    assert len(plan.refusals) == 2
    assert all(
        r.live_elsewhere is not None for r in plan.refusals
    ), "this fixture proves nothing unless EVERY refusal is captioned"
    assert plan.removals == [], "refusals must be the only outstanding item in this tree"
    assert plan.is_clean is False, "a captioned refusal is still a refusal"

    _fake_data_root(monkeypatch, tmp_path)
    assert bk.main(["reap"]) == 1
    out = capsys.readouterr().out
    assert "REFUSE (moved)" in out
    assert "of those refusals, 2 are MOVED" in out
    assert bk.main(["reap", "--apply"]) == 1, "applying nothing does not answer a refusal"
    capsys.readouterr()
    assert _files_under(tmp_path) == before, "a captioned refusal is still never deleted"
    assert all(p.exists() for p in made)


def test_captioning_changes_NOTHING_about_what_the_reap_would_delete(tmp_path):
    """The resolver's whole contract in one comparison: the same tree, with and without it,
    and the destructive half must come out identical. `plan_reap` with no `resolve_live` is
    the shipped behaviour every other test in this file pins, so a caption that perturbed a
    removal set would have to red something here first."""
    status = tmp_path / "projects" / "alpha" / "status"
    specs = tmp_path / "projects" / "alpha" / "design" / "specs"
    _group(status, "registry.md", 9)
    _group(specs, "2026-09-01-live-epic.md", 5)
    _spec(specs / "2026-09-04-renamed-thing.md", "renamed-thing")
    _orphans(specs, "2026-08-30-old-thing.md", 4, text="---\nslug: renamed-thing\n---\n\nc\n")
    _orphans(specs, "2026-08-30-lost-thing.md", 4, text="---\nslug: lost-thing\n---\n\nc\n")

    bare = bk.plan_reap(tmp_path, keep=3)
    captioned = bk.plan_reap(tmp_path, keep=3, resolve_live=bk._make_live_resolver(tmp_path))

    assert {r.path for r in bare.removals} == {r.path for r in captioned.removals}
    assert bare.kept == captioned.kept
    assert bare.bytes_removed == captioned.bytes_removed
    assert bare.groups == captioned.groups
    assert bare.refused_backups == captioned.refused_backups
    assert {r.primary for r in bare.refusals} == {r.primary for r in captioned.refusals}
    assert bare.is_clean == captioned.is_clean

    # ...and the ONLY difference is the caption, which the bare plan never carries.
    assert {r.live_elsewhere for r in bare.refusals} == {None}
    assert sum(1 for r in captioned.refusals if r.live_elsewhere is not None) == 1

def test_TWO_refused_groups_claiming_ONE_live_file_both_lose_their_caption(tmp_path):
    """Uniqueness is TWO-sided, and the resolver can only see one side of it.

    The resolver is handed one group at a time, so it can enforce "one live file per slug" and
    still let two different absent primaries both point at that file. At most one of them is
    the relocation the report asserts. Captioning both tells a human that two separate last
    copies are disposable on the strength of a single survivor - a wrong caption, which is the
    one failure mode strictly worse than no caption at all.
    """
    specs = tmp_path / "specs"
    live = _write(specs / "done" / "2026-09-01-survivor.md", "---" + NEWLINE + "slug: shared-identity" + NEWLINE + "---" + NEWLINE)
    for name in ("2026-07-18-one.md", "2026-07-18-two.md"):
        _write(
            specs / bk.backup_name(name, "steward-f1", BASE),
            "---" + NEWLINE + "slug: shared-identity" + NEWLINE + "---" + NEWLINE,
            mtime=BASE.timestamp(),
        )

    def resolve(primary, members):
        return live

    plan = bk.plan_reap(tmp_path, keep=3, resolve_live=resolve)

    assert len(plan.refusals) == 2
    assert [r.live_elsewhere for r in plan.refusals] == [None, None], (
        "both captions must be withdrawn when two refused groups claim one live file"
    )
    assert "REFUSE (moved)" not in bk.render(plan)
    assert plan.is_clean is False


def test_a_BARE_slug_key_never_adopts_the_following_line_as_its_identity(tmp_path):
    """`slug:` with nothing after it is a MISSING identity, not the next key's value.

    A half-written frontmatter key is the shape a backfill leaves behind, and this tree is
    actively backfilling `slug:` onto older specs. A pattern whose leading whitespace class
    crosses a newline reads the NEXT line as the value, so two unrelated documents whose next
    key happens to match would caption each other.
    """
    specs = tmp_path / "specs"
    # The following line is a BARE TOKEN, deliberately: it is shape-valid as a slug, so the
    # shape whitelist cannot be what saves us here. Only a pattern that refuses to cross the
    # newline does. An earlier version of this test used "status: done" as the next line and
    # passed against the broken pattern too - the whitelist was doing the work and the test
    # proved nothing about the pattern it was written for.
    _write(specs / "done" / "2026-09-01-zulu.md", "---" + NEWLINE + "slug:" + NEWLINE + "draft" + NEWLINE + "---" + NEWLINE)
    _write(
        specs / bk.backup_name("2026-01-01-alpha.md", "steward-f1", BASE),
        "---" + NEWLINE + "slug:" + NEWLINE + "draft" + NEWLINE + "---" + NEWLINE,
        mtime=BASE.timestamp(),
    )

    resolve = bk._make_live_resolver(tmp_path)
    plan = bk.plan_reap(tmp_path, keep=3, resolve_live=resolve)

    assert len(plan.refusals) == 1
    assert plan.refusals[0].live_elsewhere is None, (
        "a bare `slug:` must yield no identity; it adopted the next line instead"
    )
