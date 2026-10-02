"""The harness tools a loop cannot run without: declared in the manifest, detected in code.

THREE HALVES, and none is sufficient alone.

The DECLARATION half asserts `install/manifest.md` names both tools. That file calls itself
"the source of truth for what runtimes agentflow needs", and before this epic the string
`Workflow` appeared **zero times** in it, in `README.md`, in `docs/INSTALL.md` and in
`docs/ARCHITECTURE.md` - while `ScheduleWakeup` appeared once, in a README glossary entry that
ends at "check the tool is in your session tool list" with no remedy. A stranger completing the
installer had nothing to check and nowhere to look.

The DETECTION half asserts the shared preflight helper behaves: it reports every required tool,
names the missing ones, carries a remedy, and REFUSES loudly rather than returning a value a
caller can ignore. That is behavior 4 of the spec - detection, never a replacement mechanism.

The WIRING half asserts a loop actually CALLS the helper at wake. It is the half this file
lacked for nineteen days: the module shipped, four documents described it, the detection tests
passed, and no SKILL.md invoked it - so the check a loop needs at wake was never performed and a
session missing `ScheduleWakeup` still produced a loop that ran once and looked finished. A
helper nothing calls detects nothing, which is the same silent dead install as an undeclared
tool, one layer out. The name collision is why it stayed invisible: `python -m agentflow.registry
preflight` lists open Directives before a merge, is wired, and answers a grep for "preflight"
with the wrong hit. Every assertion below pins the distinguishing form.

What no half claims, because the spec forbids asserting it in either direction: whether
these tools ship to every Claude Code user. Availability is unknown; the asymmetry is that
declaring a universal prerequisite costs one line and omitting a non-universal one costs a
silent dead install.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentflow.preflight import (
    RECOMMENDED_TOOLS,
    RECOVERY_TOOL,
    SELF_WAKE_TOOL,
    WAVE_TOOL,
    MissingHarnessTool,
    check_harness_tools,
    main,
    require_harness_tools,
)
from agentflow.registry import AGENTS

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = REPO_ROOT / "skills"
MANIFEST = REPO_ROOT / "install" / "manifest.md"
DOCS = {
    "install/manifest.md": MANIFEST,
    "docs/INSTALL.md": REPO_ROOT / "docs" / "INSTALL.md",
    "README.md": REPO_ROOT / "README.md",
    "docs/ARCHITECTURE.md": REPO_ROOT / "docs" / "ARCHITECTURE.md",
}

#: The loops that continue themselves and therefore cannot launch without `ScheduleWakeup`.
#: Derived from the product roster, never hand-listed: a hand list is how jr-dev-3 would ship
#: unwired and nothing would go red. dev-manager is the one exclusion, because E-0220 moved the
#: unattended pump to the steward and left it with no continuation to lose.
SELF_WAKING = tuple(a for a in AGENTS if a != "dev-manager")

#: THE DISTINGUISHING FORM. `python -m agentflow.registry preflight` does not contain it, which
#: is the whole point: the bare word "preflight" is already in all six files - twice over, from
#: the registry Directive verb and from the gh#315 continuation paragraph - so an assertion on
#: the word passes on a tree with no wiring at all.
PREFLIGHT_CALL = "python -m agentflow.preflight"

#: The unrelated command that shares the name. Asserted against, never for.
REGISTRY_PREFLIGHT = "python -m agentflow.registry preflight"

#: The opening of dev-manager's exemption paragraph. The seat is wired to nothing on purpose,
#: so what is asserted there is that the file SAYS so - an unexplained omission is one a later
#: reader closes by "fixing" it.
EXEMPTION_OPENER = "**No harness preflight runs here"

#: Where each loop enters its wake, so the call is pinned AT WAKE rather than anywhere in a
#: thousand-line file. A preflight that runs after the work is a preflight in name only.
#: Roster-checked below, so a loop with no anchor fails instead of being skipped silently.
WAKE_ANCHORS = {
    "lead-dev": "## 1. Orient",
    "jr-dev-1": "## Selection (REPLACES primary §2 — this is the whole point)",
    "jr-dev-2": "## Selection (REPLACES primary §2 — this is the whole point)",
    "jr-dev-3": "## Selection (REPLACES primary §2 — this is the whole point)",
    "auditor": (
        "## Step 0.5 — Registry wake read (firing contract steps 1–2, "
        "then your `## Agents` row)"
    ),
    "steward": (
        "### Validate FIRST, fail closed (firing-contract step 1, before any of the "
        "above except the harness preflight)"
    ),
}

#: Whether each seat passes `--wave`, i.e. whether it really dispatches through `Workflow`.
#: Per-seat because it is per-seat in fact: steward is explicitly excluded from lead-dev's §4
#: build machinery and stops other lanes' waves rather than running its own, while jr-dev-2
#: dispatches waves purely by inheriting §4 and never writes the word `Workflow` - so a wiring
#: pass driven by grep would give exactly those two seats the wrong flag.
NEEDS_WAVE = {
    "lead-dev": True,
    "jr-dev-1": True,
    "jr-dev-2": True,
    "jr-dev-3": True,
    "auditor": True,
    "steward": False,
}

_WS = re.compile(r"\s+")


def _skill(loop: str) -> str:
    return (SKILLS_ROOT / loop / "SKILL.md").read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Whitespace-normalised, so a pure reflow of a wrapped document is not a test failure."""
    return _WS.sub(" ", text)


