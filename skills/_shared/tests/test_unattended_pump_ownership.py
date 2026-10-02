"""The unattended pump lives in exactly one loop, and the prose says which (E-0220).

`reap` / `stand-down` / `repair` / `cull` changed owner in code, and the actor gates for that
are tested in `test_registry.py`. This module is the PROSE half: the two skill bodies that tell
a loop what it is responsible for. Both halves matter, and they fail differently — a gate with
no prose is a capability nobody uses, and prose with no gate is a rule nothing enforces.

The failure this guards against is specific and has happened here: a duty that reads as owned
by two loops, or by none. dev-manager self-woke on a cadence AND was the human surface, one
transcript served both, and the pump had to move rather than be told to be quieter.

WHAT THIS GATE CAN AND CANNOT DO — stated because a gate that overstates itself is worse than
none:

* It CAN prove a named duty appears in the skill that now owns it and has stopped being claimed
  by the skill that lost it. It CAN prove the `ScheduleWakeup` token is absent from
  dev-manager, which is the blunt check the spec asked for: a rename cannot evade it.
* It CANNOT prove the prose is correct, well-placed, or that a loop will follow it. It is a
  file-level substring check. A pass here is "the words are in the right file", never "the
  design is right".
* It deliberately does NOT assert the steward's Ntfy or cadence behaviour. Those are unchanged
  by this epic, and a test that restates untouched behaviour reads as coverage while proving
  the move worked no better than not existing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[2]
STEWARD = SKILLS_ROOT / "steward" / "SKILL.md"
DEV_MANAGER = SKILLS_ROOT / "dev-manager" / "SKILL.md"

#: The four verbs that changed owner. Kept as one tuple so a fifth cannot be added to the
#: code gates without this list going stale visibly.
MOVED_VERBS = ("reap", "stand-down", "repair", "cull")


@pytest.fixture(scope="module")
def steward() -> str:
    return STEWARD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dev_manager() -> str:
    return DEV_MANAGER.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# dev-manager: the seat exists only when a human opens it
# --------------------------------------------------------------------------- #
def test_dev_manager_body_contains_no_wake_scheduling_token(dev_manager: str) -> None:
    """The spec's own test, and the bluntest form on purpose.

    A substring check cannot be satisfied by a rename or a clever paraphrase, which is exactly
    what you want guarding a self-wake that was removed for causing harm. It also means the
    PROSE saying "this schedules nothing" must not spend the token either - that is a real
    constraint on how the section is worded, and it is the right trade.
    """
    assert "ScheduleWakeup" not in dev_manager, (
        "dev-manager must contain no wake-scheduling call; the self-wake was deleted in E-0220 "
        "because one transcript served both the pump and the human surface"
    )


def test_dev_manager_says_it_only_exists_when_opened(dev_manager: str) -> None:
    assert "schedules nothing" in dev_manager
    assert "only when a human opens it" in dev_manager


def test_dev_manager_no_longer_claims_the_moved_duties(dev_manager: str) -> None:
    """It must not still advertise a sweep or a cull it can no longer perform.

    Checked against the Overview, which is what a reader skims to learn what this loop is for.
    """
    overview = dev_manager.split("## CARDINAL RULE", 1)[0]
    assert "Registry janitor" not in overview
    assert "Unattended pump" not in overview
    assert "liveness sweep, blocker ownership" not in overview


# --------------------------------------------------------------------------- #
# steward: it absorbed the pump, and says so
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("duty", [
    "missed-wake alarm",
    "Delegated-authority answering",
    "The cull",
    "Stall alerting",
])
def test_the_steward_names_every_duty_it_absorbed(steward: str, duty: str) -> None:
    assert duty in steward, f"steward does not name the absorbed duty: {duty}"


def test_the_steward_claims_the_four_verbs_exclusively(steward: str) -> None:
    claim = "hold `reap`, `stand-down`, `repair` and `cull` EXCLUSIVELY"
    assert claim in steward
    assert "dev-manager cannot call them" in steward


def test_the_pump_is_not_in_the_rotated_domains(steward: str) -> None:
    """A liveness sweep that runs one wake in three is not a liveness sweep.

    The steward rotates its expensive domains E-G one per wake. The pump must be stated as
    every-wake, or the alarm it carries inherits the rotation's latency.
    """
    assert "EVERY wake, NEVER rotated" in steward


# --------------------------------------------------------------------------- #
# The two things most likely to be smoothed away in a later edit
# --------------------------------------------------------------------------- #
def test_the_signal_seam_is_carried_verbatim(steward: str) -> None:
    """The two sentences, quoted rather than paraphrased.

    They are the reason a quiet wake is readable at all: an empty sweep and an empty queue are
    both silent and only one of them is fine.
    """
    assert "Lower bad signals, increase good signals, never ever miss a signal." in steward
    assert "An empty queue is a call to action." in steward


def test_the_steward_is_told_to_raise_rather_than_ask(steward: str) -> None:
    """Not interactive, and not shy - both halves, because either alone is wrong.

    Without the first it becomes a second human surface; without the second it goes quiet to
    avoid overstepping, and an unrecorded observation is a lost one.
    """
    assert "Never ask the human anything" in steward
    assert "Raising is not asking" in steward
    assert "Comment, record and notify freely" in steward


def test_the_charter_exception_for_cross_row_verbs_is_explicit(steward: str) -> None:
    """The steward may not write a row it does not own - except via the verbs that exist to.

    Without this the skill forbids what it mandates: `reap`, `stand-down` and `repair` all edit
    another lane's row by definition. The old line predates the move and scoped its hazard to a
    builder's live work, which a probe-confirmed dead lane is not.
    """
    charter = steward.split("YOU MAY NOT FIX", 1)[1]
    assert "any registry row you do not own" in charter
    assert "proven dead by probe" in charter


def test_the_inherited_reap_rules_are_stated_where_its_only_caller_reads_them(steward: str) -> None:
    """The one actor who can run `reap` must read its REAL state rules, not a stale account.

    This test used to pin the opposite. It asserted `"gh#153" in steward` and `"no state filter"
    in steward`, which held this file's prose at "the verb is defective" long after gh#153 landed
    — so the only reader permitted to call `reap` was told that two of its three CORRECT return
    targets, `parked` and `review`, were bugs. A steward following that text would have read a
    healthy reap as board damage and hesitated. That is gh#197: a rule's home is the reader's
    path, and a test can hold the wrong rule there indefinitely.

    The intent survives the rewrite and is worth naming. "Carried, not laundered" meant: do not
    let a relocated verb look sound by omission. Now that the verb IS sound, the same discipline
    applies to the reason it is — the incident and the allowlist that answered it stay on the
    page, so nobody reintroduces the denylist form that reproduced the bug through a typo.

    These assert the FIXED description POSITIVELY, not merely the absence of the old one: a
    rewrite that deleted the passage outright would satisfy a negative-only test while leaving the
    steward with no state rules at all.
    """
    # Matched against a whitespace-collapsed copy. These are prose phrases in a hard-wrapped
    # markdown file, so any of them can land across a line break; asserting the raw text would
    # red this gate on a reflow that changed no meaning. That is not hypothetical — it fired on
    # the very rewrite this test was written for.
    flat = " ".join(steward.split())

    assert "provenance, not a claim" in flat, (
        "the sentence distinguishing a claim from provenance is the doctrine, not decoration"
    )
    assert "`claimed_by` and `waiting_on` cleared" in flat, (
        "what a return actually writes has to be stated, or 'returns it' is unfalsifiable"
    )
    assert "byte-identical" in flat, "terminal rows are untouched, not merely left `done`"
    assert "What `reap` may not touch" in flat, (
        "the page must point at the canonical table rather than grow a second copy of it"
    )

    # The stale account is GONE, not merely outvoted by newer text beside it. Without this, a fix
    # that appended a correct paragraph above the defective one would pass every assertion above.
    assert "no state filter" not in flat, (
        "the pre-gh#153 description of `reap` is still on the steward's page"
    )
