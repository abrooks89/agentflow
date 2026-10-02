"""Item 4: the fleet-wide backoff, and the properties that make it safe to ship.

Every test here is written to FAIL IF ITS GUARDED LINE IS REVERTED, which is a stronger bar than
"exercises the code". The ones that matter, and the reversion each catches:

* the uncertainty case - delete the ``if not rows: return None`` in ``refs_signal``, or collapse
  UNKNOWN into STILL in ``compare``, and ``test_a_broken_git_is_not_stillness`` /
  ``test_an_unreadable_signal_can_never_produce_a_still`` go red. This is the defect the module
  exists for: a probe that reads its own failure as a frozen board.
* **the stand-down** - make the floor return a schedulable number again (``honourable_s`` for a
  floored plan) and ``test_the_floor_is_a_stand_down_because_no_wake_can_express_it`` /
  ``test_the_backoff_is_not_inert_across_the_whole_streak_range`` go red. That reversion is the
  module's first shipped shape, and it was inert: ``ScheduleWakeup`` is clamped at an hour and
  the base cadence IS an hour, so every delay it could name was the delay it replaced.
* **the elapsed-time condition on the floor** - drop ``still_age_s`` from the floor test and
  ``test_the_time_component_is_load_bearing_and_not_decorative`` /
  ``test_four_observations_inside_one_wake_cycle_cannot_reach_the_floor`` go red. Six lanes share
  the log, so a streak of four is under an hour old; the count alone cannot tell a lunch break
  from the measured 70-hour freeze.
* the growth - flatten the doubling to a constant and ``test_the_delay_doubles_per_still`` goes red.
* the reset - make MOVED or UNKNOWN extend the run and ``test_movement_resets_the_streak`` /
  ``test_an_unknown_ends_the_run`` go red.

The refs probe is driven through a REAL git checkout with a real unreachable remote rather than a
stub, because the ``[]``-on-failure behaviour is ``refs.local_tracking_refs``'s, and a test that
stubs the function under test cannot hold that function's contract (the shape ``test_refs`` uses).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentflow import backoff, registry, wake_watch
from agentflow.config import WAKE_DELAY_MAX_S, WAKE_DELAY_MIN_S

#: A fixed clock, so every age in this file is arithmetic a reader can check rather than a race.
T0 = datetime(2026, 9, 22, 6, 0, 0, tzinfo=UTC)

#: Comfortably older than the evidence window, for the tests whose subject is not the window.
OLD_ENOUGH = backoff.FLOOR_MIN_STILL_AGE_S


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A clone of a bare remote, so ``refs/remotes/origin/*`` is genuinely populated."""
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "master", str(seed)], check=True, capture_output=True)
    for k, v in (("user.email", "t@e.st"), ("user.name", "t")):
        _git(seed, "config", k, v)
    (seed / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-u", "origin", "master")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    for k, v in (("user.email", "t@e.st"), ("user.name", "t")):
        _git(work, "config", k, v)
    return work


class _Rec:
    """Minimal stand-in for an ``epic_survey.EpicRecord`` - only the three fields read."""

    def __init__(self, slug, lifecycle_state, current_state):
        self.slug = slug
        self.lifecycle_state = lifecycle_state
        self.current_state = current_state


def _log(tmp_path: Path) -> Path:
    return tmp_path / "status" / backoff.LOG_NAME


def _append(
    path: Path,
    verdict: str,
    marker: backoff.Marker | None = None,
    *,
    at: datetime | None = None,
) -> None:
    backoff.record(
        path, backoff.Movement(verdict), marker or backoff.Marker("r", "q"), "test", now=at
    )


# --------------------------------------------------------------------------- #
# 1. THE UNCERTAINTY CASE - the trap the module is built around
# --------------------------------------------------------------------------- #
def test_a_broken_git_is_not_stillness(tmp_path: Path):
    """``local_tracking_refs`` returns ``[]`` on git failure. That must read as UNKNOWN.

    Driven against a REAL directory that is not a repository, so git really fails, rather than
    against a stub that returns ``[]`` on request. Deleting ``refs_signal``'s empty check makes
    this red: the empty list would digest to a stable value, two consecutive broken probes would
    compare equal, and the fleet would back off to the floor because git is down.
    """
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    assert backoff.refs_signal(not_a_repo) is None

    marker = backoff.take_marker(repo=not_a_repo, queue_fingerprints=())
    assert "refs" in marker.unreadable
    verdict = backoff.compare(marker, marker)
    assert verdict.verdict == backoff.UNKNOWN
    assert verdict.unreadable == ("refs",)


def test_a_live_checkout_yields_a_refs_value(checkout: Path):
    """The control for the test above: a healthy checkout is readable, so ``[]`` really did
    mean failure and not "this probe never returns anything"."""
    assert backoff.refs_signal(checkout) is not None


def test_a_new_commit_on_the_remote_moves_the_refs_signal(checkout: Path):
    """Real movement is really detected - the other half of the control."""
    before = backoff.refs_signal(checkout)
    (checkout / "g.txt").write_text("more\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-m", "second")
    _git(checkout, "push", "origin", "master")
    after = backoff.refs_signal(checkout)

    assert before != after
    assert backoff.compare(backoff.Marker(refs=before, queue="q"),
                           backoff.Marker(refs=after, queue="q")).verdict == backoff.MOVED


def test_an_empty_queue_is_a_measurement_not_an_absence():
    """``()`` is "zero claimable rows" - the steward's 62-wake measurement - and MUST drive
    backoff. ``None`` is "the survey did not answer" and must not.

    Conflating them in either direction breaks this: make ``()`` return None and two
    known-empty boards read as UNKNOWN forever, so backoff never engages on exactly the board it
    was built for; make ``None`` return a digest and a failed survey reads as a frozen queue.
    """
    empty = backoff.queue_signal(())
    assert empty is not None
    assert backoff.queue_signal(None) is None

    still = backoff.compare(
        backoff.Marker(refs="r", queue=empty),
        backoff.Marker(refs="r", queue=empty),
    )
    assert still.verdict == backoff.STILL


def test_an_unreadable_signal_can_never_produce_a_still():
    """Any unreadable signal on either side downgrades the verdict to UNKNOWN.

    Reverting this - returning STILL when the readable signals match - is the single line that
    turns a partial outage into fleet-wide backoff on a board nobody measured.
    """
    for previous, current in (
        (backoff.Marker("r", None), backoff.Marker("r", "q")),
        (backoff.Marker("r", "q"), backoff.Marker(None, "q")),
        (backoff.Marker(None, "q"), backoff.Marker(None, "q")),
    ):
        result = backoff.compare(previous, current)
        assert result.verdict == backoff.UNKNOWN, (previous, current)
        assert result.unreadable, "the verdict must name which signal it could not read"


def test_a_definite_change_outranks_an_unreadable_signal():
    """MOVED beats UNKNOWN: a change we watched happen is not unmade by a blind second signal."""
    result = backoff.compare(
        backoff.Marker("r1", None),
        backoff.Marker("r2", None),
    )
    assert result.verdict == backoff.MOVED
    assert result.moved == ("refs",)
    assert result.unreadable == ("queue",)


def test_no_prior_marker_is_unknown_not_still():
    """A first observation has nothing to compare against. Reading it as STILL would let a
    fresh log open a streak out of nothing and back a healthy fleet off on wake one."""
    assert backoff.compare(None, backoff.Marker("r", "q")).verdict == backoff.UNKNOWN


# --------------------------------------------------------------------------- #
# 2. NO PR SIGNAL - the network leg that was removed, pinned so it stays removed
# --------------------------------------------------------------------------- #
def test_the_marker_carries_only_local_signals():
    """The PR leg cost a `gh pr list` per lane per wake and could not say anything the refs
    signal does not: a merge moves master, and master is a ref this module reads with no fetch.

    Pinned as the SIGNAL ROSTER rather than as the absence of one function, because the roster
    is what `Marker`, `compare` and `still_streak` all key off - a third signal added back for
    symmetry would have to pass through here.
    """
    assert backoff.SIGNALS == ("refs", "queue")
    assert backoff.Marker().as_dict() == {"refs": None, "queue": None}
    assert not hasattr(backoff, "merged_pr_signal"), (
        "the merged-PR signal is back; it is a network round-trip per lane per wake for a fact "
        "the local refs signal already carries"
    )
    assert not hasattr(backoff, "merged_pr_fingerprints")


def test_a_row_written_by_the_older_three_signal_shape_still_reads_back():
    """The log is append-only and spans the change, so rows carrying `prs` still exist.

    An unknown key is ignored rather than rejected: raising here would make one historical line
    unreadable, which ends a streak - an honest direction, but a needless one.
    """
    marker = backoff.Marker.from_dict({"refs": "r", "queue": "q", "prs": "p"})
    assert marker == backoff.Marker(refs="r", queue="q")
    assert marker.unreadable == ()


# --------------------------------------------------------------------------- #
# 3. THE SCHEDULE - growth, the floor, and the THREE outcomes
# --------------------------------------------------------------------------- #
def test_a_moving_board_is_paced_exactly_as_it_is_today():
    """Streak 0 must reproduce the literal 3600 every idle call site writes today, or adopting
    this module silently re-paces a healthy fleet."""
    plan = backoff.plan_wake(0)
    assert plan.intended_s == 3600
    assert plan.honourable_s == 3600
    assert plan.action == backoff.SCHEDULE
    assert plan.clamped is False
    assert plan.at_floor is False


def test_the_delay_doubles_per_still():
    """Exponential, not linear and not constant.

    Pinned as explicit numbers rather than as ``base * 2 ** n``, because restating the
    implementation's arithmetic in the assertion is how a test agrees with whatever the code
    does. Flattening the growth to a constant makes this red at streak 1.
    """
    assert [backoff.plan_wake(n).intended_s for n in range(4)] == [3600, 7200, 14400, 18000]


def test_the_hard_floor_caps_the_delay():
    """The ruling's floor: one wake every N hours once the board is provably frozen, and no less
    often than that however long the freeze runs.

    62 is the measured streak from the blackout window. Without the cap that streak asks for
    roughly 4.6e18 seconds; with it, five hours.
    """
    plan = backoff.plan_wake(62, still_age_s=OLD_ENOUGH)
    assert plan.intended_s == backoff.FLOOR_INTERVAL_S == 5 * 3600
    assert plan.at_floor is True
    assert backoff.plan_wake(500, still_age_s=OLD_ENOUGH).intended_s == backoff.FLOOR_INTERVAL_S


def test_the_floor_is_a_stand_down_because_no_wake_can_express_it():
    """THE CORRECTION THIS EPIC'S SECOND PASS MADE, and the reason it was needed.

    ``ScheduleWakeup`` is clamped into [60, 3600] and the base cadence IS 3600, so a plan that
    only ever says "schedule this many seconds" cannot name any delay longer than the one it
    replaced. The floor is therefore not a longer wake - it is NO wake, with the standing
    five-hour ``--recovery-guard`` cron carrying the lane back.

    Reverting ``honourable_s`` at the floor to the clamped number makes this red, and that
    reversion is exactly the inert shape: it schedules 3600 while the record claims 18000.
    """
    plan = backoff.plan_wake(62, still_age_s=OLD_ENOUGH, now=T0)
    assert plan.action == backoff.STAND_DOWN
    assert plan.honourable_s is None, (
        "a floored plan carries a schedulable number again; a lane copying it would take an "
        "hourly wake while its own record claimed the five-hour floor"
    )
    assert plan.residual_s == backoff.FLOOR_INTERVAL_S
    assert plan.clamped is True
    assert backoff.answer_line(plan).startswith("action=stand_down delaySeconds=none eta=")
    assert "3600" not in backoff.answer_line(plan)


def test_the_backoff_is_not_inert_across_the_whole_streak_range():
    """THE MEASUREMENT THAT CONDEMNED THE FIRST SHAPE, turned into a gate.

    Adversarial verification measured ``{plan_wake(s).honourable_s for s in range(0, 2000)} ==
    {3600}``: every lane's next delay was the hard-coded hour it replaced, on every streak that
    can exist, while each wake now paid for a marker observation. A backoff whose whole output
    range is one value is not a backoff.
    """
    plans = [backoff.plan_wake(s, still_age_s=OLD_ENOUGH) for s in range(0, 2000)]
    actions = {p.action for p in plans}
    assert actions == {backoff.SCHEDULE, backoff.STAND_DOWN}, (
        f"the backoff produced only {actions}; with the schedule-only shape every wake is the "
        "same hour the fleet already took and the module is pure added cost"
    )
    assert None in {p.honourable_s for p in plans}


def test_the_three_outcomes_are_a_closed_vocabulary():
    """A fourth action nobody wrote a call site for is an instruction six prose loops cannot
    follow, so the set is asserted rather than left open."""
    assert backoff.ACTIONS == (backoff.SCHEDULE, backoff.STAND_DOWN, backoff.UNCERTAIN)
    seen = {
        backoff.plan_wake(0).action,
        backoff.plan_wake(9, still_age_s=OLD_ENOUGH).action,
        backoff.uncertain_plan().action,
    }
    assert seen == set(backoff.ACTIONS)


def test_uncertain_is_a_different_claim_from_a_moving_board():
    """Both schedule the base hour and they are still not the same answer.

    ``plan_wake(0)`` says "the board moved"; ``uncertain_plan()`` says "I never looked". A lane
    that records the first when it meant the second has written an observation it did not take,
    into a log five other lanes read as evidence.
    """
    guess = backoff.uncertain_plan()
    assert guess.action == backoff.UNCERTAIN
    assert guess.honourable_s == backoff.BASE_WAKE_S == backoff.plan_wake(0).honourable_s
    assert guess.action != backoff.plan_wake(0).action
    assert backoff.answer_line(guess) == "action=uncertain delaySeconds=3600"


def test_a_scheduled_plan_always_names_a_delay_the_harness_will_take():
    """The clamp is silent in the harness, so it must be loud here. Every action the lane can
    be told to SCHEDULE has to carry a number inside [60, 3600]."""
    for n in range(0, 64):
        for age in (None, 0, OLD_ENOUGH):
            p = backoff.plan_wake(n, still_age_s=age)
            if p.action == backoff.STAND_DOWN:
                assert p.honourable_s is None, (n, age)
                continue
            assert WAKE_DELAY_MIN_S <= p.honourable_s <= WAKE_DELAY_MAX_S, (n, age)
            assert p.intended_s >= p.honourable_s, (n, age)


def test_a_negative_streak_raises_rather_than_scheduling_something():
    with pytest.raises(ValueError):
        backoff.plan_wake(-1)


def test_a_negative_age_raises_rather_than_sailing_through_the_comparison():
    with pytest.raises(ValueError):
        backoff.plan_wake(9, still_age_s=-1)


# --------------------------------------------------------------------------- #
# 4. THE ELAPSED-TIME CONDITION - "provably frozen" means proven, not counted
# --------------------------------------------------------------------------- #
def test_the_time_component_is_load_bearing_and_not_decorative():
    """DELETE THE TIME CONDITION AND THIS IS THE TEST THAT GOES RED.

    The streak counts OBSERVATIONS over a fleet-SHARED log. Six lanes observe within one wake
    cycle, so the doubling ladder tops out at the fourth observation - inside a single hour. If
    ``at_floor`` were ``streak_at_floor`` alone, a 45-minute lull would be indistinguishable
    from the 70-hour freeze this epic measured, and every lane would stop scheduling.

    An age of None is "I could not establish how long this has been true", and it must refuse
    the floor for the same reason an unreadable signal refuses a STILL.
    """
    assert backoff.plan_wake(62).at_floor is False
    assert backoff.plan_wake(62).action == backoff.SCHEDULE
    assert backoff.plan_wake(62).honourable_s == 3600
    assert backoff.plan_wake(62).floor_withheld is True


def test_the_evidence_window_is_the_sleep_it_buys():
    """Never sleep longer than you have watched.

    The window is derived from the floor rather than chosen beside it, so the two cannot drift
    into a lane that stops scheduling for five hours on three hours of evidence.
    """
    assert backoff.FLOOR_MIN_STILL_AGE_S == backoff.FLOOR_INTERVAL_S == 5 * 3600

    just_short = backoff.plan_wake(62, still_age_s=backoff.FLOOR_MIN_STILL_AGE_S - 1)
    assert just_short.action == backoff.SCHEDULE
    assert just_short.floor_withheld is True

    just_enough = backoff.plan_wake(62, still_age_s=backoff.FLOOR_MIN_STILL_AGE_S)
    assert just_enough.action == backoff.STAND_DOWN
    assert just_enough.floor_withheld is False


def test_four_observations_inside_one_wake_cycle_cannot_reach_the_floor(tmp_path: Path):
    """D2, against the real log rather than against ``plan_wake``'s arguments.

    Six lanes each record a STILL over forty minutes - a perfectly ordinary quiet hour, or one
    lane mid-build that has pushed nothing yet. The streak is six and the ladder is at the top;
    the board has been still for forty minutes and nothing stands down.
    """
    path = _log(tmp_path)
    lanes = ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "auditor", "steward")
    for i, lane in enumerate(lanes):
        backoff.record(
            path, backoff.Movement(backoff.STILL), backoff.Marker("r", "q"), lane,
            now=T0 + timedelta(minutes=8 * i),
        )

    run = backoff.still_run(path)
    assert run.count == 6
    age = run.age_s(now=T0 + timedelta(minutes=40))
    assert age == 40 * 60

    plan = backoff.plan_wake(run.count, still_age_s=age)
    assert plan.streak_at_floor is True, "the ladder did top out - the count alone would floor"
    assert plan.at_floor is False
    assert plan.action == backoff.SCHEDULE
    assert plan.floor_withheld is True


