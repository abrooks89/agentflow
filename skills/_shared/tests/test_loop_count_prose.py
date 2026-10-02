"""Loop counts in the loop skills' prose match the roster in code (gh#787).

`registry.AGENTS` is the fleet roster. The skills used to say "six loops" in nine places after
the roster grew to seven, because nothing tied the prose to the tuple. This test derives the
expected number from `AGENTS` and fails, naming file:line, on any spelled-out "<n> loops" in
the loop skills that disagrees.

It is deliberately narrow: only the word "loops" directly after a count is read, so subset
counts phrased as lanes ("two lanes") or as a named subset ("the builder loops", "six
self-waking lanes") are not flagged. A sentence that really means a subset should name it that
way rather than count "loops".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentflow.registry import AGENTS

SKILLS = Path(__file__).resolve().parents[2]
FILES = [
    SKILLS / name / "SKILL.md"
    for name in ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "auditor", "orchestrator")
]

WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
# \s+ spans a line wrap, so "six\nloops" is still read as one phrase.
COUNT_LOOPS = re.compile(
    r"\b(" + "|".join(WORDS) + r"|\d+)\s+loops\b",
    re.IGNORECASE,
)


def _value(token: str) -> int:
    return int(token) if token.isdigit() else WORDS[token.lower()]


def wrong_counts(text: str, label: str, expected: int) -> list[str]:
    """Every "<n> loops" in `text` whose n is not `expected`, as `label:line: phrase`."""
    out = []
    for m in COUNT_LOOPS.finditer(text):
        if _value(m.group(1)) != expected:
            line = text.count("\n", 0, m.start()) + 1
            out.append(f"{label}:{line}: {' '.join(m.group(0).split())!r}")
    return out


def test_roster_is_what_the_prose_is_pinned_to():
    # Guard the derivation itself: an empty or duplicated roster would make every count "right".
    assert len(AGENTS) == len(set(AGENTS)) > 0
    assert "dev-manager" in AGENTS


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.parent.name)
def test_loop_counts_match_roster(path: Path):
    text = path.read_text(encoding="utf-8")
    label = f"skills/{path.parent.name}/SKILL.md"
    bad = wrong_counts(text, label, len(AGENTS))
    assert not bad, (
        f"loop count disagrees with registry.AGENTS ({len(AGENTS)} loops); fix the count or "
        "name the subset without counting 'loops':\n" + "\n".join(bad)
    )


def test_negative_control_wrong_count_is_flagged():
    wrong = len(AGENTS) - 1
    word = next(w for w, n in WORDS.items() if n == wrong)
    sample = (
        f"the single\ncoordination surface all {word} loops share.\n"
        f"{word.capitalize()}\nloops appending to one store. Also {wrong} loops here.\n"
    )
    bad = wrong_counts(sample, "synthetic", len(AGENTS))
    assert bad == [
        f"synthetic:2: '{word} loops'",
        f"synthetic:3: '{word.capitalize()} loops'",
        f"synthetic:4: '{wrong} loops'",
    ]


def test_positive_control_right_count_and_subsets_pass():
    right = next(w for w, n in WORDS.items() if n == len(AGENTS))
    sample = (
        f"all {right} loops share it; {len(AGENTS)} loops in all. "
        "Resets the streak for all six self-waking lanes; two lanes; the builder loops."
    )
    assert wrong_counts(sample, "synthetic", len(AGENTS)) == []
