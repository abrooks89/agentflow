"""F2's host census must name the image the CLI sessions actually run under - and keep node.

THE DEFECT, gh#482. `skills/steward/SKILL.md` closed its "Host process hygiene" domain with a
single classifier: ``Get-CimInstance Win32_Process -Filter "Name='node.exe'"`` on CommandLine +
CreationDate. The fleet's sessions are not `node.exe`. Measured on the box the fleet runs on: 80
`node.exe`, 27 `claude.exe`, and zero `node.exe` carrying `claude` in its command line - the two
populations are disjoint. A sweep following that line literally enumerated no sessions at all, and
F2's entire discharge is ``Report what you killed and what you left, with counts``, so the blind
probe reported `killed 0 / left 0`, which is exactly what a tidy host reports. A probe that is
blind every wake reads like a board that is clean.

WHY THE ASSERTIONS PIN A FILTER-TO-POPULATION PAIR, and not a name. Every cheap predicate here
passes on the broken tree or on a tree that merely discusses the fix:

* ``"Get-CimInstance" in section`` was ALREADY TRUE on the uncorrected tree - twice, at the
  classifier line and at the stranded-subagent bullet that names the classifier to say it cannot
  help. It never discriminated at all.
* ``"claude.exe" in steward`` goes true the instant anyone writes a sentence ABOUT the session
  image, which any edit adding this prose does by definition, while the `-Filter` beside it still
  says `node.exe`. It decays into a pass.
* ``"node.exe" not in section`` is WORSE THAN USELESS - it is satisfied by the destructive repair.
  Swapping one token for the other fixes the session census and silently zeroes the two bullets
  that genuinely are node processes (measured: 80 live MCP servers, plus every vitest/tinypool
  worker the moment a suite runs). That is the same defect relocated onto the only count this
  section produces non-zero.

So each test pins the `-Filter` expression TOGETHER WITH the population it is applied to, and
both directions are asserted: `claude.exe` must be the session census AND `node.exe` must survive
as the worker/MCP census. :func:`test_the_weak_predicates_would_have_passed_on_the_broken_line`
is the positive control that proves those three weak forms do not fail on the uncorrected line.

WHAT IT READS. One named file, `skills/steward/SKILL.md`, by `read_text`, sliced to the F2 section
between its own heading and G's. A file-wide substring passes with a corrected sentence parked in
a section no sweep step reads, which is the same class of bug one layer out.

WHY IT SHELLS OUT TO NOTHING. Proving a CIM filter matches would make this module Windows-only,
and this suite forbids the escape hatch that would paper over that (`test_evidence_table.py`: "a
skip is not a kill - a skipped case reads as a green file"). The live measurements are in the
prose above and in the epic record; this file asserts the WIRING, prose-level, on both CI legs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STEWARD_SKILL = REPO_ROOT / "skills" / "steward" / "SKILL.md"

#: The section the classifier has to live in. Opened and closed by the neighbouring domain
#: headings, so the sentence cannot pass while parked in the charter or the cadence notes.
F2_HEADING = "**F2. Host process hygiene.**"
G_HEADING = "**G. Issue triage.**"

#: THE TWO FILTERS, pinned as filter expressions rather than as image names. A bare `"claude.exe"`
#: is satisfied by prose; a bare `"node.exe"` was already true on the broken tree.
SESSION_FILTER = "-Filter \"Name='claude.exe'\""
NODE_FILTER = "-Filter \"Name='node.exe'\""

#: The narrowing that separates a CLI session from the desktop application sharing its image name
#: (measured: 27 `claude.exe` = 10 CLI + 17 app). Without it the census over-matches into a
#: human's open application, which is a worse failure than the one gh#482 fixed.
SESSION_NARROWING = "claude-code"

#: The populations each filter is applied to, in the section's own words.
WORKER_POPULATION = "vitest|tinypool"
BROWSER_IMAGE = "chrome.exe"

#: The field the parent-session test needs and the broken line never named.
PARENT_FIELD = "ParentProcessId"

#: The uncorrected line, verbatim, kept as the positive control's input.
BROKEN_LINE = (
    "On Windows classify with `Get-CimInstance Win32_Process -Filter \"Name='node.exe'\"` on "
    "CommandLine + CreationDate; on POSIX `ps -eo pid,lstart,args`. Report what you killed and "
    "what you left, with counts."
)

_WS = re.compile(r"\s+")


def _steward() -> str:
    return STEWARD_SKILL.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Whitespace-normalised, so a pure reflow of a wrapped document is not a test failure."""
    return _WS.sub(" ", text)