def _window(text: str, needle: str, width: int, back: int = 0) -> str:
    """The span around `needle` - `back` characters before it, `width` after. Raises if absent.

    It reaches BACKWARDS because the block sits on either side of the wake anchor depending on
    the file. Four loops open their wake step with a heading and carry the numbered wake read
    underneath it, so the preflight goes between the two; auditor and steward are anchored by a
    neighbouring gate on the very heading their wake read opens with
    (`test_skill_call_sites._WAKE_READ_WINDOW`, 1600 characters from that heading to the
    `registry validate` call), so a block inserted under it would push their own step 1 out of
    that window and redden a gate about something else entirely. Their preflight is its own
    section immediately ABOVE the heading instead - still the first thing the wake does, which
    is the only property asserted here.
    """
    at = text.index(needle)
    return text[max(0, at - back) : at + width]


# ── The declaration ──────────────────────────────────────────────────────────

def test_the_manifest_names_both_harness_tools():
    """`manifest.md` is the source of truth for what agentflow needs, by its own first paragraph.

    A prerequisite absent from the file that claims to list prerequisites is not an oversight a
    reader can route around - there is nowhere else they would think to look.
    """
    text = MANIFEST.read_text(encoding="utf-8")
    for tool in (SELF_WAKE_TOOL, WAVE_TOOL):
        assert tool in text, f"{tool} is not declared in install/manifest.md"


def test_the_manifest_marks_them_REQUIRED_not_merely_mentioned():
    """Named is not declared. The manifest's own table carries a Required? column.

    Worth pinning separately because `ScheduleWakeup` was already *mentioned* in this file
    before this epic - in a paragraph about project selection - and that mention taught a
    reader nothing about whether they needed the tool.
    """
    text = MANIFEST.read_text(encoding="utf-8")
    for tool in (SELF_WAKE_TOOL, WAVE_TOOL):
        rows = [ln for ln in text.splitlines() if tool in ln and ln.lstrip().startswith("|")]
        assert rows, f"{tool} appears in manifest.md but not in a table row"
        assert any("Required" in r for r in rows), (
            f"{tool} is in a manifest table row but not marked Required: {rows}"
        )


