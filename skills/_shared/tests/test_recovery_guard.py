"""The rate-limit recovery guard: declared, identifiable, and not swept away by its own fleet.

WHAT THIS GATES AND WHY IT IS PROSE THAT NEEDS A GATE. The guard is a `CronCreate` call written in
six SKILL.md files. Nothing executes those files, so the only thing standing between a correct
guard and a guard that quietly stops meaning anything is a test that reads the prose.

Three failures are specifically in scope, and each one is silent in the same way:

1. **The sweep eats it.** `lead-dev/SKILL.md` has instructed a `CronList` / `CronDelete` sweep of
   stale session crons since 2026-06-28, when a stale cron re-fired one done epic seven times. That
   sweep and this guard are both "a session cron this loop created", so the sweep's own predicate
   has to distinguish them - and the distinction has to be written where the SWEEPER reads it, not
   only where the guard is defined. A lane that deletes its own guard goes on waking hourly and
   looks perfectly healthy right up to the rate limit the guard existed for.
2. **The identity decays into a convention.** "The five-hourly one is the guard" is a fact a reader
   has to remember and cannot check. `--recovery-guard` in the prompt is a fact they can grep. The
   tests below pin the token, and pin that every loop STRIPS it, because an identity token that
   reaches the target parser is a phantom build target - the exact failure `--project` already had.
3. **The tool goes undeclared.** Six loops now instruct a `CronCreate` call. `preflight.py`,
   `install/manifest.md` and the stranger-facing docs are where a reader learns which tools their
   session must supply; a load-bearing tool absent from all of them is discovered by following an
   instruction that fails.

THE ASSERTIONS ARE CONTIGUOUS DIRECTIONAL PHRASES, NOT TOKENS. A first draft of this file checked
that "CronCreate", "session" and "daemon" appeared somewhere in each skill. Every one of those
survives a rewrite into the opposite rule - "a recovery cron IS a daemon, so do not schedule one" -
and a token proves only that a word survived. What is asserted here is a phrase carrying a
direction, co-located with the instruction it governs.
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
    check_harness_tools,
    main,
)
from agentflow.registry import AGENTS

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = REPO_ROOT / "skills"

#: Every loop that schedules its own next wake needs a guard, for the same reason it needs a
#: continuation: nothing outside the session will wake it. dev-manager is excluded because it
#: stopped self-waking in E-0220 - it runs only while a human sits in it, and a human who is
#: sitting in it does not need a cron to notice a rate limit.
GUARDED_LOOPS = tuple(a for a in AGENTS if a != "dev-manager")

#: Where each loop's argument split lives, so the strip rule can be checked WHERE IT BELONGS
#: rather than anywhere in the file. Derived-set hazard handled below: the roster is asserted
#: against the product constant, so a seventh loop fails here instead of being silently skipped.
STRIP_ANCHORS = {
    "lead-dev": "**STRIP FIRST.**",
    "jr-dev-1": "**Strip, then read the remainder.**",
    "jr-dev-2": "**Strip, then read the remainder.**",
    "jr-dev-3": "**Strip, then read the remainder.**",
    "auditor": "### 0.1 — Strip `--project` out of `$ARGUMENTS` FIRST",
    "steward": "**STRIP FIRST, then read the remainder.**",
}

#: EACH LOOP'S OWN FALLBACK when the backoff helper cannot answer, because there are three
#: different answers across the six loops and the previous version of this gate pinned none of
#: them. It asserted the trigger phrase "non-zero exit" and nothing else, so rewriting a loop's
#: `**On a non-zero exit use delaySeconds=3600**` into `**On a non-zero exit do whatever**` left
#: it green - proven by mutation, 2026-09-22. A fallback nobody pins is a fallback that can be
#: deleted, and a lane with no fallback ends its turn scheduling nothing at all.
FALLBACK_DELAYS = {
    "lead-dev": "use `delaySeconds=3600`",
    "jr-dev-1": "use `delaySeconds=3600`",
    "jr-dev-2": "use `delaySeconds=3600`",
    "jr-dev-3": "use `delaySeconds=3600`",
    "auditor": "use `audit.idle_wake_minutes * 60`",
    "steward": "use your ladder's interval unchanged",
}

DOCS = {
    "install/manifest.md": REPO_ROOT / "install" / "manifest.md",
    "docs/INSTALL.md": REPO_ROOT / "docs" / "INSTALL.md",
    "README.md": REPO_ROOT / "README.md",
    "docs/ARCHITECTURE.md": REPO_ROOT / "docs" / "ARCHITECTURE.md",
}

_WS = re.compile(r"\s+")

#: How far past the backoff call a loop's instructions for its three actions may run. Wide enough
#: for the longest loop's (the auditor's, whose schedule and stand-down bullets each carry the
#: reason its own dial does not govern them, and whose stand-down carries its literal row write),
#: narrow enough that a phrase elsewhere in the file cannot satisfy it.
_CALL_WINDOW = 3200


def _skill(loop: str) -> str:
    return (SKILLS_ROOT / loop / "SKILL.md").read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Whitespace-normalised, so a pure reflow of a wrapped document is not a test failure."""
    return _WS.sub(" ", text)


