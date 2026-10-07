"""``python -m agentflow.wake_watch`` — the missed-wake alarm, run from OUTSIDE the fleet.

THE FAILURE THIS EXISTS FOR, and it is not the one the alarm already covers. `missed_wakes()`
works: it correctly names every `## Agents` row past its own declared `eta`. But every caller of
it lives inside a loop's own sweep, and the steward — the loop that runs the sweep — is one of
the lanes it watches. So the detector's coverage is exactly the set of moments when a loop is
still waking, which is the complement of the set of moments the alarm is for.

Three live instances are on the record. The sharpest is 2026-09-04T18:53Z: **nothing had died.**
The process census was unchanged from the previous sweep, two lanes heartbeated mid-sweep, and
four of seven rows were 74–144 minutes past their etas — the steward's own row among them. The
timers simply stopped. That stall was reported only because a human typed the command. Had they
not, nothing on this machine would have said a word.

**WHAT THIS MODULE IS.** One command that needs no agentflow loop alive, no session, and nothing
running: it reads the registry file through the normal resolver, runs the SAME `missed_wakes()`,
`lapsed_etas()` and `blind_rows()` the sweep runs, and pushes what it finds to ntfy. Point Windows Task
Scheduler at it, point a cron on another host at it, or type it yourself — the command does not
know or care which. **Nothing in THIS FILE registers a scheduler entry or starts a process that
outlives the call**: it is one report-only read. Registering the schedule is the installer's and
the operator's business (see ``docs/INSTALL.md`` and ``docs/REGISTRY.md``, liveness sweep).

**THE SINGLE-DAEMON RULE, and why it replaced the daemon-free invariant.** The ruling, 2026-09-22,
verbatim: *"revoke the rule. Single daemon only is the new rule until it is challenged."* The
suite was daemon-free by design until a quota or rate-limit window proved that posture cannot
detect its own worst failure. Three blackouts — 2026-09-08, 2026-09-16, 2026-09-19 — stopped all
six lanes for days at a time; each was reported by nothing and ended only when a human pressed the
button. The window may be the account's or a single model's (2026-09-22: one lane refused on a
per-model limit, same shape — no tool call, ~630 ms after the fire, one retry ~21 min later).
The argument is structural rather than a gap in the search: an in-fleet detector is itself an
agent firing, the window refuses EVERY model turn it covers — that detector's included — so the
only thing that survives is an invoker that takes no model turn, and every non-model invoker is
a daemon. So the suite now runs **exactly one daemon**: an
OS-scheduled, report-only invocation of this command. The single-daemon budget is one, it is now
spent, and a second daemon is a new decision rather than a precedent this one sets.

**IT STILL NEVER RELAUNCHES ANYTHING**, and that half of the rule did not change — what changed
is *who may invoke it*, never *what it may do*. It reads, it prints, it pushes to ntfy; it never
relaunches, never merges and never writes. A watcher that relaunched on a false positive would
start a second copy of a live lane, and lane duplication is the one thing the registry's
single-writer rule exists to prevent. There is no relaunch arm, no subprocess, and no spawn
anywhere in this file, and a test asserts the absence rather than trusting this paragraph.

**ONE IMPLEMENTATION, TWO CALLERS**, the shape ``agentflow.doctor`` already uses. The rule about
what counts as a missed wake — and, just as importantly, the three exclusions that keep it from
paging for a halt, a reap or a steward ``stand-down`` (the registry verb; a lane that took the
backoff's ``action=stand_down`` is NOT excluded, and stays quiet only by declaring the ``eta=``
that answer carries) — lives in ``registry.missed_wakes()`` and
is not restated, re-derived or re-filtered here. This module resolves a path, calls those
functions, and decides what to say. If the exclusion set ever changes, it changes in one place.

**WHO WATCHES THIS.** Nothing, and that is the honest answer at this depth rather than an
oversight. A watcher for the watcher is the same recursion one level down; what breaks the loop
is that this command holds no state, keeps no timer of its own and can be run by anything — so
its own liveness is the scheduler's problem, and every scheduler worth using reports a job that
stopped firing. What this module CANNOT do is notice that it was never invoked.

**AND IT DOES NOT CONFIRM DELIVERY.** ``ntfy.notify()`` returns ``False`` when it sent nothing
(switched off by ``notify.kind: none``, or no destination in the environment) and ``True`` when
the sender returned, but this module does not read that value: ``pushed`` records that a send
was ATTEMPTED, so a send that quietly went nowhere is indistinguishable here from one that
landed. Only a raising sender is visible. That is stated because a watcher that overstates its
own reach is the failure mode this whole epic is about. And even ``True`` means only that ntfy
accepted the POST, never that a human read it.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Optional

from . import ntfy, registry

_NL = chr(10)

#: Ntfy trigger for a stalled fleet. ESCALATION rather than a new member of ``TriggerType``:
#: its priority is already `high` and its tag already `warning`, and its meaning — a human has
#: to look now — is exactly this event. A dedicated member would buy a nicer banner word at the
#: cost of a change in the notification module for a distinction the title already carries.
_TRIGGER = ntfy.TriggerType.ESCALATION


@dataclass(frozen=True)
class WatchResult:
    """One run's findings, plus what the run actually did about them.

    ``missed`` and ``blind`` are the untouched returns of ``registry.missed_wakes()`` and
    ``registry.blind_rows()`` — carried whole rather than flattened to counts, because a caller
    that only gets numbers cannot name the lane, and naming the lane is the entire product.

    ``pushed`` is *a send was attempted*, never *a human was reached*: see the module docstring
    on why delivery is not observed here. ``findings`` being false with ``pushed`` false
    is the quiet, correct case.
    """

    project: str
    grace_minutes: float
    missed: list
    blind: list
    pushed: bool
    #: Rows past their own eta that ARE still stamping a heartbeat (gh#281) — the untouched
    #: return of ``registry.lapsed_etas()``, carried whole for the same reason as the other two.
    #: A SEPARATE list rather than more ``missed`` rows, because the remedies differ: a missed
    #: wake sends you to probe a lane that may be dead, a lapsed eta sends you to tell a lane
    #: that is demonstrably alive to re-declare. One list would erase that.
    lapsed: list = field(default_factory=list)
    #: The grace ``lapsed`` was computed at. Its own number, NOT ``grace_minutes`` — see
    #: ``registry.LAPSED_ETA_GRACE_MIN``. Carried so ``render`` can state which rope it used.
    lapsed_grace_minutes: float = registry.LAPSED_ETA_GRACE_MIN
    #: How many rows the alarm ACTUALLY JUDGED -- `registry.judged_row_count`, shared with the
    #: in-sweep reporter. Carried because an all-clear is a claim about coverage: a board whose
    #: every lane has been stood down excludes every row, leaves `missed` and `blind` both
    #: empty, and would otherwise report full health having looked at nothing.
    judged: int = 0

    @property
    def findings(self) -> bool:
        """True when this run has something to say. A blind row COUNTS.

        A row the alarm structurally cannot judge is not a row it cleared, and the two silences
        looked identical until ``blind_rows()`` existed. Treating blind rows as "nothing found"
        here would rebuild that conflation one layer up, in the one caller whose whole job is to
        speak when no loop is awake to.

        A LAPSED ETA COUNTS TOO, for the same reason and with more force: that class exists
        because a lane re-stamping a heartbeat while never advancing its eta was reported by
        nothing at any threshold (gh#281). Adding it to the detector and not to this property
        would leave the only unattended reporter computing it and throwing it away.
        """
        return bool(self.missed) or bool(self.lapsed) or bool(self.blind)


def render(result: WatchResult) -> str:
    """The human-readable report — what a scheduler logs and what a person sees on a manual run.

    The per-row wording is the SAME as ``read --missed-wakes`` and is now the same BUILDER —
    ``registry.missed_wake_line`` / ``registry.blind_row_line``, called through the module so a
    test can watch the call land. It used to be the same words by copy-paste while this
    docstring advertised the sameness as something an operator could rely on; a claim two people
    keep in sync by retyping an f-string goes false the first time one of them is edited.

    Only the framing differs: this one names the project, since the thing running it may have no
    idea which board it just read.
    """
    lines = [registry.missed_wake_line(m, result.grace_minutes) for m in result.missed]
    lines.extend(
        registry.lapsed_eta_line(le, result.lapsed_grace_minutes) for le in result.lapsed
    )
    lines.extend(registry.blind_row_line(b) for b in result.blind)
    if not result.missed:
        # The all-clear is QUALIFIED by two things: what could not be checked, and HOW MANY ROWS
        # WERE CHECKED AT ALL -- exactly as the in-sweep report qualifies it. The count is the
        # half that was missing, and it is the load-bearing half: stand every lane down and both
        # finding lists go empty, so an all-clear without the count reports full health over a
        # board where the alarm judged nothing. A false green from the outside watcher is worse
        # than one from the sweep, because nothing else is looking.
        if result.blind:
            lines.append(
                f"# no missed wakes among the {result.judged} checked row(s) on "
                f"{result.project} (grace {result.grace_minutes:g}m) -- but "
                f"{len(result.blind)} row(s) above CANNOT be checked"
            )
        else:
            # THE TAIL IS CONDITIONAL, AND THAT IS THE WHOLE POINT OF THIS BRANCH NOW.
            # "nothing to report and nothing was notified" was unconditionally TRUE while
            # `findings` meant `missed or blind` - this branch was reachable only when both were
            # empty. Adding a THIRD class to `findings` made it reachable on a run that reports a
            # lapsed row AND pushes it, so the one sentence an operator scans in a scheduler log
            # asserted silence two lines under the finding it had just printed. A lapsed-only
            # board is the LITERAL gh#281 shape, so that was the common output of the new class
            # rather than an edge. The count stays unconditional; only the silence claim is
            # qualified, because the count is true either way.
            tail = "" if result.findings else ", nothing to report and nothing was notified"
            lines.append(
                f"# no missed wakes on {result.project} (grace {result.grace_minutes:g}m); "
                f"{result.judged} row(s) checked{tail}"
            )
    if not result.lapsed:
        # ITS OWN ALL-CLEAR, over the SAME judged count, because `lapsed_etas` walks exactly the
        # rows `judged_row_count` counts. A class that prints nothing when it does not fire is
        # indistinguishable from one that was never wired in - which is the shape of the hole
        # gh#281 found, and adding a silent detector would only move it.
        lines.append(
            f"# no lapsed etas on {result.project} "
            f"(grace {result.lapsed_grace_minutes:g}m); {result.judged} row(s) checked"
        )
    if result.findings:
        lines.append(
            "# reported only -- this watcher never relaunches a lane (see wake_watch.__doc__)"
        )
    return _NL.join(lines) + _NL


def push_body(result: WatchResult) -> str:
    """The ntfy body: every finding, one per line, and the no-action disclaimer.

    PUBLIC because it is the only part of the send a caller can actually check. This module
    does not read ``notify()``'s return value, and ``pushed`` records only that a send was
    attempted, so "what would this run have said to a human" is not answerable from the outside
    unless the body is reachable as a value. The drill asserts on it
    for exactly that reason.

    Folded to ASCII at this call site rather than left to ``notify()``'s assertion, and that is
    a deliberate trade. Registry cells are human-typed — a lane's `phase` or `status` can carry
    a typographic dash — and ``notify()`` REFUSES a non-ASCII body rather than mangling it. That
    refusal is right for a build notification and wrong here: an alarm that declines to fire
    because a lane typed an em dash is the alarm being silent at precisely the moment it exists
    for. Lossy is the correct direction when the alternative is mute.
    """
    lines = [f"{len(result.missed)} missed wake(s) on {result.project}:"] if result.missed else []
    lines.extend(registry.missed_wake_line(m, result.grace_minutes) for m in result.missed)
    if result.lapsed:
        lines.append(f"{len(result.lapsed)} lane(s) alive but past an unadvanced eta:")
        lines.extend(
            registry.lapsed_eta_line(le, result.lapsed_grace_minutes) for le in result.lapsed
        )
    if result.blind:
        lines.append(f"{len(result.blind)} row(s) the alarm CANNOT judge:")
        lines.extend(registry.blind_row_line(b) for b in result.blind)
    lines.append("Reported from outside the fleet. Nothing was relaunched.")
    return ntfy.to_ascii(_NL.join(lines))


def watch(
    registry_path: Path,
    *,
    project: str,
    now: Optional[datetime] = None,
    grace_minutes: Optional[float] = None,
    push: bool = True,
    sender: Optional[ntfy.Sender] = None,
    report: Optional[Callable[[WatchResult], None]] = None,
) -> WatchResult:
    """Read the board, run ALL THREE detectors, and push once if any of them found anything.

    ``report`` IS CALLED BEFORE THE PUSH IS ATTEMPTED, ALWAYS, and that ordering is the point of
    its existence rather than a detail of it. ``main()`` used to write the report after
    ``watch()`` returned, so a raising sender — ntfy container down, a DNS blip, a closed port —
    took the findings with it: the watcher computed ``jr-dev-2 MISSED WAKE ... overdue 121m``,
    threw it away, and left stderr carrying a bare ``URLError``. The one moment the push leg is
    broken is the one moment the stdout fallback has to carry something.

    ``now`` is injected for the same reason ``missed_wakes()`` injects it: the boundary is a
    number a test has to be able to sit exactly on. ``sender`` is the injection point
    ``agentflow.ntfy`` already defines, so the drill and the suite exercise the real push path
    without a packet leaving the machine.

    Reads through ``registry._read_registry_text``, not ``Path.read_text``: that function turns
    an absent, truncated, or wrongly-pointed registry into a documented refusal instead of an
    OSError, and this caller wants exactly that distinction — see ``main()`` on why a broken
    watcher and a stalled fleet must never produce the same signal.

    ONE push per run, carrying every finding, rather than one per row: a scheduler firing this
    every 30 minutes against four stalled lanes would otherwise send four notifications an hour
    apiece, and an alarm that floods is an alarm that gets muted.
    """
    now = now or datetime.now(UTC)
    grace = registry.MISSED_WAKE_GRACE_MIN if grace_minutes is None else grace_minutes
    text = registry._read_registry_text(registry_path)
    reg = registry.parse_registry(text)
    # All of them, always, and NONE re-filtered here. `missed_wakes` answers "did a
    # promised wake fire"; `blind_rows` answers "which rows could that question not even be
    # asked of". Calling them through the module rather than importing the names is what makes
    # the reuse checkable: a test can replace `registry.missed_wakes` and see this call land on
    # it, which a `from .registry import missed_wakes` would have hidden.
    # `lapsed_etas` joins them on the same terms and for the same reason: it answers "is this
    # lane still saying when it will wake next", which neither of the other two asks, and this
    # watcher is the only thing reporting at all when no loop is awake. Its grace is its own
    # constant, passed explicitly rather than defaulted, so the number this run used is visible
    # in the result and not implied by an import.
    lapsed_grace = registry.LAPSED_ETA_GRACE_MIN
    result = WatchResult(
        project=project,
        grace_minutes=grace,
        missed=registry.missed_wakes(reg, now, grace),
        blind=registry.blind_rows(reg),
        pushed=False,
        lapsed=registry.lapsed_etas(reg, now, lapsed_grace),
        lapsed_grace_minutes=lapsed_grace,
        judged=registry.judged_row_count(reg),
    )
    # The findings leave this function before anything that can fail is attempted.
    if report is not None:
        report(result)
    if not (push and result.findings):
        return result
    ntfy.notify(
        _TRIGGER,
        # FOLDED, for the same reason `detail` is folded one call down and stated here because
        # the two fields sat one line apart with only one of them protected. `--project` is
        # slug-guarded, but `_resolve_project` DERIVES the project from a directory name when
        # `--project` is absent, and a directory name is not constrained: one non-ASCII
        # character there raised NtfyAsciiError, which `main()` turned into exit 1 with no push
        # and no report on a genuinely stalled fleet -- the alarm silenced by typography, which
        # is precisely what the fold on `detail` exists to prevent.
        epic=ntfy.to_ascii(f"{project} missed-wakes"),
        detail=push_body(result),
        sender=sender,
    )
    return WatchResult(
        project=result.project,
        grace_minutes=result.grace_minutes,
        missed=result.missed,
        blind=result.blind,
        pushed=True,
        lapsed=result.lapsed,
        lapsed_grace_minutes=result.lapsed_grace_minutes,
        judged=result.judged,
    )


def _resolve(args: argparse.Namespace) -> tuple[Path, str]:
    """Registry path and project name, resolved THE WAY EVERY OTHER COMMAND RESOLVES THEM.

    ``registry._resolve_registry_path`` is reached rather than reimplemented, so the precedence
    and the rules on ``--project`` cannot drift into a second, subtly different answer. That
    precedence (gh#715): ``--file`` wins outright; otherwise ``--project`` - a bare slug that
    must name a configured project - resolves under ``data_root``, and ``$AGENTFLOW_REGISTRY``
    is used only when no ``--project`` is given. Given both, the env var does NOT win: the two
    must name the same board or the command refuses (``registry_board_conflict``).
    ``wiki._resolve_wiki_root`` holds to the same rules (registry's slug regex, the same
    configured-project check) for the same reason: a watcher pointed at a different board than
    the fleet writes to would report an all-clear about a registry nobody uses.
    """
    path = registry._resolve_registry_path(args)
    return path, registry._resolve_project(args, path)


def main(argv: Optional[list[str]] = None) -> int:
    """Run the watcher once. Exit code reports whether the WATCHER could do its job.

    THE EXIT CODE IS NOT A VERDICT ON THE FLEET, and a scheduler that reads only the exit code
    is not monitoring anything. A scheduler has exactly one failure channel, and if a found-rows
    result shared it with a crashed run then "the watcher is broken" and "the fleet is stalled"
    would arrive as the same signal — the precise confusion this whole epic exists to remove.
    That design stands. So:

      * 0 — the watcher did its job. Findings or none; findings travel on stdout and by ntfy.
      * 1 — the watcher could not do its job: registry unreadable, config unresolvable, or the
        push itself failed. Nothing is being claimed about the fleet.

    **BE PRECISE ABOUT WHAT THAT SECOND LINE ADMITS, because an earlier version of this
    docstring claimed an absolute the code does not deliver.** "Could not do its job" INCLUDES
    delivery, and a push is only attempted when there is something to push — so with a broken
    ntfy this command exits 0 on a healthy board and 1 on a board with findings. The exit code
    is therefore *weakly correlated* with fleet state through the delivery leg, and a scheduler
    that inferred "1 means findings" would be right often enough to be dangerous and wrong the
    first time the registry moved.

    The operative rule follows from that, and it is the one an operator has to wire: **CAPTURE
    STDOUT AND STDERR, NOT ONLY THE EXIT CODE.** The report is written to stdout BEFORE any push
    is attempted, so it survives a delivery fault; ``notify()`` prints its own warning to stderr
    when notifications are on but nothing is configured, and this command does not read
    ``notify()``'s return value (``pushed`` means a send was attempted), so a green exit with an
    unconfigured channel is a silence that looks exactly like health.
    """
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    ap = argparse.ArgumentParser(
        prog="python -m agentflow.wake_watch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Report lanes past their own declared eta, from outside any agentflow loop. "
            "Reports only; never relaunches anything."
        ),
        epilog=(
            "CAPTURE STDOUT AND STDERR, NOT ONLY THE EXIT CODE." + _NL +
            "  The exit code says whether THIS COMMAND could do its job, never what it found:"
            + _NL +
            "  0 = it ran (findings or none), 1 = it could not run OR could not deliver." + _NL +
            "  The findings are written to stdout before any push is attempted, so they" + _NL +
            "  survive an ntfy outage. A misconfigured channel prints a warning to stderr" + _NL +
            "  and still exits 0, so a job wired to the exit code alone reports permanent" + _NL +
            "  health over a fleet nobody is watching." + _NL +
            "  Delivery is not observed here: a push counts as attempted, not delivered."
        ),
    )
    ap.add_argument("--project", default=None, help="project whose registry to read")
    ap.add_argument("--file", default=None, help="explicit registry.md path (wins over --project)")
    ap.add_argument(
        "--grace-minutes", type=float, default=None,
        help=f"minutes past a row's own eta before the wake counts as missed "
             f"(default {registry.MISSED_WAKE_GRACE_MIN:g})",
    )
    ap.add_argument(
        "--no-notify", action="store_true",
        help="print the report and send nothing -- for a dry run against a live board",
    )
    args = ap.parse_args(argv)

    try:
        path, project = _resolve(args)
        # The report is handed to `watch` as a callback rather than written from the return
        # value, and that is the whole fix for "findings discarded when the push fails": a
        # sender that raises now happens AFTER stdout already carries every finding, so the
        # exit-1 line on stderr sits under a readable report instead of replacing one.
        watch(
            path,
            project=project,
            grace_minutes=args.grace_minutes,
            push=not args.no_notify,
            report=lambda r: sys.stdout.write(render(r)),
        )
    except Exception as exc:  # noqa: BLE001 - deliberately not narrower, and NOT swallowed.
        # Everything from here can fail in a way this command has to survive reporting: a
        # RegistryError on an unreadable board, a ConfigError or OSError out of the resolver, an
        # ntfy sender raising on a wedged host. They are caught in ONE place so that every one
        # of them lands on the single documented meaning of exit 1, instead of some escaping as
        # a traceback and some as a code the caller's table does not admit exists. The exception
        # text is printed, so nothing is hidden -- what is refused is a traceback as the output
        # of a tool a scheduler reads.
        sys.stderr.write(f"wake-watch FAILED to run: {type(exc).__name__}: {exc}{_NL}")
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover — thin CLI shim
    raise SystemExit(main())
