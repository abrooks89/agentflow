"""The pillar residue ratchet has exactly one caller, and this file is what says so.

THE DEFECT, gh#473. `pillar_residue.py` shipped complete: 29 tests, a three-way exit contract, a
recorded baseline, and four documents describing it (`CLAUDE.md`, `docs/runbooks/publish.md`, the
merge commit's own message, `ci.yml`'s sanitize job). Nothing called it. A merged commit said the
steward runs it every sweep and `skills/steward/SKILL.md` said nothing about it, so the check ran
only when a human typed it by hand. It held only because the baseline was recorded the same day -
the first page to get worse is the one nobody would have heard about, and the CI sanitize gate
cannot cover for it because that gate greps the checkout while the pillar lives under the data
root. A mechanism whose documentation outlives its caller is the same silent dead check as one
that was never written, one layer out.

WHY A DISCRIMINATING FORM, and the two halves are NOT the same claim. `"agentflow.wiki"` was
ALREADY true on the unwired tree - the projection refresh two paragraphs up carries it - so
that predicate never discriminated at all. `"residue"` was NOT: measured at 85a75a1, the
unwired `steward/SKILL.md` contained the word zero times. It is dangerous for the other
reason - it goes true the instant anyone writes a sentence ABOUT this check, which any edit
adding the block does by definition, so it is a predicate that decays into a pass without
the call ever appearing. Review demonstrated exactly that: a tree carrying eight `residue`
mentions and no subcommand still failed five of these assertions. Every assertion here pins
``python -m agentflow.wiki residue``, and
:func:`test_the_weak_predicates_would_have_passed_on_the_unwired_tree` is the positive control
that proves the weak versions would not have failed.

WHY NOT PARAMETRIZED OVER THE ROSTER, unlike `test_harness_tool_prereqs.py` whose location and
co-location idiom this file otherwise borrows: residue has exactly ONE caller by design. The
steward owns the pillar and `wiki/digest.md`; auditor is read-only, and the builders are excluded
from wiki duties outright (`test_skill_call_sites.py`, NON_SAVING_SEATS). A sweep over `AGENTS`
here would be theatre and would redden five seats that must never make this call.

WHAT IT READS. One named file, `skills/steward/SKILL.md`, by `read_text`. That is not a walk of
the working tree - it is the same shape as `test_pillar_residue.py`'s single `DEFAULT_CI_YML`
read - and nothing here touches the live pillar, runs the check, or imports the module under
test: this file asserts the WIRING, and `test_pillar_residue.py` asserts the behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STEWARD_SKILL = REPO_ROOT / "skills" / "steward" / "SKILL.md"

#: THE DISTINGUISHING FORM. Not `"residue"` (the file discusses the pillar in prose, and the word
#: is one sentence away from being true with no call behind it) and not `"agentflow.wiki"` (the
#: projection refresh in the same section already contains it). Only the subcommand invocation
#: is evidence that a sweep actually runs the ratchet.
RESIDUE_CALL = "python -m agentflow.wiki residue"

#: The neighbouring command that shares the module path. Asserted against, never for.
REFRESH_CALL = "python -m agentflow.wiki refresh --project <name>"

#: The section the call has to live in. A file-wide substring passes with the line parked
#: anywhere, including a section no sweep step reads.
DIGEST_HEADING = "## The digest — "

#: The steward's own enumeration of the wake. A duty described in prose but absent from here is
#: one a tired seat drops with nothing going red.
CHECKLIST_HEADING = "## End-of-wake checklist"

#: The one flag that writes. Recording a baseline is accepting every page it lists, so the sweep
#: must be told never to reach for it - and "never" has to be in the block, not in a runbook the
#: seat is not reading at that moment.
WRITING_FLAG = "--update-baseline"

_WS = re.compile(r"\s+")


def _steward() -> str:
    return STEWARD_SKILL.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Whitespace-normalised, so a pure reflow of a wrapped document is not a test failure."""
    return _WS.sub(" ", text)