def _window(text: str, needle: str, width: int) -> str:
    """The `width` characters following `needle`. Raises if the needle is absent."""
    at = text.index(needle)
    return text[at : at + width]


# ── the roster ───────────────────────────────────────────────────────────────

def test_the_guarded_roster_is_the_product_roster_minus_the_human_seat():
    """A hand-listed roster is how a seventh loop ships with no guard and nothing goes red.

    The set under test is derived from `registry.AGENTS`, so the only way to add a loop without
    a guard is to also add it to that constant - where this assertion sees it.
    """
    assert set(GUARDED_LOOPS) == set(AGENTS) - {"dev-manager"}
    assert set(STRIP_ANCHORS) == set(GUARDED_LOOPS), (
        "STRIP_ANCHORS and the loop roster disagree - a loop with no anchor would be skipped "
        "silently by every co-location assertion in this file"
    )
    assert set(FALLBACK_DELAYS) == set(GUARDED_LOOPS), (
        "FALLBACK_DELAYS and the loop roster disagree - a loop whose fallback is not listed "
        "would have its fallback unpinned, which is exactly the hole this table closed"
    )


# ── the guard itself, in every loop ──────────────────────────────────────────

@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_every_self_waking_loop_schedules_the_guard(loop: str):
    text = _skill(loop)
    assert "CronCreate(" in text, f"{loop}/SKILL.md schedules no recovery guard at all"
    assert "--recovery-guard" in text, f"{loop}/SKILL.md has no guard identity token"


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_the_guard_is_a_SECOND_leg_and_says_so(loop: str):
    """The one sentence that keeps this from being read as a replacement for `ScheduleWakeup`.

    A reader who takes the cron as the new continuation deletes the hourly wake and ships a loop
    whose entire pacing dies with the session - and whose recovery leg, being the only leg, has
    nothing to recover.
    """
    flat = _flat(_skill(loop))
    assert "a SECOND wake leg, not a replacement" in flat, loop
    assert "Leg one is every `ScheduleWakeup`" in flat, loop


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_each_loop_states_WHY_the_loop_cannot_recover_from_inside_itself(loop: str):
    """The mechanism, not the symptom. Without this sentence the guard reads as belt-and-braces.

    It is not: a firing refused before its first tool call never reaches its own `ScheduleWakeup`,
    so the loop has no pending wake and no row to be late on. That is why an in-session leg
    scheduled EARLIER is the only thing that can help, and why nothing created on the failure path
    could have.
    """
    flat = _flat(_skill(loop))
    assert "before its first tool call" in flat, loop
    assert "never reaches its own `ScheduleWakeup`" in flat, loop


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_each_loop_states_the_guard_fires_on_a_CLOCK_and_never_on_a_failure(loop: str):
    """The item-2 ruling, 2026-09-22, and the half a reader is most likely to widen.

    A recovery cron created in response to a crash wakes into the same crash. The ruling permits
    auto-resume on a rate-limit / quota clock ONLY, and the phrase asserted here carries the
    exclusion rather than merely the permission.
    """
    flat = _flat(_skill(loop))
    assert "never a crash, an error, a `halted` row, or an unknown-cause stop" in flat, loop
    assert "rate-limit / quota window and nothing else" in flat, loop


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_each_loop_states_what_the_guard_CANNOT_do(loop: str):
    """Four limits, each of which someone will otherwise build on.

    Asserted as separate phrases rather than as one "limitations" heading, because the heading
    survives the deletion of any three of them.
    """
    flat = _flat(_skill(loop))
    assert "writes nothing to disk and the job dies with this Claude Code session" in flat, loop
    assert "Auto-expires after 7 days" in flat, loop
    assert "Reports nothing during the outage" in flat, loop
    assert "`durable: true` has no effect" in flat, loop


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_each_loop_states_the_guard_does_NOT_spend_the_single_daemon_budget(loop: str):
    """The budget is one and it was spent on `wake_watch` (2026-09-22).

    This assertion exists because the guard LOOKS like a daemon - it is a cron, it fires on a
    clock, it survives the firing that created it. It is not one, and the distinguishing property
    has to be written down or the next reader either refuses to ship it or ships a real daemon
    beside it believing the budget was already broken.
    """
    flat = _flat(_skill(loop))
    assert "does NOT spend the single-daemon budget" in flat, loop
    assert "in-session self-scheduling" in flat, loop
    assert "starts no process, writes nothing to disk, survives no exit" in flat, loop


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_the_guard_prompt_carries_no_target(loop: str):
    """The identity and the sweep predicate both rest on this, so it is pinned separately.

    "Prompt names a done target" and "prompt contains `--recovery-guard`" are only disjoint
    because a guard prompt has no target. Let a target into the guard prompt and the sweep becomes
    correct to delete it.
    """
    flat = _flat(_skill(loop))
    assert "then append the literal" in flat, loop
    assert "`--recovery-guard` token" in flat, loop
    assert ("and no target" in flat) or ("No target" in flat) or (
        "and NO target" in flat
    ), f"{loop} does not say the guard prompt carries no target"


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_every_loop_STRIPS_the_identity_token_where_it_strips_the_project_flag(loop: str):
    """CO-LOCATION, not a file-wide substring.

    `--recovery-guard` reaching the target parser is a phantom build target - the identical
    failure `--project` had, which cost §2 a hunt for an epic named `--project`. The rule is only
    worth anything at the argument split, so that is where it is asserted: within the window
    following each loop's own strip instruction.
    """
    text = _skill(loop)
    anchor = STRIP_ANCHORS[loop]
    assert anchor in text, f"{loop}: strip anchor {anchor!r} not found - the file moved"
    window = _flat(_window(text, anchor, 2500))
    assert "--recovery-guard" in window, (
        f"{loop}/SKILL.md never strips --recovery-guard at its argument split; the token would "
        "survive into <target> and be hunted as a work target"
    )


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_every_loop_reasserts_the_guard_each_wake_rather_than_trusting_the_launch_one(loop: str):
    """A recurring cron auto-expires after 7 days. A guard created once at launch is a guard the
    fleet loses in a week - and loses silently, since nothing reports its absence."""
    flat = _flat(_skill(loop))
    assert "Run `CronList`" in flat, loop
    assert "More than one -> `CronDelete`" in flat or "more than one → `CronDelete`" in flat, loop


