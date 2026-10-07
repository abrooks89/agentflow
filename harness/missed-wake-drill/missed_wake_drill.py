#!/usr/bin/env python
"""The missed-wake alarm's done-condition, run for real rather than asserted.

WHY THIS EXISTS. `missed-wake-alarm` defined done as *"a deliberate stall of a test lane past
its declared `eta` produces an alert, with nobody at a terminal."* That was never exercised. A
repo-wide grep for `stall drill` / `deliberate stall` / `unattended alert` returned nothing, and
the shipping PR's verification listed mutation kills and passing tests but no stall. So the one
thing the fleet uses to watch itself carried more assurance than it had earned.

This drill stalls a lane and walks the whole path the steward walks:

  1. build a board where one lane's declared `eta` has lapsed past the grace window and whose
     heartbeat predates that eta - the exact shape of 2026-08-29, when every process stayed
     alive and no timer fired;
  2. run the DETECTOR the steward runs (`read --missed-wakes`), and read its output;
  3. drive the ALERT leg the steward's prose specifies - a routed Note at the stalled lane -
     through the mediated CLI, and confirm it landed on the board;
  4. write a record a later audit can cite, naming the versions and the clock it used.

THEN IT WALKS THE PATH NO STEWARD CAN, which is why steps 5-8 exist. The detector above runs
inside the steward's own sweep, and the steward is one of the lanes it watches: on
2026-09-04T18:53Z four of seven rows were 74-144 minutes overdue with the steward's own row
among them, nothing had died, the timers had simply stopped - and a human typing a command is
the only reason it was ever reported. `agentflow.wake_watch` is the outside watcher that closes
that, and these steps exercise it:

  5. run the watcher **in a separate interpreter with no agentflow loop in it**, against the
     same stalled board, and require it to name the stalled lane and exit 0;
  6. stall the **steward's own row** and require the watcher to name it - the one case no
     in-sweep detector can reach, because the sweep is the thing that stopped;
  7. stand a lane down through the real verb, give it the SAME lapsed cells as the stalled lane,
     and require the watcher to stay silent about it - a documented halt ages forever and is
     correct;
  8. drive the watcher's real push path with an injected sender, and read back the payload it
     would have sent - comparing the bytes that reached the sender against the body the run
     reported. That comparison is UNCONDITIONAL and runs in CI: it used to be guarded on the
     sender having been reached at all, which under a suite that stubs `ntfy.notify` it never
     was, so a corrupted payload passed every build (see `_forced_push_path`).

WHAT IT DOES NOT PROVE, said here rather than left for an auditor to discover:

  * **"Outside a loop session" is proven as far as a process boundary reaches, and no further.**
    Step 5 runs the watcher in a fresh interpreter whose only inputs are the registry file and
    the config - no sweep, no loop, no session object. What a drill cannot assert is that the
    MACHINE has no agentflow session open at the time; that is a property of the operator's
    desktop, not of this code. The claim earned here is that the watcher needs nothing alive.
  * **No packet leaves the machine, so delivery is not proven.** Step 8 injects a sender and
    inspects the payload. `ntfy.notify()` returns True only when the sender returned without
    raising, and even then it means ntfy accepted the POST, not that a human read it - so an
    end-to-end delivery claim is not available to any caller. See `agentflow.wake_watch.__doc__`.
  * **The drill schedules nothing, and cannot see whether anything else did.** The watcher is a
    command; invoking it on a cadence is the suite's one permitted daemon (the single-daemon
    rule, 2026-09-22) and lives in an OS scheduler entry, not in here. This drill registers none
    and reads none. A watcher that is never invoked is silent, and no test in this file can see
    that.
  * **The watcher never relaunches a lane, and this drill does not test that it could.** That is
    the LOCKED decision on the epic and the 2026-09-22 revocation did not touch it: the
    single-daemon budget buys a scheduled REPORTER, and it is already spent. Notify costs one
    push; relaunch costs the single-writer rule, because relaunching on a false positive starts a
    second copy of a live lane.

Usage:
  python harness/missed-wake-drill/missed_wake_drill.py [--out <dir>]

Exits 0 iff every step passed. Writes `<out>/missed-wake-drill.md`; `--out` defaults to a temp
directory, so a bare run never touches the live board.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[2] / "skills" / "_shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from agentflow import ntfy, wake_watch  # noqa: E402  (path set above on purpose)
from agentflow.registry import (  # noqa: E402  (path set above on purpose)
    MISSED_WAKE_GRACE_MIN,
    main,
    missed_wakes,
)

NL = chr(10)
STALLED_LANE = "jr-dev-2"
LIVE_LANE = "auditor"
#: The lane the outside watcher exists for. Not a parameter and not a preference: the steward is
#: the loop that runs the in-sweep detector, so when the steward is what stopped there is nothing
#: left inside the fleet to notice. Every other lane has a watcher; this one only has this one.
WATCHER_BLIND_LANE = "steward"
#: A lane a human deliberately stopped. Given the SAME lapsed cells as STALLED_LANE on purpose -
#: if the exclusion ever stops working, the two rows are indistinguishable on timing and the
#: watcher names both, which is exactly the false page `stand-down` was built to end.
STOOD_DOWN_LANE = "jr-dev-1"


class DrillFailure(RuntimeError):
    """A step did not hold. Carries the sentence a reader needs, not a traceback."""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _stall_the_row(path: Path, agent: str, heartbeat: str, eta: str) -> None:
    """Rewrite one Agents row's heartbeat and eta directly.

    Directly, because the CLI cannot produce this row: every `status` write stamps
    `heartbeat=now`, so a row written through the CLI always proves its own wake fired. Staging
    the stall through `status` would produce a green that means nothing - which an earlier
    version of the alarm's own tests did, and said so.
    """
    lines = path.read_text(encoding="utf-8").split(NL)
    hit = False
    for i, line in enumerate(lines):
        if line.startswith(f"| {agent} |"):
            cells = line.split("|")
            cells[5] = f" {heartbeat} "
            cells[6] = f" {eta} "
            lines[i] = "|".join(cells)
            hit = True
    if not hit:
        raise DrillFailure(f"no Agents row for {agent!r} to stall - the board shape changed")
    path.write_text(NL.join(lines), encoding="utf-8")


def _run_watcher_outside(reg: Path, project: str) -> tuple[int, str, str]:
    """Run `python -m agentflow.wake_watch` in a SEPARATE INTERPRETER and return (code, out, err).

    A subprocess rather than a function call, and the distinction is the whole reason steps 5-7
    exist. Calling `wake_watch.watch()` in-process would prove the detector works - which step 2
    already proves - while leaving untouched the claim actually at issue: that the alarm can fire
    with no loop, no sweep and no session anywhere in the interpreter running it. Only a fresh
    process establishes that, because only a fresh process starts from nothing.

    `--no-notify` because a drill must never push to a real topic, and `--file` because the drill
    board is a temp directory no config knows about. PYTHONPATH carries the tree this drill
    already imported from, so the subprocess reads the SAME `wake_watch` rather than whatever an
    editable install happens to point at.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(_SHARED) + (os.pathsep + existing if existing else "")
    proc = subprocess.run(
        [sys.executable, "-m", "agentflow.wake_watch",
         "--project", project, "--file", str(reg), "--no-notify"],
        capture_output=True, text=True, env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


#: Env-var NAMES the forced push path below owns. Deliberately not NTFY_URL/NTFY_TOPIC: a fleet
#: shell exports those, and a drill must never read, overwrite or depend on the operator's real
#: destination. The sender is injected regardless, so neither name ever reaches a socket.
_DRILL_URL_ENV = "AGENTFLOW_DRILL_NTFY_URL"
_DRILL_TOPIC_ENV = "AGENTFLOW_DRILL_NTFY_TOPIC"


@contextlib.contextmanager
def _forced_push_path():
    """Make step 8's send ACTUALLY REACH the injected sender, whatever the box or the suite says.

    THE CHECK THIS EXISTS FOR WAS DEAD IN CI, and that is worth stating plainly because the
    drill's own prose claimed it was running. Step 8 compares the bytes that reached the sender
    against `push_body`'s output. Three ordinary conditions stop the sender from ever being
    called: a suite that stubs `ntfy.notify` for the whole session (`test_missed_wake_drill.py`
    does, and must), an operator config carrying `notify.kind: none`, and no destination exported.
    In all three the capture list stays empty, a guarded `if sent:` comparison passes by not
    executing, and a mutation that corrupts the payload on the wire survives the entire CI suite
    while failing only for a human running the drill on a fully-configured box. A check that runs
    on somebody's desktop and not in the gate is not a gate.

    So the drill supplies its own send path rather than borrowing the process's: a FRESH import of
    `agentflow.ntfy`, unpatched by construction — which is what makes this independent of import
    order against anybody's fixture — with its config resolution pinned to two env names this
    module owns. Everything else about the leg is real: the same `notify()` body, the same ASCII
    gate, the same header assembly.

    **No packet can leave the machine.** `notify()` uses the injected sender when one is passed
    and `_urlopen_sender` only when one is not, and step 8 always passes one — so the network path
    is unreachable for the duration, and the real `ntfy.notify` and the two env vars are restored
    on the way out whether the step passed or raised.
    """
    spec = importlib.util.find_spec("agentflow.ntfy")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)          # NOT registered in sys.modules: nothing else sees it
    # The fresh module defines its own `TriggerType`, and an enum member is identified by its
    # class - so `_PRIORITY[trigger]` would miss on the trigger `wake_watch` passes, which comes
    # from the ORIGINAL module, and `_headers_for` would raise "missing a _PRIORITY/_TAGS entry".
    # The lookup tables are shared rather than duplicated: the send path under test is the real
    # one, and only the config resolution is pinned.
    fresh.TriggerType = ntfy.TriggerType
    fresh._PRIORITY = ntfy._PRIORITY
    fresh._TAGS = ntfy._TAGS
    fresh._resolved_notify = lambda: fresh._ResolvedNotify(
        "ntfy", _DRILL_URL_ENV, _DRILL_TOPIC_ENV, True, None
    )
    saved_notify = ntfy.notify
    saved_env = {name: os.environ.get(name) for name in (_DRILL_URL_ENV, _DRILL_TOPIC_ENV)}
    os.environ[_DRILL_URL_ENV] = "http://missed-wake-drill.invalid"
    os.environ[_DRILL_TOPIC_ENV] = "missed-wake-drill"
    ntfy.notify = fresh.notify
    sent: list[tuple[str, dict, bytes]] = []
    try:
        yield sent
    finally:
        ntfy.notify = saved_notify
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_drill(out_dir: Path, now: datetime | None = None) -> str:
    """Run every step. Returns the record text; raises DrillFailure on the first step that fails."""
    now = now or datetime.now(UTC)
    root = out_dir / "drill-board"
    reg = root / "projects" / "demo" / "status" / "registry.md"
    lock = out_dir / "locks"
    lock.mkdir(parents=True, exist_ok=True)
    base = ["--file", str(reg), "--lock-dir", str(lock), "--project", "demo"]

    if main(["init", *base]) != 0:
        raise DrillFailure("could not initialize the drill board")

    # --- 1. two lanes declare a next wake; one of them then stalls -------------
    # FRESH UNDER BOTH CLOCKS, and that is not belt-and-braces. Steps 2-4 judge the board against
    # the INJECTED `now`, but step 5 runs the watcher in a separate interpreter that can only read
    # the real one. With `now` injected in the past - which the suite does, to make the record's
    # eta and lag deterministic - `now + 1h` is an eta already lapsed by the time the subprocess
    # reads it, while `status` stamped the control lane's heartbeat from the real clock. That is
    # the gh#281 shape exactly: alive, past an eta it never advanced. The control lane was
    # therefore reported by the outside watcher and the drill failed on its own fixture.
    fresh_eta = _iso(max(now, datetime.now(UTC)) + timedelta(hours=1))
    for lane in (STALLED_LANE, LIVE_LANE):
        if main(["status", *base, "--actor", lane, "--state", "building",
                 "--phase", "drill", "--eta", fresh_eta]) != 0:
            raise DrillFailure(f"{lane} could not declare an eta")

    overdue_min = MISSED_WAKE_GRACE_MIN * 4
    lapsed_eta = _iso(now - timedelta(minutes=overdue_min))
    _stall_the_row(reg, STALLED_LANE, heartbeat=_iso(now - timedelta(minutes=overdue_min + 60)),
                   eta=lapsed_eta)

    # --- 2. the DETECTOR, exactly as the steward runs it ----------------------
    text = reg.read_text(encoding="utf-8")
    found = missed_wakes(text, now)
    names = [m.agent for m in found]
    if STALLED_LANE not in names:
        raise DrillFailure(
            f"the stalled lane was NOT detected: missed_wakes reported {names or 'nothing'} "
            f"for a row {overdue_min:.0f} minutes past its own eta"
        )
    if LIVE_LANE in names:
        raise DrillFailure(
            f"the CONTROL lane was reported too ({names}) - a detector that names every row "
            "proves nothing about the one that stalled"
        )
    hit = next(m for m in found if m.agent == STALLED_LANE)
    if hit.eta != lapsed_eta:
        raise DrillFailure(f"the report cites the wrong eta: {hit.eta!r} != {lapsed_eta!r}")
    if hit.overdue_minutes < MISSED_WAKE_GRACE_MIN:
        raise DrillFailure(
            f"overdue_minutes={hit.overdue_minutes:.0f} is inside the {MISSED_WAKE_GRACE_MIN:.0f}"
            "-minute grace, so this row should never have been reported at all"
        )

    # --- 3. the ALERT leg the steward's prose specifies -----------------------
    alert = (f"missed-wake alarm: your declared eta {lapsed_eta} lapsed "
             f"{hit.overdue_minutes:.0f}m ago with no heartbeat since. Ping on first sight.")
    if main(["note", *base, "--actor", "steward", "--to", STALLED_LANE, "--text", alert]) != 0:
        raise DrillFailure("the steward could not write the alert Note")
    board = reg.read_text(encoding="utf-8")
    if f"steward -> {STALLED_LANE}: missed-wake alarm" not in board:
        raise DrillFailure("the alert Note is not on the board after a successful write")

    # --- 5. the OUTSIDE watcher, in an interpreter with no loop in it -----------
    code, out, err = _run_watcher_outside(reg, "demo")
    if code != 0:
        raise DrillFailure(
            f"the outside watcher exited {code} on a board it read successfully; the exit code "
            f"means THE WATCHER IS BROKEN and must never carry a finding. stderr: {err.strip()!r}"
        )
    if STALLED_LANE not in out:
        raise DrillFailure(
            f"the outside watcher did not name {STALLED_LANE} on a board where the in-sweep "
            f"detector did. Output was: {out.strip()!r}"
        )
    if LIVE_LANE in out:
        raise DrillFailure(
            f"the outside watcher named the CONTROL lane {LIVE_LANE}; a watcher that names every "
            "row is a watcher nobody reads twice"
        )

    # --- 6. the steward's OWN row - the case no in-sweep detector can reach -----
    # Stalled with the same cells as every other stalled row. The point is not that the steward
    # is special to the detector (it is not); it is that when THIS row is the one that stopped,
    # the sweep that would have reported it is the thing that stopped.
    _stall_the_row(reg, WATCHER_BLIND_LANE,
                   heartbeat=_iso(now - timedelta(minutes=overdue_min + 60)), eta=lapsed_eta)
    code, out, err = _run_watcher_outside(reg, "demo")
    if code != 0:
        raise DrillFailure(f"the outside watcher exited {code} reading a stalled steward: {err!r}")
    if WATCHER_BLIND_LANE not in out:
        raise DrillFailure(
            f"the outside watcher did NOT name {WATCHER_BLIND_LANE} when {WATCHER_BLIND_LANE} was "
            "the stalled lane - which is the entire reason this watcher exists. Output: "
            f"{out.strip()!r}"
        )

    # --- 7. a lane halted by design must stay unreported ------------------------
    # Stand it down through the REAL verb, then give it the stalled lane's exact cells. Written
    # in that order deliberately: `stand-down` preserves heartbeat and eta, so the row ends up
    # timing-identical to a row the watcher just alarmed on, and the ONLY thing keeping it quiet
    # is the exclusion. Restating the exclusion here instead of exercising it would prove nothing
    # about the code that decides.
    if main(["stand-down", *base, "--actor", "steward", "--agent", STOOD_DOWN_LANE,
             "--reason", "drill: stopped on purpose, must not page"]) != 0:
        raise DrillFailure(f"could not stand {STOOD_DOWN_LANE} down through the CLI")
    _stall_the_row(reg, STOOD_DOWN_LANE,
                   heartbeat=_iso(now - timedelta(minutes=overdue_min + 60)), eta=lapsed_eta)
    code, out, err = _run_watcher_outside(reg, "demo")
    if code != 0:
        raise DrillFailure(f"the outside watcher exited {code} on the stood-down board: {err!r}")
    if STOOD_DOWN_LANE in out:
        raise DrillFailure(
            f"the outside watcher PAGED for {STOOD_DOWN_LANE}, a lane a human deliberately "
            "stopped. A documented halt ages forever and is correct; every false page on a "
            f"deliberate act teaches the reader to discount the alarm. Output: {out.strip()!r}"
        )
    if STALLED_LANE not in out or WATCHER_BLIND_LANE not in out:
        raise DrillFailure(
            "the exclusion silenced more than the stood-down row - the two genuinely stalled "
            f"lanes must still be named. Output: {out.strip()!r}"
        )

    # --- 8. the push leg, with the sender injected -----------------------------
    # The one limb that cannot be a subprocess: proving the alert leaves the module means reading
    # the payload; `ntfy.notify()` reports only whether a send was made, not what it said.
    # The sender injection point `agentflow.ntfy` already defines is how this is read without
    # a packet on the wire.
    with _forced_push_path() as sent:
        watched = wake_watch.watch(
            reg, project="demo", now=now,
            sender=lambda url, headers, body: sent.append((url, headers, body)),
        )
    if not watched.pushed:
        raise DrillFailure(
            "the watcher found stalled lanes and reported pushing nothing - findings that do not "
            "leave the machine are the exact failure this watcher was built for"
        )
    payload = wake_watch.push_body(watched)
    for lane in (STALLED_LANE, WATCHER_BLIND_LANE):
        if lane not in payload:
            raise DrillFailure(f"the alert body does not name {lane}: {payload!r}")
    if STOOD_DOWN_LANE in payload or LIVE_LANE in payload:
        raise DrillFailure(f"the alert body names a lane it should not: {payload!r}")
    # UNCONDITIONAL, and it used to be `if sent and ...`. The guard was the bug: under the CI
    # suite the sender was never reached, so the comparison never ran and a corrupted payload
    # passed the build. `_forced_push_path` is what makes requiring it safe everywhere.
    if not sent:
        raise DrillFailure(
            "the watcher reported a push and the injected sender was never called - the send leg "
            "did not run, so nothing here checked what would have gone on the wire"
        )
    if sent[0][2].decode("ascii") != payload:
        raise DrillFailure(
            "the body that reached the sender is not the body this run built - the push and the "
            "report would be describing different boards"
        )

    # --- 9. the record ---------------------------------------------------------
    record = NL.join((
        "# Missed-wake alarm: stall drill",
        "",
        f"- **Run at:** {_iso(now)} (clock injected, not read inside the detector)",
        f"- **Grace window:** {MISSED_WAKE_GRACE_MIN:.0f} minutes",
        f"- **Stalled lane:** `{STALLED_LANE}` - declared eta {lapsed_eta}, heartbeat older still",
        f"- **Overdue when detected:** {hit.overdue_minutes:.0f} minutes",
        f"- **Control lane:** `{LIVE_LANE}` - eta {fresh_eta}, correctly NOT reported",
        "- **Alert leg (in-sweep):** steward routed a Note at the stalled lane; verified on the"
        " board",
        f"- **Outside watcher:** `python -m agentflow.wake_watch`, run in a separate interpreter"
        f" with no loop in it; named `{STALLED_LANE}`, exited 0",
        f"- **Self-blind case:** `{WATCHER_BLIND_LANE}` stalled with the same cells and named by"
        " the outside watcher - the row no in-sweep detector can reach",
        f"- **Halted by design:** `{STOOD_DOWN_LANE}` stood down through the real verb, then given"
        f" `{STALLED_LANE}`'s exact lapsed cells; correctly NOT reported",
        "- **Alert body:** built from the findings and asserted, and the bytes that reached the"
        " injected sender compared against it; the sender injection is what makes it impossible"
        " for a drill run to put a packet on the wire",
        "",
        "## What this establishes",
        "",
        "The detector fires on a lane that declared a wake and did not take it, names that lane",
        "and no other, and cites the eta it missed. The alert a steward is instructed to send",
        "reaches the board.",
        "",
        "And the part the in-sweep detector structurally cannot reach: a watcher running in an",
        "interpreter with no agentflow loop in it reads the same board, reuses the same",
        "`missed_wakes()` and the same exclusions, names the stalled steward, stays silent about",
        "the lane a human stopped on purpose, and builds an ntfy body carrying exactly the lanes",
        "it named. Its exit code says whether THE WATCHER could do its job -- delivery included --",
        "never what it found, and the findings are on stdout before any push is attempted.",
        "",
        "## What it deliberately does not establish",
        "",
        "1. **The drill schedules nothing, and cannot see whether anything else did.** The",
        "   watcher is a command; invoking it on a cadence is the suite's one permitted daemon",
        "   (the single-daemon rule, 2026-09-22) and lives in an OS scheduler entry. This drill",
        "   registers none and reads none. A watcher nobody invokes is silent, and no limb here",
        "   can see that.",
        "2. **`--no-notify` and an injected sender mean no packet left the machine**, so delivery",
        "   is unproven. Even a True from `notify()` means only that ntfy accepted the POST, not",
        "   that a human read it, so no caller anywhere can prove delivery today.",
        "3. **Outside a loop is proven to a process boundary and no further.** The subprocess has",
        "   no sweep, no loop and no session in it. Whether the MACHINE had an agentflow session",
        "   open while this ran is a property of the desktop, not of this code.",
        "4. **The watcher never relaunches a lane, and this drill does not test that it could.**",
        "   That is the LOCKED decision on the epic, and the 2026-09-22 revocation of the",
        "   daemon-free invariant did not touch it: the single-daemon budget buys a scheduled",
        "   REPORTER and is already spent. Notify costs one push; relaunch costs the",
        "   single-writer rule, because relaunching on a false positive starts a second copy of",
        "   a live lane.",
        "",
        "Recorded here so a later audit cites the limits rather than rediscovering them.",
        "",
    ))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "missed-wake-drill.md").write_text(record, encoding="utf-8")
    return record


def main_cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="missed_wake_drill.py", description=__doc__)
    ap.add_argument("--out", default=None,
                    help="directory for the board and the record (default: a temp dir)")
    args = ap.parse_args(argv)
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="missed-wake-drill-"))
    try:
        record = run_drill(out)
    except DrillFailure as exc:
        sys.stderr.write(f"DRILL FAILED: {exc}{NL}")
        return 1
    sys.stdout.write(record)
    sys.stdout.write(f"{NL}# record written: {out / 'missed-wake-drill.md'}{NL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
