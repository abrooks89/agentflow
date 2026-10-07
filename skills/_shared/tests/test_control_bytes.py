#!/usr/bin/env python3
"""No tracked file carries an escape that became a literal control byte (gh#297).

WHY THIS FILE EXISTS. An escape written as two characters can arrive as one byte -- a shell
heredoc, a sed, a JSON round-trip or an editor will do it -- and the result compiles, runs, and
matches nothing. gh#297 records the instance: a registry-verb alternation in
`test_skill_call_sites.py` was anchored on backspace bytes instead of word boundaries, so the
alternation never matched and the gate around it ran on half its predicate while reporting green.

WHY IT IS A SEPARATE MODULE AND NOT ANOTHER TEST IN THAT FILE. The scan gh#297 asked for is
tree-wide; the one it got was `Path(__file__)`, one file out of the repo's tracked corpus. That
narrowness was not a mistake so much as a consequence of where it lived: a scan hosted inside the
module it protects has no natural reason to look further, and its own assert said so out loud
("literal control characters in this module's source"). Moving it here makes the corpus the
subject rather than the host, which is the same move `test_deployed_skill_paths.py` made for
deployed paths and for the same reason.

THIS MODULE MUST NOT CARRY THE THING IT DETECTS, and saying so is not enough -- the previous
attempt at this scan carried a backspace in its own docstring, invisible to itself, because a
non-raw docstring turns the two-character escape into one byte. So every control byte named here
is built with `chr()` and never written literally, and `test_this_module_is_clean_by_its_own_gate`
holds that to the standard the rest of the tree is held to.

THE CORPUS IS THE GIT INDEX, WITH NO FALLBACK -- same rule and same reason as
`test_deployed_skill_paths.py`: a working-tree walk scans whatever happens to be lying around, so
the same commit produces different verdicts here and in CI, with the suite green on the machine
that cannot reproduce it.

Run: `python -m pytest skills/_shared/tests/test_control_bytes.py -v`
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agentflow.tracked_paths import tracked_paths

SKILLS_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = SKILLS_ROOT.parent
_GIT = "git"

# The bytes a mangled escape leaves behind, mapped to the escape that was meant. Built with
# `chr()` on purpose: writing the byte literally here is the defect, and a table of the defect
# written in the defect's own form is how the previous version of this scan carried one.
#
# Deliberately NOT the whole C0 range. These five are the ones a single-letter escape collapses
# into -- \a \b \v \f \e -- which is the shape that reads as intentional in a regex and matches
# nothing. A stray NUL or a 0x1f is a different accident with a different remedy, and widening
# this set without a case in hand would red files for reasons nobody could act on.
BANNED = {
    chr(7): "\\a",
    chr(8): "\\b",
    chr(11): "\\v",
    chr(12): "\\f",
    chr(27): "\\e",
}

# path -> the reason it is exempt. An entry means "this byte is deliberate", not "we gave up".
KNOWN_BROKEN = {
    "skills/_shared/tests/test_wave_dispatch.py": (
        "a real backspace byte inside a parametrised list of deliberately-invalid tag values, "
        "where a control character IS the test input - gh#297's own body inspected this exact "
        "line and left it alone as intentional. Exempting it here is strictly better than the "
        "status quo it replaces: the byte was invisible, and is now named with its reason. The "
        "better repair is to spell it chr(8) at the call site so it is legible in a diff, which "
        "is a one-line change in a file this epic does not declare."
    ),
}

# Must always EQUAL len(KNOWN_BROKEN); neither moves without the other in the same diff, which is
# what a reviewer sees. This does not make widening impossible - an entry plus a matching raise is
# green, and is the deliberate path. Enforcement is a human reading the raise, not the suite
# refusing it.
KNOWN_BROKEN_CEILING = 1


def _tracked_paths() -> frozenset[str]:
    """Every path in the git index at `SUITE_ROOT`, SUITE_ROOT-relative POSIX. The ONLY source.

    NO FALLBACK. If git cannot answer, this raises. A silent fall back to a tree walk would scan
    whatever the checkout happens to hold - build artefacts, a local editable install, an
    `npm ci` tree - none of which CI has, so the same commit would produce two verdicts and the
    disagreeing one would be green.
    """
    return tracked_paths(SUITE_ROOT, git=_GIT)


def _scan(root: Path, rel_paths) -> list[tuple[str, str, int]]:
    """(path, escape, line) for every banned byte under `root`, read as BYTES.

    Bytes rather than decoded text on purpose. Every banned value is below 0x80, so it can never
    appear inside a UTF-8 continuation sequence - a byte search is exact here and cannot be
    defeated by a file that fails to decode, which a text scan would either crash on or skip.
    """
    wanted = {ord(ch): esc for ch, esc in BANNED.items()}
    found: list[tuple[str, str, int]] = []
    for rel in sorted(rel_paths):
        target = root / rel
        try:
            blob = target.read_bytes()
        except (OSError, ValueError):
            continue  # a path in the index but not on disk (sparse checkout) is not this gate's
        for value, esc in wanted.items():
            start = 0
            while True:
                at = blob.find(value, start)
                if at == -1:
                    break
                found.append((rel, esc, blob.count(b"\n", 0, at) + 1))
                start = at + 1
    return found


def _all_hits() -> list[tuple[str, str, int]]:
    """Every banned byte in the whole corpus, exemptions NOT applied.

    The gate and the reach test both go through here, and that is the point rather than tidiness.
    When they each built their own corpus, narrowing the GATE to `Path(__file__)` - the exact
    regression this module was written to end - killed no test: the reach test was still asking
    the helper directly and still got the right answer about a corpus the gate no longer used. A
    mutation run found that. One source means narrowing it reds both.
    """
    return _scan(SUITE_ROOT, _tracked_paths())


def test_no_tracked_file_carries_a_literal_control_byte() -> None:
    """The gate. Every tracked file, every banned byte, minus what is recorded as deliberate."""
    offenders = [hit for hit in _all_hits() if hit[0] not in KNOWN_BROKEN]

    assert not offenders, (
        "literal control bytes in tracked source (gh#297): an escape that became a control byte "
        "compiles fine, matches nothing, and is invisible in a diff. Rebuild the escape in "
        "Python - chr(92) + the letter, never a backslash travelling through a shell - or record "
        f"the byte as deliberate in KNOWN_BROKEN with its reason. Offenders: {offenders[:5]}"
    )


def test_the_corpus_reaches_past_this_module() -> None:
    """Supersedes the single-module scan this replaces, and proves the reach rather than claiming it.

    The scan removed from `test_skill_call_sites.py` read `Path(__file__)` - one file. Deleting it
    is only safe if the file it protected is genuinely inside this corpus, and that file matters:
    p245 added a live word-boundary escape to it, so something must still hold that escape to two
    characters. Asserting the corpus is "tree-wide" would restate the intent; naming the file the
    old gate covered is the check.
    """
    mine = Path(__file__).resolve().relative_to(SUITE_ROOT).as_posix()
    corpus = _tracked_paths()

    assert "skills/_shared/tests/test_skill_call_sites.py" in corpus, (
        "the file the superseded single-module scan protected is not in this gate's corpus, so "
        "removing that scan lost coverage instead of widening it"
    )
    assert mine in corpus, (
        "this module is not in its own corpus - a gate that cannot see itself is the shape "
        "gh#297 is about"
    )
    assert len(corpus) > 100, (
        f"corpus is {len(corpus)} paths; a tree-wide gate that scans a handful of files passes "
        "for the same reason an empty one does"
    )
    # THE ONE THAT BITES. Everything above asks the corpus HELPER, and a mutation proved that is
    # not the same question: narrowing the gate itself to `Path(__file__)` left all three green.
    # This asks what the GATE actually reached, and the witness is deliberately a file that is
    # neither this module nor the one whose scan it superseded.
    reached = {hit[0] for hit in _all_hits()}
    assert reached - {mine, "skills/_shared/tests/test_skill_call_sites.py"}, (
        "the gate's own scan reached no file beyond this module and the one it superseded, so it "
        f"is single-module again under a tree-wide name. Reached: {sorted(reached)}"
    )


def test_this_module_is_clean_by_its_own_gate() -> None:
    """The previous attempt at this scan carried a backspace in its own docstring."""
    mine = Path(__file__).resolve().relative_to(SUITE_ROOT).as_posix()

    assert _scan(SUITE_ROOT, {mine}) == []


def test_the_ratchet_cannot_widen() -> None:
    """`KNOWN_BROKEN` may only shrink. Growing it quietly is how a gate becomes a formality."""
    assert len(KNOWN_BROKEN) <= KNOWN_BROKEN_CEILING, (
        f"KNOWN_BROKEN grew to {len(KNOWN_BROKEN)} against a ceiling of {KNOWN_BROKEN_CEILING}. "
        "Rebuild the escape instead. If a byte is genuinely deliberate, raise the ceiling in the "
        "same commit and say why - deliberately, not incidentally."
    )
    assert len(KNOWN_BROKEN) == KNOWN_BROKEN_CEILING, (
        f"KNOWN_BROKEN is {len(KNOWN_BROKEN)} but the ceiling reads {KNOWN_BROKEN_CEILING}. "
        "Lower the ceiling to match - headroom above the real count is a silent widening waiting "
        "to happen."
    )


@pytest.mark.parametrize("path,reason", sorted(KNOWN_BROKEN.items()), ids=lambda v: v[:24])
def test_each_known_break_states_its_reason(path: str, reason: str) -> None:
    """An exemption with no stated reason outlives its reason, because nobody can check it."""
    assert len(reason.split()) >= 10, f"{path}: KNOWN_BROKEN reason too thin: {reason!r}"


@pytest.mark.parametrize("path", sorted(KNOWN_BROKEN), ids=lambda v: v.split("/")[-1])
def test_each_known_break_is_still_a_break(path: str) -> None:
    """An exemption that outlives its cause goes on excusing whatever lands at that path next.

    If this fails, the byte was rebuilt as an escape - delete the entry and lower the ceiling
    rather than rewording it.
    """
    assert (SUITE_ROOT / path).is_file(), (
        f"{path} is exempt but no longer exists; delete the KNOWN_BROKEN entry"
    )
    assert _scan(SUITE_ROOT, {path}), (
        f"{path} is exempt but carries no banned byte any more - the exemption is stale. Delete "
        "the entry and lower KNOWN_BROKEN_CEILING in the same diff."
    )


@pytest.mark.parametrize("literal,escape", sorted((ch, esc) for ch, esc in BANNED.items()))
def test_the_gate_can_actually_fail(tmp_path: Path, literal: str, escape: str) -> None:
    """A gate nobody has watched fail is a gate nobody knows the reach of.

    One case per banned byte, because a scanner that finds four of five is exactly as green as one
    that finds all five, and the missing one is whichever nobody planted.
    """
    planted = tmp_path / "planted.py"
    planted.write_bytes(b"pattern = r'x" + literal.encode("latin-1") + b"y'\n")

    hits = _scan(tmp_path, {"planted.py"})

    assert hits == [("planted.py", escape, 1)], f"{escape} went undetected: {hits}"