@pytest.mark.parametrize("label", sorted(DOCS))
def test_every_stranger_facing_doc_names_the_dependency(label):
    """All four places the source assessment checked, not just the one that was easiest to fix.

    The gap was uniform - four documents, zero mentions of `Workflow` between them - so fixing
    one and calling it done would leave three doors a stranger can still walk through.
    """
    path = DOCS[label]
    assert path.is_file(), f"missing {label}"
    text = path.read_text(encoding="utf-8")
    for tool in (SELF_WAKE_TOOL, WAVE_TOOL):
        assert tool in text, f"{label} does not name {tool}"


@pytest.mark.parametrize("label", ["docs/ARCHITECTURE.md", "docs/INSTALL.md", "install/manifest.md"])
def test_the_docs_say_these_come_from_the_SESSION_not_from_the_installer(label):
    """Naming the tools is half of it; saying nothing here provisions them is the other half.

    A reader who meets `ScheduleWakeup` in a manifest of runtimes reasonably concludes the
    installer will handle it - which is the belief that produces the silent dead install this
    epic exists for. The docs have to say where the tool actually comes from.

    Added after a mutation ran that stripped exactly this claim while leaving both tool names in
    place, and nothing went red: the names were asserted, the point was not.
    """
    text = DOCS[label].read_text(encoding="utf-8")
    assert "Claude Code session" in text or "session" in text.lower(), label
    assert any(
        phrase in text
        for phrase in ("not provisioned", "cannot provision", "not installed by anything")
    ), f"{label} names the tools but not that nothing here installs them"


def test_INSTALL_carries_a_REMEDY_and_not_only_a_check():
    """The absent remedy IS the finding, so its presence is the assertion.

    README's glossary already told a reader to check the tool list and then stopped, which
    leaves the one person who finds the tool missing with nothing to do about it.
    """
    text = DOCS["docs/INSTALL.md"].read_text(encoding="utf-8").lower()
    assert "python -m agentflow.preflight" in text, "INSTALL must show how to run the check"
    assert "if it is not" in text or "if either is missing" in text, (
        "INSTALL names the check but not what to do when it fails"
    )


def test_INSTALL_Before_you_start_names_every_session_tool_the_preflight_checks():
    """The prerequisites list is what a stranger reads BEFORE deciding to install.

    The tools used to appear only in the check-and-remedy section, eleven steps into the
    walkthrough - after the machine, the credentials and the install were already committed.
    Read from the preflight's own constants, so a tool added there reds here until the list
    names it too. Says nothing about whether the tools reach every user, per the docstring above.
    """
    text = DOCS["docs/INSTALL.md"].read_text(encoding="utf-8")
    start = text.index("\n## Before you start\n")
    end = text.index("\n## ", start + 1)
    section = text[start:end]
    for tool in (SELF_WAKE_TOOL, WAVE_TOOL, *RECOMMENDED_TOOLS):
        assert f"`{tool}`" in section, f"INSTALL's Before you start does not name {tool}"


# ── The detection ────────────────────────────────────────────────────────────

def test_a_complete_tool_list_passes():
    report = check_harness_tools(["Bash", "Read", SELF_WAKE_TOOL], needs_wave=False)
    assert report.ok
    assert report.missing == ()


def test_a_missing_self_wake_tool_is_reported():
    report = check_harness_tools(["Bash", "Read"])
    assert not report.ok
    assert report.missing == (SELF_WAKE_TOOL,)


def test_the_wave_tool_is_required_only_when_the_caller_says_so():
    """Two of the six loops dispatch waves; the others must not be blocked on a tool they never call.

    A preflight that demanded every tool of every caller would be refused into irrelevance on
    the first lane that does not need it.
    """
    without = check_harness_tools([SELF_WAKE_TOOL], needs_wave=False)
    with_wave = check_harness_tools([SELF_WAKE_TOOL], needs_wave=True)
    assert without.ok
    assert with_wave.missing == (WAVE_TOOL,)


def test_a_caller_that_does_not_self_continue_is_not_asked_for_the_tool():
    """dev-manager stopped self-waking when E-0220 moved the pump to the steward."""
    report = check_harness_tools(["Bash"], needs_self_wake=False)
    assert report.ok
    assert report.required == ()


