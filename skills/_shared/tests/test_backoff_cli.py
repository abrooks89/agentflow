"""The backoff module's CLI: the only form six prose loops can actually ask for a delay.

`backoff.py` is a library and the loops are prose programs, so without a command line the module
is unreachable from the thing it was built for. That makes the CLI load-bearing, and these are the
properties a lane's correctness rests on:

* **The last line is an INSTRUCTION, not a number.** `action=<verb> delaySeconds=<n|none>`. The
  number alone cannot express the floor: `ScheduleWakeup` is clamped into [60, 3600] and the base
  cadence IS 3600, so a helper that only ever prints a delay can only ever print the hour it was
  supposed to replace. A floored lane schedules NOTHING and the five-hour recovery guard wakes it,
  so the last line has to be able to say that - and to say it without a number beside it that
  somebody copies.
* **The three probes are local, and none touches the network.** This runs on every idle wake of
  every lane; anything it does is multiplied by six an hour and charged against the exact spend the
  epic exists to reduce. A merged-PR leg was here and was removed: a merge moves master, and the
  refs signal reads the tracking refs, master's included, locally - as of the last fetch, since
  nothing here fetches.
* **The signals are gathered independently.** They fail independently in the field - a detached
  checkout, a survey that raises on a malformed spec, a damaged registry - and one `try` around
  all of them turns any one failure into three unreadable signals, which looks identical to a
  fleet with no coordinates at all.
* **A failure is a REASON, not a silence.** An unreadable signal is a legitimate value in this
  module's vocabulary, so nothing raises; but a value whose cause nobody can see is how a probe
  that is blind every single wake goes unnoticed. Every `None` arrives with a note.
* **An unresolvable project exits non-zero rather than printing a delay** - and still hands back an
  `action=uncertain` line, so the lane has the same shape of instruction it would have had.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import pathlib

from agentflow import backoff
from agentflow.config import WAKE_DELAY_MAX_S
from agentflow.fleet_report import ReportInputs

EPOCH = "1970-01-01T00:00:00Z"

#: A floored plan, built the only way one can be: a long streak AND evidence old enough for it.
#: The clock it is planned at, so the stand-down's eta is a fixed value: 06:00Z puts the next guard
#: slot at 10:23Z in UTC, 10:38Z with the jitter - in any local zone the eta is some slot.
T0 = datetime(2026, 9, 22, 6, 0, tzinfo=UTC)
FLOORED = backoff.plan_wake(50, still_age_s=backoff.FLOOR_MIN_STILL_AGE_S, now=T0)


#: A registry with every section and an empty Queue table: the smallest board `parse_registry`
#: reads, so the `lanes` signal is a known zero rather than unreadable.
EMPTY_REGISTRY = chr(10).join((
    "# Fleet Registry - demo-project",
    "",
    "## Agents",
    "",
    "| agent | status | work | phase | heartbeat | eta | waiting_on |",
    "|---|---|---|---|---|---|---|",
    "",
    "## Queue",
    "",
    "| id | work item | lane | claimed_by | state | waiting_on | notes |",
    "|---|---|---|---|---|---|---|",
    "",
    "## Questions",
    "",
    "## Blockers",
    "",
    "| id | raised_by | owner | work | state | description |",
    "|---|---|---|---|---|---|",
    "",
    "## Directives",
    "",
))


def _inputs(tmp_path: Path, **over) -> ReportInputs:
    """A ReportInputs over a tmp tree: no repo, an empty (but readable) survey and registry."""
    board = tmp_path / "projects" / "demo-project" / "status" / "registry.md"
    if not board.exists():
        board.parent.mkdir(parents=True, exist_ok=True)
        board.write_text(EMPTY_REGISTRY, encoding="utf-8", newline=chr(10))
    base = dict(
        data_root=tmp_path,
        project="demo-project",
        mtime_cutoff_iso=EPOCH,
        registry_path=board,
        repo_path=None,
        tracker_host_ref="",
    )
    base.update(over)
    return ReportInputs(**base)


# ── the rendered report ──────────────────────────────────────────────────────

def test_the_last_line_is_an_action_and_a_delay_a_wake_can_actually_honour():
    """What a lane copies, and the verb that tells it whether to copy anything at all."""
    plan = backoff.plan_wake(0)
    out = backoff.render(backoff.Movement(backoff.MOVED, moved=("refs",)), plan, [])
    assert out.strip().splitlines()[-1] == "action=schedule delaySeconds=3600", out
    assert plan.honourable_s == WAKE_DELAY_MAX_S


def test_the_floor_renders_a_stand_down_and_no_number_anyone_could_copy():
    """THE CORRECTION THIS EPIC'S SECOND PASS MADE.

    The first shape printed `delaySeconds=3600` here while the report claimed an 18000-second
    back-off - the harness clamp turned the floor into the hourly wake it replaced, silently, and
    the lane's own record said otherwise. `delaySeconds=none` is spelled out rather than omitted
    so a reader who copies mechanically fails loudly at the tool call instead of quietly
    scheduling an hour.
    """
    out = backoff.render(backoff.Movement(backoff.STILL), FLOORED, [])
    last = out.strip().splitlines()[-1]
    eta = backoff.stand_down_eta(T0).strftime(backoff.ETA_FMT)
    assert last == f"action=stand_down delaySeconds=none eta={eta}", out
    assert "delaySeconds=3600" not in last, (
        "the floored report handed the lane a schedulable hour; that is the inert shape, where "
        "the backoff reports five hours and the fleet takes one"
    )
    assert "honourable: none" in out
    assert "intended: 18000s" in out
    assert "residual: 18000s" in out
    assert "at_floor: yes" in out


def test_a_stand_down_carries_the_interlock_it_depends_on():
    """A lane that stops scheduling is trusting a cron that dies with its session and expires
    after seven days. The report says so at the moment the lane reads the delay, because that is
    the one moment it certainly reads something."""
    out = backoff.render(backoff.Movement(backoff.STILL), FLOORED, [])
    assert "caution:" in out
    assert "--recovery-guard" in out
    assert "CronList" in out
    assert f"delaySeconds={backoff.BASE_WAKE_S}" in out, (
        "the caution does not name the fallback a lane takes when the guard is absent, so a lane "
        "with no guard has an instruction to schedule nothing and nothing to wake it"
    )


def test_the_report_shows_how_long_the_board_has_been_still_and_how_long_it_must_be():
    """D2's decision, visible in the output rather than only in the code.

    A streak that topped the ladder but has not yet earned the floor is the interesting state,
    and `floor_withheld` is where a reader sees the difference was noticed rather than missed.
    """
    withheld = backoff.plan_wake(50, still_age_s=600)
    out = backoff.render(backoff.Movement(backoff.STILL), withheld, [])
    assert "still_for: 600s" in out
    assert f"floor_needs: {backoff.FLOOR_MIN_STILL_AGE_S}s" in out
    assert "floor_withheld: yes" in out
    assert "at_floor: no" in out
    assert out.strip().splitlines()[-1] == "action=schedule delaySeconds=3600"


def test_an_unestablished_age_is_printed_as_unknown_rather_than_as_a_zero():
    """A zero would read as a fact ("the run opened this second") and this is not one."""
    out = backoff.render(backoff.Movement(backoff.UNKNOWN), backoff.plan_wake(0), [])
    assert "still_for: unknown" in out


def test_a_moving_board_renders_exactly_todays_cadence():
    """Adopting this module must change nothing while the board moves, or it is a re-pacing of a
    healthy fleet dressed as a safety feature."""
    out = backoff.render(backoff.Movement(backoff.MOVED, moved=("refs",)), backoff.plan_wake(0), [])
    assert out.strip().splitlines()[-1] == "action=schedule delaySeconds=3600"
    assert "residual: 0s" in out
    assert "moved: refs" in out


def test_gather_notes_reach_the_report():
    """A blind probe has to be visible in the same output the lane already reads, or nobody will
    look for it in a second place."""
    out = backoff.render(
        backoff.Movement(backoff.UNKNOWN, unreadable=("refs",)), backoff.plan_wake(0),
        ["refs: no repo_path in this project's config"],
    )
    assert "note: refs: no repo_path in this project's config" in out
    assert "unreadable: refs" in out


def _second_last(out: str) -> str:
    return out.strip().splitlines()[-2]


def test_a_blind_probe_on_this_wake_names_its_side_and_prints_a_blind_line():
    """The case the row has to show: this wake's probe could not read refs. It exits 0 with
    `action=schedule`, so a lane keyed only on `uncertain` or a non-zero exit would never record
    it, and a probe blind on every wake would look like a healthy hourly board."""
    movement = backoff.compare(
        backoff.Marker(refs="refs-a", queue="queue-a", lanes="lanes-a"), backoff.Marker(refs=None, queue="queue-a", lanes="lanes-a")
    )
    out = backoff.render(movement, backoff.plan_wake(0), [])
    assert "unreadable: refs" in out, out
    assert "reason: unreadable, cannot claim stillness: refs (this probe)" in out, out
    assert _second_last(out) == "blind=refs", out
    assert out.strip().splitlines()[-1] == "action=schedule delaySeconds=3600", out


def test_a_blind_first_observation_still_says_why():
    """No prior marker AND a blind probe: the verdict is UNKNOWN for two reasons, and the report
    must carry both rather than printing `unknown` with nothing beside it."""
    movement = backoff.compare(None, backoff.Marker(refs=None, queue="queue-a", lanes="lanes-a"))
    out = backoff.render(movement, backoff.plan_wake(0), [])
    assert "reason: no prior marker to compare against; unreadable: refs (this probe)" in out, out
    assert _second_last(out) == "blind=refs", out


def test_a_blind_previous_marker_is_not_blamed_on_this_wake():
    """The previous marker lost refs and this probe read it fine. The verdict is still UNKNOWN,
    but the side is named, and the blind line is `-`: this wake's probe is healthy, and the
    previous marker's gap is gone by the next wake."""
    movement = backoff.compare(
        backoff.Marker(refs=None, queue="queue-a", lanes="lanes-a"), backoff.Marker(refs="refs-a", queue="queue-a", lanes="lanes-a")
    )
    out = backoff.render(movement, backoff.plan_wake(0), [])
    assert movement.verdict == backoff.UNKNOWN
    assert "unreadable: refs" in out, out
    assert "reason: unreadable, cannot claim stillness: refs (previous marker)" in out, out
    assert _second_last(out) == "blind=-", out


