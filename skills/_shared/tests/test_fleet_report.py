"""Tests for the fleet-report collector — the one data pass behind every rendering.

Every fixture is built in a tmp dir. NOTHING here reads the live data root: the collector's
whole value is that it is a pure read of a live corpus, and a test that pointed at that corpus
would (a) be non-reproducible and (b) be the one place where a bug in "never acts" would do
real damage.

Two assertions are deliberately about a PROPERTY rather than a number:

* the inert-promotion check asserts the ASYMMETRY, never a finding count. "That number does
  not reproduce ... assert the asymmetry, not the arithmetic."
* the skip capture feeds a genuinely malformed spec through the REAL ``survey_epics`` rather
  than asserting against a hand-written log string, because the coupling under test is to the
  shape of a log line and only the real emitter can prove it still matches.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentflow import epic_survey, fleet_report as fr, registry as reg_mod
from agentflow.issue_intake import Issue

PAST_CUTOFF = "2020-01-01T00:00:00+00:00"
NOW = datetime(2026, 8, 31, 18, 0, 0, tzinfo=timezone.utc)

APPROVED = epic_survey.APPROVED_CURRENT_STATE


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str) -> Path:
    """LF, explicitly — `registry.py` writes with ``newline=""`` so a real registry on disk is
    LF even on Windows, and its section regex (`^## Agents[ \\t]*$`) does not match a CR."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def make_spec(root: Path, project: str, slug: str, *, frontmatter: str = "") -> Path:
    body = f"---\nslug: {slug}\ntitle: {slug} title\n{frontmatter}---\n\nspec body\n"
    return _write(root / "projects" / project / "design" / "specs" / f"{slug}.md", body)


def make_broken_spec(root: Path, project: str, name: str) -> Path:
    """A spec whose frontmatter is real YAML delimiters wrapping unparseable YAML.

    Not "no frontmatter" — that is a legitimate shape the survey handles by deriving the slug
    from the filename, and it is NOT a skip. Only a file the survey drops on the floor
    exercises the capture.
    """
    return _write(
        root / "projects" / project / "design" / "specs" / name,
        "---\nslug: broken\ntitle: [unclosed\n---\n\nbody\n",
    )


def make_status(
    root: Path,
    project: str,
    slug: str,
    *,
    status: str = "running",
    done: int = 1,
    total: int = 2,
) -> Path:
    """An orchestrator status.md — the ONLY thing that marks an epic done.

    Needed here because the diff's SHIPPED half is keyed on the reconciled lifecycle state, and
    a fixture that sets `current_state: done` in the spec instead is the one shape that cannot
    catch a SHIPPED bug: the two signals agree there by construction.
    """
    tasks = "".join(
        f"  {i}:\n    state: {'done' if i <= done else 'pending'}\n" for i in range(1, total + 1)
    )
    body = (
        f"---\nepic: {slug} epic\nplan: /plans/{slug}.md\n"
        f"started: 2026-01-01T00:00:00+00:00\nstatus: {status}\n"
        f"current_phase: phase-1\npaused: false\ntasks:\n{tasks}---\n\nstatus body\n"
    )
    return _write(root / "projects" / project / "status" / f"{slug}.status.md", body)


def stamp(now: datetime, **delta) -> str:
    return (now - timedelta(**delta)).isoformat()


def snapshot_like(**overrides) -> dict:
    """A well-formed prior snapshot at the CURRENT schema version.

    Built through a helper so a version bump cannot leave a test asserting against a shape the
    diff now refuses — which would read as the feature working while every real prior was being
    rejected.
    """
    base = {
        "version": fr.SNAPSHOT_VERSION,
        "generated_at": "2026-08-31T06:00:00+00:00",
        "project": "alpha",
        "lanes": {},
        "epics": {},
        "lifecycle": {},
        "questions": {},
        "issues": {"available": False, "untriaged": 0, "selectable": 0, "triaged_numbers": []},
        "health": {},
    }
    base.update(overrides)
    return base


AGENT_HEADER = "| agent | status | work | phase | heartbeat | eta | waiting_on |\n|---|---|---|---|---|---|---|\n"


def make_registry(
    path: Path,
    *,
    agents: str = "",
    queue: str = "",
    questions: str = "",
    blockers: str = "",
    directives: str = "",
    notes: str = "- **Notes** — (none)",
    deploy: str = "- **Deploy lock** — holder: none, taken-at: -",
) -> Path:
    text = (
        "# Fleet Registry — alpha\n\n"
        "## Agents\n\n" + AGENT_HEADER + agents + "\n"
        "## Queue\n\n" + deploy + "\n" + notes + "\n\n"
        "| id | work item | lane | claimed_by | state | waiting_on | notes |\n|---|---|---|---|---|---|---|\n"
        + queue + "\n"
        "## Questions\n\n" + questions + "\n"
        "## Blockers\n\n"
        "| id | raised_by | owner | work | state | description |\n|---|---|---|---|---|---|\n"
        + blockers + "\n"
        "## Directives\n\n" + directives + "\n"
    )
    return _write(path, text)


def agent_row(agent, status, work="-", phase="-", heartbeat="-", eta="-", waiting_on="-") -> str:
    return f"| {agent} | {status} | {work} | {phase} | {heartbeat} | {eta} | {waiting_on} |\n"


def qblock(qid, **fields) -> str:
    lines = [f"### {qid}", ""]
    lines += [f"- {k}: {v}" for k, v in fields.items()]
    return "\n".join(lines) + "\n\n"


def inputs_for(root: Path, project: str = "alpha", **kw) -> fr.ReportInputs:
    defaults = dict(
        data_root=root,
        project=project,
        mtime_cutoff_iso=PAST_CUTOFF,
        registry_path=root / "projects" / project / "status" / "registry.md",
        # The host the PR readers can read, so a test that sets only `tracker_host_ref` reaches
        # them; a test about another host says so.
        git_host_kind="github",
        tracker_cli="gh",
    )
    defaults.update(kw)
    return fr.ReportInputs(**defaults)


# --------------------------------------------------------------------------- #
# Skip capture — the HARD RULE's mechanism
# --------------------------------------------------------------------------- #
def test_skip_capture_names_the_file_the_survey_dropped(tmp_path):
    """`survey_epics` returns only records; the file it could not parse must still be named."""
    make_spec(tmp_path, "alpha", "healthy", frontmatter=f"current_state: {APPROVED}\n")
    make_broken_spec(tmp_path, "alpha", "2026-08-31-broken-spec.md")

    records, scan = fr.survey_with_skips(tmp_path, PAST_CUTOFF, project="alpha")

    assert [r.slug for r in records] == ["healthy"]
    assert scan.counted == 1
    assert scan.skipped_count == 1
    assert scan.skipped[0].name == "2026-08-31-broken-spec.md"
    assert scan.skipped[0].parent == "specs"
    assert scan.skipped[0].reason  # the parser's own complaint, verbatim
    assert str(scan) == "counted 1, skipped 1"


def test_a_non_zero_skip_is_a_warning_not_a_footnote(tmp_path):
    make_spec(tmp_path, "alpha", "healthy")
    make_broken_spec(tmp_path, "alpha", "broken.md")
    _, scan = fr.survey_with_skips(tmp_path, PAST_CUTOFF, project="alpha")
    assert scan.is_warning is True

    make_spec(tmp_path, "beta", "clean")
    _, clean = fr.survey_with_skips(tmp_path, PAST_CUTOFF, project="beta")
    assert clean.is_warning is False
    assert str(clean) == "counted 1, skipped 0"


def test_skip_capture_restores_the_logger_it_borrowed(tmp_path):
    """The handler is a loan, not a takeover — an operator's own logging must survive it."""
    import logging

    logger = logging.getLogger(epic_survey.__name__)
    before_handlers = list(logger.handlers)
    before_level = logger.level

    make_broken_spec(tmp_path, "alpha", "broken.md")
    with fr.capture_survey_skips("epics") as rows:
        epic_survey.survey_epics(tmp_path, PAST_CUTOFF, project="alpha")
    assert len(rows) == 1

    assert logger.handlers == before_handlers
    assert logger.level == before_level


def test_counted_skipped_section_is_never_truncated(tmp_path):
    """`rows_per_section` caps list sections; it must NOT cap the account of what was missed."""
    make_spec(tmp_path, "alpha", "healthy")
    for i in range(12):
        make_broken_spec(tmp_path, "alpha", f"broken-{i:02d}.md")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    report = fr.collect(inputs_for(tmp_path, rows_per_section=3), now=NOW)

    assert report.counted_skipped.total_skipped == 12
    assert len(report.counted_skipped.skips) == 12  # every one NAMED, cap or no cap
    assert report.counted_skipped.is_warning is True


# --------------------------------------------------------------------------- #
# The inert-promotion asymmetry (NON-NEGOTIABLE 6)
# --------------------------------------------------------------------------- #
def _two_project_corpus(tmp_path: Path) -> None:
    """`alpha` holds one healthy approved epic; `beta` holds another, foreign to alpha.

    Both are approved AND dispatchable in their OWN project, so a correctly-scoped check has
    nothing to report about either of them.
    """
    make_spec(
        tmp_path, "alpha", "alpha-healthy",
        frontmatter=f"current_state: {APPROVED}\nproject: alpha\nqueue_class: fleet-buildable\npriority: 10\n",
    )
    make_spec(
        tmp_path, "beta", "beta-healthy",
        frontmatter=f"current_state: {APPROVED}\nproject: beta\nqueue_class: fleet-buildable\npriority: 20\n",
    )


def test_inert_promotion_scoped_on_both_sides_finds_nothing(tmp_path):
    _two_project_corpus(tmp_path)
    scoped = epic_survey.survey_epics(tmp_path, PAST_CUTOFF, project="alpha")
    assert [r.slug for r in scoped] == ["alpha-healthy"]

    assert fr.inert_promotions(scoped, project="alpha") == []


def test_inert_promotion_scopes_the_approved_side_itself(tmp_path):
    """Handed a project-WIDE survey it still reports nothing: the scoping lives in the
    function, so a caller cannot reintroduce the asymmetry by passing an unscoped list."""
    _two_project_corpus(tmp_path)
    wide = epic_survey.survey_epics(tmp_path, PAST_CUTOFF)
    assert {r.slug for r in wide} == {"alpha-healthy", "beta-healthy"}

    assert fr.inert_promotions(wide, project="alpha") == []


def test_the_asymmetric_comparison_reports_another_projects_healthy_epic(tmp_path):
    """The bug this contract exists to prevent, demonstrated.

    A corpus-wide approved set compared against a project-scoped dispatchable set reports
    `beta-healthy` — an epic that is approved, dispatchable and entirely fine in its own
    project — as inert. One false positive per foreign approved epic, growing with every
    project added to the data root.
    """
    _two_project_corpus(tmp_path)
    wide = epic_survey.survey_epics(tmp_path, PAST_CUTOFF)

    approved_wide = {r.slug for r in fr.approved_epics(wide)}
    claimable_narrow = {
        r.slug
        for r in epic_survey.dispatchable(wide, (), project="alpha", warn_foreign=False)
    }
    asymmetric = approved_wide - claimable_narrow

    assert "beta-healthy" in asymmetric
    # ... and the same corpus, checked the contracted way, is clean.
    assert fr.inert_promotions(wide, project="alpha") == []


def test_inert_promotion_still_catches_a_real_stuck_epic(tmp_path):
    """The scoping must not make the check toothless: an in-project approved epic that no
    builder can claim (a non-dispatchable queue_class) is still reported."""
    make_spec(
        tmp_path, "alpha", "stuck",
        frontmatter=f"current_state: {APPROVED}\nproject: alpha\nqueue_class: local-tooling\n",
    )
    scoped = epic_survey.survey_epics(tmp_path, PAST_CUTOFF, project="alpha")
    assert [r.slug for r in fr.inert_promotions(scoped, project="alpha")] == ["stuck"]


# --------------------------------------------------------------------------- #
# Lane classification — three conditions, three fixes
# --------------------------------------------------------------------------- #
def _lane_registry(tmp_path: Path, *, reap_heartbeat: str) -> Path:
    agents = (
        agent_row("lead-dev", "building", work="alpha-healthy",
                  heartbeat=stamp(NOW, minutes=5), eta=(NOW + timedelta(hours=1)).isoformat())
        + agent_row("jr-dev-1", "halted", work="some-epic",
                    heartbeat=stamp(NOW, hours=6), waiting_on="Q-0009")
        + agent_row("jr-dev-2", "idle", phase=reg_mod.REAPED_PHASE, heartbeat=reap_heartbeat)
        + agent_row("auditor", "idle", heartbeat=stamp(NOW, hours=30))
        + agent_row("steward", "halted", heartbeat=stamp(NOW, hours=4))
        + agent_row("dev-manager", "building", heartbeat=stamp(NOW, minutes=1))
    )
    return make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agents,
        # dev-manager is this fixture's LIVE control. Before `_lane_has_work` became total it
        # was LIVE because a lane-less seat could not be measured at all; now it has to EARN
        # the verdict, so the fixture routes it a note. Same assertion, real reason.
        notes=_note("lead-dev", "dev-manager"),
    )


def _dry(report) -> int:
    return sum(1 for lane in report.lanes.lanes if lane.state == fr.LANE_DRY)


def test_lane_classification_splits_halted_reaped_and_silent(tmp_path):
    _lane_registry(tmp_path, reap_heartbeat=stamp(NOW, hours=2))
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    by_agent = {lane.agent: lane for lane in report.lanes.lanes}

    # lead-dev HAS a lane and this fixture publishes nothing to it AND holds no claimable
    # epic or selectable bug, so the honest answer is DRY rather than LIVE - E-0290 exists
    # because those two rendered the same green node after p190/p195 retired the halt that
    # used to distinguish them. With startable work the answer changes; see
    # `test_an_idle_lead_dev_with_startable_work_is_not_DRY`.
    assert by_agent["lead-dev"].state == fr.LANE_DRY
    # dev-manager has no Queue lane, and DRY is no longer undefined for it: it stays LIVE
    # because this fixture routes it a note, which is the channel its work actually arrives
    # through. Before E-0224 it was LIVE no matter what the board said.
    assert by_agent["dev-manager"].state == fr.LANE_LIVE

    # HALTED-BY-DESIGN: self-declared, and it carries its resume condition.
    assert by_agent["jr-dev-1"].state == fr.LANE_HALTED
    assert by_agent["jr-dev-1"].resume_condition == "Q-0009"
    assert "Q-0009" in by_agent["jr-dev-1"].why

    # A halt with no stated resume condition says so rather than rendering blank.
    assert by_agent["steward"].state == fr.LANE_HALTED
    assert "unstated" in by_agent["steward"].resume_condition

    # REAPED beats staleness: the reap PRESERVES the dead heartbeat, so a stale-first test
    # would report every reaped lane as a fresh silent one.
    assert by_agent["jr-dev-2"].state == fr.LANE_REAPED
    assert by_agent["jr-dev-2"].reaped_recent is True

    # SILENT: stale on both signals and claiming neither halt nor reap.
    assert by_agent["auditor"].state == fr.LANE_SILENT
    assert "undiagnosed" in by_agent["auditor"].why

    # 2 -> 1 + 1: lead-dev moved from LIVE to DRY, which is the whole point of this
    # change. The other three counts are untouched, which says DRY did not eat a
    # problem state on its way in.
    assert report.lanes.live_count == 1
    assert _dry(report) == 1
    assert report.lanes.halted_count == 2
    assert report.lanes.reaped_count == 1
    assert report.lanes.silent_count == 1


def test_a_stood_down_lane_is_not_rendered_SILENT_and_says_why(tmp_path):
    """gh#98, one surface over from the alarm.

    A lane a human stopped is stale on both signals and claims neither halt nor reap, so it fell
    all the way through to SILENT -- whose why-string is "the row claims neither halt nor reap -
    undiagnosed". That is the report saying it cannot tell why the lane is quiet, printed beside
    a phase cell that says so in words, on the surface dev-manager actually pushes to the human.

    Also not REAPED: reaped means DIED and had its claims returned, and this lane keeps its work.
    """
    agents = (
        agent_row("lead-dev", "building", work="alpha-healthy",
                  heartbeat=stamp(NOW, minutes=5), eta=(NOW + timedelta(hours=1)).isoformat())
        + agent_row("jr-dev-1", "building", work="some-epic",
                    phase=reg_mod.STOOD_DOWN_PHASE + ": cleared by hand for the migration window",
                    heartbeat=stamp(NOW, hours=30))
        # The control: identical staleness, no mark, and it IS undiagnosed.
        + agent_row("auditor", "idle", heartbeat=stamp(NOW, hours=30))
    )
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md", agents=agents)
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    by_agent = {lane.agent: lane for lane in report.lanes.lanes}

    assert by_agent["jr-dev-1"].state == fr.LANE_STOOD_DOWN
    assert by_agent["jr-dev-1"].state != fr.LANE_REAPED
    # The REASON travels to the reader, which is the entire reason the mark carries one.
    assert "migration window" in by_agent["jr-dev-1"].why
    assert "undiagnosed" not in by_agent["jr-dev-1"].why
    # The claim is still the lane's -- standing down is not reaping.
    assert by_agent["jr-dev-1"].work == "some-epic"

    # Control: the same 30h staleness with no mark still renders SILENT/undiagnosed, so the
    # new state is doing the work rather than the fixture being quietly un-stale.
    assert by_agent["auditor"].state == fr.LANE_SILENT
    assert "undiagnosed" in by_agent["auditor"].why

    # STOOD-DOWN did not eat a problem state on its way in.
    assert report.lanes.silent_count == 1
    assert report.lanes.reaped_count == 0
    assert report.lanes.halted_count == 0


def test_a_stood_down_lane_with_no_recorded_reason_says_so(tmp_path):
    """The CLI refuses a blank --reason, but a row hand-written before this verb existed (or by
    a `repair`) can still carry the bare marker. Render that honestly instead of an empty
    sentence trailing a colon."""
    agents = (
        agent_row("lead-dev", "building", work="a", heartbeat=stamp(NOW, minutes=5),
                  eta=(NOW + timedelta(hours=1)).isoformat())
        + agent_row("auditor", "idle", phase=reg_mod.STOOD_DOWN_PHASE,
                    heartbeat=stamp(NOW, hours=30))
    )
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md", agents=agents)
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    lane = {r.agent: r for r in report.lanes.lanes}["auditor"]

    assert lane.state == fr.LANE_STOOD_DOWN
    assert "no reason recorded" in lane.why


def test_a_reap_ages_out_of_attention_after_24h(tmp_path):
    _lane_registry(tmp_path, reap_heartbeat=stamp(NOW, hours=100))
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    reaped = next(lane for lane in report.lanes.lanes if lane.state == fr.LANE_REAPED)
    assert reaped.reaped_recent is False


def test_silent_count_is_reported_even_when_zero(tmp_path):
    """"0 silent" is the reassuring half — it has to be a field, not an absence."""
    agents = agent_row("dev-manager", "building", heartbeat=stamp(NOW, minutes=1))
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md", agents=agents,
                  notes=_note("lead-dev", "dev-manager"))
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    assert report.lanes.silent_count == 0
    assert report.lanes.live_count == 1


def test_a_lane_that_is_not_working_always_says_why(tmp_path):
    _lane_registry(tmp_path, reap_heartbeat=stamp(NOW, hours=2))
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    for lane in report.lanes.lanes:
        if lane.state != fr.LANE_LIVE:
            assert lane.why.strip(), f"{lane.agent} is {lane.state} with no reason next to it"


# --------------------------------------------------------------------------- #
# The diff contract
# --------------------------------------------------------------------------- #
def test_no_prior_report_says_so_rather_than_rendering_everything_as_new(tmp_path):
    make_spec(tmp_path, "alpha", "one", frontmatter=f"current_state: {APPROVED}\n")
    make_spec(tmp_path, "alpha", "two", frontmatter="current_state: drafted\n")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    report = fr.collect(inputs_for(tmp_path, vault_root=tmp_path / "vault"), now=NOW)

    assert report.changed_since.no_prior is True
    assert report.changed_since.note.startswith("first report - no prior to diff")
    assert report.changed_since.epic_moves == ()
    assert report.changed_since.lane_transitions == ()
    # ... and SHIPPED does not invent a window it does not have.
    assert report.work.shipped.total == 0
    assert "does not exist yet" in report.work.shipped_note


def test_diff_reports_lane_epic_question_issue_and_health_movement():
    prior = snapshot_like(
        lanes={"lead-dev": fr.LANE_LIVE, "auditor": fr.LANE_LIVE},
        epics={"shipped-one": "running", "still-drafted": "drafted"},
        lifecycle={"shipped-one": "running", "still-drafted": "speccing"},
        questions={"Q-0001": "open", "Q-0002": "open"},
        issues={"triaged_numbers": [7]},
        health={"inert_promotions": 0, "silent_lanes": 2},
        generated_at="2026-08-31T12:00:00+00:00",
    )
    current = snapshot_like(
        lanes={"lead-dev": fr.LANE_LIVE, "auditor": fr.LANE_SILENT},
        epics={"shipped-one": "done", "still-drafted": "drafted", "brand-new": "speccing"},
        lifecycle={"shipped-one": "done", "still-drafted": "speccing", "brand-new": "speccing"},
        questions={"Q-0001": "answered", "Q-0002": "applied", "Q-0003": "open"},
        issues={"triaged_numbers": [7, 51]},
        health={"inert_promotions": 3, "silent_lanes": 0},
    )
    changed = fr.diff_since(prior, current, prior_path="prior.md")

    assert changed.no_prior is False
    assert changed.lane_transitions == (fr.LaneTransition("auditor", fr.LANE_LIVE, fr.LANE_SILENT),)
    assert fr.EpicMove("shipped-one", "running", "done") in changed.epic_moves
    assert fr.EpicMove("brand-new", "new", "speccing") in changed.epic_moves
    assert fr.EpicMove("shipped-one", "running", "done") in changed.lifecycle_moves
    assert changed.questions_opened == ("Q-0003",)
    assert changed.questions_answered == ("Q-0001",)
    assert changed.questions_applied == ("Q-0002",)
    assert changed.issues_triaged == (51,)  # a tracker number IS the identifier
    crossings = {c.metric: c.direction for c in changed.health_crossings}
    assert crossings == {"inert_promotions": "crossed", "silent_lanes": "cleared"}


def test_diff_can_prove_a_quiet_period():
    """"Nothing changed since 14:20Z" is a real sentence the archive makes demonstrable."""
    core = snapshot_like(
        lanes={"lead-dev": fr.LANE_LIVE},
        epics={"one": "drafted"},
        lifecycle={"one": "speccing"},
        issues={"triaged_numbers": []},
        health={"inert_promotions": 0},
        generated_at="2026-08-31T14:20:00+00:00",
    )
    changed = fr.diff_since(core, dict(core, generated_at="2026-08-31T18:00:00+00:00"))
    assert changed.is_empty
    assert changed.note == "nothing changed since 2026-08-31T14:20:00+00:00"


def _archive_prior(vault: Path, prior: dict, *, stamp_name: str = "2026-08-31T060000Z") -> Path:
    directory = vault / fr.DEFAULT_ARCHIVE_SUBDIR / "alpha"
    directory.mkdir(parents=True, exist_ok=True)
    return _write(
        directory / (stamp_name + fr.ARCHIVE_SUFFIX),
        "# report\n\n```" + fr.SNAPSHOT_FENCE_TAG + "\n" + json.dumps(prior) + "\n```\n",
    )


def test_a_prior_archived_report_makes_shipped_computable(tmp_path):
    """The fixture ships an epic the way the fleet actually does: `status.md` says done while
    the spec frontmatter still carries the approval it was dispatched under.

    A fixture that set `current_state: done` instead would be the one case that cannot fail —
    both signals agree there by construction — and it left SHIPPED keyed on the approval map,
    where an epic that really shipped registered as no movement at all.
    """
    vault = tmp_path / "vault"
    make_spec(tmp_path, "alpha", "delivered", frontmatter=f"current_state: {APPROVED}\n")
    make_status(tmp_path, "alpha", "delivered", status="done", done=2, total=2)
    make_spec(tmp_path, "alpha", "waiting", frontmatter="current_state: drafted\n")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    _archive_prior(
        vault,
        snapshot_like(
            epics={"delivered": APPROVED, "waiting": "drafted"},
            lifecycle={"delivered": "running", "waiting": "speccing"},
        ),
    )

    report = fr.collect(inputs_for(tmp_path, vault_root=vault), now=NOW)

    assert report.changed_since.no_prior is False
    assert report.changed_since.prior_generated_at == "2026-08-31T06:00:00+00:00"
    assert [row.slug for row in report.work.shipped] == ["delivered"]  # by SLUG
    assert report.work.shipped_note == ""
    # The approval map never moved: `current_state` still reads approved-for-autonomous. Keyed
    # on that, SHIPPED would be empty and the report would say "nothing changed".
    assert report.snapshot()["epics"]["delivered"] == APPROVED
    assert not [m for m in report.changed_since.epic_moves if m.slug == "delivered"]
    # ...and the epic that already shipped is not offered back as claimable work.
    assert "delivered" not in [row.slug for row in report.work.approved_claimable]


