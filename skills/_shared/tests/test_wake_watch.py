"""Tests for `agentflow.wake_watch` — the missed-wake alarm run from outside the fleet.

The failure this closes: `missed_wakes()` works, and every caller of it lives inside a loop's own
sweep. The steward is both the loop that sweeps and one of the lanes it watches, so the detector's
coverage is precisely the set of moments when a loop is still waking — the complement of the set
of moments the alarm exists for. On 2026-09-04T18:53Z nothing had died, the timers had simply
stopped, four of seven rows were 74–144 minutes overdue with the steward's own row among them, and
a human typing a command is the only reason it was ever reported.

Two things are being pinned here and they are different in kind:

* **Behaviour** — it finds what the sweep finds, it can name the steward, it stays quiet about a
  lane a human stopped, it pushes when there is something to say, and its exit code means what it
  says it means.
* **Structure** — it does not reimplement the rule and it contains no relaunch path. Those are
  asserted against the module SOURCE, because a behavioural test cannot tell "reused the shared
  detector" from "reimplemented it identically today", and the difference only shows up on the
  day the exclusion set changes.
"""
from __future__ import annotations

import inspect
import io
import tokenize
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentflow import ntfy, registry, wake_watch
from agentflow.registry import MISSED_WAKE_GRACE_MIN

pytest_plugins = ["tests.test_registry"]  # reuse the `env` fixture (initialized registry + run())

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Captured at import, BEFORE `tests.test_registry`'s autouse stub swaps `ntfy.notify` out for
#: every test in the session. One test here needs the real function to prove the body survives
#: the send path; everything else wants the stub.
_REAL_NOTIFY = ntfy.notify

NL = chr(10)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ")


#: Token types after which a STRING token is a docstring (or a bare free-standing string), rather
#: than a value the code actually uses. A string can only start a logical line in those positions.
_STATEMENT_START = frozenset((tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                              tokenize.ENCODING))


def _code_only(module) -> str:
    """The module's source as CODE ONLY: tokens joined with NOTHING between them, no docstrings.

    Three deliberate choices, and all three were wrong in the version this replaces — the review
    that found them demonstrated live mutations surviving the whole suite.

    **Joined with nothing, not with a space.** The old helper joined tokens with `" "`, so
    `os.system(...)` came out as `os . system` and the needle `"os.system"` could never match.
    `def _relaunch(lane): import os; os.system('claude -p /' + lane)` passed the full suite.

    **Comments and DOCSTRINGS are dropped; ordinary string literals are KEPT.** The reason for
    dropping anything at all is narrow: a docstring that names `subprocess` in order to promise
    there is none must not fail the test that checks there is none, or the next reader deletes
    the promise instead of keeping it. That argument covers prose and nothing else. Stripping
    EVERY string also erased `__import__('subprocess')`, and erased `"halted"` — a marker the
    restate test below lists and could therefore never fire on. A string in a statement position
    is prose; a string anywhere else is a value the code uses, and the code is what is under
    test. (Consequence, stated so it is not a surprise: this module's own help text and messages
    are now searched too, so they must not spell a forbidden name.)
    """
    return _code_of_source(inspect.getsource(module))


def _code_of_source(src: str) -> str:
    """`_code_only`'s body, over source text, so the helper itself can be tested."""
    kept = []
    prev = tokenize.ENCODING
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev in _STATEMENT_START:
            prev = tok.type       # a docstring: prose, not code
            continue
        prev = tok.type
        kept.append(tok.string)
    return "".join(kept)


def _stall(env, agent: str, *, now: datetime | None = None, overdue: float | None = None) -> str:
    """Give one Agents row a lapsed eta and a heartbeat older than it. Returns the eta.

    Written into the cells directly, for the reason the drill states: every `status` write stamps
    `heartbeat=now`, so a stall staged through the CLI always proves its own wake fired and the
    detector would be tested against a row that cannot fail.
    """
    # The real clock by default, because `main()` reads the real clock: a fixed T0 here would
    # make every CLI test's overdue figure a function of how long ago this file was written.
    now = now or datetime.now(UTC)
    overdue = overdue if overdue is not None else MISSED_WAKE_GRACE_MIN * 4
    eta = _iso(now - timedelta(minutes=overdue))
    heartbeat = _iso(now - timedelta(minutes=overdue + 60))
    lines = env.registry_path.read_text(encoding="utf-8").split(NL)
    hit = False
    for i, line in enumerate(lines):
        if line.startswith(f"| {agent} |"):
            cells = line.split("|")
            cells[5] = f" {heartbeat} "
            cells[6] = f" {eta} "
            lines[i] = "|".join(cells)
            hit = True
    assert hit, f"no Agents row for {agent!r} - the board shape changed"
    env.registry_path.write_text(NL.join(lines), encoding="utf-8")
    return eta


@pytest.fixture
def captured(monkeypatch):
    """Every ntfy send in this module lands here. NOTHING reaches the network.

    Patched on the `ntfy` module rather than on `wake_watch`, because `wake_watch` calls
    `ntfy.notify(...)` through the module object - so this catches the real call site and would
    stop catching it if that ever became a `from ... import notify`, which is the shape that
    makes a patched test silently exercise the unpatched path.
    """
    calls: list[dict] = []

    def _fake(trigger, **kw):
        calls.append({"trigger": trigger, **kw})

    monkeypatch.setattr(ntfy, "notify", _fake)
    return calls


def _argv(env, *extra: str) -> list[str]:
    return ["--project", "demo", "--file", str(env.registry_path), *extra]