def test_a_signal_blind_on_both_sides_names_both():
    """The same signal lost on the previous marker and on this probe: the reason names both
    sides, and the blind line still reports this probe's loss."""
    blind = backoff.Marker(refs=None, queue="queue-a", lanes="lanes-a")
    out = backoff.render(backoff.compare(blind, blind), backoff.plan_wake(0), [])
    assert "reason: unreadable, cannot claim stillness: refs (previous marker, this probe)" in out
    assert _second_last(out) == "blind=refs", out


def test_a_healthy_wake_prints_blind_dash():
    """Both sides readable: nothing to record, and the line says so explicitly rather than being
    absent, so a lane can tell a healthy probe from a report it failed to read."""
    same = backoff.Marker(refs="refs-a", queue="queue-a", lanes="lanes-a")
    out = backoff.render(backoff.compare(same, same), backoff.plan_wake(1), [])
    assert "unreadable: -" in out, out
    assert "reason:" not in out, "a STILL verdict needs no reason line"
    assert _second_last(out) == "blind=-", out


def test_the_blind_line_fits_the_phase_cap_with_every_signal_blind():
    """The lane records this line in its phase, so its worst case has to fit there."""
    from agentflow.registry import PHASE_MAX_BYTES

    movement = backoff.compare(None, backoff.Marker())
    line = backoff.blind_line(movement)
    assert line == "blind=" + ",".join(backoff.SIGNALS)
    assert len(line.encode("utf-8")) < PHASE_MAX_BYTES