def test_the_claimable_filter_follows_the_vocabulary_it_borrows(tmp_path, monkeypatch):
    """gh#325: this filter spelled `("done", "archived")` inline — a THIRD subset of the suite's
    terminal vocabulary, after the one gh#261 removed from `_done_slugs`.

    DERIVATION IS THE PROPERTY UNDER TEST, and it is tested by varying the INPUT. Comparing the
    filter's output against a second copy of the terminal set would pass just as well for a
    restatement that was accurate on the day it was typed — which is the defect, not the fix.
    Here the vocabulary gains a member and the rendered rows must move with it: an epic at a
    state the set does not contain is claimable, and stops being claimable when that state is
    added and NOTHING else changes. A literal tuple cannot pass this.

    `APPROVED` is the state varied because it is one this corpus can actually produce; see
    `test_a_terminal_lifecycle_cannot_diverge_from_its_current_state` for why the terminal
    values themselves cannot be reached in this position today.

    It is also why the fix reads `_survey.TERMINAL_CURRENT_STATES` through the module rather
    than binding the frozenset with a `from ... import`, which would snapshot the value and be
    a restatement in a slower disguise.
    """
    make_spec(tmp_path, "alpha", "novel", frontmatter=f"current_state: {APPROVED}\n")

    before = fr.collect(inputs_for(tmp_path), now=NOW).work.approved_claimable
    assert "novel" in [row.slug for row in before], "fixture is not claimable to begin with"

    monkeypatch.setattr(
        epic_survey,
        "TERMINAL_CURRENT_STATES",
        epic_survey.TERMINAL_CURRENT_STATES | {APPROVED},
    )

    after = fr.collect(inputs_for(tmp_path), now=NOW).work.approved_claimable
    assert "novel" not in [row.slug for row in after]


@pytest.mark.parametrize("terminal", sorted(epic_survey.TERMINAL_CURRENT_STATES - {"archived"}))
def test_a_terminal_lifecycle_cannot_diverge_from_its_current_state(tmp_path, terminal):
    """WHY THE WIDENING HAS NO LIVE VICTIM, pinned as a property rather than left as a claim.

    The filter above exists for a divergence: `lifecycle_state` terminal while `current_state`
    still carries the approval the epic was dispatched under, which `dispatchable` cannot see
    because it never reads status.md. For `done` that divergence is real and this filter is what
    catches it. For the other terminal spellings it is NOT REACHABLE today, and the reason is
    structural rather than lucky: pass 1 derives `lifecycle_state` FROM `current_state`, pass 2
    freezes both to the spec's value together when the spec is terminal, and pass 3 emits only
    `done`, `running` or an ended board status - never a word from the terminal set but `done`.
    The ended board statuses are a separate exclusion, pinned by
    `test_an_ended_board_is_not_claimable_while_its_spec_still_reads_approved`.

    So a `superseded` record is refused by `dispatchable` on approval alone and never reaches
    the lifecycle filter. That makes gh#325 a vocabulary fix, exactly as its issue says — and
    this test is here so that if a future pass ever DOES produce the divergence, the assertion
    that it cannot fails loudly instead of the report quietly offering finished work.

    `archived` is excluded because those records leave the corpus entirely rather than surviving
    with a state to compare.
    """
    make_spec(tmp_path, "alpha", "finished", frontmatter=f"current_state: {terminal}\n")
    make_spec(tmp_path, "alpha", "live", frontmatter=f"current_state: {APPROVED}\n")

    report = fr.collect(inputs_for(tmp_path), now=NOW)
    records, _ = fr.survey_with_skips(tmp_path, PAST_CUTOFF, project="alpha")
    finished = [r for r in records if r.slug == "finished"]

    assert finished, "fixture left the corpus — the assertions below would prove nothing"
    assert finished[0].lifecycle_state == terminal
    assert epic_survey._norm_state(finished[0].current_state) == terminal
    assert "finished" not in [row.slug for row in report.work.approved_claimable]
    assert "live" in [row.slug for row in report.work.approved_claimable]


@pytest.mark.parametrize("ended", sorted(epic_survey._ENDED_BOARD_STATUSES))
def test_an_ended_board_is_not_claimable_while_its_spec_still_reads_approved(tmp_path, ended):
    """gh#705: a board whose run ENDED - abandoned, failed, or done with its PR left open for a
    human merge - is not work a lane can start, whatever approval its spec still carries.

    Pass 3 hands these statuses through as `lifecycle_state`, and none of them is in
    `TERMINAL_CURRENT_STATES` (that set means "satisfies a `depends_on`", which they do not).
    So the approval gate passes, the terminal-lifecycle gate passes, and without its own
    exclusion the report offers a dead run to the human as claimable. The shape is the issue's
    repro: approved spec, ended board. The live neighbour proves the fixture is claimable at all.
    """
    make_spec(tmp_path, "alpha", "ended", frontmatter=f"current_state: {APPROVED}\n")
    make_status(tmp_path, "alpha", "ended", status=ended, done=1, total=2)
    make_spec(tmp_path, "alpha", "live", frontmatter=f"current_state: {APPROVED}\n")

    records, _ = fr.survey_with_skips(tmp_path, PAST_CUTOFF, project="alpha")
    ended_rec = [r for r in records if r.slug == "ended"]
    assert ended_rec and ended_rec[0].lifecycle_state == ended, (
        "pass 3 did not carry the board status through - the assertion below would prove nothing"
    )

    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    claimable = [row.slug for row in report.work.approved_claimable]
    assert "ended" not in claimable
    assert "live" in claimable
    claimable_now = [row.slug for row in report.work.claimable_now]
    assert "ended" not in claimable_now
    assert "live" in claimable_now


def test_the_ended_board_filter_follows_the_set_it_borrows(tmp_path, monkeypatch):
    """The ended-board exclusion reads `_survey._ENDED_BOARD_STATUSES` through the module.

    Same property as `test_the_claimable_filter_follows_the_vocabulary_it_borrows`, tested the
    same way: the set gains the lifecycle word of a claimable epic with NO board, and that epic
    must leave the claimable list with nothing else changed. Pass 3 is the only other reader of
    the set and never touches a board-less record, so only the filter can move it. A restated
    literal cannot pass this.
    """
    make_spec(tmp_path, "alpha", "live", frontmatter=f"current_state: {APPROVED}\n")

    records, _ = fr.survey_with_skips(tmp_path, PAST_CUTOFF, project="alpha")
    live = [r for r in records if r.slug == "live"]
    assert live, "fixture produced no record"
    before = fr.collect(inputs_for(tmp_path), now=NOW).work.approved_claimable
    assert "live" in [row.slug for row in before], "fixture is not claimable to begin with"

    monkeypatch.setattr(
        epic_survey,
        "_ENDED_BOARD_STATUSES",
        epic_survey._ENDED_BOARD_STATUSES | {live[0].lifecycle_state},
    )

    after = fr.collect(inputs_for(tmp_path), now=NOW).work.approved_claimable
    assert "live" not in [row.slug for row in after]


def test_an_epic_whose_only_board_has_ended_leaves_the_product_with_no_work(tmp_path):
    """An ended board counts toward no WORK list, so it does not suppress the no-work alert.

    The only epic is an approved spec whose board is `done_on_hold`: nothing claimable, nothing
    running, nothing drafted. That IS a board with no startable work, and the alert must say so
    rather than stay silent on a dead run's behalf.
    """
    make_spec(tmp_path, "alpha", "held", frontmatter=f"current_state: {APPROVED}\n")
    make_status(tmp_path, "alpha", "held", status="done_on_hold", done=2, total=2)
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert report.work.claimable_now.total == 0
    assert report.unread_work_sources == (), "zeros over an unread source prove nothing"
    assert report.no_work_anywhere is True


def test_the_shipped_transition_stays_narrower_than_the_terminal_set(tmp_path):
    """The neighbour gh#325 warns must NOT be swept up with the fix, pinned so that sweeping it
    up fails here instead of in the report.

    SHIPPED asks `now == "done" and was != "done"` — a TRANSITION, not set membership. Widening
    it to `TERMINAL_CURRENT_STATES` for consistency with the claimable filter would print a spec
    reconciled to `superseded` under a heading that says it shipped: a false statement about work
    that was withdrawn rather than delivered.
    """
    vault = tmp_path / "vault"
    make_spec(tmp_path, "alpha", "withdrawn", frontmatter="current_state: superseded\n")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    _archive_prior(
        vault,
        snapshot_like(epics={"withdrawn": APPROVED}, lifecycle={"withdrawn": "running"}),
    )

    report = fr.collect(inputs_for(tmp_path, vault_root=vault), now=NOW)

    assert fr.EpicMove("withdrawn", "running", "superseded") in report.changed_since.lifecycle_moves
    assert "withdrawn" not in [row.slug for row in report.work.shipped]


def test_a_prior_snapshot_at_another_schema_version_is_refused_not_diffed(tmp_path):
    """A prior whose shape this report does not understand renders as NO prior, with the
    reason, rather than as a corpus where every epic is brand new and every done epic just
    shipped."""
    vault = tmp_path / "vault"
    make_spec(tmp_path, "alpha", "old-news", frontmatter=f"current_state: {APPROVED}\n")
    make_status(tmp_path, "alpha", "old-news", status="done", done=2, total=2)
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    _archive_prior(vault, {"version": 1, "generated_at": "2026-08-31T06:00:00+00:00"})

    report = fr.collect(inputs_for(tmp_path, vault_root=vault), now=NOW)

    assert report.changed_since.no_prior is True
    assert "version 1" in report.changed_since.note
    assert report.work.shipped.total == 0  # NOT "everything ever done, shipped just now"


def test_an_archive_with_no_snapshot_block_is_reported_not_guessed(tmp_path):
    vault = tmp_path / "vault"
    archive_dir = vault / fr.DEFAULT_ARCHIVE_SUBDIR / "alpha"
    archive_dir.mkdir(parents=True)
    _write(archive_dir / ("2026-08-30T060000Z" + fr.ARCHIVE_SUFFIX), "# a hand-written page\n")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    report = fr.collect(inputs_for(tmp_path, vault_root=vault), now=NOW)
    assert report.changed_since.no_prior is True
    assert "carries no snapshot block" in report.changed_since.note


def test_snapshot_round_trips_through_the_fence(tmp_path):
    make_spec(tmp_path, "alpha", "one", frontmatter="current_state: drafted\n")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    report = fr.collect(inputs_for(tmp_path), now=NOW)

    assert fr.parse_archived_snapshot(report.snapshot_fence()) == report.snapshot()


def test_the_snapshot_diffs_every_epic_not_just_the_rendered_page(tmp_path):
    """Truncation is a rendering concern. If the snapshot were built from capped rows the
    diff would silently go blind to everything past `rows_per_section`."""
    for i in range(15):
        make_spec(tmp_path, "alpha", f"epic-{i:02d}", frontmatter="current_state: drafted\n")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    report = fr.collect(inputs_for(tmp_path, rows_per_section=5), now=NOW)
    assert len(report.snapshot()["epics"]) == 15


# --------------------------------------------------------------------------- #
# Truncation
# --------------------------------------------------------------------------- #
def test_a_truncated_section_reports_showing_n_of_total(tmp_path):
    for i in range(23):
        make_spec(
            tmp_path, "alpha", f"epic-{i:02d}",
            frontmatter=f"current_state: {APPROVED}\nproject: alpha\npriority: {100 - i}\n",
        )
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    report = fr.collect(inputs_for(tmp_path), now=NOW)
    rows = report.work.approved_claimable

    assert rows.total == 23
    assert rows.shown == fr.DEFAULT_ROWS_PER_SECTION == 10
    assert rows.truncated is True
    assert rows.showing == "showing 10 of 23"
    # Lowest priority first: epic-22 carries priority 78.
    assert [row.slug for row in rows][0] == "epic-22"
    assert [row.priority for row in rows] == sorted(row.priority for row in rows)


def test_an_untruncated_section_says_nothing_about_showing(tmp_path):
    make_spec(tmp_path, "alpha", "only", frontmatter=f"current_state: {APPROVED}\nproject: alpha\n")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    rows = fr.collect(inputs_for(tmp_path), now=NOW).work.approved_claimable
    assert rows.truncated is False
    assert rows.showing == ""


def test_rows_per_section_is_configurable(tmp_path):
    for i in range(8):
        make_spec(
            tmp_path, "alpha", f"epic-{i}",
            frontmatter=f"current_state: {APPROVED}\nproject: alpha\npriority: {i}\n",
        )
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    rows = fr.collect(inputs_for(tmp_path, rows_per_section=3), now=NOW).work.approved_claimable
    assert rows.showing == "showing 3 of 8"


# --------------------------------------------------------------------------- #
# The report never acts
# --------------------------------------------------------------------------- #
def test_collect_writes_nothing_to_the_data_root(tmp_path):
    """Byte-identical before and after. "Anything that both reports and mutates will
    eventually be run for its side effect.\""""
    _two_project_corpus(tmp_path)
    make_broken_spec(tmp_path, "alpha", "broken.md")
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("lead-dev", "building", heartbeat=stamp(NOW, minutes=3)),
        questions=qblock("Q-0001", state="open", urgency="halted", question="ship it?",
                         raised_at=stamp(NOW, hours=9), raised_by="lead-dev"),
        blockers="| B-0001 | lead-dev | human | alpha-healthy | open | needs a call |\n",
        directives="- **hold merges** until the gate is green\n",
    )
    issues = [Issue(number=51, title="a bug", labels=("triaged",))]

    before = fr.fingerprint_tree(tmp_path)
    report = fr.collect(
        inputs_for(tmp_path, vault_root=tmp_path / "vault"), now=NOW, issues=issues
    )
    after = fr.fingerprint_tree(tmp_path)

    assert before == after
    assert report.blocked.questions.total == 1


def test_collect_does_not_create_the_archive_directory_it_looks_in(tmp_path):
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    vault = tmp_path / "vault"
    report = fr.collect(inputs_for(tmp_path, vault_root=vault), now=NOW)
    assert not vault.exists()
    assert report.archive.available is True  # a location is RESOLVED, nothing is written
    assert report.archive.filename == "2026-08-31T180000Z-fleet-report.md"


def test_no_vault_configured_still_renders_and_says_the_archive_was_skipped(tmp_path):
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    report = fr.collect(inputs_for(tmp_path, vault_root=None), now=NOW)
    assert report.archive.available is False
    assert "archive skipped" in report.archive.reason
    assert report.changed_since.no_prior is True
    assert "no vault configured" in report.changed_since.note
    assert len(report.sections()) == len(fr.SECTION_ORDER)  # the report still renders


# --------------------------------------------------------------------------- #
# Sections: order, presence, and the pairs that carry meaning
# --------------------------------------------------------------------------- #
def test_every_section_is_present_on_an_empty_corpus(tmp_path):
    """An empty section reads "checked, nothing there"; an absent one is indistinguishable
    from a section that failed to run."""
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    report = fr.collect(inputs_for(tmp_path), now=NOW)

    names = [name for name, _ in report.sections()]
    assert names == list(fr.SECTION_ORDER)
    # EIGHT, not seven: DONE was added at position 5 by E-0290, between WORK and LANES.
    # The literal list is spelled out rather than derived from SECTION_ORDER on purpose -
    # asserting a tuple against itself proves nothing, and this line is what makes a
    # reorder or an insertion a visible, reviewed edit.
    assert names == [
        "blocked", "decided", "intake", "work", "done", "lanes", "health",
        "counted_skipped",
    ]
    for name, section in report.sections():
        assert section is not None, name
    assert report.blocked.is_empty and report.decided.is_empty and report.work.is_empty


def test_section_lookup_refuses_a_name_outside_the_locked_order(tmp_path):
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    with pytest.raises(KeyError):
        report.section("summary")


def test_intake_always_carries_both_halves_of_the_pair(tmp_path):
    """"20 untriaged / 0 selectable" told the whole story of the stall; either number
    alone told nothing."""
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    issues = [
        Issue(number=1, title="raw", labels=()),
        Issue(number=2, title="raw two", labels=()),
        Issue(number=3, title="carved", labels=("triaged", "epic")),
    ]
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=issues)

    assert report.intake.available is True
    assert report.intake.untriaged_count == 2
    assert report.intake.selectable_count == 0  # triaged, but an `epic` — no builder takes it
    assert report.intake.untriaged.total == 2


def test_intake_says_it_could_not_look_rather_than_printing_zero(tmp_path):
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    report = fr.collect(inputs_for(tmp_path), now=NOW)  # no tracker configured
    assert report.intake.available is False
    assert "no issue tracker" in report.intake.reason
    assert report.intake.untriaged_count == 0
    assert report.intake.selectable_count == 0
    # An unreachable tracker is not an EMPTY section: a renderer told it is empty prints
    # "checked, nothing there" and drops the reason that says the look never happened.
    assert report.intake.is_empty is False


def test_intake_reports_a_failing_tracker_cli_instead_of_swallowing_it(tmp_path):
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    inputs = inputs_for(
        tmp_path, tracker_host_ref="owner/repo", tracker_kind="github-issues", tracker_cli="gh"
    )
    report = fr.collect(
        inputs, now=NOW, issue_runner=lambda argv: (1, "", "could not resolve host")
    )
    assert report.intake.available is False
    assert "tracker unreachable" in report.intake.reason


def test_blocked_lists_open_questions_oldest_first_with_age(tmp_path):
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        questions=(
            qblock("Q-0001", state="open", urgency="advisory", question="old one",
                   raised_at=stamp(NOW, hours=50), raised_by="steward")
            + qblock("Q-0002", state="open", urgency="halted", question="new one",
                     raised_at=stamp(NOW, hours=2), raised_by="lead-dev")
            + qblock("Q-0003", state="answered", answered_by="dev-manager",
                     question="settled", answer="yes", answered_at=stamp(NOW, hours=1))
        ),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)

    assert [q.qid for q in report.blocked.questions] == ["Q-0001", "Q-0002"]
    assert report.blocked.questions.items[0].age == "2d 2h"
    assert report.blocked.questions.items[1].age == "2h00m"


def test_blocked_counts_drafted_specs_awaiting_promotion(tmp_path):
    make_spec(tmp_path, "alpha", "waiting-one", frontmatter="current_state: drafted\n")
    make_spec(tmp_path, "alpha", "waiting-two", frontmatter="current_state: drafted\n")
    make_spec(tmp_path, "alpha", "promoted", frontmatter=f"current_state: {APPROVED}\n")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    report = fr.collect(inputs_for(tmp_path), now=NOW)
    assert report.blocked.drafted_awaiting_promotion == 2
    assert report.blocked.drafted_slugs == ("waiting-one", "waiting-two")  # SLUGS


def test_blocked_only_lists_blockers_a_person_has_to_resolve(tmp_path):
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        blockers=(
            "| B-0001 | lead-dev | human | some-epic | open | needs a call |\n"
            "| B-0002 | steward | lead-dev | other-epic | open | a loop owns this |\n"
            "| B-0003 | lead-dev | human | third-epic | resolved | already handled |\n"
        ),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    assert [b.bid for b in report.blocked.blockers] == ["B-0001"]


def test_decided_shows_only_what_an_agent_decided_not_what_the_human_answered(tmp_path):
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        questions=(
            qblock("Q-0001", state="answered", answered_by="dev-manager", question="delegated",
                   answer="proceed", answered_at=stamp(NOW, hours=1), raised_by="lead-dev")
            + qblock("Q-0002", state="answered", answered_by="human", question="asked a person",
                     answer="no", answered_at=stamp(NOW, hours=2), raised_by="steward")
        ),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    assert [d.qid for d in report.decided.questions] == ["Q-0001"]
    assert report.decided.windowed is False  # no prior report, so there is no window yet


@pytest.mark.parametrize("seat", [v for v in reg_mod.ANSWERED_BY if v != "human"])
def test_decided_accepts_every_delegated_answerer_the_registry_allows(tmp_path, seat):
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        questions=qblock("Q-0001", state="answered", answered_by=seat, question="delegated",
                         answer="yes", answered_at=stamp(NOW, hours=1), raised_by="auditor"),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    assert [(d.qid, d.answered_by) for d in report.decided.questions] == [("Q-0001", seat)]


def test_a_steward_answer_is_decided_and_labelled_as_the_stewards(tmp_path, monkeypatch):
    """The gh#740 repro through the registry CLI: a question the steward answers under its
    delegated authority is no longer open, so BLOCKED omits it, and DECIDED must carry it with
    the seat that decided, so the human can tell it from a dev-manager answer."""
    from agentflow import ntfy

    monkeypatch.setattr(ntfy, "notify", lambda trigger, **kw: None)
    reg_path = tmp_path / "projects" / "alpha" / "status" / "registry.md"
    lock = tmp_path / "locks"
    lock.mkdir()
    base = ["--file", str(reg_path), "--lock-dir", str(lock), "--project", "alpha"]
    assert reg_mod.main(["init", *base]) == 0
    for actor, question in (("auditor", "file gap epic for X?"), ("jr-dev-1", "file gap epic for Y?")):
        assert reg_mod.main(["ask", *base, "--actor", actor, "--urgency", "waiting",
                             "--question", question, "--default", "yes"]) == 0
    assert reg_mod.main(["answer", *base, "--actor", "steward", "--id", "Q-0001",
                         "--answer", "yes, file it", "--by", "steward"]) == 0
    assert reg_mod.main(["answer", *base, "--actor", "dev-manager", "--id", "Q-0002",
                         "--answer", "no", "--by", "dev-manager"]) == 0

    report = fr.collect(inputs_for(tmp_path), now=NOW)

    assert [q.qid for q in report.blocked.questions] == []
    assert sorted((d.qid, d.answered_by) for d in report.decided.questions) == [
        ("Q-0001", "steward"), ("Q-0002", "dev-manager"),
    ]


def test_the_windowed_decided_path_carries_a_steward_answer(tmp_path):
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        questions=(
            qblock("Q-0001", state="applied", raised_by="auditor", question="file gap?",
                   answer="yes", answered_by="steward", answered_at=stamp(NOW, hours=2))
            + qblock("Q-0002", state="answered", raised_by="lead-dev", question="ask a person",
                     answer="no", answered_by="human", answered_at=stamp(NOW, hours=1))
        ),
    )
    prior = snapshot_like(questions={"Q-0001": "open", "Q-0002": "open"})
    report = fr.collect(inputs_for(tmp_path), now=NOW, prior=prior)

    assert report.decided.windowed is True
    assert [(d.qid, d.answered_by) for d in report.decided.questions] == [("Q-0001", "steward")]


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def test_health_names_the_directives_it_counts(tmp_path):
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        directives=(
            "- [2026-08-31T05:12Z] **hold merges** until the release gate is green\n"
            "- [expired 2026-08-01] an old instruction\n"
            "- [2026-09-04T00:13Z] never rebuild the image without a canary first\n"
        ),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)

    assert report.health.directive_count == 2  # the expired one is not in force
    names = [d.name for d in report.health.directives]
    assert names == ["hold merges", "never rebuild the image without a canary first"]


def test_health_reports_the_deploy_lock_and_the_note_split(tmp_path):
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        deploy=f"- **Deploy lock** — holder: lead-dev, taken-at: {stamp(NOW, hours=2)}",
        notes=(
            "- **Notes**\n"
            "- dev-manager -> auditor: heartbeat stale, first detection\n"
            "- ~~steward -> lead-dev: consumed already~~\n"
        ),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)

    assert report.health.deploy_lock.held is True
    assert report.health.deploy_lock.holder == "lead-dev"
    assert report.health.deploy_lock.stale is True  # 2h > the 45-minute stale ceiling
    assert report.health.notes_live == 1
    assert report.health.notes_struck == 1
    assert report.health.registry_bytes > 0


def test_health_counts_out_of_vocabulary_states_with_their_slugs(tmp_path):
    make_spec(tmp_path, "alpha", "typo", frontmatter="current_state: aproved-for-autonomous\n")
    make_spec(tmp_path, "alpha", "fine", frontmatter="current_state: drafted\n")
    make_spec(tmp_path, "alpha", "legacy")  # unset is NOT out-of-vocabulary
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    report = fr.collect(inputs_for(tmp_path), now=NOW)
    assert report.health.out_of_vocabulary_count == 1
    assert report.health.out_of_vocabulary.items[0] == ("typo", "aproved-for-autonomous")


def test_health_records_why_it_could_not_read_git_rather_than_reporting_clean(tmp_path):
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    assert report.health.branches.available is False
    assert report.health.branches.reason
    assert report.health.worktrees.available is False
    assert report.health.worktrees.reason


def test_health_reports_worktree_directories_no_live_lane_claims(tmp_path):
    """At the REAL depth — `<worktree_root>/<project>/<epic-slug>`, which is what config.py's
    own remediation and six SKILL.md files write out.

    A flat fixture (`worktrees/<epic-slug>`) does not exist in production and passed against a
    listing of the ROOT, which yields PROJECT directories: every project reported as a
    permanently unclaimed worktree, and the actual abandoned per-epic worktree never seen.
    """
    worktrees = tmp_path / "worktrees"
    (worktrees / "alpha" / "e0280-fleet-report").mkdir(parents=True)
    (worktrees / "alpha" / "e0999-abandoned").mkdir(parents=True)
    (worktrees / "beta" / "someone-elses").mkdir(parents=True)
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("lead-dev", "building", work="fleet-report",
                         heartbeat=stamp(NOW, minutes=2)),
    )
    report = fr.collect(inputs_for(tmp_path, worktree_root=worktrees), now=NOW)

    assert report.health.worktrees.available is True
    assert report.health.worktrees.directories == ("e0280-fleet-report", "e0999-abandoned")
    assert report.health.worktrees.unclaimed == ("e0999-abandoned",)
    # Another project's worktrees are not this project's drift.
    assert "beta" not in report.health.worktrees.directories
    assert "someone-elses" not in report.health.worktrees.directories


