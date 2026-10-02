"""Fleet-wide movement detection and the exponential wake backoff it drives (item 4).

WHAT THIS IS FOR, measured rather than argued. Between two account-limit blackouts the six
lanes logged 523 scheduled firings and roughly 8,025 assistant turns across 3.7 days while the
steward measured 0 claimable rows for 62 consecutive wakes and master did not move for 70
hours. The fleet spent a weekly account allowance re-deriving an unchanged board. Item 2's
recovery cron gets the fleet moving again AFTER a limit; this module is the other half - it
stops the fleet walking into one by making a wake's cost fall as the board's productivity does.

THE ONE RULING THIS ENCODES, 2026-09-22: *"4a with b"* - exponential, keyed on whether
anything moved **fleet-wide** (master sha, queue rows) rather than on each lane's local cadence,
plus a hard floor dropping all lanes to one wake every N hours once the board is provably frozen.
"Fleet-wide" is the load-bearing word and it is why the observation log below is shared rather
than per-lane: a jr-dev that personally has nothing to do is not evidence the board is frozen,
and six lanes each backing off on their own private idleness would back off on a board that is
moving under them.

THREE-VALUED, AND THAT IS THE WHOLE SAFETY OF IT. A movement probe has three answers, not two:
it moved, it did not move, and *I could not tell*. Collapsing the third into the second is the
specific defect this module is built around, because the cost is asymmetric and silent - a probe
that reads its own failure as stillness makes every lane back off to the floor on a perfectly
healthy board, and the fleet looks calm rather than broken. ``refs.local_tracking_refs`` is the
live instance: it returns ``[]`` both when a checkout genuinely has no tracking refs and when the
``git`` call failed, so this module maps that ``[]`` to UNKNOWN and never to a value. Same rule
for the queue: ``None`` means "the survey did not answer", while an empty sequence is a real,
known, *zero* - the 62-wake measurement above is exactly a run of known-empty queue
observations, and it must be able to drive backoff.

UNKNOWN DOES NOT EXTEND BACKOFF, AND IT DOES NOT PRESERVE IT EITHER. ``still_streak`` counts
backwards from the newest observation and stops at the first record that is not a STILL - an
UNKNOWN, a MOVED, or a line it cannot parse all end the run. So a probe that breaks returns the
fleet to base cadence rather than freezing it at the floor. That is deliberately the expensive
direction: waking too often on a broken probe costs turns, while sleeping five hours on a board
nobody can see costs the epic that was ready the whole time.

WHY THERE IS NO PR SIGNAL, AND WHY NOBODY SHOULD ADD ONE BACK FOR SYMMETRY. An earlier shape of
this module read merged pull requests as a third signal. It was removed on 2026-09-22 and the
reasoning is structural rather than a preference. A merged PR necessarily moves master, and the
``refs`` signal reads every remote-tracking ref, master's included, from a local read - so the
PR leg could only ever confirm something the cheaper signal reports once that ref is fetched. What it cost was a
``gh pr list`` network round-trip per lane per wake: six network calls an hour, added to the exact
per-wake spend this epic exists to reduce. It also had a second failure mode of its own - an
unreachable tracker made ``prs`` unreadable on EVERY observation, so ``compare`` returned UNKNOWN
forever and the backoff never engaged at all on a fleet with no tracker configured. Both
remaining signals are local reads. Keep it that way: a signal is worth its cost only when it can
say something the others cannot.

THE GAP THAT TRADE LEAVES, stated because the local read hides it. ``refs`` reads the checkout's
remote-tracking refs, which are only as fresh as its last ``git fetch``, and no builder or auditor
idle path fetches before calling this module. A merge made on the host that no lane has fetched
yet - a human merging an awaiting-merge PR is the ordinary case - leaves every tracking ref where
it was, so it reads as STILL and counts toward the floor on a board that moved. Whatever fetches the checkout
next (a builder creating a worktree, the steward's sweep) closes the gap, and the next observation
reads MOVED. Adding a fetch to the idle path would close it sooner at the cost of a network call
per lane per wake; that is the operator's trade, and this module does not make it.

WHAT A SINGLE WAKE CAN ACTUALLY HONOUR - AND WHY THERE ARE THREE OUTCOMES, NOT TWO.
``ScheduleWakeup`` is clamped by the harness into ``[WAKE_DELAY_MIN_S, WAKE_DELAY_MAX_S]`` =
[60, 3600] and *reports nothing when it clamps*. Read that together with the base cadence, which
is 3600, and the consequence is total: **no lane can ever sleep longer than an hour by
scheduling**. A backoff that only ever returns "schedule this many seconds" is therefore inert by
construction - every delay it can express is the hour the lanes already took - while still paying
a marker observation per lane per wake. That was the first shipped shape of this module and it
added cost and reduced nothing.

So a floored lane does not schedule a longer wake. It schedules NOTHING, and lets the longer
clock that already exists bring it back: item 2's standing five-hour ``--recovery-guard``
``CronCreate``. That is why the floor is five hours and the guard window is five hours - the two
numbers are the same number, deliberately, because the guard IS the floored lane's wake. A floor
the harness cannot honour is not a floor, so the only reading of ruling 4b that is
achievable at all is this one.

``plan_wake`` therefore returns an ACTION, and there are exactly three:

* ``SCHEDULE``   - call ``ScheduleWakeup(delaySeconds=plan.honourable_s)``. The ordinary case.
* ``STAND_DOWN`` - the floor. Schedule nothing; the five-hour recovery guard is the wake.
                   ``honourable_s`` is ``None`` here, because there is no delay that honours
                   this plan and a number printed beside it would be a number someone copies.
                   ``eta`` is the latest moment the guard can fire, and the lane declares it
                   as its row's ``eta`` - see ``stand_down_eta``.
* ``UNCERTAIN``  - no usable observation was taken at all. Take the base cadence and say so.
                   This is NOT the UNKNOWN verdict, which is a perfectly good observation of an
                   unreadable board and lands on ``SCHEDULE`` at base cadence through the streak.
                   ``UNCERTAIN`` is "this helper could not answer", the CLI's exit-2 path.

THE CALLER'S HALF OF THE STAND-DOWN CONTRACT, which this module cannot check and so must state.
A lane that stops scheduling is relying on a cron that is session-scoped, dies with its process
and auto-expires after seven days. **Standing down without having confirmed the guard is present
this firing is the silent stop this whole fleet is built to avoid.** The six SKILL.md call sites
carry that interlock and the backstop chain behind it: own ``ScheduleWakeup`` -> five-hour
``CronCreate`` guard -> ``agentflow.wake_watch`` on the OS scheduler, which cannot wake a lane
but does report that one went quiet.

THIS MODULE STARTS NOTHING. It reads, digests, appends one line, and returns numbers. No
process, no scheduler entry, nothing that outlives the call - the suite's single-daemon budget
was spent on ``agentflow.wake_watch`` and is not available here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import refs
from .config import WAKE_DELAY_MAX_S, WAKE_DELAY_MIN_S
from .registry import ETA_HORIZON_HOURS

# --------------------------------------------------------------------------- #
# Verdict vocabulary
# --------------------------------------------------------------------------- #
#: Something demonstrably changed. At least one signal was readable on BOTH sides and differs.
MOVED = "moved"
#: Nothing changed, and we could read every signal on both sides to say so. Only this extends
#: the streak, and only a run of these can drive a lane to the floor.
STILL = "still"
#: We could not tell. At least one signal was unreadable, or there is no prior marker to
#: compare against. Never counted as stillness anywhere in this file.
UNKNOWN = "unknown"

VERDICTS = (MOVED, STILL, UNKNOWN)

#: The signals a marker carries, in the order a rendered note lists them. BOTH ARE LOCAL READS
#: and that is a requirement, not an accident - see the module docstring on the dropped PR leg.
SIGNALS = ("refs", "queue")


# --------------------------------------------------------------------------- #
# Actions - what a caller is told to DO, which is not the same as a number
# --------------------------------------------------------------------------- #
#: Call ``ScheduleWakeup(delaySeconds=plan.honourable_s)``. The board is moving, or it has not
#: been still long enough to earn a stand-down.
SCHEDULE = "schedule"
#: THE FLOOR. Schedule nothing at all this wake; the standing five-hour ``--recovery-guard``
#: cron is the wake. This is the only action that can make a lane wake less often than hourly,
#: because ``ScheduleWakeup`` is clamped at an hour and cannot express it.
STAND_DOWN = "stand_down"
#: The helper could not observe anything. Take the base cadence - today's behaviour exactly -
#: and record that it was a fallback rather than a measurement.
UNCERTAIN = "uncertain"

ACTIONS = (SCHEDULE, STAND_DOWN, UNCERTAIN)


# --------------------------------------------------------------------------- #
# Cadence constants
# --------------------------------------------------------------------------- #
#: The cadence a lane wakes at when the board is moving - today's idle wake, unchanged. Every
#: existing idle call site writes delaySeconds=3600, so a zero streak must reproduce exactly
#: that number or this module silently re-paces a healthy fleet on its first observation.
BASE_WAKE_S = WAKE_DELAY_MAX_S

#: Doubling. Named rather than inlined so the growth law is one edit, and so a test can pin the
#: shape (1h, 2h, 4h, floor) against the constant instead of restating the arithmetic.
GROWTH = 2

#: THE HARD FLOOR, and the word is the ruling's: a floor on how OFTEN a lane wakes, which is a
#: CEILING on the delay. One wake every five hours on a provably frozen board. Five because
#: item 2's recovery guard runs on a five-hour clock, and at the floor that guard is not a
#: backstop but the lane's actual wake - the backoff ceiling and the recovery window being the
#: same number is the mechanism, not a coincidence.
FLOOR_INTERVAL_S = 5 * 3600

#: HOW OLD THE EVIDENCE MUST BE BEFORE THE FLOOR APPLIES, and it is a second, independent
#: condition on top of the streak.
#:
#: The streak alone cannot carry this. It counts OBSERVATIONS over a fleet-shared log, and six
#: lanes observe within one wake cycle, so a doubling ladder that reaches the floor at four
#: observations reaches it inside a single hour. A 45-minute lull, or one lane mid-build that
#: has pushed nothing yet, would then be indistinguishable from the measured 70-hour freeze -
#: and the ruling says "once the board is PROVABLY frozen".
#:
#: The interval is the floor interval itself, derived rather than chosen independently, and the
#: rule it encodes is: **never sleep longer than you have watched.** A lane that stops
#: self-scheduling for five hours on three hours of evidence is claiming more than it measured;
#: making the required evidence window equal the sleep it buys means the claim can never outrun
#: the measurement. The cost is stated rather than hidden: a genuinely frozen board is paced at
#: the base hourly cadence for five hours - about thirty lane-wakes - before any lane stands
#: down. That is the price of not standing down on a lunch break.
FLOOR_MIN_STILL_AGE_S = FLOOR_INTERVAL_S

#: THE RECOVERY GUARD'S SCHEDULE, which a stand-down lane's next wake IS. Every loop's SKILL.md
#: writes this exact expression into its ``CronCreate`` call, and a test pins the two together:
#: the eta a floored lane declares is computed from it, so a guard moved in prose and not here
#: would have every floored lane declare a wake that is not coming.
GUARD_CRON = "23 */5 * * *"
#: The minute and the hour step of ``GUARD_CRON``, parsed out of it so the two cannot drift: a
#: guard moved by editing the expression moves the slots ``next_guard_fire`` computes with it.
#: ``CronCreate`` reads the expression in the user's LOCAL timezone, so these are local
#: wall-clock values: hours 0, 5, 10, 15 and 20.
def _parse_guard_cron(expr: str) -> tuple[int, int]:
    minute, hour = expr.split()[:2]
    if not hour.startswith("*/"):
        raise ValueError(f"GUARD_CRON hour field must be a */N step, got {hour!r}")
    return int(minute), int(hour[2:])


GUARD_MINUTE, GUARD_HOUR_STEP = _parse_guard_cron(GUARD_CRON)
#: How late the scheduler may fire a recurring job: up to 10% of its period, capped at 15
#: minutes. The guard's period is five hours, so the cap is what binds.
GUARD_JITTER_S = 15 * 60

#: The registry's eta spelling, so the answer line's ``eta=`` value goes into ``status --eta``
#: exactly as printed.
ETA_FMT = "%Y-%m-%dT%H:%MZ"

#: The observation log's filename inside a project's status directory.
LOG_NAME = "fleet-movement.jsonl"

#: How much of the tail ``still_streak`` reads. A streak longer than this window under-counts,
#: which shortens the delay - the safe direction. ~200 bytes a record, six lanes: this covers
#: several days of observations without ever reading a log whole.
_TAIL_BYTES = 65536

#: Field separator inside a fingerprint line. ASCII US, so no slug, title or state can contain
#: it and thus no two different rows can fingerprint alike by putting the separator in a value.
_FS = "\x1f"

#: Written rather than typed, so no literal newline byte has to survive an editor round-trip.
_NL = chr(10)

#: The one timestamp spelling this log is written in and read back in.
_STAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
def _digest(kind: str, items: Sequence[str]) -> str:
    """A stable short fingerprint of an ordered item list, tagged with its kind.

    The count is inside the digest input, so a truncated list cannot fingerprint as the full
    one, and the ``kind`` tag means two signals holding the same rows can never be compared as
    if they were the same signal.
    """
    h = hashlib.sha256()
    h.update(f"{kind}:{len(items)}\n".encode("utf-8"))
    for item in items:
        h.update(item.encode("utf-8"))
        h.update(b"\n")
    return f"{kind}-{h.hexdigest()[:16]}"


def epic_row_fingerprints(records: Iterable) -> tuple[str, ...]:
    """One line per epic record: slug, lifecycle state, approval state.

    Reads the three attributes BY NAME and lets an ``AttributeError`` escape. A ``getattr``
    default here would fingerprint a record shape this module no longer understands as a
    perfectly stable one, which reads as a frozen board for as long as the mismatch lasts.

    Sorted, because the survey's ordering is a presentation choice (priority, then slug) and a
    re-prioritisation that moved no epic would otherwise register as fleet movement.
    """
    return tuple(sorted(
        f"{r.slug}{_FS}{r.lifecycle_state}{_FS}{r.current_state}" for r in records
    ))


def refs_signal(repo, *, reader=refs.local_tracking_refs) -> Optional[str]:
    """Fingerprint of every remote-tracking ref this checkout holds, or None if unreadable.

    THE ``[]`` TRAP, stated where it is defused. ``refs.local_tracking_refs`` returns an empty
    list on a git failure AND on a checkout that genuinely holds no tracking refs. Those are
    not the same fact, but neither is a fleet-movement value: a checkout that can show no ref
    cannot show master's sha either. So an empty list is UNKNOWN here, never a signal value,
    and a git that broke can never be mistaken for a master that stopped moving.

    THIS IS BROADER THAN MASTER'S SHA ON PURPOSE. Master moving is fleet movement; so is a lane
    pushing a branch, and so is a merged PR's branch being deleted. During the blackouts this
    epic measures, nothing pushed at all - so every ref movement in that window would have been
    real movement, and the narrower probe would only have been blinder. It is also why the
    module needs no separate merged-PR probe: a merge moves master, and master is a ref.

    AS OF THE LAST FETCH, NOT AS OF NOW. These are the checkout's local tracking refs and this
    function never fetches, so a merge nobody has fetched yet reads as unchanged. See the module
    docstring for the gap and why closing it is left to the operator.
    """
    rows = reader(Path(repo))
    if not rows:
        return None
    return _digest("refs", tuple(sorted(f"{name}{_FS}{sha}" for name, sha in rows)))


def queue_signal(fingerprints: Optional[Sequence[str]]) -> Optional[str]:
    """Fingerprint of the dispatchable queue, or None when the survey did not answer.

    ``None`` and ``()`` are different answers and the difference is the whole point. ``()`` is
    "there are zero claimable rows" - a real measurement, the one the steward took 62 times in
    a row, and it MUST be able to drive backoff. ``None`` is "the survey could not run", which
    must not.
    """
    if fingerprints is None:
        return None
    return _digest("queue", tuple(fingerprints))


# --------------------------------------------------------------------------- #
# Marker
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Marker:
    """One fleet-wide snapshot. A ``None`` field means UNREADABLE, never "empty"."""

    refs: Optional[str] = None
    queue: Optional[str] = None

    @property
    def unreadable(self) -> tuple[str, ...]:
        return tuple(name for name in SIGNALS if getattr(self, name) is None)

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in SIGNALS}

    @classmethod
    def from_dict(cls, data: dict) -> "Marker":
        """Rebuild from a logged row. An absent key reads as unreadable, which is the honest
        reading of a record written by an older shape that never carried that signal.

        A key this shape no longer knows - ``prs``, written by the pre-2026-09-22 module - is
        ignored rather than rejected, so a log spanning the change reads back cleanly.
        """
        return cls(**{name: data.get(name) for name in SIGNALS})


def take_marker(
    *,
    repo=None,
    queue_fingerprints: Optional[Sequence[str]] = None,
    refs_reader=refs.local_tracking_refs,
) -> Marker:
    """Assemble a marker from whatever the caller could gather.

    Every argument defaults to the unreadable answer, so a caller that cannot reach a source
    omits it and gets UNKNOWN rather than having to fabricate an empty value for it.
    """
    return Marker(
        refs=None if repo is None else refs_signal(repo, reader=refs_reader),
        queue=queue_signal(queue_fingerprints),
    )


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Movement:
    """A three-valued answer to "did anything move", carrying WHY it says so.

    ``moved`` and ``unreadable`` travel with the verdict because the verdict alone cannot be
    audited afterwards: a lane that backed off wants to be able to say which signal was frozen,
    and a lane that refused to back off wants to say which one it could not see.

    ``unreadable`` is the union of both sides; ``blind_previous`` and ``blind_now`` split it. The
    split matters because the two mean different things: a blind previous marker clears itself on
    the next wake, while a blind probe on this wake is the one that can be blind every wake.
    """

    verdict: str
    moved: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    note: str = ""
    #: Signals the previous marker could not read.
    blind_previous: tuple[str, ...] = ()
    #: Signals this wake's probe could not read.
    blind_now: tuple[str, ...] = ()

    @property
    def extends_streak(self) -> bool:
        """Only a STILL extends a no-movement run. Stated once, read by ``still_streak``."""
        return self.verdict == STILL


def _sided(
    names: Sequence[str], *, blind_previous: Sequence[str], blind_now: Sequence[str]
) -> str:
    """``refs (previous marker), queue (this probe)`` - each signal with the side that lost it.

    A name on neither side is printed bare, which is what a ``Movement`` built without the split
    carries.
    """
    out = []
    for name in names:
        where = [
            label
            for label, side in (("previous marker", blind_previous), ("this probe", blind_now))
            if name in side
        ]
        out.append(f"{name} ({', '.join(where)})" if where else name)
    return ", ".join(out)


def compare(previous: Optional[Marker], current: Marker) -> Movement:
    """Has anything moved fleet-wide since ``previous``?

    MOVED BEATS UNREADABLE. If one signal was readable on both sides and differs, the board
    demonstrably moved, whatever the other did - an unreadable second signal cannot unmake a
    change we watched happen, and the verdict biases toward waking.

    NO PRIOR MARKER IS UNKNOWN, NOT STILL. A first observation has nothing to compare against.
    Reading that as stillness would let a fresh log start a streak out of thin air.
    """
    if previous is None:
        note = "no prior marker to compare against"
        if current.unreadable:
            note += "; unreadable: " + _sided(
                current.unreadable, blind_previous=(), blind_now=current.unreadable
            )
        return Movement(
            UNKNOWN,
            unreadable=current.unreadable,
            note=note,
            blind_now=current.unreadable,
        )

    moved = tuple(
        name
        for name in SIGNALS
        if getattr(previous, name) is not None
        and getattr(current, name) is not None
        and getattr(previous, name) != getattr(current, name)
    )
    unreadable = tuple(sorted(set(previous.unreadable) | set(current.unreadable)))
    sides = {"blind_previous": previous.unreadable, "blind_now": current.unreadable}

    if moved:
        return Movement(
            MOVED,
            moved=moved,
            unreadable=unreadable,
            note="changed: " + ", ".join(moved),
            **sides,
        )
    if unreadable:
        return Movement(
            UNKNOWN,
            unreadable=unreadable,
            note="unreadable, cannot claim stillness: " + _sided(unreadable, **sides),
            **sides,
        )
    return Movement(STILL, note="every signal readable on both sides and unchanged")


# --------------------------------------------------------------------------- #
# The schedule
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WakePlan:
    """What the backoff wants, what one ``ScheduleWakeup`` can deliver, and what to DO.

    THE ACTION IS THE ANSWER, not the number. Two numbers were the first shape of this class
    and it was inert: ``honourable_s`` is clamped into [60, 3600] and the base cadence is 3600,
    so "schedule the honourable number" is the hour the lanes already took, on every streak
    value that exists. A backoff that can only name delays under the harness's ceiling cannot
    lengthen anything. The floor is reachable only by declining to schedule at all, so that is a
    different instruction and it gets its own value - see ``STAND_DOWN``.

    ``honourable_s`` is ``None`` for a stand-down ON PURPOSE. There is no delay that honours
    that plan, and any number printed in that slot is a number a reader eventually copies into
    a ``ScheduleWakeup`` call, which would convert the floor back into an ordinary hourly wake
    while the record claimed five hours.
    """

    streak: int
    intended_s: int
    honourable_s: Optional[int]
    floor_s: int
    at_floor: bool
    action: str
    #: The ladder alone reached the floor. NOT sufficient: the evidence must also be old enough.
    streak_at_floor: bool = False
    #: Age of the oldest STILL in the current run, or None when it could not be established.
    still_age_s: Optional[int] = None
    #: How old that oldest STILL has to be before the floor applies.
    min_still_age_s: int = FLOOR_MIN_STILL_AGE_S
    #: A stand-down's declared wake: the latest the recovery guard can fire (``stand_down_eta``).
    #: None on every other action, and on a stand-down planned without a clock, which
    #: ``answer_line`` refuses to render.
    eta: Optional[datetime] = None

    @property
    def residual_s(self) -> int:
        """Seconds of the intended delay that one ``ScheduleWakeup`` cannot cover.

        At the floor the honourable number is ``None`` - no wake is scheduled at all - so the
        whole intended interval is the recovery guard's, and the residual is the whole of it.
        """
        if self.honourable_s is None:
            return self.intended_s
        return self.intended_s - self.honourable_s

    @property
    def clamped(self) -> bool:
        return self.residual_s > 0

    @property
    def floor_withheld(self) -> bool:
        """The ladder asked for the floor and the evidence was too young to grant it.

        Worth its own name because it is the state D2 exists to produce: a board that has been
        still for four observations inside one wake cycle looks exactly like a frozen one to the
        streak, and this is the flag that says the difference was noticed.
        """
        return self.streak_at_floor and not self.at_floor


def plan_wake(
    streak: int,
    *,
    still_age_s: Optional[int] = None,
    base_s: int = BASE_WAKE_S,
    growth: int = GROWTH,
    floor_s: int = FLOOR_INTERVAL_S,
    min_still_age_s: int = FLOOR_MIN_STILL_AGE_S,
    now: Optional[datetime] = None,
) -> WakePlan:
    """What to do after ``streak`` consecutive no-movement observations.

    ``streak`` 0 returns ``base_s`` exactly: a moving board is paced exactly as it is today, so
    adopting this module changes nothing until the board actually stops.

    TWO CONDITIONS GATE THE FLOOR, AND ``still_age_s`` DEFAULTS TO REFUSING IT. The streak is a
    count of observations over a fleet-shared log; with six lanes it reaches the ladder's top
    inside one wake cycle, so on its own it cannot tell a 45-minute lull from a 70-hour freeze.
    ``still_age_s`` is how long ago the current run of STILL observations OPENED, and the floor
    applies only when that is at least ``min_still_age_s``. A caller that cannot establish the
    age passes nothing and gets ``SCHEDULE``: an unknown age must never buy a stand-down.

    The doubling is computed by a bounded loop rather than ``base * growth ** streak``. A
    62-wake streak - the measured one - makes that expression a 4.6-quintillion-second integer
    whose only use is to be thrown away by the ``min``, and an unbounded exponent on a value
    read from a file is a cost that grows with the log.

    ``now`` is the clock a stand-down's ``eta`` is computed from. Without it a floored plan
    carries no eta, and its answer line refuses to render rather than print a stand-down the
    lane cannot declare.
    """
    if streak < 0:
        raise ValueError("streak must be >= 0, got " + repr(streak))
    if base_s < 1 or floor_s < 1 or growth < 1:
        raise ValueError(
            "base_s, floor_s and growth must all be >= 1; got "
            f"{base_s}, {floor_s}, {growth}"
        )
    if min_still_age_s < 0:
        raise ValueError("min_still_age_s must be >= 0, got " + repr(min_still_age_s))
    if still_age_s is not None and still_age_s < 0:
        raise ValueError("still_age_s must be >= 0 or None, got " + repr(still_age_s))

    intended = min(base_s, floor_s)
    for _ in range(streak):
        if intended >= floor_s:
            break
        intended = min(intended * growth, floor_s)

    streak_at_floor = intended >= floor_s
    old_enough = still_age_s is not None and still_age_s >= min_still_age_s
    at_floor = streak_at_floor and old_enough

    return WakePlan(
        streak=streak,
        intended_s=intended,
        honourable_s=None if at_floor else max(WAKE_DELAY_MIN_S, min(intended, WAKE_DELAY_MAX_S)),
        floor_s=floor_s,
        at_floor=at_floor,
        action=STAND_DOWN if at_floor else SCHEDULE,
        streak_at_floor=streak_at_floor,
        still_age_s=still_age_s,
        min_still_age_s=min_still_age_s,
        eta=stand_down_eta(now) if at_floor and now is not None else None,
    )


def next_guard_fire(now: datetime, *, tz: Optional[timezone] = None) -> datetime:
    """The first ``GUARD_CRON`` slot strictly after *now*, in UTC.

    Computed in *tz*, which defaults to this machine's local offset: ``CronCreate`` reads its
    expression in the user's local timezone, and the helper runs on the machine whose session
    holds the guard. Strictly after, because a lane running on a slot minute may be that very
    fire, and the next one is then five hours on.

    The default takes the CURRENT offset, so across a daylight-saving change the slot is off by
    the hour that changed. Because the offset is fixed, every gap this computes is exactly the
    step or the midnight wrap (5h or 4h), so a DST change only ever makes the slot EARLY: one
    early page on the night the clocks move.
    """
    local = now.astimezone(tz) if tz is not None else now.astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    for day in range(2):
        for hour in range(0, 24, GUARD_HOUR_STEP):
            slot = midnight + timedelta(days=day, hours=hour, minutes=GUARD_MINUTE)
            if slot > local:
                return slot.astimezone(UTC)
    raise AssertionError("unreachable: a two-day window always holds a later guard slot")


def stand_down_eta(
    now: datetime,
    *,
    tz: Optional[timezone] = None,
    horizon_h: float = ETA_HORIZON_HOURS,
) -> datetime:
    """The eta a floored lane declares: the guard's next slot plus the scheduler's jitter.

    A stand-down schedules nothing, so the lane's next wake is the guard's next fire, and the
    latest that fire can arrive is its slot plus ``GUARD_JITTER_S``. Declaring anything earlier -
    the hour-out eta a lane writes before it asks this helper - has the missed-wake alarm page a
    lane that is sleeping correctly, and lists it stale for reaping three hours later.

    Bounded by the registry horizon, measured from *now*. With today's ``GUARD_CRON`` the widest
    gap is five hours, so the eta is at most 5h15m out and the bound never binds; a DST change
    cannot make it bind either, since ``next_guard_fire`` works in a fixed offset. The bound is
    there for a ``GUARD_CRON`` changed to a wider step or a caller passing a *tz* whose slots sit
    further out: declaring the horizon then costs one early page, where a refused ``status``
    write would cost the whole declaration.
    """
    eta = next_guard_fire(now, tz=tz) + timedelta(seconds=GUARD_JITTER_S)
    cap = (now + timedelta(hours=horizon_h)).astimezone(UTC).replace(second=0, microsecond=0)
    return min(eta, cap)


def uncertain_plan(*, base_s: int = BASE_WAKE_S, floor_s: int = FLOOR_INTERVAL_S) -> WakePlan:
    """The third outcome: no usable observation, so take today's cadence and say it was a guess.

    Kept distinct from ``plan_wake(0)`` even though both schedule the base hour, because the
    two make different claims. ``plan_wake(0)`` says "the board moved"; this says "I never
    looked". A lane that records the first when it meant the second has written down an
    observation it did not take, and the shared log would carry that as evidence for everyone.
    """
    honourable = max(WAKE_DELAY_MIN_S, min(base_s, WAKE_DELAY_MAX_S))
    return WakePlan(
        streak=0,
        intended_s=base_s,
        honourable_s=honourable,
        floor_s=floor_s,
        at_floor=False,
        action=UNCERTAIN,
    )


# --------------------------------------------------------------------------- #
# State: the shared observation log
# --------------------------------------------------------------------------- #
def log_path(data_root, project: str) -> Path:
    """``<data_root>/projects/<project>/status/fleet-movement.jsonl``.

    WHY HERE AND WHY THIS SHAPE. Three requirements pick the store and nothing was invented for
    it. It has to be per-project: ``status/`` is already the per-project runtime-state directory
    (``registry.md`` and ``status/workflows/`` both live there), so this is the path layout every
    other reader already uses. It must not be able to corrupt the registry: this is a different
    file, and no code path in this module opens ``registry.md`` at all - the registry is a
    single-writer document under an edit lock, and putting a per-wake counter in it would add a
    high-frequency write to the fleet's hottest lock for a number no human reads.

    And it must be safe under six concurrent lanes, which is why it is APPEND-ONLY JSONL rather
    than a counter file. The suite already runs this shape - ``use_case_id`` appends birth
    certificates to a ``registry.jsonl`` - and it is the one layout where concurrency needs no
    coordination at all: every lane appends its own line and no lane ever rewrites bytes another
    lane wrote, so there is no read-modify-write window to lose an update in and no lock to
    leave orphaned. A counter would have needed ``lock_file`` around every observation, and an
    orphaned lock on the backoff counter would wedge all six lanes for a wake interval.

    WHAT THE APPEND-ONLY CHOICE COSTS, stated rather than left to be discovered: the log grows
    without bound, and a line could in principle be torn by an ill-timed write. Growth is ~200
    bytes per observation and every read is tail-bounded (``_TAIL_BYTES``), so no reader's cost
    grows with it; a torn line is treated by every reader here as "could not tell" and never as
    a value, so the worst it can do is shorten a streak.
    """
    return Path(data_root) / "projects" / project / "status" / LOG_NAME


def record(
    path: Path,
    movement: Movement,
    marker: Marker,
    actor: str,
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Append one observation. Returns the row as written.

    One line, one ``write`` call, opened in append mode - the two properties that make six
    concurrent lanes safe without a lock.
    """
    if movement.verdict not in VERDICTS:
        raise ValueError(
            f"unknown verdict {movement.verdict!r}; expected one of {VERDICTS}"
        )
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime(_STAMP_FMT)
    row = {
        "at": stamp,
        "actor": actor,
        "verdict": movement.verdict,
        "moved": list(movement.moved),
        "unreadable": list(movement.unreadable),
        "marker": marker.as_dict(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
    return row


def _tail_rows(path: Path) -> list:
    """Parsed rows from the tail window, oldest first. ``None`` marks a line we could not read.

    An unparseable line becomes ``None`` rather than being skipped: skipping would join the
    STILL either side of it into one longer run, which is the one thing a damaged log must not
    be able to buy. The first line of the window is dropped when the window did not reach the
    start of the file, because a mid-file seek lands inside a record.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return []
    with path.open("rb") as fh:
        start = max(0, size - _TAIL_BYTES)
        fh.seek(start)
        raw = fh.read()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            out.append(None)
            continue
        out.append(parsed if isinstance(parsed, dict) else None)
    return out


def _parse_stamp(value) -> Optional[datetime]:
    """A logged ``at`` back into a datetime, or None if it is not the one spelling we write.

    Never guesses at another format. A timestamp this module did not write is a timestamp whose
    timezone and precision nobody here can vouch for, and an age derived from a guess is exactly
    the kind of number that grants a five-hour stand-down on five minutes of evidence.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _STAMP_FMT).replace(tzinfo=UTC)
    except ValueError:
        return None


@dataclass(frozen=True)
class StillRun:
    """The current run of STILL observations: how many, and when it opened.

    Two facts rather than one, because the floor needs both and the count alone is the D2
    defect: six lanes observing within one wake cycle produce four STILL rows in well under an
    hour, and a count cannot tell that from a board nobody has touched since Friday.
    """

    count: int
    opened_at: Optional[datetime] = None

    def age_s(self, now: Optional[datetime] = None) -> Optional[int]:
        """How long ago this run opened, in seconds, or None when that cannot be established.

        Clamped at zero. A stamp in the future is a clock that is wrong, and a negative age
        would otherwise sail through any ``>=`` comparison it met later.
        """
        if self.opened_at is None:
            return None
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        return max(0, int((moment - self.opened_at).total_seconds()))


def still_run(path: Path) -> StillRun:
    """The run of STILL observations at the tail of the shared log, with its opening time.

    Counts backwards and stops at the first record that is not a STILL. A MOVED stops it
    because the board moved; an UNKNOWN stops it because nobody could say it did not; an
    unparseable line stops it for the same reason. There is one rule here, not three, and it
    always resolves toward waking sooner.

    FLEET-WIDE BY CONSTRUCTION: the run is read over every lane's observations in one log, so a
    single lane seeing movement resets the streak for all six. That is ruling 4a - the
    backoff is keyed on the board, not on a lane's own cadence.

    ``opened_at`` is the timestamp of the OLDEST row in the run, and it is None whenever that
    row's stamp cannot be parsed - which costs a stand-down and never grants one. Two bounded
    readings are deliberate rather than corrected for: a run older than the tail window reports
    the oldest row still visible, so the age is under-stated, and a run whose rows were written
    by a skewed clock is read in log order rather than re-sorted. Both under-state the evidence,
    which is the direction that keeps a lane awake.
    """
    count = 0
    opened_at: Optional[datetime] = None
    for row in reversed(_tail_rows(path)):
        if row is None or row.get("verdict") != STILL:
            break
        count += 1
        opened_at = _parse_stamp(row.get("at"))
    return StillRun(count=count, opened_at=opened_at)


def still_streak(path: Path) -> int:
    """Consecutive STILL observations at the tail of the shared log.

    The count alone. Kept as its own name because it is the number a human reads in a row, but
    it is NOT sufficient to grant the floor - see ``still_run`` and ``FLOOR_MIN_STILL_AGE_S``.
    """
    return still_run(path).count


def last_marker(path: Path) -> Optional[Marker]:
    """The newest READABLE marker in the log, or None if there is none.

    A torn newest line falls through to an older marker, which widens the comparison window
    rather than narrowing it: comparing against something further back can only turn a STILL
    into a MOVED, never the reverse.
    """
    for row in reversed(_tail_rows(path)):
        if row is None:
            continue
        marker = row.get("marker")
        if isinstance(marker, dict):
            return Marker.from_dict(marker)
    return None


def observe(
    path: Path,
    marker: Marker,
    actor: str,
    *,
    now: Optional[datetime] = None,
) -> tuple:
    """The whole cycle: compare against the log's newest marker, append, plan the next wake.

    Returns ``(Movement, WakePlan)`` together so a caller cannot report one without the other -
    a backoff that does not say what it observed is a number nobody can audit later.

    THE PLAN COUNTS THE OBSERVATION JUST RECORDED, which is why the FIRST still already returns
    two hours rather than one. That is the intended reading: ``plan_wake(0)`` is the cadence of
    a board that just moved, and a board that just failed to move is by definition no longer
    that board. The alternative - one free quiet wake before the doubling starts - buys an hour
    of latency on a board that came back to life, and movement resets the streak anyway, so the
    hour would be spent on every freeze and saved on none.

    The run's AGE is read back from the log after the append, not carried in memory, because the
    run belongs to the fleet and not to this lane: the observation that opened it was very
    probably another lane's.
    """
    movement = compare(last_marker(path), marker)
    record(path, movement, marker, actor, now=now)
    run = still_run(path)
    moment = now or datetime.now(UTC)
    return movement, plan_wake(run.count, still_age_s=run.age_s(moment), now=moment)


# --------------------------------------------------------------------------- #
# Gathering the signals from a real fleet
# --------------------------------------------------------------------------- #
#: What a caller gets back beside the marker: one human line per signal that could not be read,
#: naming the exception. An unreadable signal is a VALUE in this module's vocabulary, not a
#: swallowed error - but a value nobody can see the cause of is how a permanently blind probe
#: goes unnoticed for weeks, so every None here arrives with the reason attached.
GatherNote = str


def gather_marker(project: str) -> tuple[Marker, list[GatherNote]]:
    """Take a live fleet-wide marker for *project*, plus a note per signal that failed.

    BOTH PROBES ARE LOCAL READS AND NEITHER TOUCHES THE NETWORK. That is a budget, not a
    coincidence: this function runs on every idle wake of every lane, so anything it does is
    multiplied by six lanes an hour and charged against the exact spend this epic exists to
    reduce. A ``gh pr list`` leg was here and was removed for precisely that arithmetic - see
    the module docstring - and anything proposed to replace it has to clear the same bar.

    EACH SIGNAL IS GUARDED SEPARATELY, and that is the shape of this function. The probes fail
    independently - a detached checkout, a survey that raises on a malformed spec - and one
    `try` around both would turn either failure into two unreadable signals, which reads
    identically to a fleet with no coordinates at all.

    The failure is converted into this module's explicit UNKNOWN value rather than swallowed: an
    unreadable signal ends the streak, so the lane returns to base cadence, and the reason rides
    back to the caller in the notes so a probe that is blind every wake can be seen to be blind.

    WHAT IT CANNOT DO. A fleet whose config names no ``repo_path`` has `refs` unreadable on every
    observation, so `compare` returns UNKNOWN forever and the backoff never engages. That is the
    conservative direction - such a fleet keeps exactly today's hourly cadence and loses nothing
    it had - but it does mean item 4 is inert there, and the notes say so rather than leaving it
    to be discovered.
    """
    # Deferred: `fleet_report` pulls in the config resolver and the whole report surface, and
    # nothing else in this module needs either. Importing it at module scope would make every
    # `plan_wake` caller pay for a config read.
    from .fleet_report import inputs_from_config

    notes: list[GatherNote] = []
    inputs = inputs_from_config(project)

    # 1. refs - local only, no fetch. `refs_signal` maps `[]` to None itself.
    refs_fp = None
    if inputs.repo_path is None:
        notes.append("refs: no repo_path in this project's config")
    else:
        try:
            refs_fp = refs_signal(inputs.repo_path)
        except OSError as exc:  # git missing, checkout gone - a real, nameable failure
            notes.append(f"refs: {type(exc).__name__}: {exc}")
        if refs_fp is None and inputs.repo_path is not None:
            notes.append("refs: no remote-tracking refs readable (git failed, or none exist)")

    # 2. queue - the dispatchable rows, exactly as a builder's selection would see them.
    queue_fp = None
    try:
        from .epic_survey import dispatchable, survey_epics

        records = survey_epics(
            inputs.data_root, inputs.mtime_cutoff_iso, project=inputs.project
        )
        queue_fp = epic_row_fingerprints(
            dispatchable(records, inputs.excluded_domains, project=inputs.project)
        )
    except (OSError, ValueError, AttributeError, KeyError) as exc:
        notes.append(f"queue: {type(exc).__name__}: {exc}")

    return Marker(refs=refs_fp, queue=queue_signal(queue_fp)), notes


# --------------------------------------------------------------------------- #
# CLI - the only form a prose skill can be told to run
# --------------------------------------------------------------------------- #
#: The machine-readable last line, and the ONE thing a lane is told to read. Two fields, because
#: the number alone cannot express the floor: a stand-down has no delay, and `delaySeconds=none`
#: is spelled out rather than omitted so a reader who copies mechanically fails loudly at the
#: tool call instead of quietly scheduling an hour while the record claims five.
#:
#: A stand-down adds a third field, `eta=`, because it is the one action whose next wake the lane
#: cannot compute from the line: the wake is the guard's next fire, not now plus a delay. The
#: lane declares that value as its row's `--eta`; the hour-out eta it held before asking would
#: page the missed-wake alarm on a lane that is sleeping correctly.
def answer_line(plan: WakePlan) -> str:
    """`action=<verb> delaySeconds=<seconds|none>[ eta=<ISO>]` - the one line a lane reads."""
    delay = "none" if plan.honourable_s is None else str(plan.honourable_s)
    line = f"action={plan.action} delaySeconds={delay}"
    if plan.action != STAND_DOWN:
        return line
    if plan.eta is None:
        raise ValueError(
            "a stand-down plan has no eta; plan it with now= so the lane can declare its wake"
        )
    return f"{line} eta={plan.eta.astimezone(UTC).strftime(ETA_FMT)}"


#: What a stand-down obliges the lane to have already done. Printed with the plan, because the
#: interlock lives in prose the lane may or may not have read this firing, and the one moment it
#: certainly reads something is when it reads the delay.
STAND_DOWN_CAUTION = (
    "schedule NOTHING this wake; the standing 5h --recovery-guard cron is your wake. "
    "Rewrite your row with --eta set to the eta= value on the last line and a phase naming "
    "the floor, or the missed-wake alarm pages you while you sleep. "
    "If CronList did not show that guard present this firing, ignore this and schedule "
    f"delaySeconds={BASE_WAKE_S} instead."
)


def blind_line(movement: "Movement") -> str:
    """`blind=<signals|->` - the signals THIS wake's probe could not read, for the lane's phase.

    This probe only, not the previous marker: a blind previous marker is gone by the next wake,
    while a probe that is blind now can be blind on every wake, and that is the state the row has
    to show. Short by design (17 bytes at worst), so it leaves the rest of the phase cap to the
    lane.
    """
    return "blind=" + (",".join(movement.blind_now) or "-")


def render(movement: "Movement", plan: WakePlan, notes: Sequence[GatherNote]) -> str:
    """The report a lane reads. One fact per line, and the instruction it must follow LAST.

    The last line is `action=<verb> delaySeconds=<n|none>` in exactly that spelling, because a
    reader matching two different spellings is a reader who eventually copies the wrong one. The
    delay is `honourable_s`, never `intended_s`: the harness clamps at an hour without saying
    so, and a lane that scheduled `intended_s` would record a back-off it did not take.
    """
    age = "unknown" if plan.still_age_s is None else f"{plan.still_age_s}s"
    lines = [
        f"verdict: {movement.verdict}",
        "moved: " + (", ".join(movement.moved) or "-"),
        "unreadable: " + (", ".join(movement.unreadable) or "-"),
        f"streak: {plan.streak}",
        f"still_for: {age}",
        f"floor_needs: {plan.min_still_age_s}s",
        f"intended: {plan.intended_s}s",
        "honourable: " + ("none" if plan.honourable_s is None else f"{plan.honourable_s}s"),
        f"residual: {plan.residual_s}s",
        "at_floor: " + ("yes" if plan.at_floor else "no"),
        "floor_withheld: " + ("yes" if plan.floor_withheld else "no"),
    ]
    if plan.action == STAND_DOWN:
        lines.append(f"caution: {STAND_DOWN_CAUTION}")
    # UNKNOWN only: a MOVED note repeats `moved:`, and a STILL note is a fixed sentence.
    if movement.verdict == UNKNOWN and movement.note:
        lines.append(f"reason: {movement.note}")
    lines += [f"note: {n}" for n in notes]
    lines.append(blind_line(movement))
    lines.append(answer_line(plan))
    return _NL.join(lines) + _NL


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m agentflow.backoff --actor <lane> --project <name>`.

    Exit 0 with a report, or exit 2 when the fleet could not be resolved at all. Two codes and
    not one: a lane that cannot resolve its own project must not treat a missing report as "no
    movement" and sleep on it. The refusal still carries an answer line - `action=uncertain`
    with the base cadence - so the lane has the same shape of instruction it would have had on
    success, on stderr where nobody can mistake it for an observation.
    """
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    ap = argparse.ArgumentParser(
        prog="python -m agentflow.backoff",
        description=(
            "Observe fleet-wide movement and print the delay the next idle wake should take."
        ),
    )
    ap.add_argument("--actor", required=True, help="the lane taking this observation")
    ap.add_argument("--project", required=True, help="the fleet to observe")
    ap.add_argument(
        "--data-root", default=None,
        help="override the log's root (tests and drills; the config's data_root otherwise)",
    )
    args = ap.parse_args(argv)

    try:
        marker, notes = gather_marker(args.project)
    except Exception as exc:  # noqa: BLE001 - re-raised as an exit code with its own text
        sys.stderr.write(
            f"could not resolve project {args.project!r}: {type(exc).__name__}: {exc}{_NL}"
            f"fall back to the base idle cadence ({BASE_WAKE_S}s) and say so in your row.{_NL}"
            # Nothing was gathered, so every signal is blind; the lane reads this line on every
            # path, so it has to be here too.
            f"blind={','.join(SIGNALS)}{_NL}"
            f"{answer_line(uncertain_plan())}{_NL}"
        )
        return 2

    if args.data_root is not None:
        root = Path(args.data_root)
    else:
        from .fleet_report import inputs_from_config

        root = Path(inputs_from_config(args.project).data_root)

    movement, plan = observe(log_path(root, args.project), marker, args.actor)
    sys.stdout.write(render(movement, plan, notes))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
