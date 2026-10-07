"""Tests for the missed-wake alarm (E-0260) and the `eta`-as-a-field guard it reads.

The failure this exists to catch: on 2026-08-29 every loop in the fleet stopped waking
while every `claude.exe` stayed alive. The processes lived, the timers did not fire, and
nothing noticed for 32 hours -- the human found it by sitting down at the seat. No rule
was watching the one thing each lane had already told the board it would do.

Two halves, and the first is what makes the second non-vacuous:

* `eta` is mandatory on every `status` write except `halted`, so the declared next wake is
  a FIELD. Before this, lanes split between a real `eta` and prose like "next ~21:14Z"
  buried in `phase`, and an alarm reading that would have been a gate that passes against
  whatever a fixture happened to contain.
* `missed_wakes()` reports rows past their OWN declared eta with no heartbeat since it.

Every assertion here pins a REASON, not just a count: a test that only checks "something
was reported" cannot tell a working alarm from one firing for the wrong cause.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from agentflow.registry import (
    LAPSED_ETA_GRACE_MIN,
    MISSED_WAKE_GRACE_MIN,
    REAPED_FORCED_PHASE,
    REAPED_PHASE,
    STOOD_DOWN_PHASE,
    main,
    blind_rows,
    judged_row_count,
    lapsed_eta_line,
    lapsed_etas,
    missed_wakes,
    parse_registry,
    stale_rows,
)

pytest_plugins = ["tests.test_registry"]  # reuse the `env` fixture (initialized registry + run())


T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _blind_the_row(env, agent: str) -> None:
    """Rewrite one Agents row's eta cell to '-' in place, leaving a real heartbeat.

    This is the live gh#64 shape (a lane that WENT blind), which the CLI cannot produce any
    more -- the whole point of the guard. Written directly so the tests can still build it.
    """
    path = env.registry_path
    text = path.read_text(encoding="utf-8")
    out = []
    for line in text.split(chr(10)):
        if line.startswith(f"| {agent} |"):
            cells = line.split("|")
            cells[6] = " - "          # eta column
            line = "|".join(cells)
        out.append(line)
    path.write_text(chr(10).join(out), encoding="utf-8")


def _board(rows: list[tuple[str, str, str, str, str]]) -> str:
    """Minimal registry text with the given (agent, status, phase, heartbeat, eta) rows."""
    head = (
        "# Fleet Registry\n\n## Agents\n\n"
        "| agent | status | work | phase | heartbeat | eta | waiting_on |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    body = "".join(
        "| %s | %s | - | %s | %s | %s | - |\n" % (a, s, p, hb, eta) for a, s, p, hb, eta in rows
    )
    return (
        head + body
        + "\n## Queue\n\n"
        + "- **Deploy lock** - holder: none, taken-at: -\n"
        + "- **Notes** - (none)\n\n"
        + "| id | work item | lane | claimed_by | state | waiting_on | notes |\n"
        + "|---|---|---|---|---|---|---|\n"
        + "\n## Questions\n\n"
        + "\n## Blockers\n\n"
        + "| id | raised_by | owner | work | state | description |\n"
        + "|---|---|---|---|---|---|\n"
        + "\n## Directives\n\n"
    )


# --------------------------------------------------------------------------- #
# The alarm fires -- and for the stated reason
# --------------------------------------------------------------------------- #
def test_silent_row_past_its_own_eta_by_more_than_one_cadence_is_alarmed():
    """The core case: eta promised, grace elapsed, no heartbeat since."""
    eta = T0 - timedelta(minutes=90)
    board = _board([("lead-dev", "building", "building E-0001", _iso(eta - timedelta(minutes=5)), _iso(eta))])

    found = missed_wakes(board, T0)

    assert [m.agent for m in found] == ["lead-dev"]
    # The REASON, not merely the count: overdue is measured from the eta, not the heartbeat.
    assert found[0].overdue_minutes == pytest.approx(90.0)
    assert found[0].eta == _iso(eta)


def test_inside_the_grace_window_is_not_alarmed():
    """A lane a few minutes late is overrunning, not missing. The boundary is exclusive."""
    eta = T0 - timedelta(minutes=MISSED_WAKE_GRACE_MIN)
    board = _board([("steward", "idle", "sleeping", _iso(eta - timedelta(minutes=1)), _iso(eta))])

    assert missed_wakes(board, T0) == []
    # One minute past the grace and it fires -- proving the boundary is where it is claimed
    # to be rather than somewhere convenient.
    assert [m.agent for m in missed_wakes(board, T0 + timedelta(minutes=1))] == ["steward"]


def test_a_heartbeat_after_the_eta_proves_the_wake_fired():
    """Woke on time, stamped on the way through, then went quiet -- that is staleness, not a
    missed wake, and the two must not be conflated."""
    eta = T0 - timedelta(hours=6)
    board = _board([("auditor", "idle", "swept", _iso(eta + timedelta(minutes=2)), _iso(eta))])

    assert missed_wakes(board, T0) == []


def test_most_overdue_first():
    eta_a, eta_b = T0 - timedelta(minutes=200), T0 - timedelta(minutes=100)
    board = _board([
        ("jr-dev-1", "building", "x", _iso(eta_b - timedelta(minutes=1)), _iso(eta_b)),
        ("jr-dev-2", "building", "y", _iso(eta_a - timedelta(minutes=1)), _iso(eta_a)),
    ])

    assert [m.agent for m in missed_wakes(board, T0)] == ["jr-dev-2", "jr-dev-1"]


# --------------------------------------------------------------------------- #
# The two exclusions -- the spec's explicit "NEVER alarmed on" cases
# --------------------------------------------------------------------------- #
def test_a_declared_halt_is_never_alarmed_on():
    """A halt already told the human it stopped. Alarming pages twice for one event, and an
    alarm that repeats a reported fact is how a human learns to swipe it away."""
    eta = T0 - timedelta(hours=9)
    board = _board([("jr-dev-2", "halted", "blocked on Q-0009", _iso(eta - timedelta(minutes=1)), _iso(eta))])

    assert missed_wakes(board, T0) == []


def test_a_reaped_row_is_never_alarmed_on():
    """`reap` PRESERVES the dead eta as evidence, so without the marker a reaped row is
    permanently past an eta it will never answer -- it would alarm on every sweep forever."""
    eta = T0 - timedelta(hours=20)
    board = _board([("steward", "idle", REAPED_PHASE, _iso(eta - timedelta(minutes=1)), _iso(eta))])

    assert missed_wakes(board, T0) == []
    # Control: the SAME row without the reap marker is alarmed -- so the exclusion is doing
    # the work, and the row is not being skipped for some unrelated reason.
    live = _board([("steward", "idle", "sleeping", _iso(eta - timedelta(minutes=1)), _iso(eta))])
    assert [m.agent for m in missed_wakes(live, T0)] == ["steward"]


def test_reap_stamps_the_marker_the_alarm_reads(env):
    """The exclusion above is only real if `reap` actually writes that phase."""
    # Another lane, forced: the steward may not reap itself and this row's heartbeat is fresh
    # (gh#575). The rc is asserted so a refusal cannot pass as an unwritten marker.
    env.run("status", "--actor", "jr-dev-1", "--state", "idle", "--eta", _iso(T0))
    assert env.run("reap", "--actor", "steward", "--agent", "jr-dev-1",
                   "--force", "--reason", "test: the marker only") == 0

    # A forced reap stamps the forced marker (gh#575: no unverified "(stale)"); it is excluded
    # exactly like the stale one (pinned in test_registry: gh575_a_forced_reap_row...).
    phase = parse_registry(env.text()).agents["jr-dev-1"].phase
    assert phase == REAPED_FORCED_PHASE


# --------------------------------------------------------------------------- #
# Rows the alarm must stay quiet about
# --------------------------------------------------------------------------- #
def test_absent_eta_is_not_a_missed_wake():
    """An absent eta is `stale_rows`' business (death), not the alarm's (a broken promise).
    A row that never promised a wake cannot have missed one."""
    board = _board([("lead-dev", "idle", "held", _iso(T0 - timedelta(hours=40)), "-")])

    assert missed_wakes(board, T0) == []
    # ...and the DEATH rule still catches it, so nothing falls between the two functions.
    assert [s.agent for s in stale_rows(board, T0)] == ["lead-dev"]


def test_unreadable_heartbeat_is_skipped():
    """A freshly provisioned registry writes '-' there and must not report its whole roster."""
    board = _board([("auditor", "idle", "fresh", "-", _iso(T0 - timedelta(hours=5)))])

    assert missed_wakes(board, T0) == []


# --------------------------------------------------------------------------- #
# eta-as-a-field: the guard that stops the alarm being vacuous
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state", ["building", "idle", "waiting"])
def test_status_without_eta_is_refused_for_every_state_that_declares_a_wake(env, capsys, state):
    args = ["status", "--actor", "jr-dev-2", "--state", state]
    if state == "waiting":
        env.run("ask", "--actor", "jr-dev-2", "--urgency", "waiting",
                "--question", "q?", "--default", "d")
        args += ["--waiting-on", "Q-0001"]

    rc = env.run(*args)
    err = capsys.readouterr().err

    assert rc == 2, f"expected refusal (2), got {rc}; stderr={err!r}"
    assert "eta_required" in err, err
    # The message must be ACTIONABLE, not merely correct: the five live loops of the time met
    # this guard the moment it shipped, and an error that only says "no" wedges them instead of telling them
    # what to send. It must name the flag AND carry a usable example value.
    assert "--eta" in err, err
    assert re.search(r"--eta \d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", err), err



def test_halted_may_omit_eta(env):
    """A declared halt is blocked on an answer it cannot get. It still wakes on schedule, but
    no wake of its own unblocks it, so the eta is optional rather than a promise it owes.
    (An empty lane is NOT a halt; it schedules its next check and keeps a real eta.)"""
    env.run("ask", "--actor", "jr-dev-2", "--urgency", "halted",
            "--question", "blocked?", "--default", "d")

    assert env.run("status", "--actor", "jr-dev-2", "--state", "halted",
                   "--waiting-on", "Q-0001") == 0
    assert parse_registry(env.text()).agents["jr-dev-2"].eta == "-"


def test_heartbeat_only_still_works_on_a_row_that_has_an_eta(env):
    """The guard must not break the 30-minute ceiling re-stamp, which asserts nothing."""
    env.run("status", "--actor", "jr-dev-1", "--state", "building", "--eta", _iso(T0))

    assert env.run("status", "--actor", "jr-dev-1", "--heartbeat-only") == 0
    assert parse_registry(env.text()).agents["jr-dev-1"].eta == _iso(T0)


#: Every (status, phase) shape a row can be in that this pair of rules distinguishes, plus
#: controls. The universe is enumerated here rather than derived so that ADDING an exclusion to
#: `registry.py` without adding its shape here shows up as an unexercised case in review - but
#: the two SETS below are computed by running the module, never listed.
_ROW_SHAPES = (
    ("building", ""),
    ("building", "ordinary work in progress"),
    ("waiting", "parked on a question"),
    ("idle", ""),
    ("halted", "blocked on an answer"),
    ("building", REAPED_PHASE),
    ("idle", REAPED_PHASE),
    ("building", STOOD_DOWN_PHASE + ": quiet window for a migration"),
    ("idle", STOOD_DOWN_PHASE + ": quiet window for a migration"),
)

#: The phase exclusions as this FILE models them, transcribed rather than asked for. That is the
#: whole point and it is easy to "simplify" away: `_eta_is_optional` below resolves the phase side
#: of its invariant through `registry.py`'s own `_row_is_excluded_phase`, and so does the alarm, so
#: deleting an exclusion shrinks BOTH sides of the subset assertion together and it stays green.
#: `halted` never had that problem because both rules name it as a literal. These two now do too.
_PHASE_EXCLUSIONS_AS_MODELLED = {
    "reaped": REAPED_PHASE,
    "stood down": STOOD_DOWN_PHASE + ": quiet window for a migration",
}


def _alarm_excludes(status: str, phase: str) -> bool:
    """Does the ALARM skip this shape? Answered by running it, on a row that is plainly late.

    Uses this module's existing `_board` rather than a second one. An earlier draft defined its
    own and shadowed it, which broke seventeen tests that had nothing to do with this change -
    a reminder that a helper named for what it builds is a name someone else already took.

    THE PROBE ROW MUST BE A REAL LANE NAME. `parse_registry` keeps only rows whose agent is one
    of the six (`registry.py:526`), so a made-up name like "probe" is dropped before the alarm
    ever sees it - and every shape then reports as EXCLUDED, which is the most convincing
    possible way for a derived test to be vacuous. The non-vacuity test below is what caught it.
    """
    probe = "jr-dev-1"
    row = (probe, status, phase, _iso(T0 - timedelta(hours=5)), _iso(T0 - timedelta(hours=4)))
    return not any(m.agent == probe for m in missed_wakes(_board([row]), T0))


def _eta_is_optional(status: str, phase: str) -> bool:
    """May this shape carry an unreadable eta at all? Answered by running the GUARD.

    Both write paths are asked, because either one letting a row through leaves it eta-less:
    the full `status` write (which decides for a lane declaring a new state) and the
    `--heartbeat-only` re-stamp (which decides for a lane preserving the cell it already has).
    A shape is eta-optional if EITHER path accepts it with no readable eta.

    READ THE TWO CLAUSES BELOW AS DIFFERENT IN KIND, not just in spelling. `status == "halted"`
    is a TRANSCRIPTION: this file states the rule and the module is asked separately, so the two
    can disagree and something reds. `_row_is_excluded_phase(row.phase)` is a DELEGATION: it is
    the same call the alarm makes, so this side cannot disagree with the alarm by construction,
    and any subset assertion built on it is blind to that predicate changing. The independent
    model for those two exclusions lives at `_PHASE_EXCLUSIONS_AS_MODELLED`; keep the delegation
    here, because asserting the module's real refusal is what stops the transcription becoming a
    second implementation - but do not mistake it for coverage of the predicate itself.
    """
    from agentflow import registry as reg_mod

    class _A:  # the shape `_require_eta`-style guards read off a parsed args object
        pass

    row = reg_mod.AgentRow(
        agent="probe", status=status, work="-", phase=phase,
        heartbeat=_iso(T0 - timedelta(hours=5)), eta="-", waiting_on="-",
    )
    # The full-write guard, transcribed as the module states it, then asserted against the
    # module's own refusal below so this cannot drift into a second implementation.
    full_write_allows = status == "halted"

    heartbeat_only_allows = (
        reg_mod._parse_iso(row.eta) is not None
        or status == "halted"
        or reg_mod._row_is_excluded_phase(row.phase)
    )
    return full_write_allows or heartbeat_only_allows


def test_every_row_allowed_to_omit_eta_is_a_row_the_alarm_skips(env):
    """The invariant, DERIVED from the module on both sides.

    WHAT THIS REPLACED, because the shape matters more than the fix: this test used to read

        exempt_from_guard  = {"halted"}
        excluded_from_alarm = {"halted"}
        assert exempt_from_guard == excluded_from_alarm

    - two hardcoded literals compared to each other, importing nothing. PR #62's body said "a
    test pins those two sets as equal so they cannot drift apart"; the assurance was stated and
    the mechanism was absent. It could not go red if either rule changed, and by the time it was
    audited the real exclusion set had grown from one member to three without it noticing.

    WHY A SUBSET AND NOT AN EQUALITY, which is the other thing the old test got wrong. The
    failure mode named in its own docstring is one-directional: *a state that may omit eta but
    is still alarmed on would fire on every sweep with nothing the lane can do about it.* So
    every eta-optional shape must be alarm-excluded. The converse is legitimate and live - a
    reaped or stood-down row is excluded from the alarm and still required to declare an eta on
    any write that brings it back - so asserting equality would fail on correct code.

    AND THE LIMIT OF DERIVING BOTH SIDES, which cost this test most of its reach and is the
    general rule worth carrying: **an invariant whose two sides resolve through the SAME function
    is not pinned by comparing them.** The phase side of `_eta_is_optional` asks
    `registry.py`'s own `_row_is_excluded_phase`, and so does the alarm - so deleting an
    exclusion from that predicate shrinks both sides together and this assertion stays green.
    Measured: dropping `REAPED_PHASE` or `STOOD_DOWN_PHASE` left every derived-set test in this
    file passing. Deriving was still the right fix for what it replaced - two literals compared
    to each other could not see a rule change at all - but derivation moves the blind spot
    rather than removing it, and where both sides share a source the model has to be independent.
    `halted` was accidentally safe because both rules name it as a literal;
    `test_each_phase_exclusion_is_exempt_on_both_sides_by_name` and
    `test_the_modelled_phase_exclusions_are_the_only_ones` give the other two the same footing
    deliberately.
    """
    eta_optional = {shape for shape in _ROW_SHAPES if _eta_is_optional(*shape)}
    alarm_excluded = {shape for shape in _ROW_SHAPES if _alarm_excludes(*shape)}

    assert eta_optional <= alarm_excluded, (
        "these shapes may omit eta but are still alarmed on, so they would fire every sweep "
        f"with nothing the lane can do about it: {sorted(eta_optional - alarm_excluded)}"
    )


def test_the_derived_sets_are_not_vacuously_empty(env):
    """A subset assertion is satisfied by an empty left side, so pin both sides non-empty.

    Without this, deleting every exclusion from `_row_is_excluded_phase` AND every eta exemption
    would leave the test above green on two empty sets - the tautology's failure mode wearing a
    derivation's clothes.
    """
    eta_optional = {shape for shape in _ROW_SHAPES if _eta_is_optional(*shape)}
    alarm_excluded = {shape for shape in _ROW_SHAPES if _alarm_excludes(*shape)}
    alarmed = {shape for shape in _ROW_SHAPES if not _alarm_excludes(*shape)}

    assert eta_optional, "no shape may omit eta - the guard's exemption has been deleted"
    assert alarm_excluded, "the alarm excludes nothing - its exclusions have been deleted"
    assert alarmed, "the alarm excludes EVERYTHING, so it can never fire"


def test_halted_is_exempt_on_both_sides_by_name(env):
    """`halted` specifically, because it is the one shape both rules name in their own comments.

    The subset test above survives deleting the GUARD's halted exemption - the left side just
    shrinks - so the mutual exemption needs an assertion of its own. Both `registry.py`'s eta
    guard and its alarm cite each other about this exact state ("eta is mandatory exactly where
    the alarm reads it"), and that mutual claim is what is pinned here.
    """
    halted = ("halted", "blocked on an answer")
    assert _eta_is_optional(*halted), "a halted row must be allowed to omit eta"
    assert _alarm_excludes(*halted), "a halted row must not be alarmed on"


@pytest.mark.parametrize("label", sorted(_PHASE_EXCLUSIONS_AS_MODELLED))
def test_each_phase_exclusion_is_exempt_on_both_sides_by_name(env, label):
    """The same assertion `halted` gets, for the two exclusions that live in `phase`.

    Named rather than derived, because the subset test cannot see these at all: delete
    `REAPED_PHASE` from `_row_is_excluded_phase` and the alarm stops excluding a reaped row AND
    the guard stops exempting it, so the left side of `eta_optional <= alarm_excluded` shrinks in
    step with the right and nothing reds. Measured before this test existed: dropping either
    phase exclusion left all four derived-set tests passing, while deleting the `halted` clause
    red two of them. The mechanism caught the one exclusion it did not derive and was blind to
    the two it did.
    """
    shape = ("idle", _PHASE_EXCLUSIONS_AS_MODELLED[label])
    assert _alarm_excludes(*shape), f"a {label} row must not be alarmed on"
    assert _eta_is_optional(*shape), f"a {label} row must be allowed to omit eta"


def test_the_modelled_phase_exclusions_are_the_only_ones(env):
    """The other direction: an exclusion ADDED to the module that nobody transcribed here.

    The test above is one-directional - it catches a deletion. This catches the opposite drift,
    which is the one that decays quietly: a third exclusion lands in `_row_is_excluded_phase`,
    every derived set absorbs it silently, and this file's model of the rule is wrong without any
    assertion noticing. Equality over the enumerated universe is what makes the model load-bearing
    rather than decorative.

    It also pins the `startswith` in `_row_is_excluded_phase`, which is the likeliest regression
    of the three: `registry.py` records that its own first cut shipped `==` there, and the
    stand-down verb writes `"<marker>: <reason>"`, so `==` mutes the mark for every real row that
    carries a reason.
    """
    from agentflow import registry as reg_mod  # local, as `_eta_is_optional` imports it

    modelled = set(_PHASE_EXCLUSIONS_AS_MODELLED.values())
    for status, phase in _ROW_SHAPES:
        expected = phase in modelled
        assert reg_mod._row_is_excluded_phase(phase) == expected, (
            f"`_row_is_excluded_phase({phase!r})` returned "
            f"{reg_mod._row_is_excluded_phase(phase)!r}, this file models {expected!r} "
            f"(status {status!r}). Either an exclusion moved in `registry.py` and "
            "`_PHASE_EXCLUSIONS_AS_MODELLED` was not updated with it, or the predicate's "
            "`startswith` became `==` and a stand-down carrying a reason no longer matches. "
            "Fix whichever is wrong - do not delete the shape from `_ROW_SHAPES` to make this "
            "pass, because that is how the universe stops enumerating the case under review."
        )


def test_the_guard_transcription_matches_the_modules_own_refusal(env):
    """`_eta_is_optional` restates the guard, so prove the restatement against the real CLI.

    A helper that models a rule is a second implementation of it, and a second implementation
    is a place for the two to disagree quietly. This drives the actual `status` command for
    every shape and asserts the model predicted the module's answer.
    """
    for status, phase in _ROW_SHAPES:
        if phase.startswith(STOOD_DOWN_PHASE) or phase == REAPED_PHASE:
            # Those shapes cannot be reached through `status` on a live row: a stood-down row is
            # refused for a different reason entirely, and a reaped one is not written again.
            continue
        args = ["status", "--actor", "jr-dev-1", "--state", status]
        if status in ("waiting", "halted"):
            continue    # both demand a live waiting_on referent, tested elsewhere
        code = env.run(*args, "--phase", phase or "probe")
        predicted_ok = _eta_is_optional(status, phase)
        assert (code == 0) == predicted_ok, (
            f"model and module disagree for status={status!r} phase={phase!r}: "
            f"model said {'accept' if predicted_ok else 'refuse'}, CLI returned {code}"
        )


# --------------------------------------------------------------------------- #
# CLI surface -- report-only, never relaunches (the LOCKED decision)
# --------------------------------------------------------------------------- #
def test_cli_reports_and_writes_nothing(env, capsys, tmp_path):
    """A missed wake CANNOT be staged through `status`: that call stamps heartbeat=now, so the
    row it writes always proves its own wake fired. The board is written directly instead --
    which is the only honest way to reach this branch, and worth stating because an earlier
    version of this test staged it through `status` and read a green that meant nothing."""
    now = datetime.now(UTC)
    eta = now - timedelta(hours=4)
    board = tmp_path / "board.md"
    board.write_text(
        _board([("lead-dev", "building", "impl", _iso(eta - timedelta(minutes=5)), _iso(eta))]),
        encoding="utf-8",
    )
    before = board.read_text(encoding="utf-8")

    rc = main(["read", "--file", str(board), "--lock-dir", str(env.lock_dir),
               "--project", "demo", "--missed-wakes"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "lead-dev MISSED WAKE" in out
    # Range, not equality: `_iso` truncates the eta to the minute while the CLI measures from a
    # real clock, so the overdue figure carries up to a minute of seconds and rounds either way.
    # Asserting exactly 240 passed alone and failed in the full suite purely on when the second
    # hand happened to be -- a flaky assertion is worse than a loose one.
    overdue = re.search(r"overdue (\d+)m", out)
    assert overdue, out
    assert 239 <= int(overdue.group(1)) <= 241, out
    assert board.read_text(encoding="utf-8") == before, \
        "the alarm is report-only; it must never touch the registry"


def test_cli_honours_grace_ZERO_instead_of_rewriting_it_to_the_default(env, capsys, tmp_path):
    """`--grace-minutes 0` means zero. It is falsy, and falsy is not absent.

    The flag was resolved with `or`, so `0.0` fell through to the 30-minute default and a row
    six minutes past its eta printed `# no missed wakes (grace 30m); 1 row(s) checked` - while
    `agentflow.wake_watch --grace-minutes 0`, which has always tested `is None`, printed
    `jr-dev-2 MISSED WAKE ... (overdue 6m > 0m)` off the same board. Two callers of one shared
    rule giving two answers is the exact failure the shared-detector design exists to prevent,
    and the operator sees it as an alarm that fires or not depending on which command they typed.
    """
    now = datetime.now(UTC)
    eta = now - timedelta(minutes=6)
    board = tmp_path / "board.md"
    board.write_text(
        _board([("lead-dev", "building", "impl", _iso(eta - timedelta(minutes=5)), _iso(eta))]),
        encoding="utf-8",
    )
    rc = main(["read", "--file", str(board), "--lock-dir", str(env.lock_dir),
               "--project", "demo", "--missed-wakes", "--grace-minutes", "0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "lead-dev MISSED WAKE" in out, f"grace 0 was rewritten to the default: {out!r}"
    assert "> 0m)" in out, out


def test_cli_prints_through_the_SHARED_line_builders(env, capsys, tmp_path, monkeypatch):
    """The per-row wording is one builder, reached by both reporters.

    `agentflow.wake_watch.render`'s docstring advertises its per-row wording as deliberately the
    same as this command's, and until now it was the same by copy-paste: two f-strings kept in
    step by whoever remembered. Patch the builder and BOTH outputs must move; the companion
    assertion on the watcher side lives in `test_wake_watch.py`.
    """
    from agentflow import registry as registry_mod

    now = datetime.now(UTC)
    eta = now - timedelta(hours=4)
    board = tmp_path / "board.md"
    board.write_text(
        _board([("lead-dev", "building", "impl", _iso(eta - timedelta(minutes=5)), _iso(eta)),
                ("jr-dev-1", "building", "impl", _iso(now), "-")]),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry_mod, "missed_wake_line", lambda m, g: f"SENTINEL {m.agent}")
    monkeypatch.setattr(registry_mod, "blind_row_line", lambda b: f"SENTINEL-BLIND {b.agent}")
    assert main(["read", "--file", str(board), "--lock-dir", str(env.lock_dir),
                 "--project", "demo", "--missed-wakes"]) == 0
    out = capsys.readouterr().out
    assert "SENTINEL lead-dev" in out and "SENTINEL-BLIND jr-dev-1" in out


def test_cli_counts_judged_rows_through_the_SHARED_helper(env, capsys, tmp_path, monkeypatch):
    """The all-clear's coverage figure is the same function the outside watcher qualifies with."""
    from agentflow import registry as registry_mod

    board = tmp_path / "board.md"
    board.write_text(
        _board([("lead-dev", "building", "impl", _iso(datetime.now(UTC)),
                 _iso(datetime.now(UTC) + timedelta(hours=2)))]),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry_mod, "judged_row_count", lambda reg: 4242)
    assert main(["read", "--file", str(board), "--lock-dir", str(env.lock_dir),
                 "--project", "demo", "--missed-wakes"]) == 0
    assert "4242 row(s) checked" in capsys.readouterr().out


def test_cli_says_so_when_the_board_is_clean(env, capsys):
    env.run("status", "--actor", "lead-dev", "--state", "building",
            "--eta", _iso(datetime.now(UTC) + timedelta(hours=2)))

    assert env.run("read", "--missed-wakes") == 0
    assert "no missed wakes" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# gh#64 -- the alarm's silence and its all-clear looked identical
#
# Live reproduction, 2026-09-01: `read --missed-wakes` printed "# no missed wakes"
# against a board where TWO of six lanes carried `eta = -`, one of them silent 2.9h.
# The alarm was right about the rows it could see and mute about the rows it could
# not, and the output gave a reader no way to tell those apart.
# --------------------------------------------------------------------------- #
def test_row_with_unparseable_eta_is_reported_as_blind_not_silently_skipped():
    """The exact live shape: a long-silent row carrying `eta = -`."""
    board = _board([
        ("lead-dev", "idle", "idle", _iso(T0 - timedelta(hours=3)), "-"),
        ("steward", "idle", "idle", _iso(T0 - timedelta(minutes=5)), _iso(T0 + timedelta(minutes=30))),
    ])

    # It is NOT a missed wake -- it never declared a wake to miss. That distinction is the
    # reason this is a separate list rather than extra alarm rows.
    assert missed_wakes(board, T0) == []

    blind = blind_rows(board)
    assert [b.agent for b in blind] == ["lead-dev"]
    assert blind[0].eta == "-"
    assert blind[0].heartbeat == _iso(T0 - timedelta(hours=3))


def test_blind_excludes_halted_and_reaped_for_the_same_reasons_the_alarm_does():
    """A declared halt legitimately carries no eta; a reaped row is preserved dead evidence."""
    board = _board([
        ("jr-dev-1", "halted", "blocked on Q-0001", _iso(T0 - timedelta(hours=4)), "-"),
        ("jr-dev-2", "idle", REAPED_PHASE, _iso(T0 - timedelta(hours=9)), "-"),
        ("auditor", "idle", "idle", _iso(T0 - timedelta(hours=2)), "-"),
    ])

    # Only the genuinely-unaccounted row is blind; the other two are excluded BY REASON,
    # not by accident of ordering.
    assert [b.agent for b in blind_rows(board)] == ["auditor"]


def test_all_readable_board_reports_zero_blind():
    """The negative control: this must be able to come back empty, or the assertion is vacuous."""
    board = _board([
        ("lead-dev", "building", "building", _iso(T0), _iso(T0 + timedelta(minutes=30))),
        ("steward", "idle", "idle", _iso(T0), _iso(T0 + timedelta(hours=1))),
    ])
    assert blind_rows(board) == []


def test_cli_all_clear_is_QUALIFIED_when_a_row_cannot_be_checked(env, capsys):
    """`# no missed wakes` unqualified over a partly-unreadable board is the false green.

    UNCONDITIONAL assertions on a board built to CONTAIN a blind row. The first version of this
    test branched on the output (`if "INVISIBLE" in out: ... else: ...`), which selected its
    expectation from the behaviour under test -- whichever way the code went, one branch called
    it correct. Review proved it stayed green with `blind_rows()` gutted to `return []`, and it
    was the only cover for the qualified all-clear. A test that passes when you delete the thing
    it tests is worse than no test.
    """
    # steward is judgeable; lead-dev is deliberately left blind (real heartbeat, no eta).
    env.run("status", "--actor", "steward", "--state", "idle", "--eta", _iso(T0 + timedelta(hours=1)))
    env.run("status", "--actor", "lead-dev", "--state", "idle", "--eta", _iso(T0 + timedelta(hours=1)))
    _blind_the_row(env, "lead-dev")
    capsys.readouterr()

    env.run("read", "--missed-wakes")
    out = capsys.readouterr().out

    assert "lead-dev INVISIBLE" in out          # the row is NAMED, not just counted
    assert "no-eta" in out                       # and the reason says which cell to look at
    assert "CANNOT be checked" in out            # the all-clear is qualified
    assert "no missed wakes among the" in out
    # The unqualified form must NOT appear on a board with a blind row.
    assert "; 2 row(s) checked" not in out


def test_cli_unqualified_all_clear_only_on_a_fully_readable_board(env, capsys):
    """The negative control: the clean message must still be reachable, or the test above is
    asserting a message the code can never not print."""
    for who in ("lead-dev", "steward"):
        env.run("status", "--actor", who, "--state", "idle", "--eta", _iso(T0 + timedelta(hours=1)))
    capsys.readouterr()

    env.run("read", "--missed-wakes")
    out = capsys.readouterr().out

    assert "INVISIBLE" not in out
    assert "row(s) checked" in out
    assert "CANNOT be checked" not in out


def test_never_started_board_is_not_reported_blind(env, capsys):
    """A fresh `init` seeds every lane with '-' in BOTH cells. That is never-started, not gone
    blind, and reporting the whole roster is the false-positive class the docstring warns about."""
    capsys.readouterr()
    env.run("read", "--missed-wakes")
    out = capsys.readouterr().out
    assert "INVISIBLE" not in out


def test_blind_reports_an_unreadable_heartbeat_too_not_only_a_missing_eta():
    """`missed_wakes()` has TWO silent skips. Covering only the eta one leaves the other hole."""
    board = _board([
        ("jr-dev-1", "idle", "idle", "-", _iso(T0 + timedelta(hours=1))),   # eta fine, hb unreadable
        ("steward", "idle", "idle", _iso(T0), _iso(T0 + timedelta(hours=1))),
    ])
    blind = blind_rows(board)
    assert [(b.agent, b.reason) for b in blind] == [("jr-dev-1", "no-heartbeat")]


def test_heartbeat_only_refuses_to_perpetuate_a_row_with_no_eta(env):
    """The root cause: --heartbeat-only returned before `eta_required`, preserving `eta = -`.

    This is the call the 30-minute in-flight ceiling tells every lane to use, so the
    most-travelled path was the one that kept the hole open forever.
    """
    assert env.run("status", "--actor", "lead-dev", "--heartbeat-only") == 2

    # And it is CURABLE by the documented route -- one full write, then re-stamps work.
    env.run("status", "--actor", "lead-dev", "--state", "idle", "--eta", _iso(T0 + timedelta(hours=1)))
    assert env.run("status", "--actor", "lead-dev", "--heartbeat-only") == 0


# --------------------------------------------------------------------------- #
# gh#98 -- a lane a human stopped on purpose is not a lane that died
# --------------------------------------------------------------------------- #
def _overdue_board(phase: str) -> str:
    """One row, 90 minutes past its own eta and silent since -- alarmed unless excluded."""
    eta = T0 - timedelta(minutes=90)
    return _board([("auditor", "idle", phase, _iso(eta - timedelta(minutes=5)), _iso(eta))])


def test_a_stood_down_row_is_not_alarmed_but_the_SAME_board_without_the_mark_is():
    """The fix and its own control, in one test.

    A human clearing a lane leaves exactly the signal of that lane dying: a live eta nothing
    will answer. The second half is what makes the first mean anything -- without it a broken
    alarm that reports nothing at all would pass.
    """
    assert missed_wakes(_overdue_board(f"{STOOD_DOWN_PHASE}: migration window"), T0) == []
    assert [m.agent for m in missed_wakes(_overdue_board("swept"), T0)] == ["auditor"]


def test_the_reason_travels_IN_the_marked_cell():
    """Written first as an equality test, the exclusion could never have matched a real row.

    The verb writes ``<marker>: <reason>`` -- the reason has to be in the cell or the row does
    not say why it is excluded -- so ``phase.strip() == STOOD_DOWN_PHASE`` was a perfectly
    vacuous fix that no board produced by the CLI would ever satisfy.

    This pins the PREDICATE'S tolerance for a `<marker>: <reason>` suffix, not the verb's output --
    it builds the cell itself and never calls the CLI. The verb's rendered cell is pinned by
    exact equality in test_the_mark_lands_on_the_row_with_its_reason_and_mutes_the_alarm.
    """
    marked = f"{STOOD_DOWN_PHASE}: cleared by hand to open a quiet window"
    assert marked != STOOD_DOWN_PHASE, "the fixture must carry a reason, or it proves nothing"
    assert missed_wakes(_overdue_board(marked), T0) == []


def test_the_marker_is_ANCHORED_not_a_substring_search():
    """A lane writes its own phase. An unanchored test would let any lane mute the alarm for
    itself by mentioning the marker in passing -- gh#88's bug, on the Directive marker in this
    same module."""
    quoting = f"asking dev-manager whether to be {STOOD_DOWN_PHASE} before the migration"
    assert [m.agent for m in missed_wakes(_overdue_board(quoting), T0)] == ["auditor"]


def test_a_stood_down_row_is_not_reported_BLIND_either():
    """The two reports share an exclusion list on purpose. A stood-down row with an unreadable
    eta is not a lane that lost its next wake; reporting it as one restores the false page
    through the other half of the same command."""
    board = _board([("auditor", "idle", f"{STOOD_DOWN_PHASE}: window", _iso(T0), "-")])
    assert blind_rows(board) == []
    # ...and the identical board without the mark IS reported, so the skip is the mark's doing.
    plain = _board([("auditor", "idle", "swept", _iso(T0), "-")])
    assert [b.agent for b in blind_rows(plain)] == ["auditor"]


def test_the_all_clear_does_not_COUNT_a_row_it_never_judged(env, capsys):
    """The qualified all-clear exists to be honest about coverage. Counting an excluded row as
    'checked' is a milder version of the false green the whole report exists to remove."""
    # `read --missed-wakes` reads the REAL clock, so both rows are dated off it: fully
    # readable, not overdue, so the all-clear is what prints and the COUNT is what is on trial.
    now = datetime.now(UTC)
    soon = _iso(now + timedelta(hours=1))
    text = _board([
        ("lead-dev", "building", "impl", _iso(now), soon),
        ("auditor", "idle", f"{STOOD_DOWN_PHASE}: window", _iso(now), soon),
    ])
    env.registry_path.write_text(text, encoding="utf-8")
    capsys.readouterr()
    assert main(["read", "--file", str(env.registry_path), "--missed-wakes"]) == 0
    out = capsys.readouterr().out
    assert "1 row(s) checked" in out, f"the stood-down row must not be counted as judged: {out!r}"


# --- the verb ---------------------------------------------------------------------------- #
def _mark(env, agent="auditor", reason="cleared by hand for the migration window"):
    return env.run("stand-down", "--actor", "steward", "--agent", agent, "--reason", reason)


def test_only_the_steward_may_stand_a_lane_down(env, capsys):
    """The human is not a writer -- the steward is the scribe -- and a lane muting the alarm
    for itself is the whole failure mode inverted."""
    capsys.readouterr()
    assert env.run("stand-down", "--actor", "lead-dev", "--agent", "auditor", "--reason", "x") == 2
    err = capsys.readouterr().err
    assert "stand_down_steward" in err and "REGISTRY.md" in err


def test_the_seat_may_no_longer_stand_a_lane_down(env, capsys):
    """The verb moved WITH the pump: dev-manager held it until this epic and holds it no more."""
    capsys.readouterr()
    assert env.run("stand-down", "--actor", "dev-manager", "--agent", "auditor", "--reason", "x") == 2
    err = capsys.readouterr().err
    assert "stand_down_steward" in err and "REGISTRY.md" in err


def test_a_blank_reason_is_refused(env, capsys):
    """argparse enforces the flag's PRESENCE; whitespace is the value it lets through. A mark
    saying a human stopped this lane and refusing to say why is indistinguishable from the
    dead lane the alarm exists to catch."""
    capsys.readouterr()
    assert _mark(env, reason="   ") == 2
    err = capsys.readouterr().err
    # BOTH halves, like its two siblings: RegistryError falls back to rendering the bare key when
    # RULES has no entry, so asserting the key alone cannot tell the documented refusal from the
    # degraded one that prints a code at an operator.
    assert "stand_down_needs_reason" in err and "REGISTRY.md" in err


def test_a_pipe_in_the_reason_cannot_corrupt_the_table(env, capsys):
    """The reason lands in a markdown cell read by every loop."""
    capsys.readouterr()
    assert _mark(env, reason="stopped | for the migration") == 2
    assert "inline_only" in capsys.readouterr().err


def test_the_mark_lands_on_the_row_with_its_reason_and_mutes_the_alarm(env):
    eta = datetime.now(UTC) + timedelta(hours=2)
    assert env.run("status", "--actor", "auditor", "--state", "idle", "--phase", "swept",
                   "--eta", _iso(eta)) == 0
    # Judged from beyond the declared wake: heartbeat is NOW (the CLI stamps it), so the eta
    # has to be in the future or the "heartbeat after the eta" rule mutes it for the wrong
    # reason and the control below would pass against a broken alarm.
    later = eta + timedelta(minutes=90)
    assert [m.agent for m in missed_wakes(env.reg(), later)] == ["auditor"], "control: it alarms first"

    reason = "cleared by hand for the migration window"
    assert _mark(env, reason=reason) == 0
    row = env.reg().agents["auditor"]
    # EXACT equality on the rendered cell, which is what docs/REGISTRY.md promises and what makes
    # the cell readable to a human diffing the registry -- the stated reason the reason lives in
    # the phase at all. `startswith` plus a substring both hold for a concatenation with no
    # separator, so neither pins the shape; this subsumes both.
    assert row.phase == f"{STOOD_DOWN_PHASE}: {reason}"
    assert missed_wakes(env.reg(), later) == []


def test_claims_and_eta_survive_the_mark(env):
    """Deliberately NOT reap -- and built with the things reap actually returns.

    The first version of this test asserted over a fixture that had claimed nothing and held no
    lock, so `reg.queue` was empty and `deploy_holder` was `none` throughout: it could not have
    caught a stand-down that returned claims, which is the ONE guarantee the docstring,
    docs/REGISTRY.md and dev-manager/SKILL.md all lead with. It uses the reap test's fixture now
    (test_registry.py: publish -> claim -> status -> deploy-lock take) and asserts the inverse.
    """
    assert env.run("publish", "--actor", "lead-dev", "--slug", "first-queued-item", "--title", "x",
                   "--lane", "jr-dev-1") == 0
    assert env.run("claim", "--actor", "jr-dev-1", "--id", "E-0001-first-queued-item") == 0
    assert env.run("status", "--actor", "jr-dev-1", "--state", "building", "--work", "E-0001-first-queued-item",
                   "--phase", "impl", "--eta", "2026-09-01T12:00Z") == 0
    assert env.run("deploy-lock", "take", "--actor", "jr-dev-1") == 0
    before_reg = env.reg()
    before = before_reg.agents["jr-dev-1"]
    before_row = before_reg.queue["E-0001-first-queued-item"]

    assert _mark(env, agent="jr-dev-1") == 0

    reg = env.reg()
    # The two things `reap` returns, both untouched. Compared against what the row actually held
    # rather than against a literal: `status --state building` propagates into the Queue row's
    # state, so a hard-coded "claimed" here would fail for a reason that has nothing to do with
    # stand-down.
    assert reg.queue["E-0001-first-queued-item"].claimed_by == "jr-dev-1"
    assert reg.queue["E-0001-first-queued-item"].state == before_row.state != "ready"
    assert reg.deploy_holder == "jr-dev-1"
    # And the row itself: everything but `phase`.
    after = reg.agents["jr-dev-1"]
    assert (after.status, after.work, after.eta, after.heartbeat, after.waiting_on) == (
        before.status, before.work, before.eta, before.heartbeat, before.waiting_on
    )


def test_standing_down_the_deploy_lock_holder_warns_and_does_not_release_it(env, capsys):
    """The one thing stand-down deliberately leaves dangerous.

    It does not release the lock, because releasing could step on a half-applied merge -- but the
    lock serializes every merge for every builder, so a stood-down holder wedges the fleet
    with no other signal. The warning is that signal, and a failure it prevents is invisible by
    definition, so prose alone would not hold it.
    """
    assert env.run("status", "--actor", "lead-dev", "--state", "idle", "--phase", "swept",
                   "--eta", "2026-09-01T23:00Z") == 0
    assert env.run("deploy-lock", "take", "--actor", "lead-dev") == 0
    capsys.readouterr()

    assert _mark(env, agent="lead-dev") == 0

    err = capsys.readouterr().err
    assert "deploy lock" in err, f"a stood-down holder wedges every merge; got {err!r}"
    assert env.reg().deploy_holder == "lead-dev", "the warning must not double as a release"


def test_the_mark_CLEARS_on_the_lanes_next_real_status_write(env):
    """Nothing has to remember to un-stand-down a lane: the lane saying it is back is what
    clears the mark, and the alarm watches it again from that moment."""
    eta = datetime.now(UTC) + timedelta(hours=2)
    later = eta + timedelta(minutes=90)
    assert env.run("status", "--actor", "auditor", "--state", "idle", "--phase", "swept",
                   "--eta", _iso(eta)) == 0
    assert _mark(env) == 0
    assert missed_wakes(env.reg(), later) == []

    assert env.run("status", "--actor", "auditor", "--state", "idle", "--phase", "back",
                   "--eta", _iso(eta)) == 0
    assert STOOD_DOWN_PHASE not in env.reg().agents["auditor"].phase
    assert [m.agent for m in missed_wakes(env.reg(), later)] == ["auditor"]


def test_heartbeat_only_cannot_re_stamp_a_stood_down_row(env, capsys):
    """A fresh timestamp over a row that says this lane is stopped is worse than a stale one:
    a stale row is visibly stale, a fresh one reads as ground truth. Refusing here is also
    what makes "clears on the next real status write" true rather than merely intended --
    --heartbeat-only preserves the phase, so it would keep the mark alive indefinitely."""
    assert env.run("status", "--actor", "auditor", "--state", "idle", "--phase", "swept",
                   "--eta", "2026-09-01T23:00Z") == 0
    assert _mark(env) == 0
    capsys.readouterr()
    assert env.run("status", "--actor", "auditor", "--heartbeat-only") == 2
    err = capsys.readouterr().err
    assert "stand_down_heartbeat_only" in err and "REGISTRY.md" in err


# --------------------------------------------------------------------------- #
# gh#281 -- a lane that re-stamps its heartbeat and never advances its eta
#
# `eta` is the field the whole missed-wake design rests on, and nothing was
# watching whether it was still being maintained. `missed_wakes()` skips a row
# whose heartbeat is newer than its eta -- true about the wake that already
# fired, silent about the next one -- and `stale_rows()` skips any row whose
# heartbeat is fresh. So a loop that satisfied `eta_required` exactly once and
# then only ever ran `--heartbeat-only` was reported by NO detector at ANY
# threshold, for as long as it lived.
# --------------------------------------------------------------------------- #
def _lapsed_board(*, status="building", phase="impl", hb_age_min=5.0, eta_age_min=300.0) -> str:
    """One `lead-dev` row in the gh#281 shape: eta long past, heartbeat minutes old."""
    return _board([
        (
            "lead-dev",
            status,
            phase,
            _iso(T0 - timedelta(minutes=hb_age_min)),
            _iso(T0 - timedelta(minutes=eta_age_min)),
        ),
    ])


def test_a_lapsed_eta_with_a_FRESH_heartbeat_is_reported_as_its_OWN_class():
    """The defect, and the shape of the fix, in one test.

    The row is alive by every signal the board carries -- five minutes since its last stamp --
    and five hours past the wake it promised. Both other detectors are asserted SILENT on the
    same board, unconditionally, because "the new one fires" proves nothing about the hole
    unless the old ones are shown to still be in it.
    """
    board = _lapsed_board()

    assert missed_wakes(board, T0) == [], "the heartbeat is newer than the eta; not a missed wake"
    assert stale_rows(board, T0) == [], "the heartbeat is 5 minutes old; the lane is not stale"
    assert blind_rows(board) == [], "both cells parse; nothing here is invisible"

    rows = lapsed_etas(board, T0)
    assert [x.agent for x in rows] == ["lead-dev"]
    assert rows[0].lapsed_minutes == pytest.approx(300.0)
    assert rows[0].heartbeat == _iso(T0 - timedelta(minutes=5))

    line = lapsed_eta_line(rows[0], LAPSED_ETA_GRACE_MIN)
    assert "LAPSED ETA" in line, "the class has to be readable off the line, like its two siblings"
    assert "MISSED WAKE" not in line, (
        "reported as an ordinary missed wake, a reader probes a lane that is demonstrably alive "
        "instead of asking it to re-declare -- the two remedies are the reason this is a class"
    )
    assert "status --eta" in line, "the line carries the remedy that distinguishes this class"


def test_the_healthy_shape_is_NOT_reported():
    """The control without which every assertion above is a predicate that fires on everything."""
    board = _board([
        ("lead-dev", "building", "impl",
         _iso(T0 - timedelta(minutes=5)), _iso(T0 + timedelta(minutes=30))),
    ])
    assert lapsed_etas(board, T0) == []


def test_a_row_the_MISSED_WAKE_alarm_reports_is_NOT_also_reported_as_lapsed():
    """Disjointness, asserted rather than assumed. Two reports of one row is a worse output than
    one, because a reader who sees both remedies applies neither with confidence.

    The two predicates partition the rows with a parseable eta past its grace: `missed_wakes`
    takes `hb <= eta`, this takes `hb > eta`. A board carrying one of each proves the partition
    in both directions -- neither list is empty, and no agent is in both.
    """
    board = _board([
        # classic missed wake: silent since before the eta
        ("jr-dev-1", "building", "impl",
         _iso(T0 - timedelta(hours=6)), _iso(T0 - timedelta(hours=5))),
        # gh#281: still stamping, eta never advanced
        ("jr-dev-2", "building", "impl",
         _iso(T0 - timedelta(minutes=5)), _iso(T0 - timedelta(hours=5))),
    ])
    missed = {m.agent for m in missed_wakes(board, T0)}
    lapsed = {x.agent for x in lapsed_etas(board, T0)}

    assert missed == {"jr-dev-1"}
    assert lapsed == {"jr-dev-2"}
    assert missed & lapsed == set(), "a row in both lists is a double report"
    assert missed | lapsed == {"jr-dev-1", "jr-dev-2"}, "a row in neither is the gh#281 silence"


def test_a_HALTED_row_is_exempt_from_the_lapsed_class_TOO():
    """The exemption is expressed on `status`, not `phase` -- read off `_cmd_status`, which
    waives `eta_required` for `--state halted` and for nothing else. A halted lane has no next
    wake by contract, so an eta it never advances is the documented shape rather than a defect.

    Paired with the identical board minus the marker, because without the control this cannot
    tell "correctly excluded" from "the predicate never fires".
    """
    assert lapsed_etas(_lapsed_board(status="halted"), T0) == []
    assert [x.agent for x in lapsed_etas(_lapsed_board(status="building"), T0)] == ["lead-dev"]


def test_a_REAPED_row_is_exempt_from_the_lapsed_class_TOO():
    """`reap` preserves the dead eta as evidence and the heartbeat freezes -- but for the window
    before that heartbeat ages past the staleness threshold, a reaped row wears exactly this
    shape. Without the skip every reap would add a permanent row to this report, which is the
    gh#313 defect re-opened in a new alarm.
    """
    assert lapsed_etas(_lapsed_board(phase=REAPED_PHASE), T0) == []
    assert [x.agent for x in lapsed_etas(_lapsed_board(phase="impl"), T0)] == ["lead-dev"]


def test_a_STOOD_DOWN_row_is_exempt_from_the_lapsed_class_TOO():
    """gh#98: a human stopped this lane on purpose, so its unanswered eta is an intended fact.
    Matched by PREFIX because `stand-down` always writes the marker followed by the reason --
    equality would match no real row and the exclusion would be perfectly vacuous.
    """
    phase = f"{STOOD_DOWN_PHASE}: quiet window for a migration"
    assert lapsed_etas(_lapsed_board(phase=phase), T0) == []
    assert [x.agent for x in lapsed_etas(_lapsed_board(phase="impl"), T0)] == ["lead-dev"]


def test_an_unreadable_heartbeat_is_BLIND_and_not_lapsed():
    """Whether this lane re-declared has no evidence either way without a readable heartbeat,
    and `blind_rows` already owns that row and names the cell. Reporting it here as well would
    both double-report it and assert a fact -- the lane is alive -- that nothing on the board
    shows.
    """
    board = _board([("lead-dev", "building", "impl", "-", _iso(T0 - timedelta(hours=5)))])
    assert lapsed_etas(board, T0) == []
    assert [(b.agent, b.reason) for b in blind_rows(board)] == [("lead-dev", "no-heartbeat")]


def test_the_grace_is_a_BOUNDARY_and_not_a_suggestion():
    """Exactly at the grace is not past it; one minute beyond is. Asserted on both sides so a
    flipped comparison cannot pass by being wrong in only one direction.
    """
    at = _lapsed_board(eta_age_min=LAPSED_ETA_GRACE_MIN)
    past = _lapsed_board(eta_age_min=LAPSED_ETA_GRACE_MIN + 1)
    assert lapsed_etas(at, T0) == [], "a row exactly at the grace has not passed it"
    assert [x.agent for x in lapsed_etas(past, T0)] == ["lead-dev"]


def test_the_lapsed_grace_is_DELIBERATELY_LONGER_than_the_missed_wake_grace():
    """The false-positive class here is the fleet's NORMAL operating shape.

    `status --heartbeat-only` is what the 30-minute in-flight ceiling instructs every lane to
    use, and it preserves `eta` verbatim by design -- so every lane whose work outruns its
    declared eta wears this shape legitimately for as long as the work runs. At the missed-wake
    grace this check would fire on the whole fleet during any multi-hour build, and an alarm
    that fires on correct behaviour is one the reader mutes. Pinned behaviourally as well as
    numerically: a lane 45 minutes past its eta -- past the WAKE grace, inside this one -- is
    still quiet, and the same lane past this grace is not.
    """
    assert LAPSED_ETA_GRACE_MIN > MISSED_WAKE_GRACE_MIN
    honest_overrun = _lapsed_board(eta_age_min=MISSED_WAKE_GRACE_MIN + 15)
    assert lapsed_etas(honest_overrun, T0) == [], (
        "an honest overrun of a declared wake is not a lane that stopped declaring"
    )
    assert lapsed_etas(_lapsed_board(eta_age_min=LAPSED_ETA_GRACE_MIN * 2), T0) != []


def test_the_ISSUE_REPRODUCTION_is_now_reported_at_EVERY_sample():
    """The issue's own reproduction, run as a test: eta frozen, heartbeat re-stamped to
    now-5min at each sample, sampled every six hours across thirty days.

    That run produced empty stale and empty missed lists at every one of those samples -- the
    whole reason gh#281 is a defect and not a tuning complaint. Both of those silences are
    re-asserted here at every sample, unconditionally and beside the new report, so this test
    fails if the new class stops firing AND if either old one is quietly widened to cover it
    instead.
    """
    eta = T0                       # declared once, never advanced
    samples = 0
    for hours in range(0, 30 * 24, 6):
        now = T0 + timedelta(hours=hours) + timedelta(minutes=LAPSED_ETA_GRACE_MIN + 1)
        board = _board([
            ("lead-dev", "building", "impl", _iso(now - timedelta(minutes=5)), _iso(eta)),
        ])
        # The two detectors that were blind to this -- still blind, and still correct to be:
        assert stale_rows(board, now, 0.1) == [], (
            f"a heartbeat 5 minutes old is not stale at hour {hours}, even at a 6-minute threshold"
        )
        assert missed_wakes(board, now, 0.0) == [], (
            f"the heartbeat is newer than the eta at hour {hours}; no missed wake at any grace"
        )
        # The one that is not:
        rows = lapsed_etas(board, now)
        assert [x.agent for x in rows] == ["lead-dev"], (
            f"unreported at hour {hours} -- this is gh#281 exactly as the issue reproduced it"
        )
        samples += 1
    assert samples == 120, "the horizon shrank; a reproduction that samples nothing proves nothing"


def test_lapsed_etas_examines_EXACTLY_the_rows_judged_row_count_COUNTS():
    """The all-clear's coverage figure is `judged_row_count`, and this is why it is honest here.

    That function's predicate -- not halted, not reaped or stood down, eta parses, heartbeat
    parses -- is the same set this detector walks past its own skips. The equality is asserted
    against a board built to contain one row of every excluded kind, and the expected number is
    written down here rather than derived from either function, so a change to one of them
    cannot quietly re-define what the count claims.
    """
    board = _board([
        ("lead-dev", "building", "impl", _iso(T0), _iso(T0 + timedelta(hours=1))),   # judged
        ("jr-dev-1", "building", "impl", _iso(T0 - timedelta(minutes=5)),
         _iso(T0 - timedelta(hours=9))),                                             # judged
        ("jr-dev-2", "halted", "blocked on Q-0001", _iso(T0), "-"),                  # exempt
        ("auditor", "idle", REAPED_PHASE, _iso(T0 - timedelta(hours=9)), _iso(T0)),  # exempt
        ("steward", "idle", f"{STOOD_DOWN_PHASE}: migration", _iso(T0), _iso(T0)),   # exempt
        ("dev-manager", "idle", "idle", _iso(T0), "-"),                              # unparseable
    ])
    reg = parse_registry(board)
    assert judged_row_count(reg) == 2
    assert [x.agent for x in lapsed_etas(reg, T0)] == ["jr-dev-1"], (
        "only one of the two judged rows is lapsed; the other is the in-set control"
    )


# --------------------------------------------------------------------------- #
# gh#281 at the CLI -- the class has to reach a surface, or it is the same hole
# --------------------------------------------------------------------------- #
def _cli_lapsed_board(tmp_path, *, eta_age_min=LAPSED_ETA_GRACE_MIN + 60) -> str:
    """A board dated off the REAL clock: `read --missed-wakes` reads `datetime.now(UTC)`."""
    now = datetime.now(UTC)
    path = tmp_path / "board.md"
    path.write_text(
        _board([("lead-dev", "building", "impl", _iso(now - timedelta(minutes=5)),
                 _iso(now - timedelta(minutes=eta_age_min)))]),
        encoding="utf-8",
    )
    return str(path)


def test_cli_missed_wakes_PRINTS_the_lapsed_class(env, capsys, tmp_path):
    """It rides the EXISTING selector rather than a new `read --lapsed-eta`: a second verb is
    another thing nobody runs, which is the hole this closes, one layer out.
    """
    board = _cli_lapsed_board(tmp_path)
    assert main(["read", "--file", board, "--lock-dir", str(env.lock_dir),
                 "--project", "demo", "--missed-wakes"]) == 0
    out = capsys.readouterr().out
    assert "lead-dev LAPSED ETA" in out
    assert "MISSED WAKE" not in out, "the two classes must stay distinguishable in the output"
    assert "no missed wakes" in out, "the wake alarm's own all-clear still has to be printed"


def test_cli_missed_wakes_prints_the_lapsed_ALL_CLEAR_when_nothing_lapsed(env, capsys, tmp_path):
    """A check that prints nothing when it does not fire is indistinguishable from one that was
    never wired in -- the shape of gh#281 itself, moved one layer out.
    """
    now = datetime.now(UTC)
    board = tmp_path / "board.md"
    board.write_text(
        _board([("lead-dev", "building", "impl", _iso(now), _iso(now + timedelta(hours=2)))]),
        encoding="utf-8",
    )
    assert main(["read", "--file", str(board), "--lock-dir", str(env.lock_dir),
                 "--project", "demo", "--missed-wakes"]) == 0
    out = capsys.readouterr().out
    assert "no lapsed etas" in out
    assert "1 row(s) checked" in out
    assert f"grace {LAPSED_ETA_GRACE_MIN:g}m" in out, (
        "the all-clear has to name the rope it used, or a reader cannot tell what it cleared"
    )


def test_cli_lapsed_wording_is_the_SHARED_builder_and_not_a_second_copy(
    env, capsys, tmp_path, monkeypatch
):
    """Replace `registry.lapsed_eta_line` and this reporter's output must change with it -- the
    same requirement `missed_wake_line` and `blind_row_line` already carry, so the wording an
    operator learns here is the wording the outside watcher emits.
    """
    from agentflow import registry as registry_mod

    board = _cli_lapsed_board(tmp_path)
    monkeypatch.setattr(registry_mod, "lapsed_eta_line", lambda x, g: f"SENTINEL-LAPSED {x.agent}")
    assert main(["read", "--file", board, "--lock-dir", str(env.lock_dir),
                 "--project", "demo", "--missed-wakes"]) == 0
    assert "SENTINEL-LAPSED lead-dev" in capsys.readouterr().out


def test_cli_grace_minutes_does_NOT_retune_the_lapsed_class(env, capsys, tmp_path):
    """`--grace-minutes` tunes the wake alarm and nothing else, which the pairing table already
    says. Sharing the knob would mean an operator tightening the wake alarm silently floods this
    report with every lane doing exactly what the in-flight runbook tells it to.
    """
    board = _cli_lapsed_board(tmp_path, eta_age_min=MISSED_WAKE_GRACE_MIN + 15)
    assert main(["read", "--file", board, "--lock-dir", str(env.lock_dir),
                 "--project", "demo", "--missed-wakes", "--grace-minutes", "0"]) == 0
    out = capsys.readouterr().out
    assert "LAPSED ETA" not in out, "a 45-minute overrun is inside the lapsed grace"
    assert f"no lapsed etas (grace {LAPSED_ETA_GRACE_MIN:g}m)" in out