def test_branch_drift_reads_git_through_an_injected_runner(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, "master\nfeature-one\nfeature-two\n", ""

    report = fr.collect(
        inputs_for(tmp_path, repo_path=repo, base_branch="master"), now=NOW, git_runner=runner
    )
    assert calls and calls[0][:2] == ["git", "-C"]
    assert report.health.branches.available is True
    assert report.health.branches.extra == ("feature-one", "feature-two")


def test_a_failed_git_read_reports_both_streams_not_whichever_is_non_empty(tmp_path):
    """gh#478's expression lived here too, and this section is where it costs most.

    `available=False` plus a reason is the report's way of saying "could not look" rather
    than printing an empty branch list that reads as clean -- so the reason IS the section.
    The old `(err or out or "")` handed the reader whatever stderr happened to hold, and git
    is a tool that writes advisories there on runs that also fail for an unrelated stated
    cause. Both streams, labelled, or the reader debugs the advisory.
    """
    repo = tmp_path / "checkout"
    repo.mkdir()
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    def runner(argv):
        return 128, "fatal: not a git repository", "warning: core.hooksPath is set"

    report = fr.collect(
        inputs_for(tmp_path, repo_path=repo, base_branch="master"), now=NOW, git_runner=runner
    )
    reason = report.health.branches.reason
    assert report.health.branches.available is False
    assert "stdout: fatal: not a git repository" in reason, (
        f"the stated cause is missing from the reason a human reads: {reason!r}"
    )
    assert "stderr: warning: core.hooksPath is set" in reason
    assert "exited 128" in reason


def test_a_failed_git_read_reason_can_be_written_into_the_registry(tmp_path):
    """The reason travels a markdown table, so a `|` from git would refuse the whole write.

    Checked against the registry's OWN guard rather than a re-implementation of it, because a
    copy of the rule in a test is a copy that stops tracking the rule.
    """
    repo = tmp_path / "checkout"
    repo.mkdir()
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    def runner(argv):
        return 1, "fatal: run `git log | head` first", "advisory\nwrapped onto two lines"

    report = fr.collect(
        inputs_for(tmp_path, repo_path=repo, base_branch="master"), now=NOW, git_runner=runner
    )
    reg_mod._require_inline(report.health.branches.reason, "reason")


# --------------------------------------------------------------------------- #
# Registry unavailable
# --------------------------------------------------------------------------- #
def test_a_missing_registry_is_reported_not_rendered_as_an_empty_healthy_fleet(tmp_path):
    make_spec(tmp_path, "alpha", "one", frontmatter="current_state: drafted\n")
    report = fr.collect(inputs_for(tmp_path), now=NOW)

    assert report.registry_available is False
    assert "could not read the registry" in report.registry_reason
    assert report.lanes.lanes == ()
    assert len(report.sections()) == len(fr.SECTION_ORDER)


# --------------------------------------------------------------------------- #
# Small helpers with load-bearing behaviour
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "hours,expected",
    [(None, "unknown"), (0.0, "0m"), (0.25, "15m"), (1.85, "1h51m"), (50.0, "2d 2h")],
)
def test_format_age(hours, expected):
    assert fr.format_age(hours) == expected


def test_an_unreadable_timestamp_never_renders_as_fresh():
    """A dead lane showing "beat 0m" is worse than one showing "beat unknown"."""
    row = reg_mod.AgentRow("auditor", "idle", "-", "-", "not-a-timestamp", "-", "-")
    lane = fr.classify_lane(row, now=NOW, silent_agents=frozenset())
    assert lane.beat == "unknown"
    assert lane.heartbeat_age_hours is None


def test_directive_name_prefers_the_bold_span():
    assert fr.directive_name("**hold merges** until the gate is green") == "hold merges"
    assert fr.directive_name("never force-push; it loses work") == "never force-push"
    long = "x" * 200
    assert len(fr.directive_name(long)) <= 72


# --------------------------------------------------------------------------- #
# Repairs — each of these pins a defect three adversarial reviews reproduced.
# Every one was mutation-checked: revert the fix in a copy of the tree and the
# named test goes red.
# --------------------------------------------------------------------------- #
def test_a_degenerate_registry_is_reported_not_raised(tmp_path):
    """`parse_registry` raises on a file missing any `## <Section>` heading — which a
    zero-byte, half-written, or CRLF registry all are. A traceback here kills the report in
    the one state where the human most needs the picture, because the report is what they
    read INSTEAD of the registry."""
    make_spec(tmp_path, "alpha", "x")
    registry = tmp_path / "projects" / "alpha" / "status" / "registry.md"

    for text in ("", "\x00\x01 not a registry", "# R\n\n## Agents\n\n" + AGENT_HEADER + "\n"):
        _write(registry, text)
        report = fr.collect(inputs_for(tmp_path), now=NOW)  # must not raise
        assert report.registry_available is False
        assert "registry" in report.registry_reason
        assert len(report.sections()) == len(fr.SECTION_ORDER)


def test_a_crlf_registry_is_reported_not_raised(tmp_path):
    """Its own hazard, and the one most likely on this fleet: `registry.py`'s section regex is
    `^## Agents[ \\t]*$`, so a single stray CR makes every section 'not found'."""
    make_spec(tmp_path, "alpha", "x")
    registry = make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    registry.write_bytes(registry.read_bytes().replace(b"\n", b"\r\n"))

    report = fr.collect(inputs_for(tmp_path), now=NOW)

    assert report.registry_available is False
    assert "could not parse the registry" in report.registry_reason
    # And the failure is visible in the one section a reader checks for gaps.
    assert report.counted_skipped.is_warning is True
    assert any(s.scan == "registry" for s in report.counted_skipped.skips)


def test_registry_rows_the_parser_dropped_are_counted_and_named(tmp_path):
    """The registry is a 183 KB scan, so it owes `counted N, skipped M` like any other.

    `parse_registry` drops an Agents row with too few cells or an unknown agent name and a
    Blockers row whose id does not match, all in silence — so an unescaped pipe in a work cell
    took a lane out of LANES, and a dropped blocker row took a blocker out of BLOCKED ON YOU, while
    COUNTED / SKIPPED read `counted 0, skipped 0`.
    """
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=(
            agent_row("lead-dev", "building", work="wide|cell", heartbeat=stamp(NOW, minutes=2))
            + "| jr-dev-1 | building | epic | - |\n"
            + agent_row("nobody", "building", heartbeat=stamp(NOW, minutes=2))
            + agent_row("jr-dev-2", "building", work="ok", heartbeat=stamp(NOW, minutes=2))
        ),
        blockers=(
            "| B-1 | jr-dev-1 | human | - | open | needs a call |\n"
            "| B-0002 | jr-dev-1 | human | - | open | second |\n"
        ),
        questions=qblock("Q-1", state="open", question="bad id", raised_at=stamp(NOW, hours=1)),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)

    scans = {scan.label: scan for scan in report.counted_skipped.scans}
    assert "registry" in scans, "the registry scan carries no `counted N, skipped M` at all"
    names = {s.name for s in scans["registry"].skipped}
    assert {"jr-dev-1", "nobody", "B-1", "Q-1"} <= names
    assert "lead-dev" in names  # over-wide: parsed, but with every column after the pipe shifted
    assert scans["registry"].is_warning is True
    assert report.counted_skipped.is_warning is True


def test_health_counts_carry_their_skip_count(tmp_path):
    """`inert_promotions` and `out_of_vocabulary_count` are both derived from the epic survey,
    so they are scan-derived numbers; rendered bare they are the confident-looking partial fact
    the HARD RULE forbids."""
    make_spec(tmp_path, "alpha", "healthy", frontmatter=f"current_state: {APPROVED}\n")
    make_broken_spec(tmp_path, "alpha", "broken.md")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    report = fr.collect(inputs_for(tmp_path), now=NOW)

    assert report.health.scan is not None
    assert report.health.scan.skipped_count == 1
    assert report.health.scan.is_warning is True


def test_intake_never_reports_a_pair_for_a_tracker_it_did_not_query(tmp_path):
    """`work_source.kind` DEFAULTS to "filesystem" and "none" is valid — both truthy strings.

    A falsiness guard let them through, `list_open_issues` short-circuited on its own rule and
    returned [] without shelling out, and INTAKE printed "0 untriaged / 0 selectable" as a
    measurement. That pair is the section's entire reason to exist.
    """
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    def never(argv):  # pragma: no cover — the point is that it is not called
        raise AssertionError(f"the tracker was queried: {argv}")

    for kind in ("filesystem", "none"):
        report = fr.collect(
            inputs_for(tmp_path, tracker_host_ref="owner/repo", tracker_kind=kind, tracker_cli="gh"),
            now=NOW,
            issue_runner=never,
        )
        assert report.intake.available is False, kind
        assert kind in report.intake.reason
        assert report.intake.is_empty is False


def test_intake_computes_a_non_zero_selectable_count(tmp_path):
    """The other half of the pair. Every earlier assertion on `selectable_count` was `== 0`, so
    deleting the computation outright was invisible."""
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    issues = [
        Issue(number=1, title="raw", labels=()),
        Issue(number=2, title="a bug to pick up", labels=("triaged",)),
        Issue(number=3, title="carved", labels=("triaged", "epic")),
    ]
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=issues)

    assert report.intake.untriaged_count == 1
    assert report.intake.selectable_count == 1
    assert [i.number for i in report.intake.selectable] == [2]


def test_intake_does_not_re_report_an_issue_a_carved_spec_already_links(tmp_path):
    """An issue a spec already carved is not untriaged, whatever its labels say. Without the
    cross-link every already-carved issue is offered back for triage on every report."""
    make_spec(
        tmp_path, "alpha", "carved-from-51",
        frontmatter='source: "carved from gh#51 by dev-manager"\n',
    )
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    issues = [Issue(number=51, title="already carved", labels=()),
              Issue(number=52, title="genuinely new", labels=())]

    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=issues)

    assert [i.number for i in report.intake.untriaged] == [52]
    assert report.intake.untriaged_count == 1


def test_intake_says_when_the_tracker_page_came_back_full(tmp_path):
    """A page that came back exactly full is indistinguishable from a truncated one, so the
    pair would be a partial count with nothing beside it saying so."""
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    payload = json.dumps([{"number": n, "title": f"i{n}", "labels": []} for n in range(1, 4)])
    inputs = inputs_for(
        tmp_path, tracker_host_ref="owner/repo", tracker_kind="github-issues",
        tracker_cli="gh", issue_limit=3,
    )
    report = fr.collect(inputs, now=NOW, issue_runner=lambda argv: (0, payload, ""))

    assert report.intake.capped is True
    assert report.intake.limit == 3
    assert report.intake.is_empty is False


# --------------------------------------------------------------------------- #
# INTAKE's fix-claimed count (E-0913): claims, never verdicts
# --------------------------------------------------------------------------- #
_FIX_ISSUES = json.dumps([
    {"number": 7, "title": "a triaged bug", "labels": [{"name": "triaged"}]},
    {"number": 8, "title": "raw", "labels": []},
])
_FIX_PRS = json.dumps([
    {"number": 30, "title": "E-0001 gh#7: the fix", "body": "Source: gh#7",
     "mergedAt": "2026-08-31T10:00:00Z", "mergeCommit": {"oid": "abcdef0123"},
     "headRefName": "e0001-the-fix"},
])


def _fix_runner(pr_reply):
    """Answers the open-issue read, and the merged-PR read with ``pr_reply``."""
    def run(argv):
        return pr_reply if argv[1:3] == ["pr", "list"] else (0, _FIX_ISSUES, "")
    return run


def _fix_inputs(tmp_path):
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    return inputs_for(tmp_path, tracker_host_ref="owner/repo", tracker_kind="github-issues")


def test_intake_counts_issues_a_merged_pr_claims_fixed(tmp_path):
    report = fr.collect(_fix_inputs(tmp_path), now=NOW,
                        issue_runner=_fix_runner((0, _FIX_PRS, "")))

    assert report.intake.fix_claimed_count == 1
    assert [c.number for c in report.intake.fix_claimed.items] == [7]
    assert report.intake.fix_claims_note == ""


def test_a_failed_merged_pr_read_leaves_the_count_unknown_and_the_pair_alone(tmp_path):
    good = fr.collect(_fix_inputs(tmp_path), now=NOW,
                      issue_runner=_fix_runner((0, _FIX_PRS, "")))
    bad = fr.collect(_fix_inputs(tmp_path), now=NOW,
                     issue_runner=_fix_runner((1, "", "could not resolve host")))

    assert bad.intake.fix_claimed_count is None
    assert "could not resolve host" in bad.intake.fix_claims_note
    assert bad.intake.available is True
    assert (bad.intake.untriaged_count, bad.intake.selectable_count) == (
        good.intake.untriaged_count, good.intake.selectable_count)
    assert (bad.intake.untriaged_count, bad.intake.selectable_count) == (1, 1)


def test_fix_claimed_is_on_both_sides_of_the_diff(tmp_path, monkeypatch):
    """`diff_since` reads `snapshot()` one report ago as `was` and `collect`'s core as `now`."""
    seen = {}
    real = fr.diff_since

    def spy(prior, current, **kw):
        seen["core"] = current
        return real(prior, current, **kw)

    monkeypatch.setattr(fr, "diff_since", spy)
    report = fr.collect(_fix_inputs(tmp_path), now=NOW,
                        issue_runner=_fix_runner((0, _FIX_PRS, "")))

    assert report.snapshot()["issues"]["fix_claimed"] == 1
    assert seen["core"]["issues"]["fix_claimed"] == 1


def test_intake_is_not_empty_while_issues_are_claimed_fixed(tmp_path):
    section = fr.IntakeSection(available=True, tracker_configured=True, fix_claimed_count=2)
    assert section.is_empty is False
    assert fr.IntakeSection(available=True, tracker_configured=True,
                            fix_claimed_count=0).is_empty is True
    # An unread count is not an empty one: the note must stay on screen.
    assert fr.IntakeSection(available=True, tracker_configured=True, fix_claimed_count=None,
                            fix_claims_note="merged PRs not read: boom").is_empty is False


def test_a_lane_with_no_readable_heartbeat_is_silent_not_live(tmp_path):
    """`stale_rows` skips an unparseable heartbeat so a fresh install does not report its whole
    roster as dead — a REAPING default. Inherited as a HEALTH verdict it rendered a lane that
    has never stamped a beat as a green node with "0 silent" beside it: the reassuring half of
    the exact stall this report was written for."""
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=(
            agent_row("lead-dev", "building", work="some-epic", heartbeat="-")
            + agent_row("jr-dev-1", "building", work="other-epic", heartbeat="garbage")
            + agent_row("steward", "idle", heartbeat=stamp(NOW, minutes=5),
                        eta=stamp(NOW, hours=-1))
        ),
        # The LIVE control this test needs beside its two SILENT rows. It earns the verdict
        # from its inbox now rather than from being unmeasurable.
        notes=_note("lead-dev", "steward"),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    by_agent = {lane.agent: lane for lane in report.lanes.lanes}

    assert by_agent["lead-dev"].state == fr.LANE_SILENT
    assert by_agent["jr-dev-1"].state == fr.LANE_SILENT
    assert "heartbeat unreadable or absent" in by_agent["lead-dev"].why
    assert report.lanes.silent_count == 2
    assert report.lanes.live_count == 1


def test_a_fresh_roster_is_not_reported_as_a_dead_fleet(tmp_path):
    """The narrow shape the reaping default protects: an `idle` row holding no work. A fresh
    install must stay non-alarming, or the honesty above becomes noise on day one."""
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents="".join(agent_row(a, "idle") for a in ("lead-dev", "jr-dev-1", "auditor")),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    assert report.lanes.silent_count == 0
    # A fresh roster is not DEAD, which is what this test protects. It is now split
    # more precisely: the seats with a lane and nothing published read DRY (quiet, needs
    # work), and the seats with no lane read LIVE. Neither is an alarm, and that is the
    # property - a fresh install must not look like a stalled fleet.
    assert report.lanes.reaped_count == 0
    assert report.lanes.live_count + _dry(report) == 3


def test_decided_carries_a_delegated_answer_the_raiser_already_applied(tmp_path):
    """`open -> answered -> applied` inside one window is the NORMAL completion — the raiser
    applies on its next wake (~30 min) and the report's ceiling is 24h. Reading only
    `questions_answered` dropped most delegated decisions from the section whose whole purpose
    is that the fleet's judgement is otherwise never seen."""
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        questions=(
            qblock("Q-0001", state="applied", raised_by="jr-dev-1", question="ship A?",
                   answer="yes", answered_by="dev-manager", answered_at=stamp(NOW, hours=2))
            + qblock("Q-0002", state="answered", raised_by="jr-dev-1", question="ship B?",
                     answer="yes", answered_by="dev-manager", answered_at=stamp(NOW, hours=1))
        ),
    )
    prior = snapshot_like(questions={"Q-0001": "open", "Q-0002": "open"})
    report = fr.collect(inputs_for(tmp_path), now=NOW, prior=prior)

    assert report.decided.windowed is True
    assert sorted(d.qid for d in report.decided.questions) == ["Q-0001", "Q-0002"]


def test_the_windowed_decided_path_shows_only_this_window(tmp_path):
    """The windowed branch was entirely untested; a mutation ignoring the window and listing
    every answered question in the registry survived the suite."""
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        questions=(
            qblock("Q-0001", state="answered", raised_by="lead-dev", question="old news",
                   answer="yes", answered_by="dev-manager", answered_at=stamp(NOW, days=4))
            + qblock("Q-0002", state="answered", raised_by="lead-dev", question="this window",
                     answer="yes", answered_by="dev-manager", answered_at=stamp(NOW, hours=1))
        ),
    )
    prior = snapshot_like(questions={"Q-0001": "answered", "Q-0002": "open"})
    report = fr.collect(inputs_for(tmp_path), now=NOW, prior=prior)

    assert report.decided.windowed is True
    assert [d.qid for d in report.decided.questions] == ["Q-0002"]


def test_a_skipped_file_is_named_correctly_when_a_path_holds_a_parenthesis(tmp_path):
    """The capture reads `LogRecord.args`, not the rendered message.

    `(?P<path>.+?)` is non-greedy and stops at the FIRST open paren, which is a path component
    whenever one exists — a `Docs (old)` data root, a `foo (draft).md` spec, any Windows `(2)`
    duplicate. The report then named a file that does not exist and never named the one it
    missed, which is worse than a bare count.
    """
    root = tmp_path / "Docs (old)"
    make_spec(root, "alpha", "healthy")
    make_broken_spec(root, "alpha", "2026-08-31-broken (draft).md")

    _, scan = fr.survey_with_skips(root, PAST_CUTOFF, project="alpha")

    assert [s.name for s in scan.skipped] == ["2026-08-31-broken (draft).md"]
    assert scan.skipped[0].parent == "specs"


def test_a_skip_reason_is_one_line_and_names_no_machine_path(tmp_path):
    """`SkippedFile.name` is deliberately not an absolute path "because an absolute path leaks
    a machine layout into a page that gets archived and shared" — and `reason` carried one
    verbatim, plus embedded newlines that break the push's one-fact-per-line rule."""
    make_spec(tmp_path, "alpha", "healthy")
    _write(tmp_path / "projects" / "alpha" / "design" / "specs" / "badyaml.md",
           "---\nslug: [a, b\n  c: {x\n---\n\nbody\n")
    (tmp_path / "projects" / "alpha" / "design" / "specs" / "adir.md").mkdir(parents=True)

    _, scan = fr.survey_with_skips(tmp_path, PAST_CUTOFF, project="alpha")

    assert scan.skipped_count == 2
    for skipped in scan.skipped:
        assert "\n" not in skipped.reason
        for spelling in fr.redaction_prefixes(tmp_path):
            assert spelling not in skipped.reason, skipped.reason


def test_an_epic_archived_inside_the_window_still_moves(tmp_path):
    """Without `include_archived=True` the epic simply leaves the corpus: no move, no SHIPPED
    row, no WORK row — it disappears from the report in silence."""
    make_spec(tmp_path, "alpha", "retired", frontmatter="current_state: archived\n")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    prior = snapshot_like(epics={"retired": "running"}, lifecycle={"retired": "running"})

    report = fr.collect(inputs_for(tmp_path), now=NOW, prior=prior)

    assert fr.EpicMove("retired", "running", "archived") in report.changed_since.lifecycle_moves
    assert report.changed_since.is_empty is False
    # ...and it is still not offered as claimable work.
    assert "retired" not in [row.slug for row in report.work.approved_claimable]


def test_the_archive_path_the_skill_writes_is_the_path_the_next_run_reads(tmp_path):
    """A ROUND TRIP, because writer-path against reader-path was the only thing not checked.

    `ArchiveInfo.directory` was the leaf name, so `<vault.root>/<directory>/<filename>` dropped
    the `fleet-reports` component that `find_latest_archive` globs. Every run wrote where no run
    looked; the diff had no prior on run 1, run 2 and run N, and nothing errored.
    """
    vault = tmp_path / "vault"
    make_spec(tmp_path, "alpha", "one", frontmatter=f"current_state: {APPROVED}\n")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    inputs = inputs_for(tmp_path, vault_root=vault)

    first = fr.collect(inputs, now=NOW)
    assert first.changed_since.no_prior is True
    assert Path(first.archive.path) == fr.archive_directory(inputs) / first.archive.filename
    assert Path(first.archive.directory) == fr.archive_directory(inputs)

    # Write the page exactly where Step 5b says to, then collect again.
    _write(Path(first.archive.path), "# report\n\n" + first.snapshot_fence() + "\n")
    second = fr.collect(inputs, now=NOW + timedelta(hours=1))

    assert second.changed_since.no_prior is False, (
        "the report the skill was told to write is not the report the next run reads"
    )
    assert second.changed_since.prior_generated_at == first.generated_at


def test_a_running_epic_carries_the_eta_and_lane_that_hold_it(tmp_path):
    """The spec's WORK row is "in progress WITH eta"; nothing exercised the lookup, so the
    column could silently go blank.

    IT WAS BLANK IN PRODUCTION FOR THE WHOLE LIFE OF THIS TEST, and the test was green. The
    fixture put a bare SLUG in the Agents `work` cell, which is what made the lookup match --
    and `status --work` has never accepted one: `_E_ID` refuses anything that is not an E- id,
    so no real board could hold the value being asserted against. On the live board the cell
    held `E-0220`, the lookup was keyed on `r.slug`, and every in-progress epic rendered with an
    empty eta and an empty lane. Nothing failed, because a missing eta and an unstarted epic
    look the same in the output.

    The fixture now carries a real id, which is only joinable at all because E-0260 put the
    epic's NAME inside it.
    """
    make_spec(tmp_path, "alpha", "in-flight", frontmatter=f"current_state: {APPROVED}\n")
    make_status(tmp_path, "alpha", "in-flight", status="running")
    eta = stamp(NOW, hours=-1)
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("jr-dev-1", "building", work="E-0007-in-flight",
                         heartbeat=stamp(NOW, minutes=4), eta=eta),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)

    rows = {row.slug: row for row in report.work.in_progress}
    assert rows["in-flight"].eta == eta
    assert rows["in-flight"].lane == "jr-dev-1"


def test_the_eta_lane_join_matches_a_TRUNCATED_name_and_not_a_mere_prefix(tmp_path):
    """The id carries the first four slug tokens, not the slug, so the join has to derive the
    same truncation from both sides.

    The negative half is the one that matters: a DIFFERENT epic whose slug merely starts the
    same way must not inherit the first one's lane and eta. A `startswith` test would hand
    `acme-3-5-chat-history` the row belonging to `acme-3-5-chat`.
    """
    make_spec(tmp_path, "alpha", "seam-guard-for-the-publisher",
              frontmatter=f"current_state: {APPROVED}\n")
    make_status(tmp_path, "alpha", "seam-guard-for-the-publisher", status="running")
    make_spec(tmp_path, "alpha", "seam-guard-for-the-reader",
              frontmatter=f"current_state: {APPROVED}\n")
    make_status(tmp_path, "alpha", "seam-guard-for-the-reader", status="running")
    eta = stamp(NOW, hours=-1)
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        # Four tokens of the first slug: `seam-guard-for-the`, which is ALSO the first four
        # tokens of the second. Both resolve, and to the same row -- so the assertion below is
        # about the pair that does NOT share a truncation.
        agents=agent_row("jr-dev-2", "building", work="E-0011-seam-guard-for-the",
                         heartbeat=stamp(NOW, minutes=4), eta=eta),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    rows = {row.slug: row for row in report.work.in_progress}
    assert rows["seam-guard-for-the-publisher"].lane == "jr-dev-2"

    # THE NEGATIVE HALF, which this test promised in its docstring and did not assert. Review
    # caught it: both slugs above truncate to `seam-guard-for-the`, so they cannot tell name
    # EQUALITY from `startswith` -- a `startswith` implementation passed unchanged. This slug
    # truncates to `seam-guard-for-there`, which STARTS WITH the id's name and is not equal to
    # it, so it is the one fixture that separates the two implementations.
    make_spec(tmp_path, "alpha", "seam-guard-for-there-and-back",
              frontmatter=f"current_state: {APPROVED}\n")
    make_status(tmp_path, "alpha", "seam-guard-for-there-and-back", status="running")
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    rows = {row.slug: row for row in report.work.in_progress}
    assert rows["seam-guard-for-the-publisher"].lane == "jr-dev-2"
    assert rows["seam-guard-for-there-and-back"].lane != "jr-dev-2", (
        "a DIFFERENT epic whose truncated name merely STARTS WITH the id's name inherited that "
        "id's lane; the join is matching on a prefix rather than on the name"
    )

    # A legacy bare id has no name, so it joins to nothing -- a blank cell, never a wrong one.
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("jr-dev-2", "building", work="E-0220",
                         heartbeat=stamp(NOW, minutes=4), eta=eta),
    )
    rows = {row.slug: row for row in fr.collect(inputs_for(tmp_path), now=NOW).work.in_progress}
    assert rows["seam-guard-for-the-publisher"].lane == ""
    assert rows["seam-guard-for-the-publisher"].eta == ""