def _window(text: str, needle: str, width: int) -> str:
    """The `width` characters following `needle`, whitespace-normalised. Raises if absent.

    Forward only, unlike the harness-preflight helper this borrows from: that block sits above
    its anchor for two seats, while this one is a sibling of the refresh call INSIDE the digest
    section and below its heading by construction. The section's own ordering requires it -
    the refresh runs first and the digest is drawn from the projection, so a residue report
    emitted above the refresh would describe a corpus the digest was not built from.
    """
    at = text.index(needle)
    return _flat(text[at : at + width])


# ── The wiring ───────────────────────────────────────────────────────────────

def test_the_steward_INVOKES_the_residue_ratchet():
    """The defect itself: the module, its baseline and four documents existed, and no SKILL.md
    made the call, so the pillar's only cover ran only when someone typed it by hand."""
    assert RESIDUE_CALL in _flat(_steward()), (
        f"skills/steward/SKILL.md never invokes {RESIDUE_CALL!r}; the pillar residue ratchet has "
        "no caller, so nothing measures the one corpus the CI sanitize gate cannot reach"
    )


def test_the_call_is_in_the_DIGEST_STEP_and_not_merely_somewhere_in_the_file():
    """CO-LOCATION, not a file-wide substring.

    The sweep's per-wake duties are the sections; a command parked in the charter or the cadence
    notes is a command no wake performs. It belongs beside the refresh because it reads the same
    projection, resolves `--project` through the same helper, and is the second half of one
    "sweep the pillar" step.
    """
    text = _steward()
    assert DIGEST_HEADING in text, f"digest heading {DIGEST_HEADING!r} not found - the file moved"
    window = _window(text, DIGEST_HEADING, 4000)
    assert RESIDUE_CALL in window, (
        "skills/steward/SKILL.md invokes the residue ratchet somewhere, but not inside the "
        f"digest section ({DIGEST_HEADING!r}) - a call outside a per-wake duty is never run"
    )


def test_the_ratchet_runs_AFTER_the_refresh_not_before_it():
    """The section says the refresh is first and the digest is drawn from the projection.

    A residue report emitted before the refresh describes a corpus the digest was not built
    from, which is a report about a different state of the world wearing this one's numbers.
    """
    flat = _flat(_steward())
    assert RESIDUE_CALL in flat and REFRESH_CALL in flat, (
        f"skills/steward/SKILL.md is missing one of the two pillar commands, so there is no "
        f"ordering to check: refresh present={REFRESH_CALL in flat}, "
        f"residue present={RESIDUE_CALL in flat}"
    )
    assert flat.index(REFRESH_CALL) < flat.index(RESIDUE_CALL), (
        "the residue call precedes the projection refresh, contradicting the section's own "
        "ordering - the digest is drawn from the projection, so residue must read the refreshed one"
    )


def test_the_call_passes_the_project_and_nothing_that_writes():
    """`--project <name>` is the whole argument list a sweep needs.

    `--wiki-root` is for tests, `--person-name` and `--ci-yml` default correctly per install, and
    `--update-baseline` must never appear in a command a loop runs unattended: recording is
    accepting every page in the report.
    """
    window = _window(_steward(), DIGEST_HEADING, 4000)
    assert RESIDUE_CALL in window, (
        "skills/steward/SKILL.md has no residue invocation in its digest section, so there are "
        "no arguments to check - the test above says what that costs"
    )
    call = window[window.index(RESIDUE_CALL) :][: len(RESIDUE_CALL) + 40]
    assert "--project <name>" in call, (
        f"the residue call does not pass --project: {call!r}. Without it the check cannot "
        "resolve a wiki root and exits 2 for a reason that has nothing to do with the pillar"
    )
    assert WRITING_FLAG not in call, (
        f"the residue call carries {WRITING_FLAG} - the one flag that WRITES. A sweep that "
        "records a baseline accepts every page in its own report, silently"
    )


# ── The prose contract: what the seat DOES with each outcome ─────────────────

def test_the_block_states_what_each_of_the_three_exit_codes_MEANS():
    """Measured off `pillar_residue.run()`, not off the issue, which assumed report-only meant
    exit 0 always. All three are reachable from a plain read-only run, and 2 is the live answer
    for a project with no baseline file - so a block that names only 0 hands the seat an
    unexplained failure every wake, which is the state operators learn to ignore."""
    window = _window(_steward(), DIGEST_HEADING, 4000)
    for code in ("Exit 0", "Exit 1", "Exit 2"):
        assert code in window, f"the residue block never says what {code} means"
    assert "CANNOT MEASURE" in window, (
        "the block never names the CANNOT MEASURE outcome, which is exit 2's actual output and "
        "the live result for a project whose baseline has not been recorded"
    )