# ── gathering ────────────────────────────────────────────────────────────────

def test_the_per_wake_marker_makes_no_network_call(tmp_path, monkeypatch):
    """The `gh pr list` leg is gone and this is what keeps it gone.

    It ran once per lane per wake - six network round-trips an hour - to detect merges, which
    necessarily move master, which the local refs read already sees. Re-adding it "for symmetry"
    re-adds the cost and detects nothing new, so the tracker reader is booby-trapped here.

    THE GUARD IS ON THE BEHAVIOUR, NOT ON ONE SPELLING. An earlier version of this test patched
    only `fleet_report._merged_prs`, so it caught the exact re-addition it was written against
    and nothing else: a raw `subprocess.run(["gh", "pr", "list", ...])` dropped into
    `gather_marker` passed the whole suite. That is this epic's own recurring defect - a test
    whose docstring claims more than its assertion holds - so the assertion now refuses ANY
    subprocess this path did not already need. `git` stays allowed because the refs signal is a
    local `git for-each-ref` with no fetch; everything else is a finding by construction, which
    is what makes a leg nobody has thought of yet still fail here.
    """
    import subprocess as _subprocess

    from agentflow import fleet_report

    def _no(*args, **kwargs):
        raise AssertionError(
            "gather_marker reached the tracker; the PR leg is a network round-trip per lane per "
            "wake for a fact the local refs signal already carries"
        )

    reached: list[str] = []
    _real_run = _subprocess.run

    def _watched_run(cmd, *args, **kwargs):
        argv0 = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else str(cmd).split()[0]
        reached.append(str(argv0))
        return _real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_subprocess, "run", _watched_run)
    monkeypatch.setattr(fleet_report, "_merged_prs", _no)
    monkeypatch.setattr(
        fleet_report, "inputs_from_config",
        lambda p: _inputs(tmp_path, tracker_host_ref="owner/repo"),
    )
    marker, _ = backoff.gather_marker("demo-project")
    assert marker.as_dict() == {"refs": None, "queue": marker.queue, "lanes": marker.lanes}
    strayed = [c for c in reached if pathlib.Path(c).stem != "git"]
    assert not strayed, (
        f"the per-wake marker shelled out to {strayed}; only a local `git` read is allowed on "
        "this path, and anything else is a round-trip added to every lane's every wake"
    )