def test_health_is_not_empty_while_it_carries_directives(tmp_path):
    """"An empty section reads as 'checked, nothing there'" — so a section that renders three
    directives in force must never claim to be empty."""
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        directives="- **hold merges** until the gate is green\n",
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    assert report.health.directive_count == 1
    assert report.health.is_empty is False


def test_an_uncapped_collection_is_available_for_the_walk_verbs(tmp_path):
    """`intake` and `blockers` walk items ONE AT A TIME, and the walk cannot reach past the
    cap. A negative `rows_per_section` is the documented seam for that."""
    for i in range(15):
        make_spec(tmp_path, "alpha", f"epic-{i:02d}",
                  frontmatter=f"current_state: {APPROVED}\nproject: alpha\npriority: {i}\n")
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")

    capped = fr.collect(inputs_for(tmp_path), now=NOW).work.approved_claimable
    full = fr.collect(inputs_for(tmp_path, rows_per_section=-1), now=NOW).work.approved_claimable

    assert capped.shown == 10 and capped.truncated is True
    assert full.shown == 15 and full.truncated is False
# --------------------------------------------------------------------------- #
# DRY and BLOCKED (E-0290 change 7) - derived lane conditions, not agent states
# --------------------------------------------------------------------------- #
ALL_SEATS = ("auditor", "dev-manager", "jr-dev-1", "jr-dev-2", "jr-dev-3", "lead-dev", "steward")


def _alive_roster() -> str:
    """Six seats, all plainly alive, so any non-LIVE state in these tests came from
    the derivation under test rather than from staleness."""
    beat = stamp(NOW, hours=0)
    return "".join(
        agent_row(a, "idle", heartbeat=beat, eta="2099-01-01T00:00Z") for a in ALL_SEATS
    )


def _states_registry(tmp_path, **kw) -> None:
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=_alive_roster(),
        **kw,
    )


def _lane_state(tmp_path, agent: str):
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    return {lane.agent: lane for lane in report.lanes.lanes}[agent]


def _open_q(raised_by: str, qid: str = "Q-0001") -> str:
    return qblock(qid, state="open", urgency="waiting", raised_by=raised_by,
                  question="x", default="y")


def test_a_lane_with_no_claim_and_nothing_ready_is_DRY(tmp_path):
    """The condition p190/p195 left with no indicator: before them a dry lane
    announced itself by halting; after them it re-checks hourly forever and rendered
    as a green LIVE node identical to a lane shipping code."""
    _states_registry(tmp_path)
    assert _lane_state(tmp_path, "jr-dev-1").state == fr.LANE_DRY


def test_a_lane_holding_a_CLAIM_is_not_dry(tmp_path):
    _states_registry(tmp_path, queue="| E-0001 | work | jr-dev-1 | jr-dev-1 | building | - |  |\n")
    assert _lane_state(tmp_path, "jr-dev-1").state == fr.LANE_LIVE


def test_a_lane_with_a_READY_row_published_to_it_is_not_dry(tmp_path):
    """Nothing claimed yet, but work is waiting - the lane needs no attention."""
    _states_registry(tmp_path, queue="| E-0001 | work | jr-dev-1 | - | ready | - |  |\n")
    assert _lane_state(tmp_path, "jr-dev-1").state == fr.LANE_LIVE


def test_a_ready_row_in_ANOTHER_lane_does_not_rescue_this_one(tmp_path):
    """The join has to be lane-scoped. A queue with rows in it is not the same as a
    queue with rows for YOU, and reading the count instead of the lane is the easy
    way to get a permanently-green board."""
    _states_registry(tmp_path, queue="| E-0001 | work | jr-dev-2 | - | ready | - |  |\n")
    assert _lane_state(tmp_path, "jr-dev-1").state == fr.LANE_DRY
    assert _lane_state(tmp_path, "jr-dev-2").state == fr.LANE_LIVE


def test_a_non_ready_row_in_the_lane_does_not_count_as_work(tmp_path):
    """A parked row is not claimable. Counting any row in the lane would make a lane
    full of parked or finished work look busy forever."""
    _states_registry(tmp_path, queue="| E-0001 | work | jr-dev-1 | - | parked | Q-1 |  |\n")
    assert _lane_state(tmp_path, "jr-dev-1").state == fr.LANE_DRY


def _note(actor: str, to: str, body: str = "something to do", stamp: str = "2026-08-31T17:00Z") -> str:
    return f"- **Notes** — 1 live\n  - [{stamp}] {actor} -> {to}: {body}"


@pytest.mark.parametrize("agent", ["auditor", "steward", "dev-manager"])
def test_a_LANE_LESS_seat_with_an_empty_inbox_renders_DRY(tmp_path, agent):
    """THE CLASS THIS EPIC CLOSES, and it replaces a test that asserted the opposite.

    That test — `test_DRY_NEVER_renders_on_a_seat_that_has_no_lane` — was right about its
    reason and wrong about its remedy. Colouring `auditor` / `steward` / `dev-manager` dry
    against a Queue lane they do not have WOULD assert something false, so the old code
    passed `lane_has_work=None` for them and `classify_lane` has exactly one unproductivity
    branch, `if lane_has_work is False`. The consequence is what nobody wrote down: the three
    lane-less seats could not be reported as unproductive by any input at all, and rendered
    LIVE-green forever. A seat that cannot fail a check is not passing it.

    The remedy is not to lie about a lane. It is to ask each seat the question its own work
    arrives through — these three are fed by the routed-note inbox, which is why each of them
    is instructed to drain it at the wake read."""
    _states_registry(tmp_path)
    lane = _lane_state(tmp_path, agent)
    assert lane.state == fr.LANE_DRY
    assert fr.INBOX_BASIS in lane.why


@pytest.mark.parametrize("agent", ["auditor", "steward", "dev-manager"])
def test_a_LANE_LESS_seat_with_a_routed_note_is_not_dry(tmp_path, agent):
    """The other half, without which the test above passes for a predicate hard-wired to
    False — which would be the same defect with the colour reversed."""
    _states_registry(tmp_path, notes=_note("lead-dev", agent))
    assert _lane_state(tmp_path, agent).state == fr.LANE_LIVE


@pytest.mark.parametrize("agent", ["auditor", "steward", "dev-manager"])
def test_a_STRUCK_note_does_not_keep_a_lane_less_seat_busy(tmp_path, agent):
    """Striking is how a recipient records that it consumed a note. Counting struck notes
    would make every long-lived seat permanently busy, which is the always-True predicate
    that reports nothing."""
    _states_registry(tmp_path, notes=_note("lead-dev", agent, body="done~~").replace(
        "  - [", "  - ~~["))
    assert _lane_state(tmp_path, agent).state == fr.LANE_DRY


@pytest.mark.parametrize("agent", ["auditor", "steward", "dev-manager"])
def test_a_FLEET_broadcast_does_not_count_as_this_seat_s_work(tmp_path, agent):
    """`fleet` is a broadcast address, not an assignment. Counting it would mark all three
    lane-less seats busy the moment anyone broadcasts anything — the same always-True
    predicate, reached by a different route. Matches `registry._notes_routed_to`, which
    excludes `-> fleet` for its own version of this reason."""
    _states_registry(tmp_path, notes=_note("lead-dev", "fleet"))
    assert _lane_state(tmp_path, agent).state == fr.LANE_DRY


def test_a_note_routed_to_ONE_lane_less_seat_leaves_the_others_dry(tmp_path):
    """Lane-scoped, the way the builders' predicate is. Reading "any live note" instead of
    "a note addressed to ME" is the easy way back to a permanently-green board."""
    _states_registry(tmp_path, notes=_note("lead-dev", "steward"))
    assert _lane_state(tmp_path, "steward").state == fr.LANE_LIVE
    assert _lane_state(tmp_path, "auditor").state == fr.LANE_DRY
    assert _lane_state(tmp_path, "dev-manager").state == fr.LANE_DRY


def test_an_unparseable_note_stamp_does_not_hide_the_note(tmp_path):
    """Routing is knowable without a readable timestamp. Going through
    `_parse_note_prefix` — which returns None on a stamp it cannot parse — would drop the
    note from the count and report the seat dry while real work sat in its inbox."""
    _states_registry(tmp_path, notes=_note("lead-dev", "steward", stamp="not-a-date"))
    assert _lane_state(tmp_path, "steward").state == fr.LANE_LIVE


def test_dry_count_counts_only_the_DRY_lanes(tmp_path):
    """Asserted against a MIXED board, not an all-dry one. `== len(lanes)` passes for any
    field wired to a lane count at all - including `len(self.lanes)` itself - so it could not
    have caught a miswiring. The board here holds one claimed builder lane and one seat with a
    routed note, so the expected number is neither 0 nor the roster size."""
    _states_registry(
        tmp_path,
        queue="| E-0001 | work | jr-dev-1 | jr-dev-1 | building | - |  |\n",
        notes=_note("lead-dev", "steward"),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    by_state = {}
    for lane in report.lanes.lanes:
        by_state[lane.state] = by_state.get(lane.state, 0) + 1
    assert report.lanes.dry_count == by_state.get(fr.LANE_DRY)
    assert 0 < report.lanes.dry_count < len(report.lanes.lanes)


def test_blocked_count_counts_only_the_BLOCKED_lanes(tmp_path):
    """The tally computed BLOCKED and the constructor dropped it, so the section could
    render a BLOCKED node and still report no blocked lanes — a discard, which is worse
    than a wrong number because nothing looks broken.

    Mixed board for the same reason `dry_count`'s test uses one: on an all-blocked board
    `== len(lanes)` and `== dry_count` and `len(self.lanes)` all agree, so any of the
    plausible miswirings would pass. Here one claimed lane is blocked by its own open
    question and the rest are dry, so the three answers are different numbers."""
    _states_registry(
        tmp_path,
        queue="| E-0001 | work | jr-dev-1 | jr-dev-1 | building | - |  |\n",
        questions=_open_q("jr-dev-1"),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    by_state = {}
    for lane in report.lanes.lanes:
        by_state[lane.state] = by_state.get(lane.state, 0) + 1
    assert report.lanes.blocked_count == by_state.get(fr.LANE_BLOCKED)
    assert 0 < report.lanes.blocked_count < len(report.lanes.lanes)
    # Not wired to the neighbouring count: they are tallied on adjacent lines from the
    # same dict, which is exactly where a copy-paste puts LANE_DRY into both slots.
    assert report.lanes.blocked_count != report.lanes.dry_count


def test_an_open_question_the_seat_RAISED_makes_it_BLOCKED(tmp_path):
    _states_registry(
        tmp_path,
        queue="| E-0001 | work | jr-dev-1 | jr-dev-1 | building | - |  |\n",
        questions=_open_q("jr-dev-1"),
    )
    lane = _lane_state(tmp_path, "jr-dev-1")
    assert lane.state == fr.LANE_BLOCKED
    assert "Q-0001" in lane.why


def test_a_question_RAISED_BY_ANOTHER_SEAT_does_not_block_this_one(tmp_path):
    """A question is answered FOR its raiser. Reading the field the other way round
    marks the wrong seat as waiting, and every seat looks blocked at once."""
    _states_registry(
        tmp_path,
        queue="| E-0001 | work | jr-dev-1 | jr-dev-1 | building | - |  |\n",
        questions=_open_q("lead-dev"),
    )
    assert _lane_state(tmp_path, "jr-dev-1").state == fr.LANE_LIVE


def test_an_ANSWERED_question_does_not_block(tmp_path):
    _states_registry(
        tmp_path,
        queue="| E-0001 | work | jr-dev-1 | jr-dev-1 | building | - |  |\n",
        questions=qblock("Q-0001", state="applied", urgency="waiting",
                         raised_by="jr-dev-1", question="x", default="y"),
    )
    assert _lane_state(tmp_path, "jr-dev-1").state == fr.LANE_LIVE


def test_an_open_blocker_the_seat_OWNS_makes_it_BLOCKED(tmp_path):
    """Owned, not raised - a blocker is resolved BY its owner, the mirror image of a
    question, and getting that pair backwards is the whole risk in this derivation."""
    _states_registry(
        tmp_path,
        queue="| E-0001 | work | lead | lead-dev | building | - |  |\n",
        blockers="| B-0001 | jr-dev-1 | lead-dev | E-0001 | open | something |\n",
    )
    assert _lane_state(tmp_path, "lead-dev").state == fr.LANE_BLOCKED
    assert _lane_state(tmp_path, "jr-dev-1").state != fr.LANE_BLOCKED


def test_BLOCKED_applies_to_a_seat_with_no_lane(tmp_path):
    """Unlike DRY. Any seat can raise a question, so any seat can be waiting on one."""
    _states_registry(tmp_path, questions=_open_q("steward"))
    assert _lane_state(tmp_path, "steward").state == fr.LANE_BLOCKED


def test_BLOCKED_beats_DRY_when_a_lane_is_both(tmp_path):
    """A decision waiting on a human is more actionable than "nothing published yet",
    and a dry lane that also raised a question is honestly both."""
    _states_registry(tmp_path, questions=_open_q("jr-dev-1"))
    assert _lane_state(tmp_path, "jr-dev-1").state == fr.LANE_BLOCKED


def test_a_PROBLEM_state_still_beats_both(tmp_path):
    """DRY and BLOCKED refine what would otherwise be LIVE. A reaped or silent lane
    is not merely dry, and reporting it as dry would hide the thing needing a human."""
    stale = stamp(NOW, hours=48)
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=(
            agent_row("jr-dev-1", "idle", phase=fr._registry.REAPED_PHASE, heartbeat=stale)
            + agent_row("jr-dev-2", "building", work="E-9", phase="working", heartbeat=stale)
        ),
        questions=_open_q("jr-dev-1"),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    by_agent = {lane.agent: lane for lane in report.lanes.lanes}
    assert by_agent["jr-dev-1"].state == fr.LANE_REAPED
    assert by_agent["jr-dev-2"].state == fr.LANE_SILENT
# --------------------------------------------------------------------------- #
# The no-work signal (E-0290 change 5) - the channel p195 traded lead-dev's halt for
# --------------------------------------------------------------------------- #
def _report(*, claimable=0, drafted=0, selectable=0, shipped=0, claimable_now=None) -> fr.FleetReport:
    """A FleetReport carrying only the four numbers this property reads.

    Constructed rather than surveyed, exactly as the spec requires: the live corpus
    will not produce an all-zero board on demand, and a requirement that can only be
    observed when it happens to occur is a requirement that ships unproven.
    """
    return fr.FleetReport(
        generated_at="2026-08-31T18:00:00+00:00",
        project="alpha",
        rows_per_section=10,
        blocked=fr.BlockedSection(),
        decided=fr.DecidedSection(),
        intake=fr.IntakeSection(selectable_count=selectable),
        work=fr.WorkSection(
            # BOTH, from the one parameter, because this helper's `claimable` means "there is
            # claimable work" and gh#298 split that idea across two fields. Setting only the
            # old one would leave `claimable_now` at zero and make every caller here read as
            # an empty board - a helper quietly deciding the property it exists to exercise.
            approved_claimable=fr.Rows(total=claimable),
            claimable_now=fr.Rows(total=claimable if claimable_now is None else claimable_now),
            drafted_count=drafted,
            shipped=fr.Rows(total=shipped),
        ),
        done=fr.DoneSection(),
        lanes=fr.LanesSection(),
        health=fr.HealthSection(),
        counted_skipped=fr.CountedSkippedSection(),
        changed_since=fr.ChangedSince(),
        archive=fr.ArchiveInfo(),
    )


def test_all_three_zero_is_the_no_work_condition():
    """THE REQUIREMENT lead-dev's halt was traded for. Deciding p195 as option A
    removed the only channel that seat had for telling a human the product had run
    out of work, and the report is now that channel."""
    assert _report().no_work_anywhere is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"claimable": 1},
        {"drafted": 1},
        {"selectable": 1},
    ],
    ids=["claimable", "drafted", "selectable"],
)
def test_any_ONE_source_holding_work_defeats_it(kwargs):
    """"When AND ONLY WHEN all three are zero." Each term is load-bearing on its own,
    and a property that ignored one would report no-work while work sat in it."""
    assert _report(**kwargs).no_work_anywhere is False


def test_SHIPPED_does_not_defeat_it_and_this_is_why_it_is_not_is_empty():
    """The case `WorkSection.is_empty` gets wrong, and the one that matters most.

    `is_empty` also reads `shipped`, so a day on which something shipped but no work
    remains answers False - and that is precisely the day a human needs told. Shipped
    is a diff window over the previous report; it says nothing about work remaining.
    """
    report = _report(shipped=3)
    assert report.work.is_empty is False       # the older, wider predicate
    assert report.no_work_anywhere is True     # the one this epic adds


def test_the_sentence_states_the_fact_rather_than_printing_zeros():
    """A reader combining three zeros into an inference is the thing this replaces,
    so the constant has to be a sentence and name all three sources."""
    sentence = fr.FleetReport.NO_WORK_SENTENCE
    assert "no work anywhere" in sentence.lower()
    for source in ("claimable", "drafted", "selectable"):
        assert source in sentence.lower(), f"the sentence does not name {source}"
# --------------------------------------------------------------------------- #
# DONE (E-0290 change 2) - merged PRs, and the reading it is NOT
# --------------------------------------------------------------------------- #
def _pr_json(*prs) -> str:
    import json as _json

    return _json.dumps([
        {"number": n, "title": t, "mergedAt": "2026-08-31T10:00:00Z",
         "mergeCommit": {"oid": sha}}
        for n, t, sha in prs
    ])


def _runner(rc=0, out="[]", err=""):
    def run(argv):
        run.argv = argv
        return rc, out, err

    run.argv = None
    return run


def _collect_with(tmp_path, runner, **kw):
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    return fr.collect(
        inputs_for(tmp_path, tracker_host_ref="owner/repo", **kw),
        now=NOW,
        git_runner=runner,
    )


def test_DONE_sits_between_WORK_and_LANES():
    """It reads as the natural other half of WORK, and answers the question a status
    board is most often opened for. Position is part of the change, not incidental."""
    order = list(fr.SECTION_ORDER)
    assert order.index("done") == order.index("work") + 1
    assert order.index("done") == order.index("lanes") - 1


def test_DONE_renders_merged_prs_with_their_numbers_and_shas(tmp_path):
    runner = _runner(out=_pr_json((94, "The readability pass", "7b5f222abc"),
                                  (93, "WORK names the no-work condition", "eac1880def")))
    report = _collect_with(tmp_path, runner)
    assert report.done.available is True
    assert [r.number for r in report.done.rows.items] == ["94", "93"]
    assert [r.sha for r in report.done.rows.items] == ["7b5f222", "eac1880"]
    assert "readability" in report.done.rows.items[0].title


def test_DONE_SAYS_WHICH_READING_IT_IS_SHOWING(tmp_path):
    """THE POINT OF THE SECTION, and the spec's own requirement.

    WORK's `shipped` comes from the snapshot diff - correct but narrow, since it can
    only show what changed since the LAST report, so on a ten-minute cadence it is
    almost always empty while ten things shipped that day. DONE reads merged PRs.
    Both are true and they disagree; a reader seeing "0 shipped" beside six merged
    rows must be able to tell that is not a contradiction.
    """
    report = _collect_with(tmp_path, _runner(out=_pr_json((1, "a thing", "aaaaaaa"))))
    note = report.done.source_note.lower()
    assert "merged pull requests" in note
    assert "shipped" in note, "the note does not name the reading it is NOT"


def test_no_tracker_configured_is_reported_not_silently_empty(tmp_path):
    """A section that prints nothing for an unreachable tracker is worse than one
    that says it could not look - the reader cannot tell "quiet day" from "broken"."""
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    report = fr.collect(inputs_for(tmp_path), now=NOW, git_runner=_runner())
    assert report.done.available is False
    assert "no tracker" in report.done.reason


def test_a_failing_gh_call_carries_its_error_and_does_not_crash(tmp_path):
    report = _collect_with(tmp_path, _runner(rc=1, out="", err="gh: not authenticated"))
    assert report.done.available is False
    assert "not authenticated" in report.done.reason


def test_unreadable_json_is_reported_rather_than_raised(tmp_path):
    report = _collect_with(tmp_path, _runner(out="this is not json"))
    assert report.done.available is False
    assert "unreadable" in report.done.reason.lower()


def test_the_read_goes_through_the_EXISTING_git_runner_seam(tmp_path):
    """A second seam would be a second thing every test has to stub, and two seams
    drift. The argv shape is asserted so a silent change to the query is visible."""
    runner = _runner(out=_pr_json((1, "x", "aaaaaaa")))
    _collect_with(tmp_path, runner)
    assert runner.argv[:3] == ["gh", "pr", "list"]
    assert "--state" in runner.argv and "merged" in runner.argv
    assert "owner/repo" in runner.argv


def test_shipped_rows_get_their_sha_and_pr_when_a_merged_PR_NAMES_the_slug():
    rows = [fr.ShippedRow(slug="fleet-report-skill")]
    prs = (fr.MergedPR(number="73", title="Add the fleet-report-skill epic", sha="5aa7593"),)
    out = fr._attach_pr_evidence(rows, prs)
    assert out[0].sha == "5aa7593"
    assert out[0].pr == "73"


def test_a_near_miss_is_left_BLANK_rather_than_guessed():
    """A wrong sha beside a slug is worse than no sha: the reader has no way to tell
    a guess from a lookup, and the whole value of the field is that it is checkable."""
    rows = [fr.ShippedRow(slug="fleet-report-skill")]
    prs = (fr.MergedPR(number="99", title="Something unrelated entirely", sha="deadbee"),)
    out = fr._attach_pr_evidence(rows, prs)
    assert out[0].sha == ""
    assert out[0].pr == ""


def test_WORK_shipped_and_DONE_may_legitimately_disagree(tmp_path):
    """The case the spec names: nothing moved since the last report, and yet the day
    was not empty. Both numbers are right, which is why DONE names its source."""
    report = _collect_with(tmp_path, _runner(out=_pr_json((1, "a", "aaaaaaa"),
                                                          (2, "b", "bbbbbbb"))))
    assert report.work.shipped.total == 0      # nothing moved since the prior report
    assert report.done.rows.total == 2         # two things merged all the same
    assert report.done.source_note, "a disagreement with no explanation is alarming"


# --------------------------------------------------------------------------- #
# PRODUCTION (E-0224 change 5) - the only signal that answers "is it SHIPPING"
#
# Every other signal on a lane row answers "is it alive". The case this epic was written
# from is a seat that reported the same blocked verdict for five consecutive wakes and
# shipped nothing, with every liveness signal green the whole time: fresh heartbeat,
# declared eta, plausible phase. A lane green on liveness and zero on production for N
# wakes is the shape that had no field to render in.
# --------------------------------------------------------------------------- #
def _production_registry(tmp_path, *, queue="", log="") -> Path:
    path = make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=_alive_roster(),
        queue=queue,
    )
    if log:
        (path.parent / "registry-log.md").write_text(log, encoding="utf-8")
    return path


def _lanes_of(tmp_path, runner):
    report = fr.collect(
        inputs_for(tmp_path, tracker_host_ref="owner/repo"), now=NOW, git_runner=runner
    )
    return report, {lane.agent: lane for lane in report.lanes.lanes}


def test_production_counts_merged_prs_against_the_seat_that_held_the_epic(tmp_path):
    _production_registry(
        tmp_path,
        queue="| E-0224 | live row | jr-dev-1 | jr-dev-1 | building | - |  |\n",
    )
    runner = _runner(out=_pr_json((270, "Do the thing (E-0224)", "aaaaaaa")))
    report, by_agent = _lanes_of(tmp_path, runner)
    assert report.lanes.production_available is True
    assert by_agent["jr-dev-1"].merged_in_window == 1
    assert by_agent["lead-dev"].merged_in_window == 0
    assert report.lanes.production_unattributed == 0


def test_production_resolves_an_epic_that_has_already_been_CULLED_off_the_board(tmp_path):
    """WITHOUT THIS THE SIGNAL IS VACUOUS. A shipped row is culled within a wake or two,
    so a map built from the live Queue alone resolves almost nothing and every lane reads
    zero - which is the confidently-wrong reading this whole epic is about. The cull log
    is the append-only archive of those rows, with their claimed_by cell intact."""
    _production_registry(
        tmp_path,
        log="<!-- culled 2026-08-30T17:26Z -->\n"
            "| E-0136 | overflow matcher | jr-dev-1 | jr-dev-1 | done | - |  |\n",
    )
    _, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((267, "Fix it (E-0136)", "bbbbbbb"))))
    assert by_agent["jr-dev-1"].merged_in_window == 1