# ── the conflict with lead-dev's stale-cron sweep ────────────────────────────

def _stale_guard_block() -> str:
    """lead-dev's §1.3 stale-wake guard, sliced at the next section heading.

    Sliced rather than spanned: a span from the guard to some later landmark would let a new
    subsection hold the exemption while §1.3 itself went back to an unqualified delete.
    """
    text = _skill("lead-dev")
    start = text.index("**Stale-wake guard (LOAD-BEARING).**")
    nl = chr(10)
    end = text.index(nl + "## ", start)
    return text[start:end]


def test_the_stale_cron_sweep_names_the_exemption_WHERE_THE_SWEEPER_READS_IT():
    """THE CONFLICT THIS EPIC HAD TO RESOLVE.

    lead-dev has instructed `CronList` + `CronDelete` of stale session crons since 2026-06-28. The
    recovery guard IS a session cron this loop created. Stating the exemption only in the guard's
    own section would leave the sweeper's own instruction unqualified - and a sweeper following an
    unqualified instruction is behaving correctly while destroying the fleet's only rate-limit
    recovery leg.
    """
    block = _flat(_stale_guard_block())
    assert "CronDelete" in block, "the sweep moved out of §1.3 - re-point this test"
    assert "--recovery-guard" in block, (
        "lead-dev's stale-cron sweep does not mention the recovery guard, so a sweeper reading "
        "only §1.3 has an unqualified instruction to delete every session cron it finds"
    )
    assert "**LEAVE it**" in block, "the exemption is mentioned but carries no direction"
    assert "**DELETE it**" in block, "the sweep's own direction went missing with the rewrite"


def test_the_sweep_predicate_is_a_PROMPT_test_and_not_a_schedule_or_a_memory():
    """"The five-hourly one is the guard" is a convention; a token in the prompt is a check.

    The requirement was explicitly that a reader can CHECK the identity rather than remember it,
    so the predicate has to be stated as something read off a `CronList` row.
    """
    block = _flat(_stale_guard_block())
    assert "its prompt carries" in block, block[:400]
    assert "carries no target by construction" in block, (
        "the block does not say WHY the two predicates cannot both match one row, so a reader "
        "with a cron matching neither description has nothing to reason from"
    )


def test_the_sweep_tables_two_columns_say_which_one_is_which():
    """D5, and the judgement call it asked for, written down rather than left implicit.

    Adversarial mutation found two deletions in this block that no test noticed: the bold lead-in
    above the table, and the table's own header row. The verdict cells (`DELETE it` / `LEAVE it`)
    were pinned, so the SUBSTANCE was safe - but a table whose header row is gone renders with
    three unlabelled columns, and two cells reading DELETE and LEAVE with nothing saying which
    cron each belongs to is worse than no table.

    THE CALL: pin the captions, not the punctuation. The captions carry meaning - they are the
    subject of every verdict in the block - so losing one is a real defect. The separator row,
    the pipe alignment and the leading empty cell are rendering, and a gate on markdown
    punctuation fires on a reflow that changed nothing, which is how a suite teaches its readers
    to ignore it. The lead-in is pinned too: it is the sentence that says the predicate is READ
    rather than remembered, which is the whole claim this section makes.
    """
    block = _flat(_stale_guard_block())
    assert "This sweep must NOT delete the" in block, (
        "the bold lead-in above the table is gone; the table then states two verdicts with "
        "nothing above it saying the sweep must not eat the guard"
    )
    assert "told apart by reading the prompt" in block
    assert "a stale epic-carrying cron" in block, (
        "the table's header row lost the column that names what gets DELETEd"
    )
    assert block.count("the §0.6 recovery guard") >= 2, (
        "the table's header row lost the column that names what gets LEFT. Counted rather than "
        "matched: the same phrase appears in the bold lead-in two lines above, so a bare "
        "substring is satisfied by the lead-in alone and the LEAVE column can go unlabelled "
        "while this test stays green - which is the exact failure the docstring calls worse "
        "than no table"
    )