def test_each_signal_is_guarded_SEPARATELY(tmp_path, monkeypatch):
    """The point of two `try`s instead of one.

    Here refs is unavailable while the survey is perfectly readable. A single guard would have
    produced two unreadable signals from one failure - and an all-unreadable marker is the shape
    that makes UNKNOWN permanent, which disables the backoff entirely.
    """
    from agentflow import fleet_report

    monkeypatch.setattr(fleet_report, "inputs_from_config", lambda p: _inputs(tmp_path))
    marker, notes = backoff.gather_marker("demo-project")

    assert marker.refs is None, "no repo_path configured - refs cannot be a value"
    assert marker.queue is not None, (
        "an EMPTY survey is a measurement, not a failure; collapsing it into unreadable is the "
        "defect that would have thrown away the steward's 62-wake observation"
    )
    assert marker.lanes is not None, "an empty Queue table is a measurement, not a failure"
    assert any(n.startswith("refs:") for n in notes), notes
    assert not any(n.startswith("queue:") for n in notes), notes
    assert not any(n.startswith("lanes:") for n in notes), notes


def test_a_failing_survey_is_a_note_and_not_an_exception(tmp_path, monkeypatch):
    """The queue arm's own failure path: a value nobody can see the cause of is how a probe that
    is blind every wake goes unnoticed, so the reason rides back with the None."""
    from agentflow import epic_survey, fleet_report

    def _boom(*args, **kwargs):
        raise ValueError("malformed spec frontmatter")

    monkeypatch.setattr(fleet_report, "inputs_from_config", lambda p: _inputs(tmp_path))
    monkeypatch.setattr(epic_survey, "survey_epics", _boom)
    marker, notes = backoff.gather_marker("demo-project")

    assert marker.queue is None
    assert any("malformed spec frontmatter" in n for n in notes), notes


# ── the command line ─────────────────────────────────────────────────────────

def test_an_unresolvable_project_exits_2_and_still_hands_back_an_action(capsys, monkeypatch):
    """Not exit 0 with a default delay. A lane that cannot resolve its own fleet has observed
    nothing, and a printed observation would be indistinguishable from a real one - but it still
    needs an instruction, so the refusal carries `action=uncertain` on stderr."""
    from agentflow import fleet_report

    def _boom(project):
        raise KeyError("no such project: " + project)

    monkeypatch.setattr(fleet_report, "inputs_from_config", _boom)
    assert backoff.main(["--actor", "steward", "--project", "nope"]) == 2
    err = capsys.readouterr().err
    assert "could not resolve project" in err
    assert str(backoff.BASE_WAKE_S) in err, "the fallback cadence must ride with the refusal"
    assert err.strip().splitlines()[-1] == "action=uncertain delaySeconds=3600", err
    assert err.strip().splitlines()[-2] == "blind=" + ",".join(backoff.SIGNALS), err