def test_a_LIVE_row_beats_a_logged_one_for_the_same_id(tmp_path):
    """A re-published id is held by whoever holds it NOW. The log is history."""
    _production_registry(
        tmp_path,
        queue="| E-0136 | re-published | jr-dev-2 | jr-dev-2 | building | - |  |\n",
        log="| E-0136 | overflow matcher | jr-dev-1 | jr-dev-1 | done | - |  |\n",
    )
    _, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((267, "Fix it (E-0136)", "bbbbbbb"))))
    assert by_agent["jr-dev-2"].merged_in_window == 1
    assert by_agent["jr-dev-1"].merged_in_window == 0


def test_a_bug_pr_titling_its_ISSUE_still_attributes(tmp_path):
    """Measured against the real merged history rather than assumed: epics title themselves
    (E-0236) and bug rows title themselves (gh#78). A key that reads only the first silently
    drops every bug fix out of the production count."""
    _production_registry(
        tmp_path,
        queue="| E-0300 | fix gh#78 collection scope | jr-dev-1 | jr-dev-1 | building | - |  |\n",
    )
    _, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((270, "Collect it (gh#78)", "ccccccc"))))
    assert by_agent["jr-dev-1"].merged_in_window == 1


def test_an_AMBIGUOUS_issue_number_is_left_unattributed_rather_than_guessed(tmp_path):
    """Two seats holding rows that name the same issue means the number identifies no one
    seat. A guessed attribution is worse than an unattributed count, because a reader cannot
    tell a guess from a lookup - the same discipline _attach_pr_evidence already keeps."""
    _production_registry(
        tmp_path,
        queue="| E-0300 | fix gh#78 here | jr-dev-1 | jr-dev-1 | building | - |  |\n"
              "| E-0301 | also gh#78 | jr-dev-2 | jr-dev-2 | building | - |  |\n",
    )
    report, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((270, "Do it (gh#78)", "ccccccc"))))
    assert by_agent["jr-dev-1"].merged_in_window == 0
    assert by_agent["jr-dev-2"].merged_in_window == 0
    assert report.lanes.production_unattributed == 1


def test_an_unresolvable_pr_is_COUNTED_as_unattributed_not_dropped(tmp_path):
    """The denominator is what tells a reader whether "lead-dev: 0" means the lane shipped
    nothing or the attribution key missed."""
    _production_registry(tmp_path)
    report, _ = _lanes_of(tmp_path, _runner(out=_pr_json((99, "A title naming nothing", "ddddddd"))))
    assert report.lanes.production_unattributed == 1


def test_an_UNREACHABLE_tracker_leaves_production_None_and_NEVER_zero(tmp_path):
    """THE FAILURE THIS EPIC IS ABOUT, in miniature. "0 merged" printed beside a lane that
    shipped four things is a confident wrong answer; None plus a reason is an honest one."""
    _production_registry(tmp_path)
    report, by_agent = _lanes_of(tmp_path, _runner(rc=1, err="gh: not authenticated"))
    assert report.lanes.production_available is False
    for lane in report.lanes.lanes:
        assert lane.merged_in_window is None
        assert lane.production_basis.startswith("unmeasured:")
    assert "not authenticated" in by_agent["lead-dev"].production_basis


def test_the_five_wake_false_report_window_renders_ZERO_for_that_lane(tmp_path):
    """The spec's own replay. A seat reporting a plausible verdict every wake, alive on
    every liveness signal, with other lanes shipping around it - the production column is
    the only place that reads as anything other than healthy."""
    _production_registry(
        tmp_path,
        log="| E-0111 | something else | jr-dev-2 | jr-dev-2 | done | - |  |\n"
            "| E-0112 | another thing | jr-dev-2 | jr-dev-2 | done | - |  |\n",
    )
    _, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json(
        (1, "One (E-0111)", "aaaaaaa"), (2, "Two (E-0112)", "bbbbbbb"))))
    assert by_agent["lead-dev"].merged_in_window == 0
    assert by_agent["jr-dev-2"].merged_in_window == 2
    assert by_agent["lead-dev"].production_basis.startswith("merged PRs in ")


def test_production_reuses_the_ONE_merged_pr_read(tmp_path):
    """One read of the MERGED list, three consumers - DONE, WORK's sha evidence, and LANES. A
    second read of the same query would be a second answer that can disagree with the first.

    THE PREDICATE NAMES THE QUERY, NOT THE COMMAND, and that is the whole invariant. gh#300 added
    a read of OPEN pull requests, which is a different question about a disjoint set: a merged PR
    and an open one cannot be the same row, so those two answers cannot contradict each other and
    counting them together would forbid a call this rule was never about. Bumping the total to 2
    instead would have left a genuine second MERGED read free to slip in behind it."""
    _production_registry(tmp_path)
    inner = _runner(out=_pr_json((1, "x", "aaaaaaa")))
    calls = []

    def counting(argv):
        calls.append(argv)
        return inner(argv)

    fr.collect(inputs_for(tmp_path, tracker_host_ref="owner/repo"), now=NOW, git_runner=counting)
    pr_lists = [c for c in calls if c[:3] == ["gh", "pr", "list"]]
    assert sum(1 for c in pr_lists if "merged" in c) == 1, pr_lists
    assert sum(1 for c in pr_lists if "open" in c) == 1, "gh#300 reads open PRs exactly once too"


@pytest.mark.parametrize("agent", ["auditor", "steward", "dev-manager"])
def test_a_lane_less_seat_with_no_PRs_is_UNMEASURED_not_ZERO(tmp_path, agent):
    """A ZERO IS A VERDICT. For a builder, 0 merged PRs means it shipped nothing - the exact
    fact this signal exists to surface. For a seat that does not ship pull requests at all it
    asserts unproductivity that was never measured, which is the mirror image of the
    unmeasured-seat-rendered-healthy failure this epic is about and just as confidently wrong."""
    _production_registry(tmp_path)
    _, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((1, "Something (E-9999)", "aaaaaaa"))))
    lane = by_agent[agent]
    assert lane.merged_in_window is None
    assert lane.production_basis.startswith("unmeasured:")
    assert "does not ship pull requests" in lane.production_basis
    assert fr._NON_PR_OUTPUT[agent] in lane.production_basis


def test_a_BUILDER_with_no_PRs_still_reports_ZERO(tmp_path):
    """The other side of the same rule. For a lane, zero IS the measurement, and softening it
    to None would delete the only signal that answers "is it producing"."""
    _production_registry(tmp_path)
    _, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((1, "Something (E-9999)", "aaaaaaa"))))
    assert by_agent["lead-dev"].merged_in_window == 0


def test_a_lane_less_seat_that_DID_land_a_pr_still_shows_the_count(tmp_path):
    """An observed fact is not hidden behind "unmeasured". The exemption is about a zero that
    means nothing, not about refusing to look at the seat."""
    _production_registry(
        tmp_path,
        log="| E-0500 | a steward repair | lead | steward | done | - |  |\n",
    )
    _, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((5, "Repair it (E-0500)", "eeeeeee"))))
    assert by_agent["steward"].merged_in_window == 1
    assert by_agent["steward"].production_basis.startswith("merged PRs in ")


# --------------------------------------------------------------------------- #
# NOTE BACKLOG (E-0224 change 4) - the count that was collected and never alarmed
# --------------------------------------------------------------------------- #
def test_the_note_backlog_has_a_threshold_and_it_is_not_one():
    """The other four thresholds count things that should never happen at all, so 1 is the
    signal. Live notes are the fleet's normal traffic: at 1 this metric fires on every board
    forever, which is a number nobody reads rather than an alarm."""
    assert fr.HEALTH_THRESHOLDS["note_backlog"] > 1


def test_the_note_backlog_is_PUBLISHED_into_the_snapshot_the_thresholds_read(tmp_path):
    """It was collected and rendered with no threshold beside it, which is the difference
    between a number and a signal. A threshold is only reachable if its metric is published
    into the same snapshot every other threshold is compared against."""
    _production_registry(tmp_path)
    report = fr.collect(inputs_for(tmp_path), now=NOW)
    health = report.snapshot()["health"]
    assert set(fr.HEALTH_THRESHOLDS) <= set(health), "a threshold with no metric behind it"
    assert health["note_backlog"] == report.health.notes_live


def test_a_note_backlog_crossing_its_threshold_is_a_signal_in_the_diff():
    """The count already existed; what did not exist was anything that fires on it. Asserted
    through the real crossing machinery rather than against the constant, so a metric wired
    to the wrong key cannot pass."""
    over = fr.HEALTH_THRESHOLDS["note_backlog"]
    prior = snapshot_like(health={"note_backlog": over - 1}, generated_at="2026-08-31T12:00:00+00:00")
    current = snapshot_like(health={"note_backlog": over})
    crossings = {c.metric: c.direction for c in fr.diff_since(prior, current, prior_path="p.md").health_crossings}
    assert crossings.get("note_backlog") == "crossed"

    drained = snapshot_like(health={"note_backlog": over - 1})
    back = fr.diff_since(current, drained, prior_path="p.md").health_crossings
    assert {c.metric: c.direction for c in back}.get("note_backlog") == "cleared"


def test_ordinary_note_traffic_does_NOT_trip_the_backlog_alarm():
    """The reason this threshold is not 1 like the other four. Six seats routing a note
    apiece per wake is a healthy channel, and an alarm that fires on healthy traffic is a
    number nobody reads."""
    prior = snapshot_like(health={"note_backlog": 0}, generated_at="2026-08-31T12:00:00+00:00")
    current = snapshot_like(health={"note_backlog": 6})
    crossings = [c.metric for c in fr.diff_since(prior, current, prior_path="p.md").health_crossings]
    assert "note_backlog" not in crossings


# --------------------------------------------------------------------------- #
# E-0224 second pass - defects the adversarial review found in the first one
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("agent", ["auditor", "steward", "dev-manager"])
def test_a_DRY_lane_less_seat_is_NOT_told_to_be_given_work(tmp_path, agent):
    """THE FIRST PASS SHIPPED A CONFIDENT FALSEHOOD HERE, on the first real board it met.

    "needs work, not diagnosis" is right for a builder: its lane is filled by lead-dev, so an
    empty one really is a call to publish. It is wrong for a lane-less seat - `steward` is a
    self-feeding sweeper whose work is generated by time passing, so telling a human to feed it
    work names an action nobody can take. Worse, the seat renders DRY precisely when it has
    DRAINED its inbox, which is the hygiene the fleet asks of it.

    The remedy clause was untested in the first pass, which is exactly how the false half
    shipped: the test asserted the state and the basis and never read the sentence."""
    _states_registry(tmp_path)
    why = _lane_state(tmp_path, agent).why
    assert "needs work" not in why, "a remedy this seat cannot act on"
    assert fr.INBOX_BASIS in why, "the true half of the sentence must survive"
    assert "drained inbox" in why
    assert fr._NON_PR_OUTPUT[agent] in why, "it must say what this seat DOES produce"


def test_a_DRY_BUILDER_is_still_told_to_be_given_work(tmp_path):
    """The other side. A builder's empty lane IS a call to publish, and softening that would
    delete the signal the DRY state was added for."""
    _states_registry(tmp_path)
    why = _lane_state(tmp_path, "jr-dev-1").why
    assert "needs work, not diagnosis" in why
    assert fr.LANE_BASIS in why


def test_an_E_id_two_seats_have_held_is_DROPPED_not_resolved_by_recency(tmp_path):
    """E- ids come from priority numbers and this fleet REUSES them for unrelated epics; the
    live log holds seven duplicated ids, five of which changed hands. Last-write-wins credited
    a PR naming the OLDER epic to the NEWER epic's holder - silently, and retroactively the
    moment an id was reused. Unattributed is the honest answer."""
    _production_registry(
        tmp_path,
        log="| E-0204 | wiki-pillar-backfill | jr-dev-2 | jr-dev-2 | done | - |  |\n"
            "| E-0204 | retire-the-autoloop-value | lead | lead-dev | done | - |  |\n",
    )
    report, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((1, "Backfill it (E-0204)", "aaaaaaa"))))
    assert by_agent["lead-dev"].merged_in_window == 0
    assert by_agent["jr-dev-2"].merged_in_window == 0
    assert report.lanes.production_unattributed == 1


def test_the_same_seat_holding_an_id_twice_is_NOT_ambiguous(tmp_path):
    """Republishing to the same seat is the case recency was written for, and it still
    resolves - the drop is about two SEATS, not two rows."""
    _production_registry(
        tmp_path,
        log="| E-0204 | first go | jr-dev-2 | jr-dev-2 | done | - |  |\n"
            "| E-0204 | second go | jr-dev-2 | jr-dev-2 | done | - |  |\n",
    )
    _, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((1, "Do it (E-0204)", "aaaaaaa"))))
    assert by_agent["jr-dev-2"].merged_in_window == 1


def test_a_MENTION_of_an_issue_does_not_outrank_the_row_that_is_ABOUT_it(tmp_path):
    """Work-item cells carry seam prose that routinely names other issues ("measured during
    the gh#134 review"). Read as ownership those mentions make two seats claim one number, the
    uniqueness rule discards it, and the seat that actually owns the issue loses its credit -
    measured on the live log, five keys were being thrown away by prose alone. A row whose
    SUBJECT is the issue is a structural claim and outranks any number of mentions."""
    _production_registry(
        tmp_path,
        log="| E-0300 | BUG gh#134 - the real fix | jr-dev-1 | jr-dev-1 | done | - |  |\n"
            "| E-0301 | something else, measured during the gh#134 review | lead | lead-dev | done | - |  |\n",
    )
    report, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((1, "Fix it (gh#134)", "aaaaaaa"))))
    assert by_agent["jr-dev-1"].merged_in_window == 1
    assert by_agent["lead-dev"].merged_in_window == 0
    assert report.lanes.production_unattributed == 0


def test_two_rows_ABOUT_one_issue_are_still_ambiguous(tmp_path):
    """The subject layer overrules mentions; it does not overrule itself. Two structural
    claims on one number identify neither seat."""
    _production_registry(
        tmp_path,
        log="| E-0300 | BUG gh#134 - my half | jr-dev-1 | jr-dev-1 | done | - |  |\n"
            "| E-0301 | gh#134 - my half too | jr-dev-2 | jr-dev-2 | done | - |  |\n",
    )
    report, _ = _lanes_of(tmp_path, _runner(out=_pr_json((1, "Fix it (gh#134)", "aaaaaaa"))))
    assert report.lanes.production_unattributed == 1


def test_an_ABSENT_cull_log_is_normal_and_still_measures(tmp_path):
    """A fresh install has no log. That narrows the map; it does not break the signal."""
    _production_registry(
        tmp_path,
        queue="| E-0001 | live | jr-dev-1 | jr-dev-1 | building | - |  |\n",
    )
    report, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((1, "Do it (E-0001)", "aaaaaaa"))))
    assert report.lanes.production_available is True
    assert by_agent["jr-dev-1"].merged_in_window == 1


def test_an_UNREADABLE_cull_log_refuses_to_measure_rather_than_printing_zeros(tmp_path):
    """ABSENCE IS NOT DENIAL, and on a mature fleet the log IS the map - shipped rows are
    culled within a wake or two. Swallowing the error collapsed every lane to zero while
    `production_available` still said True and every basis still claimed an attribution had
    been made. "0 merged" beside a lane that shipped four things is the precise failure this
    signal was written against."""
    _production_registry(tmp_path, log="| E-0001 | x | jr-dev-1 | jr-dev-1 | done | - |  |\n")
    log = tmp_path / "projects" / "alpha" / "status" / "registry-log.md"
    real = Path.read_text

    def denied(self, *a, **kw):
        if Path(self) == log:
            raise PermissionError(13, "Permission denied", str(log))
        return real(self, *a, **kw)

    import unittest.mock as _mock
    with _mock.patch.object(Path, "read_text", denied):
        report, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((1, "Do it (E-0001)", "aaaaaaa"))))
    assert report.lanes.production_available is False
    for lane in report.lanes.lanes:
        assert lane.merged_in_window is None
        assert lane.production_basis.startswith("unmeasured:")
    assert "registry-log.md" in by_agent["lead-dev"].production_basis


def test_one_bad_byte_in_the_cull_log_does_not_take_the_report_down(tmp_path):
    """A 900KB append-only archive is exactly where a stray byte turns up, and a mangled
    character in a culled work_item cannot change an id or a claimed_by cell."""
    _production_registry(tmp_path)
    log = tmp_path / "projects" / "alpha" / "status" / "registry-log.md"
    log.write_bytes(b"| E-0001 | caf\xe9 broken | jr-dev-1 | jr-dev-1 | done | - |  |\n")
    report, by_agent = _lanes_of(tmp_path, _runner(out=_pr_json((1, "Do it (E-0001)", "aaaaaaa"))))
    assert report.lanes.production_available is True
    assert by_agent["jr-dev-1"].merged_in_window == 1


# --------------------------------------------------------------------------- #
# The threshold reports a LEVEL, not only an edge
# --------------------------------------------------------------------------- #
def test_a_metric_that_STAYS_over_keeps_reporting(tmp_path):
    """THE DEFECT THAT MADE THE FIRST PASS'S THRESHOLD VACUOUS. A pure edge detector emits
    `crossed` only on the report where the value first goes over. The live note backlog has
    been over 40 continuously and has never been under it, so an edge detector fires ZERO
    times on it - a threshold that cannot produce a signal, which is the "number rather than a
    signal" this epic set out to fix."""
    over = fr.HEALTH_THRESHOLDS["note_backlog"]
    prior = snapshot_like(health={"note_backlog": over + 100},
                          generated_at="2026-08-31T12:00:00+00:00")
    current = snapshot_like(health={"note_backlog": over + 150})
    crossings = {c.metric: c.direction
                 for c in fr.diff_since(prior, current, prior_path="p.md").health_crossings}
    assert crossings.get("note_backlog") == "over"


def test_a_metric_ABSENT_from_the_prior_snapshot_still_reports_on_the_first_run(tmp_path):
    """A metric added by a release has no key in the previous release's archived snapshot.
    Treating that absence as "unknown" made every new threshold silent on the one report where
    it is most worth seeing - and the live board would have been over on day one."""
    over = fr.HEALTH_THRESHOLDS["note_backlog"]
    prior = snapshot_like(health={"inert_promotions": 0},
                          generated_at="2026-08-31T12:00:00+00:00")
    current = snapshot_like(health={"note_backlog": over + 158})
    crossings = {c.metric: c.direction
                 for c in fr.diff_since(prior, current, prior_path="p.md").health_crossings}
    assert crossings.get("note_backlog") == "crossed"


def test_a_metric_UNDER_its_threshold_reports_nothing(tmp_path):
    """The level report must not become a permanent line for a healthy metric."""
    prior = snapshot_like(health={"note_backlog": 5}, generated_at="2026-08-31T12:00:00+00:00")
    current = snapshot_like(health={"note_backlog": 6})
    assert not fr.diff_since(prior, current, prior_path="p.md").health_crossings


# --------------------------------------------------------------------------- #
# THE HEALTH DICT IS ONE DICT (gh#276) - the archived side and the diffed side
# --------------------------------------------------------------------------- #
def _divergent_skip_board(tmp_path):
    """A board where the two old spellings of `survey_skips` gave DIFFERENT numbers.

    Registry rows the parser drops, and not one broken spec: epic skips 0, registry skips
    many. That is ordinary traffic on a live board - an epic title or a note carrying an
    unescaped `|` is all it takes - and it is the exact shape under which the epic-only
    spelling and the every-scan spelling disagree.
    """
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=(
            agent_row("lead-dev", "building", work="wide|cell", heartbeat=stamp(NOW, minutes=2))
            + "| jr-dev-1 | building | epic | - |\n"
            + agent_row("nobody", "building", heartbeat=stamp(NOW, minutes=2))
            + agent_row("jr-dev-2", "building", work="ok", heartbeat=stamp(NOW, minutes=2))
        ),
        blockers="| B-1 | jr-dev-1 | human | - | open | needs a call |\n",
        questions=qblock("Q-1", state="open", question="bad id", raised_at=stamp(NOW, hours=1)),
    )


def test_the_archived_health_dict_and_the_diffed_one_are_the_SAME_dict(tmp_path):
    """`diff_since` reads `snapshot()["health"]` from the archive as `was` and `collect`'s
    `core["health"]` as `now`. They were built by two hands over two sets of locals and had
    already drifted: `survey_skips` was every scan on one side and the epic survey only on the
    other, so the metric was being compared against a different metric.

    Pins the PAIR, by VALUE, on a board where the two old spellings disagree. A key-set
    assertion passes on a tree where one site still drifts, which is how this survived.
    """
    _divergent_skip_board(tmp_path)
    report = fr.collect(inputs_for(tmp_path), now=NOW)

    scans = {scan.label: scan for scan in report.counted_skipped.scans}
    assert scans["epics"].skipped_count == 0
    assert scans["registry"].skipped_count > 0, "fixture no longer produces the divergent shape"

    archived = report.snapshot()["health"]   # the WAS side, one report from now
    diffed = report._core_health             # the NOW side, this report

    assert set(archived) == set(fr.HEALTH_THRESHOLDS), "a threshold with no metric behind it"
    assert set(diffed) == set(fr.HEALTH_THRESHOLDS)
    for metric in sorted(fr.HEALTH_THRESHOLDS):
        assert archived[metric] == diffed[metric], (
            f"{metric} is computed differently on the two sides of the diff: "
            f"archived {archived[metric]!r} vs diffed {diffed[metric]!r}"
        )


def test_survey_skips_counts_EVERY_scan_on_both_sides_of_the_diff(tmp_path):
    """The reading, pinned to the number a human sees in COUNTED / SKIPPED.

    A dropped registry row is as much "something the report could not see" as a dropped spec -
    a dropped AGENT row takes a lane out of LANES and a dropped BLOCKER row takes a blocker
    out of BLOCKED ON YOU (not the same row: dropping an agent leaves its blockers intact,
    measured) - and the renderer contract in SKILL.md already reads both scans. A threshold firing
    on a narrower population than the warning two sections below it is the same defect one
    layer up.
    """
    _divergent_skip_board(tmp_path)
    report = fr.collect(inputs_for(tmp_path), now=NOW)

    every_scan = report.counted_skipped.total_skipped
    assert every_scan > 0
    assert report.snapshot()["health"]["survey_skips"] == every_scan
    assert report._core_health["survey_skips"] == every_scan


def test_an_unmoved_board_reports_NO_health_crossing_across_the_archive_boundary(tmp_path):
    """End to end, through the real diff, which is where the drift actually bit.

    Archive a report, collect the same board again, hand run 1's snapshot back as the prior.
    Nothing on the board moved, so no health metric may report a `was` different from its
    `now`. Under the old spellings this board archived `survey_skips: 5` and re-read it as 0,
    and announced "cleared" on every run forever - a permanent all-clear on a defect that
    never went away, printed beside an archived JSON saying the opposite.
    """
    _divergent_skip_board(tmp_path)
    first = fr.collect(inputs_for(tmp_path), now=NOW)
    second = fr.collect(inputs_for(tmp_path), now=NOW, prior=first.snapshot())

    moved = [
        (c.metric, c.was, c.now, c.direction)
        for c in second.changed_since.health_crossings
        if c.was != c.now
    ]
    assert moved == [], f"health metrics moved on a board that did not: {moved}"


# --------------------------------------------------------------------------- #
# gh#202 - the minted `[<ISO>] ` stamp is machine metadata, and it was being named
# --------------------------------------------------------------------------- #
def test_directive_name_strips_the_minted_stamp_before_naming():
    """The live-board shape, which no fixture in this file used to have.

    Every Directive reaches `directive_name` as `_cmd_directive` minted it - `[<ISO>] <text>` -
    and the clause split breaks on `[.;:]`, so the colon inside `T05:12Z` fired first. The tests
    were green because their fixtures were hand-written WITHOUT the stamp: the suite never looked
    at the shape the product actually produces.
    """
    minted = "[2026-08-31T05:12Z] NO CLAUDE CODE SESSION URLS ON THE HOST. Standing."

    assert fr.directive_name(minted) == "NO CLAUDE CODE SESSION URLS ON THE HOST"


def test_directive_names_carry_content_not_timestamps():
    """The property, stated the way the defect actually behaves.

    gh#202 says "every Directive renders as the same name". Measured over the ten live ones they
    were all DISTINCT - ten different truncated timestamps - which is why nobody saw a collision
    to report. Asserting uniqueness would therefore pass on the broken code; what has to be
    asserted is that a name carries the human's words.
    """
    minted = [
        "[2026-08-31T05:12Z] NO CLAUDE CODE SESSION URLS ON THE HOST. Standing.",
        "[2026-08-31T20:12Z] A SPEC WITHOUT A ## Files SECTION MAY NOT BE PROMOTED. Binding.",
        "[2026-09-05T06:54Z] WIKI PAGES ARE TOO VERBOSE - CUT THEM TO THE MINIMUM.",
    ]

    names = [fr.directive_name(d) for d in minted]

    assert names == [
        "NO CLAUDE CODE SESSION URLS ON THE HOST",
        "A SPEC WITHOUT A ## Files SECTION MAY NOT BE PROMOTED",
        "WIKI PAGES ARE TOO VERBOSE",
    ]
    for name in names:
        assert not name.startswith("["), f"named after its own stamp: {name!r}"