def test_a_run_that_is_genuinely_old_does_reach_the_floor(tmp_path: Path):
    """The positive control for the test above: the window is a gate, not a wall."""
    path = _log(tmp_path)
    for i in range(6):
        backoff.record(
            path, backoff.Movement(backoff.STILL), backoff.Marker("r", "q"), "steward",
            now=T0 + timedelta(minutes=8 * i),
        )
    run = backoff.still_run(path)
    plan = backoff.plan_wake(run.count, still_age_s=run.age_s(now=T0 + timedelta(hours=6)))
    assert plan.at_floor is True
    assert plan.action == backoff.STAND_DOWN


def test_the_run_opens_at_its_OLDEST_row_not_its_newest(tmp_path: Path):
    """The age is the run's, not the last observation's. Reading the newest row would make every
    run seconds old and the floor unreachable; reading past the run's start would let a freeze
    inherit the age of the movement that ended the previous one."""
    path = _log(tmp_path)
    _append(path, backoff.MOVED, at=T0)
    _append(path, backoff.STILL, at=T0 + timedelta(hours=1))
    _append(path, backoff.STILL, at=T0 + timedelta(hours=2))

    run = backoff.still_run(path)
    assert run.count == 2
    assert run.opened_at == T0 + timedelta(hours=1)
    assert run.age_s(now=T0 + timedelta(hours=3)) == 2 * 3600


