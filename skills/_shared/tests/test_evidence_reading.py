"""`Reading[T]`: one read of the outside world, carrying which of the five evidence states it is in.

The rule it serves is `skills/_shared/EVIDENCE.md`: empty, unavailable, truncated and stale are
different answers, and a gate passes only on complete evidence. These tests pin each constructor,
the one refusal (`require_complete`), the coverage line reports print (`describe`), and that a
reading cannot be edited after the fact into a better answer than the read gave.

This file also holds the skill-reference check: every gate-bearing skill points at
`_shared/EVIDENCE.md` with one fixed line and carries none of its rule text.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from agentflow.evidence import IncompleteEvidence, Reading, State
from agentflow.tracked_paths import tracked_files

# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------


def test_ok_is_a_complete_value():
    r = Reading.ok([1, 2], source="gh issue list", coverage="2 of 2 rows", as_of="abc123")
    assert r.state is State.VALUE
    assert r.value == [1, 2]
    assert r.source == "gh issue list"
    assert r.coverage == "2 of 2 rows"
    assert r.as_of == "abc123"
    assert r.is_complete


def test_empty_looked_completely_and_found_nothing():
    r = Reading.empty([], source="registry Queue")
    assert r.state is State.EMPTY
    assert r.value == []
    assert r.as_of is None
    assert r.is_complete


def test_empty_value_defaults_to_none():
    assert Reading.empty(source="s").value is None


def test_unavailable_carries_the_reason_and_no_value():
    r = Reading.unavailable("gh exited 1: HTTP 502", source="gh pr list")
    assert r.state is State.UNAVAILABLE
    assert r.value is None
    assert r.coverage == "gh exited 1: HTTP 502"
    assert not r.is_complete


def test_truncated_keeps_the_partial_value_and_says_what_is_missing():
    r = Reading.truncated(
        list(range(400)), "400 of more than 400 (page limit)", source="gh issue list"
    )
    assert r.state is State.TRUNCATED
    assert len(r.value) == 400
    assert r.coverage == "400 of more than 400 (page limit)"
    assert not r.is_complete


def test_stale_keeps_the_value_and_what_it_was_true_as_of():
    r = Reading.stale(
        {"verdict": "green"},
        "0123abc",
        source="gh pr checks",
        coverage="checks for the previous head",
    )
    assert r.state is State.STALE
    assert r.value == {"verdict": "green"}
    assert r.as_of == "0123abc"
    assert not r.is_complete


@pytest.mark.parametrize(
    "build",
    [
        lambda: Reading.unavailable("", source="s"),
        lambda: Reading.unavailable("   ", source="s"),
        lambda: Reading.truncated([1], "", source="s"),
        lambda: Reading.stale(1, "", source="s"),
    ],
    ids=[
        "unavailable-no-reason",
        "unavailable-blank-reason",
        "truncated-no-coverage",
        "stale-no-as_of",
    ],
)
def test_an_incomplete_state_must_say_why(build):
    # An incomplete reading with no reason is the silent failure this type exists to remove.
    with pytest.raises(ValueError):
        build()


def test_state_must_be_one_of_the_five():
    with pytest.raises(ValueError):
        Reading(state="partial", value=None, coverage="", source="s", as_of=None)
    assert [s.value for s in State] == ["value", "empty", "unavailable", "truncated", "stale"]


def test_a_state_given_as_its_string_is_normalised():
    assert (
        Reading(state="empty", value=None, coverage="", source="s", as_of=None).state is State.EMPTY
    )


@pytest.mark.parametrize(
    "value", [[1, 2], {"a": 1}, "rows", (0,)], ids=["list", "dict", "str", "tuple"]
)
def test_an_empty_reading_cannot_carry_rows(value):
    # Otherwise a migration could label a non-empty read "nothing found" and pass the gate.
    with pytest.raises(ValueError, match="empty reading cannot carry"):
        Reading.empty(value, source="gh issue list")


def test_an_empty_reading_cannot_be_given_rows_by_replace():
    r = Reading.empty([], source="s")
    with pytest.raises(ValueError, match="empty reading cannot carry"):
        dataclasses.replace(r, value=[1])


@pytest.mark.parametrize(
    "value", [None, [], {}, "", 0], ids=["none", "list", "dict", "str", "zero"]
)
def test_an_empty_reading_takes_an_empty_or_unsized_value(value):
    assert Reading.empty(value, source="s").value == value


def test_ok_with_an_empty_container_is_not_refused():
    # Refusing it would turn a generic ok(rows) into a run-time crash on a read path; the
    # docstring sends the caller to empty() instead.
    assert Reading.ok([], source="s").state is State.VALUE


# ---------------------------------------------------------------------------
# require_complete
# ---------------------------------------------------------------------------


def test_require_complete_returns_the_value_on_value_and_empty():
    assert Reading.ok(7, source="s").require_complete() == 7
    assert Reading.empty([], source="s").require_complete() == []


@pytest.mark.parametrize(
    "reading",
    [
        Reading.unavailable("timed out after 30s", source="ntfy"),
        Reading.truncated([1, 2], "2 of at least 3", source="gh"),
        Reading.stale(1, "2026-01-01T00:00:00+00:00", source="status board"),
    ],
    ids=["unavailable", "truncated", "stale"],
)
def test_require_complete_raises_on_every_incomplete_state(reading):
    with pytest.raises(IncompleteEvidence) as info:
        reading.require_complete()
    assert info.value.reading is reading
    assert reading.state.value in str(info.value)
    assert reading.source in str(info.value)


def test_require_complete_checks_state_not_binding():
    # Rule 2's binding half is the caller's: a complete reading with no as_of still passes here.
    assert Reading.ok(1, source="s").as_of is None
    assert Reading.ok(1, source="s").require_complete() == 1


def test_incomplete_evidence_is_not_a_value_error():
    # A caller catching ValueError around a parse must not swallow a refusal by accident.
    assert not issubclass(IncompleteEvidence, ValueError)


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reading, line",
    [
        (
            Reading.ok([1], source="gh pr list", coverage="1 of 1"),
            "gh pr list: read in full (1 of 1)",
        ),
        (Reading.ok([1], source="gh pr list"), "gh pr list: read in full"),
        (Reading.empty([], source="registry Queue"), "registry Queue: read in full, nothing found"),
        (
            Reading.unavailable("HTTP 502", source="gh pr list"),
            "gh pr list: unavailable (HTTP 502)",
        ),
        (
            Reading.truncated([1], "400 of more than 400", source="gh issue list"),
            "gh issue list: truncated (400 of more than 400)",
        ),
        (
            Reading.stale(1, "0123abc", source="gh pr checks", coverage="previous head"),
            "gh pr checks: stale (previous head), as of 0123abc",
        ),
        (Reading.stale(1, "0123abc", source="gh pr checks"), "gh pr checks: stale, as of 0123abc"),
        (Reading.ok(1, source="git log", as_of="feed123"), "git log: read in full, as of feed123"),
        (
            Reading.unavailable("no such file", source=""),
            "(unnamed source): unavailable (no such file)",
        ),
    ],
)
def test_describe_renders_one_coverage_line(reading, line):
    assert reading.describe() == line
    assert "\n" not in reading.describe()


def test_describe_never_renders_an_unavailable_source_as_zero_rows():
    line = Reading.unavailable("gh not on PATH", source="open PRs").describe()
    assert "unavailable" in line
    assert "0" not in line and "nothing found" not in line


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["state", "value", "coverage", "source", "as_of"])
def test_a_reading_cannot_be_edited_after_the_read(field):
    r = Reading.truncated([1], "1 of 2", source="s")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(r, field, None)


def test_readings_compare_and_hash_by_value():
    a = Reading.unavailable("x", source="s")
    assert a == Reading.unavailable("x", source="s")
    assert hash(a) == hash(Reading.unavailable("x", source="s"))
    assert a != Reading.unavailable("y", source="s")


def test_replace_makes_a_new_reading_and_still_validates():
    r = Reading.truncated([1], "1 of 2", source="s")
    with pytest.raises(ValueError):
        dataclasses.replace(r, coverage="")
    assert r.coverage == "1 of 2"


def test_reading_is_generic_and_subscriptable():
    alias = Reading[list[int]]
    assert alias.__origin__ is Reading


# ---------------------------------------------------------------------------
# Every gate-bearing skill references EVIDENCE.md and keeps no copy of it
# ---------------------------------------------------------------------------

SKILLS = Path(__file__).resolve().parents[2]
INCLUDE = SKILLS / "_shared" / "EVIDENCE.md"
REFERENCE = "_shared/EVIDENCE.md"

#: The whole pointer line. It names the include and restates no rule, so it cannot drift from it.
POINTER_LINE = (
    "**Evidence states.** Empty, unavailable, truncated and stale are different answers: "
    "`<suite_root>/skills/_shared/EVIDENCE.md`."
)

GATE_SKILLS = (
    "orchestrator",
    "lead-dev",
    "jr-dev-1",
    "jr-dev-2",
    "jr-dev-3",
    "steward",
    "auditor",
    "dev-manager",
    "solution",
)

#: Phrases only the include states. One in any `SKILL.md` means the rule was pasted into a skill
#: instead of referenced.
INCLUDE_ONLY_PHRASES = (
    "Never collapse unavailable, truncated or stale into empty",
    "bound to what it gates",
    "states what it read and what it did not",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", GATE_SKILLS)
def test_gate_skill_references_the_evidence_include(skill):
    text = _text(SKILLS / skill / "SKILL.md")
    assert REFERENCE in text, f"{skill}: no reference to {REFERENCE} at its gate or report"
    lines = [line.strip() for line in text.splitlines()]
    assert POINTER_LINE in lines or any(line.endswith(POINTER_LINE) for line in lines), (
        f"{skill}: the reference is not the fixed pointer sentence; rule text belongs in the include only"
    )


def test_evidence_include_states_five_states_and_three_rules():
    assert INCLUDE.is_file(), f"missing {INCLUDE}"
    text = " ".join(_text(INCLUDE).split())
    for state in State:
        assert f"`{state.value}`" in text, f"include does not name the {state.value!r} state"
    for phrase in INCLUDE_ONLY_PHRASES:
        assert phrase in text, f"include no longer states {phrase!r}"
    assert "agentflow.evidence" in text and "Reading" in text


def test_no_skill_carries_the_evidence_rule_text():
    # Tracked files only: the verdict is about the commit, not about whatever lies in a checkout.
    skill_files = tracked_files(
        SKILLS.parent, lambda rel: rel.startswith("skills/") and rel.endswith("/SKILL.md")
    )
    assert len(skill_files) >= len(GATE_SKILLS), "too few tracked SKILL.md files to judge"
    for skill_md in skill_files:
        text = " ".join(_text(skill_md).split()).lower()
        for phrase in INCLUDE_ONLY_PHRASES:
            assert phrase.lower() not in text, (
                f"{skill_md.relative_to(SKILLS)}: carries include text ({phrase!r}); reference {REFERENCE} instead"
            )