def test_directive_name_keeps_a_human_bracket_that_is_not_a_stamp():
    """Stripping is decided by CONTENT, not by shape - the same rule gh#127 established.

    A shape-anchored regex would eat any leading bracket and silently rename a human bullet that
    opens with one. Delegating to `directive_match_text` means the bracket comes off only when
    the registry's own parser reads it as a timestamp.
    """
    assert fr.directive_name("[URGENT] hold merges until the gate is green") == (
        "[URGENT] hold merges until the gate is green"
    )


def test_directive_name_uses_the_registry_helper_rather_than_its_own_stripper(monkeypatch):
    """Pins WHERE the stamp is stripped, not just that it is.

    gh#127 and gh#202 are the same stamp in two surfaces, and the fix for the first was to put
    one named helper in charge of the boundary. A second private stripper here would pass every
    other test in this file and reopen the drift the moment the minter's format widens.
    """
    seen = []

    def spy(text):
        seen.append(text)
        return text

    monkeypatch.setattr(fr._registry, "directive_match_text", spy)
    fr.directive_name("[2026-08-31T05:12Z] anything at all")

    assert seen == ["[2026-08-31T05:12Z] anything at all"], (
        "directive_name did not route through registry.directive_match_text"
    )


# --------------------------------------------------------------------------- #
# gh#298 — the stall alert counted work already in flight
#
# `selectable()` reads the spec corpus and never the Queue, which is correct and is not the
# defect. The defect was building a STALL DETECTOR on it: an epic a lane is building right now
# is approved, dependency-ready and in scope, so it counts - and the alert that exists to catch
# a starved board therefore fires one or more epics late.
#
# Measured on the live board 2026-09-22, which is worse than the issue states: `in_progress` was
# EMPTY while two lanes were building, because it read `lifecycle_state == "running"` and no spec
# frontmatter in this corpus carries that value. One absent column degraded three signals - the
# stall count, the in-flight column itself, and the gh#121 seam check, whose holder list is built
# from `work.in_progress` and so could only ever name bugs.
#
# In flight is now derived from the CLAIM, the way the bug column already did it.
# --------------------------------------------------------------------------- #

_APPROVED = "current_state: approved-for-autonomous\nqueue_class: fleet-buildable\npriority: {p}\n"


def _work_corpus(tmp_path: Path, *, agents: str = "", queue: str = "") -> None:
    """Two approved, dependency-free epics and a registry describing who holds what."""
    make_spec(tmp_path, "alpha", "held-epic", frontmatter=_APPROVED.format(p=100))
    make_spec(tmp_path, "alpha", "free-epic", frontmatter=_APPROVED.format(p=200))
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md", agents=agents, queue=queue
    )


def _q(row_id: str, *, state: str, claimed_by: str, lane: str = "jr-dev-1") -> str:
    return f"| {row_id} | some work | {lane} | {claimed_by} | {state} | - | - |\n"


def _qt(row_id: str, title: str, *, state: str, claimed_by: str, lane: str = "jr-dev-1") -> str:
    """A Queue row carrying its real `work_item` title, which is where the full slug lives."""
    return f"| {row_id} | {title} | {lane} | {claimed_by} | {state} | - | - |\n"


def _colliders(tmp_path: Path, *, agents: str = "", queue: str = "") -> None:
    """Two approved epics whose FIRST FOUR hyphen-tokens are identical.

    `acme-3-5-chat-alpha` and `acme-3-5-chat-beta` both derive the epic name `acme-3-5-chat`,
    because numeric tokens eat the four-token budget. This is not a contrived shape - it is the
    one `_short_name`'s own docstring measured on the live data root, and the `<slug>` beside
    `<slug>-plan` shape this fleet generates routinely collides the same way.
    """
    make_spec(tmp_path, "alpha", "acme-3-5-chat-alpha", frontmatter=_APPROVED.format(p=100))
    make_spec(tmp_path, "alpha", "acme-3-5-chat-beta", frontmatter=_APPROVED.format(p=200))
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md", agents=agents, queue=queue
    )


def _work_of(tmp_path):
    return fr.collect(inputs_for(tmp_path), now=NOW, issues=[]).work


def test_an_epic_a_lane_is_building_is_in_flight_and_not_claimable_now(tmp_path):
    _work_corpus(tmp_path, queue=_q("E-0001-held-epic", state="building", claimed_by="jr-dev-1"))
    work = _work_of(tmp_path)
    assert [r.slug for r in work.in_progress] == ["held-epic"]
    assert [r.slug for r in work.claimable_now] == ["free-epic"]
    # BOTH numbers are kept on purpose: "how much approved work exists" and "how much could a
    # lane start this second" are different questions, and one number standing for both is how
    # a starved board reads healthy.
    assert sorted(r.slug for r in work.approved_claimable) == ["free-epic", "held-epic"]


def test_an_epic_claimed_only_in_an_agents_row_is_also_in_flight(tmp_path):
    """The Agents `work` cell is a claim too - a lane can be building before its Queue row moves."""
    _work_corpus(tmp_path, agents=agent_row("jr-dev-2", "building", work="E-0001-held-epic"))
    work = _work_of(tmp_path)
    assert [r.slug for r in work.in_progress] == ["held-epic"]
    assert [r.slug for r in work.claimable_now] == ["free-epic"]


def test_a_terminal_queue_row_is_provenance_not_a_claim(tmp_path):
    """`claimed_by` on a done row records who shipped it. Counting it as a claim would hide a
    finished epic's slug from the claimable set forever."""
    _work_corpus(tmp_path, queue=_q("E-0001-held-epic", state="done", claimed_by="jr-dev-1"))
    work = _work_of(tmp_path)
    assert [r.slug for r in work.in_progress] == []
    assert sorted(r.slug for r in work.claimable_now) == ["free-epic", "held-epic"]


def test_parked_is_counted_separately_and_folded_into_neither_number(tmp_path):
    """A parked epic is arguably MORE alarming than an absent one, so it gets its own count
    rather than disappearing into a subtraction - which is what made the state list look like a
    decision nobody could take.

    NEITHER number, and this assertion is the one an earlier draft got backwards: it asserted
    the parked epic into `in_progress`, contradicting the rule written into report/SKILL.md by
    the same commit. A parked row IS claimed, so it leaves `claimable_now`; nobody is building
    it, so calling it in flight overstates how much of the board is moving. It is reported by
    `parked_count` and by nothing else."""
    _work_corpus(tmp_path, queue=_q("E-0001-held-epic", state="parked", claimed_by="jr-dev-1"))
    work = _work_of(tmp_path)
    assert work.parked_count == 1
    assert [r.slug for r in work.in_progress] == [], "parked is not in flight - nobody is building it"
    assert [r.slug for r in work.claimable_now] == ["free-epic"], "parked is claimed, so not startable"


def test_no_work_anywhere_reads_claimable_now_not_approved_claimable():
    """The predicate asks whether any lane can start anything. Approved work that a lane is
    already building cannot be started by a second one, so counting it delayed the alert by
    however long the in-flight epics took - exactly the window it exists to catch.

    Two approved epics exist and both are held: the OLD reading says work is available, the
    new one says no lane can start anything, and only the second is true of the board."""
    report = _report(claimable=2, claimable_now=0)
    nwa = report.no_work_anywhere
    assert (nwa() if callable(nwa) else nwa) is True

    still_startable = _report(claimable=2, claimable_now=1)
    nwa2 = still_startable.no_work_anywhere
    assert (nwa2() if callable(nwa2) else nwa2) is False, "one startable epic is not an empty board"


def test_one_claim_does_not_suppress_a_sibling_sharing_its_four_token_name(tmp_path):
    """An epic id carries the first four hyphen-tokens of its slug, and that name is NOT unique.

    Nine names collide across one live project's corpus. Keyed on the name alone, one claim
    suppressed every approved spec sharing it: the untouched sibling dropped out of
    `claimable_now` and printed as in flight under the other lane's name and eta. With nothing
    else claimable the stall alert then fired on a board that had startable work - the gh#298
    failure inverted, and worse, because it halts the seat that feeds every other lane.
    """
    _colliders(
        tmp_path,
        queue=_qt(
            "E-0001-acme-3-5-chat",
            "acme-3-5-chat-alpha (p100, alpha)",
            state="building",
            claimed_by="jr-dev-1",
        ),
    )
    work = _work_of(tmp_path)
    assert [r.slug for r in work.in_progress] == ["acme-3-5-chat-alpha"]
    assert [r.slug for r in work.claimable_now] == ["acme-3-5-chat-beta"], (
        "the sibling nobody claimed is startable"
    )
    assert [r.slug for r in work.claim_ambiguous] == [], "the title resolved it, so nothing is unknown"


def test_a_claim_that_cannot_be_resolved_names_the_candidates_instead_of_guessing(tmp_path):
    """An `## Agents` row carries only the id, so it cannot disambiguate a shared name.

    Holding neither would invite a lane into occupied work; holding both would hide a startable
    epic. The third answer is the honest one: keep them out of `claimable_now` AND name them, the
    same discipline `seam_unknown_count` already follows one section down.
    """
    _colliders(tmp_path, agents=agent_row("jr-dev-2", "building", work="E-0001-acme-3-5-chat"))
    work = _work_of(tmp_path)
    assert [r.slug for r in work.in_progress] == [], "we do not know which one is held"
    assert [r.slug for r in work.claimable_now] == [], "nor which one is free"
    assert sorted(r.slug for r in work.claim_ambiguous) == [
        "acme-3-5-chat-alpha",
        "acme-3-5-chat-beta",
    ]


def test_an_unresolvable_claim_keeps_the_stall_alert_silent(tmp_path):
    """`claimable_now == 0` is not evidence of an empty board when a claim could not be resolved.

    A stall that goes unreported wastes a wake. A stall reported falsely stops the foreman, and
    with it three other lanes. So the alert waits for the ambiguity to clear rather than reading
    a number it knows is understated.
    """
    _colliders(tmp_path, agents=agent_row("jr-dev-2", "building", work="E-0001-acme-3-5-chat"))
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert report.work.claimable_now.total == 0
    assert report.work.claim_ambiguous.total == 2
    nwa = report.no_work_anywhere
    assert (nwa() if callable(nwa) else nwa) is False, (
        "an epic a claim MIGHT hold is not proof the product has no work"
    )


def test_a_name_no_approved_epic_carries_holds_nothing(tmp_path):
    """A claim naming an epic outside the claimable corpus - a bug row, a shipped epic, another
    project's - must not subtract from anything. It resolves to zero candidates, not to all."""
    _work_corpus(tmp_path, queue=_q("E-0009-nothing-like-this", state="building", claimed_by="jr-dev-1"))
    work = _work_of(tmp_path)
    assert sorted(r.slug for r in work.claimable_now) == ["free-epic", "held-epic"]
    assert [r.slug for r in work.claim_ambiguous] == []


def test_a_queue_state_beats_a_heartbeat_when_the_board_contradicts_itself(tmp_path):
    """The two claim routes carry different kinds of fact, and the live board disagreed with
    itself the day this was written: a lane's `## Agents` row read `building` against an epic its
    own `## Queue` row had `parked`.

    Read as two independent claims, that one epic landed in `in_progress` AND in `parked_count` -
    the double-count this change exists to remove, arriving by the other door. The Queue row is
    the state machine and owns the verdict; the Agents row is a heartbeat and fills only what no
    Queue row claims.
    """
    _work_corpus(
        tmp_path,
        agents=agent_row("jr-dev-1", "building", work="E-0001-held-epic"),
        queue=_q("E-0001-held-epic", state="parked", claimed_by="jr-dev-1"),
    )
    work = _work_of(tmp_path)
    assert work.parked_count == 1
    assert [r.slug for r in work.in_progress] == [], "the heartbeat does not override the state"
    assert [r.slug for r in work.claimable_now] == ["free-epic"]


def test_an_agents_claim_still_counts_when_no_queue_row_claims_that_epic(tmp_path):
    """Precedence is per-epic, not global. A lane can be building before its Queue row moves, and
    that claim must still hold its epic out of `claimable_now`."""
    _work_corpus(
        tmp_path,
        agents=agent_row("jr-dev-2", "building", work="E-0001-held-epic"),
        queue=_q("E-0002-free-epic", state="ready", claimed_by="-"),
    )
    work = _work_of(tmp_path)
    assert [r.slug for r in work.in_progress] == ["held-epic"]
    # The ready row publishes free-epic into jr-dev-1's lane, so it is waiting there, not
    # startable by any free lane - and it is still not held by the Agents claim.
    assert [r.slug for r in work.claimable_now] == []
    assert [(p.name, p.lane) for p in work.published_unclaimed] == [("free-epic", "jr-dev-1")]
    assert work.parked_count == 0


# --------------------------------------------------------------------------- #
# Worktree REGISTRATION drift — git's view vs the filesystem's (gh#286)
# --------------------------------------------------------------------------- #
def _wt_runner(registered_paths):
    """A `git worktree list --porcelain` runner returning exactly these worktree paths."""
    body = "".join(f"worktree {p}\nHEAD 0000000\n\n" for p in registered_paths)

    seen = []

    def run(argv):
        # The SAME injected runner serves other git callers, so dispatch on the subcommand rather
        # than asserting a shape: a runner that assumes it is only ever asked one question fails
        # on the other caller, which is a defect in the fixture and not in the code under test.
        seen.append(list(argv))
        if "worktree" in argv:
            return 0, body, ""
        return 0, "master", ""

    run.seen = seen
    return run


def _wt_fixture(tmp_path, dirs, *, agents=""):
    worktrees = tmp_path / "worktrees"
    for d in dirs:
        (worktrees / "alpha" / d).mkdir(parents=True)
    (tmp_path / "checkout").mkdir(exist_ok=True)
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md", agents=agents)
    return worktrees


def test_a_directory_git_never_registered_is_reported_as_unregistered(tmp_path):
    """The steward's sweep enumerates with `git worktree list`, so a directory git has no
    registration for is reported by neither it nor the charter's removal step — and the sweep
    keeps reporting clean however many accumulate. Measured on the live board the day this was
    built: git listed 2, the filesystem held 5."""
    worktrees = _wt_fixture(tmp_path, ["e0280-live", "e0999-orphan-shell"])
    runner = _wt_runner([str(tmp_path / "checkout"), str(worktrees / "alpha" / "e0280-live")])
    report = fr.collect(
        inputs_for(tmp_path, worktree_root=worktrees, repo_path=tmp_path / "checkout"),
        now=NOW, git_runner=runner,
    )
    w = report.health.worktrees
    assert w.directories == ("e0280-live", "e0999-orphan-shell")
    assert w.unregistered == ("e0999-orphan-shell",)
    assert w.missing == ()
    assert w.registration_note == ""


def test_a_registration_whose_directory_is_gone_is_the_OTHER_finding(tmp_path):
    """The two directions are not one count. An unregistered directory is a shell
    `git worktree remove` cannot take; a registration with no directory is git holding admin
    state for a tree that is gone, which `git worktree prune` clears. Reporting them together
    would tell a reader to run the wrong command half the time."""
    worktrees = _wt_fixture(tmp_path, ["e0280-live"])
    runner = _wt_runner([
        str(tmp_path / "checkout"),
        str(worktrees / "alpha" / "e0280-live"),
        str(worktrees / "alpha" / "e0777-deleted-by-hand"),
    ])
    w = fr.collect(
        inputs_for(tmp_path, worktree_root=worktrees, repo_path=tmp_path / "checkout"),
        now=NOW, git_runner=runner,
    ).health.worktrees
    assert w.unregistered == ()
    assert w.missing == ("e0777-deleted-by-hand",)


def test_the_canonical_checkout_and_another_projects_trees_are_not_this_ones(tmp_path):
    """`git worktree list` always names the main checkout first, and on a multi-project box it
    names every sibling project's trees too. Only entries whose parent IS this project's root
    count, or the main checkout reads as a registration this project is missing."""
    worktrees = _wt_fixture(tmp_path, ["e0280-live"])
    runner = _wt_runner([
        str(tmp_path / "checkout"),
        str(worktrees / "alpha" / "e0280-live"),
        str(worktrees / "beta" / "someone-elses"),
    ])
    w = fr.collect(
        inputs_for(tmp_path, worktree_root=worktrees, repo_path=tmp_path / "checkout"),
        now=NOW, git_runner=runner,
    ).health.worktrees
    assert w.missing == (), "the main checkout and beta's tree are not alpha's registrations"
    assert w.unregistered == ()


def test_a_failed_git_read_does_not_take_the_filesystem_half_down_with_it(tmp_path):
    """A report that says nothing because one of its two probes failed is how a sweep goes quiet
    at the moment it has something to say. The disk listing still lands; the note says why the
    other half is absent, so empty tuples are never read as 'nothing is drifting'."""
    worktrees = _wt_fixture(tmp_path, ["e0280-live", "e0999-orphan-shell"])

    def boom(argv):
        return 128, "", "fatal: not a git repository"

    w = fr.collect(
        inputs_for(tmp_path, worktree_root=worktrees, repo_path=tmp_path / "checkout"),
        now=NOW, git_runner=boom,
    ).health.worktrees
    assert w.available is True
    assert w.directories == ("e0280-live", "e0999-orphan-shell")
    assert "128" in w.registration_note and "not a git repository" in w.registration_note
    assert w.unregistered == () and w.missing == ()


def test_a_worktree_a_lane_is_building_is_not_reported_unclaimed(tmp_path):
    """gh#456, and it matters MORE beside the registration columns than it did before them: a row
    reading registered, present and unclaimed is what makes removing live work look safe. The
    directory carries `e0768`, the work cell carries `E-0768`, and no substring of either is in
    the other."""
    worktrees = _wt_fixture(
        tmp_path, ["e0768-account-limit-keystone"],
        agents=agent_row("jr-dev-1", "building", work="E-0768-the-fleet-cannot-survive",
                         heartbeat=stamp(NOW, minutes=2)),
    )
    runner = _wt_runner([
        str(tmp_path / "checkout"),
        str(worktrees / "alpha" / "e0768-account-limit-keystone"),
    ])
    w = fr.collect(
        inputs_for(tmp_path, worktree_root=worktrees, repo_path=tmp_path / "checkout"),
        now=NOW, git_runner=runner,
    ).health.worktrees
    assert w.unclaimed == (), "a lane is building here"
    assert w.unregistered == ()


def test_a_different_epics_claim_does_not_cover_this_worktree(tmp_path):
    """The number has to MATCH, not merely be present on both sides - otherwise every worktree
    reads as claimed the moment any lane holds any epic, which is the same blindness pointed the
    other way."""
    worktrees = _wt_fixture(
        tmp_path, ["e0768-account-limit-keystone"],
        agents=agent_row("jr-dev-1", "building", work="E-0771-the-pillar-residue-was",
                         heartbeat=stamp(NOW, minutes=2)),
    )
    runner = _wt_runner([str(tmp_path / "checkout")])
    w = fr.collect(
        inputs_for(tmp_path, worktree_root=worktrees, repo_path=tmp_path / "checkout"),
        now=NOW, git_runner=runner,
    ).health.worktrees
    assert w.unclaimed == ("e0768-account-limit-keystone",)


def test_the_git_command_is_pinned_because_the_parser_depends_on_it(tmp_path):
    """The porcelain format is the contract this parser reads: plain `git worktree list` prints
    one padded line per worktree with the sha and branch appended, and the `worktree ` prefix the
    parser keys on does not exist there at all. The fixture dispatches on the subcommand and
    ignores the rest of the command, so without this the flag could be dropped and every test
    still passes while the real runner returns something the parser silently reads as zero
    registrations."""
    worktrees = _wt_fixture(tmp_path, ["e0280-live"])
    runner = _wt_runner([str(tmp_path / "checkout"), str(worktrees / "alpha" / "e0280-live")])
    fr.collect(
        inputs_for(tmp_path, worktree_root=worktrees, repo_path=tmp_path / "checkout"),
        now=NOW, git_runner=runner,
    )
    calls = [c for c in runner.seen if "worktree" in c]
    assert len(calls) == 1, calls
    assert calls[0] == ["git", "-C", str(tmp_path / "checkout"), "worktree", "list", "--porcelain"]


def test_a_root_spelled_differently_than_git_prints_it_still_matches(tmp_path):
    """Git records and prints the fully RESOLVED real path; the config does not have to.
    `_require_dir` expands `~` and nothing else, so a root carrying a `..` segment reaches the
    comparison in a spelling git never emits. Compared textually nothing matched, so every live
    worktree printed as unregistered while `registration_note` stayed empty - the report claiming
    it read git successfully while saying the opposite of the truth."""
    worktrees = _wt_fixture(tmp_path, ["e0280-live"])
    # The same directory, spelled the way a config might and git never would.
    indirect = worktrees / "alpha" / ".." / ".." / "worktrees"
    runner = _wt_runner([str(tmp_path / "checkout"), str(worktrees / "alpha" / "e0280-live")])
    w = fr.collect(
        inputs_for(tmp_path, worktree_root=indirect, repo_path=tmp_path / "checkout"),
        now=NOW, git_runner=runner,
    ).health.worktrees
    assert w.registration_note == ""
    assert w.unregistered == (), "the worktree IS registered; only the spelling differed"
    assert w.missing == ()


def test_health_is_not_empty_while_it_carries_registration_findings(tmp_path):
    """`is_empty` decides whether a renderer prints 'checked, nothing there'. It already read
    `unclaimed`; the registration findings render in the same section and were not added to it,
    so a board whose only finding was four unregistered shells rendered as empty - the exact
    silent zero this property's own docstring was written against. A FAILED probe counts too: a
    section that could not look is not a section with nothing to report."""
    only_unregistered = fr.HealthSection(
        worktrees=fr.WorktreeDrift(available=True, directories=("a",), unregistered=("a",))
    )
    assert only_unregistered.is_empty is False

    only_missing = fr.HealthSection(
        worktrees=fr.WorktreeDrift(available=True, missing=("e0777-gone",))
    )
    assert only_missing.is_empty is False

    probe_failed = fr.HealthSection(
        worktrees=fr.WorktreeDrift(available=True, registration_note="git worktree list exited 128")
    )
    assert probe_failed.is_empty is False, "could not look is a finding, not an absence"

    assert fr.HealthSection().is_empty is True, "a genuinely empty section still reports empty"


# --------------------------------------------------------------------------- #
# OPEN pull requests — the "finished, green, never landed" case (gh#300)
# --------------------------------------------------------------------------- #
def _pr_runner(payload, *, rc=0, err=""):
    """A `gh pr list` runner. Serves the OPEN query from `payload`; every other git/gh call,
    including the merged-PR and branch readers, gets a benign empty answer."""
    seen = []

    def run(argv):
        seen.append(list(argv))
        if "pr" in argv and "list" in argv:
            if "open" in argv:
                return rc, json.dumps(payload), err
            return 0, "[]", ""
        return 0, "", ""

    run.seen = seen
    return run


def _health_of(tmp_path, runner, *, agents="", queue=""):
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md", agents=agents, queue=queue
    )
    return fr.collect(
        inputs_for(tmp_path, tracker_host_ref="owner/repo"), now=NOW, git_runner=runner
    ).health


def test_an_open_pr_is_reported_with_the_parts_that_make_a_stall_visible(tmp_path):
    """Nothing in the sweep read open PRs, so finished-and-never-landed had no reader at all.
    Every existing detector was individually correct and all of them were silent, because the
    signal is the CONJUNCTION: green checks, mergeable, and untouched for hours, next to a lane
    row that still reads building. This carries the parts so a reader can see that conjunction."""
    runner = _pr_runner([{
        "number": 295, "title": "ground an escalation", "headRefName": "e0228-ground-an-escalation",
        "createdAt": "2026-09-05T14:17:00Z", "updatedAt": "2026-09-05T14:17:00Z",
        "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}] * 14,
    }])
    h = _health_of(tmp_path, runner)
    assert len(h.open_prs) == 1
    pr = h.open_prs[0]
    assert pr.number == "295"
    assert pr.branch == "e0228-ground-an-escalation"
    assert pr.mergeable == "MERGEABLE" and pr.merge_state == "CLEAN"
    assert pr.checks == "14/14 SUCCESS"
    assert pr.created_at == pr.updated_at, "untouched since it opened is the whole tell"
    assert h.open_prs_note == ""


def test_the_pr_names_the_lane_whose_row_owns_its_branch(tmp_path):
    """A branch is spelled `e0228-...` and a work cell `E-0228-...`, and neither contains the
    other - the gh#456 mismatch. Matching on the epic NUMBER is what lets the report say WHOSE
    row is sitting behind an unmerged PR, which is half of the conjunction."""
    runner = _pr_runner([{
        "number": 295, "title": "t", "headRefName": "e0228-ground-an-escalation",
        "createdAt": "2026-09-05T14:17:00Z", "updatedAt": "2026-09-05T14:17:00Z",
        "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
    }])
    h = _health_of(
        tmp_path, runner,
        agents=agent_row("jr-dev-1", "building", work="E-0228-ground-an-escalation",
                         heartbeat=stamp(NOW, minutes=57)),
    )
    assert h.open_prs[0].lane == "jr-dev-1"


def test_a_pr_no_row_claims_reports_an_empty_lane_rather_than_guessing(tmp_path):
    """An open PR no queue row and no agents row owns is its own finding: nobody's board says
    that work exists. Attaching a plausible lane would hide exactly that."""
    runner = _pr_runner([{
        "number": 999, "title": "orphan", "headRefName": "e0999-nobody-claims-this",
        "createdAt": "2026-09-05T14:17:00Z", "updatedAt": "2026-09-05T14:17:00Z",
        "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
    }])
    h = _health_of(
        tmp_path, runner,
        agents=agent_row("jr-dev-1", "building", work="E-0228-something-else",
                         heartbeat=stamp(NOW, minutes=2)),
    )
    assert h.open_prs[0].lane == ""