def test_the_block_denies_that_GREEN_means_an_empty_pillar():
    """The baseline lists still-dirty pages on purpose - a ratchet red on day one gets turned
    off. A seat that reads exit 0 as "clean" will report a clean pillar to a human who is about
    to publish from it."""
    window = _window(_steward(), DIGEST_HEADING, 4000)
    assert "NOT \"the pillar is clean\"" in window or "not \"the pillar is clean\"" in window, (
        "the block never says exit 0 is not an empty pillar, so the recorded-dirty pages the "
        "baseline carries read as absent"
    )


def test_the_block_says_a_NON_ZERO_is_DISCHARGED_not_swallowed():
    """Report-only is a property of the prose, not of the exit code.

    Exit 1 and exit 2 are both reachable read-only, so the block has to say the seat routes them
    through the three-output rule rather than treating a non-zero as a broken command - and that
    it never edits a `pages/` file to clear one, because pages are creation-only here.
    """
    window = _window(_steward(), DIGEST_HEADING, 4000)
    assert "BACKLOG" in window and "issue" in window, (
        "the residue block never says a violation is discharged as an issue or a backlog entry, "
        "so the seat is left to invent a disposal for it"
    )
    assert "Do not edit the page" in window, (
        "the block never forbids clearing residue by editing the page - `wiki/pages/` is "
        "creation-only and the steward projects it rather than authoring it"
    )


def test_the_block_forbids_recording_a_baseline_from_a_sweep():
    """`--update-baseline` is the remedy a reader reaches for when they meet a 2, and it is the
    one command that accepts every dirty page in the corpus. The prohibition has to travel with
    the call, not live in a runbook the seat is not reading at that moment."""
    window = _window(_steward(), DIGEST_HEADING, 4000)
    assert f"Never pass `{WRITING_FLAG}`" in window, (
        f"the block never forbids {WRITING_FLAG} from a sweep - the obvious response to a "
        "CANNOT MEASURE is the one command that loosens the ratchet"
    )


def test_the_end_of_wake_checklist_ENUMERATES_the_duty():
    """The checklist is the steward's own enumeration and carries one line per duty.

    Described-but-not-enumerated is how a duty gets dropped on a tired wake with nothing going
    red; enumerated-but-not-called is gh#473 itself, which is why both halves are asserted and
    the call above is pinned on the invocation rather than on the checklist's wording.
    """
    text = _steward()
    assert CHECKLIST_HEADING in text, f"{CHECKLIST_HEADING!r} not found - the file moved"
    window = _window(text, CHECKLIST_HEADING, 4000)
    assert RESIDUE_CALL in window, (
        "the end-of-wake checklist has no residue line, so the duty is described in the digest "
        "section and enumerated nowhere - a wake that skips it reports a clean sweep"
    )


# ── The positive control ─────────────────────────────────────────────────────

def test_the_weak_predicates_would_have_passed_on_the_unwired_tree():
    """A test OF THE PREDICATE, because the weak version is the likely one to be written.

    The two weak predicates fail differently and only one was already true. On the tree this
    epic fixes, `steward/SKILL.md` already contained `python -m agentflow.wiki` from the
    projection refresh, so that predicate passed with no residue call anywhere. It did NOT
    contain the word "residue" - zero occurrences at 85a75a1 - but that word goes true the
    moment any prose about this check is written, so it decays into a pass rather than
    starting as one. Only the subcommand form discriminates in both directions, and this
    states that as an executable claim rather than a comment.
    """
    assert "residue" in "the pillar has its own residue ratchet"
    assert "agentflow.wiki" in REFRESH_CALL
    assert RESIDUE_CALL not in REFRESH_CALL, (
        "the pinned invocation is a substring of the neighbouring refresh command - every "
        "assertion in this file would pass on a tree wired only to the projection refresh"
    )