def _f2_section() -> str:
    """The F2 domain only, whitespace-normalised. Raises with a named file if either anchor moved."""
    text = _steward()
    for anchor in (F2_HEADING, G_HEADING):
        assert anchor in text, (
            f"skills/steward/SKILL.md no longer contains {anchor!r} - the domain headings moved, "
            "so this gate cannot tell which section the host classifier is in"
        )
    start = text.index(F2_HEADING)
    end = text.index(G_HEADING, start)
    return _flat(text[start:end])


def _window_after(section: str, needle: str, width: int) -> str:
    at = section.index(needle)
    return section[at : at + width]


# -- The session census: the defect itself -----------------------------------


def test_the_census_filters_on_the_CLI_SESSION_image_not_on_node():
    """gh#482: the classifier named `node.exe` and the sessions are `claude.exe`.

    Pinned as the filter expression, because the image name alone appears the moment anyone
    writes prose about this and would pass with `-Filter "Name='node.exe'"` still beside it.
    """
    section = _f2_section()
    assert SESSION_FILTER in section, (
        "skills/steward/SKILL.md, domain F2: the host census carries no "
        f"{SESSION_FILTER} - it enumerates no CLI sessions, so every parent-session test in the "
        "section divides by an empty set and the sweep discharges `killed 0 / left 0` from a "
        "probe that looked at nothing (gh#482)"
    )


def test_the_session_filter_is_NARROWED_off_the_desktop_application():
    """`claude.exe` is the CLI *and* the desktop application: measured 27 = 10 CLI + 17 app.

    A census that stops at the image name proposes killing a human's open application, so the
    narrowing has to travel with the filter rather than being left to the reader.
    """
    section = _f2_section()
    assert SESSION_FILTER in section, (
        "skills/steward/SKILL.md, domain F2: no session filter at all, so there is nothing to "
        "narrow - the test above says what that costs"
    )
    window = _window_after(section, SESSION_FILTER, 700)
    assert SESSION_NARROWING in window, (
        "skills/steward/SKILL.md, domain F2: the session filter names the image and never "
        f"narrows it with {SESSION_NARROWING!r}. The image is shared with the desktop "
        "application (measured 27 rows = 10 CLI + 17 app), so an un-narrowed census hands the "
        "sweep a human's open application as a kill candidate"
    )


# -- The node census: what the naive repair destroys --------------------------


def test_node_SURVIVES_as_the_worker_and_MCP_census():
    """The one-token swap is the trap, and this is the assertion that catches it.

    `node.exe` → `claude.exe` fixes the sessions and zeroes the two bullets that genuinely are
    node processes: measured 80 live MCP servers, and every vitest/tinypool worker whenever a
    suite runs. Same defect, relocated onto the only count F2 currently produces non-zero.
    """
    section = _f2_section()
    assert NODE_FILTER in section, (
        "skills/steward/SKILL.md, domain F2: the node census is gone. The test-runner workers "
        "and the MCP server pairs ARE node processes (measured: 80 `node.exe`, all 80 MCP "
        "servers), so a section that filters only on the session image has moved gh#482's "
        "silent zero rather than fixed it"
    )


def test_the_node_filter_is_PAIRED_with_the_population_it_classifies():
    """A surviving `node.exe` filter that no longer says what it is FOR is one edit from being
    deleted as a leftover. The pairing is the thing under test, not the token."""
    section = _f2_section()
    assert NODE_FILTER in section, (
        "skills/steward/SKILL.md, domain F2: no node filter, so there is no pairing to check"
    )
    window = _window_after(section, NODE_FILTER, 700)
    assert WORKER_POPULATION in window, (
        "skills/steward/SKILL.md, domain F2: the node filter is not paired with the population "
        f"it classifies ({WORKER_POPULATION!r} within 700 chars). An unexplained filter reads as "
        "residue from the fix and is the next thing a tidying edit removes"
    )


def test_the_browser_bullet_is_given_its_own_image():
    """The Playwright-profile bullet is `chrome.exe` and no name-filtered node or session query
    ever reached it - a second, pre-existing silent zero that a one-token repair leaves in
    place while looking complete."""
    section = _f2_section()
    assert BROWSER_IMAGE in section, (
        "skills/steward/SKILL.md, domain F2: the browser-automation bullet still has no image of "
        f"its own ({BROWSER_IMAGE!r}). No node- or session-named filter reaches it, so it is "
        "swept by nothing while being reported as swept"
    )


# -- The fields the section's own question needs ------------------------------