def test_a_terminal_row_does_not_lend_its_lane_to_a_later_pr(tmp_path):
    """A shipped epic's number must not attach a lane to some later PR that happens to carry it.
    Terminal rows are provenance, not a claim - the same rule the claimable count already keeps."""
    runner = _pr_runner([{
        "number": 300, "title": "t", "headRefName": "e0228-reused-number",
        "createdAt": "2026-09-05T14:17:00Z", "updatedAt": "2026-09-05T14:17:00Z",
        "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
    }])
    h = _health_of(
        tmp_path, runner,
        queue="| E-0228-ground-an-escalation | t | jr-dev-2 | jr-dev-2 | done | - | - |\n",
    )
    assert h.open_prs[0].lane == ""


def test_a_pr_with_no_checks_is_not_a_pr_whose_checks_passed(tmp_path):
    """An empty rollup renders empty, never `0/0 SUCCESS`. A vacuous green is the reading that
    makes an unverified PR look ready to land."""
    runner = _pr_runner([{
        "number": 1, "title": "t", "headRefName": "b", "createdAt": "2026-09-05T14:17:00Z",
        "updatedAt": "2026-09-05T14:17:00Z", "isDraft": False, "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "statusCheckRollup": [],
    }])
    assert _health_of(tmp_path, runner).open_prs[0].checks == ""


def test_a_failing_check_is_named_rather_than_averaged_away(tmp_path):
    """`13/14 FAILURE` says both how much passed and that something did not. A bare count would
    let one red check hide inside a mostly-green number."""
    rollup = [{"conclusion": "SUCCESS"}] * 13 + [{"conclusion": "FAILURE"}]
    runner = _pr_runner([{
        "number": 1, "title": "t", "headRefName": "b", "createdAt": "2026-09-05T14:17:00Z",
        "updatedAt": "2026-09-05T14:17:00Z", "isDraft": False, "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED", "statusCheckRollup": rollup,
    }])
    assert _health_of(tmp_path, runner).open_prs[0].checks == "13/14 FAILURE"


def test_a_failed_gh_call_says_so_instead_of_reporting_no_open_prs(tmp_path):
    """An empty list because the tracker was unreachable is indistinguishable from an empty list
    because nothing is stuck, and only one of those is good news."""
    runner = _pr_runner([], rc=1, err="gh: could not resolve to a Repository")
    h = _health_of(tmp_path, runner)
    assert h.open_prs == ()
    assert "could not resolve" in h.open_prs_note


def test_health_is_not_empty_while_it_carries_an_open_pr_or_a_failed_read(tmp_path):
    """`is_empty` decides whether a renderer prints 'checked, nothing there'. Both the finding
    and the failure-to-look have to defeat it, for the reason the property's docstring gives."""
    assert fr.HealthSection(open_prs=(fr.OpenPR(number="1", title="t"),)).is_empty is False
    assert fr.HealthSection(open_prs_note="gh pr list exited 1").is_empty is False
    assert fr.HealthSection().is_empty is True


def test_no_tracker_configured_is_a_reason_not_a_silent_empty(tmp_path):
    """Same rule one step earlier: a report with no tracker has not looked."""
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    h = fr.collect(inputs_for(tmp_path), now=NOW, git_runner=_pr_runner([])).health
    assert h.open_prs == ()
    assert "no tracker" in h.open_prs_note


@pytest.mark.parametrize("label", ["STARTUP_FAILURE", "STALE", "ERROR", "EXPECTED"])
def test_a_check_state_the_bad_list_never_named_is_not_reported_as_success(tmp_path, label):
    """The first version started at SUCCESS and lowered it only for seven hardcoded labels, so
    every label it had not been told about fell through to the most reassuring answer. GitHub
    emits more than seven: a CheckRun can conclude STARTUP_FAILURE or STALE, and a legacy
    StatusContext - the shape the `conclusion or state` fallback exists to read - carries ERROR
    and EXPECTED. All four rendered `13/14 SUCCESS`, beside `mergeable=MERGEABLE`. EXPECTED is
    the single most common way a green-looking PR silently cannot merge."""
    rollup = [{"conclusion": "SUCCESS"}] * 13 + [{"conclusion": label}]
    runner = _pr_runner([{
        "number": 1, "title": "t", "headRefName": "b", "createdAt": "2026-09-05T14:17:00Z",
        "updatedAt": "2026-09-05T14:17:00Z", "isDraft": False, "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "statusCheckRollup": rollup,
    }])
    assert _health_of(tmp_path, runner).open_prs[0].checks == f"13/14 {label}"


def test_a_check_state_this_code_has_never_seen_reports_itself_rather_than_passing(tmp_path):
    """Membership decides and the ranking only orders what is already failing, so a label no list
    here mentions sorts ahead of every ranked one. The one thing a reader must never be told is
    that an unrecognised check passed."""
    rollup = [{"conclusion": "SUCCESS"}, {"conclusion": "SOME_FUTURE_STATE"}]
    runner = _pr_runner([{
        "number": 1, "title": "t", "headRefName": "b", "createdAt": "2026-09-05T14:17:00Z",
        "updatedAt": "2026-09-05T14:17:00Z", "isDraft": False, "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "statusCheckRollup": rollup,
    }])
    assert _health_of(tmp_path, runner).open_prs[0].checks == "1/2 SOME_FUTURE_STATE"


def _run(name, conclusion, job=None, started=None, workflow="CI", status="COMPLETED"):
    """One gh `statusCheckRollup` CheckRun, shaped the way `gh pr view --json` emits it."""
    c = {"__typename": "CheckRun", "name": name, "workflowName": workflow, "status": status,
         "conclusion": conclusion}
    if job is not None:
        c["detailsUrl"] = f"https://github.com/o/r/actions/runs/1/job/{job}"
    if started is not None:
        c["startedAt"] = started
    return c


def test_a_cancelled_older_run_beside_a_green_newer_one_reads_as_the_merge_gate_does():
    """GitHub's merge gate reads the newest check per name, but the rollup lists every run on the
    head SHA. #542 merged green and read `14/28 FAILURE`: counting the superseded run is a
    report that disagrees with the gate about the same PR."""
    names = [f"job {i}" for i in range(7)]
    rollup = [_run(n, "CANCELLED", job=100 + i) for i, n in enumerate(names)]
    rollup += [_run(n, "SUCCESS", job=200 + i) for i, n in enumerate(names)]
    assert fr._check_rollup(rollup) == "7/7 SUCCESS"


def test_the_dedupe_never_hides_a_newer_red_behind_an_older_green():
    """The collapse keeps the NEWEST entry, not the best one."""
    rollup = [_run("a", "SUCCESS", job=100), _run("a", "FAILURE", job=200), _run("b", "SUCCESS", job=150)]
    assert fr._check_rollup(rollup) == "1/2 FAILURE"


def test_a_queued_rerun_with_no_start_time_outranks_the_older_green_by_job_id():
    """A queued re-run has its job id before it has a `startedAt`, so the id orders it and the
    pending re-run is what the gate is waiting on."""
    rollup = [
        _run("a", "SUCCESS", job=100, started="2026-09-24T04:29:19Z"),
        _run("a", None, job=200, status="QUEUED"),
    ]
    out = fr._check_rollup(rollup)
    assert not out.endswith("SUCCESS"), out
    assert out.startswith("0/1 "), out


def test_the_same_check_name_in_two_workflows_is_two_checks():
    """Identity is (workflowName, name): a `lint` in CI and a `lint` in Nightly are different
    checks, and folding one into the other would drop a result the gate still reads."""
    rollup = [_run("lint", "SUCCESS", job=100, workflow="CI"),
              _run("lint", "FAILURE", job=50, workflow="Nightly")]
    assert fr._check_rollup(rollup) == "1/2 FAILURE"


def test_a_pair_nothing_can_order_keeps_both_entries():
    """No job id and no start time: guessing a winner could pick the green one, so both count."""
    rollup = [_run("a", "SUCCESS"), _run("a", "CANCELLED")]
    assert fr._check_rollup(rollup) == "1/2 CANCELLED"


def test_nameless_entries_are_never_collapsed():
    """An entry with no identity cannot be matched to another, so it counts as it always did."""
    assert fr._check_rollup([{"conclusion": "SUCCESS"}] * 14) == "14/14 SUCCESS"
    assert fr._check_rollup([{"conclusion": "SUCCESS"}] * 13 + [{"conclusion": "FAILURE"}]) == "13/14 FAILURE"


def test_a_group_with_job_ids_on_only_some_entries_is_ordered_by_one_key_not_a_mix():
    """Mixing keys pairwise is not one order: A beats B by id, B beats C by start, C beats A by
    start. That collapse dropped all three reds and read the PR as `1/1 SUCCESS`. Every entry has
    a start time, so the group is ordered by start time alone and the newest red survives."""
    rollup = [
        _run("a", "FAILURE", job=200, started="2026-09-24T01:00:00Z"),
        _run("a", "FAILURE", job=100, started="2026-09-24T03:00:00Z"),
        _run("a", "FAILURE", started="2026-09-24T02:00:00Z"),
        _run("b", "SUCCESS", job=300),
    ]
    assert fr._check_rollup(rollup) == "1/2 FAILURE"


def test_a_group_no_single_key_covers_keeps_every_entry():
    """One entry has only a job id, the other only a start time: nothing orders the pair, so both
    count rather than a guess that could pick the green one."""
    rollup = [_run("a", "SUCCESS", job=200), _run("a", "FAILURE", started="2026-09-24T03:00:00Z")]
    assert fr._check_rollup(rollup) == "1/2 FAILURE"


def test_a_start_time_with_no_offset_is_read_as_utc_rather_than_crashing_the_render():
    """Comparing an offset-less stamp with a `Z` one raises TypeError, which would take down the
    whole report for one odd check."""
    rollup = [_run("a", "FAILURE", started="2026-09-24T01:00:00"),
              _run("a", "SUCCESS", started="2026-09-24T02:00:00Z")]
    assert fr._check_rollup(rollup) == "1/1 SUCCESS"


def test_a_year_one_start_time_is_no_start_time_so_it_cannot_order_the_group():
    """gh renders a run that has not started as year 1. Read as a date it would be the OLDEST and
    lose to the green, hiding the pending run the gate is waiting on."""
    rollup = [_run("a", None, started="0001-01-01T00:00:00Z", status="QUEUED"),
              _run("a", "SUCCESS", started="2026-09-24T02:00:00Z")]
    assert fr._check_rollup(rollup).startswith("1/2 "), fr._check_rollup(rollup)


def test_a_saturated_open_pr_list_says_so_because_the_dropped_rows_are_the_stalled_ones(tmp_path):
    """gh returns newest-first, so a full page means the rows that fell off are the OLDEST - and a
    PR that opened days ago, is green and never landed is by construction among the oldest. A cap
    that looks complete while hiding exactly the population this signal exists to find is worse
    than no list, so saturation is reported."""
    payload = [{
        "number": n, "title": f"pr {n}", "headRefName": f"b{n}",
        "createdAt": "2026-09-05T14:17:00Z", "updatedAt": "2026-09-05T14:17:00Z",
        "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
    } for n in range(fr.OPEN_PR_LIMIT)]
    h = _health_of(tmp_path, _pr_runner(payload))
    assert len(h.open_prs) == fr.OPEN_PR_LIMIT
    assert "older ones may be missing" in h.open_prs_note
    assert "newest-first" in h.open_prs_note


def test_an_unsaturated_list_carries_no_note(tmp_path):
    """The note is a finding, not decoration: it must not fire on the ordinary case or a reader
    learns to skip it."""
    payload = [{
        "number": 1, "title": "t", "headRefName": "b", "createdAt": "2026-09-05T14:17:00Z",
        "updatedAt": "2026-09-05T14:17:00Z", "isDraft": False, "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "statusCheckRollup": [],
    }]
    assert _health_of(tmp_path, _pr_runner(payload)).open_prs_note == ""


def test_the_open_pr_limit_is_not_the_merged_window(tmp_path):
    """Borrowing the merged window's 20 made the stall detector discard the stalls first. A bound
    on MERGED pull requests is a window; the same bound on OPEN ones is a blind spot, so they are
    separate constants and the gh call must carry the open one."""
    runner = _pr_runner([])
    _health_of(tmp_path, runner)
    opens = [c for c in runner.seen if "pr" in c and "list" in c and "open" in c]
    assert len(opens) == 1, opens
    assert str(fr.OPEN_PR_LIMIT) in opens[0]
    assert fr.OPEN_PR_LIMIT > fr.DONE_WINDOW_LIMIT


def test_an_unknown_state_OUTRANKS_a_known_one_so_it_cannot_hide_behind_it(tmp_path):
    """The previous test could not see this: with only one failing state, `min` returns it
    whatever its rank, so the default rank was never exercised. A mutation flipping the unknown
    default from worst to best survived the whole suite.

    Here an unrecognised state competes with a RANKED one. If unknown sorted behind PENDING the
    report would print PENDING and the label nobody has seen would vanish - which is the same
    'reassuring default' failure one layer down from the one this fix was for."""
    rollup = [{"conclusion": "SUCCESS"}, {"conclusion": "PENDING"}, {"conclusion": "SOME_FUTURE_STATE"}]
    runner = _pr_runner([{
        "number": 1, "title": "t", "headRefName": "b", "createdAt": "2026-09-05T14:17:00Z",
        "updatedAt": "2026-09-05T14:17:00Z", "isDraft": False, "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED", "statusCheckRollup": rollup,
    }])
    assert _health_of(tmp_path, runner).open_prs[0].checks == "1/3 SOME_FUTURE_STATE"


def test_among_KNOWN_failing_states_the_ranking_still_picks_the_worst(tmp_path):
    """The ranking has to do its job too: FAILURE outranks PENDING, so a red check is not
    reported as merely pending."""
    rollup = [{"conclusion": "PENDING"}, {"conclusion": "FAILURE"}]
    runner = _pr_runner([{
        "number": 1, "title": "t", "headRefName": "b", "createdAt": "2026-09-05T14:17:00Z",
        "updatedAt": "2026-09-05T14:17:00Z", "isDraft": False, "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED", "statusCheckRollup": rollup,
    }])
    assert _health_of(tmp_path, runner).open_prs[0].checks == "0/2 FAILURE"


# --------------------------------------------------------------------------- #
# gh#446 - a decision-bearing bug leaves `selectable`, so INTAKE and WORK must carry it
# --------------------------------------------------------------------------- #
def _decision_bug(n, title="waiting on a product call"):
    return Issue(number=n, title=title, labels=("bug", "triaged", "needs-decision"))


def test_a_registry_held_decision_bearing_bug_is_counted_in_progress(tmp_path):
    """On the base tree the bug was selectable, so a registry row naming it put it in
    `bug_in_progress`. gh#446 moved it out of `selectable`; without INTAKE's awaiting half
    being unioned into WORK's bug set it fell out of BOTH columns while a lane built it."""
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("lead-dev", "building", work="E-0800", phase="building gh#17",
                         heartbeat=stamp(NOW, minutes=3)),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[_decision_bug(17)])
    assert [i.number for i in report.work.bug_in_progress.items] == [17]
    assert report.work.bug_claimable.total == 0


def test_attach_bug_work_unions_the_awaiting_half():
    """The unit form of the above, so a regression names `_attach_bug_work` directly."""
    class _Row:
        work, phase = "E-0800", "building gh#17"

    class _Reg:
        agents = {"lead-dev": _Row()}
        queue = {}

    intake = fr.IntakeSection(
        available=True,
        awaiting_count=1,
        awaiting=fr.Rows(items=(_decision_bug(17),), total=1),
    )
    work = fr._attach_bug_work(fr.WorkSection(), intake, _Reg())
    assert [i.number for i in work.bug_in_progress.items] == [17]
    # Unclaimed awaiting bugs are NOT claimable: a human call comes first.
    unclaimed = fr._attach_bug_work(fr.WorkSection(), intake, None)
    assert unclaimed.bug_claimable.total == 0
    assert unclaimed.bug_in_progress.total == 0


def test_an_unclaimed_decision_bearing_bug_makes_intake_non_empty_and_is_carried(tmp_path):
    """"Looked, found nothing" is false when a bug is waiting on a human's call. The row
    is carried with its title, beside its own count, for the INTAKE renderer."""
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[_decision_bug(13)])
    assert report.intake.selectable_count == 0
    assert report.intake.awaiting_count == 1
    assert [(i.number, i.title) for i in report.intake.awaiting.items] == [
        (13, "waiting on a product call")
    ]
    assert report.intake.is_empty is False


def test_is_empty_reads_the_awaiting_half():
    section = fr.IntakeSection(available=True, awaiting_count=1,
                               awaiting=fr.Rows(items=(_decision_bug(13),), total=1))
    assert section.is_empty is False
    assert fr.IntakeSection(available=True).is_empty is True   # the control


def test_a_board_whose_only_bug_work_awaits_a_decision_is_not_no_work_anywhere():
    """Like a drafted spec, a decision-bearing bug is work waiting on a human, not an empty
    product; telling the seat the product ran out of work would bury the call it owes."""
    report = _report()
    waiting = fr.IntakeSection(available=True, awaiting_count=1,
                               awaiting=fr.Rows(items=(_decision_bug(13),), total=1))
    from dataclasses import replace as _replace
    assert _report().no_work_anywhere is True                  # the control
    assert _replace(report, intake=waiting).no_work_anywhere is False


# --------------------------------------------------------------------------- #
# lead-dev reads its selection, not only its lane (a stall must not read as DRY)
# --------------------------------------------------------------------------- #
def _idle_pair(tmp_path: Path, *, queue: str = "") -> None:
    """lead-dev and jr-dev-1 idle between wakes: fresh beat, eta ahead, holding nothing."""
    beat, eta = stamp(NOW, minutes=5), (NOW + timedelta(minutes=30)).isoformat()
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("lead-dev", "idle", heartbeat=beat, eta=eta)
        + agent_row("jr-dev-1", "idle", heartbeat=beat, eta=eta),
        queue=queue,
    )


def _two_claimable(tmp_path: Path) -> None:
    for slug in ("first-epic", "second-epic"):
        make_spec(tmp_path, "alpha", slug,
                  frontmatter=f"current_state: {APPROVED}\nqueue_class: fleet-buildable\n")


def test_an_idle_lead_dev_with_startable_work_is_not_DRY(tmp_path):
    """The repro: two claimable epics and a selectable bug, and lead-dev read DRY - "needs work,
    not diagnosis" - over the one stall the report exists to catch. lead-dev picks from the
    claimable set and only then publishes into its lane, so its empty lane was never the
    question. The junior beside it IS dry: its lane is where its work arrives."""
    _idle_pair(tmp_path)
    _two_claimable(tmp_path)
    bug = Issue(number=7, title="a triaged bug", labels=("bug", "triaged"))
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[bug])
    assert report.work.claimable_now.total == 2
    assert report.work.bug_claimable.total == 1

    by_agent = {lane.agent: lane for lane in report.lanes.lanes}
    lead = by_agent["lead-dev"]
    assert lead.state != fr.LANE_DRY
    assert lead.state == fr.LANE_LIVE
    assert "needs work" not in lead.why
    assert "3 startable items" in lead.why
    assert "2 epics claimable now" in lead.why and "1 bug claimable" in lead.why
    assert lead.why.startswith("idle with ")

    assert by_agent["jr-dev-1"].state == fr.LANE_DRY, "an empty junior lane is still dry"
    assert report.lanes.dry_count == 1


def test_lead_dev_with_nothing_startable_is_DRY_and_names_both_channels(tmp_path):
    """The control: no claimable epic, no selectable bug, empty lane. DRY is true, and its
    sentence names the selection as well as the lane, because both were read and both were
    empty."""
    _idle_pair(tmp_path)
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    lead = {lane.agent: lane for lane in report.lanes.lanes}["lead-dev"]
    assert lead.state == fr.LANE_DRY
    assert fr.SELECTION_BASIS in lead.why
    assert "needs work, not diagnosis" in lead.why


def test_lead_dev_DRY_over_an_unread_tracker_says_the_bug_side_was_not_read(tmp_path):
    """No epic is claimable and the tracker failed. DRY stays (nothing on disk or in the lane),
    but a verdict built over an unread bug side must say so rather than claim it was empty."""
    _idle_pair(tmp_path)
    inputs = inputs_for(
        tmp_path, tracker_host_ref="owner/repo", tracker_kind="github-issues", tracker_cli="gh"
    )
    report = fr.collect(inputs, now=NOW, issue_runner=lambda argv: (1, "", "could not resolve host"))
    lead = {lane.agent: lane for lane in report.lanes.lanes}["lead-dev"]
    assert lead.state == fr.LANE_DRY
    assert lead.why.startswith(
        f"nothing waiting for it in {fr.SELECTION_BASIS}; needs work, not diagnosis "
        "(the bug side was not read: tracker unreachable"
    )


def test_a_published_lead_row_still_answers_through_the_lane(tmp_path):
    """CONTROL, and it passes before and after the selection change on purpose: a ready row in
    `lead` is work arriving the ordinary way, so the selection is not consulted and the LIVE row
    carries no startable count."""
    _idle_pair(tmp_path, queue="| E-0001-first-epic | first-epic | lead | - | ready | - | - |\n")
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    lead = {lane.agent: lane for lane in report.lanes.lanes}["lead-dev"]
    assert lead.state == fr.LANE_LIVE
    assert lead.why == ""


# --------------------------------------------------------------------------- #
# "No work anywhere" is a finding, never the absence of a look
# --------------------------------------------------------------------------- #
def test_an_unreachable_tracker_is_could_not_determine_not_no_work(tmp_path):
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    inputs = inputs_for(
        tmp_path, tracker_host_ref="owner/repo", tracker_kind="github-issues", tracker_cli="gh"
    )
    report = fr.collect(inputs, now=NOW, issue_runner=lambda argv: (1, "", "could not resolve host"))
    assert report.intake.available is False
    assert report.intake.tracker_configured is True
    assert report.no_work_anywhere is False
    assert report.work_sentence.startswith("Could not determine")
    assert "could not resolve host" in report.work_sentence
    assert "no work anywhere" not in report.work_sentence.lower()


def test_a_missing_registry_is_could_not_determine_not_no_work(tmp_path):
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert report.registry_available is False
    assert report.no_work_anywhere is False
    assert "the registry" in report.work_sentence
    assert report.work_sentence.startswith("Could not determine")


def test_a_missing_registry_does_not_report_a_claim_check_that_never_ran(tmp_path):
    """With no registry no claim was subtracted, so `claimable_now` equal to every approved epic
    would be a claim check reported as run. The epics are named as possibly held instead."""
    make_spec(tmp_path, "alpha", "maybe-held",
              frontmatter=f"current_state: {APPROVED}\nqueue_class: fleet-buildable\n")
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert report.work.approved_claimable.total == 1
    assert report.work.claimable_now.total == 0
    assert [r.slug for r in report.work.claim_ambiguous] == ["maybe-held"]
    assert report.work.claim_note.startswith("no claim could be checked")
    assert report.no_work_anywhere is False


def test_with_no_tracker_configured_the_sentence_does_not_name_the_tracker(tmp_path):
    """No tracker is an ABSENT source, not an unread one: the alert may fire, and its sentence
    must not send the reader to a tracker that does not exist."""
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    report = fr.collect(inputs_for(tmp_path, tracker_kind="filesystem"), now=NOW)
    assert report.intake.tracker_configured is False
    assert report.no_work_anywhere is True
    assert report.work_sentence == fr.FleetReport.NO_WORK_SENTENCE_NO_TRACKER
    assert "the tracker" not in report.work_sentence
    assert "no work anywhere" in report.work_sentence.lower()


def test_a_configured_tracker_with_nothing_selectable_keeps_the_tracker_sentence(tmp_path):
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert report.no_work_anywhere is True
    assert report.work_sentence == fr.FleetReport.NO_WORK_SENTENCE


def test_an_inert_approved_epic_keeps_the_alert_false_and_is_named_with_its_reason(tmp_path):
    """One frontmatter fix from claimable is not an empty product. The sentence names the
    epic; HEALTH says which gate it failed."""
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    make_spec(tmp_path, "alpha", "shiny-thing",
              frontmatter=f"current_state: {APPROVED}\nqueue_class: feature\n")
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert report.health.inert_count == 1
    assert report.no_work_anywhere is False
    assert "1 approved epic is inert: shiny-thing" in report.work_sentence
    (row,) = report.health.inert_promotions
    assert "out of vocabulary" in row.reason and "'feature'" in row.reason


@pytest.mark.parametrize(
    "frontmatter, excluded, expected",
    [
        ("queue_class: local-tooling\n", (), "known but not dispatchable"),
        ("queue_class: fleet-buildable\ndomain: side-program\n", ("side-program",),
         "excluded domain side-program"),
    ],
    ids=["non-dispatchable-class", "excluded-domain"],
)
def test_each_inert_reason_names_the_gate_it_failed(tmp_path, frontmatter, excluded, expected):
    make_spec(tmp_path, "alpha", "stuck", frontmatter=f"current_state: {APPROVED}\n{frontmatter}")
    records, _ = fr.survey_with_skips(tmp_path, PAST_CUTOFF, project="alpha")
    (inert,) = fr.inert_promotions(records, project="alpha", excluded_domains=excluded)
    assert expected in fr.inert_reason(inert, excluded)