def test_no_instruction_to_delete_a_cron_stands_without_the_exemption_beside_it():
    """The whole-file sweep, because §1.3 is not the only place a delete could be written.

    A future `CronDelete` added anywhere in this file - a cleanup step, a migration note - would
    reintroduce exactly the conflict this epic closed, in a place no test was watching.
    """
    text = _skill("lead-dev")
    sites = [m.start() for m in re.finditer("CronDelete", text)]
    assert sites, "no CronDelete in lead-dev - the sweep moved and this gate now gates nothing"
    for at in sites:
        window = _flat(text[max(0, at - 700) : at + 700])
        assert "--recovery-guard" in window, (
            "a CronDelete instruction in lead-dev/SKILL.md has no recovery-guard exemption "
            f"within 700 characters: ...{_flat(text[at - 120 : at + 200])}..."
        )


def test_lead_devs_exit_checklist_carries_the_guard():
    """The checklist is what a firing actually reads at the end. A duty absent from it is a duty
    performed on the wakes someone happened to remember."""
    flat = _flat(_skill("lead-dev"))
    assert "Recovery guard present and singular (§0.6)" in flat
    assert "It is never swept as stale" in flat


# ── the declaration: CronCreate as a harness dependency ──────────────────────

def test_the_recovery_tool_is_declared_in_code_at_the_RECOMMENDED_tier():
    """The conclusion this epic reached, pinned so it cannot drift either way.

    NOT Required: a session without `CronCreate` runs, wakes and builds exactly as before, and a
    check that refused such a session would be a refusal operators learn to bypass. NOT undeclared
    either: six SKILL.md files instruct a call to it. The third answer is a reported tier that
    never touches the exit code, and both halves of that are asserted here.
    """
    assert RECOVERY_TOOL == "CronCreate"
    assert RECOMMENDED_TOOLS == (RECOVERY_TOOL,)
    report = check_harness_tools(["Bash", SELF_WAKE_TOOL])
    assert report.ok, "a session without CronCreate must still be allowed to launch"
    assert report.missing == (), "CronCreate must never be reported as MISSING"
    assert report.degraded == (RECOVERY_TOOL,), "CronCreate absent must be reported as DEGRADED"
    assert RECOVERY_TOOL not in report.required


def test_the_rendered_report_uses_DEGRADED_and_never_MISSING_for_the_recovery_tool():
    rendered = check_harness_tools(["Bash", SELF_WAKE_TOOL]).render()
    lines = [ln for ln in rendered.splitlines() if RECOVERY_TOOL in ln and not ln.startswith(RECOVERY_TOOL)]
    assert lines, rendered
    assert any(ln.startswith("DEGRADED") for ln in lines), rendered
    assert not any(ln.startswith("MISSING") for ln in lines), (
        "CronCreate rendered as MISSING - that word is what stops a launch, and this tool does not"
    )


def test_the_report_carries_the_recovery_REMEDY_and_says_the_loop_still_runs_without_it():
    """A degraded line nobody can act on is a line that trains the reader to skip the channel."""
    rendered = check_harness_tools(["Bash", SELF_WAKE_TOOL]).render()
    assert "still runs, wakes and builds exactly as before" in rendered
    assert "unattended recovery" in rendered


def test_a_present_recovery_tool_is_reported_as_present():
    """A report that goes quiet when everything is fine reads like a report that never ran."""
    rendered = check_harness_tools(["Bash", SELF_WAKE_TOOL, RECOVERY_TOOL]).render()
    assert any(
        ln.startswith("ok") and ln.endswith(RECOVERY_TOOL) for ln in rendered.splitlines()
    ), rendered
    assert "DEGRADED" not in rendered


def test_the_cli_still_exits_0_when_only_the_recovery_tool_is_absent(capsys):
    """THE assertion that keeps the tier from becoming a requirement by accident."""
    assert main(["--tools", f"Bash,{SELF_WAKE_TOOL}"]) == 0
    out = capsys.readouterr().out
    assert "DEGRADED" in out and RECOVERY_TOOL in out
    assert "MISSING" not in out