def test_an_unparseable_timestamp_cannot_buy_a_stand_down(tmp_path: Path):
    """A row whose ``at`` this module did not write has an age nobody can vouch for, and a
    guessed age is how a five-hour stand-down gets granted on five minutes of evidence."""
    path = _log(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({
            "at": "last Tuesday", "actor": "steward", "verdict": backoff.STILL,
            "moved": [], "unreadable": [], "marker": {"refs": "r", "queue": "q"},
        }) + "\n")

    run = backoff.still_run(path)
    assert run.count == 1
    assert run.opened_at is None
    assert run.age_s(now=T0) is None
    assert backoff.plan_wake(99, still_age_s=run.age_s(now=T0)).action == backoff.SCHEDULE


def test_a_future_timestamp_reads_as_zero_age_rather_than_a_negative_one(tmp_path: Path):
    """A clock that is ahead must not hand out a stand-down, and a negative age would sail
    through every ``>=`` it later met."""
    path = _log(tmp_path)
    _append(path, backoff.STILL, at=T0 + timedelta(hours=9))
    run = backoff.still_run(path)
    assert run.age_s(now=T0) == 0
    assert backoff.plan_wake(99, still_age_s=run.age_s(now=T0)).action == backoff.SCHEDULE


# --------------------------------------------------------------------------- #
# 5. THE STATE - streak counting over the shared append-only log
# --------------------------------------------------------------------------- #
def test_stills_accumulate_and_drive_the_delay_up(tmp_path: Path):
    path = _log(tmp_path)
    assert backoff.still_streak(path) == 0, "a missing log is zero, not an error"
    for _ in range(3):
        _append(path, backoff.STILL)
    assert backoff.still_streak(path) == 3
    assert backoff.plan_wake(backoff.still_streak(path)).intended_s == backoff.FLOOR_INTERVAL_S


