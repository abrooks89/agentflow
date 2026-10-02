"""Every self-waking seat records that its continuation cannot be verified from code (gh#315).

WHAT gh#315 ASKED FOR AND WHY IT IS NOT HERE. The issue proposed a post-condition: the lane
asserts its own next wake exists before it writes `idle`, the way `deploy-lock assert` does. That
cannot be built. `ScheduleWakeup` leaves no artifact this suite can read -- `preflight.py` states
the general boundary in its own docstring, and the harness's `scheduled-tasks` directory was
measured holding one entry dated 2026-04-09 against fifteen wakes in a single session. So the
assert could only re-state the seat's own belief, and a firing that forgot the call would equally
forget the assert. **A check that appears to verify something it cannot is worse than no check**,
because the board then reads as guarded. Q-10071 put that to the fleet and the steward ruled it
out of scope under delegated authority on 2026-09-13, leaving the enum branch with the human.

SO THE DELIVERABLE IS THE RECORD, AND THIS GATE IS WHAT KEEPS IT. A paragraph in six files rots
silently: one seat gets rewritten, the block goes with it, and the next reader re-opens gh#315
assuming the artifact exists. Prose that is load-bearing needs something that can go red.

WHY BYTE-EQUALITY RATHER THAN A KEYWORD. Six near-copies drift into six different claims, and the
measurement is the part that drifts first -- a date softened to "recently", a count dropped. The
block is asserted identical across all six, so a partial edit fails in the commit that makes it
rather than at the next reader.

WHY THE SEAT LIST IS DERIVED. `registry.AGENTS` minus `dev-manager` IS the self-waking set, and
deriving it means a seventh seat joining the roster lands in this gate on the commit that adds it.
A hand-written list of six would have been complete on the day it was written and silently short
after that -- the failure `test_corpus_from_index` exists to prevent one layer down.

WHAT THIS GATE DOES NOT CLAIM. It proves the record is present and identical. It does not prove
any seat obeys it, and it cannot: that is the same unverifiable boundary the block is about. The
honest position is that this pins the FACT, not the behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS_ROOT))

import pytest  # noqa: E402

from agentflow.registry import AGENTS  # noqa: E402
from agentflow.tracked_paths import only_tracked  # noqa: E402

REPO_ROOT = SKILLS_ROOT.parents[1]

#: The self-waking seats: every roster seat except the one a human launches by hand.
#: `dev-manager` never schedules its own next firing, so the record would be a duty it
#: cannot discharge.
SELF_WAKING = tuple(a for a in AGENTS if a != "dev-manager")
HUMAN_LAUNCHED = tuple(a for a in AGENTS if a == "dev-manager")

#: The first line of the block, used only to locate it in a failure message.
ANCHOR = (
    "**THE CONTINUATION CANNOT BE VERIFIED FROM CODE, "
    "AND NOTHING HERE PRETENDS IT CAN.**"
)

#: Claims the block must carry. Each is a load-bearing fact the record exists to preserve (the
#: missing artifact, the boundary, the verdict, and the stall a post-condition would miss);
#: losing any one of them turns the paragraph back into an assertion.
REQUIRED_CLAIMS = (
    "unbuildable as specified",
    "preflight.py",
    "does not record `ScheduleWakeup` wakes",
    "make the call and still get no wake",
    "would have been GREEN",
)


def _skill(seat: str) -> Path:
    return SKILLS_ROOT.parent / seat / "SKILL.md"


def _text(seat: str) -> str:
    return _skill(seat).read_text(encoding="utf-8")


def _block(text: str) -> str:
    """The block as it appears in `text`, from the anchor to the blank line after it.

    Slicing to the end of the second paragraph rather than to a fixed length keeps the
    comparison honest if the block is ever deliberately extended: every copy still has to
    agree with every other one.
    """
    i = text.index(ANCHOR)
    end = text.index("\n\n", text.index("\n\n", i) + 2)
    return text[i:end]


def test_the_roster_split_is_exhaustive_and_disjoint() -> None:
    """Non-vacuity for every case below: a renamed seat must not silently leave the gate."""
    assert set(SELF_WAKING) | set(HUMAN_LAUNCHED) == set(AGENTS)
    assert not set(SELF_WAKING) & set(HUMAN_LAUNCHED)
    assert len(SELF_WAKING) == 6, SELF_WAKING
    assert HUMAN_LAUNCHED == ("dev-manager",), HUMAN_LAUNCHED


@pytest.mark.parametrize("seat", SELF_WAKING)
def test_every_self_waking_seat_carries_the_record(seat: str) -> None:
    assert ANCHOR in _text(seat), (
        f"skills/{seat}/SKILL.md lost the gh#315 record. The paragraph is the epic's whole "
        f"deliverable: without it the next reader re-opens gh#315 believing the post-condition "
        f"it proposes can be built."
    )


@pytest.mark.parametrize("seat", SELF_WAKING)
@pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
def test_the_record_keeps_the_measurement_that_earns_it(seat: str, claim: str) -> None:
    """A record stripped of its measurements is an opinion, and opinions get overruled."""
    assert claim in _block(_text(seat)), (
        f"skills/{seat}/SKILL.md still has the block but dropped {claim!r}. That string is "
        f"evidence, not decoration - it is what makes the paragraph re-checkable."
    )


def test_all_six_copies_are_byte_identical() -> None:
    """The drift case, and the reason this is not six independent substring checks."""
    blocks = {seat: _block(_text(seat)) for seat in SELF_WAKING}
    distinct = set(blocks.values())
    assert len(distinct) == 1, (
        "the six copies of the gh#315 record have diverged; edit them together or not at all. "
        f"Lengths by seat: { {s: len(b) for s, b in blocks.items()} }"
    )


def test_the_human_launched_seat_does_NOT_carry_it() -> None:
    """The discriminating case. Without it, a gate that pasted the block everywhere would pass.

    `dev-manager` is launched by a human and schedules nothing, so the record would be about a
    call it never makes. This asserts the split means something rather than being decorative.
    """
    for seat in HUMAN_LAUNCHED:
        assert ANCHOR not in _text(seat), (
            f"skills/{seat}/SKILL.md carries a record about scheduling its own wake, which it "
            f"never does. Either the seat gained a continuation - in which case move it into "
            f"the self-waking set deliberately - or the block was pasted too widely."
        )


def test_every_skill_this_gate_reads_is_in_the_index() -> None:
    """The corpus rule from gh#175, applied to a set that is derived rather than walked.

    Nothing here globs, so the static walk detector has nothing to catch - but a path that is
    not in the index is still a verdict about one checkout rather than about the commit, and
    that is the defect the rule is named for.
    """
    wanted = [_skill(seat) for seat in AGENTS]
    tracked = only_tracked(REPO_ROOT, wanted)
    missing = sorted(p.name for p in set(wanted) - set(tracked))
    assert not missing, (
        f"these SKILL.md are read by this gate but are not in the git index: {missing}. "
        f"A gate over an untracked file passes in one checkout and fails in CI."
    )
    assert len(tracked) == len(AGENTS), (tracked, AGENTS)