def test_the_recovery_line_can_be_silenced_without_touching_the_exit_code(capsys):
    assert main(["--tools", f"Bash,{SELF_WAKE_TOOL}", "--no-recovery"]) == 0
    assert RECOVERY_TOOL not in capsys.readouterr().out


def test_a_missing_required_tool_still_exits_1_with_the_recovery_tier_present(capsys):
    """The two tiers must not interfere: a degraded line beside a missing one changes nothing."""
    assert main(["--tools", "Bash"]) == 1
    out = capsys.readouterr().out
    assert "MISSING" in out and SELF_WAKE_TOOL in out
    assert "DEGRADED" in out and RECOVERY_TOOL in out


@pytest.mark.parametrize("label", sorted(DOCS))
def test_every_stranger_facing_doc_names_the_recovery_tool(label: str):
    """The same four doors the `ScheduleWakeup` / `Workflow` declaration had to cover.

    Fixing one and calling it done leaves three places a reader can meet the fleet without ever
    learning that six loops call a tool their session may not have.
    """
    text = DOCS[label].read_text(encoding="utf-8")
    assert RECOVERY_TOOL in text, f"{label} does not name {RECOVERY_TOOL}"


@pytest.mark.parametrize("label", sorted(DOCS))
def test_every_doc_says_a_session_without_it_still_WORKS(label: str):
    """Naming it is half. A reader who meets `CronCreate` beside `ScheduleWakeup` reasonably
    concludes it is equally required and goes hunting for a tool they may not be able to get.

    The tier is the claim, so the tier is the assertion - and it is a directional phrase, because
    "recommended" as a bare word survives a rewrite that then treats it as mandatory anyway.
    """
    flat = _flat(DOCS[label].read_text(encoding="utf-8"))
    assert "recommended" in flat.lower(), label
    assert any(
        phrase in flat
        for phrase in (
            "still runs, wakes and builds",
            "still run, wake and build",
            "loses only unattended recovery",
            "loses unattended recovery",
        )
    ), f"{label} names {RECOVERY_TOOL} but never says a session without it still functions"


def test_the_manifest_row_marks_it_Recommended_and_NOT_Required():
    """The manifest's table is where the Required? column is answered, so the answer is pinned
    there specifically - a paragraph elsewhere saying "recommended" does not reach a reader
    scanning the table."""
    text = DOCS["install/manifest.md"].read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if RECOVERY_TOOL in ln and ln.lstrip().startswith("|")]
    assert rows, f"{RECOVERY_TOOL} is in manifest.md but not in a table row"
    assert any("Recommended, not Required" in r for r in rows), rows


def test_the_docs_say_the_recovery_tool_is_not_a_daemon():
    """The single-daemon budget is one and it is spent. A reader who reads `CronCreate` as a second
    daemon either refuses to ship the guard or concludes the rule is already broken."""
    for label in ("install/manifest.md", "docs/INSTALL.md", "docs/ARCHITECTURE.md"):
        flat = _flat(DOCS[label].read_text(encoding="utf-8"))
        assert "single-daemon" in flat or "one permitted daemon" in flat, label
        assert any(
            phrase in flat
            for phrase in (
                "is not a daemon",
                "not a daemon",
                "Nothing here is the suite's one permitted daemon",
                "does not touch the single-daemon budget",
                "nothing here adds to it",
            )
        ), f"{label} names {RECOVERY_TOOL} without placing it against the daemon budget"


def test_the_required_tools_are_untouched_by_this_epic():
    """A guard against fixing the tier by demoting something that really is required."""
    report = check_harness_tools([], needs_wave=True)
    assert report.required == (SELF_WAKE_TOOL, WAVE_TOOL)
    assert report.missing == (SELF_WAKE_TOOL, WAVE_TOOL)
    assert not report.ok


# ── the backoff wiring ───────────────────────────────────────────────────────