def test_movement_resets_the_streak(tmp_path: Path):
    """A MOVED ends the run, and the very next wake is back at base cadence.

    Making MOVED transparent to the count keeps a woken fleet asleep at the floor while work is
    landing - the failure that would be hardest to see, because the board looks busy.
    """
    path = _log(tmp_path)
    for _ in range(4):
        _append(path, backoff.STILL)
    assert backoff.still_streak(path) == 4

    _append(path, backoff.MOVED)
    assert backoff.still_streak(path) == 0
    assert backoff.plan_wake(backoff.still_streak(path)).intended_s == backoff.BASE_WAKE_S


def test_an_unknown_ends_the_run(tmp_path: Path):
    """"I could not tell" is not "nothing moved", in the log as well as in the verdict."""
    path = _log(tmp_path)
    for _ in range(4):
        _append(path, backoff.STILL)
    _append(path, backoff.UNKNOWN)
    assert backoff.still_streak(path) == 0


def test_the_streak_is_fleet_wide_not_per_lane(tmp_path: Path):
    """Ruling 4a: keyed on the board, not on each lane's local cadence.

    Five lanes see stillness; the sixth sees movement. Every lane's next delay is base cadence,
    because they all read the same run. A per-lane log would have five lanes at the floor while
    the board was demonstrably moving.
    """
    path = _log(tmp_path)
    for lane in ("lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "auditor"):
        backoff.record(path, backoff.Movement(backoff.STILL), backoff.Marker("r", "q"), lane)
    assert backoff.still_streak(path) == 5

    backoff.record(path, backoff.Movement(backoff.MOVED, moved=("refs",)),
                   backoff.Marker("r2", "q"), "steward")
    assert backoff.still_streak(path) == 0


def test_a_torn_line_reads_as_uncertainty_not_as_a_longer_run(tmp_path: Path):
    """A damaged log must not be able to buy backoff.

    Skipping unparseable lines would splice the STILL on either side into one run of six. They
    are counted as an unreadable record instead, which stops the count - the same direction
    every other unknown resolves in.
    """
    path = _log(tmp_path)
    for _ in range(3):
        _append(path, backoff.STILL)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"at": "2026-09-22T00:00:0\n')
    for _ in range(2):
        _append(path, backoff.STILL)

    assert backoff.still_streak(path) == 2


def test_last_marker_skips_a_torn_tail_rather_than_failing(tmp_path: Path):
    path = _log(tmp_path)
    backoff.record(path, backoff.Movement(backoff.STILL), backoff.Marker("r1", "q1"), "a")
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("{not json at all\n")

    assert backoff.last_marker(path) == backoff.Marker("r1", "q1")
    assert backoff.last_marker(tmp_path / "nothing-here.jsonl") is None


def test_concurrent_appends_do_not_lose_or_corrupt_rows(tmp_path: Path):
    """Six lanes, interleaved, no lock: every row survives and every row parses.

    This is the property the store was chosen for. A counter file under read-modify-write would
    lose observations here; an append-only log cannot, because no lane rewrites another's bytes.
    """
    path = _log(tmp_path)
    lanes = ["lead-dev", "jr-dev-1", "jr-dev-2", "jr-dev-3", "auditor", "steward"]
    for i in range(30):
        backoff.record(path, backoff.Movement(backoff.STILL), backoff.Marker("r", "q"), lanes[i % 6])

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 30
    assert sorted({r["actor"] for r in rows}) == sorted(lanes)
    assert backoff.still_streak(path) == 30


def test_the_log_never_touches_the_registry(tmp_path: Path):
    """The store lives beside ``registry.md``, never inside it."""
    registry = tmp_path / "projects" / "demo-project" / "status" / "registry.md"
    registry.parent.mkdir(parents=True)
    registry.write_text("# registry\n", encoding="utf-8")
    before = registry.read_bytes()

    path = backoff.log_path(tmp_path, "demo-project")
    assert path == registry.parent / backoff.LOG_NAME
    _append(path, backoff.STILL)

    assert registry.read_bytes() == before


def test_record_refuses_a_verdict_outside_the_vocabulary(tmp_path: Path):
    with pytest.raises(ValueError):
        backoff.record(_log(tmp_path), backoff.Movement("idle"), backoff.Marker(), "test")


# --------------------------------------------------------------------------- #
# 6. THE CYCLE - observe() end to end
# --------------------------------------------------------------------------- #
def test_observe_walks_a_freeze_from_base_cadence_to_a_stand_down(tmp_path: Path):
    """The whole item-4 story in one test, and it is a story about a CLOCK as much as a count.

    The first wake is UNKNOWN (no prior marker). Five more lanes then observe the same frozen
    board inside forty minutes: the ladder tops out at the five-hour rung but every one of them
    still SCHEDULES, because forty minutes of stillness is a lunch break and not a frozen board.
    Five hours later the same run finally earns the floor and the lane stands down. One movement
    drops everybody back to base cadence.
    """
    path = _log(tmp_path)
    frozen = backoff.Marker(refs="r", queue=backoff.queue_signal(()))

    movement, plan = backoff.observe(path, frozen, "lead-dev", now=T0)
    assert movement.verdict == backoff.UNKNOWN
    assert plan.intended_s == backoff.BASE_WAKE_S
    assert plan.action == backoff.SCHEDULE

    seen = []
    for i, lane in enumerate(("jr-dev-1", "jr-dev-2", "jr-dev-3", "auditor", "steward"), start=1):
        movement, plan = backoff.observe(path, frozen, lane, now=T0 + timedelta(minutes=8 * i))
        assert movement.verdict == backoff.STILL
        seen.append((plan.intended_s, plan.action))

    assert [s[0] for s in seen] == [7200, 14400, 18000, 18000, 18000]
    assert {s[1] for s in seen} == {backoff.SCHEDULE}, (
        "a lane stood down inside the first hour of stillness - six lanes share the log, so the "
        "ladder tops out after four observations, which is what the elapsed-time gate is for"
    )
    assert plan.floor_withheld is True
    assert plan.honourable_s == 3600

    movement, plan = backoff.observe(
        path, frozen, "lead-dev", now=T0 + timedelta(hours=5, minutes=30)
    )
    assert movement.verdict == backoff.STILL
    assert plan.action == backoff.STAND_DOWN
    assert plan.honourable_s is None
    assert plan.residual_s == backoff.FLOOR_INTERVAL_S

    moved = backoff.Marker(refs="r2", queue=frozen.queue)
    movement, plan = backoff.observe(
        path, moved, "lead-dev", now=T0 + timedelta(hours=5, minutes=35)
    )
    assert movement.verdict == backoff.MOVED
    assert plan.action == backoff.SCHEDULE
    assert plan.intended_s == backoff.BASE_WAKE_S and plan.clamped is False


def test_real_survey_shaped_rows_fingerprint_by_state_not_by_order():
    """Reordering the survey's output is not fleet movement; a state change is."""
    a = [_Rec("alpha", "ready", "approved"), _Rec("beta", "running", "approved")]
    assert backoff.epic_row_fingerprints(a) == backoff.epic_row_fingerprints(list(reversed(a)))

    b = [_Rec("alpha", "ready", "approved"), _Rec("beta", "done", "approved")]
    assert backoff.epic_row_fingerprints(a) != backoff.epic_row_fingerprints(b)


def test_a_record_missing_a_fingerprint_field_raises_rather_than_reading_as_frozen():
    """A ``getattr`` default here would fingerprint an unrecognised record shape as a perfectly
    stable one, which reads as a frozen board for as long as the mismatch lasts."""

    class _Partial:
        slug = "alpha"

    with pytest.raises(AttributeError):
        backoff.epic_row_fingerprints([_Partial()])


# --------------------------------------------------------------------------- #
# 7. THE STAND-DOWN ETA - what a floored lane declares, so its row is not paged
# --------------------------------------------------------------------------- #
#: Fixed offsets, so the guard's local-time slots are arithmetic a reader can check. CronCreate
#: reads its expression in the user's local timezone; these stand in for three of them.
_UTC_MINUS_5 = timezone(timedelta(hours=-5))
_UTC_PLUS_530 = timezone(timedelta(hours=5, minutes=30))


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "slot"),
    [
        # 01:00 local -> 05:23 local, the next hour divisible by five.
        (_utc(2026, 9, 22, 6, 0), _utc(2026, 9, 22, 10, 23)),
        # 20:00 local -> 20:23 local, the same hour.
        (_utc(2026, 9, 22, 1, 0), _utc(2026, 9, 22, 1, 23)),
        # 20:30 local -> 00:23 local the next day: the short 4h gap the */5 step leaves at midnight.
        (_utc(2026, 9, 23, 1, 30), _utc(2026, 9, 23, 5, 23)),
        # Exactly on a slot: this wake may BE that fire, so the next one is five hours on.
        (_utc(2026, 9, 22, 10, 23), _utc(2026, 9, 22, 15, 23)),
    ],
)
def test_the_next_guard_fire_is_the_next_local_slot_of_the_guard_cron(now, slot):
    assert backoff.next_guard_fire(now, tz=_UTC_MINUS_5) == slot


