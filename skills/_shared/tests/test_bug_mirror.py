"""The bug mirror is DERIVED, and regenerating it against unchanged state changes nothing.

E-0212. Issues are the system of record; `design/bugs/` is a rendering of them. Two failure
modes are specific enough to gate, and both are silent:

  CHURN - the steward regenerates on its sweep. Without write-if-changed, every sweep rewrites
  every file, and the noise lands in `git log` and in the vault mirror. The generated-at stamp
  guarantees the bytes differ every run, so the comparison has to exclude it - while the stamp
  still ships, because a reader must be able to tell a fresh mirror from a stale one.

  HISTORY REWRITING - regenerating a closed month can only ever rewrite the past. Only the
  current month is derived; earlier months are frozen once written.

WHAT THIS GATE CAN AND CANNOT DO:

* It CAN prove the renderers are pure and deterministic, that write_if_changed is a no-op
  against unchanged state, and that the two closure outcomes are distinguished.
* It CANNOT prove the tracker data is right, or that the steward actually invokes the
  regeneration. The invocation is prose in steward/SKILL.md and is gated separately.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS_ROOT))

from agentflow.issue_intake import (  # noqa: E402
    ClosedIssue,
    Issue,
    in_flight,
    render_closed_month,
    render_open_index,
    write_if_changed,
)

STAMP_A = "2026-09-03T06:00:00Z"
STAMP_B = "2026-09-03T07:45:00Z"


def _issue(n, title, *labels):
    return Issue(number=n, title=title, labels=tuple(labels))


@pytest.fixture
def issues():
    return [
        _issue(7, "untriaged thing", "bug"),
        _issue(3, "selectable thing", "bug", "triaged"),
        _issue(5, "an epic, not a bug", "triaged", "epic"),
        _issue(9, "being built now", "bug", "triaged"),
    ]


# --------------------------------------------------------------------------- #
# Determinism - the churn defect this whole design exists to avoid
# --------------------------------------------------------------------------- #
def test_the_same_state_renders_byte_identically(issues):
    """Identical input, identical bytes. Sorting is by issue number, not by tracker order."""
    a = render_open_index(issues, linked=set(), generated_at=STAMP_A)
    b = render_open_index(list(reversed(issues)), linked=set(), generated_at=STAMP_A)
    assert a == b


def test_regenerating_unchanged_state_does_not_touch_the_file(tmp_path, issues):
    """THE regression test for churn: a second run leaves the bytes AND the mtime alone."""
    p = tmp_path / "bugs" / "index.md"
    assert write_if_changed(p, render_open_index(issues, set(), STAMP_A)) is True
    first_bytes = p.read_bytes()
    first_mtime = p.stat().st_mtime_ns

    # A LATER stamp, same tracker state - the case that would churn if the comparison
    # did not exclude the stamp.
    assert write_if_changed(p, render_open_index(issues, set(), STAMP_B)) is False
    assert p.read_bytes() == first_bytes
    assert p.stat().st_mtime_ns == first_mtime


def test_a_real_change_does_write(tmp_path, issues):
    """The no-op must not be achieved by never writing."""
    p = tmp_path / "index.md"
    write_if_changed(p, render_open_index(issues, set(), STAMP_A))
    changed = issues + [_issue(11, "brand new bug", "bug")]
    assert write_if_changed(p, render_open_index(changed, set(), STAMP_A)) is True
    assert "#11" in p.read_text(encoding="utf-8")


def test_freshness_is_still_legible_after_a_no_op(tmp_path, issues):
    """The stamp SHIPS even though the comparison ignores it.

    'ran and found nothing' must not read identically to 'never ran' - the same discipline
    as an alarm that records a non-firing check.
    """
    p = tmp_path / "index.md"
    write_if_changed(p, render_open_index(issues, set(), STAMP_A))
    assert STAMP_A in p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #
def test_in_flight_comes_from_claimed_rows_not_from_a_label(issues):
    """Derived from the registry's own claims: a label would be a second thing to update."""
    assert [i.number for i in in_flight(issues, ["building gh#9 now"])] == [9]