# --------------------------------------------------------------------------- #
# A paused fleet reads as paused
# --------------------------------------------------------------------------- #
def test_stood_down_lanes_are_counted_and_only_a_held_claim_is_called_kept(tmp_path):
    """The repro: two lanes stood down and one reaped rendered "0 live, 0 halted, 1 reaped" with
    the stopped lanes counted nowhere. And "It keeps its claim" was said of a row holding `-`."""
    phase = reg_mod.STOOD_DOWN_PHASE + ": human paused the fleet"
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("lead-dev", "idle", phase=phase, heartbeat=stamp(NOW, hours=5))
        + agent_row("jr-dev-1", "building", work="E-0001-some-epic", phase=phase,
                    heartbeat=stamp(NOW, hours=5))
        + agent_row("jr-dev-2", "idle", phase=reg_mod.REAPED_PHASE, heartbeat=stamp(NOW, hours=9)),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert report.lanes.stood_down_count == 2
    assert report.lanes.reaped_count == 1
    assert report.lanes.live_count == report.lanes.silent_count == 0

    by_agent = {lane.agent: lane for lane in report.lanes.lanes}
    assert "It keeps its claim" in by_agent["jr-dev-1"].why
    assert "It holds no claim" in by_agent["lead-dev"].why
    assert "keeps its claim" not in by_agent["lead-dev"].why


# --------------------------------------------------------------------------- #
# SILENT says how overdue, or that nothing was promised
# --------------------------------------------------------------------------- #
def test_silent_distinguishes_a_lapsed_eta_from_no_eta(tmp_path):
    beat = stamp(NOW, hours=5)
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("jr-dev-1", "building", work="E-0001-a", heartbeat=beat)
        + agent_row("jr-dev-2", "building", work="E-0002-b", heartbeat=beat,
                    eta=stamp(NOW, hours=4)),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    by_agent = {lane.agent: lane for lane in report.lanes.lanes}
    absent, lapsed = by_agent["jr-dev-1"], by_agent["jr-dev-2"]
    assert absent.state == lapsed.state == fr.LANE_SILENT

    assert absent.why.startswith("last beat 5h00m ago; no eta recorded")
    assert lapsed.why.startswith("last beat 5h00m ago; promised back by 14:00Z, 4h00m overdue")
    for lane in (absent, lapsed):
        assert "no lapsing eta" not in lane.why
        assert "undiagnosed" in lane.why


def test_an_eta_on_another_day_carries_its_date():
    assert fr._eta_account(stamp(NOW, days=2), NOW) == (
        "promised back by 2026-08-29T18:00Z, 2d 0h overdue"
    )
    assert fr._eta_account("-", NOW) == "no eta recorded"
    assert fr._eta_account("soon", NOW) == "eta unreadable ('soon')"


# --------------------------------------------------------------------------- #
# What lead-dev can START: seam-blocked excluded, ambiguous not dry, unread bugs named
# --------------------------------------------------------------------------- #
def _unreachable_tracker_inputs(tmp_path: Path) -> fr.ReportInputs:
    return inputs_for(
        tmp_path, tracker_host_ref="owner/repo", tracker_kind="github-issues", tracker_cli="gh"
    )


def _unreachable(argv):
    return (1, "", "could not resolve host")


def test_lead_dev_startable_says_bugs_not_read_rather_than_zero_bugs(tmp_path):
    """One claimable epic and an unreachable tracker. "0 bugs claimable" would be a count of a
    side nobody read."""
    _idle_pair(tmp_path)
    make_spec(tmp_path, "alpha", "first-epic",
              frontmatter=f"current_state: {APPROVED}\nqueue_class: fleet-buildable\n")
    report = fr.collect(_unreachable_tracker_inputs(tmp_path), now=NOW, issue_runner=_unreachable)
    lead = {lane.agent: lane for lane in report.lanes.lanes}["lead-dev"]
    assert lead.state == fr.LANE_LIVE
    assert "1 startable item (1 epic claimable now, bugs not read: tracker unreachable" in lead.why
    assert "0 bugs" not in lead.why


def _spec_with_files(root: Path, slug: str, *paths: str) -> None:
    files = "\n".join(f"- `{p}` - because" for p in paths)
    _write(
        root / "projects" / "alpha" / "design" / "specs" / f"{slug}.md",
        f"---\nslug: {slug}\ntitle: {slug} title\ncurrent_state: {APPROVED}\n"
        f"queue_class: fleet-buildable\npriority: 100\n---\n\n# {slug}\n\n## Files\n\n{files}\n",
    )


def test_a_seam_blocked_epic_is_not_startable_for_lead_dev(tmp_path):
    """The only claimable epic overlaps the files an in-flight epic holds: a free lane cannot
    start it, so it does not count toward what lead-dev can start - and the DRY sentence says
    why the claimable count did not help."""
    _spec_with_files(tmp_path, "held-epic", "shared/module.py")
    _spec_with_files(tmp_path, "free-epic", "shared/module.py")
    beat, eta = stamp(NOW, minutes=5), (NOW + timedelta(minutes=30)).isoformat()
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("lead-dev", "idle", heartbeat=beat, eta=eta)
        + agent_row("jr-dev-1", "building", work="E-0001-held-epic", heartbeat=beat, eta=eta),
        queue=_q("E-0001-held-epic", state="building", claimed_by="jr-dev-1"),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert [r.slug for r in report.work.claimable_now] == ["free-epic"]
    assert [b.name for b in report.work.seam_blocked] == ["free-epic"]

    lead = {lane.agent: lane for lane in report.lanes.lanes}["lead-dev"]
    assert lead.state == fr.LANE_DRY
    assert "1 claimable item seam-blocked behind in-flight work" in lead.why


def test_ambiguous_approved_epics_keep_lead_dev_off_DRY_and_are_named(tmp_path):
    """An epic whose claim cannot be resolved may be free. "Needs work" over it would prescribe
    writing work that may already exist."""
    beat, eta = stamp(NOW, minutes=5), (NOW + timedelta(minutes=30)).isoformat()
    _colliders(
        tmp_path,
        agents=agent_row("lead-dev", "idle", heartbeat=beat, eta=eta)
        + agent_row("jr-dev-2", "building", work="E-0001-acme-3-5-chat", heartbeat=beat, eta=eta),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert report.work.claimable_now.total == 0
    assert report.work.claim_ambiguous.total == 2

    lead = {lane.agent: lane for lane in report.lanes.lanes}["lead-dev"]
    assert lead.state == fr.LANE_LIVE
    assert "needs work" not in lead.why
    assert ("2 approved epics whose claim states are ambiguous: "
            "acme-3-5-chat-alpha, acme-3-5-chat-beta") in lead.why


# --------------------------------------------------------------------------- #
# The bug side does not report a claim check that never ran
# --------------------------------------------------------------------------- #
def test_a_missing_registry_holds_selectable_bugs_out_of_bug_claimable(tmp_path):
    bug = Issue(number=7, title="a triaged bug", labels=("bug", "triaged"))
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[bug])
    assert report.registry_available is False
    assert report.work.bug_claimable.total == 0
    assert [i.number for i in report.work.bug_claim_ambiguous] == [7]
    assert report.work.bug_note.startswith(
        "only the in-progress label was checked; the registry claim check never ran"
    )
    assert report.work.is_empty is False


# --------------------------------------------------------------------------- #
# A tracker kind with an unusable CLI is an unread tracker, never "no tracker"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cli", ["", "not-a-cli"], ids=["cli-unset", "cli-unknown"])
def test_a_tracker_kind_with_an_unusable_cli_is_unread_not_absent(tmp_path, cli):
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    inputs = inputs_for(
        tmp_path, tracker_host_ref="owner/repo", tracker_kind="github-issues", tracker_cli=cli
    )
    report = fr.collect(inputs, now=NOW)
    assert report.intake.tracker_configured is True
    assert "tracker CLI unusable" in report.intake.reason
    assert report.unread_work_sources
    assert report.no_work_anywhere is False
    assert report.work_sentence.startswith("Could not determine")
    assert report.work_sentence != fr.FleetReport.NO_WORK_SENTENCE_NO_TRACKER


def test_the_unread_registry_is_not_named_twice(tmp_path):
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert "the registry (could not read the registry" not in report.work_sentence
    assert report.work_sentence.count("the registry") == 1


# --------------------------------------------------------------------------- #
# The PR readers honour the resolved git_host (kind and cli)
# --------------------------------------------------------------------------- #
def _write_config(root: Path, *, kind: str, cli: str, work_source: str) -> Path:
    """A scratch config home: environment.md plus one project `alpha`, as `config` reads them."""
    home = root / "cfg"
    (home / "projects").mkdir(parents=True)
    d = str(root).replace("\\", "/")
    cli_line = f"\n  cli: {cli}" if cli else ""
    (home / "environment.md").write_text(
        "# environment\n\n```yaml\n"
        f"schema_version: 1\nperson:\n  name: Sample User\ndata_root: {d}\n"
        f"git_host:\n  kind: {kind}{cli_line}\nnotify:\n  kind: none\n"
        f"lock_dir: {d}\nshell: posix\nsuite_root: {d}\nworktree_root: {d}\n```\n",
        encoding="utf-8",
    )
    (home / "projects" / "alpha.md").write_text(
        "# project alpha\n\n```yaml\n"
        "schema_version: 1\nname: alpha\ncanonical_repo:\n"
        f"  host_ref: nobody-xyz/alpha-does-not-exist\n  local_checkout: {d}\n"
        f"verification:\n  strategy: test-suite\nwork_source:\n  kind: {work_source}\n```\n",
        encoding="utf-8",
    )
    return home


def _merged_pr_runner():
    """Records every argv; answers any `pr list` with one merged PR, everything else empty."""
    seen = []

    def run(argv):
        seen.append(list(argv))
        if "pr" in argv and "list" in argv:
            return 0, json.dumps([{"number": 7, "title": "t", "mergedAt": "2026-08-31T12:00:00Z",
                                   "mergeCommit": {"oid": "abcdef1234"}}]), ""
        return 0, "", ""

    run.seen = seen
    return run


@pytest.mark.parametrize(
    "kind,cli,work_source,expect",
    [
        ("gitea", "none", "gitea", "no pull request reader for git_host gitea (cli: none)"),
        ("gitea", "gh", "none", "no pull request reader for git_host gitea (cli: gh)"),
        ("gitlab", "glab", "none", "no pull request reader for git_host gitlab (cli: glab)"),
        ("local", "", "none", "no pull request reader for git_host local (cli: none)"),
        ("github", "none", "none", "no gh-compatible CLI for git_host github (cli: none)"),
    ],
    ids=["gitea-none", "gitea-gh", "gitlab-glab", "local", "github-cli-none"],
)
def test_the_pr_readers_do_not_call_gh_on_a_host_without_a_gh_reader(
    tmp_path, monkeypatch, kind, cli, work_source, expect
):
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(_write_config(
        tmp_path, kind=kind, cli=cli, work_source=work_source)))
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("jr-dev-1", "building", work="E-0228-x", heartbeat=stamp(NOW, minutes=2)),
    )
    runner = _merged_pr_runner()

    report = fr.collect(fr.inputs_from_config("alpha"), now=NOW, git_runner=runner)

    assert not [a for a in runner.seen if "pr" in a], runner.seen
    assert report.done.available is False
    assert report.done.reason == expect
    assert report.health.open_prs == ()
    assert report.health.open_prs_note == expect
    assert report.lanes.production_available is False
    assert all(expect in ln.production_basis for ln in report.lanes.lanes)


def test_a_github_host_with_gh_still_reads_pull_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(_write_config(
        tmp_path, kind="github", cli="gh", work_source="github-issues")))
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    runner = _merged_pr_runner()

    report = fr.collect(fr.inputs_from_config("alpha"), now=NOW, git_runner=runner, issues=[])

    assert report.done.available is True
    assert [r.number for r in report.done.rows] == ["7"]
    pr_calls = [a for a in runner.seen if "pr" in a]
    assert pr_calls and all(a[0] == "gh" for a in pr_calls)


def test_the_default_runner_names_the_binary_that_was_missing():
    rc, out, err = fr._default_git_runner(["agentflow-no-such-cli-xyz", "pr", "list"])
    assert rc == 127
    assert err.startswith("agentflow-no-such-cli-xyz not found:")
    assert "git not found" not in err


def test_a_missing_gh_is_reported_as_gh_not_found_in_every_pr_reading(tmp_path, monkeypatch):
    """git runs and gh does not: DONE, open PRs and production must name gh, not git."""

    monkeypatch.setenv("AGENTFLOW_CONFIG", str(_write_config(
        tmp_path, kind="github", cli="gh", work_source="none")))
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("jr-dev-1", "building", work="E-0228-x", heartbeat=stamp(NOW, minutes=2)),
    )

    def fake_run(argv, *args, **kwargs):
        if argv[0] == "gh":
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(fr.subprocess, "run", fake_run)

    report = fr.collect(fr.inputs_from_config("alpha"), now=NOW)

    assert report.done.reason.startswith("gh not found"), report.done.reason
    assert report.health.open_prs_note.startswith("gh not found"), report.health.open_prs_note
    assert report.lanes.lanes
    for ln in report.lanes.lanes:
        assert "gh not found" in ln.production_basis
        assert "git not found" not in ln.production_basis


def test_a_gh_that_fails_to_run_is_reported_as_gh_failed_to_run(monkeypatch):
    """A timeout is a SubprocessError, not a missing binary: the reason must still name gh."""

    def fake_run(argv, *args, **kwargs):
        if argv[0] == "gh":
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(fr.subprocess, "run", fake_run)

    rc, out, err = fr._default_git_runner(["gh", "pr", "list"])

    assert rc == 1
    assert out == ""
    assert err.startswith("gh failed to run:"), err


def test_a_hand_built_input_with_no_git_host_kind_is_refused_not_read_as_github(tmp_path):
    """No `git_host_kind` means an unset host: the PR readers refuse it and call nothing."""
    inputs = fr.ReportInputs(
        data_root=tmp_path,
        project="alpha",
        mtime_cutoff_iso=PAST_CUTOFF,
        tracker_host_ref="o/r",
        tracker_cli="gh",
    )
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, "[]", ""

    expected = "no pull request reader for git_host unset (cli: gh)"
    assert fr._merged_prs(inputs, runner, 5) == (None, expected)
    assert fr._open_prs(inputs, runner, (), (), 5) == (None, expected)
    assert calls == []


# --------------------------------------------------------------------------- #
# WORK counts past INTAKE's row cap (gh#760)
# --------------------------------------------------------------------------- #
def test_bug_totals_count_every_selectable_bug_not_the_rows_intake_rendered(tmp_path):
    """The repro: 48 selectable bugs, `rows_per_section` 10. INTAKE printed 48 and WORK printed
    10 claimable with no `showing` note, because WORK split INTAKE's capped rows. One bug past
    the cap is held by a registry row, so the split has to run over the whole set to find it."""
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        queue="| E-0300 | fix gh#45 here | jr-dev-1 | jr-dev-1 | building | - |  |\n",
    )
    issues = [Issue(number=n, title=f"bug {n}", labels=("bug", "triaged")) for n in range(1, 49)]
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=issues)

    assert report.intake.selectable_count == 48
    assert report.intake.selectable.shown == 10
    work = report.work
    assert work.bug_claimable.total == 47
    assert work.bug_claimable.shown == 10
    assert work.bug_claimable.showing == "showing 10 of 47"
    assert [i.number for i in work.bug_in_progress] == [45]
    # The seam verdict saw only the rendered rows, and now says so.
    assert work.seam_partial is True


def test_lead_dev_is_credited_with_every_claimable_bug_past_the_cap(tmp_path):
    """The same cap reached LANES: lead-dev's why counted the rendered rows, not the bugs."""
    _idle_pair(tmp_path)
    issues = [Issue(number=n, title=f"bug {n}", labels=("bug", "triaged")) for n in range(1, 49)]
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=issues)
    lead = {lane.agent: lane for lane in report.lanes.lanes}["lead-dev"]
    assert "48 bugs claimable" in lead.why


def test_an_uncapped_board_still_reads_as_complete(tmp_path):
    """CONTROL: under the cap nothing is truncated, and nothing may claim it was."""
    make_registry(tmp_path / "projects" / "alpha" / "status" / "registry.md")
    issues = [Issue(number=n, title=f"bug {n}", labels=("bug", "triaged")) for n in range(1, 4)]
    work = fr.collect(inputs_for(tmp_path), now=NOW, issues=issues).work
    assert work.bug_claimable.total == 3
    assert work.bug_claimable.showing == ""
    assert work.seam_partial is False


# --------------------------------------------------------------------------- #
# Lane liveness reads only rows a lane can act on (gh#741)
# --------------------------------------------------------------------------- #
def _junior_pair(tmp_path: Path, *, queue: str = "") -> None:
    beat = stamp(NOW, minutes=5)
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("jr-dev-1", "idle", heartbeat=beat)
        + agent_row("jr-dev-2", "idle", heartbeat=beat),
        queue=queue,
    )


@pytest.mark.parametrize("terminal", reg_mod.TERMINAL_QUEUE_STATES)
def test_a_terminal_row_does_not_keep_its_claimant_LIVE(tmp_path, terminal):
    """The repro: an idle junior whose only Queue row is `done` or `withdrawn` read LIVE with an
    empty why. A terminal row is provenance, not a claim, so both juniors are DRY."""
    _junior_pair(
        tmp_path,
        queue=f"| E-0007-old-thing | old thing | jr-1 | jr-dev-1 | {terminal} | - | shipped |\n",
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    by_agent = {lane.agent: lane for lane in report.lanes.lanes}
    assert by_agent["jr-dev-1"].state == fr.LANE_DRY
    assert by_agent["jr-dev-2"].state == fr.LANE_DRY


def test_no_queue_row_leaves_both_juniors_DRY(tmp_path):
    """CONTROL for the above."""
    _junior_pair(tmp_path)
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert {lane.state for lane in report.lanes.lanes} == {fr.LANE_DRY}


def _published_board(tmp_path: Path, queue: str) -> None:
    make_spec(tmp_path, "alpha", "alpha-three", frontmatter=_APPROVED.format(p=100))
    beat, eta = stamp(NOW, minutes=5), (NOW + timedelta(minutes=30)).isoformat()
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents="".join(
            agent_row(a, "idle", heartbeat=beat, eta=eta)
            for a in ("jr-dev-1", "jr-dev-2", "lead-dev")
        ),
        queue=queue,
    )


def test_a_published_unclaimed_epic_is_named_in_its_lane_not_claimable(tmp_path):
    """The repro: `alpha-three` published into jr-dev-1's lane and not yet claimed. It stayed in
    `claimable_now` and lead-dev read LIVE on "1 epic claimable now", although lead-dev can
    neither select nor republish it. It now waits in `published_unclaimed` with its lane, lead-dev
    is credited with nothing, and the lane owner is LIVE on its ready row."""
    _published_board(
        tmp_path, "| E-0003-alpha-three | alpha-three | jr-dev-1 | - | ready | - | - |\n"
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    work = report.work
    assert [r.slug for r in work.approved_claimable] == ["alpha-three"]
    assert [r.slug for r in work.claimable_now] == []
    assert [(p.name, p.lane, p.row_id, p.kind) for p in work.published_unclaimed] == [
        ("alpha-three", "jr-dev-1", "E-0003-alpha-three", "epic")
    ]
    by_agent = {lane.agent: lane for lane in report.lanes.lanes}
    assert by_agent["lead-dev"].state == fr.LANE_DRY
    assert "startable" not in by_agent["lead-dev"].why
    assert by_agent["jr-dev-1"].state == fr.LANE_LIVE
    # Work waiting in a lane is not an empty board.
    assert report.no_work_anywhere is False
    assert report.work_sentence == ""


def test_a_published_row_whose_name_collides_falls_to_claim_ambiguous(tmp_path):
    """The same join as a claim: a name two approved epics share, with a title that separates
    neither, cannot be resolved to one epic, so both are named as ambiguous."""
    _colliders(
        tmp_path,
        queue="| E-0001-acme-3-5-chat | acme chat | jr-dev-1 | - | ready | - | - |\n",
    )
    work = _work_of(tmp_path)
    assert sorted(r.slug for r in work.claim_ambiguous) == [
        "acme-3-5-chat-alpha", "acme-3-5-chat-beta"
    ]
    assert work.published_unclaimed.total == 0
    assert work.claimable_now.total == 0


def test_a_published_unclaimed_bug_leaves_bug_claimable(tmp_path):
    """The bug half: a non-terminal row naming gh#12 with `claimed_by` `-` holds that bug in its
    lane. It leaves `bug_claimable`, is named with its lane, and lead-dev is not credited with it."""
    _published_board(
        tmp_path, "| E-0300 | BUG gh#12 - the fix | jr-dev-2 | - | ready | - | - |\n"
    )
    issues = [
        Issue(number=12, title="published", labels=("bug", "triaged")),
        Issue(number=13, title="free", labels=("bug", "triaged")),
    ]
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=issues)
    work = report.work
    assert [i.number for i in work.bug_claimable] == [13]
    assert work.bug_in_progress.total == 0
    assert [(p.name, p.lane, p.kind) for p in work.published_unclaimed if p.kind == "bug"] == [
        ("gh#12", "jr-dev-2", "bug")
    ]
    lead = {lane.agent: lane for lane in report.lanes.lanes}["lead-dev"]
    assert "1 bug claimable" in lead.why


# --------------------------------------------------------------------------- #
# A lane halted on a dead referent reaches BLOCKED ON YOU (gh#671)
# --------------------------------------------------------------------------- #
def test_lanes_halted_on_dead_referents_are_blocked_on_you_with_reason_and_fix(tmp_path):
    """The repro: jr-dev-1 halted on Q-0001, answered 26h ago, heartbeat 30h old; jr-dev-2 halted
    on Q-0009, which exists nowhere. The report read two ordinary HALTED rows ("resumes when:
    Q-0001") and an empty BLOCKED ON YOU, while `read --stranded` called both unjudgeable. A
    third lane is halted on a question it raised and then retracted."""
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("jr-dev-1", "halted", heartbeat=stamp(NOW, hours=30), waiting_on="Q-0001")
        + agent_row("jr-dev-2", "halted", heartbeat=stamp(NOW, minutes=5), waiting_on="Q-0009")
        + agent_row("jr-dev-3", "halted", heartbeat=stamp(NOW, minutes=5), waiting_on="Q-0002"),
        questions=qblock("Q-0001", state="answered", urgency="waiting", raised_by="jr-dev-1",
                         question="which way", default="left", raised_at=stamp(NOW, hours=40),
                         answer="left", answered_by="human", answered_at=stamp(NOW, hours=26))
        + qblock("Q-0002", state="retracted", urgency="waiting", raised_by="jr-dev-3",
                 question="moot", default="-", raised_at=stamp(NOW, hours=5)),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])

    assert report.lanes.halted_count == 3
    assert report.lanes.silent_count == 0
    assert report.blocked.questions.total == 0
    stranded = {r.agent: r for r in report.blocked.stranded}
    assert {a: (r.waiting_on, r.reason) for a, r in stranded.items()} == {
        "jr-dev-1": ("Q-0001", "question-closed"),
        "jr-dev-2": ("Q-0009", "referent-missing"),
        "jr-dev-3": ("Q-0002", "question-retracted"),
    }
    assert all(r.fix == fr.DEAD_REFERENT_FIXES[r.reason] for r in stranded.values())
    assert report.blocked.is_empty is False

    by_agent = {lane.agent: lane for lane in report.lanes.lanes}
    for agent, row in stranded.items():
        assert by_agent[agent].state == fr.LANE_HALTED
        assert "resumes when" not in by_agent[agent].why
        assert row.reason in by_agent[agent].why and row.fix in by_agent[agent].why


def test_a_lane_halted_on_a_live_open_question_is_not_stranded(tmp_path):
    """CONTROL: a live open question really is waiting on the human, and it is already listed
    as an open question. It must not be doubled into the stranded rows."""
    make_registry(
        tmp_path / "projects" / "alpha" / "status" / "registry.md",
        agents=agent_row("jr-dev-1", "halted", heartbeat=stamp(NOW, minutes=5), waiting_on="Q-0001"),
        questions=qblock("Q-0001", state="open", urgency="waiting", raised_by="jr-dev-1",
                         question="which way", default="left", raised_at=stamp(NOW, hours=2)),
    )
    report = fr.collect(inputs_for(tmp_path), now=NOW, issues=[])
    assert report.blocked.stranded.total == 0
    assert [q.qid for q in report.blocked.questions] == ["Q-0001"]
    lane = {lane.agent: lane for lane in report.lanes.lanes}["jr-dev-1"]
    assert lane.why == "self-declared halt; resumes when: Q-0001"


def test_the_dead_referent_reasons_are_the_registrys_own_minus_the_live_waits():
    """The fix map is keyed on `registry.UNJUDGEABLE_REASONS` and must not drift from it. The
    three reasons left out are live waits: a blocker with an owner, and an open question whose
    age cannot be read."""
    assert set(fr.DEAD_REFERENT_FIXES) <= set(reg_mod.UNJUDGEABLE_REASONS)
    assert set(reg_mod.UNJUDGEABLE_REASONS) - set(fr.DEAD_REFERENT_FIXES) == {
        "blocker-referent", "raised-at-unreadable", "raised-at-in-future"
    }