def test_the_stand_down_eta_is_the_next_guard_fire_plus_the_schedulers_jitter():
    """The scheduler fires a recurring job up to 10% of its period late, capped at 15 minutes -
    so the latest the guard can honestly arrive is its slot plus 15 minutes, and that is the eta."""
    now = _utc(2026, 9, 22, 6, 0)
    eta = backoff.stand_down_eta(now, tz=_UTC_MINUS_5)
    assert eta == _utc(2026, 9, 22, 10, 38)
    assert backoff.GUARD_JITTER_S == 15 * 60


@pytest.mark.parametrize("tz", [UTC, _UTC_MINUS_5, _UTC_PLUS_530])
def test_the_stand_down_eta_always_fits_the_registry_horizon_and_never_precedes_the_fire(tz):
    """Swept across two days at a seven-minute step, so every slot and every gap is visited.

    Two bounds, and each is the half of gh#700 the other cannot catch. Earlier than the guard's
    latest fire and the missed-wake alarm pages a lane that is sleeping correctly; later than the
    registry horizon and `status` refuses the write (`eta_too_far`), leaving the stale eta in place.
    """
    horizon = timedelta(hours=registry.ETA_HORIZON_HOURS)
    start = _utc(2026, 9, 22, 0, 0)
    for step in range(0, 48 * 60, 7):
        now = start + timedelta(minutes=step)
        fire = backoff.next_guard_fire(now, tz=tz)
        eta = backoff.stand_down_eta(now, tz=tz)
        assert now < fire <= now + timedelta(hours=backoff.GUARD_HOUR_STEP), (now, fire)
        assert fire.astimezone(tz).minute == backoff.GUARD_MINUTE
        assert fire.astimezone(tz).hour % backoff.GUARD_HOUR_STEP == 0
        assert eta == fire + timedelta(seconds=backoff.GUARD_JITTER_S), (now, eta)
        assert eta - now <= horizon, (now, eta)