@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_every_loop_asks_the_backoff_helper_for_its_idle_delay(loop: str):
    """Requirement: the lane ASKS rather than hard-codes.

    Pinned per loop and with the loop's own `--actor`, because the observation log is shared and
    keyed by actor - a lane that passes someone else's name writes a row attributing its own
    silence to another seat.
    """
    flat = _flat(_skill(loop))
    call = f"python -m agentflow.backoff --actor {loop} --project <name>"
    assert call in flat, f"{loop}/SKILL.md never asks the backoff helper: expected {call!r}"


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_every_loop_states_the_fallback_when_the_helper_cannot_answer(loop: str):
    """CO-LOCATED with the call, AND PINNING THE DELAY rather than the trigger.

    A helper that exits non-zero must not leave a lane with no delay at all - that is a lane that
    ends the turn without scheduling anything, which is the silent stop this whole fleet is built
    to avoid.

    The first version of this test asserted only `"non-zero exit"`, the words that introduce the
    rule, and so it held nothing: mutating `**On a non-zero exit use delaySeconds=3600**` into
    `**On a non-zero exit do whatever**` left it green. What the docstring claimed it held is the
    FALLBACK, so the fallback is what it asserts - each loop's own, from `FALLBACK_DELAYS`,
    because the six loops have three different ones.
    """
    text = _skill(loop)
    call = f"python -m agentflow.backoff --actor {loop} --project <name>"
    window = _flat(_window(text, call, _CALL_WINDOW))
    assert "non-zero exit" in window, (
        f"{loop}/SKILL.md calls the backoff helper without saying what to do when it fails"
    )
    fallback = FALLBACK_DELAYS[loop]
    assert fallback in window, (
        f"{loop}/SKILL.md says there is a fallback but never names it: expected {fallback!r} "
        "within reach of the call. A lane reading 'on a non-zero exit' with no delay beside it "
        "has an instruction it cannot follow"
    )


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_every_loop_reads_an_ACTION_and_not_a_bare_number(loop: str):
    """THE DEFECT THE SECOND PASS FOUND, and the shape that fixes it.

    Measured: `{plan_wake(s).honourable_s for s in range(0, 2000)} == {3600}`. `ScheduleWakeup`
    is clamped into [60, 3600] and the base cadence IS 3600, so a helper that can only answer
    with a delay can only ever answer with the hour it replaced - while each lane now paid for a
    marker observation per wake. The backoff added cost and reduced nothing.

    So the answer is a verb, and there are three of them. A loop that reads only `delaySeconds=`
    cannot act on the floor at all, which puts it straight back in the inert shape.
    """
    text = _skill(loop)
    call = f"python -m agentflow.backoff --actor {loop} --project <name>"
    window = _flat(_window(text, call, _CALL_WINDOW))
    for verb in ("action=schedule", "action=stand_down", "action=uncertain"):
        assert verb in window, f"{loop}/SKILL.md never tells the lane what to do on {verb}"


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_every_loop_states_plainly_what_a_FLOORED_lane_DOES(loop: str):
    """"Schedule nothing" is an instruction a lane will not follow unless it is told why.

    The why is arithmetic: the harness clamps at an hour, so the five-hour floor is unreachable
    by scheduling and reachable only by declining to schedule. A loop that carries the verb but
    not the reason has a reader who will "fix" it back to an hourly wake the first time it looks
    wrong.
    """
    flat = _flat(_skill(loop))
    assert "no lane can sleep longer than an hour by scheduling" in flat, loop
    assert "call no `ScheduleWakeup` at all this wake" in flat, loop


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_every_loop_names_what_WAKES_a_lane_that_stopped_scheduling(loop: str):
    """THE WORST FAILURE THIS FILE COULD SHIP is a lane that stops self-scheduling without
    knowing what wakes it. The whole chain is named where the lane reads it, in order.

    Leg two is the guard, and it is not a backstop at the floor - it IS the wake. Leg three is
    the OS-scheduled watchdog, which cannot wake anything and is asserted with the phrase that
    says so, because a reader who takes `wake_watch` for a relauncher has a fourth wrong belief
    about their own recovery path.
    """
    flat = _flat(_skill(loop))
    assert "the standing five-hour `--recovery-guard` cron is the wake" in flat, loop
    assert "it cannot wake you, but it *reports* a row past its `eta`" in flat, loop
    # Windowed, not file-wide: every loop names this module in its own single-daemon paragraph,
    # so a file-wide substring is satisfied whatever leg 3 says - renaming the module here used
    # to pass. The window is the sentence that makes leg 3 a BACKSTOP rather than a mention.
    leg3 = flat.find("it cannot wake you, but it *reports* a row past its `eta`")
    assert "python -m agentflow.wake_watch" in flat[max(0, leg3 - 400):leg3], loop


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_no_loop_may_stand_down_without_a_standing_guard(loop: str):
    """The interlock. A stand-down trusts a cron that is session-scoped, dies with its process
    and auto-expires after seven days; standing down with nothing standing converts the floor
    into the silent stop it was built to avoid.

    Pinned with the checklist half too (`present THIS firing`), because an exemption written at
    the call site while the checklist still demands a continuation is a loop whose two halves
    disagree - and a lane reading the checklist last does the wrong one.
    """
    flat = _flat(_skill(loop))
    assert "Never stand down on a wake where `CronList` did not show the guard present" in flat, loop
    assert "present THIS firing" in flat, (
        f"{loop}/SKILL.md's end-of-firing checklist still demands a continuation unconditionally, "
        "so its checklist contradicts its own stand-down instruction"
    )


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_no_loop_still_promises_the_marker_reads_merged_PRs(loop: str):
    """The PR leg was a `gh pr list` per lane per wake, added to the exact spend this epic
    reduces, for a fact the local refs read already carries: a merge moves master.

    Asserted inside the call's own window rather than file-wide, because `merged PRs` is a
    perfectly ordinary phrase elsewhere in these files - the steward's claim-selection lesson
    uses it - and a file-wide ban would fire on prose that has nothing to do with the marker.
    """
    text = _skill(loop)
    call = f"python -m agentflow.backoff --actor {loop} --project <name>"
    window = _flat(_window(text, call, _CALL_WINDOW))
    assert "both local reads, no network call" in window, loop
    assert "merged PRs" not in window, (
        f"{loop}/SKILL.md still tells the lane the marker reads merged PRs; that leg was a "
        "network round-trip per lane per wake and it was removed"
    )


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_every_loop_records_the_HONOURABLE_number_not_the_intended_one(loop: str):
    """gh#82's class, reached from a new direction.

    `ScheduleWakeup` clamps into [60, 3600] and says nothing when it does, so a lane that writes
    the backoff's `intended` value into its state file records an interval that never happened -
    the machine stays right and only the writing goes wrong, which is the failure this fleet is
    least able to notice.
    """
    flat = _flat(_skill(loop))
    assert "clamps `delaySeconds`" in flat, loop
    assert any(
        phrase in flat
        for phrase in (
            "Record the `honourable` number",
            "record `honourable`, never `intended`",
            "Record the number you actually scheduled",
        )
    ), f"{loop} does not say which of the two numbers goes in its record"


