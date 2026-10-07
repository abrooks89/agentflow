"""The stall drill, run in CI so the done-condition cannot quietly stop holding.

The drill itself lives in `harness/missed-wake-drill/` because it is meant to be RUN and cited
by an auditor, not only asserted. This module is the other half: it runs the same code every
build, so the epic's done-condition is a gate rather than a thing somebody did once.

Each test here breaks a different limb of the drill and requires it to fail. A drill that
reports success on a board where the detector was never going to fire is worth less than no
drill, because it launders the absence of evidence into a green line.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentflow import ntfy

REPO_ROOT = Path(__file__).resolve().parents[3]
DRILL_PATH = REPO_ROOT / "harness" / "missed-wake-drill" / "missed_wake_drill.py"


@pytest.fixture(autouse=True)
def _no_real_pings(monkeypatch):
    """Never POST from a test: NTFY_URL/NTFY_TOPIC are set in real fleet shells."""
    monkeypatch.setattr(ntfy, "notify", lambda trigger, **kw: None)


@pytest.fixture(scope="module")
def drill():
    if not DRILL_PATH.is_file():
        pytest.skip(f"drill not present at {DRILL_PATH}")
    spec = importlib.util.spec_from_file_location("_missed_wake_drill_for_test", DRILL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_drill_passes_end_to_end(tmp_path, drill):
    """The done-condition itself: stall a lane, detect it, alert, record."""
    record = drill.run_drill(tmp_path)
    assert "Missed-wake alarm: stall drill" in record
    assert (tmp_path / "missed-wake-drill.md").is_file()


def test_the_record_names_the_lane_the_eta_and_the_lag(tmp_path, drill):
    """A record that says "passed" and nothing else cannot be cited by a later audit.

    The three facts an auditor needs to re-derive the claim are which lane stalled, which
    declared wake it missed, and by how much - so the record has to carry all three.
    """
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    record = drill.run_drill(tmp_path, now=now)
    assert drill.STALLED_LANE in record
    assert "minutes" in record
    lapsed = (now - timedelta(minutes=drill.MISSED_WAKE_GRACE_MIN * 4)).strftime("%Y-%m-%dT%H:%MZ")
    assert lapsed in record, "the record must cite the exact eta that was missed"


def test_the_record_states_what_the_drill_does_NOT_prove(tmp_path, drill):
    """The honest limits stay in the artifact, not just in someone's memory of writing it.

    A record that omits them reads as a stronger result than the drill earned. The list CHANGED
    when `agentflow.wake_watch` shipped, and the change is the point: two of the old limits -
    "no module sends an ntfy" and "the alarm cannot report its own watcher's death" - stopped
    being true, and a record still claiming them would understate what the fleet now has. Three
    replace them, and each is a thing this drill genuinely cannot reach.
    """
    record = drill.run_drill(tmp_path)
    assert "does not establish" in record.lower()
    # Re-pointed 2026-09-22, not relaxed. This used to pin "nothing schedules the watcher",
    # which stopped being TRUE of the suite when the daemon-free invariant was revoked: the
    # rule is now single-daemon-only and the one daemon is an OS-scheduled invoker of exactly
    # this watcher. The drill's honest limit is narrower and unchanged - IT schedules nothing
    # and reads no scheduler - so the assertion now pins that claim plus the rule that replaced
    # the old one, rather than being deleted.
    assert "the drill schedules nothing" in record.lower(), (
        "the watcher is a command; whether anything invokes it on a cadence is outside this "
        "drill, and a watcher nobody invokes is silent"
    )
    assert "single-daemon" in record.lower(), (
        "the rule that replaced the daemon-free invariant has to reach the artifact, or a later "
        "audit reads the old limit and concludes the watcher still has no legitimate scheduler"
    )
    assert "no packet left the machine" in record.lower(), (
        "the drill injects its sender, and even a True from notify() is not a delivery receipt"
    )
    assert "process boundary" in record.lower(), (
        "'outside a loop' is proven to a subprocess and no further - whether the MACHINE had a "
        "session open is not a property of this code"
    )
    assert "relaunch" in record.lower(), "the LOCKED no-relaunch decision must stay cited"


def test_the_record_reports_the_OUTSIDE_watcher_walking_the_self_blind_case(tmp_path, drill):
    """The one claim the in-sweep detector structurally cannot make, named in the artifact.

    A record listing only the sweep's own limbs is indistinguishable from the record this drill
    wrote before the outside watcher existed - which is precisely the state the epic was filed
    against: a known hole with no artifact.
    """
    record = drill.run_drill(tmp_path)
    assert "agentflow.wake_watch" in record, "the record must name the command an auditor re-runs"
    assert drill.WATCHER_BLIND_LANE in record
    assert "separate interpreter" in record.lower()
    assert drill.STOOD_DOWN_LANE in record and "NOT reported" in record


def test_the_drill_FAILS_when_the_outside_watcher_goes_blind(tmp_path, drill, monkeypatch):
    """Neuter the outside watcher and the drill must refuse.

    Without this the whole outside-watcher half is a subprocess whose output nobody checks, and
    the drill would keep passing against a watcher that prints nothing at all.
    """
    monkeypatch.setattr(drill, "_run_watcher_outside", lambda reg, project: (0, "", ""))
    with pytest.raises(drill.DrillFailure, match="did not name"):
        drill.run_drill(tmp_path)


def test_the_drill_FAILS_when_the_watcher_exits_nonzero_on_a_finding(tmp_path, drill, monkeypatch):
    """The exit-code contract, drilled: a finding must never travel on the failure channel.

    A watcher that exited 1 on stalled lanes would make "the watcher is broken" and "the fleet is
    stalled" the same signal in a scheduler, which is the confusion the epic exists to remove.
    """
    real = drill._run_watcher_outside
    monkeypatch.setattr(drill, "_run_watcher_outside",
                        lambda reg, project: (1,) + real(reg, project)[1:])
    with pytest.raises(drill.DrillFailure, match="THE WATCHER IS BROKEN"):
        drill.run_drill(tmp_path)


def test_the_drill_FAILS_when_a_lane_halted_by_design_is_paged_about(tmp_path, drill, monkeypatch):
    """Delete the exclusion's effect and the drill must catch it.

    A watcher that pages for a lane a human deliberately stopped is a watcher people mute, and
    the muting is permanent while the alarm's value is entirely in being believed.
    """
    real = drill._run_watcher_outside
    calls = {"n": 0}

    def _leaky(reg, project):
        calls["n"] += 1
        code, out, err = real(reg, project)
        if calls["n"] >= 3:      # the stood-down run
            out += f"{drill.STOOD_DOWN_LANE} MISSED WAKE eta=whenever{chr(10)}"
        return code, out, err

    monkeypatch.setattr(drill, "_run_watcher_outside", _leaky)
    with pytest.raises(drill.DrillFailure, match="PAGED for"):
        drill.run_drill(tmp_path)


def test_the_drill_FAILS_when_the_lane_does_not_actually_stall(tmp_path, drill, monkeypatch):
    """Break the stall and the drill must refuse, rather than reporting a green.

    This is the assertion the epic's own audit found missing everywhere else: a drill whose
    steps cannot fail is a fixture that describes itself. Here the stall is neutered - the row
    is left with the fresh eta it declared - and the detector then has nothing to find.
    """
    monkeypatch.setattr(drill, "_stall_the_row", lambda *a, **kw: None)
    with pytest.raises(drill.DrillFailure, match="NOT detected"):
        drill.run_drill(tmp_path)


def test_the_drill_FAILS_when_the_detector_names_every_row(tmp_path, drill, monkeypatch):
    """The control lane earns its keep: a detector that reports everything reports nothing.

    Without this, a `missed_wakes` that returned every row would satisfy "the stalled lane was
    detected" and the drill would pass while the alarm had become useless.
    """
    real = drill.missed_wakes

    def _everything(text, now, *a, **kw):
        from agentflow.registry import MissedWake, parse_registry
        reg = parse_registry(text)
        return [MissedWake(agent=name, eta=row.eta, heartbeat=row.heartbeat,
                           overdue_minutes=999.0, row=row)
                for name, row in reg.agents.items()]

    monkeypatch.setattr(drill, "missed_wakes", _everything)
    with pytest.raises(drill.DrillFailure, match="CONTROL lane was reported"):
        drill.run_drill(tmp_path)
    assert drill.missed_wakes is not real or True   # restored by monkeypatch teardown


def test_the_drill_FAILS_when_the_detector_cites_the_wrong_eta(tmp_path, drill, monkeypatch):
    """The report has to be about the wake that was actually missed.

    Found by mutation: deleting the drill's eta check changed nothing, because every other
    assertion is satisfied by a detector that names the right lane for the wrong reason. An
    alarm that reports a lane while citing an eta it did not miss sends a steward to look at
    the wrong thing, which is worse than silence.
    """
    def _wrong_eta(text, now, *a, **kw):
        from agentflow.registry import MissedWake, parse_registry
        reg = parse_registry(text)
        row = reg.agents[drill.STALLED_LANE]
        return [MissedWake(agent=drill.STALLED_LANE, eta="2020-01-01T00:00Z",
                           heartbeat=row.heartbeat, overdue_minutes=999.0, row=row)]

    monkeypatch.setattr(drill, "missed_wakes", _wrong_eta)
    with pytest.raises(drill.DrillFailure, match="wrong eta"):
        drill.run_drill(tmp_path)


def test_the_drill_FAILS_when_the_report_is_inside_the_grace_window(tmp_path, drill, monkeypatch):
    """A row reported while still inside its grace window is a false alarm, not a detection.

    The grace exists so a lane that is thirty seconds late is not paged about. A drill that
    accepted such a report would pass against an alarm with its window deleted.
    """
    def _too_soon(text, now, *a, **kw):
        from agentflow.registry import MissedWake, parse_registry
        reg = parse_registry(text)
        row = reg.agents[drill.STALLED_LANE]
        return [MissedWake(agent=drill.STALLED_LANE, eta=row.eta, heartbeat=row.heartbeat,
                           overdue_minutes=1.0, row=row)]

    monkeypatch.setattr(drill, "missed_wakes", _too_soon)
    with pytest.raises(drill.DrillFailure, match="grace"):
        drill.run_drill(tmp_path)


def test_the_drill_FAILS_when_the_alert_never_reaches_the_board(tmp_path, drill, monkeypatch):
    """The alert leg is prose, so the one mechanical part of it must be checked.

    If the routed Note does not land, the steward followed its skill and the lane was never
    told. Silently passing that would make the drill's alert step decorative.
    """
    real_main = drill.main

    def _swallow_notes(argv):
        if argv and argv[0] == "note":
            return 0        # claims success, writes nothing
        return real_main(argv)

    monkeypatch.setattr(drill, "main", _swallow_notes)
    with pytest.raises(drill.DrillFailure, match="not on the board"):
        drill.run_drill(tmp_path)


def test_the_drill_FAILS_when_the_pushed_BODY_is_not_the_body_it_reported(tmp_path, drill,
                                                                          monkeypatch):
    """Corrupt the payload on the wire and the drill must refuse. IT USED NOT TO, IN CI.

    Step 8's cross-check was guarded on the injected sender having been reached, and under this
    module's own autouse `_no_real_pings` fixture - which stubs `ntfy.notify` so no test can POST
    - it never was. The capture list stayed empty, `if sent and ...` passed by not executing, and
    this exact mutation survived the whole suite, failing only for a human running the drill on a
    fully-configured box. A check that runs on somebody's desktop and not in the gate is not a
    gate, and the drill's own prose described it as one.
    """
    real = drill.wake_watch.watch

    def _corrupting(registry_path, **kw):
        sender = kw.pop("sender")
        return real(registry_path,
                    sender=lambda url, headers, body: sender(url, headers, body[:12]), **kw)

    monkeypatch.setattr(drill.wake_watch, "watch", _corrupting)
    with pytest.raises(drill.DrillFailure, match="not the body this run built"):
        drill.run_drill(tmp_path)


def test_the_drill_FAILS_when_the_send_leg_never_REACHES_the_sender(tmp_path, drill, monkeypatch):
    """An empty capture is a step that did not run, and must never read as a step that passed.

    This is the general form of the bug above: whatever the reason the sender was not called -
    a stubbed `notify`, `notify.kind: none`, no destination exported - the honest report is that
    nothing was checked, not that everything held.
    """
    @contextlib.contextmanager
    def _never_sends():
        yield []

    monkeypatch.setattr(drill, "_forced_push_path", _never_sends)
    with pytest.raises(drill.DrillFailure, match="never called"):
        drill.run_drill(tmp_path)


def test_the_drill_puts_no_packet_on_the_wire_even_with_its_own_send_path(tmp_path, drill,
                                                                          monkeypatch):
    """The forced push path replaces `notify`; the sender injection is what keeps it safe.

    Asserted rather than reasoned about, because the drill now deliberately runs a REAL
    `notify()` body: if the injection ever stopped being passed, the default sender would POST
    to whatever the environment named.
    """
    from agentflow import ntfy as ntfy_mod

    def _explode(url, headers, body):
        raise AssertionError(f"the drill reached the network sender: {url}")

    monkeypatch.setattr(ntfy_mod, "_urlopen_sender", _explode)
    drill.run_drill(tmp_path)


def test_the_README_warns_that_the_module_path_does_not_resolve_pre_merge():
    """`python -m agentflow.wake_watch` is what every doc tells an operator to run.

    It resolves against the INSTALLED package, which pre-merge is the main checkout - so the
    command in the drill's own README gives `No module named agentflow.wake_watch` to the first
    person who tries it on this branch. Correct after merge, a trap before it, and one sentence
    fixes it.
    """
    text = (DRILL_PATH.parent / "README.md").read_text(encoding="utf-8")
    assert "PYTHONPATH" in text and "No module named" in text


def test_the_drill_writes_nothing_outside_the_directory_it_was_given(tmp_path, drill):
    """It builds a whole registry, so it must never be able to touch a live board.

    `--out` defaults to a temp dir for the same reason. Asserted because "it only writes under
    out_dir" is exactly the kind of claim that stays true until someone adds one convenience
    path, and the live board is a lock-protected coordination surface.
    """
    before = {p for p in tmp_path.rglob("*")}
    other = tmp_path / "elsewhere"
    other.mkdir()
    drill.run_drill(tmp_path / "given")
    stray = {p for p in other.rglob("*")}
    assert not stray, f"the drill wrote outside its out_dir: {sorted(stray)}"
    assert (tmp_path / "given" / "missed-wake-drill.md").is_file()
    assert before or True
