"""WORK shows bug work beside epic work, and an empty bug column says which kind of empty.

E-0212, decision 6. Bugs already reach builders through `builder_selectable` and lead-dev section 2 step 4;
what was missing is that a reader could not see "three bugs are being worked right now" at a
glance. WORK was extended rather than a ninth section added: the section order is locked at
eight and bugs are straightforwardly work.

WHAT THIS GATE CAN AND CANNOT DO:

* It CAN prove the bug columns are a re-slice of INTAKE's single issue read, that in-flight is
  decided by EITHER claim route - a registry claim or the `in-progress` marker (gh#56) - that a
  marked bug reaches the in-progress column rather than falling out of both, that
  `no_work_anywhere` still accounts for bugs, and that an unreachable tracker is
  distinguishable from a tracker with no bugs.
* It CANNOT prove the rendered markdown looks right. That is the artifact renderer's, and this
  module deliberately stops at the data.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS_ROOT))

from agentflow.fleet_report import (  # noqa: E402
    IntakeSection,
    Rows,
    WorkSection,
    _attach_bug_work,
)
from agentflow.issue_intake import Issue  # noqa: E402


def _bug(n, title="a bug"):
    return Issue(number=n, title=title, labels=("bug", "triaged"))


def _claimed_bug(n, title="a claimed bug"):
    """A bug carrying the `in-progress` marker - which means `builder_selectable` no longer
    returns it, so it reaches WORK through INTAKE's `claimed` half and nowhere else."""
    return Issue(number=n, title=title, labels=("bug", "triaged", "in-progress"))


def _intake(*selectable, untriaged_count=0, available=True, reason="", claimed=()):
    return IntakeSection(
        available=available,
        reason=reason,
        selectable_count=len(selectable),
        untriaged_count=untriaged_count,
        selectable=Rows(items=tuple(selectable), total=len(selectable)),
        claimed=Rows(items=tuple(claimed), total=len(claimed)),
    )


class _Row:
    def __init__(self, work="", phase=""):
        self.work, self.phase = work, phase


class _Reg:
    def __init__(self, *rows):
        self.agents = {str(i): r for i, r in enumerate(rows)}
        self.queue = {}


# --------------------------------------------------------------------------- #
# The split
# --------------------------------------------------------------------------- #
def test_a_selectable_bug_nobody_claimed_is_claimable():
    w = _attach_bug_work(WorkSection(), _intake(_bug(21)), _Reg())
    assert [i.number for i in w.bug_claimable.items] == [21]
    assert w.bug_in_progress.total == 0


def test_a_bug_named_in_a_claimed_row_is_in_flight_not_claimable():
    """The registry route, unchanged by gh#56. It was the only route, and it survives as one
    of two: the registry claim is the RECORD of who is building what and has a reaper behind
    it, so widening in-flight to also honour the marker did not replace it."""
    reg = _Reg(_Row(work="E-0212", phase="building gh#21 now"))
    w = _attach_bug_work(WorkSection(), _intake(_bug(21), _bug(22)), reg)
    assert [i.number for i in w.bug_in_progress.items] == [21]
    assert [i.number for i in w.bug_claimable.items] == [22]


def test_a_bug_is_never_in_both_columns():
    """The two columns partition the selectable set; double-counting inflates the board."""
    reg = _Reg(_Row(phase="gh#21"))
    w = _attach_bug_work(WorkSection(), _intake(_bug(21)), reg)
    both = {i.number for i in w.bug_claimable.items} & {i.number for i in w.bug_in_progress.items}
    assert both == set()


def test_a_label_claimed_bug_stays_in_the_in_progress_column():
    """gh#56's blast radius in WORK, and the reason `IntakeSection` grew a `claimed` half.

    `bug_in_progress` used to be a re-slice of `intake.selectable` alone. Once
    `builder_selectable` excludes the marker, a marked bug is not in that list at all, so
    splitting it drops the issue out of BOTH columns - a board reading `0 in progress` while
    a lane builds, which is indistinguishable from an idle fleet. It is the visibility E-0212
    was built to add, deleted by the fix for the opposite problem.
    """
    w = _attach_bug_work(WorkSection(), _intake(_bug(22), claimed=(_claimed_bug(21),)), _Reg())
    assert [i.number for i in w.bug_in_progress.items] == [21]
    assert [i.number for i in w.bug_claimable.items] == [22]


def test_a_label_claimed_bug_is_counted_once_even_when_the_registry_claims_it_too():
    """Both routes on the same issue is the EXPECTED steady state under the lane-writes-it
    disposition - a lane takes a registry row and marks the issue. It must not appear twice."""
    reg = _Reg(_Row(work="E-0212", phase="building gh#21 now"))
    w = _attach_bug_work(WorkSection(), _intake(claimed=(_claimed_bug(21),)), reg)
    assert [i.number for i in w.bug_in_progress.items] == [21]
    assert w.bug_in_progress.total == 1
    assert w.bug_claimable.total == 0


def test_a_board_whose_only_bug_work_is_claimed_is_not_reported_as_empty():
    """`IntakeSection.is_empty` counted untriaged + selectable. A tracker holding one marked
    bug and nothing else would have read "looked, found nothing" - the conflation this
    section refuses everywhere else."""
    assert _intake(claimed=(_claimed_bug(21),)).is_empty is False


def test_untriaged_carries_across_as_the_bug_equivalent_of_drafted():
    w = _attach_bug_work(WorkSection(), _intake(_bug(21), untriaged_count=4), _Reg())
    assert w.bug_untriaged_count == 4


# --------------------------------------------------------------------------- #
# The two kinds of empty - the distinction INTAKE exists to preserve
# --------------------------------------------------------------------------- #
def test_an_unreachable_tracker_records_WHY_rather_than_showing_zero_bugs():
    """"No open bugs" and "we could not look" are different facts a reader acts on differently.

    INTAKE refuses to conflate them; WORK must not undo that one section later.
    """
    w = _attach_bug_work(
        WorkSection(), _intake(available=False, reason="tracker unreachable: boom"), _Reg())
    assert w.bug_note == "tracker unreachable: boom"
    assert w.bug_claimable.total == 0


def test_a_reachable_tracker_with_no_bugs_leaves_no_note():
    w = _attach_bug_work(WorkSection(), _intake(), _Reg())
    assert w.bug_note == ""


# --------------------------------------------------------------------------- #
# is_empty must account for the new columns, or a bug-only board reads as idle
# --------------------------------------------------------------------------- #
def test_a_work_section_with_only_bugs_is_not_empty():
    """The exact shape this epic exists for: bug work happening, zero epic work."""
    w = _attach_bug_work(WorkSection(), _intake(_bug(21)), _Reg())
    assert w.approved_claimable.total == 0
    assert w.is_empty is False


@pytest.mark.parametrize("field,value", [
    ("bug_claimable", Rows(items=(_bug(1),), total=1)),
    ("bug_in_progress", Rows(items=(_bug(1),), total=1)),
    ("bug_untriaged_count", 3),
    ("bug_shipped", Rows(items=(_bug(1),), total=1)),
])
def test_every_bug_column_alone_makes_the_section_non_empty(field, value):
    """Each column is checked separately: a partial is_empty is the silent version of this bug."""
    assert replace(WorkSection(), **{field: value}).is_empty is False