def test_a_bug_in_flight_is_not_also_listed_as_selectable(issues):
    body = render_open_index(issues, set(), STAMP_A, claimed_refs=["gh#9"])
    in_flight_block = body.split("## Triaged")[0]
    selectable_block = body.split("## Triaged")[1].split("## Untriaged")[0]
    assert "#9" in in_flight_block
    assert "#9" not in selectable_block


def test_an_epic_labelled_issue_is_not_a_bug(issues):
    assert "#5" not in render_open_index(issues, set(), STAMP_A)


def test_an_empty_group_says_so_rather_than_rendering_nothing():
    """An empty section that renders as nothing is indistinguishable from one that failed."""
    assert "None." in render_open_index([], set(), STAMP_A)


def test_a_bug_carrying_the_marker_renders_in_flight_and_not_nowhere(issues):
    """gh#56, and the reason the fix is not a one-line exclusion in `builder_selectable`.

    `in_flight` and `render_open_index` were both BUILT ON TOP of `builder_selectable`, so
    excluding the marker there and nowhere else deletes the issue from all three groups at
    once: not untriaged, not selectable, and no longer in flight. The bug a lane is actively
    building disappears from the mirror entirely, and "In flight" renders `None.` - a line
    that exists precisely so an empty group cannot be mistaken for a failed one, turned into
    a false statement by the fix for the opposite problem.

    Note the empty `claimed_refs`: the marker alone has to be enough, because the two claim
    routes are written at different moments and a lane may hold one before the other.
    """
    marked = _issue(11, "claimed by label alone", "bug", "triaged", "in-progress")
    body = render_open_index(issues + [marked], set(), STAMP_A, claimed_refs=())

    in_flight_block = body.split("## Triaged")[0]
    rest = body.split("## Triaged")[1]
    assert "#11" in in_flight_block
    assert "#11" not in rest
    assert "## In flight (1)" in body


def test_every_bug_lands_in_exactly_one_group(issues):
    """The general form of the test above, so a future regrouping cannot reopen the hole:
    every issue handed in is named once, in one group, and never in none of them.

    gh#446 widened the population: a decision-bearing bug is excluded from selectable, so
    unless a group picks it up it is exactly the invisible row this test exists to catch -
    once unclaimed (#13), once held by the marker (#15), once by a registry row (#17)."""
    marked = _issue(11, "claimed by label alone", "bug", "triaged", "in-progress")
    all_issues = issues + [
        marked,
        _issue(13, "waiting on a product call", "bug", "triaged", "needs-decision"),
        _issue(15, "decision label, marker held", "bug", "triaged", "needs-decision",
               "in-progress"),
        _issue(17, "decision label, registry held", "bug", "triaged", "needs-decision"),
    ]
    body = render_open_index(all_issues, set(), STAMP_A, claimed_refs=["gh#9", "gh#17"])
    blocks = [b for b in body.split("## ")[1:]]
    for issue in all_issues:
        if "epic" in issue.labels:
            continue  # an epic is not bug work at all - asserted separately above
        hits = [b for b in blocks if f"#{issue.number}" in b]
        assert len(hits) == 1, f"#{issue.number} appears in {len(hits)} groups, expected 1"


def _group(body, heading):
    """The block under one ``## <heading>`` line, up to the next group."""
    return body.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


def test_the_groups_render_in_lifecycle_order_with_the_decision_group_before_untriaged(issues):
    """In flight, then selectable, then waiting on a human, then not yet dispositioned - the
    reverse of the path a bug walks, so the group nearest done comes first. The decision group
    sits after selectable because the split-based assertions above read everything between
    ``## Triaged`` and ``## Untriaged`` as not-in-flight, and must keep doing so."""
    body = render_open_index(issues, set(), STAMP_A)
    headings = [line.split(" (")[0] for line in body.splitlines() if line.startswith("## ")]
    assert headings == ["## In flight", "## Triaged - selectable",
                        "## Awaiting a decision", "## Untriaged"]