# --------------------------------------------------------------------------- #
# The exit-code contract, in both directions
# --------------------------------------------------------------------------- #
def test_it_exits_0_when_it_FOUND_something(env, captured, capsys):
    """A finding travels by ntfy, never by exit code.

    A scheduler has ONE failure channel. If a found-rows result shared it with a crashed run,
    then "the watcher is broken" and "the fleet is stalled" would arrive as the same signal -
    which is the exact confusion this epic exists to remove. So a stalled fleet is a SUCCESSFUL
    run of the watcher.
    """
    _stall(env, "jr-dev-2")
    assert wake_watch.main(_argv(env)) == 0
    assert captured, "it found a stalled lane and pushed nothing"
    assert "jr-dev-2" in capsys.readouterr().out


def test_it_exits_0_when_it_found_NOTHING(env, captured, capsys):
    """The quiet case is the same exit code as the loud one. That is the whole contract."""
    assert wake_watch.main(_argv(env)) == 0
    out = capsys.readouterr().out
    assert "no missed wakes" in out
    assert "nothing was notified" in out, (
        "silence has to be explicit: a scheduler log that just ends looks identical to a run "
        "that died before it read anything"
    )
    assert not captured, "nothing was wrong and it pushed anyway"


def test_a_LAPSED_ONLY_board_never_claims_nothing_was_notified(env, captured, capsys):
    """The all-clear must not assert silence on a run that reported AND pushed.

    THE DEFECT THIS PINS, found by review and created by this epic. The missed-wake all-clear
    ends "nothing to report and nothing was notified". That was unconditionally true while a
    finding meant `missed or blind`, because the branch was reachable only when both were empty.
    Adding a third class to `findings` made it reachable on a lapsed-only board - which is the
    LITERAL gh#281 shape, so it was the common output of the new class, not an edge - and the one
    sentence an operator scans in a scheduler log then denied the finding printed two lines above
    it, and denied the push that had already gone out.

    Asserted on the RENDERED text and the PUSH together, because either alone passes while the
    pair contradicts: the pre-existing all-clear test builds a board with no lapsed row, so it
    cannot reach this branch, and the lapsed-class test asserts `"MISSED WAKE" not in out` while
    the false line says "no missed wakes" in lower case and slips straight through both.
    """
    _lapse(env, "jr-dev-2")
    assert wake_watch.main(_argv(env)) == 0
    out = capsys.readouterr().out
    assert "LAPSED ETA" in out, "the board was staged lapsed and nothing reported it"
    assert captured, "a lapsed row is a finding, so it must have been pushed"
    assert "nothing was notified" not in out, (
        "the report claimed nothing was notified in the same body that reported a lapsed row "
        "and sent a push"
    )
    assert "nothing to report" not in out, (
        "the report claimed nothing to report while a LAPSED ETA line stood above it"
    )
    assert "row(s) checked" in out, "the judged count is unconditional and must survive"


def test_the_two_cases_are_INDISTINGUISHABLE_by_exit_code(env, captured):
    """Pinned as its own assertion because it is the requirement, not a side effect.

    Written against both runs rather than against one constant, so a future change that makes
    findings exit 1 fails HERE, with this docstring attached, instead of somewhere downstream in
    a scheduler nobody is reading.
    """
    quiet = wake_watch.main(_argv(env))
    _stall(env, "lead-dev")
    loud = wake_watch.main(_argv(env))
    assert quiet == loud == 0


def test_it_exits_NONZERO_when_the_registry_is_unreadable(env, captured, capsys):
    """The watcher itself failed, learned nothing about the fleet, and must say so."""
    missing = env.registry_path.parent / "not-a-registry.md"
    rc = wake_watch.main(["--project", "demo", "--file", str(missing)])
    assert rc == 1
    assert "wake-watch FAILED" in capsys.readouterr().err
    assert not captured, "a watcher that could not read the board must not page about the board"


def test_it_exits_NONZERO_when_no_board_can_be_resolved(env, capsys, monkeypatch):
    """No --file, no $AGENTFLOW_REGISTRY, no --project: unresolvable config, not an all-clear."""
    monkeypatch.delenv("AGENTFLOW_REGISTRY", raising=False)
    assert wake_watch.main([]) == 1
    assert "wake-watch FAILED" in capsys.readouterr().err


def test_it_exits_NONZERO_when_the_NOTIFY_call_raises(env, monkeypatch, capsys):
    """A push that could not happen is a broken watcher, not a quiet fleet.

    This is the one failure mode where the finding is real and the report never left: the alarm
    saw the stall and could not say it. Exiting 0 there would tell the scheduler everything is
    fine on the strength of a report nobody received.
    """
    _stall(env, "steward")

    def _boom(trigger, **kw):
        raise ntfy.NtfyError("host wedged")

    monkeypatch.setattr(ntfy, "notify", _boom)
    assert wake_watch.main(_argv(env)) == 1
    assert "NtfyError" in capsys.readouterr().err


