"""choose-next's bucket prose matches what `blocked_status` computes (gh#704).

Selection is computed by code, but the explanation the operator reads comes from the prose in
Steps 2 and 3. The prose quotes `blocked_status`'s own ordered list, so this pins the quote to
the docstring and pins the ended-board states the list alone does not spell out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentflow import epic_survey as s

SKILL = Path(__file__).resolve().parents[2] / "choose-next" / "SKILL.md"


def _step(n: int) -> str:
    text = SKILL.read_text(encoding="utf-8")
    body = text.split(f"### Step {n}:", 1)[1]
    return body.split("\n### ", 1)[0]


def _docstring_order() -> list[str]:
    doc = s.blocked_status.__doc__ or ""
    block = doc.split("Evaluated in this order (first match wins):", 1)[1].split("\n\n", 1)[0]
    lines = [ln.strip() for ln in block.strip().splitlines()]
    assert lines and all(ln.startswith("* ") for ln in lines), lines
    return lines


def test_docstring_order_names_the_fall_through():
    # The last bucket is where a done_on_hold parent lands; a list that stops at "unknown"
    # leaves the reader to guess.
    assert "done_on_hold" in _docstring_order()[-1]
    assert "CHAINED" in _docstring_order()[-1]


def test_step3_quotes_the_docstring_order_verbatim():
    step3 = [ln.strip() for ln in _step(3).splitlines()]
    order = _docstring_order()
    start = step3.index(order[0]) if order[0] in step3 else -1
    assert start >= 0, f"Step 3 does not quote blocked_status's order; first line: {order[0]!r}"
    assert step3[start : start + len(order)] == order


def test_step2_names_the_ended_board_states():
    step2 = _step(2)
    for state in ("abandoned", "failed", "done_on_hold"):
        assert f"`{state}`" in step2, state


def test_step2_buckets_unreadable_boards_apart_from_candidates():
    # A board nobody can read a run out of (an unrecognised `status:`, or more than one board for
    # the slug) is refused by the survey: neither a candidate nor a dep anyone can wait on.
    step2 = _step(2)
    assert "**Unreadable boards**" in step2
    bullet = step2.split("**Unreadable boards**", 1)[1].split("\n- **", 1)[0]
    for state in (s.UNRECOGNISED_BOARD_STATUS, s.DUPLICATE_STATUS_BOARDS):
        assert f"`{state}`" in bullet, state
        assert not s.is_candidate(s.EpicRecord(slug="x", title="x", lifecycle_state=state))
    assert "not a candidate" in bullet and "MISSING-DEP" in bullet


def test_step3_explains_ended_parents_honestly():
    step3 = _step(3)
    assert "will never land" in step3
    assert "warn_stranded_terminal_dependencies" in step3
    assert "awaiting a human merge" in step3
    # The old reading, that every MISSING-DEP is a not-yet-written epic, is gone.
    assert not re.search(r"MISSING-DEP\*\* \(treat as near-ready", step3)


@pytest.mark.parametrize(
    ("parent", "bucket"),
    [
        ("abandoned", "MISSING-DEP"),
        ("failed", "MISSING-DEP"),
        ("done_on_hold", "CHAINED"),
        (s.UNRECOGNISED_BOARD_STATUS, "MISSING-DEP"),
        (s.DUPLICATE_STATUS_BOARDS, "MISSING-DEP"),
    ],
)
def test_the_prose_matches_the_computed_bucket(parent, bucket):
    def rec(slug, state, deps=()):
        return s.EpicRecord(
            slug=slug, title=slug, lifecycle_state=state, depends_on=list(deps), project="demo"
        )

    recs = [rec("parent", parent), rec("child", "speccing", ["parent"])]
    assert s.blocked_status(recs[1], recs) == bucket
    blocks = _bucket_explanations()
    assert bucket in blocks, f"Step 3 explains no {bucket} bucket"
    assert f"`{parent}`" in blocks[bucket], f"Step 3 does not put {parent} in {bucket}"
    for other, text in blocks.items():
        if other != bucket:
            assert f"`{parent}`" not in text, f"Step 3 also puts {parent} in {other}"


EXPLANATION = "What each bucket tells the operator:"


def _bucket_explanations() -> dict[str, str]:
    """Maps each bucket to its bullet, sub-bullets included, in Step 3's explanation list.

    Only the text after EXPLANATION counts: the verbatim docstring quote above it names every
    parent on its bucket's line, so a check that read it would pass whatever the explanation says.
    """
    step3 = _step(3)
    assert EXPLANATION in step3, "Step 3 lost its per-bucket explanation"
    tail = step3.split(EXPLANATION, 1)[1].split("\n\n", 1)[0]
    blocks: dict[str, str] = {}
    current = None
    for ln in tail.strip().splitlines():
        m = re.match(r"- \*\*([A-Z-]+)\*\*", ln)
        if m:
            current = m.group(1)
            blocks[current] = ln
        elif current is not None:
            blocks[current] += "\n" + ln
    return blocks


def test_step2_reads_candidates_through_the_shared_predicate():
    # The skill and `blocked_status` must agree on what a candidate is; a restated tuple is how
    # the skill came to skip the drafted and approved stages.
    step2 = _step(2)
    assert "is_candidate(r)" in step2
    assert '("speccing", "planning")' not in step2
    for state in ("drafted", "approved-for-autonomous"):
        assert f"`{state}`" in step2, state


def test_missing_dep_names_the_file_stem_cause_and_the_check_that_finds_it():
    blocks = _bucket_explanations()
    assert "dep spelled as a file stem; maps to `<slug>` (done)" in blocks["MISSING-DEP"]
    assert "one of five reasons" in blocks["MISSING-DEP"]
    assert "resolves_after_normalization(dep, known)" in _step(3)
    assert "_resolves_after_normalization" not in _step(3)

    # ...and the check the prose names really separates the two causes on such a dep.
    def rec(slug, state, deps=()):
        return s.EpicRecord(
            slug=slug, title=slug, lifecycle_state=state, depends_on=list(deps), project="otherproj"
        )

    recs = [
        rec("parent", "done"),
        rec("child", "drafted", ["2026-01-02-parent", "ghost"]),
    ]
    assert s.blocked_status(recs[1], recs) == "MISSING-DEP"
    known = {r.slug for r in recs}
    assert s.resolves_after_normalization("2026-01-02-parent", known) == "parent"
    assert s._resolves_after_normalization("ghost", known) is None


def test_steps_5_and_7_hand_over_the_surveyed_source_path():
    step5, step7 = _step(5), _step(7)
    assert "handoff_path(record, env.data_root)" in step5
    assert "record.source_path" in step5
    assert "handoff_path" in step7
    # The rebuilt-path recipe survives only as the fallback handoff_path runs, never as the
    # instruction.
    assert "design/specs/YYYY-MM-DD-<slug>.md" not in step5


def test_a_drafted_pick_shows_its_missing_approval_on_the_card_and_at_handoff():
    # Drafted and speccing epics are candidates now; nothing else on the way to /project tells
    # the human that no one has approved them for autonomous builds.
    assert "speccing" in _step(2) and "no approval for autonomous builds" in _step(2)
    step6 = _step(6)
    assert "lifecycle state" in step6
    assert "drafted - not yet approved for autonomous builds" in step6
    step7 = _step(7).split("If the user picks", 1)[0]
    assert "is drafted - not yet approved for autonomous builds" in step7
    assert step7.index("not yet approved") < step7.index("Invoke `/project <path>`")