def test_an_unclaimed_decision_bearing_bug_renders_under_awaiting_a_decision(issues):
    """gh#446 - the issue is off the build queue, so it has to be ON a view a human reads,
    or the decision nobody is asked to make is never made."""
    waiting = _issue(13, "waiting on a product call", "bug", "triaged", "needs-decision")
    body = render_open_index(issues + [waiting], set(), STAMP_A)
    assert "## Awaiting a decision (1)" in body
    assert "#13" in _group(body, "Awaiting a decision")
    assert "#13" not in _group(body, "Triaged - selectable")


def test_a_claimed_decision_bearing_bug_renders_in_flight_not_awaiting(issues):
    """Held work is live work first. Both claim routes, as in_flight honours both."""
    marked = _issue(15, "decision label, marker held", "bug", "triaged", "needs-decision",
                    "in-progress")
    registry = _issue(17, "decision label, registry held", "bug", "triaged", "needs-decision")
    body = render_open_index(issues + [marked, registry], set(), STAMP_A,
                             claimed_refs=["gh#17"])
    flight = _group(body, "In flight")
    awaiting = _group(body, "Awaiting a decision")
    assert "#15" in flight and "#17" in flight
    assert "#15" not in awaiting and "#17" not in awaiting
    assert "## Awaiting a decision (0)" in body


def test_the_glosses_say_what_each_group_excludes_and_who_acts(issues):
    """The selectable gloss used to read 'triaged and not epic', which stopped being the rule
    at gh#56 and again at gh#446. The decision group must read as a human call, not as build
    work a lane will get to."""
    body = render_open_index(issues, set(), STAMP_A)
    selectable = _group(body, "Triaged - selectable")
    for label in ("`triaged`", "`epic`", "`needs-decision`", "`in-progress`"):
        assert label in selectable
    awaiting = _group(body, "Awaiting a decision")
    assert "`needs-decision`" in awaiting
    assert "human" in awaiting
    assert "not build work" in awaiting


def test_in_flight_honours_the_marker_as_well_as_a_registry_claim(issues):
    """Both routes, and neither swallows the other."""
    marked = _issue(11, "claimed by label alone", "bug", "triaged", "in-progress")
    both = in_flight(issues + [marked], ["building gh#9 now"])
    assert [i.number for i in both] == [9, 11]


# --------------------------------------------------------------------------- #
# Closed months
# --------------------------------------------------------------------------- #
def test_the_two_closure_outcomes_are_distinguished():
    """An issue closed with no PR was dispositioned, not shipped.

    Reading the second as the first overstates what the fleet delivered.
    """
    shipped = ClosedIssue(1, "fixed", ("bug",), "2026-09-01T00:00:00Z",
                          closing_prs=((10, "u"),), merge_shas=("abcdef1234",))
    disposed = ClosedIssue(2, "not real", ("bug",), "2026-09-01T00:00:00Z")
    assert shipped.outcome == "shipped"
    assert disposed.outcome == "closed without a PR"
    body = render_closed_month([shipped, disposed], "2026-09", STAMP_A)
    assert "PR #10 (abcdef1)" in body
    assert "closed without a PR" in body


def test_a_missing_sha_is_stated_not_omitted():
    """The reader must tell 'no merge commit recorded' from 'this mirror did not look'."""
    c = ClosedIssue(1, "x", (), "2026-09-01T00:00:00Z", closing_prs=((10, "u"),), merge_shas=("",))
    assert "sha unavailable" in render_closed_month([c], "2026-09", STAMP_A)


def test_a_frozen_month_is_never_rewritten(tmp_path):
    """Only the current month regenerates; a past month keeps whatever it already says."""
    past = tmp_path / "closed-2026-08.md"
    past.write_text("# Bugs closed 2026-08\n\nhand-frozen content\n", encoding="utf-8")
    before = past.read_bytes()

    # The caller renders only the CURRENT month; nothing addresses the frozen file.
    write_if_changed(tmp_path / "closed-2026-09.md",
                     render_closed_month([], "2026-09", STAMP_A))
    assert past.read_bytes() == before


def test_the_header_tells_a_human_not_to_edit_it(issues):
    """The cost of a derived view is that annotations belong on the issue. Say so, in the file."""
    for body in (render_open_index(issues, set(), STAMP_A),
                 render_closed_month([], "2026-09", STAMP_A)):
        assert "do not edit" in body.lower()
        assert "Edit the issue" in body
