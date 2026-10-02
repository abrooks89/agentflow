"""The grounding precondition E-0228 added must STAY in every seat that raises a question.

The PR claimed this was "asserted against the shipped text" and nothing asserted it: every block
could have been deleted with the suite green. That is the epic's own failure one level up - a
rule whose only enforcement is that somebody remembered to write it.

Modelled on the repo's other prose gates: the file set is DERIVED by grepping for the ask
instruction, never a hand-kept tuple, so a new lane joins the gate the moment it can raise a
question.

THREE SEATS, THREE DUTIES, and the split is the reason the count is not one number:
  * the RAISERS (any seat whose skill names `ask`) carry the precondition itself;
  * `dev-manager` never raises - it is the last reader before the human, so it carries the
    relay-side check;
  * `steward` never raises either, but it ANSWERS under delegated authority, so it carries the
    check-before-you-answer half.
Writing "all seven skills carry the precondition" was loose in exactly the way this gate exists
to prevent.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from agentflow.tracked_paths import only_tracked

SKILLS = Path(__file__).resolve().parents[2]

#: A seat that RAISES questions: its skill names the `ask` verb or an urgency value.
_RAISES = re.compile(r"ask\s+--actor|registry\s+ask\b|--urgency\s+(halted|waiting|advisory)")

#: How far apart the precondition and the ask instruction may sit and still be read together.
#: Proximity rather than strict order: the five files legitimately differ in layout - the auditor
#: keeps its whole write-set list contiguous and puts the block under the bullet it governs -
#: and pinning one layout would fail a correct file.
_NEARBY = 6000


def _raiser_skills() -> list[Path]:
    return sorted(
        p for p in only_tracked(SKILLS, SKILLS.glob("*/SKILL.md"))
        if _RAISES.search(p.read_text(encoding="utf-8"))
    )


def test_the_derived_set_is_not_empty_and_names_every_seat_that_can_ask() -> None:
    """A derived set that silently goes empty is a gate guarding nothing. Asserted as a floor
    plus a membership, never an exact list, so a new lane joins without editing this file."""
    names = {p.parent.name for p in _raiser_skills()}
    assert len(names) >= 5, names
    for expected in ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "auditor"):
        assert expected in names, f"{expected} raises questions and is not in the derived set"


@pytest.mark.parametrize("skill", [p.parent.name for p in _raiser_skills()])
def test_every_raising_seat_carries_the_grounding_precondition(skill: str) -> None:
    """The three load-bearing halves, each its own assertion so a partial deletion is named."""
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "GROUND IT FIRST" in text, "the precondition heading is gone"
    assert "show the reading" in text, (
        "the rule is not 'use invariants' - Q-10030 tried that and picked a false one"
    )
    assert re.search(r"retract --actor", text), "the way OFF the board is not named"


@pytest.mark.parametrize("skill", [p.parent.name for p in _raiser_skills()])
def test_the_precondition_sits_WITH_the_ask_instruction(skill: str) -> None:
    """A precondition filed somewhere else in a 1200-line skill is a precondition nobody reads
    at the moment it applies."""
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    ground = text.index("GROUND IT FIRST")
    asks = [m.start() for m in _RAISES.finditer(text)]
    assert asks, skill
    assert min(abs(a - ground) for a in asks) < _NEARBY, (
        f"{skill}: the precondition is {min(abs(a - ground) for a in asks)} chars from the "
        "nearest ask instruction"
    )


@pytest.mark.parametrize("skill", [p.parent.name for p in _raiser_skills()])
def test_the_precondition_carries_the_evidence_that_earned_it(skill: str) -> None:
    """BOTH failure shapes, because the pair is the argument: one premise decays after it is
    written (Q-10028), the other is false when written (Q-10030). A block naming only the first
    teaches "re-measure"; it takes the second to teach "show the reading". Pinned as the shapes'
    own words rather than the row ids, so the rule stands without a pointer into one registry."""
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "false by the time it is read" in text and "false when it was written" in text


def test_dev_manager_carries_the_RELAY_side() -> None:
    """It does not raise, so it is correctly outside the set above - and it is the last reader
    before the human, which is the other end of the same duty."""
    text = (SKILLS / "dev-manager" / "SKILL.md").read_text(encoding="utf-8")
    assert "CHECK THE PREMISE BEFORE YOU RELAY IT" in text
    assert "raiser-only" in text, "it must say why it cannot retract the row itself"


def test_steward_carries_the_ANSWER_side() -> None:
    """It answers under delegated authority, so it meets a decayed premise from the third
    direction: not raising it, not relaying it, deciding it."""
    text = (SKILLS / "steward" / "SKILL.md").read_text(encoding="utf-8")
    assert "CHECK THE PREMISE BEFORE YOU ANSWER" in text
    assert "retract" in text


def test_no_seat_outside_the_three_groups_was_given_the_block() -> None:
    """A widening nobody signed off is the mirror of a rule nobody enforces. `wiki-save`'s
    SAVING_SEATS list learned this the hard way; the same discipline applies here."""
    allowed = {p.parent.name for p in _raiser_skills()} | {"dev-manager", "steward"}
    for p in only_tracked(SKILLS, SKILLS.glob("*/SKILL.md")):
        if p.parent.name in allowed:
            continue
        assert "GROUND IT FIRST" not in p.read_text(encoding="utf-8"), (
            f"{p.parent.name} does not raise, relay or answer questions"
        )