def test_a_full_run_appends_one_row_and_prints_a_schedulable_delay(tmp_path, capsys, monkeypatch):
    """End to end against a tmp data_root: a first observation has no prior marker, so it is
    UNKNOWN, the streak is zero, and the delay is exactly today's hourly cadence."""
    from agentflow import fleet_report

    monkeypatch.setattr(fleet_report, "inputs_from_config", lambda p: _inputs(tmp_path))
    rc = backoff.main([
        "--actor", "lead-dev", "--project", "demo-project", "--data-root", str(tmp_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip().splitlines()[-1] == "action=schedule delaySeconds=3600", out
    assert f"verdict: {backoff.UNKNOWN}" in out

    log = backoff.log_path(tmp_path, "demo-project")
    assert log.is_file(), "the observation was not recorded"
    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_a_blind_probe_reaches_the_cli_report(tmp_path, capsys, monkeypatch):
    """End to end with refs blind on this wake: `main` must hand the Movement from `observe`
    through to `render` intact, or the side split drops and the report says `blind=-`."""
    monkeypatch.setattr(
        backoff, "gather_marker",
        lambda project: (backoff.Marker(refs=None, queue="q", lanes="l"), ["refs: stub"]),
    )
    rc = backoff.main([
        "--actor", "lead-dev", "--project", "demo-project", "--data-root", str(tmp_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert _second_last(out) == "blind=refs", out
    assert "reason: " in out, out


def _real_repo(path: Path) -> Path:
    """A checkout with one real remote-tracking ref, so `refs_signal` has something to read.

    A real clone rather than a stub: `refs_signal`'s whole contract is how it treats the empty
    list `git` hands back, and a test that stubs the reader cannot hold that contract.
    """
    import subprocess

    def git(*args):
        proc = subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, f"git {args}: {proc.stderr}"
        return proc.stdout.strip()

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (path / "f.txt").write_text("x", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-q", "-m", "one")
    git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
    return path


def test_a_second_run_against_an_unchanged_board_lengthens_the_delay(tmp_path, capsys, monkeypatch):
    """The mechanism actually engaging, rather than only its parts passing in isolation.

    EVERY SIGNAL IS READABLE HERE AND NO TRACKER IS CONFIGURED, which is the point: `compare`
    returns UNKNOWN whenever any signal is unreadable, and while a merged-PR signal was one of
    three, a fleet with no tracker could never reach STILL and never backed off at all. Local
    signals removed that whole class of inert fleet.

    Two observations of the same board: the first is UNKNOWN (nothing to compare against), the
    second is STILL, and the plan counts the observation it just recorded - so the intended delay
    is already past the base cadence by the second wake, while the ACTION is still `schedule`,
    because a board that has been still for a few seconds is not a frozen one.
    """
    from agentflow import fleet_report

    repo = _real_repo(tmp_path / "repo")
    monkeypatch.setattr(
        fleet_report, "inputs_from_config", lambda p: _inputs(tmp_path, repo_path=repo)
    )

    argv = ["--actor", "lead-dev", "--project", "demo-project", "--data-root", str(tmp_path)]
    assert backoff.main(argv) == 0
    first = capsys.readouterr().out
    assert f"verdict: {backoff.UNKNOWN}" in first, first
    assert "unreadable: -" in first, (
        "a fully configured fleet still reported an unreadable signal, so this test would have "
        f"passed on a board it never actually read: {first}"
    )

    assert backoff.main(argv) == 0
    out = capsys.readouterr().out
    assert f"verdict: {backoff.STILL}" in out, out
    assert "streak: 1" in out, out
    assert "intended: 7200s" in out, out
    assert out.strip().splitlines()[-1] == "action=schedule delaySeconds=3600", out


def test_a_fleet_with_no_checkout_never_backs_off_and_that_is_visible(tmp_path, capsys, monkeypatch):
    """The stated cost of the three-valued rule, pinned so it is a decision and not a surprise.

    A fleet whose config names no `repo_path` has `refs` unreadable on every observation, so every
    verdict is UNKNOWN, the streak never opens, and the delay stays at the base hourly cadence
    forever. Nothing is worse than today - that is the safe direction - but item 4 buys such a
    fleet nothing, and the report says which signal is responsible rather than leaving it to be
    found.
    """
    from agentflow import fleet_report

    monkeypatch.setattr(fleet_report, "inputs_from_config", lambda p: _inputs(tmp_path))
    argv = ["--actor", "steward", "--project", "demo-project", "--data-root", str(tmp_path)]
    for _ in range(3):
        assert backoff.main(argv) == 0
        out = capsys.readouterr().out
    assert f"verdict: {backoff.UNKNOWN}" in out, out
    assert "streak: 0" in out, out
    assert "unreadable: refs" in out, out
    assert "note: refs: no repo_path in this project's config" in out, out
    assert out.strip().splitlines()[-1] == "action=schedule delaySeconds=3600", out


def test_the_actor_is_recorded_so_a_shared_log_can_be_read_back(tmp_path, monkeypatch):
    """Six lanes append to one file. A row with no actor cannot be attributed, and the log's whole
    value is that one lane's observation counts for all six."""
    import json

    from agentflow import fleet_report

    monkeypatch.setattr(fleet_report, "inputs_from_config", lambda p: _inputs(tmp_path))
    backoff.main([
        "--actor", "auditor", "--project", "demo-project", "--data-root", str(tmp_path),
    ])
    row = json.loads(
        backoff.log_path(tmp_path, "demo-project").read_text(encoding="utf-8").splitlines()[0]
    )
    assert row["actor"] == "auditor"


def test_the_cli_requires_both_names(capsys):
    """`--actor` and `--project` are not defaultable: guessing either writes one lane's silence
    under another lane's name, or one fleet's observation into another fleet's log."""
    for argv in ([], ["--actor", "steward"], ["--project", "demo-project"]):
        with pytest.raises(SystemExit):
            backoff.main(argv)


def test_an_unwritable_log_exits_2_with_the_full_refusal_block(tmp_path, capsys, monkeypatch):
    """A FILE where the data root should be makes `record`'s mkdir raise. That observation was
    never taken, and the lane must get the same refusal shape as an unresolvable project - an
    exit 2 carrying `blind=` and the `action=uncertain` line - not a traceback with no answer."""
    monkeypatch.setattr(
        backoff, "gather_marker",
        lambda project: (backoff.Marker(refs="r", queue="q", lanes="l"), []),
    )
    afile = tmp_path / "afile"
    afile.write_text("not a directory\n", encoding="utf-8")
    rc = backoff.main([
        "--actor", "lead-dev", "--project", "demo-project", "--data-root", str(afile),
    ])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == "", "a refusal is not an observation"
    err = captured.err.strip().splitlines()
    assert err[0].startswith("could not complete the observation: "), err
    assert err[-2] == "blind=" + ",".join(backoff.SIGNALS) == "blind=refs,queue,lanes", err
    assert err[-1] == "action=uncertain delaySeconds=3600", err


def test_an_unresolvable_log_root_exits_2_rather_than_raising(tmp_path, capsys, monkeypatch):
    """Without `--data-root` the root is re-resolved from config; a `ConfigError` there is the
    same unrecorded observation and gets the same refusal."""
    from agentflow import fleet_report
    from agentflow.config import ConfigError

    monkeypatch.setattr(
        backoff, "gather_marker",
        lambda project: (backoff.Marker(refs="r", queue="q", lanes="l"), []),
    )

    def _boom(project):
        raise ConfigError("data_root", tmp_path / "environment.md", "gone", "put it back")

    monkeypatch.setattr(fleet_report, "inputs_from_config", _boom)
    assert backoff.main(["--actor", "lead-dev", "--project", "demo-project"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert err[0].startswith("could not complete the observation: ConfigError"), err
    assert err[-1] == "action=uncertain delaySeconds=3600", err