def test_the_two_non_builders_keep_their_own_cadence_language():
    """The auditor and steward are not builders and must not inherit an empty-LANE rule.

    Each keeps its own dial and the backoff may only move it in that dial's safe direction. The
    STEWARD takes the LARGER of the helper's answer and its own ladder: a finding is its own
    direct measurement and outranks a fleet-wide quiet, so the ladder may only be lengthened.
    The AUDITOR takes the SMALLER of the helper's answer and its configured idle window, because
    that window is a CEILING on how long an empty pass may wait - an operator who set thirty
    minutes asked to hear about newly-done epics within half an hour.
    """
    aud = _flat(_skill("auditor"))
    assert "take the SMALLER of" in aud and "audit.idle_wake_minutes" in aud
    stew = _flat(_skill("steward"))
    assert "take the LARGER of" in stew and "clean_sweeps" in stew


def test_the_auditors_cadence_sentence_does_not_contradict_its_own_operator():
    """gh-class defect, found 2026-09-22: the instruction said both things at once.

    It read "Take the SMALLER of <n> and audit.idle_wake_minutes * 60 ... the backoff may only
    lengthen the wait, never shorten it". `min()` can only ever shorten. A reader following the
    sentence has to pick a half, and whichever they pick the other half is a live instruction
    saying they were wrong.

    The direction is asserted BOTH ways - the corrected phrase present and the contradictory one
    absent - because a rewrite that adds the right sentence without deleting the wrong one leaves
    the contradiction exactly where it was.
    """
    aud = _flat(_skill("auditor"))
    assert "ceiling on how long an empty pass may wait" in aud, (
        "the auditor no longer says which way its configured window binds"
    )
    assert "never stretch it past what the operator asked for" in aud
    assert "may only lengthen the wait, never shorten it" not in aud, (
        "the cadence instruction contradicts itself again: it takes the SMALLER of two numbers "
        "and then claims the backoff can only lengthen the wait"
    )


# ── the positive control ─────────────────────────────────────────────────────

def test_these_phrase_assertions_can_actually_fail():
    """Every assertion above is a substring test over a file that is currently correct, which is
    the shape of a gate that passes because it found nothing. This one shows the predicates
    discriminate: the same checks run against text that has been rewritten into its opposite.
    """
    inverted = _flat(
        "The recovery cron REPLACES the hourly ScheduleWakeup. It is a daemon, so the "
        "single-daemon budget is already spent twice. Schedule one whenever a firing crashes."
    )
    assert "a SECOND wake leg, not a replacement" not in inverted
    assert "does NOT spend the single-daemon budget" not in inverted
    assert "never a crash, an error, a `halted` row, or an unknown-cause stop" not in inverted

    # And the sweep predicate, with the exemption removed but the sweep intact.
    unqualified = _flat("run `CronList` and delete (`CronDelete`) any session cron you find")
    assert "CronDelete" in unqualified
    assert "--recovery-guard" not in unqualified


# ── the stand-down's declared wake, and the guard's schedule it is computed from ──