def test_a_fire_past_the_horizon_is_clamped_to_it_rather_than_refused():
    """A slot further out than the horizon (a GUARD_CRON moved to a wider step, or a caller's tz
    whose slots sit further out) would put the honest eta past it, and `status` would refuse it
    and keep the stale one. Declaring the horizon instead costs one early page; a refused write
    costs the whole stand-down. DST cannot trigger this: the slots use a fixed offset."""
    now = _utc(2026, 9, 22, 6, 0, 30)
    eta = backoff.stand_down_eta(now, tz=_UTC_MINUS_5, horizon_h=1.0)
    assert eta == _utc(2026, 9, 22, 7, 0)
    assert eta - now <= timedelta(hours=1)


def test_a_floored_plan_carries_its_eta_on_the_answer_line():
    """gh#700. The stand-down prints the eta the lane must declare, in the registry's own
    spelling, so the lane copies one token into `status --eta` and nothing else."""
    plan = backoff.plan_wake(62, still_age_s=OLD_ENOUGH, now=T0)
    assert plan.action == backoff.STAND_DOWN
    expected = backoff.stand_down_eta(T0).strftime(backoff.ETA_FMT)
    assert backoff.answer_line(plan) == f"action=stand_down delaySeconds=none eta={expected}"


def test_a_stand_down_with_no_eta_refuses_to_render():
    """A stand-down line with no eta is the defect itself: the lane keeps the hour-out eta it
    wrote before asking, and the alarm pages it. Refusing is louder than printing a blank."""
    plan = backoff.plan_wake(62, still_age_s=OLD_ENOUGH)
    with pytest.raises(ValueError):
        backoff.answer_line(plan)