def test_the_census_selects_the_PARENT_pid():
    """Every bullet keys on "is the parent session dead"; `CommandLine + CreationDate` carry no
    parentage, so the broken line could not have answered its own question even with the right
    image name. A start-time cohort is an age, not a parent."""
    section = _f2_section()
    assert PARENT_FIELD in section, (
        "skills/steward/SKILL.md, domain F2: the census never selects "
        f"`{PARENT_FIELD}`, so the parent-session test every bullet depends on has no field to "
        "run on and the seat is left matching cohorts by clock time"
    )


# -- The deep half: what an empty result MEANS --------------------------------


def test_an_EMPTY_census_is_declared_a_broken_probe_and_not_a_clean_host():
    """The wrong image name was the instance; the class is that zero rows read as nothing to do.

    F2's only output is a count pair, and `0 / 0` is an unremarkable report. The seat runs ON the
    box the fleet runs on, so a zero-session census is an impossible reading - it has to be
    stated as a finding here, or the next drift of this query is as invisible as gh#482 was.
    """
    section = _f2_section()
    lowered = section.lower()
    assert "impossible reading" in lowered or "broken probe" in lowered, (
        "skills/steward/SKILL.md, domain F2: nothing says what an EMPTY census means. The "
        "section's whole discharge is a count pair, so a blind probe reports `killed 0 / left 0` "
        "and reads identically to a healthy idle fleet - the class gh#482 is an instance of"
    )
    assert "finding" in lowered, (
        "skills/steward/SKILL.md, domain F2: an empty census is described but not DISPOSED of. "
        "The seat needs to be told it is a finding, not an answer, or it files the zero under a "
        "clean sweep exactly as before"
    )


def test_the_session_count_is_CROSS_CHECKED_against_the_live_lanes():
    """A floor the seat can actually evaluate. `## Agents` is already read this wake and states
    how many lanes should exist, so a census returning fewer sessions than live lanes is either
    dead lanes or a filter narrower than this host's install shape - both findings, and neither
    visible from a count with nothing to compare it to.

    ANCHORED TO THE EMPTY-CENSUS PARAGRAPH, not to the section. A bare ``"## Agents" in section``
    PASSED on the uncorrected tree - the test-runner bullet has always mentioned the roster, for
    an unrelated purpose - so the file-wide form is one of the weak predicates this module exists
    to avoid. Measured: it was written that way first and the removal proof caught it green.
    """
    section = _f2_section()
    anchor = "impossible reading" if "impossible reading" in section else "broken probe"
    assert anchor in section, (
        "skills/steward/SKILL.md, domain F2: no empty-census paragraph to anchor the cross-check "
        "to - the test above says what that costs"
    )
    window = _window_after(section, anchor, 700)
    assert "## Agents" in window, (
        "skills/steward/SKILL.md, domain F2: the empty-census rule names no expected population "
        "to compare against. Without the `## Agents` roster beside it, the seat is told a zero is "
        "suspicious and given nothing to falsify any other number with"
    )


# -- The positive control -----------------------------------------------------


def test_the_weak_predicates_would_have_passed_on_the_broken_line():
    """A test OF THE PREDICATES, because the weak forms are the likely ones to be written.

    Three of them, failing three different ways, all evaluated against the uncorrected line:

    * `Get-CimInstance` was already present on the broken tree - it never discriminated.
    * `claude.exe` as a bare substring is satisfied by prose about the fix with the wrong filter
      still in place, which is why the pinned form carries the `-Filter` wrapper.
    * "`node.exe` is gone" is satisfied by the destructive one-token swap, which is why this
      module asserts the node filter SURVIVES rather than asserting its absence.
    """
    assert "Get-CimInstance" in BROKEN_LINE, (
        "the broken line no longer contains `Get-CimInstance`, so this control has nothing to "
        "demonstrate - the weak predicate's uselessness is the claim being made"
    )
    assert SESSION_FILTER not in BROKEN_LINE
    assert NODE_FILTER in BROKEN_LINE

    prose_only = "Note that the fleet's sessions are claude.exe, not node.exe. " + BROKEN_LINE
    assert "claude.exe" in prose_only, "the weak name predicate does not even fire here"
    assert SESSION_FILTER not in prose_only, (
        "the pinned session filter matches a tree that only MENTIONS the image while the wrong "
        "filter stands - the predicate is not discriminating"
    )

    destructive = BROKEN_LINE.replace("node.exe", "claude.exe")
    assert NODE_FILTER not in destructive, (
        "the one-token swap left the node filter intact, so "
        f"{test_node_SURVIVES_as_the_worker_and_MCP_census.__name__} would not catch it"
    )
    assert SESSION_FILTER in destructive, (
        "the destructive swap does not satisfy the session assertion, so it is not the "
        "false-green this module is defending against and the defence is mis-aimed"
    )