def test_require_RAISES_rather_than_returning_something_ignorable():
    """The value of this check is that it STOPS the wake.

    A caller that gets a falsy report back and carries on is exactly the silent single-run the
    module exists to prevent, so the loop-facing entry point raises.
    """
    with pytest.raises(MissingHarnessTool) as exc:
        require_harness_tools(["Bash", "Read"])
    assert SELF_WAKE_TOOL in str(exc.value)


def test_the_refusal_carries_the_REMEDY_not_just_the_tool_name():
    """Whatever surfaces the failure carries the fix with it - a bare name sends someone hunting."""
    with pytest.raises(MissingHarnessTool) as exc:
        require_harness_tools([], needs_wave=True)
    message = str(exc.value)
    assert "never wakes again" in message, "the self-wake remedy must travel with the refusal"
    assert "no fallback wave engine" in message, "the wave remedy must travel with the refusal"


def test_the_wave_remedy_does_not_offer_autodoc_a_single_agent_path():
    """README and INSTALL send autodoc users to `preflight --wave`; the single-agent advice
    that suits a build is wrong for them, so the remedy has to say so (gh#620)."""
    with pytest.raises(MissingHarnessTool) as exc:
        require_harness_tools([SELF_WAKE_TOOL], needs_wave=True)
    message = str(exc.value)
    assert "/autodoc-init" in message and "/autodoc-update" in message
    assert "no single-agent path" in message


def test_the_report_lists_the_tools_that_ARE_present_too():
    """A report that prints only failures reads identically to a report that never ran."""
    rendered = check_harness_tools([SELF_WAKE_TOOL], needs_wave=True).render()
    lines = [ln for ln in rendered.splitlines() if ln.strip()]
    assert any(ln.startswith("ok") and ln.endswith(SELF_WAKE_TOOL) for ln in lines), rendered
    assert any(ln.startswith("MISSING") and ln.endswith(WAVE_TOOL) for ln in lines), rendered


def test_matching_is_exact_and_never_fuzzy():
    """Almost-`ScheduleWakeup` is not `ScheduleWakeup`.

    Guessing here would convert a loud stop into a confident wrong answer, which is worse than
    the silence: the operator would have been told the check passed.
    """
    for near_miss in ("Schedule_Wakeup", "schedulewakeup", "ScheduleWake", "ScheduleWakeups"):
        assert not check_harness_tools([near_miss]).ok, f"{near_miss!r} was accepted"


def test_whitespace_and_duplicates_in_the_caller_list_do_not_break_it():
    """The caller is usually splitting a comma-separated string it read off its own session."""
    report = check_harness_tools([" ScheduleWakeup ", "", "Bash", "Bash"])
    assert report.ok
    assert report.available.count("Bash") == 1


# ── The CLI, which is the only form a prose skill can be told to run ─────────

def test_the_cli_exits_1_when_a_tool_is_missing(capsys):
    assert main(["--tools", "Bash,Read"]) == 1
    out = capsys.readouterr().out
    assert "MISSING" in out and SELF_WAKE_TOOL in out


def test_the_cli_exits_0_on_a_complete_list(capsys):
    assert main(["--tools", f"Bash,{SELF_WAKE_TOOL}"]) == 0
    assert "MISSING" not in capsys.readouterr().out


def test_the_cli_can_be_told_the_caller_needs_the_wave_tool(capsys):
    assert main(["--tools", SELF_WAKE_TOOL, "--wave"]) == 1
    assert WAVE_TOOL in capsys.readouterr().out


def test_the_cli_can_be_told_the_caller_does_not_self_continue(capsys):
    assert main(["--tools", "Bash", "--no-self-wake"]) == 0
    capsys.readouterr()
# -- The wiring: a helper nothing calls detects nothing ----------------------