@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_every_loop_guard_runs_on_the_schedule_the_backoff_computes_its_eta_from(loop: str):
    """A floored lane declares `backoff.stand_down_eta` as its row's eta, and that function
    reads `GUARD_CRON`. A guard moved in prose alone would have every floored lane declare a
    wake that is not coming - an early page, or a late one that hides a dead lane."""
    from agentflow import backoff

    crons = re.findall(r'CronCreate\(cron="([^"]+)"', _skill(loop))
    assert crons == [backoff.GUARD_CRON], (loop, crons)


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_every_loop_declares_the_stand_down_eta_the_answer_line_carries(loop: str):
    """The stand-down half of the answer is two instructions, not one: schedule nothing, AND
    re-declare the row. Without the second the row keeps the eta written before asking, and the
    missed-wake alarm pages a lane that is sleeping correctly, then `read --stale` lists it."""
    flat = _flat(_skill(loop))
    assert "delaySeconds=<seconds|none>[ eta=<ISO>]" in flat, loop
    bullet = _window(flat, "- **`action=stand_down`** — **schedule NOTHING this wake.**", 1100)
    # Ordered after the guard check: a lane that rewrites first and then finds no guard would keep
    # a five-hour eta while it actually wakes in one.
    assert "Only once `CronList` has shown the guard present" in bullet, loop
    assert "rewrite your `## Agents` row with `--state idle`" in bullet, loop
    assert "with `--eta` set to the `eta=` value that line carries" in bullet, loop
    assert "a phase that names the floor" in bullet, loop
    # The literal write: `status` refuses a row with no `--state`, so the lane must not infer it.
    assert (
        f"status --actor {loop} --project <name> --state idle "
        '\\ --phase "backoff floor, guard wake" --eta <the eta= value>'
    ) in bullet, loop
    # The name collision the alarm's exclusion list invites, answered where the lane reads it.
    assert "not the registry's `stand-down` verb, so nothing else excludes your row" in bullet, loop
    assert "with `--eta` now plus that delay" in flat, (
        f"{loop}/SKILL.md: the no-guard fallback schedules a wake without declaring it"
    )


@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_the_blind_rewrite_on_a_stand_down_keeps_the_floor_and_its_eta(loop: str):
    """The `blind=` paragraph is a second row write on the same wake. With its own phase and a
    `<your next wake>` eta it overwrote the floor's name and put back the hour-out eta the
    stand-down bullet exists to remove. Every loop words it the same way, so none drifts."""
    flat = _flat(_skill(loop))
    para = _window(flat, "**Whatever the action, read the `blind=` line just above it.**", 1500)
    assert (
        "`--eta` set to the wake you take — now plus the delay you schedule, or the "
        "stand-down's `eta=` value —"
    ) in para, loop
    assert "--eta <the wake you take>" in para, loop
    assert "--eta <your next wake>" not in flat, loop
    assert (
        "On a stand-down this is the bullet's write, not a second one after it: keep `backoff "
        "floor, guard wake` as the phase with the `blind=` line appended, and the `eta=` value "
        "as its `--eta`"
    ) in para, loop


# ── the guard's trigger is a quota window, whoever it belongs to ─────────────

@pytest.mark.parametrize("loop", GUARDED_LOOPS)
def test_the_guard_names_the_window_and_not_its_scope(loop: str):
    """A lane that reads "account-wide", sees a refusal naming a model limit, and concludes the
    guard does not cover it has drawn the wrong conclusion from the text. The mechanism is a
    quota or rate-limit window refusing the turn, whether the account's or one model's."""
    text = _skill(loop)
    flat = _flat(text)
    assert "account-wide" not in text, loop
    assert "A quota or rate-limit window refuses a firing *before its first tool call*" in flat, loop
    assert "a per-model limit refuses the turn the same way" in flat, loop
    # Widened to a window, never to a refusal: the clock-only sentence stays beside it.
    assert "rate-limit / quota window and nothing else" in flat, loop


def test_the_watchdogs_rationale_names_the_window_and_not_its_scope():
    text = (REPO_ROOT / "skills" / "_shared" / "agentflow" / "wake_watch.py").read_text(
        encoding="utf-8"
    )
    assert "account-wide" not in text
    assert "a single model's" in text


@pytest.mark.parametrize(
    "rel", ("skills/_shared/agentflow/preflight.py", "README.md", "install/manifest.md")
)
def test_the_guards_user_facing_rationale_names_the_window_and_not_its_scope(rel: str):
    """The preflight DEGRADED message, the README tool table and the install manifest give the
    guard's reason to an operator; they must say the same thing the six skills do."""
    flat = _flat((REPO_ROOT / rel).read_text(encoding="utf-8"))
    assert "account-wide" not in flat, rel
    assert "quota or rate-limit window (the account's, or one model's)" in _flat(
        flat.replace('" "', "")
    ), rel


def test_the_auditor_writes_its_idle_row_on_every_empty_pass():
    """The auditor wakes `building` with a short eta, and Step 5.4 leaves the end-of-pass row to
    Step 7. If Step 7's write rides only on the `blind=` branch, the commonest empty pass
    (`action=schedule`, `blind=-`) keeps the short eta and is paged as a missed wake every idle
    wake. The write has to be stated unconditionally, ahead of the `blind=` paragraph."""
    flat = _flat(_skill("auditor"))
    lead = "**On every empty pass, write your end-of-pass row before you schedule — whatever the action.**"
    assert lead in flat
    assert flat.index(lead) < flat.index("**Whatever the action, read the `blind=` line just above it.**")
    head = _window(flat, lead, 600)
    assert "--actor auditor --project <name> --state idle" in head
    assert '--phase "idle <marker>" --eta <the wake you take>' in head