def test_a_scheduled_plan_carries_no_eta_field():
    """Only a stand-down needs one: a scheduled lane's eta is now plus the delay it schedules."""
    assert backoff.answer_line(backoff.plan_wake(0, now=T0)) == "action=schedule delaySeconds=3600"
    assert backoff.plan_wake(0, now=T0).eta is None


def test_observe_attaches_the_eta_to_a_stand_down(tmp_path: Path):
    path = _log(tmp_path)
    frozen = backoff.Marker(refs="r", queue=backoff.queue_signal(()))
    backoff.observe(path, frozen, "lead-dev", now=T0)
    for i in range(1, 6):
        backoff.observe(path, frozen, "jr-dev-1", now=T0 + timedelta(minutes=8 * i))
    later = T0 + timedelta(hours=5, minutes=30)
    _, plan = backoff.observe(path, frozen, "lead-dev", now=later)
    assert plan.action == backoff.STAND_DOWN
    assert plan.eta == backoff.stand_down_eta(later)


def _registry_with_row(tmp_path: Path, heartbeat: datetime, eta: datetime) -> Path:
    """A real registry holding one idle jr-dev-1 row, written by `status`, then re-dated.

    `status` stamps the wall clock, so the heartbeat and eta it wrote are swapped for fixed ones
    afterwards: the detectors are then judged at exact offsets rather than against a race.
    """
    reg = tmp_path / "projects" / "demo" / "status" / "registry.md"
    lock = tmp_path / "locks"
    lock.mkdir(parents=True)
    base = ["--file", str(reg), "--lock-dir", str(lock), "--project", "demo"]
    assert registry.main(["init", *base]) == 0
    written_eta = datetime.now(UTC) + timedelta(hours=1)
    assert registry.main([
        "status", *base, "--actor", "jr-dev-1", "--state", "idle",
        "--phase", "lane empty, backoff floor, guard wake",
        "--eta", written_eta.strftime(backoff.ETA_FMT),
    ]) == 0
    row = registry.parse_registry(reg.read_text(encoding="utf-8")).agents["jr-dev-1"]
    text = reg.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("| jr-dev-1 "))
    dated = line.replace(row.heartbeat, heartbeat.strftime(backoff.ETA_FMT)).replace(
        row.eta, eta.strftime(backoff.ETA_FMT))
    reg.write_text(text.replace(line, dated), encoding="utf-8", newline=chr(10))
    return reg