def test_the_self_waking_roster_is_the_product_roster_minus_the_human_seat():
    """A hand-listed roster is how a seventh loop ships unwired with nothing going red.

    The set under test is derived from `registry.AGENTS`, so the only way to add a loop that
    skips the preflight is to also add it to that constant - where this assertion sees it. The
    anchor table is checked against the same set for the same reason: a loop with no anchor
    would otherwise be dropped from every co-location assertion below without a word.
    """
    assert set(SELF_WAKING) == set(AGENTS) - {"dev-manager"}
    assert set(WAKE_ANCHORS) == set(SELF_WAKING), (
        "WAKE_ANCHORS and the self-waking roster disagree - a loop with no anchor would be "
        "skipped silently by every wiring assertion in this file"
    )
    assert set(NEEDS_WAVE) == set(SELF_WAKING), (
        "NEEDS_WAVE and the self-waking roster disagree - a loop with no declared flag would "
        "never have its flag checked"
    )


@pytest.mark.parametrize("loop", SELF_WAKING)
def test_every_self_waking_loop_actually_INVOKES_the_preflight(loop: str):
    """The defect this epic exists for: the module, its docs and its tests all existed and
    nothing called it, so the check a loop needs at wake was never performed.

    Pinned on the exact invocation `python -m agentflow.preflight`, never on the word
    "preflight", which every one of these files already contains twice for unrelated reasons.
    """
    text = _skill(loop)
    assert PREFLIGHT_CALL in _flat(text), (
        f"skills/{loop}/SKILL.md never invokes {PREFLIGHT_CALL!r}; the harness preflight is "
        "unwired for this seat, so a session missing ScheduleWakeup produces a loop that runs "
        "once and reads as finished"
    )


@pytest.mark.parametrize("loop", SELF_WAKING)
def test_the_preflight_is_invoked_AT_WAKE_and_not_merely_somewhere_in_the_file(loop: str):
    """CO-LOCATION, not a file-wide substring. A check that runs after the work is not a
    preflight: by then the lane has already claimed a row it cannot continue."""
    text = _skill(loop)
    anchor = WAKE_ANCHORS[loop]
    assert anchor in text, f"{loop}: wake anchor {anchor!r} not found - the file moved"
    window = _flat(_window(text, anchor, 2500, back=2600))
    assert PREFLIGHT_CALL in window, (
        f"skills/{loop}/SKILL.md invokes the preflight somewhere, but not inside its wake step "
        f"(anchor {anchor!r}) - a preflight that runs after the work is one in name only"
    )


@pytest.mark.parametrize("loop", SELF_WAKING)
def test_each_seat_passes_the_wave_flag_its_own_dispatch_justifies(loop: str):
    """`--wave` is per-seat in fact, and both of the seats a grep would get wrong are in here.

    jr-dev-2 never writes the word `Workflow` yet dispatches waves by inheriting lead-dev's §4,
    so a grep-driven pass would leave it unguarded. steward mentions `Workflow` twice, both
    times about STOPPING another lane's wedged run, so the same pass would require a tool it
    never dispatches through and refuse a launch that would have worked.
    """
    window = _flat(_window(_skill(loop), WAKE_ANCHORS[loop], 2500, back=2600))
    assert PREFLIGHT_CALL in window, (
        f"{loop}: no preflight invocation in the wake step, so there is no flag to check - "
        "the test above says what that costs"
    )
    call = window[window.index(PREFLIGHT_CALL):]
    call = call[: call.index("```")] if "```" in call else call[:400]
    if NEEDS_WAVE[loop]:
        assert "--wave" in call, (
            f"{loop} dispatches waves but its preflight omits --wave, so a session with no "
            f"{WAVE_TOOL} passes the check and dies inside the orchestrator instead"
        )
    else:
        assert "--wave" not in call, (
            f"{loop} dispatches no waves of its own, so --wave here turns a tool it never uses "
            "into a launch blocker"
        )


