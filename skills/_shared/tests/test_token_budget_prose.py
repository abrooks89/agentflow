"""Every loop seat must say that the harness's token-budget counter is not a full context.

The harness shows the model a `total_tokens` / "tokens left" countdown. It is an advisory task
budget, not the context window: nothing enforces it, and in a self-waking loop it only resets at
compaction, so it reaches 0 while the real context has room and every tool call still succeeds. A
seat that reads 0 as "no context left" stands its lane down for hours with nothing failing. The
cure is one sentence in each seat's wake contract, so this module pins that sentence in each of
the self-waking seats (every roster agent except dev-manager, which is the human's seat).

The seat set comes from the registry roster, and each SKILL.md is located through the git index,
never a working-tree walk (see `test_corpus_from_index.py`).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SKILLS_ROOT.parent
sys.path.insert(0, str(SKILLS_ROOT / "_shared"))

from agentflow.registry import AGENTS  # noqa: E402
from agentflow.tracked_paths import tracked_files  # noqa: E402

#: Not a self-waking loop seat: the human's seat, out of this rule's scope.
EXCLUDED = ("dev-manager",)
SELF_WAKING = tuple(a for a in AGENTS if a not in EXCLUDED)

#: The rule's binding sentences. Each must appear in every seat, whitespace-normalised so a
#: rewrap does not break the pin.
KEY_SENTENCES = (
    '`total_tokens` / "tokens left" is a budget counter, never evidence of a full context: '
    "a seat at 0 runs its wake normally.",
    "High context, or any felt sense of a full context, is never a reason to skip, shorten or "
    "stand down from a wake: auto-compaction handles the window, and the state file is the handoff.",
    'Never carry a capacity verdict ("no context left", "budget exhausted") forward from a '
    "previous wake or a compaction summary.",
    "Only a real API refusal on an actual call (a context-length error) counts as exhaustion: "
    "report its exact text and stop.",
)


#: Every seat file opens its state-surfaces section with a `## State` heading. The rule must sit
#: before it, in the wake's opening contract, so it cannot drift into an appendix nobody reads.
EARLY_ANCHOR = re.compile(r"^## State", re.MULTILINE)


def _norm(text: str) -> str:
    return " ".join(text.split())


def missing_sentences(text: str) -> list[str]:
    """The key sentences absent from `text`. Empty means the seat carries the whole rule."""
    body = _norm(text)
    return [s for s in KEY_SENTENCES if _norm(s) not in body]


def _seat_files() -> dict[str, Path]:
    wanted = {f"skills/{seat}/SKILL.md": seat for seat in SELF_WAKING}
    found = tracked_files(REPO_ROOT, lambda rel: rel in wanted)
    return {wanted[p.relative_to(REPO_ROOT).as_posix()]: p for p in found}


def test_every_self_waking_seat_has_a_tracked_skill() -> None:
    files = _seat_files()
    assert len(SELF_WAKING) == 6, f"roster changed: {SELF_WAKING} - review which seats this rule must bind"
    assert sorted(files) == sorted(SELF_WAKING), (
        f"seats with no tracked SKILL.md: {sorted(set(SELF_WAKING) - set(files))}"
    )


@pytest.mark.parametrize("seat", SELF_WAKING)
def test_seat_carries_the_token_budget_rule(seat: str) -> None:
    path = _seat_files()[seat]
    gone = missing_sentences(path.read_text(encoding="utf-8"))
    assert not gone, f"{seat}/SKILL.md is missing the token-budget rule sentence(s): {gone}"


@pytest.mark.parametrize("seat", SELF_WAKING)
def test_the_rule_sits_before_the_state_section(seat: str) -> None:
    text = _seat_files()[seat].read_text(encoding="utf-8")
    anchor = EARLY_ANCHOR.search(text)
    assert anchor, f"{seat}/SKILL.md has no `## State` heading - re-anchor this check"
    head = _norm(text[: anchor.start()])
    late = [s for s in KEY_SENTENCES if _norm(s) not in head]
    assert not late, f"{seat}/SKILL.md carries the rule only after its `## State` section: {late}"


@pytest.mark.parametrize("dropped", range(len(KEY_SENTENCES)))
def test_the_check_fails_when_a_sentence_is_removed(dropped: int) -> None:
    """Mutation: strip one key sentence from a real seat's text and the predicate must see it."""
    seat = SELF_WAKING[dropped % len(SELF_WAKING)]
    text = _norm(_seat_files()[seat].read_text(encoding="utf-8"))
    sentence = _norm(KEY_SENTENCES[dropped])
    assert sentence in text, f"control: {seat} must carry the sentence before it is removed"
    mutated = text.replace(sentence, "")
    assert missing_sentences(mutated) == [KEY_SENTENCES[dropped]]