def test_gh700_a_lane_that_declares_the_stand_down_eta_is_neither_paged_nor_reapable(tmp_path):
    """The issue's repro, end to end through the real detectors.

    Control first: the row as it stood before the fix - an hour-out eta - IS paged at +95 minutes
    and IS listed stale at +3.5h, so the assertions on the fixed row can fail. The same lane
    declaring the stand-down eta (10:38Z from 06:00Z, a 4h38m sleep) is silent on both, from the
    watchdog and from the in-sweep read alike, until its guard is actually overdue.
    """
    at95 = T0 + timedelta(minutes=95)
    at3h30 = T0 + timedelta(hours=3, minutes=30)

    control = _registry_with_row(tmp_path / "control", T0, T0 + timedelta(hours=1))
    reg = registry.parse_registry(control.read_text(encoding="utf-8"))
    assert [m.agent for m in registry.missed_wakes(reg, at95)] == ["jr-dev-1"]
    assert [r.agent for r in registry.stale_rows(reg, at3h30)] == ["jr-dev-1"]

    eta = backoff.stand_down_eta(T0, tz=UTC)
    assert eta == _utc(2026, 9, 22, 10, 38)
    fixed = _registry_with_row(tmp_path / "fixed", T0, eta)
    reg = registry.parse_registry(fixed.read_text(encoding="utf-8"))
    assert registry.missed_wakes(reg, at95) == []
    assert registry.stale_rows(reg, at3h30) == []
    grace = timedelta(minutes=registry.MISSED_WAKE_GRACE_MIN)
    assert registry.missed_wakes(reg, eta + grace - timedelta(minutes=1)) == []
    result = wake_watch.watch(fixed, project="demo", now=at95, push=False)
    assert result.missed == [] and result.blind == [] and result.lapsed == []
    # And the alarm still works on this row: a guard that never fired IS paged.
    assert [m.agent for m in registry.missed_wakes(reg, eta + grace + timedelta(minutes=1))] == [
        "jr-dev-1"
    ]


def test_the_guard_slot_constants_are_the_guard_cron_expression():
    """`next_guard_fire` computes from GUARD_MINUTE / GUARD_HOUR_STEP, and the skills pin their
    CronCreate strings to GUARD_CRON. The two must be one fact, or a guard moved by editing the
    expression leaves floored lanes declaring slots that no longer fire."""
    assert f"{backoff.GUARD_MINUTE} */{backoff.GUARD_HOUR_STEP} * * *" == backoff.GUARD_CRON
    assert backoff._parse_guard_cron("7 */3 * * *") == (7, 3)
    with pytest.raises(ValueError):
        backoff._parse_guard_cron("23 0,5,10 * * *")