@pytest.mark.parametrize("loop", SELF_WAKING)
def test_every_wired_loop_names_the_collision_that_hid_this_gap(loop: str):
    """The load-bearing sentence. Two unrelated commands are spelled "preflight" and only one
    was wired, so a reader confirming the wiring by grep got the registry Directive verb and
    stopped. A wiring that does not say which preflight it is can be un-wired the same way."""
    window = _flat(_window(_skill(loop), WAKE_ANCHORS[loop], 4000, back=2600))
    assert REGISTRY_PREFLIGHT in window, (
        f"{loop}: the preflight block never distinguishes itself from {REGISTRY_PREFLIGHT!r}, "
        "which is the collision that kept this gap invisible"
    )


@pytest.mark.parametrize("loop", SELF_WAKING)
def test_the_block_says_the_tool_list_comes_from_the_SESSION(loop: str):
    """`--tools` is unfalsifiable from Python: the module cannot see the harness, so a block
    that does not say where the list comes from invites a plausible one typed from memory."""
    window = _flat(_window(_skill(loop), WAKE_ANCHORS[loop], 4000, back=2600))
    assert "THIS session lists" in window, f"{loop}: the block never says --tools is the session's"


@pytest.mark.parametrize("loop", SELF_WAKING)
def test_the_block_separates_a_refusal_from_a_degraded_line(loop: str):
    """Exit 1 stops the wake; a `DEGRADED` line for the recovery tool does not. One word for
    both is how an operator learns to ignore the one that matters."""
    window = _flat(_window(_skill(loop), WAKE_ANCHORS[loop], 4000, back=2600))
    assert "Exit 1" in window, f"{loop}: the block never says what exit 1 means"
    assert f"`DEGRADED` line for `{RECOVERY_TOOL}` exits 0" in window, (
        f"{loop}: the block never says a degraded {RECOVERY_TOOL} line is not a stop"
    )


def test_dev_manager_is_excluded_and_SAYS_SO_rather_than_just_omitting_it():
    """An omission that reads as an oversight is one somebody 'fixes'.

    dev-manager holds zero `ScheduleWakeup`, `Workflow` and `CronCreate` instructions - verified
    here, not assumed - so the required set would be empty and the call would always exit 0.
    The file states the exemption and its cause instead.
    """
    text = _skill("dev-manager").replace("\r\n", "\n")
    paragraphs = text.split("\n\n")
    stated = [p for p in paragraphs if EXEMPTION_OPENER in p]
    assert len(stated) == 1, (
        "dev-manager does not state its preflight exemption exactly once, so the omission "
        "reads as an oversight rather than the decision it is"
    )
    assert "no continuation to lose" in stated[0] and "steward" in stated[0], (
        "dev-manager states the exemption without its cause: the unattended pump is the "
        "steward's, so this seat has no continuation to lose"
    )

    for tool in (SELF_WAKE_TOOL, WAVE_TOOL, RECOVERY_TOOL):
        assert tool not in text, (
            f"dev-manager instructs {tool} - the claim that this seat needs none of the harness "
            "tools no longer holds, so follow the file and wire the seat instead of exempting it"
        )
    assert stated[0].count(PREFLIGHT_CALL) == 1, (
        "the exemption paragraph should name the command it is exempting itself from, exactly "
        "once, so a reader searching for the wiring finds the reason this seat has none"
    )


def test_the_bare_word_preflight_would_have_passed_on_the_broken_tree():
    """A POSITIVE CONTROL for the predicate itself, because the weak version of this test is
    the likely one: `"preflight" in text` is true in every loop file for two unrelated reasons
    and was true on the tree where nothing was wired."""
    assert "preflight" in REGISTRY_PREFLIGHT
    assert PREFLIGHT_CALL not in REGISTRY_PREFLIGHT, (
        "the pinned invocation is a substring of the unrelated registry command - the "
        "assertions above would pass on a tree wired to the wrong preflight"
    )