def test_the_FINDINGS_SURVIVE_a_push_that_could_not_be_delivered(env, monkeypatch, capsys):
    """The report is on stdout even when the send raised. THE ORDERING IS THE REQUIREMENT.

    `main()` used to write the report only after `watch()` returned, so a sender that raised -
    ntfy container down, a DNS blip, a closed port - destroyed the findings on the way past: the
    watcher computed `jr-dev-2 MISSED WAKE ... overdue 121m`, threw it away, and left stderr
    carrying a bare URLError. The one moment the push leg is broken is the one moment the stdout
    fallback is all there is, and it carried nothing.

    Exit 1 still stands - the watcher genuinely could not do its job - and the two facts are not
    in tension: the code says "do not trust me", the stdout says what was seen.
    """
    _stall(env, "jr-dev-2")

    def _boom(trigger, **kw):
        raise ntfy.NtfyError("connection refused")

    monkeypatch.setattr(ntfy, "notify", _boom)
    assert wake_watch.main(_argv(env)) == 1
    captured = capsys.readouterr()
    assert "jr-dev-2 MISSED WAKE" in captured.out, (
        "the finding was discarded because the delivery failed - which is the one case where "
        "stdout is the only channel left"
    )
    assert "NtfyError" in captured.err


def test_a_failure_does_not_escape_as_a_traceback(env, capsys):
    """Whatever went wrong, the output stays a line a scheduler log can carry."""
    rc = wake_watch.main(["--project", "demo", "--file", str(env.registry_path.parent)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and err.strip().startswith("wake-watch FAILED")


# --------------------------------------------------------------------------- #
# What it says, and to whom
# --------------------------------------------------------------------------- #
def test_the_push_names_the_lane_the_eta_and_the_lag(env, captured):
    """A push saying "something is overdue" sends a human to read the board themselves."""
    eta = _stall(env, "jr-dev-1", now=datetime.now(UTC))
    wake_watch.main(_argv(env))
    detail = captured[0]["detail"]
    assert "jr-dev-1" in detail and eta in detail
    assert "MISSED WAKE" in detail
    assert "demo" in captured[0]["epic"], "the push has to name the board it read"


def test_it_can_name_the_STEWARD(env, captured):
    """The case no in-sweep detector can cover, and the reason this module exists.

    Not "the steward is special to the detector" - it is not, and a rule that special-cased it
    would be wrong. It is that when THIS row is the one that stopped, the sweep that would have
    reported it is the thing that stopped.
    """
    _stall(env, "steward")
    assert wake_watch.main(_argv(env)) == 0
    assert "steward" in captured[0]["detail"]


def test_it_does_NOT_name_a_lane_halted_by_design(env, captured):
    """A documented halt ages forever and is correct.

    The stood-down row is given the STALLED row's exact cells, so the two are indistinguishable
    on timing and the only thing keeping one quiet is the exclusion. Every false page on a
    deliberate act teaches the reader to discount the alarm, and this alarm's whole value is in
    being believed.
    """
    assert env.run("stand-down", "--actor", "steward", "--agent", "jr-dev-1",
                   "--reason", "quiet window for a migration") == 0
    _stall(env, "jr-dev-1")
    _stall(env, "jr-dev-2")
    assert wake_watch.main(_argv(env)) == 0
    detail = captured[0]["detail"]
    assert "jr-dev-2" in detail, "the genuinely stalled lane must still be named"
    assert "jr-dev-1" not in detail


def test_a_healthy_control_lane_is_not_named(env, captured):
    """A detector that names every row proves nothing about the one that stalled."""
    now = datetime.now(UTC)
    assert env.run("status", "--actor", "auditor", "--state", "building", "--phase", "control",
                   "--eta", _iso(now + timedelta(hours=1))) == 0
    _stall(env, "jr-dev-2", now=now)
    wake_watch.main(_argv(env))
    assert "auditor" not in captured[0]["detail"]


def test_a_row_inside_its_grace_window_is_not_paged_about(env, captured):
    """The grace is the shared rule's, not a second one written here."""
    _stall(env, "jr-dev-2", overdue=MISSED_WAKE_GRACE_MIN / 2)
    assert wake_watch.main(_argv(env)) == 0
    assert not captured


def test_the_grace_default_is_REGISTRYS_constant_and_not_a_copy_of_its_value(env, monkeypatch):
    """Move the shared constant and the watcher's default must move with it.

    Hard-coding `30.0` here survived the whole suite, because every other test uses the same
    number the constant happens to hold. The point of a shared rule is that it has ONE value,
    and a copy of today's value is not that - it is a second answer that agrees until the day
    somebody tunes the window.
    """
    monkeypatch.setattr(registry, "MISSED_WAKE_GRACE_MIN", 5.0)
    _stall(env, "jr-dev-2", overdue=6.0)
    result = wake_watch.watch(env.registry_path, project="demo", push=False)
    assert result.grace_minutes == 5.0
    assert [m.agent for m in result.missed] == ["jr-dev-2"], (
        "a 6-minute lapse is past a 5-minute grace; the watcher is holding its own copy of 30"
    )


def test_grace_ZERO_is_honoured_and_not_rewritten_to_the_default(env, captured, capsys):
    """`--grace-minutes 0` means zero. It is falsy, and falsy is not absent.

    The two callers disagreed on exactly this: `registry.py` resolved the flag with `or`, so a
    board with a row six minutes past its eta produced `jr-dev-2 MISSED WAKE (overdue 6m > 0m)`
    from the watcher and `# no missed wakes (grace 30m)` from `read --missed-wakes` - the same
    rule, the same row, two answers. The companion assertion on the registry side lives in
    `test_registry.py`.
    """
    _stall(env, "jr-dev-2", overdue=6.0)
    assert wake_watch.main(_argv(env, "--grace-minutes", "0")) == 0
    assert "jr-dev-2 MISSED WAKE" in capsys.readouterr().out
    assert captured and "> 0m" in captured[0]["detail"]


def test_a_BLIND_row_counts_as_a_finding(env, captured, capsys):
    """A row the alarm cannot judge is not a row it cleared.

    `blind_rows()` exists because "nothing is overdue" and "I cannot see a third of the fleet"
    printed identically. Treating a blind row as "nothing found" here would rebuild that
    conflation in the one caller whose whole job is to speak when no loop is awake to.
    """
    # A real heartbeat with an unreadable eta - the shape a lane GOES blind in, as distinct from
    # the '-'/'-' pair a freshly provisioned row carries and which is correctly ignored.
    assert env.run("status", "--actor", "jr-dev-3", "--state", "building", "--phase", "x",
                   "--eta", _iso(datetime.now(UTC) + timedelta(hours=1))) == 0
    lines = env.registry_path.read_text(encoding="utf-8").split(NL)
    for i, line in enumerate(lines):
        if line.startswith("| jr-dev-3 |"):
            cells = line.split("|")
            cells[6] = " - "
            lines[i] = "|".join(cells)
    env.registry_path.write_text(NL.join(lines), encoding="utf-8")

    assert wake_watch.main(_argv(env)) == 0
    assert captured, "a row the alarm cannot see must not pass as an all-clear"
    assert "jr-dev-3" in captured[0]["detail"] and "INVISIBLE" in captured[0]["detail"]
    assert "CANNOT be checked" in capsys.readouterr().out


def test_an_all_clear_over_a_board_where_NOTHING_WAS_JUDGED_says_so(env, captured, capsys):
    """Stand every lane down that `stand-down` accepts and the watcher must not report full health.

    The steward cannot stand down its own row (`stand_down_self`) nor an ETA-horizon-exempt
    seat (`stand_down_exempt_seat`); neither declares an eta on the fixture board, so neither is
    judged and the run still checks nothing.

    This is the false green the in-sweep report already guards and this one did not. Every row
    is excluded, so `missed` and `blind` are both empty and the old wording read
    `# no missed wakes on demo (grace 30m); nothing to report` - an all-clear from a run that
    judged NOTHING. A false green from the outside watcher is worse than one from the sweep,
    because nothing else is looking.
    """
    lanes = [lane for lane in registry.parse_registry(env.text()).agents
             if lane != "steward" and lane not in registry.ETA_HORIZON_EXEMPT]
    for lane in lanes:
        assert env.run("stand-down", "--actor", "steward", "--agent", lane,
                       "--reason", "quiet window for a migration") == 0
    assert wake_watch.main(_argv(env)) == 0
    out = capsys.readouterr().out
    assert "0 row(s) checked" in out, (
        f"an all-clear over {len(lanes)} excluded rows claimed coverage it does not have: {out!r}"
    )
    assert not captured


def test_the_all_clear_carries_the_SAME_judged_count_the_in_sweep_report_carries(env, capsys):
    """The number is `registry.judged_row_count`, shared - not a second count that agrees today.

    `registry.py`'s own comment calls the count load-bearing, and the two reporters qualifying
    their all-clears differently is the exact divergence the shared-detector design forbids.
    """
    assert env.run("status", "--actor", "auditor", "--state", "building", "--phase", "control",
                   "--eta", _iso(datetime.now(UTC) + timedelta(hours=1))) == 0
    expected = registry.judged_row_count(registry.parse_registry(env.text()))
    assert expected == 1, "the fixture board judges exactly the one lane that declared an eta"
    assert wake_watch.main(_argv(env, "--no-notify")) == 0
    assert f"{expected} row(s) checked" in capsys.readouterr().out


def test_the_BLIND_all_clear_carries_the_judged_count_too(env, captured, capsys):
    """The partially-readable board qualifies BOTH ways: what was skipped and what was read."""
    assert env.run("status", "--actor", "jr-dev-3", "--state", "building", "--phase", "x",
                   "--eta", _iso(datetime.now(UTC) + timedelta(hours=1))) == 0
    lines = env.registry_path.read_text(encoding="utf-8").split(NL)
    for i, line in enumerate(lines):
        if line.startswith("| jr-dev-3 |"):
            cells = line.split("|")
            cells[6] = " - "
            lines[i] = "|".join(cells)
    env.registry_path.write_text(NL.join(lines), encoding="utf-8")
    assert wake_watch.main(_argv(env)) == 0
    out = capsys.readouterr().out
    assert "CANNOT be checked" in out and "checked row(s)" in out


def test_ONE_push_carries_every_finding(env, captured):
    """Four stalled lanes must not become four notifications every time the scheduler fires."""
    for lane in ("lead-dev", "jr-dev-1", "jr-dev-2", "steward"):
        _stall(env, lane)
    wake_watch.main(_argv(env))
    assert len(captured) == 1
    for lane in ("lead-dev", "jr-dev-1", "jr-dev-2", "steward"):
        assert lane in captured[0]["detail"]


def test_no_notify_reports_without_pushing(env, captured, capsys):
    """The dry run a human needs to point this at a live board without paging themselves."""
    _stall(env, "jr-dev-2")
    assert wake_watch.main(_argv(env, "--no-notify")) == 0
    assert "jr-dev-2" in capsys.readouterr().out
    assert not captured


def test_the_body_is_folded_to_ascii_rather_than_refused(env, monkeypatch):
    """An alarm that declines to fire because a lane typed an em dash is silent when it matters.

    `notify()` REFUSES a non-ASCII body rather than mangling it, which is right for a build
    notification and wrong here. Lossy is the correct direction when the alternative is mute.
    """
    _stall(env, "jr-dev-2")
    text = env.registry_path.read_text(encoding="utf-8").replace(
        "| jr-dev-2 | idle |", "| jr-dev-2 | idle— |")
    env.registry_path.write_text(text, encoding="utf-8")
    sent: list[bytes] = []
    monkeypatch.setenv("NTFY_URL", "http://example.invalid")
    monkeypatch.setenv("NTFY_TOPIC", "drill")
    # The one test here that runs the REAL notify: everything else wants the session-wide stub,
    # but "the body survives the send path" is a claim about that path.
    monkeypatch.setattr(ntfy, "notify", _REAL_NOTIFY)
    monkeypatch.setattr(ntfy, "_resolved_notify", lambda: ntfy._ResolvedNotify(
        "ntfy", "NTFY_URL", "NTFY_TOPIC", True, None))
    result = wake_watch.watch(env.registry_path, project="demo",
                              sender=lambda url, headers, body: sent.append(body))
    assert result.pushed and sent, "the send must survive a non-ASCII cell"
    assert b"jr-dev-2" in sent[0]


def test_the_EPIC_field_is_folded_too_when_the_project_name_is_DERIVED(tmp_path, monkeypatch):
    """The fold has to cover both fields, not the one that was thought about.

    `detail` was folded with a paragraph explaining why an em dash must not mute the alarm, and
    `epic=` one line away was not. `--project` is slug-guarded, so the field looked safe - but
    `_resolve_project` DERIVES the project from a directory name when `--project` is absent, and
    a directory name is unconstrained. One non-ASCII character there raised NtfyAsciiError, which
    `main()` turned into exit 1 with no push and no report on a genuinely stalled fleet: exactly
    the failure the fold one field over exists to prevent.

    Run against the REAL `notify`, because the session-wide stub accepts anything and would pass
    against the unfolded field. The sender is replaced, so nothing reaches the network.
    """
    reg = tmp_path / "projects" / "démo" / "status" / "registry.md"
    lock = tmp_path / "locks"
    lock.mkdir()
    assert registry.main(["init", "--file", str(reg), "--lock-dir", str(lock),
                          "--project", "demo"]) == 0
    now = datetime.now(UTC)
    lines = reg.read_text(encoding="utf-8").split(NL)
    for i, line in enumerate(lines):
        if line.startswith("| jr-dev-2 |"):
            cells = line.split("|")
            cells[5] = f" {_iso(now - timedelta(minutes=180))} "
            cells[6] = f" {_iso(now - timedelta(minutes=120))} "
            lines[i] = "|".join(cells)
    reg.write_text(NL.join(lines), encoding="utf-8")

    sent: list[tuple] = []
    monkeypatch.setenv("NTFY_URL", "http://example.invalid")
    monkeypatch.setenv("NTFY_TOPIC", "drill")
    monkeypatch.setattr(ntfy, "notify", _REAL_NOTIFY)
    monkeypatch.setattr(ntfy, "_resolved_notify", lambda: ntfy._ResolvedNotify(
        "ntfy", "NTFY_URL", "NTFY_TOPIC", True, None))
    monkeypatch.setattr(ntfy, "_urlopen_sender", lambda url, h, b: sent.append((url, h, b)))

    assert wake_watch.main(["--file", str(reg)]) == 0, (
        "a non-ASCII directory name silenced the alarm on a stalled fleet"
    )
    assert sent, "nothing was pushed"
    assert sent[0][1]["Title"].startswith("demo missed-wakes")


# --------------------------------------------------------------------------- #
# Structure: what the module must NOT contain
# --------------------------------------------------------------------------- #
def test_it_calls_the_SHARED_detector_rather_than_its_own(env, monkeypatch, captured):
    """Replace `registry.missed_wakes` and the watcher's answer must change with it.

    This is the "one implementation, two callers" requirement made checkable. A watcher that
    reimplemented the rule would pass every behavioural test in this file today and diverge
    silently on the day the exclusion set changes - which is the day it matters, because that is
    the day someone stands a lane down and expects the pages to stop.
    """
    sentinel = registry.MissedWake("lead-dev", "2020-01-01T00:00Z", "2019-01-01T00:00Z", 99.0,
                                   registry.parse_registry(env.text()).agents["lead-dev"])
    monkeypatch.setattr(registry, "missed_wakes", lambda *a, **kw: [sentinel])
    result = wake_watch.watch(env.registry_path, project="demo")
    assert [m.agent for m in result.missed] == ["lead-dev"]


def test_it_calls_the_SHARED_blind_row_report_too(env, monkeypatch):
    """Same argument, second function: blind rows are the alarm's own account of its coverage."""
    row = registry.parse_registry(env.text()).agents["auditor"]
    sentinel = registry.BlindRow("auditor", "-", "-", "no-eta", row)
    monkeypatch.setattr(registry, "blind_rows", lambda *a, **kw: [sentinel])
    result = wake_watch.watch(env.registry_path, project="demo", push=False)
    assert [b.agent for b in result.blind] == ["auditor"]


def test_the_PER_ROW_WORDING_is_the_shared_builder_and_not_a_second_copy(env, monkeypatch):
    """Replace `registry.missed_wake_line` and both reporters' output must change with it.

    The wording WAS identical in both places - by copy-paste, with `render`'s docstring
    advertising the sameness as a property an operator could rely on. Edit one and the claim
    goes false silently, and the operator who has seen one output starts having to work out
    whether the other describes the same condition. Written against the module attribute for
    the same reason the shared-detector tests are: a `from .registry import ...` would hide the
    call from this patch and the test would pass against a reimplementation.
    """
    monkeypatch.setattr(registry, "missed_wake_line", lambda m, g: f"SENTINEL {m.agent}")
    monkeypatch.setattr(registry, "blind_row_line", lambda b: f"SENTINEL-BLIND {b.agent}")
    _stall(env, "jr-dev-2")
    result = wake_watch.watch(env.registry_path, project="demo", push=False)
    assert "SENTINEL jr-dev-2" in wake_watch.render(result)
    assert "SENTINEL jr-dev-2" in wake_watch.push_body(result)


def test_the_forbidden_name_grep_can_SEE_the_mutations_it_is_supposed_to_catch():
    """The helper the two structural tests below stand on, pinned against what it used to miss.

    Both of these survived the full suite: joining tokens with a space made `os.system` unfindable
    (`os . system`), and dropping every string literal erased `__import__('subprocess')` down to
    a dunder nobody was looking for. A structural test that cannot fire is worse than no test -
    it is a green line asserting an absence it never checked.
    """
    seen = _code_of_source(
        "def _relaunch(lane):" + NL +
        "    import os" + NL +
        "    os.system('claude -p /' + lane)" + NL +
        "    __import__('subprocess').run([lane])" + NL
    )
    for forbidden in ("os.system", "__import__", "subprocess"):
        assert forbidden in seen, f"{forbidden!r} is invisible to the grep the no-relaunch test uses"


def test_the_restate_grep_can_SEE_a_marker_written_as_a_STRING():
    """`"halted"` is one of the markers the restate test lists, and it is always a string.

    Stripping every string literal made that entry dead: a reimplementation written as
    `if row.status == "halted": continue` - the obvious way anyone would write it - matched none
    of the four markers. The property held anyway, backstopped by the monkeypatch test, but a
    listed marker that cannot fire teaches a reader to trust a check that is not there.
    """
    seen = _code_of_source('if row.status == "halted":' + NL + "    pass" + NL)
    assert "halted" in seen


def test_the_forbidden_name_grep_still_ignores_the_PROSE_that_promises_the_absence():
    """The one thing the stripping is for: a docstring may name what the code must not do."""
    seen = _code_of_source('"""There is no subprocess and no os.system here."""' + NL + "x = 1" + NL)
    assert "subprocess" not in seen and "os.system" not in seen


def test_the_module_does_not_RESTATE_the_exclusion_sets():
    """The three exclusions live in `missed_wakes()` and are named nowhere here.

    A second copy of "halted, reaped, stood down" is not a duplicate that stays in sync - it is
    a second answer that agrees until it does not, and its disagreement shows up as a page for a
    lane a human deliberately stopped.
    """
    code = _code_only(wake_watch)
    for marker in ("REAPED_PHASE", "STOOD_DOWN_PHASE", "_row_is_excluded_phase", "halted"):
        assert marker not in code, (
            f"{marker} is named in wake_watch's code - the exclusion set is being restated "
            "instead of reused"
        )


def test_the_module_contains_NO_RELAUNCH_PATH():
    """The LOCKED decision, asserted rather than trusted to a paragraph.

    UNCHANGED by the 2026-09-22 revocation, and that is the point. The daemon-free invariant was
    revoked in favour of the single-daemon rule: the suite runs exactly one daemon, an
    OS-scheduled REPORT-ONLY invoker of this command. What that ruling changed is who may invoke
    the watcher; what it may do is untouched. So this test is not weakened - the watcher stays
    ntfy-only, and a process-starting call inside it would spend a second daemon's budget that
    nobody has granted.

    Quoting the rationale it enforces, from `registry.py`'s own `--missed-wakes` branch: notify
    costs one push; relaunch costs the single-writer rule, because a relaunch on a false
    positive starts a second copy of a live lane.

    Checked against the SOURCE because there is no behaviour to observe: the property is an
    absence, and the day it stops holding is the day someone adds the convenience, not a day any
    output changes.
    """
    code = _code_only(wake_watch)
    # `__import__` and `importlib` are on the list because the review demonstrated
    # `__import__('subprocess').run([...])` surviving the whole suite: the old helper stripped
    # the string, so the only trace left of the import was the dunder itself. A dynamic import
    # is how every one of the other names gets in without being written.
    for forbidden in ("subprocess", "os.system", "Popen", "os.spawn", "os.exec",
                      "multiprocessing", "CreateProcess", "ShellExecute",
                      "__import__", "importlib"):
        assert forbidden not in code, (
            f"wake_watch names {forbidden!r} - the watcher is NTFY ONLY. The suite's "
            "single-daemon budget is spent on the OS scheduler that INVOKES this command; a "
            "process-starting call in here spends a second one nobody granted"
        )


def test_the_locked_no_relaunch_rationale_is_written_where_a_reader_would_reopen_it():
    """The reason has to travel with the rule, or the next reader re-decides it from scratch.

    This assertion used to pin the word "daemon-free". That invariant was REVOKED on
    2026-09-22 - *"revoke the rule. Single daemon only is the new rule until it is challenged"* -
    so the old token would now pin a claim the architecture contradicts. It is re-pointed at the
    rule that replaced it rather than deleted: a deleted assertion is how the next reader
    concludes nobody ever decided anything, and this decision cost three fleet blackouts.

    The no-relaunch half is unchanged and still pinned, because the revocation moved *who may
    invoke* the watcher and not *what the watcher may do*.
    """
    doc = wake_watch.__doc__ or ""
    low = doc.lower()
    assert "single-daemon" in low, (
        "the rule that replaced the daemon-free invariant has to be stated here, where a reader "
        "who wants to add a second daemon would open the file"
    )
    assert "2026-09-22" in doc, "the ruling is dated, so a reader can find it rather than re-argue it"
    assert "relaunch" in low, "report-only is unchanged by the revocation and must still say so"
    assert "single-writer" in low, "the duplication risk is the concrete half of the argument"


def test_it_never_writes_to_the_board(env):
    """Report-only, like every other alarm in this family. Asserted byte-for-byte."""
    _stall(env, "jr-dev-2")
    before = env.registry_path.read_bytes()
    wake_watch.main(_argv(env, "--no-notify"))
    assert env.registry_path.read_bytes() == before


def test_the_project_is_resolved_the_way_every_other_command_resolves_it(env, captured):
    """Derived from the path when --project is absent, not defaulted to a guess.

    A watcher pointed at a different board than the fleet writes to reports an all-clear about a
    registry nobody uses - the most dangerous output this command has.
    """
    _stall(env, "jr-dev-2")
    assert wake_watch.main(["--file", str(env.registry_path)]) == 0
    assert captured[0]["epic"].startswith("demo ")


def test_a_bad_project_slug_is_refused_rather_than_joined_into_a_path(env, capsys):
    """`--project` routes into a filesystem path; the slug rule is registry's and is reused."""
    assert wake_watch.main(["--project", "../escape", "--file", str(env.registry_path)]) == 1
    assert "bad_project" in capsys.readouterr().err


def test_the_help_text_says_it_never_relaunches(env, capsys):
    """The operator wiring a scheduler reads --help, not this test file."""
    with pytest.raises(SystemExit):
        wake_watch.main(["--help"])
    assert "never relaunches" in capsys.readouterr().out


def test_the_help_text_tells_the_operator_to_CAPTURE_STDOUT(env, capsys):
    """The delivery caveat has to be where the person wiring the scheduler meets it.

    The honest paragraph existed in the module docstring, which nobody wiring a cron opens. With
    `NTFY_URL` unset and a lane stalled this command exits 0, prints the finding on stdout and a
    warning on stderr - so a job wired to the exit code alone reports permanent health over a
    fleet nobody is watching. The mitigation is one sentence and it belongs in `--help`.
    """
    with pytest.raises(SystemExit):
        wake_watch.main(["--help"])
    out = capsys.readouterr().out
    assert "stdout" in out and "stderr" in out, "the caveat is not in --help"
    assert "exit code" in out


def test_the_binding_doc_carries_the_capture_stdout_rule_too():
    """`docs/REGISTRY.md` is the other thing an operator reads before wiring a schedule."""
    doc = (REPO_ROOT / "docs" / "REGISTRY.md").read_text(encoding="utf-8")
    window = doc[doc.index("agentflow.wake_watch"):]
    assert "capture" in window.lower() and "stdout" in window.lower() and "stderr" in window, (
        "the liveness-sweep section documents the exit code but not the thing that has to be "
        "captured for the exit code's honesty to be survivable"
    )


def test_the_module_ships_where_the_package_ships():
    """A watcher that is not in the installed package is a command an operator cannot run."""
    assert (Path(wake_watch.__file__).parent / "registry.py").is_file()


def test_the_new_command_is_in_the_CHANGELOG():
    """The release that shipped it carries an entry per shipped verb, and this is a user-facing command.

    A command an operator is told to schedule, arriving with no line in the changelog, is a
    change nobody reading the release notes can date or explain. It shipped in 0.3.0, so the
    entry is looked for in that section, not in whatever `## Unreleased` holds now.
    """
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    start = text.find(NL + "## 0.3.0 ")
    assert start != -1, "CHANGELOG.md has no '## 0.3.0 ' heading, the release that shipped wake_watch"
    release = text[start + 1:]
    end = release.find(NL + "## ", 1)
    release = release[:end] if end != -1 else release
    assert "wake_watch" in release, "the outside watcher shipped without a changelog entry"


# --------------------------------------------------------------------------- #
# gh#281 -- the lapsed-eta class, in the ONE thing that reports unattended
#
# A class wired into `registry.py` and nowhere else reaches only a steward who
# runs the flag by hand, which is the same hole one layer out: this watcher is
# what speaks when no loop is awake.
# --------------------------------------------------------------------------- #
def _lapse(env, agent: str, *, now: datetime | None = None, lapsed: float | None = None) -> str:
    """Give one Agents row a lapsed eta and a heartbeat NEWER than it. Returns the eta.

    The mirror image of `_stall`: same lapsed eta, opposite heartbeat ordering. That one cell is
    the whole difference between "the wake did not fire" and "the wake fired and the lane never
    re-declared", and it is why the two are separate classes.
    """
    now = now or datetime.now(UTC)
    lapsed = lapsed if lapsed is not None else registry.LAPSED_ETA_GRACE_MIN * 2
    eta = _iso(now - timedelta(minutes=lapsed))
    heartbeat = _iso(now - timedelta(minutes=5))
    lines = env.registry_path.read_text(encoding="utf-8").split(NL)
    hit = False
    for i, line in enumerate(lines):
        if line.startswith(f"| {agent} |"):
            cells = line.split("|")
            cells[5] = f" {heartbeat} "
            cells[6] = f" {eta} "
            lines[i] = "|".join(cells)
            hit = True
    assert hit, f"no Agents row for {agent!r} - the board shape changed"
    env.registry_path.write_text(NL.join(lines), encoding="utf-8")
    return eta


def test_it_calls_the_SHARED_lapsed_eta_detector_rather_than_its_own(env, monkeypatch):
    """Third detector, same requirement as the other two: replace `registry.lapsed_etas` and the
    watcher's answer must change with it. Written against the module attribute so a
    `from .registry import lapsed_etas` would fail here rather than pass against a copy.
    """
    row = registry.parse_registry(env.text()).agents["jr-dev-1"]
    sentinel = registry.LapsedEta("jr-dev-1", "2020-01-01T00:00Z", "2026-01-01T00:00Z", 99.0, row)
    monkeypatch.setattr(registry, "lapsed_etas", lambda *a, **kw: [sentinel])
    result = wake_watch.watch(env.registry_path, project="demo", push=False)
    assert [x.agent for x in result.lapsed] == ["jr-dev-1"]


def test_the_LAPSED_wording_is_the_shared_builder_and_not_a_second_copy(env, monkeypatch):
    """Same argument as `missed_wake_line`: an operator who has read one reporter's output is
    looking at the builder that made the other. Both surfaces of this module are checked, because
    the stdout report and the ntfy body are two call sites and only one of them is usually read.
    """
    monkeypatch.setattr(registry, "lapsed_eta_line", lambda x, g: f"SENTINEL-LAPSED {x.agent}")
    _lapse(env, "jr-dev-2")
    result = wake_watch.watch(env.registry_path, project="demo", push=False)
    assert "SENTINEL-LAPSED jr-dev-2" in wake_watch.render(result)
    assert "SENTINEL-LAPSED jr-dev-2" in wake_watch.push_body(result)


def test_a_LAPSED_row_counts_as_a_finding_and_is_PUSHED(env, captured, capsys):
    """The point of wiring it here at all. gh#281's row is alive by every signal the board
    carries, so nothing else in this module would have spoken about it - and this watcher is the
    only thing that reports when no loop is awake to run a read.
    """
    _lapse(env, "jr-dev-2")
    assert wake_watch.main(_argv(env)) == 0
    out = capsys.readouterr().out
    assert "jr-dev-2 LAPSED ETA" in out
    assert "MISSED WAKE" not in out, "the two classes have to stay distinguishable in the output"
    assert captured, "a lane that stopped declaring its next wake must reach a human"
    assert "LAPSED ETA" in captured[0]["detail"]


def test_a_STALLED_row_is_reported_as_a_MISSED_WAKE_and_not_ALSO_as_lapsed(env, capsys):
    """Disjointness at the reporter, not only in the library. Two lines for one row would hand a
    reader two different remedies for one fact.
    """
    _stall(env, "jr-dev-2")
    result = wake_watch.watch(env.registry_path, project="demo", push=False)
    assert [m.agent for m in result.missed] == ["jr-dev-2"]
    assert result.lapsed == []
    out = wake_watch.render(result)
    assert "jr-dev-2 MISSED WAKE" in out and "LAPSED ETA" not in out


def test_the_lapsed_ALL_CLEAR_is_printed_when_nothing_lapsed(env, captured, capsys):
    """A detector that prints nothing when it does not fire is indistinguishable from one that
    was never wired in - which is the shape of the hole gh#281 found, moved one layer out.
    """
    assert wake_watch.main(_argv(env)) == 0
    out = capsys.readouterr().out
    assert "no lapsed etas on demo" in out
    assert f"grace {registry.LAPSED_ETA_GRACE_MIN:g}m" in out
    assert not captured


def test_the_lapsed_grace_is_NOT_the_missed_wake_grace_in_the_watcher(env, capsys):
    """`--grace-minutes` tunes the wake alarm and nothing else here either. A lane 45 minutes
    past its eta is past the WAKE grace and inside the lapsed one, and the fleet's own in-flight
    procedure (`--heartbeat-only`, which preserves `eta` verbatim) makes that the NORMAL shape of
    an honest long build. An alarm that fires on correct behaviour is one the reader mutes.
    """
    _lapse(env, "jr-dev-2", lapsed=MISSED_WAKE_GRACE_MIN + 15)
    assert wake_watch.main(_argv(env, "--grace-minutes", "0")) == 0
    out = capsys.readouterr().out
    assert "LAPSED ETA" not in out
    assert "no lapsed etas on demo" in out


def test_gh682_a_far_eta_row_is_reported_INVISIBLE_through_the_shared_blind_report(env, captured):
    """wake_watch reuses `registry.blind_rows`, so an eta beyond the horizon past its own
    heartbeat -- the dead lane a year typo hides -- is pushed rather than passed as all-clear."""
    lines = env.registry_path.read_text(encoding="utf-8").split(NL)
    for i, line in enumerate(lines):
        if line.startswith("| jr-dev-1 |"):
            cells = line.split("|")
            cells[2] = " building "
            cells[5] = " 2026-09-20T00:00Z "
            cells[6] = " 2030-01-01T00:00Z "
            lines[i] = "|".join(cells)
    env.registry_path.write_text(NL.join(lines), encoding="utf-8")
    result = wake_watch.watch(env.registry_path, project="demo", push=False)
    assert [b.agent for b in result.blind] == ["jr-dev-1"], result.blind
    assert wake_watch.main(_argv(env)) == 0
    assert captured and "jr-dev-1" in captured[0]["detail"]
    assert "INVISIBLE" in captured[0]["detail"]
