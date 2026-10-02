"""Tests for the filesystem epic-survey (replacement for an earlier HTTP work-discovery service).

Fixtures mirror the three-pillar default layout (projects/<name>/{design/{specs,plans},status})
and the wire shape the old server emitted. They assert the behavior-frozen
contract: precedence ladder, 3-pass reconciliation, status.md always-wins,
raw current_state vs reconciled lifecycle_state as separate fields,
slug-collision keep-more-progressed + warn, the priority/+inf rule, the
depends_on inversion (blocks_count), and the ready/blocked dep math.
"""

from __future__ import annotations

import codecs
import math
import os
import re
from pathlib import Path

import pytest

from agentflow import epic_survey as _survey
from agentflow.epic_survey import (
    APPROVED_CURRENT_STATE,
    DISPATCHABLE_QUEUE_CLASSES,
    FILES_GATED_CURRENT_STATES,
    EpicRecord,
    KNOWN_QUEUE_CLASSES,
    NON_DISPATCHABLE_QUEUE_CLASSES,
    TERMINAL_CURRENT_STATES,
    UNSET_PRIORITY,
    blocked_status,
    blocks_count,
    by_priority,
    dispatchable,
    forbidden_cross_project_dependencies,
    is_orchestrator_status,
    ready_set,
    selectable,
    survey_epics,
    warn_dependencies_satisfied_by_a_superseded_parent,
    warn_stranded_terminal_dependencies,
    warn_unresolvable_dependencies,
)
from agentflow.plan_parser import files_section, parse_spec_frontmatter
from agentflow.vocabulary import CURRENT_QUEUE_CLASS, RETIRED_QUEUE_CLASS

# A cutoff far in the past so freshly-created tmp fixtures always pass.
PAST_CUTOFF = "2020-01-01T00:00:00+00:00"

# The standing human approval, as frontmatter. Every fixture that expects to SURVIVE
# dispatchable() has to carry it, because approval is now dispatchable()'s primary filter
# (issue #40) rather than a line of builder prose. Kept as a constant so a fixture exercising
# some OTHER dispatch rule reads as "approved, plus the one thing under test".
APPROVED = "current_state: approved-for-autonomous\n"


# --------------------------------------------------------------------------- #
# Fixture builders (Phase-2 default layout)
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_spec(root: Path, project: str, slug: str, *, frontmatter: str = "") -> Path:
    body = f"---\nslug: {slug}\ntitle: {slug} title\n{frontmatter}---\n\nspec body\n"
    return _write(root / "projects" / project / "design" / "specs" / f"{slug}.md", body)


def make_plan(root: Path, project: str, slug: str, *, frontmatter: str = "") -> Path:
    body = (
        "---\n"
        f"epic: {slug} epic\n"
        f"spec: /specs/{slug}.md\n"
        f"slug: {slug}\n"
        "phases: [phase-1]\n"
        "tasks:\n"
        "  1:\n"
        "    phase: phase-1\n"
        "    deps: []\n"
        "test_commands: {}\n"
        f"{frontmatter}"
        "---\n\nplan body\n"
    )
    return _write(root / "projects" / project / "design" / "plans" / f"{slug}.md", body)


def make_status(
    root: Path,
    project: str,
    slug: str,
    *,
    status: str = "running",
    done: int = 1,
    total: int = 2,
    paused: bool = False,
    dir_named: bool = False,
    epic_signature: bool = True,
) -> Path:
    tasks_lines = ""
    for i in range(1, total + 1):
        state = "done" if i <= done else "pending"
        tasks_lines += f"  {i}:\n    state: {state}\n"
    head = "epic:" if epic_signature else "notepic:"
    body = (
        "---\n"
        f"{head} {slug} epic\n"
        f"plan: /plans/{slug}.md\n"
        "started: 2026-01-01T00:00:00+00:00\n"
        f"status: {status}\n"
        "current_phase: phase-1\n"
        f"paused: {'true' if paused else 'false'}\n"
        "tasks:\n"
        f"{tasks_lines}"
        "---\n\nstatus body\n"
    )
    if dir_named:
        return _write(root / "projects" / project / "status" / slug / "status.md", body)
    return _write(root / "projects" / project / "status" / f"{slug}.status.md", body)


def _by_slug(records: list[EpicRecord]) -> dict[str, EpicRecord]:
    return {r.slug: r for r in records}


# --------------------------------------------------------------------------- #
# Precedence / reconciliation
# --------------------------------------------------------------------------- #
def test_spec_only_is_speccing(tmp_path: Path):
    make_spec(tmp_path, "proj", "alpha")
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["alpha"].lifecycle_state == "speccing"


def test_plan_promotes_over_spec(tmp_path: Path):
    make_spec(tmp_path, "proj", "beta")
    make_plan(tmp_path, "proj", "beta")
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["beta"].lifecycle_state == "planning"
    assert recs["beta"].title == "beta epic"  # plan's `epic` wins the title


def test_status_always_wins_over_spec_and_plan(tmp_path: Path):
    make_spec(tmp_path, "proj", "gamma")
    make_plan(tmp_path, "proj", "gamma")
    make_status(tmp_path, "proj", "gamma", status="running", done=1, total=4)
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    g = recs["gamma"]
    assert g.lifecycle_state == "running"
    assert g.progress == {"done": 1, "total": 4, "percent": 25}
    assert g.current_phase == "phase-1"


def test_status_done_sets_done(tmp_path: Path):
    make_plan(tmp_path, "proj", "delta")
    make_status(tmp_path, "proj", "delta", status="done", done=3, total=3)
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["delta"].lifecycle_state == "done"
    assert recs["delta"].progress == {"done": 3, "total": 3, "percent": 100}


def test_current_state_raw_separate_from_lifecycle_state(tmp_path: Path):
    # An approval override on the spec + a status.md marking it running: the raw
    # approval must survive on current_state while lifecycle_state reflects progress.
    make_spec(tmp_path, "proj", "epsilon", frontmatter="current_state: approved-for-autonomous\n")
    make_status(tmp_path, "proj", "epsilon", status="running")
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    e = recs["epsilon"]
    assert e.current_state == "approved-for-autonomous"   # raw approval signal
    assert e.lifecycle_state == "running"                 # reconciled progress signal


def test_a_nonterminal_approval_alone_does_not_mark_the_epic_done(tmp_path: Path):
    """Kept, but renamed for what it actually proves (gh#319).

    It used to be called ``test_completion_truth_is_status_never_frontmatter`` and its comment
    claimed "only a status.md with status: done does". The fixture says
    ``approved-for-autonomous`` -- a NON-TERMINAL value, which no path turns into ``done`` -- so
    the assertion could not fail for the reason the name gave. It is a real check of a narrower
    thing: approval is not progress.
    """
    make_spec(tmp_path, "proj", "zeta", frontmatter="current_state: approved-for-autonomous\n")
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["zeta"].lifecycle_state != "done"


def test_terminal_frontmatter_alone_DOES_set_done_when_no_status_md_exists(tmp_path: Path):
    """The discriminating fixture the renamed test above omits (gh#319).

    This is the behaviour the module's docstring denied for its whole life, and it is the
    behaviour every consumer actually gets: on this box NO status.md file exists for either
    installed project, so `lifecycle_state` is the normalised frontmatter state for every
    record, and 317 of 590 of them read ``done``.

    Pinned as TRUE rather than fixed, deliberately. Whether the fleet-wide
    "completion truth = status.md" safeguard should be made real, retired, or documented as
    inert is an open decision (gh#319); this test records what the code does so that decision
    is taken against a measurement instead of against the docstring.
    """
    make_spec(tmp_path, "proj", "zeta_done", frontmatter="current_state: done\n")
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["zeta_done"].lifecycle_state == "done", (
        "frontmatter no longer sets lifecycle_state; if that is deliberate, gh#319 was decided "
        "and this test plus the epic_survey docstring must move together"
    )


def test_a_status_md_still_outranks_the_frontmatter_when_one_exists(tmp_path: Path):
    """The precedence half is real code and stays pinned (gh#319).

    The defect is that no corpus on this box exercises it, not that it is wrong. A terminal
    frontmatter with a RUNNING status.md must read ``running``: filesystem evidence of an epic
    in flight outranks an approval field a lane may have written ahead of itself.
    """
    make_spec(tmp_path, "proj", "eta", frontmatter="current_state: done\n")
    make_status(tmp_path, "proj", "eta", status="running")
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["eta"].lifecycle_state == "running", (
        "status.md no longer outranks frontmatter, so the only independent completion signal "
        "the survey has is gone"
    )


# --------------------------------------------------------------------------- #
# Sidecar fields: priority (+inf) and domain
# --------------------------------------------------------------------------- #
def test_priority_read_and_unset_is_inf(tmp_path: Path):
    make_spec(tmp_path, "proj", "hasprio", frontmatter="priority: 300\n")
    make_spec(tmp_path, "proj", "noprio")
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["hasprio"].priority == 300.0
    assert recs["noprio"].priority == UNSET_PRIORITY == math.inf


def test_by_priority_sorts_unset_last(tmp_path: Path):
    make_spec(tmp_path, "proj", "p100", frontmatter="priority: 100\n")
    make_spec(tmp_path, "proj", "p200", frontmatter="priority: 200\n")
    make_spec(tmp_path, "proj", "punset")
    ordered = [r.slug for r in by_priority(survey_epics(tmp_path, PAST_CUTOFF))]
    assert ordered.index("p100") < ordered.index("p200") < ordered.index("punset")


def test_domain_passthrough(tmp_path: Path):
    make_spec(tmp_path, "proj", "withdomain", frontmatter="domain: backend\n")
    make_spec(tmp_path, "proj", "nodomain")
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["withdomain"].domain == "backend"
    assert recs["nodomain"].domain is None


# --------------------------------------------------------------------------- #
# Terminal current_state is not reopened by a stale plan sidecar (issue #1)
# --------------------------------------------------------------------------- #
def test_done_spec_is_not_reopened_by_a_stale_approved_plan(tmp_path: Path):
    """The exact shape issue #1 measured: 10 shipped epics surveyed as approved.

    Reconciling a shipped epic means editing the SPEC. Nobody also edits the plan sidecar, so
    the plan keeps saying `approved-for-autonomous` forever - and before the fix it won
    unconditionally, putting merged work back in front of a selector.
    """
    make_spec(tmp_path, "proj", "shipped-epic", frontmatter="current_state: done\n")
    make_plan(tmp_path, "proj", "shipped-epic",
              frontmatter="current_state: approved-for-autonomous\n")
    rec = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))["shipped-epic"]
    assert rec.current_state == "done"


@pytest.mark.parametrize("terminal", sorted(TERMINAL_CURRENT_STATES - {"archived"}))
def test_every_terminal_state_survives_a_stale_plan(tmp_path: Path, terminal: str):
    """One parametrize so a future edit cannot fix `done` and leave the siblings open -
    the whole set moves together or the gap is silent.

    `archived` is covered separately below: it is ALSO dropped by the archived filter, so
    asserting it here would pass for the wrong reason.
    """
    make_spec(tmp_path, "proj", "term", frontmatter=f"current_state: {terminal}\n")
    make_plan(tmp_path, "proj", "term", frontmatter="current_state: approved-for-autonomous\n")
    assert _by_slug(survey_epics(tmp_path, PAST_CUTOFF))["term"].current_state == terminal


@pytest.mark.parametrize(
    "plan_frontmatter, label",
    [
        ("current_state: approved-for-autonomous\n", "unlisted-token"),
        ("current_state: planning\n", "planning"),      # 20 real plans in the corpus
        ("current_state: running\n", "running"),
        ("current_state: done\n", "done"),
        ("", "no-current-state"),                       # 8 real plans; defaults to "planning"
    ],
)
def test_archived_spec_is_excluded_whatever_the_stale_plan_says(
    tmp_path: Path, plan_frontmatter: str, label: str
):
    """An archived spec stays archived no matter what the plan sidecar carries.

    The earlier version of this test used ONLY `approved-for-autonomous` and generalised from
    it. That value happens to be absent from `_LIFECYCLE_PRECEDENCE`, so it scored -999 and lost
    to archived's -1 by accident of vocabulary - the test passed while the guarantee it claimed
    did not hold. Every value that IS in the table (`planning` 1, `running` 2, `done` 3) beat
    archived, as did a plan with no `current_state`, which defaults to "planning". Those are the
    common corpus shapes, so the one case the fixture chose was the only one that worked.

    Parametrized over all of them precisely so the fixture can no longer pick its own winner.
    """
    make_spec(tmp_path, "proj", "arc", frontmatter="current_state: archived\n")
    make_plan(tmp_path, "proj", "arc", frontmatter=plan_frontmatter)

    assert "arc" not in _by_slug(survey_epics(tmp_path, PAST_CUTOFF)), label

    # "absent" alone would also be true of a fixture that failed to parse, so prove the record
    # exists and is archived on BOTH fields.
    revealed = _by_slug(survey_epics(tmp_path, PAST_CUTOFF, include_archived=True))["arc"]
    assert revealed.lifecycle_state == "archived", label
    assert revealed.current_state == "archived", label


def test_terminal_spec_freezes_lifecycle_state_not_just_current_state(tmp_path: Path):
    """The half of issue #1 the first fix missed.

    `lifecycle_state` is the field the archived filter and choose-next's candidate bucket
    actually key on, and `_promote` was lifting a terminal spec up to the plan's state. Live
    instances existed in the corpus: archived specs offered as `planning` candidates.
    """
    make_spec(tmp_path, "proj", "frozen", frontmatter="current_state: done\n")
    make_plan(tmp_path, "proj", "frozen", frontmatter="current_state: planning\n")
    rec = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))["frozen"]
    assert rec.lifecycle_state == "done"
    assert rec.current_state == "done"


def test_a_non_string_current_state_does_not_destroy_the_whole_survey(tmp_path: Path):
    """YAML 1.1 turns `current_state: yes` into a bool and `2026-07-24` into a date.

    `current_state` reaches the survey uncoerced, and the plan-pass call site sits OUTSIDE the
    per-file try/except - so calling a str method on it raised straight out of `survey_epics`
    and took EVERY OTHER EPIC with it. The bystander assertion is the point: this module's
    posture everywhere else is log-and-skip one file, never abort the run.
    """
    make_spec(tmp_path, "proj", "bystander", frontmatter="current_state: approved-for-autonomous\n")
    for slug, raw in [("yes-bool", "yes"), ("on-bool", "on"), ("a-date", "2026-07-24"),
                      ("an-int", "1")]:
        make_spec(tmp_path, "proj", slug, frontmatter=f"current_state: {raw}\n")
        make_plan(tmp_path, "proj", slug, frontmatter="current_state: approved-for-autonomous\n")

    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))   # must not raise
    assert recs["bystander"].current_state == "approved-for-autonomous"
    # The malformed ones are not terminal, so the plan still wins - unchanged from before.
    assert recs["yes-bool"].current_state == "approved-for-autonomous"


def test_casing_is_normalized_for_dependency_math_too(tmp_path: Path):
    """`current_state: Done` must close the epic for BOTH approval and dep math.

    Tolerating case on the approval field alone produced the mirror image of issue #1: the epic
    read closed for dispatch but never entered `_done_slugs`, so every dependant stayed blocked
    forever. Asserted through `ready_set`, which is what consumers actually call.
    """
    make_spec(tmp_path, "proj", "base", frontmatter='current_state: "Done"\n')
    make_spec(tmp_path, "proj", "child", frontmatter="depends_on: [base]\n")
    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert _by_slug(recs)["base"].lifecycle_state == "done"
    assert "child" in {r.slug for r in ready_set(recs)}


def test_a_terminal_spec_beats_a_colliding_sibling_regardless_of_glob_order(tmp_path: Path):
    """The duplicated hazard: `_merge_spec_record` kept the unguarded merge.

    `superseded` and `approved-for-autonomous` are BOTH absent from `_LIFECYCLE_PRECEDENCE`, so
    the comparison was -999 > -999 = False and the winner fell out of filename order. Both
    orderings are asserted, because a test that only writes one of them passes on the accident.
    """
    for first, second in (("aaa", "zzz"), ("zzz", "aaa")):
        root = tmp_path / f"{first}{second}"
        _write(root / "projects" / "proj" / "design" / "specs" / f"{first}.md",
               "---\nslug: dup\ntitle: dup\ncurrent_state: superseded\n---\n\nbody\n")
        _write(root / "projects" / "proj" / "design" / "specs" / f"{second}.md",
               "---\nslug: dup\ntitle: dup\ncurrent_state: approved-for-autonomous\n---\n\nbody\n")
        rec = _by_slug(survey_epics(root, PAST_CUTOFF))["dup"]
        assert rec.current_state == "superseded", f"{first} then {second}"


def test_terminal_match_tolerates_casing_and_whitespace(tmp_path: Path):
    """This value gates dispatch; an exact-match test would let `Done ` reopen a shipped epic."""
    make_spec(tmp_path, "proj", "loud", frontmatter='current_state: "  DONE  "\n')
    make_plan(tmp_path, "proj", "loud", frontmatter="current_state: approved-for-autonomous\n")
    assert _by_slug(survey_epics(tmp_path, PAST_CUTOFF))["loud"].current_state.strip() == "DONE"


def test_a_yaml_trailing_comment_does_not_defeat_the_terminal_check(tmp_path: Path):
    """The corpus really does write `current_state: archived  # cancelled by ...`.

    The YAML parser strips an unquoted trailing comment, so the value arrives clean - this
    asserts that end to end rather than trusting it.
    """
    make_spec(tmp_path, "proj", "cmt",
              frontmatter="current_state: archived  # 2026-08-13 queue audit: CANCELLED\n")
    make_plan(tmp_path, "proj", "cmt", frontmatter="current_state: approved-for-autonomous\n")
    rec = _by_slug(survey_epics(tmp_path, PAST_CUTOFF, include_archived=True))["cmt"]
    assert rec.current_state == "archived"    # not "archived  # 2026-08-13 ..."


def test_a_NON_terminal_spec_state_still_yields_to_the_plan(tmp_path: Path):
    """The negative that keeps the fix narrow.

    Promotion still has to flow the normal direction: a spec sitting at `drafted` with a plan
    that says `approved-for-autonomous` must survey as APPROVED, or the fix would freeze every
    epic at its spec state and silently empty the ready set.
    """
    make_spec(tmp_path, "proj", "live", frontmatter="current_state: drafted\n")
    make_plan(tmp_path, "proj", "live", frontmatter="current_state: approved-for-autonomous\n")
    rec = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))["live"]
    assert rec.current_state == "approved-for-autonomous"


def test_terminal_spec_with_no_plan_state_is_unaffected(tmp_path: Path):
    make_spec(tmp_path, "proj", "nostate", frontmatter="current_state: done\n")
    make_plan(tmp_path, "proj", "nostate")
    assert _by_slug(survey_epics(tmp_path, PAST_CUTOFF))["nostate"].current_state == "done"


def test_the_reopened_epic_is_actually_undispatchable_end_to_end(tmp_path: Path):
    """The full selection expression the builder skills run, end to end.

    WHAT CHANGED (issue #40): this test used to append a hand-written
    `current_state == "approved-for-autonomous"` predicate to the pipeline, because
    `dispatchable()` did not read `current_state` at all and the approval rule lived only in
    the builder SKILL.md prose. Transcribing prose into a test asserted the rule as written,
    not the rule as enforced -- so the published expression really did return a `superseded`
    epic as its top pick. `dispatchable()` now enforces approval itself, so the composition
    below is the WHOLE selection: no local predicate, nothing transcribed.

    It still matters, and arguably more: it is the only test that runs the expression the
    skills actually publish -- `selectable()` over `survey_epics()` -- against real files on
    disk rather than hand-built records. It is what would catch the reconciliation half
    (a terminal spec freezing a stale approved plan sidecar) coming apart from the selection
    half, which is a seam no single-function test crosses.
    """
    make_spec(tmp_path, "proj", "was-shipped", frontmatter="current_state: done\n")
    make_plan(tmp_path, "proj", "was-shipped",
              frontmatter="current_state: approved-for-autonomous\n")
    make_spec(tmp_path, "proj", "really-ready", frontmatter="current_state: drafted\n")
    make_plan(tmp_path, "proj", "really-ready",
              frontmatter="current_state: approved-for-autonomous\n")
    recs = survey_epics(tmp_path, PAST_CUTOFF)
    selected = {r.slug for r in selectable(recs)}
    assert selected == {"really-ready"}


def test_priority_domain_inherited_from_spec_when_plan_omits(tmp_path: Path):
    make_spec(tmp_path, "proj", "inh", frontmatter="priority: 150\ndomain: infra\n")
    make_plan(tmp_path, "proj", "inh")  # plan carries neither field
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["inh"].priority == 150.0
    assert recs["inh"].domain == "infra"
    assert recs["inh"].lifecycle_state == "planning"


# --------------------------------------------------------------------------- #
# Slug collision
# --------------------------------------------------------------------------- #
def test_slug_collision_keeps_more_progressed_and_warns(tmp_path: Path, caplog):
    # Two spec files that resolve to the same slug; one carries a planning
    # override so it is more-progressed. The survey keeps it and warns.
    make_spec(tmp_path, "projA", "dup", frontmatter="current_state: planning\n")
    make_spec(tmp_path, "projB", "dup")  # plain speccing
    with caplog.at_level("WARNING"):
        recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["dup"].lifecycle_state == "planning"
    assert any("slug collision" in r.message for r in caplog.records)


def test_a_drafted_spec_shadowed_by_a_done_one_is_named_in_the_warning(tmp_path: Path, caplog):
    # gh#723: two dated gap specs share a slug once the date prefix is stripped. The done one wins
    # (precedence unchanged), and the warning must name the drafted loser as shadowed, since that
    # warning is the only place the lost draft appears.
    specs = tmp_path / "projects" / "proj" / "design" / "specs"
    _write(specs / "2026-09-10-complete-shipped-one.md",
           "---\nslug: complete-shipped-one\ntitle: Gap A\ncurrent_state: done\n---\n\nbody\n")
    drafted = _write(specs / "2026-09-20-complete-shipped-one.md",
                     "---\nslug: complete-shipped-one\ntitle: Gap B\ncurrent_state: drafted\n---\n\nbody\n")
    with caplog.at_level("WARNING"):
        recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF, project="proj"))
    assert recs["complete-shipped-one"].lifecycle_state == "done"
    shadow = [r.message for r in caplog.records if "is shadowed" in r.message]
    assert len(shadow) == 1, [r.message for r in caplog.records]
    assert "DRAFTED spec" in shadow[0] and drafted.name in shadow[0]


def test_a_collision_without_a_drafted_loser_carries_no_shadow_clause(tmp_path: Path, caplog):
    make_spec(tmp_path, "projA", "dup", frontmatter="current_state: planning\n")
    make_spec(tmp_path, "projB", "dup")
    with caplog.at_level("WARNING"):
        survey_epics(tmp_path, PAST_CUTOFF)
    collisions = [r.message for r in caplog.records if "slug collision" in r.message]
    assert collisions and not any("shadowed" in m for m in collisions)


# --------------------------------------------------------------------------- #
# archived / mtime cutoff
# --------------------------------------------------------------------------- #
def test_archived_filtered_by_default_included_when_flagged(tmp_path: Path):
    make_spec(tmp_path, "proj", "old", frontmatter="current_state: archived\n")
    default = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert "old" not in default
    withar = _by_slug(survey_epics(tmp_path, PAST_CUTOFF, include_archived=True))
    assert withar["old"].lifecycle_state == "archived"


def test_mtime_cutoff_skips_old_files(tmp_path: Path):
    p = make_spec(tmp_path, "proj", "stale")
    old = 1_500_000_000  # 2017-07-14
    os.utime(p, (old, old))
    # cutoff after the file's mtime (2020 > 2017) → skipped
    assert survey_epics(tmp_path, PAST_CUTOFF) == []
    # cutoff before the file's mtime (2010 < 2017) → included
    assert _by_slug(survey_epics(tmp_path, "2010-01-01T00:00:00+00:00")).get("stale") is not None


# --------------------------------------------------------------------------- #
# A STALE SPEC MUST NOT LET ITS PLAN SIDECAR REOPEN IT (issue #42).
#
# The terminal freeze in the plan pass was guarded on the spec RECORD existing, so a spec
# skipped on mtime left `existing is None`, the freeze never engaged, and the plan's
# `current_state` won unconditionally. `dispatchable()` is the advertised single enforcer of
# the approval gate, so a shipped epic that got back through it reached a lane and was rebuilt
# and auto-merged with no second gate. `DEFAULT_MTIME_CUTOFF` is a fixed date rather than a
# rolling window, so the exposed population only grows.
#
# The fix is deliberately NARROW: consult a skipped spec's frontmatter for its terminal state
# ONLY when a plan would otherwise supply that record's state. The negatives below are as
# load-bearing as the positives - they are what keeps it from being a cutoff bypass.
# --------------------------------------------------------------------------- #
_STALE_MTIME = 1_500_000_000   # 2017-07-14, comfortably before PAST_CUTOFF (2020)


def _age(path: Path) -> Path:
    """Backdate *path* past PAST_CUTOFF so the survey skips it on mtime."""
    os.utime(path, (_STALE_MTIME, _STALE_MTIME))
    return path


def _count_stale_reads(monkeypatch) -> list[Path]:
    """Record every skipped-spec read the survey performs. Returns the growing list.

    Counted at `_stale_spec_state` because that is the ONE function that opens a file the
    cutoff already said to skip - which makes this list the direct measurement of the fix's
    cost rather than a proxy for it.
    """
    seen: list[Path] = []
    real = _survey._stale_spec_state

    def counting(path: Path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(_survey, "_stale_spec_state", counting)
    return seen


def test_a_stale_terminal_spec_is_not_resurrected_by_its_fresh_plan(tmp_path: Path):
    """THE HEADLINE. Issue #42's exact scenario, end to end through `selectable()`.

    Reported shape: spec `done` at mtime 2020-06-01 against a 2021-01-01 cutoff, plan sidecar
    fresh at `approved-for-autonomous`. The survey returned
    `[('was-shipped', 'approved-for-autonomous', 'approved-for-autonomous')]` and
    `selectable()` returned `['was-shipped']` - a merged epic offered to a lane again.

    `selectable()` is asserted and not only `survey_epics()` because the harm is not that a
    field reads wrong; it is that the record REACHES A LANE, and that is the only expression
    that answers whether it does.
    """
    _age(make_spec(tmp_path, "proj", "was-shipped", frontmatter="current_state: done\n"))
    make_plan(tmp_path, "proj", "was-shipped",
              frontmatter="current_state: approved-for-autonomous\n")
    # A genuinely-ready sibling, so an empty result cannot pass for the wrong reason.
    make_spec(tmp_path, "proj", "really-ready", frontmatter="current_state: drafted\n")
    make_plan(tmp_path, "proj", "really-ready",
              frontmatter="current_state: approved-for-autonomous\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF)
    rec = _by_slug(recs)["was-shipped"]
    assert rec.current_state == "done"
    assert rec.lifecycle_state == "done"
    assert {r.slug for r in selectable(recs)} == {"really-ready"}


@pytest.mark.parametrize("terminal", sorted(TERMINAL_CURRENT_STATES))
def test_every_terminal_state_survives_a_stale_spec_and_a_fresh_plan(
    tmp_path: Path, terminal: str
):
    """The whole terminal set moves together or the gap is silent.

    Parametrized over `TERMINAL_CURRENT_STATES` itself rather than a hand-copied list, so a
    value added to the frozenset cannot be added without this test covering it.
    `include_archived=True` because `archived` is ALSO dropped by the archived filter -
    asserting absence would pass for the wrong reason on exactly one member.
    """
    _age(make_spec(tmp_path, "proj", "term", frontmatter=f"current_state: {terminal}\n"))
    make_plan(tmp_path, "proj", "term", frontmatter="current_state: approved-for-autonomous\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    rec = _by_slug(recs)["term"]
    assert rec.current_state == terminal, terminal
    # The freeze covers BOTH fields, exactly as it does for a spec that was not skipped:
    # lifecycle_state is what the archived filter and choose-next's buckets key on.
    assert rec.lifecycle_state == terminal, terminal
    assert selectable(recs) == [], terminal
    # `archived` still falls to the archived filter, as it would have with a fresh spec.
    assert ("term" in _by_slug(survey_epics(tmp_path, PAST_CUTOFF))) is (terminal != "archived")


def test_a_stale_NON_terminal_spec_still_yields_to_the_plan(tmp_path: Path):
    """The negative that keeps the fix from over-reaching. Pins TODAY'S behavior.

    This fix is about terminal specs only. A stale spec at `drafted` with a fresh approved plan
    surveyed as approved before, and must still - freezing every stale spec at its own state
    would silently empty the ready set, which is the same stranding failure as the bug being
    fixed, pointing the other way.
    """
    _age(make_spec(tmp_path, "proj", "live", frontmatter="current_state: drafted\n"))
    make_plan(tmp_path, "proj", "live", frontmatter="current_state: approved-for-autonomous\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF)
    rec = _by_slug(recs)["live"]
    assert rec.current_state == "approved-for-autonomous"
    # `_promote(None, ...)` returns the plan's own state verbatim when there is no prior, so
    # the lifecycle field carries the approval token here. Odd, and PRE-EXISTING - pinned as it
    # is precisely so the fix cannot quietly change it while claiming to leave this path alone.
    assert rec.lifecycle_state == "approved-for-autonomous"
    assert {r.slug for r in selectable(recs)} == {"live"}


def test_a_FRESH_terminal_spec_with_a_fresh_plan_still_freezes(tmp_path: Path, monkeypatch):
    """No regression on the path that already worked - and no read it does not need.

    The read-count assertion is the second half: when the spec record exists the freeze already
    has everything it needs, so consulting the skipped set there would be pure waste.
    """
    seen = _count_stale_reads(monkeypatch)
    make_spec(tmp_path, "proj", "frozen", frontmatter="current_state: done\n")
    make_plan(tmp_path, "proj", "frozen", frontmatter="current_state: approved-for-autonomous\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert _by_slug(recs)["frozen"].current_state == "done"
    assert _by_slug(recs)["frozen"].lifecycle_state == "done"
    assert selectable(recs) == []
    assert seen == []


def test_a_stale_terminal_spec_with_no_plan_is_unchanged(tmp_path: Path, monkeypatch):
    """NOT A CUTOFF BYPASS, stated as an assertion rather than as prose.

    A spec past the cutoff with nothing else pointing at its slug stays skipped: absent from
    the survey, and never even opened. A fix that "helpfully" resurrected stale terminal specs
    into the corpus, or that indexed the skipped set eagerly, fails here - and that is the only
    thing separating the narrowed option from the general bypass it was chosen over.
    """
    seen = _count_stale_reads(monkeypatch)
    _age(make_spec(tmp_path, "proj", "forgotten", frontmatter="current_state: done\n"))

    assert survey_epics(tmp_path, PAST_CUTOFF) == []
    assert survey_epics(tmp_path, PAST_CUTOFF, include_archived=True) == []
    assert seen == []


def test_a_stale_spec_is_not_consulted_when_every_plan_has_its_spec(tmp_path: Path, monkeypatch):
    """Cost tracks the STALE SET, and only when a plan actually asks.

    Two claims: nothing is read while every plan has a spec record behind it, and when one
    finally does not, the index is built ONCE over the stale set - two stale specs and two
    plans needing them cost two reads, not four.
    """
    seen = _count_stale_reads(monkeypatch)
    stale_a = _age(make_spec(tmp_path, "proj", "old-a", frontmatter="current_state: done\n"))
    stale_b = _age(make_spec(tmp_path, "proj", "old-b", frontmatter="current_state: done\n"))
    make_spec(tmp_path, "proj", "paired", frontmatter="current_state: drafted\n")
    make_plan(tmp_path, "proj", "paired", frontmatter="current_state: approved-for-autonomous\n")

    survey_epics(tmp_path, PAST_CUTOFF)
    assert seen == [], "a plan with its own spec record must not open the skipped set"

    make_plan(tmp_path, "proj", "old-a", frontmatter="current_state: approved-for-autonomous\n")
    make_plan(tmp_path, "proj", "old-b", frontmatter="current_state: approved-for-autonomous\n")
    seen.clear()
    survey_epics(tmp_path, PAST_CUTOFF)
    assert sorted(seen) == sorted([stale_a, stale_b])


def _broken_yaml_spec(root: Path) -> Path:
    return _write(
        root / "projects" / "proj" / "design" / "specs" / "broken.md",
        '---\nslug: broken\ntitle: "unterminated\ncurrent_state: done\n---\n\nbody\n',
    )


def _unreadable_spec(root: Path) -> Path:
    # A DIRECTORY named `<slug>.md` matches the spec glob and cannot be read - the unreadable
    # arm produced by the filesystem rather than by a monkeypatch.
    return _write(root / "projects" / "proj" / "design" / "specs" / "broken.md" / "inner.txt", "x").parent


@pytest.mark.parametrize(
    "make_broken, label",
    [(_broken_yaml_spec, "malformed-yaml"), (_unreadable_spec, "unreadable")],
)
def test_an_unreadable_stale_spec_fails_CLOSED_and_is_reported(
    tmp_path: Path, caplog, make_broken, label: str
):
    """FAIL CLOSED, and be counted while failing.

    A spec that cannot be read cannot be shown non-terminal, and the two mistakes do not cost
    the same: promoting wrongly re-ships merged work with no second gate, withholding wrongly
    leaves one epic unoffered until someone fixes a file that is already broken. So the plan
    record is dropped.

    Dropping it SILENTLY would be its own defect - a record that vanishes with no line is the
    failure `counted N, skipped M` exists to make impossible. The log SHAPE is asserted, not
    just the presence of a warning: the fleet report's skip handler reads these records
    structurally (the `, skipping:` template marker plus exactly two args), so a message
    saying the same thing in a different shape would be counted as zero skips.
    """
    broken = _age(make_broken(tmp_path))
    make_plan(tmp_path, "proj", "broken", frontmatter="current_state: approved-for-autonomous\n")
    make_spec(tmp_path, "proj", "healthy", frontmatter="current_state: drafted\n")
    make_plan(tmp_path, "proj", "healthy", frontmatter="current_state: approved-for-autonomous\n")

    with caplog.at_level("WARNING"):
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    assert "broken" not in _by_slug(recs), label
    assert selectable(recs) == [_by_slug(recs)["healthy"]], label   # the bystander survives

    skips = [r for r in caplog.records if ", skipping:" in str(r.msg)]
    assert len(skips) == 1, label
    assert isinstance(skips[0].args, tuple) and len(skips[0].args) == 2, label
    assert Path(str(skips[0].args[0])) == broken, label


def test_an_unreadable_stale_spec_blocks_only_its_own_slug(tmp_path: Path):
    """Fail-closed, not fail-wide.

    The broken file's slug is only as good as its filename here - there is no frontmatter left
    to read one out of - which is the same fallback pass 1 already trusts for a spec that has
    none. What must NOT happen is the blunt reading of "fail closed": one unreadable spec
    somewhere in the stale set freezing every other plan-only record in the survey.
    """
    _age(_write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "broken.md",
        '---\nslug: broken\ntitle: "unterminated\n---\n\nbody\n',
    ))
    _age(make_spec(tmp_path, "proj", "old-ok", frontmatter="current_state: drafted\n"))
    make_plan(tmp_path, "proj", "old-ok", frontmatter="current_state: approved-for-autonomous\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert {r.slug for r in selectable(recs)} == {"old-ok"}


def test_a_stale_spec_with_no_frontmatter_is_not_an_error(tmp_path: Path):
    """A spec with no frontmatter is a legitimate `speccing` record to pass 1, not a failure.

    It carries no `current_state`, so it is not terminal and it blocks nothing. Treating the
    no-frontmatter case as unreadable would fail closed on an ordinary corpus shape and strand
    real work.
    """
    _age(_write(tmp_path / "projects" / "proj" / "design" / "specs" / "bare.md", "just prose, no yaml\n"))
    make_plan(tmp_path, "proj", "bare", frontmatter="current_state: approved-for-autonomous\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert {r.slug for r in selectable(recs)} == {"bare"}


def test_the_stale_spec_is_located_by_its_declared_slug_not_its_filename(tmp_path: Path):
    """Slug resolution is pass 1's, so a date-prefixed filename declaring a different slug
    still matches. A glob invented over filenames would miss this record and leave the hole
    open for exactly the oldest specs - the ones most likely to be past the cutoff.
    """
    _age(_write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "2026-06-02-old-title.md",
        "---\nslug: renamed\ntitle: renamed\ncurrent_state: done\n---\n\nbody\n",
    ))
    make_plan(tmp_path, "proj", "renamed", frontmatter="current_state: approved-for-autonomous\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert _by_slug(recs)["renamed"].current_state == "done"
    assert selectable(recs) == []


def _bom_spec(root: Path, slug: str, state: str = "done") -> Path:
    """A perfectly ordinary spec, saved by a tool that writes a UTF-8 BOM.

    Not exotic on this fleet: PowerShell's `Out-File` and `Set-Content -Encoding utf8` both
    write one by default, and the byte is invisible in every editor. The frontmatter regex is
    anchored at `^---`, so the BOM makes a well-formed spec parse as "no frontmatter at all".
    """
    p = root / "projects" / "proj" / "design" / "specs" / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(
        codecs.BOM_UTF8
        + f"---\nslug: {slug}\ntitle: {slug} t\ncurrent_state: {state}\n---\n\nbody\n".encode()
    )
    return p


def _non_mapping_spec(root: Path, slug: str) -> Path:
    return _write(root / "projects" / "proj" / "design" / "specs" / f"{slug}.md", "---\n- a\n- b\n---\n\nb\n")


def _unterminated_spec(root: Path, slug: str) -> Path:
    return _write(
        root / "projects" / "proj" / "design" / "specs" / f"{slug}.md",
        f"---\nslug: {slug}\ncurrent_state: done\n\nno closing fence, so no block\n",
    )


@pytest.mark.parametrize(
    "make_broken, label",
    [
        (_bom_spec, "bom-before-frontmatter"),
        (_non_mapping_spec, "non-mapping-frontmatter"),
        (_unterminated_spec, "unterminated-block"),
    ],
)
def test_a_spec_that_OPENS_a_frontmatter_block_is_broken_not_frontmatterless(
    tmp_path: Path, make_broken, label: str
):
    """`SpecFormatError` covers two unlike files, and only the bytes tell them apart.

    A spec with genuinely no `---` block is an ordinary shape and must stay promotable. A spec
    that OPENS a block the parser then refused is broken - and reading it as "no frontmatter"
    hands back `current_state=None`, which is not terminal, so the plan sidecar promotes and
    issue #42 reopens through a file whose `current_state: done` any human can read. The BOM
    case is the live one on a Windows fleet: the byte is invisible and `Out-File` writes it.

    Asserted STALE and FRESH together because that asymmetry is the trap: the same file, the
    same corruption, and a disposition decided purely by mtime is not a safety property.
    """
    for stale in (True, False):
        root = tmp_path / ("stale" if stale else "fresh")
        broken = make_broken(root, "opaque")
        if stale:
            _age(broken)
        make_plan(root, "proj", "opaque", frontmatter="current_state: approved-for-autonomous\n")
        make_spec(root, "proj", "healthy", frontmatter="current_state: drafted\n")
        make_plan(root, "proj", "healthy", frontmatter="current_state: approved-for-autonomous\n")

        recs = survey_epics(root, PAST_CUTOFF, include_archived=True)
        assert "opaque" not in _by_slug(recs), (label, stale)
        assert {r.slug for r in selectable(recs)} == {"healthy"}, (label, stale)


def test_a_FRESH_spec_pass_1_REJECTED_cannot_be_promoted_by_its_plan(tmp_path: Path, caplog):
    """The half of the hole that needs no cutoff to reach - and the likelier half.

    Pass 1 logs and `continue`s on malformed YAML, so `existing is None` exactly as it is for a
    spec skipped on mtime, and the plan sidecar's `approved-for-autonomous` won unconditionally.
    A typo lives in a freshly-edited file, so this is the more reachable path of the two, and a
    fail-closed rule that held only past the cutoff would be a guarantee about mtimes rather
    than about specs.

    The record is dropped, not promoted, and pass 1's own skip line already names the file - so
    exactly one line is emitted for it, not two.
    """
    _write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "typo.md",
        '---\nslug: typo\ntitle: "unterminated\ncurrent_state: done\n---\n\nbody\n',
    )
    make_plan(tmp_path, "proj", "typo", frontmatter="current_state: approved-for-autonomous\n")
    make_spec(tmp_path, "proj", "healthy", frontmatter="current_state: drafted\n")
    make_plan(tmp_path, "proj", "healthy", frontmatter="current_state: approved-for-autonomous\n")

    with caplog.at_level("WARNING"):
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    assert "typo" not in _by_slug(recs)
    assert {r.slug for r in selectable(recs)} == {"healthy"}
    skips = [r for r in caplog.records if ", skipping:" in str(r.msg)]
    assert len(skips) == 1
    assert Path(str(skips[0].args[0])).name == "typo.md"


# --------------------------------------------------------------------------- #
# Undecodable bytes (issue #80)
#
# Every other broken-spec fixture in this file is broken at the PARSE layer or at the
# FILESYSTEM layer, and both of those the survey already survived. Nothing here was broken at
# the DECODE layer, and `_read_sidecar` caught only `OSError` - so one UTF-16 file, which is
# what a PowerShell `>` redirect writes on this fleet, aborted `survey_epics` outright and
# every lane's selection surface came back empty on an unattended wake.
#
# FRESHNESS IS HALF THE SHAPE and is why the existing coverage could not reach it: a spec past
# the cutoff never reaches `_read_sidecar` at all (pass 1 `continue`s first), and that stale
# path is where all the earlier exception-hardening went.
# --------------------------------------------------------------------------- #
def _undecodable(path: Path) -> Path:
    """Bytes that are not valid UTF-8: a UTF-16LE BOM and some high bytes behind it.

    What a `Out-File`-default redirect or a truncated transfer leaves behind. `write_bytes`
    rather than `_write` on purpose - `_write` encodes as UTF-8 and so cannot express this.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe---\x00\x81\x8f\xfe\xff title: bad\n")
    return path


@pytest.mark.parametrize("bucket", ["specs", "plans"])
def test_an_undecodable_file_does_not_take_down_the_whole_survey(tmp_path: Path, bucket: str):
    """THE decisive one: the good records still come back.

    Not a wrong count before this fix - no count. The failure was a `UnicodeDecodeError`
    propagating out of `survey_epics`, so the board reported nothing selectable while the
    backlog was full.

    Parametrized over both buckets because `_read_sidecar` has TWO call sites, one per pass,
    and both sit above their own per-file `try`. A fix that closed only the spec pass would
    leave the plan pass crashing on the identical byte.
    """
    make_spec(tmp_path, "proj", "good-a", frontmatter="current_state: drafted\n")
    make_spec(tmp_path, "proj", "good-b", frontmatter="current_state: drafted\n")
    _undecodable(tmp_path / "projects" / "proj" / "design" / bucket / "bad.md")

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert sorted(_by_slug(recs)) == ["good-a", "good-b"], bucket


@pytest.mark.parametrize(
    "bucket, expected_kind",
    [("specs", "malformed spec file"), ("plans", "malformed plan file")],
)
def test_an_undecodable_file_is_COUNTED_not_silently_dropped(
    tmp_path: Path, caplog, bucket: str, expected_kind: str
):
    """Skip AND warn AND count - the module's own third path, not a bare skip.

    A bare skip would re-enter issue #122's class from a new door: a record the survey cannot
    see reads as unsatisfied and strands its dependents, silently. It does not, because the
    widened reader hands the same bytes to the full parser one line later and that lands in the
    existing skip channel.

    The log SHAPE is asserted, not merely the presence of a warning, for the reason the stale
    case already gives: the fleet report's skip handler reads these records structurally (the
    `, skipping:` template marker plus exactly two args), so a message saying the same thing in
    a different shape is counted as ZERO skips - invisibility wearing the channel's clothes.
    """
    make_spec(tmp_path, "proj", "healthy", frontmatter="current_state: drafted\n")
    bad = _undecodable(tmp_path / "projects" / "proj" / "design" / bucket / "bad.md")

    with caplog.at_level("WARNING"):
        survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    skips = [r for r in caplog.records if ", skipping:" in str(r.msg)]
    assert len(skips) == 1, bucket
    assert str(skips[0].msg).startswith(expected_kind), bucket
    assert isinstance(skips[0].args, tuple) and len(skips[0].args) == 2, bucket
    assert Path(str(skips[0].args[0])) == bad, bucket
    assert "codec" in str(skips[0].args[1]), bucket


def test_an_undecodable_spec_cannot_be_promoted_by_its_plan_sidecar(tmp_path: Path):
    """Fail CLOSED, so surviving the crash does not open a promotion hole instead.

    The dropped spec leaves `existing is None` in the plan pass, which is precisely the state
    an `approved-for-autonomous` sidecar used to promote unconditionally (issue #42). It does
    not here: pass 1 registered the file in `broken_spec_states`, so the plan is frozen and the
    record is dropped rather than offered.
    """
    _undecodable(tmp_path / "projects" / "proj" / "design" / "specs" / "shipped.md")
    make_plan(tmp_path, "proj", "shipped", frontmatter="current_state: approved-for-autonomous\n")
    make_spec(tmp_path, "proj", "healthy", frontmatter="current_state: drafted\n")
    make_plan(tmp_path, "proj", "healthy", frontmatter="current_state: approved-for-autonomous\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert "shipped" not in _by_slug(recs)
    assert {r.slug for r in selectable(recs)} == {"healthy"}


def test_an_undecodable_spec_with_a_NON_approved_plan_still_yields_a_record(tmp_path: Path):
    """The over-reach negative: the drop is scoped by approval, not by the bad byte.

    Freezing on `_is_approved` is what makes fail-closed cheap - an unapproved plan was never
    dispatchable anyway, so its record survives at the plan's own state. Deleting in-flight
    epics because an unrelated file has a bad byte would be the stranding failure again,
    pointing the other way.
    """
    _undecodable(tmp_path / "projects" / "proj" / "design" / "specs" / "inflight.md")
    make_plan(tmp_path, "proj", "inflight", frontmatter="current_state: drafted\n")

    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF, include_archived=True))
    assert recs["inflight"].current_state == "drafted"
    assert selectable(list(recs.values())) == []


def test_an_undecodable_neighbour_does_not_cost_a_GOOD_spec_its_sidecar_fields(tmp_path: Path):
    """The control: the widened `except` did not spread onto healthy files.

    `_read_sidecar` returns a sentinel rather than reporting, so a handler that caught too much
    would not crash and would not log - it would quietly serve `UNSET_PRIORITY` with no domain
    and no approver, which is a survey returning a confidently wrong answer. Assert the
    neighbour's fields survive intact beside the bad file.
    """
    make_spec(
        tmp_path, "proj", "good",
        frontmatter="current_state: drafted\npriority: 3\ndomain: infra\nbuild_approved_by: a human\n",
    )
    _undecodable(tmp_path / "projects" / "proj" / "design" / "specs" / "bad.md")

    rec = _by_slug(survey_epics(tmp_path, PAST_CUTOFF, include_archived=True))["good"]
    assert rec.priority == 3.0
    assert rec.domain == "infra"
    assert rec.build_approved_by == "a human"


@pytest.mark.parametrize(
    "payload, parses, label",
    [
        (b"---\nslug: p\npriority: 3\n---\n\nbody\n", True, "clean-utf8"),
        (codecs.BOM_UTF8 + b"---\nslug: p\npriority: 3\n---\n\nbody\n", False, "utf8-bom"),
        (b"\xff\xfe---\x00\x81\x8f\xfe\xff title: bad\n", False, "utf16-bom"),
        (b"---\nslug: p\npriority: 3\n---\n\nbody \x9a here\n", False, "bad-byte-in-body"),
    ],
)
def test_the_sidecar_reader_and_the_frontmatter_parser_fail_on_the_SAME_bytes(
    tmp_path: Path, payload: bytes, parses: bool, label: str
):
    """The invariant that makes the sentinel safe, pinned so the tempting "fix" cannot land.

    `_read_sidecar` returns a bare sentinel on undecodable bytes and reports nothing; the file
    is only named because `parse_spec_frontmatter` is handed the SAME bytes one line later and
    raises into the skip channel. Both readers must therefore fail on exactly the same inputs.

    Decoding either one leniently would break that and ship a worse bug than #80: with
    `errors="replace"` a UTF-16 spec reading `current_state: done` decodes to mojibake, misses
    the frontmatter regex, and - because `_opens_a_frontmatter_block` strips only the UTF-8 BOM
    and not `\\xff\\xfe` - fails OPEN as a frontmatter-less `speccing` record, which the plan
    sidecar then promotes. That is issue #42, reopened by merged work, entered through the
    door this fix opens. Keep both readers strict.
    """
    p = tmp_path / "projects" / "proj" / "design" / "specs" / "p.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)

    sentinel = (UNSET_PRIORITY, None, None, None, None)
    read_ok = _survey._read_sidecar(p) != sentinel
    try:
        parse_spec_frontmatter(p)
        parse_ok = True
    except Exception:  # noqa: BLE001 — any failure is a failure for this comparison
        parse_ok = False

    assert parse_ok is parses, label
    # An IMPLICATION, not an equality, and deliberately the weaker of the two: the sentinel is
    # also `_read_sidecar`'s ordinary answer for a perfectly good spec that simply declares
    # none of these five fields, so the reader may return it where the parser succeeds. What
    # must never happen is the other direction - a sidecar read that SUCCEEDS on bytes the
    # parser will refuse, because then nothing re-raises and nothing is ever logged.
    if not parse_ok:
        assert not read_ok, f"the sidecar reader accepted bytes the parser refused: {label}"


def test_a_FRESH_spec_with_no_frontmatter_at_all_is_still_a_promotable_record(tmp_path: Path):
    """The negative that keeps the byte test from swallowing an ordinary corpus shape.

    Pass 1 has always accepted a frontmatter-less spec as a `speccing` record with the slug
    derived from its filename. Treating THAT as broken would fail closed on ordinary prose and
    strand real work - which is the same stranding failure as the bug, pointing the other way.
    """
    _write(tmp_path / "projects" / "proj" / "design" / "specs" / "prose.md", "just prose, no yaml\n")
    make_plan(tmp_path, "proj", "prose", frontmatter="current_state: approved-for-autonomous\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert _by_slug(recs)["prose"].current_state == "approved-for-autonomous"
    assert {r.slug for r in selectable(recs)} == {"prose"}


def test_a_plan_that_was_never_approved_survives_an_unreadable_spec(tmp_path: Path):
    """FAIL CLOSED ONLY WHERE FAILING CLOSED BUYS SOMETHING.

    The record has no spec behind it, so its `current_state` is the plan's own - which makes
    "is the plan approved" exactly "would this record have passed `dispatchable()`'s approval
    gate". A plan sitting at `planning` was never claimable, so dropping it buys no gate safety
    whatsoever and merely deletes an in-flight epic from the board because an unrelated file
    has a typo. That is the stranding cost recorded against the option this fix was chosen
    over, incurred in exactly the cases where the benefit is nil.
    """
    _age(_write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "wip.md",
        '---\nslug: wip\ntitle: "unterminated\ncurrent_state: done\n---\n\nbody\n',
    ))
    make_plan(tmp_path, "proj", "wip", frontmatter="current_state: planning\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert _by_slug(recs)["wip"].current_state == "planning"   # still on the board
    assert selectable(recs) == []                              # and still not claimable


def test_a_broken_spec_is_reported_ONCE_not_once_per_plan_sidecar(tmp_path: Path, caplog):
    """`counted N, skipped M` counts LINES, so one broken file named twice reads as two.

    Two plan sidecars can declare the same slug (one date-prefixed, one not). The drop decision
    is per plan record, but the thing being reported broken is one spec file.
    """
    broken = _age(_write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "ws.md",
        '---\nslug: ws\ntitle: "unterminated\ncurrent_state: done\n---\n\nbody\n',
    ))
    make_plan(tmp_path, "proj", "ws", frontmatter="current_state: approved-for-autonomous\n")
    _write(
        tmp_path / "projects" / "proj" / "design" / "plans" / "2026-01-01-ws.md",
        "---\nepic: ws epic\nspec: /s/ws.md\nslug: ws\nphases: [phase-1]\ntasks:\n  1:\n"
        "    phase: phase-1\n    deps: []\ntest_commands: {}\n"
        "current_state: approved-for-autonomous\n---\n\nplan body\n",
    )

    with caplog.at_level("WARNING"):
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    assert "ws" not in _by_slug(recs)
    skips = [r for r in caplog.records if ", skipping:" in str(r.msg)]
    assert len(skips) == 1
    assert Path(str(skips[0].args[0])) == broken


def test_only_the_asked_spec_is_opened_when_its_own_filename_answers(
    tmp_path: Path, monkeypatch
):
    """THE COST CLAIM, ON A FIXTURE THAT CAN ACTUALLY FAIL IT.

    A read-count fixture with as many asking plans as stale specs cannot distinguish
    "read what was asked for" from "read everything": both produce the same number. So the
    populations are deliberately unequal - three stale specs, one asking plan - which is the
    shape the cutoff exists to bound, and the shape an eager index answers with three reads.

    One read, at every corpus size, whenever the skipped file whose NAME resolves to the asked
    slug settles the question. That covers the shape this fix is for: a shipped spec being
    frozen by its own sidecar.
    """
    seen = _count_stale_reads(monkeypatch)
    asked = _age(make_spec(tmp_path, "proj", "old-a", frontmatter="current_state: done\n"))
    _age(make_spec(tmp_path, "proj", "old-b", frontmatter="current_state: done\n"))
    _age(make_spec(tmp_path, "proj", "old-c", frontmatter="current_state: done\n"))
    make_plan(tmp_path, "proj", "old-a", frontmatter="current_state: approved-for-autonomous\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert _by_slug(recs)["old-a"].current_state == "done"
    assert selectable(recs) == []
    assert seen == [asked]


def test_no_skipped_spec_is_opened_twice_however_many_plans_ask(tmp_path: Path, monkeypatch):
    """The whole skipped set costs at most one read APIECE, per survey - never per ask.

    A slug that no filename resolves to still has to be looked for among the skipped specs,
    because a spec may declare a slug its filename does not (25 of the 462 in the live corpus
    do) - that look is what makes the renamed case above work at all. Two ways it could
    multiply, both fixture'd here: `old-a` is answered by its own filename but NON-blockingly,
    so the fallback look runs anyway over a file it just opened; and four more plan-only
    records ask afterwards.

    Two reads, for two skipped specs, against five asks.
    """
    seen = _count_stale_reads(monkeypatch)
    _age(make_spec(tmp_path, "proj", "old-a", frontmatter="current_state: drafted\n"))
    _age(make_spec(tmp_path, "proj", "old-b", frontmatter="current_state: drafted\n"))
    make_plan(tmp_path, "proj", "old-a", frontmatter=APPROVED)
    for i in range(4):
        make_plan(tmp_path, "proj", f"orphan-{i}", frontmatter=APPROVED)

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    # The non-terminal stale spec still yields to its plan - the read bought an answer, not a
    # freeze - so this is not passing because nothing was consulted.
    assert _by_slug(recs)["old-a"].current_state == APPROVED_CURRENT_STATE
    assert sorted(seen) == sorted(set(seen))
    assert len(seen) == 2


def test_a_BLOCKING_stale_spec_beats_a_colliding_non_blocking_one(tmp_path: Path):
    """Collision among SKIPPED specs resolves the way pass 1 resolves it among fresh ones.

    `_merge_spec_record` keeps the more-progressed record, so had both files been fresh the
    terminal one would have become the record and frozen the plan. Answering from whichever
    file happens to be named after the slug would make stale and fresh disagree - in the
    reopening direction, which is the whole bug - so the cheap filename answer is trusted only
    when it BLOCKS, and a non-blocking one still costs the look it saves.
    """
    _age(make_spec(tmp_path, "proj", "coll", frontmatter="current_state: drafted\n"))
    _age(_write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "2019-01-01-other.md",
        "---\nslug: coll\ntitle: other\ncurrent_state: done\n---\n\nbody\n",
    ))
    make_plan(tmp_path, "proj", "coll", frontmatter="current_state: approved-for-autonomous\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert _by_slug(recs)["coll"].current_state == "done"
    assert selectable(recs) == []


def test_a_status_board_still_wins_over_a_frozen_stale_spec(tmp_path: Path):
    """Pass 3 is unchanged: status.md is filesystem evidence and always wins.

    Worth pinning because the freeze now writes a `lifecycle_state` for a record with no spec
    behind it, and a freeze that leaked into pass 3 would invert the precedence: a status board
    is filesystem evidence and outranks frontmatter when both are present.

    This docstring used to justify itself with "the module's oldest rule - completion truth is
    the status board, never frontmatter". That rule is not what the code does (gh#319): with no
    board, a terminal frontmatter sets `done` on its own. The PRECEDENCE this test pins is real
    and unchanged; only the absolute it was leaning on was false.
    """
    _age(make_spec(tmp_path, "proj", "boarded", frontmatter="current_state: done\n"))
    make_plan(tmp_path, "proj", "boarded", frontmatter="current_state: approved-for-autonomous\n")
    make_status(tmp_path, "proj", "boarded", status="running")

    rec = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))["boarded"]
    assert rec.lifecycle_state == "running"
    assert rec.current_state == "done"


# --------------------------------------------------------------------------- #
# A FILE THAT VANISHES UNDER THE SURVEY COSTS THAT FILE, NOT THE BOARD (gh#361).
#
# Every pass used to stat each file TWICE: once at the loop head for the cutoff, and again
# inside the record via `_file_mtime_iso`. A file deleted or renamed by another lane between
# those two points raised `FileNotFoundError` straight out of `survey_epics`. Every seat calls
# this at every wake, so one lane's ordinary cleanup blanked the whole board - the same total
# failure the undecodable-byte case above produced, reached through the filesystem instead.
#
# TWO WINDOWS, TWO DIFFERENT FIXES, and the tests are split the same way. The record's own
# stat is GONE - it reuses the loop-head value - so the reported race cannot be observed at
# all any more, which is why the first test asserts the vanished spec still becomes a record.
# The glob-then-head-stat window remains real and is caught, so the rest assert the skip.
# --------------------------------------------------------------------------- #
def _vanish_after_first_read(monkeypatch, target: Path) -> None:
    """Serve *target*'s bytes, delete it, then keep serving the bytes already in hand.

    The reported race, precisely: every read pass 1 makes SUCCEEDS and the file is gone by the
    time the record is built. Failing the later reads instead would land in the parse handler -
    a different arm, already covered, and one the survey always survived.
    """
    real = Path.read_text
    cached: dict[str, str] = {}

    def patched(self, *args, **kwargs):
        if self != target:
            return real(self, *args, **kwargs)
        if str(target) not in cached:
            cached[str(target)] = real(self, *args, **kwargs)
            target.unlink()
        return cached[str(target)]

    monkeypatch.setattr(Path, "read_text", patched)


def _vanish_on_stat(monkeypatch, target: Path) -> None:
    """Make *target* look deleted to every `Path.stat`, and nothing else.

    Narrowed to the one path because the walkers stat their way through the tree; a blanket
    raise would test the fixture, not the survey.
    """
    real = Path.stat

    def patched(self, *args, **kwargs):
        if self == target:
            raise FileNotFoundError(2, "No such file or directory", str(target))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", patched)


@pytest.mark.parametrize("bucket", ["spec", "plan", "status"])
def test_a_file_deleted_mid_read_does_not_take_down_the_whole_survey(
    tmp_path: Path, monkeypatch, bucket: str
):
    """THE reported crash. Pre-fix this raises `FileNotFoundError` out of `survey_epics`.

    The vanished file is asserted to still produce its RECORD, not a skip, because the fix is
    by construction: the record carries the loop-head mtime, so everything it needs was already
    in hand before the file went away and there is nothing left to fail on. A fix that caught
    the error instead would drop the record and still pass a "survey returns" assertion while
    quietly losing an epic.

    Parametrized over all three passes because each built its record with its own second stat.
    """
    make_spec(tmp_path, "proj", "good-a", frontmatter="current_state: drafted\n")
    make_spec(tmp_path, "proj", "good-b", frontmatter="current_state: drafted\n")
    if bucket == "spec":
        ghost = make_spec(tmp_path, "proj", "ghost", frontmatter="current_state: drafted\n")
    elif bucket == "plan":
        ghost = make_plan(tmp_path, "proj", "ghost")
    else:
        ghost = make_status(tmp_path, "proj", "ghost", status="running")
    _vanish_after_first_read(monkeypatch, ghost)

    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF, include_archived=True))

    assert not ghost.exists(), bucket
    assert sorted(recs) == ["ghost", "good-a", "good-b"], bucket
    assert recs["ghost"].last_modified, bucket  # the head-stat value, not a second look


@pytest.mark.parametrize("bucket", ["spec", "plan", "status"])
def test_a_file_that_vanishes_before_its_head_stat_is_skipped_and_reported(
    tmp_path: Path, caplog, monkeypatch, bucket: str
):
    """The remaining window - glob/walk, then stat - costs one file and says so.

    Parametrized over all three passes because each has its own head stat; a guard on the spec
    pass alone leaves the other two crashing on the identical missing file.

    The log SHAPE is asserted for the reason the undecodable case already gives: the fleet
    report's skip handler reads these records structurally (the `, skipping:` marker plus
    exactly two args), so a message saying the same thing in a different shape counts as ZERO
    skips. The wording is shared across the three passes so one grep finds them.
    """
    make_spec(tmp_path, "proj", "survivor", frontmatter="current_state: drafted\n")
    if bucket == "spec":
        ghost = make_spec(tmp_path, "proj", "ghost", frontmatter="current_state: drafted\n")
    elif bucket == "plan":
        ghost = make_plan(tmp_path, "proj", "ghost")
    else:
        ghost = make_status(tmp_path, "proj", "ghost", status="running")
    _vanish_on_stat(monkeypatch, ghost)

    with caplog.at_level("WARNING"):
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    assert "survivor" in _by_slug(recs), bucket
    assert "ghost" not in _by_slug(recs), bucket
    skips = [r for r in caplog.records if ", skipping:" in str(r.msg)]
    assert len(skips) == 1, bucket
    assert str(skips[0].msg).startswith("vanished before stat"), bucket
    assert isinstance(skips[0].args, tuple) and len(skips[0].args) == 2, bucket
    assert Path(str(skips[0].args[0])) == ghost, bucket


def test_a_vanished_spec_cannot_be_promoted_by_its_plan_sidecar(tmp_path: Path, monkeypatch):
    """Surviving the crash must not open a promotion hole instead (issue #42's shape).

    A spec that never becomes a record leaves `existing is None` in the plan pass, and that is
    exactly the state an `approved-for-autonomous` sidecar promotes UNCONDITIONALLY. Every
    other "spec skipped" branch registers the slug through `_record_broken_spec` for that
    reason, and a vanished spec is the same population - so the guard that stops the crash has
    to register too, or it trades a loud crash for a silent re-ship of merged work.
    """
    ghost = make_spec(tmp_path, "proj", "shipped", frontmatter="current_state: done\n")
    make_plan(tmp_path, "proj", "shipped", frontmatter=APPROVED)
    make_spec(tmp_path, "proj", "healthy", frontmatter="current_state: drafted\n")
    make_plan(tmp_path, "proj", "healthy", frontmatter=APPROVED)
    _vanish_on_stat(monkeypatch, ghost)

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    shipped = _by_slug(recs).get("shipped")
    assert shipped is None or shipped.current_state != APPROVED_CURRENT_STATE
    assert {r.slug for r in selectable(recs)} == {"healthy"}


# --------------------------------------------------------------------------- #
# Status filename conventions + epic: gate + SKIP_DIRS
# --------------------------------------------------------------------------- #
def test_both_status_filename_conventions(tmp_path: Path):
    make_status(tmp_path, "proj", "filestem", status="running")            # <slug>.status.md
    make_status(tmp_path, "proj", "dirnamed", status="done", done=1, total=1, dir_named=True)  # status.md
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["filestem"].lifecycle_state == "running"
    assert recs["dirnamed"].lifecycle_state == "done"


def test_status_without_epic_signature_ignored(tmp_path: Path):
    make_status(tmp_path, "proj", "notreal", dir_named=True, epic_signature=False)
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert "notreal" not in recs


def test_is_orchestrator_status_gate(tmp_path: Path):
    good = make_status(tmp_path, "proj", "good", dir_named=True)
    bad = _write(tmp_path / "misc" / "status.md", "not frontmatter at all\n")
    assert is_orchestrator_status(good) is True
    assert is_orchestrator_status(bad) is False


def test_skip_dirs_not_descended(tmp_path: Path):
    # A status file buried inside a SKIP_DIRS directory must not be discovered.
    buried = (
        tmp_path / "projects" / "proj" / "status" / "node_modules" / "pkg" / "buried.status.md"
    )
    _write(
        buried,
        "---\nepic: buried epic\nplan: /p.md\nstarted: 2026-01-01T00:00:00+00:00\n"
        "status: running\ncurrent_phase: phase-1\n---\n",
    )
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert "buried" not in recs


# --------------------------------------------------------------------------- #
# Dep math helpers
# --------------------------------------------------------------------------- #
def test_blocks_count_inversion(tmp_path: Path):
    make_spec(tmp_path, "proj", "core")
    make_spec(tmp_path, "proj", "b", frontmatter="depends_on: [core]\n")
    make_spec(tmp_path, "proj", "c", frontmatter="depends_on: [core]\n")
    recs = survey_epics(tmp_path, PAST_CUTOFF)
    core = _by_slug(recs)["core"]
    assert blocks_count(core, recs) == 2


def test_ready_set(tmp_path: Path):
    make_plan(tmp_path, "proj", "dep")
    make_status(tmp_path, "proj", "dep", status="done", done=1, total=1)  # done
    make_spec(tmp_path, "proj", "ready", frontmatter="depends_on: [dep]\n")
    make_spec(tmp_path, "proj", "notready", frontmatter="depends_on: [missing]\n")
    recs = survey_epics(tmp_path, PAST_CUTOFF)
    ready_slugs = {r.slug for r in ready_set(recs)}
    assert "ready" in ready_slugs
    assert "notready" not in ready_slugs


def test_blocked_status_buckets(tmp_path: Path):
    # done dep, running dep, candidate (speccing) dep, and an unknown dep.
    make_plan(tmp_path, "proj", "donedep")
    make_status(tmp_path, "proj", "donedep", status="done", done=1, total=1)
    make_plan(tmp_path, "proj", "rundep")
    make_status(tmp_path, "proj", "rundep", status="running")
    make_spec(tmp_path, "proj", "canddep")  # speccing

    make_spec(tmp_path, "proj", "isready", frontmatter="depends_on: [donedep]\n")
    make_spec(tmp_path, "proj", "isnear", frontmatter="depends_on: [rundep]\n")
    make_spec(tmp_path, "proj", "ischained", frontmatter="depends_on: [canddep]\n")
    make_spec(tmp_path, "proj", "ismissing", frontmatter="depends_on: [ghost]\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF)
    idx = _by_slug(recs)
    assert blocked_status(idx["isready"], recs) == "READY"
    assert blocked_status(idx["isnear"], recs) == "NEAR-READY"
    assert blocked_status(idx["ischained"], recs) == "CHAINED"
    assert blocked_status(idx["ismissing"], recs) == "MISSING-DEP"


# --------------------------------------------------------------------------- #
# Config-parameterized patterns (Phase-1 re-point seam)
# --------------------------------------------------------------------------- #
def test_patterns_are_config_repointable(tmp_path: Path):
    from agentflow.epic_survey import SurveyPatterns

    # A wholly different on-disk layout, reachable only by re-pointing patterns
    # (no code change) — the Phase-1/Phase-2 seam.
    _write(tmp_path / "myspecs" / "flat.md", "---\nslug: flat\ntitle: Flat\n---\nbody\n")
    custom = SurveyPatterns(spec_globs=("myspecs/*.md",), plan_globs=(), status_dir_globs=())
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF, patterns=custom))
    assert recs["flat"].lifecycle_state == "speccing"
    # Default patterns would not see the flat layout.
    assert survey_epics(tmp_path, PAST_CUTOFF) == []


# --------------------------------------------------------------------------- #
# current_state: the standing human approval, enforced in dispatchable() (issue #40)
#
# The primary filter. Before this it was a sentence in lead-dev/SKILL.md, at one call site,
# with the juniors trusting what lead-dev published and nothing downstream re-checking -- so
# the published selection expression returned a `superseded` epic as its top pick.
# --------------------------------------------------------------------------- #
def test_a_drafted_epic_is_not_dispatchable(tmp_path: Path):
    # `drafted` is the single largest non-approved population in the corpus: work that has
    # been written up but that no human has greenlit for autonomous build.
    make_spec(tmp_path, "proj", "a_drafted", frontmatter="current_state: drafted\n")
    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert _by_slug(recs)["a_drafted"].current_state == "drafted"  # survey still shows it
    assert dispatchable(recs) == []                                # dispatch does not


@pytest.mark.parametrize("terminal", sorted(TERMINAL_CURRENT_STATES))
def test_no_terminal_state_is_dispatchable(tmp_path: Path, terminal: str):
    """Every TERMINAL_CURRENT_STATES value is undispatchable, as a consequence of the
    approval rule rather than a second rule of its own -- none of them IS the approved value.

    `include_archived=True` because an `archived` spec is otherwise dropped by the survey
    before dispatchable() ever sees it; the point here is that the filter holds even when
    the record does reach it.
    """
    make_spec(tmp_path, "proj", "t_term", frontmatter=f"current_state: {terminal}\n")
    make_spec(tmp_path, "proj", "t_live", frontmatter=APPROVED)
    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert _by_slug(recs)["t_term"].current_state == terminal, "fixture must reach dispatchable"
    assert [r.slug for r in dispatchable(recs)] == ["t_live"]


def test_an_approved_epic_is_dispatchable(tmp_path: Path):
    make_spec(tmp_path, "proj", "a_ok", frontmatter=APPROVED)
    assert APPROVED_CURRENT_STATE == "approved-for-autonomous"
    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert [r.slug for r in dispatchable(recs)] == ["a_ok"]


def test_approval_match_tolerates_casing_and_whitespace(tmp_path: Path):
    """Same tolerance `_is_terminal` has, pointing the other way.

    An exact-match test would read a hand-typed `  Approved-For-Autonomous  ` as unapproved
    and silently strand work a human did approve -- a failure nobody would think to look for,
    because the frontmatter says the right thing.
    """
    make_spec(tmp_path, "proj", "a_loud",
              frontmatter='current_state: "  Approved-For-Autonomous  "\n')
    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert [r.slug for r in dispatchable(recs)] == ["a_loud"]


def test_current_state_absent_entirely_is_not_dispatchable(tmp_path: Path):
    """The deliberate asymmetry with `project:` (see APPROVED_CURRENT_STATE).

    An unset `project:` still dispatches, because the field postdates the earliest specs and
    its absence is legitimately unknown. Approval is not a field a spec can predate: unset
    is not approved. Both halves are asserted here so the pair cannot drift apart.
    """
    make_spec(tmp_path, "proj", "a_bare")                       # no frontmatter fields at all
    make_spec(tmp_path, "proj", "a_approved", frontmatter=APPROVED)
    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert _by_slug(recs)["a_bare"].current_state is None
    assert _by_slug(recs)["a_bare"].project is None
    assert [r.slug for r in dispatchable(recs)] == ["a_approved"]


@pytest.mark.parametrize("raw", ["yes", "true", "2026-07-24"])
def test_a_non_string_current_state_is_undispatchable_without_raising(tmp_path: Path, raw: str):
    """YAML 1.1 turns a bare `yes` / `true` into a bool and `2026-07-24` into a date.

    `current_state` reaches the record uncoerced, so the approval predicate has to reuse
    `_norm_state`'s tolerance rather than call a str method: raising here would take out the
    whole selection, and coercing with `str()` would be worse still -- `str(True)` is a
    perfectly good non-approval, but the habit is how a value ends up matching by accident.
    """
    make_spec(tmp_path, "proj", "a_odd", frontmatter=f"current_state: {raw}\n")
    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert not isinstance(_by_slug(recs)["a_odd"].current_state, str)
    assert dispatchable(recs) == []


def test_a_non_approved_drop_is_silent_while_a_foreign_project_drop_still_warns(
    tmp_path: Path, caplog
):
    """Pins the no-warning decision, which is a judgement call and therefore fixable-away.

    A foreign-project record signals a MISFILING that a human has to correct, so it warns.
    A non-approved record is the normal case and the overwhelming majority of the corpus
    (325 of 329 when this filter was written); warning per drop would bury the foreign line
    under noise and train the reader to skip the channel. If someone "improves" this by
    warning on every unapproved record, this test is what stops them.
    """
    make_spec(tmp_path, "alpha", "s_drafted", frontmatter="current_state: drafted\n")
    make_spec(tmp_path, "alpha", "s_done", frontmatter="current_state: done\n")
    quiet = survey_epics(tmp_path, PAST_CUTOFF, project="alpha")
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert dispatchable(quiet, (), project="alpha") == []
    assert [r.getMessage() for r in caplog.records] == []

    # Same call, one misfiled record added: that one still gets its line.
    make_spec(tmp_path, "alpha", "s_foreign", frontmatter=APPROVED + "project: beta\n")
    loud = survey_epics(tmp_path, PAST_CUTOFF, project="alpha")
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert dispatchable(loud, (), project="alpha") == []
    assert any("another project" in m for m in (r.getMessage() for r in caplog.records))


# --------------------------------------------------------------------------- #
# queue_class: survey parses it; dispatchable() filters non-buildable classes
# --------------------------------------------------------------------------- #
def test_queue_class_parsed_from_frontmatter(tmp_path: Path):
    make_spec(tmp_path, "proj", "tagged", frontmatter="queue_class: local-tooling\n")
    make_spec(tmp_path, "proj", "untagged")
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert recs["tagged"].queue_class == "local-tooling"
    assert recs["untagged"].queue_class is None


def test_dispatchable_drops_non_buildable_classes(tmp_path: Path):
    # All five are APPROVED, so queue_class is the only thing separating them.
    make_spec(tmp_path, "proj", "d_unset", frontmatter=APPROVED)
    make_spec(tmp_path, "proj", "d_buildable",
              frontmatter=APPROVED + "queue_class: fleet-buildable\n")
    make_spec(tmp_path, "proj", "d_local", frontmatter=APPROVED + "queue_class: local-tooling\n")
    make_spec(tmp_path, "proj", "d_server", frontmatter=APPROVED + "queue_class: on-server-ops\n")
    make_spec(tmp_path, "proj", "d_brainstorm",
              frontmatter=APPROVED + "queue_class: brainstorm-pin\n")
    records = survey_epics(tmp_path, PAST_CUTOFF)
    # survey exposes ALL classes (dev-manager sees everything)
    assert len(records) == 5
    keep = sorted(r.slug for r in dispatchable(records))
    assert keep == ["d_buildable", "d_unset"]
    assert NON_DISPATCHABLE_QUEUE_CLASSES == {"local-tooling", "on-server-ops", "brainstorm-pin"}


# excluded_domains: dispatchable() drops excluded domain/project unless the epic carries build_approved_by
def test_dispatchable_drops_excluded_domains_unless_approved(tmp_path: Path):
    # All four are APPROVED, so the exclusion/grant logic is the only thing under test.
    make_spec(tmp_path, "proj", "d_sibling", frontmatter=APPROVED + "domain: sibling-program\n")
    make_spec(tmp_path, "proj", "d_sibling_project",
              frontmatter=APPROVED + "project: sibling-program\n")
    make_spec(tmp_path, "proj", "d_granted",
              frontmatter=APPROVED
              + 'domain: sibling-program\nbuild_approved_by: "the person 2026-08-18"\n')
    make_spec(tmp_path, "proj", "d_plain", frontmatter=APPROVED)
    recs = {r.slug: r for r in survey_epics(tmp_path, PAST_CUTOFF)}
    assert recs["d_granted"].build_approved_by == "the person 2026-08-18"
    assert recs["d_plain"].build_approved_by is None
    kept = {r.slug for r in dispatchable(list(recs.values()), ["sibling-program"])}
    assert kept == {"d_granted", "d_plain"}
    # no exclusion configured = behavior unchanged
    assert {r.slug for r in dispatchable(list(recs.values()))} == set(recs)


def test_build_approved_by_blank_is_not_a_grant(tmp_path: Path):
    # APPROVED, so the empty [] below proves the blank grant failed to lift the domain ban
    # rather than proving the record was never approved in the first place.
    make_spec(tmp_path, "proj", "d_blank",
              frontmatter=APPROVED + 'domain: sibling-program\nbuild_approved_by: "  "\n')
    recs = {r.slug: r for r in survey_epics(tmp_path, PAST_CUTOFF)}
    assert recs["d_blank"].build_approved_by is None
    # Control: with no exclusion configured it DOES dispatch, so the [] below is the ban.
    assert [r.slug for r in dispatchable(list(recs.values()))] == ["d_blank"]
    assert dispatchable(list(recs.values()), ["sibling-program"]) == []


# --------------------------------------------------------------------------- #
# queue_class vocabulary: allow-list, not deny-list
# --------------------------------------------------------------------------- #
def test_dispatchable_queue_class_is_an_allow_list(tmp_path: Path):
    # Out-of-vocabulary values ('bug' / 'feature' — issue-tracker leakage) and
    # anything unrecognized are NOT dispatchable; only unset / fleet-buildable are.
    # All five APPROVED: queue_class is the only variable.
    make_spec(tmp_path, "proj", "q_unset", frontmatter=APPROVED)
    make_spec(tmp_path, "proj", "q_buildable",
              frontmatter=APPROVED + "queue_class: fleet-buildable\n")
    make_spec(tmp_path, "proj", "q_bug", frontmatter=APPROVED + "queue_class: bug\n")
    make_spec(tmp_path, "proj", "q_feature", frontmatter=APPROVED + "queue_class: feature\n")
    make_spec(tmp_path, "proj", "q_mystery",
              frontmatter=APPROVED + "queue_class: not-a-real-class\n")
    records = survey_epics(tmp_path, PAST_CUTOFF)
    # survey still exposes ALL of them (dev-manager sees everything)
    assert len(records) == 5
    assert sorted(r.slug for r in dispatchable(records)) == ["q_buildable", "q_unset"]
    assert DISPATCHABLE_QUEUE_CLASSES == {"fleet-buildable"}
    # the known non-dispatchable set stays, so the warning can tell "known and not
    # buildable" apart from "out of vocabulary"
    assert NON_DISPATCHABLE_QUEUE_CLASSES == {"local-tooling", "on-server-ops", "brainstorm-pin"}


def test_the_retired_queue_class_no_longer_dispatches(tmp_path: Path):
    """The hard cut, asserted as BEHAVIOR rather than as set membership.

    No dual-accept window was built (decision 2B), so a spec the migration has not reached
    yet is not merely deprecated - it is out of vocabulary, it warns, and it dispatches
    nothing. The retired spelling comes from `agentflow.vocabulary`, the one tracked place
    it is written; re-spelling it here would be the second place.
    """
    make_spec(tmp_path, "proj", "q_retired",
              frontmatter=APPROVED + f"queue_class: {RETIRED_QUEUE_CLASS}\n")
    # Control: an otherwise identical spec on the current value DOES dispatch, so the
    # empty result below is the retirement and not a broken fixture.
    make_spec(tmp_path, "proj", "q_current",
              frontmatter=APPROVED + f"queue_class: {CURRENT_QUEUE_CLASS}\n")
    records = survey_epics(tmp_path, PAST_CUTOFF)
    assert len(records) == 2
    assert [r.slug for r in dispatchable(records)] == ["q_current"]
    assert RETIRED_QUEUE_CLASS not in KNOWN_QUEUE_CLASSES


def test_survey_warns_once_per_out_of_vocabulary_queue_class(tmp_path: Path, caplog):
    make_spec(tmp_path, "proj", "w_one", frontmatter="queue_class: not-a-real-class\n")
    make_spec(tmp_path, "proj", "w_two", frontmatter="queue_class: not-a-real-class\n")
    make_spec(tmp_path, "proj", "w_bug", frontmatter="queue_class: bug\n")
    make_spec(tmp_path, "proj", "w_known", frontmatter="queue_class: local-tooling\n")
    make_spec(tmp_path, "proj", "w_unset")
    with caplog.at_level("WARNING"):
        records = survey_epics(tmp_path, PAST_CUTOFF)
    assert len(records) == 5  # a warning, never a raise — the survey still returns everything
    warned = [r.getMessage() for r in caplog.records if "queue_class" in r.getMessage()]
    assert len(warned) == 2  # one line per DISTINCT unknown value, not per record
    assert any("not-a-real-class" in m for m in warned)
    assert any("'bug'" in m for m in warned)
    # in-vocabulary values are silent
    assert not any("local-tooling" in m for m in warned)


# --------------------------------------------------------------------------- #
# Project scope — layer 1: glob narrowing (SurveyPatterns.for_project)
# --------------------------------------------------------------------------- #
def test_for_project_replaces_the_wildcard_project_segment():
    from agentflow.epic_survey import DEFAULT_SURVEY_PATTERNS, SurveyPatterns

    scoped = DEFAULT_SURVEY_PATTERNS.for_project("alpha")
    assert isinstance(scoped, SurveyPatterns)
    # Each authored family is the bucket root plus one glob per terminal partition. Scoping
    # substitutes the PROJECT segment only: the `done`/`archived` literals and the trailing
    # `*.md` are left alone, so a scoped survey still reaches reconciled records (gh#122).
    assert scoped.spec_globs == (
        "projects/alpha/design/specs/*.md",
        "projects/alpha/design/specs/done/*.md",
        "projects/alpha/design/specs/archived/*.md",
    )
    assert scoped.plan_globs == (
        "projects/alpha/design/plans/*.md",
        "projects/alpha/design/plans/done/*.md",
        "projects/alpha/design/plans/archived/*.md",
    )
    assert scoped.status_dir_globs == ("projects/alpha/status",)
    # the original is untouched (frozen config, single source of the layout)
    assert DEFAULT_SURVEY_PATTERNS.spec_globs[0] == "projects/*/design/specs/*.md"
    assert len(DEFAULT_SURVEY_PATTERNS.spec_globs) == 3


def test_for_project_rejects_a_name_that_is_not_one_path_segment():
    from agentflow.epic_survey import DEFAULT_SURVEY_PATTERNS

    for bad in ("", "   ", "alpha/beta", "alpha\\beta", "alph*"):
        with pytest.raises(ValueError):
            DEFAULT_SURVEY_PATTERNS.for_project(bad)


def test_for_project_warns_on_a_pattern_with_no_project_segment(caplog):
    from agentflow.epic_survey import SurveyPatterns

    custom = SurveyPatterns(spec_globs=("myspecs/*.md",), plan_globs=(), status_dir_globs=())
    with caplog.at_level("WARNING"):
        scoped = custom.for_project("alpha")
    assert scoped.spec_globs == ("myspecs/*.md",)  # unchanged — cannot be scoped
    assert any("project segment" in r.message for r in caplog.records)


def test_survey_project_scope_sees_only_its_own_records(tmp_path: Path):
    make_spec(tmp_path, "alpha", "a_spec")
    make_plan(tmp_path, "alpha", "a_plan")
    make_status(tmp_path, "alpha", "a_plan", status="running")
    make_spec(tmp_path, "beta", "b_spec")
    make_plan(tmp_path, "beta", "b_plan")
    make_status(tmp_path, "beta", "b_plan", status="running")

    alpha = sorted(r.slug for r in survey_epics(tmp_path, PAST_CUTOFF, project="alpha"))
    beta = sorted(r.slug for r in survey_epics(tmp_path, PAST_CUTOFF, project="beta"))
    assert alpha == ["a_plan", "a_spec"]
    assert beta == ["b_plan", "b_spec"]
    # default (None) is unchanged: the full, project-wide set dev-manager wants
    assert sorted(r.slug for r in survey_epics(tmp_path, PAST_CUTOFF)) == [
        "a_plan", "a_spec", "b_plan", "b_spec",
    ]


def test_survey_project_scope_composes_with_custom_patterns(tmp_path: Path):
    from agentflow.epic_survey import SurveyPatterns

    make_spec(tmp_path, "alpha", "a_only")
    make_spec(tmp_path, "beta", "b_only")
    custom = SurveyPatterns(
        spec_globs=("projects/*/design/specs/*.md",), plan_globs=(), status_dir_globs=()
    )
    recs = survey_epics(tmp_path, PAST_CUTOFF, patterns=custom, project="beta")
    assert [r.slug for r in recs] == ["b_only"]


# --------------------------------------------------------------------------- #
# Project scope — layer 2: record filter (dispatchable(..., project=...))
# --------------------------------------------------------------------------- #
def test_dispatchable_drops_a_foreign_project_record(tmp_path: Path):
    # A misfiled spec: it sits under projects/alpha/design/specs but declares another
    # project, so the glob scope alone would not catch it.
    make_spec(tmp_path, "alpha", "p_foreign", frontmatter=APPROVED + "project: beta\n")
    make_spec(tmp_path, "alpha", "p_own", frontmatter=APPROVED + "project: alpha\n")
    # Legacy spec: `project:` predates it, so unset still dispatches. `current_state` does
    # NOT get that treatment -- an unset approval is not an approval (issue #40).
    make_spec(tmp_path, "alpha", "p_unset", frontmatter=APPROVED)
    records = survey_epics(tmp_path, PAST_CUTOFF, project="alpha")
    assert len(records) == 3
    kept = sorted(r.slug for r in dispatchable(records, project="alpha"))
    assert kept == ["p_own", "p_unset"]
    # no project scope = today's behavior, everything stays
    assert len(dispatchable(records)) == 3


def test_dispatchable_project_is_keyword_only(tmp_path: Path):
    make_spec(tmp_path, "alpha", "k_own", frontmatter=APPROVED + "project: alpha\n")
    records = survey_epics(tmp_path, PAST_CUTOFF)
    # the existing positional form (records, excluded_domains) still works
    assert [r.slug for r in dispatchable(records, ())] == ["k_own"]
    assert dispatchable(records, ["alpha"]) == []
    with pytest.raises(TypeError):
        dispatchable(records, (), "alpha")  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Pass 3 must not drop the sidecar gates (dual-review HIGH, 2026-08-30)
# --------------------------------------------------------------------------- #
def test_status_board_does_not_strip_queue_class(tmp_path):
    """An epic with a status.md is exactly the in-flight class the audit was about.

    Pass 3 rebuilds the record from the status board; when it dropped queue_class
    the allow-list read the record as "unset" and dispatched it, so the rule could
    not reach any epic that had ever been dispatched.
    """
    make_spec(tmp_path, "alpha", "inflight-bug",
              frontmatter="queue_class: bug\ncurrent_state: approved-for-autonomous\n")
    make_status(tmp_path, "alpha", "inflight-bug")
    recs = survey_epics(tmp_path, PAST_CUTOFF, project="alpha")
    rec = _by_slug(recs)["inflight-bug"]
    assert rec.lifecycle_state == "running", "fixture must actually exercise pass 3"
    assert rec.queue_class == "bug"
    assert [r.slug for r in dispatchable(recs, (), project="alpha")] == []


def test_status_board_does_not_strip_build_approved_by(tmp_path):
    """Same drop, opposite direction: losing the grant fails CLOSED, silently
    un-approving an in-flight epic the human explicitly greenlit."""
    make_spec(tmp_path, "alpha", "inflight-grant",
              frontmatter=("domain: banned\ncurrent_state: approved-for-autonomous\n"
                           "build_approved_by: a human 2026-08-30\n"))
    make_status(tmp_path, "alpha", "inflight-grant")
    recs = survey_epics(tmp_path, PAST_CUTOFF, project="alpha")
    rec = _by_slug(recs)["inflight-grant"]
    assert rec.lifecycle_state == "running", "fixture must actually exercise pass 3"
    assert rec.build_approved_by == "a human 2026-08-30"
    assert [r.slug for r in dispatchable(recs, ("banned",), project="alpha")] == ["inflight-grant"]


# --------------------------------------------------------------------------- #
# selectable() — the composition seam (issue #40 review finding)
#
# Every test below exists because a two-call expression the skills transcribed by hand
# had an order that could not be inferred from the names and failed SILENTLY in the
# stranding direction. These pin the order, not just the parts.
# --------------------------------------------------------------------------- #
# EVERY finished state, DERIVED and not restated. Each of these is in `_done_slugs` and in
# `blocked_status`'s READY bucket on purpose. Every one of these tests originally used only
# `done`, which let a mutation narrowing `_done_slugs` to `done` alone survive the entire
# suite: an approved epic whose prerequisite was ARCHIVED would have been stranded forever,
# silently. Same defect class as the composition-order bug these tests exist to pin, reached
# through a different door.
#
# It was then sized to `("done", "archived")` - the same subset `_done_slugs` itself restated -
# so it guarded the vocabulary it was written against and not the vocabulary that shipped:
# `superseded` and `shipped` were terminal everywhere else in the module and unguarded here,
# which is exactly the gh#261 hole. A mutation guard that hand-copies the set it guards can
# only ever be as wide as the day it was written, so this reads the set instead.
FINISHED_STATES = tuple(sorted(TERMINAL_CURRENT_STATES))


def _dep_pair(dep_state: str = "done") -> list[EpicRecord]:
    """A finished prerequisite plus an approved epic that depends on it."""
    return [
        EpicRecord(slug="dep", title="d", lifecycle_state=dep_state, current_state=dep_state),
        EpicRecord(
            slug="kid", title="k", lifecycle_state="approved-for-autonomous",
            current_state=APPROVED_CURRENT_STATE, depends_on=["dep"],
        ),
    ]


@pytest.mark.parametrize("dep_state", FINISHED_STATES)
def test_selectable_keeps_an_approved_epic_whose_dependency_is_finished(dep_state: str):
    """The regression that made selectable() exist.

    An approved epic with a satisfied dep must be claimable. Under the old published
    expression it was not: dispatchable() drops the finished prerequisite, so ready_set()
    could no longer see that the dep had finished and reported the child blocked forever.

    Parametrized over BOTH finished states, not just `done` — see FINISHED_STATES.
    """
    assert [r.slug for r in selectable(_dep_pair(dep_state))] == ["kid"]


def test_the_naive_composition_order_strands_the_same_epic():
    """Pins WHY selectable() exists, so nobody 'simplifies' it back into the caller.

    Asserted as an inequality against the wrong order rather than as a second correct
    result: if someone reorders selectable() internally, this fails.
    """
    for dep_state in FINISHED_STATES:
        recs = _dep_pair(dep_state)
        assert [r.slug for r in ready_set(dispatchable(recs))] == []      # the old published order
        assert [r.slug for r in selectable(recs)] == ["kid"]


def test_ready_set_dep_math_is_corrupted_by_any_prior_filter_not_just_approval():
    """The footgun PREDATES the approval filter — recorded so the fix is not read as narrower.

    A `done` prerequisite carrying a non-dispatchable queue_class was already invisible to
    ready_set() under the old order, with no approval filter involved. The approval filter
    turned a latent hole into a total one; it did not create it.
    """
    dep = EpicRecord(slug="dep", title="d", lifecycle_state="done",
                     current_state="done", queue_class="local-tooling")
    kid = EpicRecord(slug="kid", title="k", lifecycle_state="approved-for-autonomous",
                     current_state=APPROVED_CURRENT_STATE, depends_on=["dep"])
    assert [r.slug for r in ready_set(dispatchable([dep, kid]))] == []
    assert [r.slug for r in selectable([dep, kid])] == ["kid"]


def test_selectable_still_blocks_an_unmet_dependency():
    """The dep math must survive the reorder — selectable() is not 'ready_set removed'."""
    dep = EpicRecord(slug="dep", title="d", lifecycle_state="planning", current_state="drafted")
    kid = EpicRecord(slug="kid", title="k", lifecycle_state="approved-for-autonomous",
                     current_state=APPROVED_CURRENT_STATE, depends_on=["dep"])
    assert selectable([dep, kid]) == []


def test_selectable_orders_by_priority_lowest_first():
    recs = [
        EpicRecord(slug="late", title="l", lifecycle_state="approved-for-autonomous",
                   current_state=APPROVED_CURRENT_STATE, priority=900.0),
        EpicRecord(slug="unset", title="u", lifecycle_state="approved-for-autonomous",
                   current_state=APPROVED_CURRENT_STATE),          # +inf sorts last
        EpicRecord(slug="early", title="e", lifecycle_state="approved-for-autonomous",
                   current_state=APPROVED_CURRENT_STATE, priority=100.0),
    ]
    assert [r.slug for r in selectable(recs)] == ["early", "late", "unset"]


def test_selectable_still_applies_scope_and_queue_class():
    """Composition must not lose a filter — the parts' rules all still bind."""
    keep = EpicRecord(slug="keep", title="k", lifecycle_state="approved-for-autonomous",
                      current_state=APPROVED_CURRENT_STATE, project="alpha")
    foreign = EpicRecord(slug="foreign", title="f", lifecycle_state="approved-for-autonomous",
                         current_state=APPROVED_CURRENT_STATE, project="beta")
    handbuilt = EpicRecord(slug="handbuilt", title="h", lifecycle_state="approved-for-autonomous",
                           current_state=APPROVED_CURRENT_STATE, project="alpha",
                           queue_class="local-tooling")
    banned = EpicRecord(slug="banned", title="b", lifecycle_state="approved-for-autonomous",
                        current_state=APPROVED_CURRENT_STATE, project="alpha", domain="sibling")
    recs = [keep, foreign, handbuilt, banned]
    assert [r.slug for r in selectable(recs, ["sibling"], project="alpha")] == ["keep"]


@pytest.mark.parametrize("near_miss", [
    "not-approved-for-autonomous",
    "pending-approved-for-autonomous",
    "approved-for-autonomous-NOT",
    "approved-for-autonomous-v2",
    "approved-for-autonomous # 2026-08-30 by a human",
])
def test_a_state_that_merely_CONTAINS_the_approved_token_is_not_approved(near_miss: str):
    """Kills the containment mutation the review found surviving.

    Every other negative fixture is `drafted`, a terminal value, unset or a non-string —
    none is a string that CONTAINS the approved token, so loosening `==` to `in` passed the
    whole suite. The last case is the realistic one: the corpus does carry states annotated
    with a trailing comment, so 'be tolerant of that' is a plausible future edit that would
    otherwise sail through green.
    """
    rec = EpicRecord(slug="nm", title="n", lifecycle_state="planning", current_state=near_miss)
    assert dispatchable([rec]) == []


def test_a_non_string_current_state_warns_even_though_ordinary_drops_are_silent(caplog):
    """The one documented exception to the silent-drop rule.

    Sits deliberately next to the silence test: the two were one fixture apart from
    contradicting each other, and the docstring now states which way each goes. A malformed
    state IS the abnormal case worth reading; an unapproved one is not.
    """
    rec = EpicRecord(slug="yamlbool", title="y", lifecycle_state="planning", current_state=True)
    with caplog.at_level("WARNING"):
        assert dispatchable([rec]) == []
    assert any("non-string current_state" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# The published path, end to end through survey_epics()
#
# The tests above build EpicRecords by hand, which is what let a FALSE GATE ship: they
# proved selectable() handles an archived prerequisite correctly while the real corpus
# never contained one, because survey_epics() deletes archived records before selectable()
# ever runs. A gate on hand-built records cannot see a defect in what fills the list.
# --------------------------------------------------------------------------- #
def _archived_dep_corpus(root: Path) -> None:
    make_spec(root, "alpha", "dep", frontmatter="current_state: archived\n")
    make_spec(root, "alpha", "kid",
              frontmatter=f"{APPROVED}depends_on: [dep]\n")


def test_an_archived_prerequisite_resolves_only_when_the_survey_includes_archived(tmp_path: Path):
    """The false gate, pinned from the side that actually decides.

    `survey_epics` drops archived records at step 4 by default, BEFORE anything downstream
    runs, so `ready_set` cannot mark a dependency satisfied by a record it cannot see and the
    approved child strands forever, silently. This asserts BOTH directions, because asserting
    only the working one would be the same mistake again: the default must strand (that is the
    live behaviour a caller has to know about) and `include_archived=True` must not.

    There is no trade-off in widening it: archived is terminal, so `_is_approved` is false and
    `dispatchable` drops the prerequisite a line later regardless. It can only ever let a
    finished dependency be recognised as finished.
    """
    _archived_dep_corpus(tmp_path)
    default = survey_epics(tmp_path, PAST_CUTOFF, project="alpha")
    full = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True, project="alpha")

    assert [r.slug for r in default] == ["kid"]          # the prerequisite is simply gone
    assert [r.slug for r in selectable(default)] == []   # ...so the child strands

    assert {r.slug for r in full} == {"dep", "kid"}
    assert [r.slug for r in selectable(full)] == ["kid"]  # and the archived dep is NOT claimable


def test_a_misfiled_record_still_warns_when_its_own_deps_are_unresolvable(caplog):
    """The warning must survive the reorder that `selectable()` introduced.

    `warn_foreign_records` says why it must see the whole corpus: once `ready_set` runs first,
    a record misfiled under another project whose `depends_on` names epics in its OWN project
    is dropped as dependency-blocked before `dispatchable` can notice it is foreign. That is
    the TYPICAL misfiling, so the line went silent exactly where it was needed.
    """
    misfiled = EpicRecord(
        slug="misfiled", title="m", lifecycle_state="approved-for-autonomous",
        current_state=APPROVED_CURRENT_STATE, project="beta", depends_on=["a-beta-epic"],
    )
    mine = EpicRecord(slug="mine", title="m", lifecycle_state="approved-for-autonomous",
                      current_state=APPROVED_CURRENT_STATE, project="alpha")
    with caplog.at_level("WARNING"):
        kept = selectable([misfiled, mine], (), project="alpha")
    assert [r.slug for r in kept] == ["mine"]
    warnings = [r.getMessage() for r in caplog.records if "another project" in r.getMessage()]
    assert warnings, "the foreign-project misfiling was dropped silently"
    assert "misfiled" in warnings[0]


def test_the_foreign_warning_is_not_logged_twice(caplog):
    """`selectable` warns from the full corpus and tells `dispatchable` not to repeat it.

    A duplicated warning is not merely noise here: this line is how a misfiling reaches a
    human, and a channel that double-reports trains the reader to skim it.
    """
    foreign = EpicRecord(slug="foreign", title="f", lifecycle_state="approved-for-autonomous",
                         current_state=APPROVED_CURRENT_STATE, project="beta")
    with caplog.at_level("WARNING"):
        selectable([foreign], (), project="alpha")
    assert len([r for r in caplog.records if "another project" in r.getMessage()]) == 1


def test_dispatchable_called_directly_still_warns():
    """`warn_foreign=True` is the default, so no existing caller lost its warning."""
    import inspect
    from agentflow.epic_survey import dispatchable as _d
    assert inspect.signature(_d).parameters["warn_foreign"].default is True


# --------------------------------------------------------------------------- #
# warn_unresolvable_dependencies — making the cross-project stranding LOUD
#
# The structural fix (unscoped survey / two inputs / same-project-only / backfill
# `project:`) is a human decision and is deliberately NOT here. What is here is the half
# that is correct under all four: a dep slug absent from the records handed in gets named
# instead of stranding its epic in silence.
#
# These fixtures cannot come from the live corpus. The filtered warning correctly measures
# ZERO there — that is the whole design — so constructed records are the only thing standing
# between this and a warning that never fires.
# --------------------------------------------------------------------------- #
# The colon is load-bearing: it matches the one-line-per-pair warnings and NOT the
# "...and N more" overflow summary, so a cap test can count the two separately.
UNRESOLVABLE = "unresolvable depends_on: "


def _unresolvable_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if UNRESOLVABLE in r.getMessage()]


def _approved(slug: str, **kw) -> EpicRecord:
    """An approved, allow-listed record — the only shape the warning considers."""
    kw.setdefault("current_state", APPROVED_CURRENT_STATE)
    return EpicRecord(slug=slug, title=slug, lifecycle_state="approved-for-autonomous", **kw)


def test_an_absent_dep_warns_once_and_names_both_slugs(caplog):
    """The defect, pinned. `ready_set` is about to hide `kid` and say nothing about it."""
    kid = _approved("kid", depends_on=["lives-elsewhere"])
    with caplog.at_level("WARNING"):
        pairs = warn_unresolvable_dependencies([kid])
    assert pairs == [("kid", "lives-elsewhere")]
    warnings = _unresolvable_warnings(caplog)
    assert len(warnings) == 1
    assert "kid" in warnings[0]
    assert "lives-elsewhere" in warnings[0]


def test_the_message_states_both_possibilities_and_the_consequence(caplog):
    """It must not GUESS. From inside the function those two causes are indistinguishable.

    Asserted because a future edit that tightens the wording to one cause would be a lie the
    function has no evidence for, and prose drift is exactly what put the bug here.
    """
    with caplog.at_level("WARNING"):
        warn_unresolvable_dependencies([_approved("kid", depends_on=["gone"])])
    msg = _unresolvable_warnings(caplog)[0]
    assert "another project" in msg          # possibility one
    assert "does not exist" in msg           # possibility two
    assert "never be selected" in msg        # the consequence, said plainly


def test_the_message_says_which_slug_is_the_dependent_and_which_is_the_dep(caplog):
    """Substring assertions cannot tell 'kid needs gone' from 'gone needs kid'.

    Both slugs appear in the line either way, so every loose assertion above stays green with
    the two format arguments swapped — and the whole value of a one-line diagnostic is telling
    the reader which file to open. Pinned by position, plus the scope clause, which answers
    the reader's second question (which project's glob was this?) and is likewise free to
    vanish under a substring test.
    """
    with caplog.at_level("WARNING"):
        warn_unresolvable_dependencies(
            [_approved("kid", project="alpha", depends_on=["lives-elsewhere"])],
            project="alpha",
        )
    msg = _unresolvable_warnings(caplog)[0]
    assert msg.startswith("unresolvable depends_on: 'kid' needs 'lives-elsewhere',")
    assert msg.endswith("'kid' is approved and will never be selected while this holds")
    assert "(scope 'alpha')" in msg


def test_a_present_dep_that_is_itself_blocked_is_NOT_reported_missing(caplog):
    """`known` comes from the records handed in, never from `ready_set(records)`.

    Sourcing it from the ready set is the one mutation that turns this channel into a liar:
    a prerequisite that exists but is itself dependency-blocked would be reported as "does not
    exist at all". Every other "dep is present" fixture here gives the dep an empty
    `depends_on`, so the two sets coincide and the distinction goes untested.
    """
    mid = EpicRecord(slug="mid", title="m", lifecycle_state="planning",
                     current_state="drafted", depends_on=["grandparent-absent"])
    kid = _approved("kid", depends_on=["mid"])
    with caplog.at_level("WARNING"):
        assert warn_unresolvable_dependencies([mid, kid]) == []
    assert _unresolvable_warnings(caplog) == []


def test_a_dep_that_is_present_and_done_does_not_warn(caplog):
    dep = EpicRecord(slug="dep", title="d", lifecycle_state="done", current_state="done")
    kid = _approved("kid", depends_on=["dep"])
    with caplog.at_level("WARNING"):
        assert warn_unresolvable_dependencies([dep, kid]) == []
    assert _unresolvable_warnings(caplog) == []


def test_a_dep_that_is_present_but_NOT_done_does_not_warn(caplog):
    """An ordinary blocked epic is the dependency system working, not a defect.

    This is the line between the two: `warn_unresolvable_dependencies` reports records
    `ready_set` can never unblock, never records it is merely holding back for now.
    """
    dep = EpicRecord(slug="dep", title="d", lifecycle_state="planning", current_state="drafted")
    kid = _approved("kid", depends_on=["dep"])
    with caplog.at_level("WARNING"):
        assert warn_unresolvable_dependencies([dep, kid]) == []
    assert _unresolvable_warnings(caplog) == []
    assert selectable([dep, kid]) == []      # still blocked, as it should be


def test_an_unresolvable_dep_on_a_NON_APPROVED_record_does_not_warn(caplog):
    """The measurement that made the filter load-bearing, asserted directly.

    Unfiltered, the sibling project produced 65 of these in one run — terminal records whose
    `depends_on` holds legacy junk (bare dates, `.md` filenames, a full prose sentence). None
    can ever be dispatched, so none is stranded work, and 65 unreadable lines would train the
    reader to skip the channel the one real line arrives on.
    """
    junk = EpicRecord(slug="old", title="o", lifecycle_state="done", current_state="done",
                      depends_on=["2026-01-05", "some-plan.md"])
    with caplog.at_level("WARNING"):
        assert warn_unresolvable_dependencies([junk]) == []
    assert _unresolvable_warnings(caplog) == []


@pytest.mark.parametrize("queue_class", sorted(NON_DISPATCHABLE_QUEUE_CLASSES))
def test_an_unresolvable_dep_on_a_NON_ALLOW_LISTED_queue_class_does_not_warn(
    queue_class: str, caplog
):
    """Approved but hand-built: no loop will ever dispatch it, so it is not stranded."""
    rec = _approved("handbuilt", queue_class=queue_class, depends_on=["gone"])
    with caplog.at_level("WARNING"):
        assert warn_unresolvable_dependencies([rec]) == []
    assert _unresolvable_warnings(caplog) == []


def test_an_unresolvable_dep_on_a_FOREIGN_record_does_not_warn(caplog):
    """A misfiling already has its own line — `warn_foreign_records`. Two would be noise.

    Asserts through `selectable`, where both warnings run, that the foreign line still fires:
    otherwise this would prove only that a foreign record can be swallowed whole and reported
    by nobody — the exact silence this change exists to end.
    """
    foreign = _approved("theirs", project="beta", depends_on=["gone"])
    with caplog.at_level("WARNING"):
        assert warn_unresolvable_dependencies([foreign], project="alpha") == []
        assert selectable([foreign], (), project="alpha") == []
    assert _unresolvable_warnings(caplog) == []
    assert len([r for r in caplog.records if "another project" in r.getMessage()]) == 1


def test_an_unresolvable_dep_on_an_EXCLUDED_DOMAIN_record_does_not_warn(caplog):
    """`project.excluded_domains` work is built by hand, so it cannot be stranded by a loop."""
    banned = _approved("banned", domain="sibling", depends_on=["gone"])
    with caplog.at_level("WARNING"):
        assert warn_unresolvable_dependencies([banned], ["sibling"]) == []
    assert _unresolvable_warnings(caplog) == []


def test_two_unresolvable_deps_return_two_pairs(caplog):
    kid = _approved("kid", depends_on=["gone-b", "gone-a"])
    with caplog.at_level("WARNING"):
        pairs = warn_unresolvable_dependencies([kid])
    assert pairs == [("kid", "gone-a"), ("kid", "gone-b")]   # sorted, stable
    assert len(_unresolvable_warnings(caplog)) == 2


def test_the_pairs_are_sorted_and_de_duplicated():
    """"Sorted and stable" needs enough pairs that set-iteration order cannot fake it.

    A mutation dropping `sorted()` kept a two-pair assertion GREEN — Python's per-process
    string-hash randomisation happened to emit those two in order. Nine pairs across three
    records makes that coincidence ~1 in 9!, so the ordering contract is actually pinned.
    The repeated dep also pins de-duplication: `depends_on` is a list and may name the same
    slug twice, which must still be ONE reported pair.
    """
    recs = [_approved(s, depends_on=["z-gone", "m-gone", "a-gone", "a-gone"])
            for s in ("c-kid", "a-kid", "b-kid")]
    pairs = warn_unresolvable_dependencies(recs)
    assert pairs == sorted(pairs)
    assert len(pairs) == 9                       # 3 records x 3 distinct deps, not 12
    assert pairs[0] == ("a-kid", "a-gone")
    assert pairs[-1] == ("c-kid", "z-gone")


def test_only_the_absent_dep_of_a_partly_resolvable_record_is_reported(caplog):
    """One record, one good dep and one bad — the bad one alone must surface."""
    dep = EpicRecord(slug="dep", title="d", lifecycle_state="done", current_state="done")
    kid = _approved("kid", depends_on=["dep", "gone"])
    with caplog.at_level("WARNING"):
        assert warn_unresolvable_dependencies([dep, kid]) == [("kid", "gone")]
    assert len(_unresolvable_warnings(caplog)) == 1


def test_the_log_is_capped_but_the_return_value_is_complete(caplog):
    """The cap mirrors `warn_foreign_records`' example list — it must not hide the count.

    Truncating a warning channel with no trace is the failure mode this whole change exists
    to undo, so the overflow is summarised and the return value stays whole.
    """
    kid = _approved("kid", depends_on=[f"gone-{i}" for i in range(7)])
    with caplog.at_level("WARNING"):
        pairs = warn_unresolvable_dependencies([kid])
    assert len(pairs) == 7
    assert len(_unresolvable_warnings(caplog)) == 5
    assert any("2 more" in r.getMessage() for r in caplog.records)


def test_selectable_emits_the_warning_from_the_full_corpus(caplog):
    """Wired into `selectable`, BEFORE `ready_set` narrows anything.

    `ready_set` drops `kid` for an unmet dep and `dispatchable` never sees it, so a check
    placed after the composition would report nothing — which is the bug, restated.
    """
    kid = _approved("kid", project="alpha", depends_on=["lives-elsewhere"])
    with caplog.at_level("WARNING"):
        assert [r.slug for r in selectable([kid], (), project="alpha")] == []
    warnings = _unresolvable_warnings(caplog)
    assert len(warnings) == 1
    assert "lives-elsewhere" in warnings[0]


def test_selectable_returns_exactly_what_the_unwarned_composition_returns():
    """THE guarantee: the warning changes selection by not one record.

    Compares against the composition `selectable` documents, element for element AND by
    identity, over a corpus deliberately mixed so every filter and both warnings are live:
    a warned record, a finished dep, a blocked child, a foreign record, a hand-built one, an
    excluded-domain one, and two selectable records that must come back priority-ordered.
    Asserting only "no exception raised" would not catch a reordering or a dropped record.
    """
    done_dep = EpicRecord(slug="dep", title="d", lifecycle_state="done", current_state="done",
                          project="alpha")
    recs = [
        _approved("stranded", project="alpha", depends_on=["lives-elsewhere"]),
        done_dep,
        _approved("blocked", project="alpha", depends_on=["not-done-yet"]),
        EpicRecord(slug="not-done-yet", title="n", lifecycle_state="planning",
                   current_state="drafted", project="alpha"),
        _approved("theirs", project="beta"),
        _approved("handbuilt", project="alpha", queue_class="local-tooling"),
        _approved("banned", project="alpha", domain="sibling"),
        _approved("late", project="alpha", priority=900.0, depends_on=["dep"]),
        _approved("early", project="alpha", priority=100.0),
    ]
    reference = by_priority(dispatchable(
        ready_set(recs), ["sibling"], project="alpha", warn_foreign=False,
    ))
    selected = selectable(recs, ["sibling"], project="alpha")

    assert [r.slug for r in selected] == ["early", "late"]     # nails the reference itself
    assert selected == reference
    assert all(a is b for a, b in zip(selected, reference))
    assert len(selected) == len(reference)


# --------------------------------------------------------------------------- #
# Which SENTENCE fires — the pair set is the same either way, but the wrong
# sentence sends the reader to a deferred design decision instead of a typo.
# --------------------------------------------------------------------------- #
def test_a_dep_written_as_the_FILE_STEM_is_named_as_a_typo_not_a_scoping_problem(caplog):
    """Record slugs are normalized on the way in; `depends_on` entries are not.

    So `depends_on: [2026-07-12-parent]` against a record surveyed as `parent` misses `known`
    for a reason that has nothing to do with project scope. Measured on the sibling corpus,
    59 of its 65 unresolvable deps are exactly this. Reporting them as possibly-cross-project
    points the reader at a deferred human decision when the fix is deleting the date prefix.
    """
    parent = EpicRecord(slug="parent", title="p", lifecycle_state="done", current_state="done")
    kid = _approved("kid", depends_on=["2026-07-12-parent"])
    with caplog.at_level("WARNING"):
        pairs = warn_unresolvable_dependencies([parent, kid])
    assert pairs == [("kid", "2026-07-12-parent")]      # still reported: it IS stranded
    msg = _unresolvable_warnings(caplog)[0]
    # Pinned by POSITION, like the cross-project sentence: both slugs appear in the line
    # whichever way round the format arguments go, so a substring test cannot tell the
    # dependent from the dep, and telling them apart is the whole job of the line.
    assert msg.startswith("unresolvable depends_on: 'kid' needs '2026-07-12-parent',")
    assert msg.endswith("so 'kid' should depend on 'parent' instead")
    assert "RAW string" in msg                          # and why the raw form misses
    assert "another project" not in msg                 # NOT the cross-project sentence
    # It says the dep should read the SLUG, never the other way about.
    assert "should depend on '2026-07-12-parent'" not in msg
    # The stranding itself is unchanged — this commit fixes silence, never selection.
    assert selectable([parent, kid]) == []


def test_a_dep_written_as_a_FILENAME_is_named_as_a_typo_too(caplog):
    """Same class, one suffix further out: the corpus also holds `<stem>.md` deps."""
    parent = EpicRecord(slug="parent", title="p", lifecycle_state="done", current_state="done")
    kid = _approved("kid", depends_on=["2026-07-12-parent.md"])
    with caplog.at_level("WARNING"):
        assert warn_unresolvable_dependencies([parent, kid]) == [("kid", "2026-07-12-parent.md")]
    msg = _unresolvable_warnings(caplog)[0]
    assert "'parent'" in msg
    assert "another project" not in msg


def test_a_dep_that_normalizes_to_nothing_present_keeps_the_cross_project_sentence(caplog):
    """The normalization hint must not swallow the case it cannot explain.

    A dated dep whose normalized form is ALSO absent is still the gh#44 shape, and claiming a
    typo there would be a guess — the failure mode the "says both ways" rule exists to stop.
    """
    kid = _approved("kid", depends_on=["2026-07-12-lives-elsewhere"])
    with caplog.at_level("WARNING"):
        warn_unresolvable_dependencies([kid])
    msg = _unresolvable_warnings(caplog)[0]
    assert "another project" in msg
    assert "RAW string" not in msg


# --------------------------------------------------------------------------- #
# The cap must not re-create the silence.
# --------------------------------------------------------------------------- #
def test_one_noisy_record_cannot_consume_the_whole_log_budget(caplog):
    """Every affected epic gets a line before any epic gets its second.

    `pairs` is slug-sorted, so a flat `pairs[:5]` lets one record with five junk deps hide
    every other stranded epic — a smaller copy of the silence this change exists to end, and
    reachable today: the sibling corpus already has a record carrying four unresolvable deps.
    """
    noisy = _approved("aaa-noisy", depends_on=[f"junk-{i}" for i in range(6)])
    real = _approved("zzz-real", depends_on=["the-actually-stranding-dep"])
    with caplog.at_level("WARNING"):
        pairs = warn_unresolvable_dependencies([noisy, real])
    assert len(pairs) == 7
    logged = _unresolvable_warnings(caplog)
    assert len(logged) == 5
    assert any("zzz-real" in m and "the-actually-stranding-dep" in m for m in logged)


def test_the_overflow_line_names_the_epics_it_could_not_fit(caplog):
    """With more affected epics than the cap, the ones cut still have to be findable.

    Otherwise the overflow line is a count with no handle and the reader cannot tell WHICH
    epics are stranded — the original defect, one indirection out.
    """
    recs = [_approved(f"kid-{i}", depends_on=[f"gone-{i}"]) for i in range(7)]
    with caplog.at_level("WARNING"):
        pairs = warn_unresolvable_dependencies(recs)
    assert len(pairs) == 7
    overflow = [r.getMessage() for r in caplog.records if "more unresolvable" in r.getMessage()]
    assert len(overflow) == 1
    assert "2 more" in overflow[0]
    assert "affecting: kid-5, kid-6" in overflow[0]


# --------------------------------------------------------------------------- #
# Logging side effects of the extra `dispatchable` pass.
# --------------------------------------------------------------------------- #
def test_the_extra_dispatchable_pass_does_not_re_warn_unrelated_malformed_records(caplog):
    """`_norm_state`'s type warning is `dispatchable`'s documented single exception.

    `selectable` now runs `dispatchable` twice, so handing the second pass the whole corpus
    re-emitted that line for every malformed record — including ones `ready_set` hides, which
    the composition never warned about at all. Restricting the pass to records that actually
    carry an unresolvable dep keeps the count where it was.
    """
    malformed = EpicRecord(slug="bad", title="b", lifecycle_state="planning", current_state=True)
    blocked_malformed = EpicRecord(slug="bad-blocked", title="b", lifecycle_state="planning",
                                   current_state=True, depends_on=["present-but-open"])
    open_dep = EpicRecord(slug="present-but-open", title="p", lifecycle_state="planning",
                          current_state="drafted")
    with caplog.at_level("WARNING"):
        selectable([malformed, blocked_malformed, open_dep])
    type_warnings = [r for r in caplog.records if "non-string current_state" in r.getMessage()]
    assert len(type_warnings) == 1


# --------------------------------------------------------------------------- #
# The defect's own shape, end to end on disk — through a glob-scoped survey.
# --------------------------------------------------------------------------- #
def test_a_scoped_survey_warns_about_a_prerequisite_that_lives_under_another_project(
    tmp_path: Path, caplog
):
    """gh#44 as the builder meets it, through `survey_epics(..., project=...)`.

    Every other fixture here hand-builds `EpicRecord`s, which skips the glob scoping that
    CAUSES the stranding — so a change to slug derivation or to the scope patterns could take
    this warning silent with the suite still green.

    The parent is DONE under beta, which is the cruel half: the work really is finished, the
    child really is unblocked in reality, and the scoped survey still cannot see it. That is
    what used to happen with no log line at all.
    """
    make_spec(tmp_path, "alpha", "kid", frontmatter=APPROVED + "depends_on: [parent]\n")
    make_spec(tmp_path, "beta", "parent", frontmatter="current_state: done\n")
    make_status(tmp_path, "beta", "parent", status="done", done=1, total=1)

    scoped = survey_epics(tmp_path, PAST_CUTOFF, project="alpha", include_archived=True)
    assert sorted(r.slug for r in scoped) == ["kid"]   # the parent is simply not in the list
    with caplog.at_level("WARNING"):
        assert selectable(scoped, (), project="alpha") == []
    logged = _unresolvable_warnings(caplog)
    assert len(logged) == 1
    assert "'kid'" in logged[0]
    assert "'parent'" in logged[0]
    assert "(scope 'alpha')" in logged[0]

    # Unscoped, the same tree resolves and the child is selectable — the cause, pinned.
    caplog.clear()
    full = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    with caplog.at_level("WARNING"):
        assert [r.slug for r in selectable(full)] == ["kid"]
    assert _unresolvable_warnings(caplog) == []


# --------------------------------------------------------------------------- #
# forbidden_cross_project_dependencies — the FORBID half of registry Q-10001
# (re-affirmed Q-10007). The warn half shipped in #65; this is the other one.
#
# The defect these pin, measured on the live corpus rather than argued:
#   ready_set(UNSCOPED corpus)         -> agentflow-phase4-use-case-traceability IS ready
#   ready_set(agentflow-SCOPED survey) -> the same record is NOT ready
# because its `depends_on` names a record living under another project. One record, two
# answers, decided by who was looking. Option 3 forbids the edge, so both views must say no.
#
# THE TRAP, also measured: comparing `record.project != dep.project` naively finds TEN
# cross-project edges on the live corpus and NINE are false positives — one side carries
# `project: None` because the record predates the field. Only ONE edge has both sides tagged.
# The both-sides-set requirement IS this feature; the tests below spend most of their weight
# on it, because a mutation dropping it is silent, plausible, and wrong 9 times out of 10.
# --------------------------------------------------------------------------- #
FORBIDDEN = "forbidden cross-project depends_on: "


def _forbidden_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if FORBIDDEN in r.getMessage()]


def _done(slug: str, **kw) -> EpicRecord:
    """A finished prerequisite — the shape the live forbidden edge actually points at."""
    kw.setdefault("current_state", "done")
    return EpicRecord(slug=slug, title=slug, lifecycle_state="done", **kw)


def test_both_projects_set_and_different_is_forbidden_and_warns_once(caplog):
    """The live edge's shape: an approved child, a DONE parent, two tagged projects.

    The parent being finished is the cruel half — the work really is done, so `ready_set` on
    the whole corpus really does call the child ready. That is precisely what must stop.
    """
    parent = _done("parent", project="beta")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        pairs = forbidden_cross_project_dependencies([parent, kid])
    assert pairs == [("kid", "parent")]
    warnings = _forbidden_warnings(caplog)
    assert len(warnings) == 1
    assert "kid" in warnings[0]
    assert "parent" in warnings[0]


def test_the_message_names_both_slugs_and_both_projects_in_order(caplog):
    """Pinned by POSITION, like its two neighbours, and for the same reason.

    Both slugs and both project names appear in the line whichever way round the format
    arguments go, so every substring assertion above survives a swap that reverses the line's
    meaning — and telling the reader which file to open is the line's whole job. The
    consequence clause is pinned too: "never satisfiable" is the sentence that stops a reader
    waiting for a dep to finish, which it already has.
    """
    parent = _done("parent", project="beta")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        forbidden_cross_project_dependencies([parent, kid])
    msg = _forbidden_warnings(caplog)[0]
    assert msg.startswith(
        "forbidden cross-project depends_on: 'kid' (project 'alpha') needs "
        "'parent' (project 'beta') - "
    )
    assert "the per-project model forbids a dependency across projects" in msg
    assert "never" in msg and "satisfiable" in msg
    assert "neither ready nor selectable from this survey" in msg


def test_the_message_does_not_claim_more_reach_than_the_function_has(caplog):
    """It must not say "not selectable from ANY survey, scoped or unscoped" — falsifiable.

    Two same-slug specs under two projects merge to ONE record whose `project:` falls out of
    lifecycle precedence and then glob order. A child depending on its OWN project's finished
    parent can therefore look foreign from the merged unscoped corpus and native from the scoped
    one — so the unscoped view drops it while the scoped view offers it, and an absolute claim
    is refuted in the same run that printed it. The function can speak for the records it was
    handed and no others; the wording has to stop there.
    """
    parent = _done("parent", project="beta")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        forbidden_cross_project_dependencies([parent, kid])
    msg = _forbidden_warnings(caplog)[0]
    assert "any survey" not in msg
    assert "scoped or unscoped" not in msg


def test_the_message_names_the_project_of_the_record_that_CARRIES_the_edge(caplog):
    """Re-looking the carrier up by slug is last-wins, and printed a self-refuting line.

    With a duplicate slug in the input the by-slug map resolves the carrier to the OTHER
    record, so the message read "'x' (project 'alpha') needs 'x' (project 'alpha')" while
    asserting the two crossed a project boundary. The carrier record travels with its own edge
    now, so the projects it prints are the two that actually differ.
    """
    carrier = _approved("x", project="alpha", depends_on=["x"])
    other = _done("x", project="beta")
    with caplog.at_level("WARNING"):
        forbidden_cross_project_dependencies([carrier, other])
    msg = _forbidden_warnings(caplog)[0]
    assert "'x' (project 'alpha') needs 'x' (project 'beta')" in msg


# --------------------------------------------------------------------------- #
# BOTH SIDES MUST BE SET. Nine of the ten live edges die here.
# --------------------------------------------------------------------------- #
def test_an_UNSET_dep_project_is_not_a_cross_project_dependency(caplog):
    """`None` vs 'alpha' is UNKNOWN, never DIFFERENT — `project:` postdates the earliest specs.

    Most of the ten naive edges on the live corpus are exactly this shape (a tagged carrier
    depending on an untagged parent, or the mirror of it). Forbidding them would strand
    finished chains on the strength of a field nobody had filled in yet.
    """
    parent = _done("parent")                                   # project unset
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == []
    assert _forbidden_warnings(caplog) == []
    assert [r.slug for r in selectable([parent, kid])] == ["kid"]   # and it still resolves


def test_an_UNSET_carrier_project_is_not_a_cross_project_dependency(caplog):
    """The mirror case — the legacy record is the DEPENDENT rather than the prerequisite.

    Separate from the test above because the predicate has two operands and a mutation can
    guard one of them: `dep.project is not None and record.project != dep.project` passes the
    unset-dep test and fails this one.
    """
    parent = _done("parent", project="beta")
    kid = _approved("kid", depends_on=["parent"])              # project unset
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == []
    assert _forbidden_warnings(caplog) == []
    assert [r.slug for r in selectable([parent, kid])] == ["kid"]


def test_BOTH_projects_unset_is_not_a_cross_project_dependency(caplog):
    """Neither side tagged.

    `None != None` is False anyway, but the tempting sentinel mutation
    (`(r.project or "") != (d.project or "")`) keeps that true while breaking the two tests
    above — so this pins the floor rather than the edge.
    """
    parent = _done("parent")
    kid = _approved("kid", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == []
    assert _forbidden_warnings(caplog) == []


def test_the_same_project_on_both_sides_is_an_ordinary_dependency(caplog):
    parent = _done("parent", project="alpha")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == []
    assert _forbidden_warnings(caplog) == []
    assert [r.slug for r in selectable([parent, kid], (), project="alpha")] == ["kid"]


def test_a_present_same_project_dep_that_is_NOT_done_stays_silent(caplog):
    """Ordinary sequencing. The record is blocked, correctly, and nobody needs telling.

    This is the binding "warn ONLY on the forbidden case" line: a channel that also spoke for
    every not-yet-finished dependency would drown the one line worth reading.
    """
    parent = EpicRecord(slug="parent", title="p", lifecycle_state="planning",
                        current_state="drafted", project="alpha")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == []
        assert selectable([parent, kid], (), project="alpha") == []
    assert _forbidden_warnings(caplog) == []


def test_an_ABSENT_dep_is_the_OTHER_channel_and_not_this_one(caplog):
    """The two warnings partition the case by what is visible; neither double-reports.

    An absent slug may be foreign or may not exist, and from inside a scoped survey those are
    indistinguishable — that is `warn_unresolvable_dependencies`' case, and it says both. This
    function only speaks where it has evidence: a dep that is PRESENT and provably foreign.
    """
    kid = _approved("kid", project="alpha", depends_on=["lives-elsewhere"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([kid]) == []
        assert selectable([kid], (), project="alpha") == []
    assert _forbidden_warnings(caplog) == []
    assert len(_unresolvable_warnings(caplog)) == 1


def test_a_dep_written_as_a_FILE_STEM_is_not_forbidden_here(caplog):
    """Resolution is RAW — the same string comparison `ready_set` makes.

    `depends_on: [2026-07-12-parent]` never resolved for the dependency math either, so calling
    it a forbidden cross-project edge would report an edge the math never satisfied and send
    the reader to a settled design decision when the fix is deleting a date prefix. It is
    already named, correctly, by the other channel.
    """
    parent = _done("parent", project="beta")
    kid = _approved("kid", project="alpha", depends_on=["2026-07-12-parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == []
        assert warn_unresolvable_dependencies([parent, kid]) == [("kid", "2026-07-12-parent")]
    assert _forbidden_warnings(caplog) == []
    assert len(_unresolvable_warnings(caplog)) == 1


# --------------------------------------------------------------------------- #
# TERMINAL CARRIERS ARE FILTERED — the documented choice, pinned both ways.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("terminal", sorted(TERMINAL_CURRENT_STATES))
def test_a_TERMINAL_carrier_is_not_LOGGED_but_is_still_RETURNED(terminal: str, caplog):
    """The filter is on the channel, not on the answer — and the difference is load-bearing.

    Silence is earned: today's ONE genuine forbidden edge is carried by a `done` record, so an
    unfiltered channel prints a line about a finished epic on every survey forever, which is the
    noise that trains a reader to skip the channel the real line will one day arrive on.

    Filtering the RETURN VALUE as well was a different thing wearing the same justification.
    It made the function named after the rule answer `[]` on the only corpus that has ever
    violated it, so a steward sweep asking "does this corpus contain a forbidden edge?" would be
    told no — a silence with no reader to protect, on a feature whose premise is that a silent
    strand is worse than a noisy one.
    """
    parent = _done("parent", project="beta")
    finished = EpicRecord(slug="finished", title="f", lifecycle_state="done",
                          current_state=terminal, project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, finished]) == [
            ("finished", "parent")
        ]
        assert selectable([parent, finished]) == []      # unselectable regardless
    assert _forbidden_warnings(caplog) == []


def test_a_terminal_carrier_does_not_consume_the_overflow_budget(caplog):
    """The cap counts what is SHOWN, so an unlogged pair must not push a real one into overflow.

    Once the return value stopped being filtered, a naive overflow line would count the terminal
    pairs it deliberately did not print and announce "1 more ... not listed" with nothing
    hidden — the finished-epic noise coming back through the summary line it was removed from.
    """
    parent = _done("parent", project="beta")
    finished = [
        EpicRecord(slug=f"fin{i}", title="f", lifecycle_state="done", current_state="done",
                   project="alpha", depends_on=["parent"])
        for i in range(4)
    ]
    live = _approved("live", project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        pairs = forbidden_cross_project_dependencies([parent, *finished, live])
    assert len(pairs) == 5                                  # every edge comes back
    assert len(_forbidden_warnings(caplog)) == 1            # only the live carrier speaks
    assert not [r for r in caplog.records if "more forbidden" in r.getMessage()]


def test_a_STALE_APPROVED_carrier_whose_status_says_done_is_still_reported(caplog):
    """The filter keys on `current_state`, deliberately, and NOT on `lifecycle_state`.

    A record whose status.md says done but whose frontmatter still reads
    `approved-for-autonomous` remains selectable — `selectable` documents that caveat and does
    not fix it. So it can still be offered, so its forbidden edge must still be excluded and
    still spoken about. Keying the filter on `lifecycle_state` would silently reopen the hole
    for exactly the records most likely to carry a stale dependency list.
    """
    parent = _done("parent", project="beta")
    stale = EpicRecord(slug="stale", title="s", lifecycle_state="done",
                       current_state=APPROVED_CURRENT_STATE, project="alpha",
                       depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, stale]) == [("stale", "parent")]
    assert len(_forbidden_warnings(caplog)) == 1
    assert selectable([parent, stale]) == []


def test_a_TERMINAL_dep_is_still_forbidden(caplog):
    """Only the CARRIER is filtered.

    The live edge points at a `done` parent, so a filter that also dropped terminal DEPS would
    make this function silent on the only real case there is.
    """
    parent = _done("parent", project="beta")               # terminal, and foreign
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == [("kid", "parent")]
    assert len(_forbidden_warnings(caplog)) == 1


# --------------------------------------------------------------------------- #
# WHICH CARRIERS, AND WHICH DEPS, THE CHANNEL SPEAKS FOR.
#
# Both axes were single-valued in the first cut: every fixture carrier was `_approved` and
# every foreign parent was `_done`. Two plausible tidy-ups therefore passed the whole suite
# while silencing the warning in the exact window it is most useful — before a human approves
# the spec, and before the foreign parent has shipped.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state", ["drafted", "speccing", "planning", "planning:review"])
def test_a_NON_APPROVED_non_terminal_carrier_is_still_reported(state: str, caplog):
    """Only TERMINAL carriers are filtered — not "everything except approved".

    Aligning this filter with its neighbour `warn_unresolvable_dependencies`, which restricts to
    `dispatchable` carriers, is the plausible tidy-up: same file, same shape, and the suite
    stayed green under it. It is wrong here because the two make different claims. The sibling
    says "this WOULD dispatch if the dep resolved", which is only true of an approved record.
    This one says "this spec is illegal", which is true the moment the edge is written — and a
    `drafted` or `planning` carrier is precisely when a human can still fix it for free, before
    approving it. Narrowing to dispatchable goes silent for that entire window, changes no
    selection, and so would never be noticed.
    """
    parent = _done("parent", project="beta")
    kid = EpicRecord(slug="kid", title="k", lifecycle_state="planning",
                     current_state=state, project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == [("kid", "parent")]
    assert len(_forbidden_warnings(caplog)) == 1


@pytest.mark.parametrize("state", ["speccing", "planning", "running", "approved-for-autonomous"])
def test_a_foreign_dep_that_has_NOT_FINISHED_is_already_forbidden(state: str, caplog):
    """The edge is illegal when WRITTEN, not when the parent lands.

    Every foreign parent in these fixtures is `_done`, because that is the shape of the one live
    edge — so a mutation gating the edge on the dep being done/archived passed the whole suite.
    It would mean an unfinishable dependency stays quiet for as long as the parent is unfinished,
    i.e. for the entire period during which the mistake is cheap to fix, and only speaks once the
    parent ships. Nothing else covers it: `ready_set` blocks the carrier on the unmet dep either
    way, so the warning going silent changes no selection and shows up nowhere but the channel.
    """
    parent = EpicRecord(slug="parent", title="p", lifecycle_state="planning",
                        current_state=state, project="beta")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == [("kid", "parent")]
    assert len(_forbidden_warnings(caplog)) == 1


# --------------------------------------------------------------------------- #
# A TYPO IN `project:` IS NOT A PROJECT BOUNDARY.
#
# `project:` gates selection now, so an un-normalized `!=` lets a casing or whitespace slip in
# ONE record strand a DIFFERENT, correctly-tagged record — permanently, and with a message
# blaming a boundary the two are on the same side of. `_is_terminal` / `_is_approved` already
# route through `_norm_state` for exactly this reason; this is the same treatment for the same
# hazard, one field over.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("typo", ["Agentflow", "AGENTFLOW", "agentflow ", " agentflow"])
def test_a_CASING_or_WHITESPACE_variant_is_the_same_project(typo: str, caplog):
    """Measured before the fix: all four of these returned `selectable == []`."""
    parent = _done("parent", project=typo)
    kid = _approved("kid", project="agentflow", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == []
        assert [r.slug for r in selectable([parent, kid], project="agentflow")] == ["kid"]
    assert _forbidden_warnings(caplog) == []


def test_an_EMPTY_project_string_is_UNSET_not_DIFFERENT(caplog):
    """`project: ""` is set-to-nothing, which carries none of the evidence the rule needs.

    It survives `_read_sidecar`'s `isinstance(raw_project, str)` test — unlike
    `build_approved_by`, which strips one line below — so it reached `_is_foreign` as a real
    value and read as "set, and different" against every actual project name.
    """
    parent = _done("parent", project="")
    kid = _approved("kid", project="agentflow", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == []
        assert [r.slug for r in selectable([parent, kid], project="agentflow")] == ["kid"]
    assert _forbidden_warnings(caplog) == []


def test_a_whitespace_variant_does_not_hide_a_REAL_cross_project_edge(caplog):
    """Normalizing must not swallow the case it exists to report."""
    parent = _done("parent", project=" Beta ")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([parent, kid]) == [("kid", "parent")]
    assert len(_forbidden_warnings(caplog)) == 1


# --------------------------------------------------------------------------- #
# THE HUMAN-FACING PATH ENFORCES IT TOO.
#
# choose-next reads `ready_set` and `blocked_status` directly, project-wide, and never calls
# `selectable`. Enforcing the rule only in `selectable` left the operator being told READY about
# a record every builder refuses forever — the same "one record, two answers" split, moved.
# --------------------------------------------------------------------------- #
def test_ready_set_does_not_resolve_a_forbidden_edge():
    """This is the measured live defect in miniature: unscoped, `ready_set` called it ready."""
    parent = _done("parent", project="beta")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    assert [r.slug for r in ready_set([parent, kid])] == ["parent"]


def test_ready_set_still_resolves_an_ordinary_same_project_dep():
    """The exclusion is the forbidden EDGE, not the presence of a finished parent."""
    parent = _done("parent", project="alpha")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    assert [r.slug for r in ready_set([parent, kid])] == ["parent", "kid"]


def test_a_forbidden_carrier_that_is_DONE_is_still_kept_out_of_ready_set():
    """The live edge's carrier is `done`, so the log filter must not reach the exclusion.

    `ready_set` takes the unfiltered carrier set on purpose: the defect as measured names a
    `done` record, and reporting it ready is the inconsistency, whether or not anyone is told.
    """
    parent = _done("parent", project="beta")
    finished = EpicRecord(slug="finished", title="f", lifecycle_state="done",
                          current_state="done", project="alpha", depends_on=["parent"])
    assert [r.slug for r in ready_set([parent, finished])] == ["parent"]


def test_excluding_a_forbidden_carrier_does_not_corrupt_anyone_elses_dep_math():
    """The done-set reads *records*, not this return value — a finished carrier still counts.

    The exclusion is applied to the OUTPUT for this reason. Filtering the input instead is the
    hazard `ready_set`'s own docstring is written about: a dropped prerequisite is not "done" as
    far as the function can tell, and everything depending on it strands forever, silently.
    """
    foreign = _done("foreign", project="beta")
    # Finished, and carries a forbidden edge — so excluded from the output itself.
    carrier = _done("carrier", project="alpha", depends_on=["foreign"])
    downstream = _approved("downstream", project="alpha", depends_on=["carrier"])
    ready = [r.slug for r in ready_set([foreign, carrier, downstream])]
    assert "carrier" not in ready
    assert "downstream" in ready


def test_blocked_status_buckets_a_forbidden_dep_as_MISSING_DEP_not_READY():
    """choose-next's Step 3 bucket. It said READY, and routed the operator at a dead end."""
    parent = _done("parent", project="beta")
    kid = EpicRecord(slug="kid", title="k", lifecycle_state="planning",
                     current_state="planning", project="alpha", depends_on=["parent"])
    assert blocked_status(kid, [parent, kid]) == "MISSING-DEP"


@pytest.mark.parametrize(
    "dep_lifecycle, would_have_been", [("running", "NEAR-READY"), ("planning", "CHAINED")]
)
def test_a_forbidden_dep_outranks_the_NEAR_READY_and_CHAINED_buckets(
    dep_lifecycle: str, would_have_been: str
):
    """Both of those promise the operator it unblocks when the parent lands. It never will."""
    parent = EpicRecord(slug="parent", title="p", lifecycle_state=dep_lifecycle,
                        current_state="planning", project="beta")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    same = EpicRecord(slug="parent", title="p", lifecycle_state=dep_lifecycle,
                      current_state="planning", project="alpha")
    assert blocked_status(kid, [same, kid]) == would_have_been     # the ordinary reading
    assert blocked_status(kid, [parent, kid]) == "MISSING-DEP"     # forbidden outranks it


def test_blocked_status_is_unchanged_for_an_ordinary_same_project_dep():
    parent = _done("parent", project="alpha")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    assert blocked_status(kid, [parent, kid]) == "READY"


def test_blocked_status_leaves_an_UNSET_project_alone():
    """The both-sides-set rule reaches this bucket too — 65 live records carry `project: None`."""
    parent = _done("parent")                                # project unset
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    assert blocked_status(kid, [parent, kid]) == "READY"


def test_the_enforcement_paths_do_not_double_log(caplog):
    """`ready_set` enforces SILENTLY; `selectable` speaks once. Two voices would double it."""
    parent = _done("parent", project="beta")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        ready_set([parent, kid])
    assert _forbidden_warnings(caplog) == []
    with caplog.at_level("WARNING"):
        selectable([parent, kid], project="alpha")
    assert len(_forbidden_warnings(caplog)) == 1


# --------------------------------------------------------------------------- #
# THE INVARIANT: scoped and unscoped must AGREE. This is the defect itself.
# --------------------------------------------------------------------------- #
def test_a_forbidden_dep_is_unselectable_from_BOTH_the_full_corpus_and_the_scope(caplog):
    """`ready_set(full)` said ready; `ready_set(scoped)` said blocked. Now neither says ready.

    Before the fix the first list came back `['parent', 'kid']` and the second `[]` — the
    measured live divergence, reproduced in miniature. The point is that the answer stops
    depending on who is looking, and it has to stop at `ready_set` and not merely at
    `selectable`, because the human-facing caller reads `ready_set` and never `selectable`.
    """
    parent = _done("parent", project="beta")
    kid = _approved("kid", project="alpha", depends_on=["parent"])
    full = [parent, kid]
    scoped = [kid]                                   # what an alpha-scoped glob returns

    # The two views AGREE about `kid` now — for different reasons, which is fine. The scoped
    # one never saw the parent; the full one saw it and refused the edge.
    assert "kid" not in [r.slug for r in ready_set(full)]
    assert ready_set(scoped) == []

    with caplog.at_level("WARNING"):
        assert selectable(full, (), project="alpha") == []
        assert selectable(scoped, (), project="alpha") == []
    assert len(_forbidden_warnings(caplog)) == 1     # only the full view has the evidence


def test_a_forbidden_dep_excludes_the_record_even_with_a_second_SATISFIED_dep(caplog):
    """One good dep does not buy off a forbidden one — it is per-edge, not per-record.

    A mutation that excludes only records with NO satisfied dep, or that stops at the first
    resolving dep, passes every single-dep fixture above and fails here.
    """
    good = _done("good", project="alpha")
    parent = _done("parent", project="beta")
    kid = _approved("kid", project="alpha", depends_on=["good", "parent"])
    with caplog.at_level("WARNING"):
        assert forbidden_cross_project_dependencies([good, parent, kid]) == [("kid", "parent")]
    assert len(_forbidden_warnings(caplog)) == 1
    assert selectable([good, parent, kid], (), project="alpha") == []


def test_two_forbidden_edges_on_one_record_are_two_pairs(caplog):
    a = _done("p-a", project="beta")
    b = _done("p-b", project="gamma")
    kid = _approved("kid", project="alpha", depends_on=["p-b", "p-a"])
    with caplog.at_level("WARNING"):
        pairs = forbidden_cross_project_dependencies([a, b, kid])
    assert pairs == [("kid", "p-a"), ("kid", "p-b")]
    assert len(_forbidden_warnings(caplog)) == 2


def test_the_same_dep_forbidden_from_two_carriers_is_two_pairs(caplog):
    """The inversion of the test above: one foreign parent, two children stranded on it."""
    parent = _done("parent", project="beta")
    one = _approved("kid-1", project="alpha", depends_on=["parent"])
    two = _approved("kid-2", project="gamma", depends_on=["parent"])
    with caplog.at_level("WARNING"):
        pairs = forbidden_cross_project_dependencies([parent, one, two])
    assert pairs == [("kid-1", "parent"), ("kid-2", "parent")]
    assert len(_forbidden_warnings(caplog)) == 2
    assert selectable([parent, one, two]) == []


def test_the_forbidden_pairs_are_sorted_and_de_duplicated():
    """Nine pairs across three carriers — enough that accidental order cannot fake it.

    Set-iteration order is randomised per process, so a two-pair assertion goes green under a
    mutation that drops `sorted()` often enough to be useless (that exact false pass happened
    on this file's sibling warning). Nine pairs makes the coincidence ~1 in 9!. The repeated
    dep pins de-duplication: `depends_on` is a list and may name the same slug twice, which is
    one edge and must be one pair.
    """
    deps = [_done(s, project="beta") for s in ("a-gone", "m-gone", "z-gone")]
    kids = [_approved(s, project="alpha",
                      depends_on=["z-gone", "m-gone", "a-gone", "a-gone"])
            for s in ("c-kid", "a-kid", "b-kid")]
    pairs = forbidden_cross_project_dependencies(deps + kids)
    assert pairs == sorted(pairs)
    assert len(pairs) == 9                       # 3 carriers x 3 distinct deps, not 12
    assert pairs[0] == ("a-kid", "a-gone")
    assert pairs[-1] == ("c-kid", "z-gone")


# --------------------------------------------------------------------------- #
# The cap, and the fairness rule it shares with its neighbour.
# --------------------------------------------------------------------------- #
def test_the_forbidden_log_is_capped_but_the_return_value_is_complete(caplog):
    deps = [_done(f"gone-{i}", project="beta") for i in range(7)]
    kid = _approved("kid", project="alpha", depends_on=[f"gone-{i}" for i in range(7)])
    with caplog.at_level("WARNING"):
        pairs = forbidden_cross_project_dependencies(deps + [kid])
    assert len(pairs) == 7
    assert len(_forbidden_warnings(caplog)) == 5
    assert any("2 more forbidden" in r.getMessage() for r in caplog.records)


def test_one_noisy_record_cannot_consume_the_whole_forbidden_log_budget(caplog):
    """Every affected epic gets a line before any epic gets its second.

    `pairs` is slug-sorted, so a flat `pairs[:5]` lets one record with six forbidden deps hide
    every other stranded epic — the same silence this change exists to end, one indirection out.
    """
    deps = [_done(f"junk-{i}", project="beta") for i in range(6)]
    real = _done("the-actually-stranding-dep", project="beta")
    noisy = _approved("aaa-noisy", project="alpha",
                      depends_on=[f"junk-{i}" for i in range(6)])
    zzz = _approved("zzz-real", project="alpha", depends_on=["the-actually-stranding-dep"])
    with caplog.at_level("WARNING"):
        pairs = forbidden_cross_project_dependencies(deps + [real, noisy, zzz])
    assert len(pairs) == 7
    logged = _forbidden_warnings(caplog)
    assert len(logged) == 5
    assert any("zzz-real" in m and "the-actually-stranding-dep" in m for m in logged)


def test_the_forbidden_overflow_line_names_the_epics_it_could_not_fit(caplog):
    parent = _done("parent", project="beta")
    recs = [_approved(f"kid-{i}", project="alpha", depends_on=["parent"]) for i in range(7)]
    with caplog.at_level("WARNING"):
        pairs = forbidden_cross_project_dependencies([parent] + recs)
    assert len(pairs) == 7
    overflow = [r.getMessage() for r in caplog.records if "more forbidden" in r.getMessage()]
    assert len(overflow) == 1
    assert "2 more" in overflow[0]
    assert "affecting: kid-5, kid-6" in overflow[0]


# --------------------------------------------------------------------------- #
# Nothing ELSE about selection moves.
# --------------------------------------------------------------------------- #
def test_selectable_drops_only_the_forbidden_carrier_and_nothing_else(caplog):
    """The full selected list, pinned — an absence assertion would not catch collateral.

    The corpus is deliberately mixed so every filter and all three warnings are live: a
    forbidden carrier whose parent is finished and foreign, a record stranded on an absent dep,
    a legitimately blocked child, a foreign record, a hand-built one, an excluded-domain one,
    and two records that must come back priority-ordered. Only `forbidden` may disappear.
    """
    foreign_parent = _done("foreign-parent", project="beta")
    done_dep = _done("dep", project="alpha")
    recs = [
        _approved("forbidden", project="alpha", depends_on=["foreign-parent"]),
        foreign_parent,
        done_dep,
        _approved("stranded", project="alpha", depends_on=["lives-elsewhere"]),
        _approved("blocked", project="alpha", depends_on=["not-done-yet"]),
        EpicRecord(slug="not-done-yet", title="n", lifecycle_state="planning",
                   current_state="drafted", project="alpha"),
        _approved("theirs", project="beta"),
        _approved("handbuilt", project="alpha", queue_class="local-tooling"),
        _approved("banned", project="alpha", domain="sibling"),
        _approved("late", project="alpha", priority=900.0, depends_on=["dep"]),
        _approved("early", project="alpha", priority=100.0),
    ]
    with caplog.at_level("WARNING"):
        selected = selectable(recs, ["sibling"], project="alpha")
    assert [r.slug for r in selected] == ["early", "late"]

    # And it is the forbidden EDGE doing it, not the mere presence of a foreign record:
    # repoint the same carrier at a same-project dep and it comes straight back.
    unforbidden = [r for r in recs if r.slug != "forbidden"]
    unforbidden.append(
        _approved("forbidden", project="alpha", priority=500.0, depends_on=["dep"])
    )
    assert [r.slug for r in selectable(unforbidden, ["sibling"], project="alpha")] == [
        "early", "forbidden", "late",
    ]


# --------------------------------------------------------------------------- #
# The defect's own shape, end to end on disk.
# --------------------------------------------------------------------------- #
def test_a_cross_project_dep_is_refused_by_the_scoped_AND_the_unscoped_survey(
    tmp_path: Path, caplog
):
    """The live divergence, reproduced through `survey_epics` rather than hand-built records.

    Every other fixture here constructs `EpicRecord`s directly, which skips the frontmatter
    parsing and the glob scoping that CAUSE the split — so a change to `project:` parsing or to
    the scope patterns could take this silent with the suite still green.

    Both surveys must now refuse it, and each explains itself on the channel that holds the
    evidence: the scoped survey cannot see the parent at all, so it gets the unresolvable line;
    the unscoped one can, so it gets the forbidden line.
    """
    make_spec(tmp_path, "alpha", "kid",
              frontmatter=APPROVED + "project: alpha\ndepends_on: [parent]\n")
    make_spec(tmp_path, "beta", "parent", frontmatter="current_state: done\nproject: beta\n")
    make_status(tmp_path, "beta", "parent", status="done", done=1, total=1)

    scoped = survey_epics(tmp_path, PAST_CUTOFF, project="alpha", include_archived=True)
    assert sorted(r.slug for r in scoped) == ["kid"]
    with caplog.at_level("WARNING"):
        assert selectable(scoped, (), project="alpha") == []
    assert _forbidden_warnings(caplog) == []             # nothing to compare against
    assert len(_unresolvable_warnings(caplog)) == 1      # the other channel speaks

    caplog.clear()
    full = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert {r.slug: r.project for r in full} == {"kid": "alpha", "parent": "beta"}
    # Through a REAL on-disk survey: the dep math itself refuses the edge, so choose-next (which
    # reads ready_set/blocked_status and never selectable) gets the same answer builders do.
    assert "kid" not in [r.slug for r in ready_set(full)]
    assert blocked_status({r.slug: r for r in full}["kid"], full) == "MISSING-DEP"
    with caplog.at_level("WARNING"):
        assert selectable(full) == []                    # ...and it is not offered
    msg = _forbidden_warnings(caplog)
    assert len(msg) == 1
    assert "'kid' (project 'alpha') needs 'parent' (project 'beta')" in msg[0]
    assert _unresolvable_warnings(caplog) == []


# --------------------------------------------------------------------------- #
# Terminal partitions — the survey must reach where `reconcile` files a record
# (gh#122). `reconcile` moves a spec/plan into `<bucket>/done/` or
# `<bucket>/archived/` the moment its `current_state` goes terminal; while the
# globs were single-level, every record it moved left the survey, and with it
# `_done_slugs` — so a FINISHED prerequisite read as unsatisfied and stranded
# every dependent naming it.
# --------------------------------------------------------------------------- #
def relocate(path: Path, partition: str) -> Path:
    """Move a spec/plan into its bucket's `done/` or `archived/` — what reconcile does.

    Deliberately a rename, not a re-write: frontmatter is the thing that must be proven
    irrelevant to placement, so the bytes have to be identical either side of the move.
    """
    dest = path.parent / partition / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    return path.rename(dest)


def test_a_dependency_on_a_spec_reconciled_into_done_is_still_satisfied(tmp_path, caplog):
    """The named consequence of gh#122, and why it is severe rather than cosmetic.

    Before the globs reached `done/`, this fixture surveyed ONE record and `selectable`
    returned nothing: the prerequisite was finished, on disk, in the same project, and
    invisible — so its dependent was blocked by a condition that could never clear.

    The warning assertion pins the reason `warn_unresolvable_dependencies` needs no new
    wording for this bug. Its message enumerates two causes — another project's subtree, or
    does not exist — and silently lumped in a third it never named: present, in this
    project, in a directory the glob could not see. The fix does not make that third cause
    distinguishable, it ELIMINATES it, and a sentence naming a cause that can no longer
    occur is just a new way to be wrong. So the claim is checked here instead of restated
    there.
    """
    prereq = make_spec(tmp_path, "alpha", "prereq", frontmatter="current_state: done\n")
    make_spec(tmp_path, "alpha", "dependent",
              frontmatter=APPROVED + "depends_on: [prereq]\n")
    relocate(prereq, "done")

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert "prereq" in _by_slug(recs), "reconcile moved the prereq out of the survey's reach"
    assert _by_slug(recs)["prereq"].lifecycle_state == "done"
    assert [r.slug for r in selectable(recs, project="alpha")] == ["dependent"]
    with caplog.at_level("WARNING"):
        warn_unresolvable_dependencies(recs)
    assert _unresolvable_warnings(caplog) == []


def test_a_plan_reconciled_into_done_is_still_surveyed(tmp_path):
    """The plans bucket is not a symmetry nicety. `plan_reconcile` walks EVERY directory
    under `design/`, so a plan goes to `plans/done/` on exactly the same rule — and a
    specs-only fix leaves the mirror-image hole where the plan moves and the spec stays."""
    prereq = make_plan(tmp_path, "alpha", "prereq", frontmatter="current_state: done\n")
    make_spec(tmp_path, "alpha", "dependent",
              frontmatter=APPROVED + "depends_on: [prereq]\n")
    relocate(prereq, "done")

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert _by_slug(recs)["prereq"].lifecycle_state == "done"
    assert [r.slug for r in selectable(recs, project="alpha")] == ["dependent"]


def test_a_spec_reconciled_into_archived_is_still_surveyed_and_satisfies_deps(tmp_path):
    """`archived/` is the second partition and carries the same weight as `done/`:
    `_done_slugs` counts `archived` as satisfying, so losing it strands dependents too."""
    prereq = make_spec(tmp_path, "alpha", "prereq", frontmatter="current_state: archived\n")
    make_spec(tmp_path, "alpha", "dependent",
              frontmatter=APPROVED + "depends_on: [prereq]\n")
    relocate(prereq, "archived")

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert _by_slug(recs)["prereq"].lifecycle_state == "archived"
    assert [r.slug for r in selectable(recs, project="alpha")] == ["dependent"]


def _terminal_corpus(root: Path) -> dict:
    """One spec per terminal state, plus one active. Returns {slug: path}."""
    paths = {state: make_spec(root, "alpha", state, frontmatter=f"current_state: {state}\n")
             for state in sorted(TERMINAL_CURRENT_STATES)}
    paths["live"] = make_spec(root, "alpha", "live", frontmatter=APPROVED)
    return paths


def test_relocating_terminal_records_does_not_change_what_include_archived_means(tmp_path):
    """The load-bearing detail, and the one the issue guessed wrong about.

    The filter is `r.lifecycle_state != "archived"` — a FIELD, read after all three passes,
    from frontmatter that travels with the file. Directory has never been an input to it, so
    widening the globs changes which files are READ and nothing about what the flag means.
    Proven by building the same corpus twice and relocating one copy: identical both ways.
    """
    flat, moved = tmp_path / "flat", tmp_path / "moved"
    _terminal_corpus(flat)
    for state, path in _terminal_corpus(moved).items():
        if state in TERMINAL_CURRENT_STATES:
            relocate(path, "done" if state in ("done", "shipped") else "archived")

    for flag in (False, True):
        assert (
            {r.slug for r in survey_epics(flat, PAST_CUTOFF, include_archived=flag)}
            == {r.slug for r in survey_epics(moved, PAST_CUTOFF, include_archived=flag)}
        ), f"relocation changed the record set at include_archived={flag}"


def test_a_superseded_record_in_archived_survives_the_include_archived_filter(tmp_path):
    """The `archived/` DIRECTORY and the `archived` STATE are two different sets, and
    neither contains the other: reconcile sends `{archived, superseded}` to that folder
    while the filter drops `archived` alone. A later tidy-up that re-keyed the filter on
    directory would silently start hiding every superseded record; this fails when it does.
    """
    relocate(make_spec(tmp_path, "alpha", "sup",
                       frontmatter="current_state: superseded\n"), "archived")
    relocate(make_spec(tmp_path, "alpha", "arc",
                       frontmatter="current_state: archived\n"), "archived")

    default = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))
    assert "sup" in default, "superseded is not archived and must not be filtered as if it were"
    assert "arc" not in default
    full = _by_slug(survey_epics(tmp_path, PAST_CUTOFF, include_archived=True))
    assert {"sup", "arc"} <= set(full)


def test_a_done_spec_in_done_is_not_reopened_by_a_flat_approved_plan_sidecar(tmp_path):
    """The half of gh#122 the issue did not see: the move does not merely HIDE a record,
    it can REOPEN one.

    `_partition_for` places each file on its own frontmatter, so reconcile routinely moves
    a spec into `done/` while its plan sidecar stays flat. With the spec unreachable,
    `existing is None` in the plan pass, the terminal freeze cannot fire, and the sidecar's
    `current_state` wins unconditionally — a delivered epic surveys as approved and
    dispatchable again. That is the "10 shipped epics back in the ready set" failure,
    reached through a directory move instead of through the mtime cutoff.
    """
    spec = make_spec(tmp_path, "alpha", "prereq", frontmatter="current_state: done\n")
    make_plan(tmp_path, "alpha", "prereq", frontmatter=APPROVED)   # sidecar stays flat
    relocate(spec, "done")

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert _by_slug(recs)["prereq"].current_state == "done", "a flat sidecar reopened it"
    assert _by_slug(recs)["prereq"].lifecycle_state == "done"
    assert [r.slug for r in selectable(recs, project="alpha")] == []


def test_a_stray_subdirectory_under_a_bucket_is_NOT_surveyed(tmp_path):
    """The negative control that makes the reach a CONTRACT rather than an accident.

    A bucket is also where humans put working material: the live tree already carries 36
    grounding notes under `design/plans/grounding/` and 13 partial fragments under
    `.scratch/`. Recursive globbing would admit them — measured, one grounding note enters
    the planning queue as a live epic — so the reach names the two partitions and stops.
    """
    stray = "---\nslug: %s\ntitle: %s\n" + APPROVED + "---\n\nnot an epic\n"
    _write(tmp_path / "projects/alpha/design/specs/grounding/note.md", stray % ("note", "note"))
    _write(tmp_path / "projects/alpha/design/plans/.scratch/partial.md",
           stray % ("partial", "partial"))
    make_spec(tmp_path, "alpha", "real", frontmatter=APPROVED)

    slugs = {r.slug for r in survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)}
    assert slugs == {"real"}, f"a non-partition subdirectory was surveyed: {slugs}"


def test_reaching_into_done_does_not_make_location_a_substitute_for_approval(tmp_path):
    """The other half of the negative control. Widening the globs must restore visibility
    and NOTHING else — position never gates dispatch, and dependency math still bites on a
    prerequisite that has not finished."""
    relocate(make_spec(tmp_path, "alpha", "misfiled",
                       frontmatter="current_state: drafted\n"), "done")  # in done/, not done
    make_spec(tmp_path, "alpha", "unfinished", frontmatter="current_state: drafted\n")
    make_spec(tmp_path, "alpha", "waiting",
              frontmatter=APPROVED + "depends_on: [unfinished]\n")

    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert _by_slug(recs)["misfiled"].lifecycle_state == "drafted"    # surveyed...
    assert [r.slug for r in selectable(recs, project="alpha")] == []  # ...claimable by nobody


# --------------------------------------------------------------------------- #
# The `## Files` promotion precondition, reported by the survey (gh#294)
#
# The standing Directive of 2026-08-31 refuses promotion to a spec that does not name the
# files it will touch, and nothing reported a violation. A whole template's worth of specs
# therefore surveyed clean, sat at `drafted` looking ordinary, and burned an attended
# promotion pass each to discover they were ineligible by construction.
# --------------------------------------------------------------------------- #
GAP_TEMPLATE = Path(__file__).resolve().parents[3] / "templates" / "gap-epic.md"

# The template is TWO halves: a ```markdown fence holding the skeleton the auditor copies,
# and prose below it documenting that skeleton. Only the fence reaches a written spec, so a
# test that read the whole file would pass on an edit made to the documentation alone.
_TEMPLATE_FENCE_RE = re.compile(r"^```markdown\n(.*?)^```$", re.DOTALL | re.MULTILINE)


def _gap_template_skeleton() -> str:
    """The fenced skeleton only - what a spec written from the template actually contains."""
    m = _TEMPLATE_FENCE_RE.search(GAP_TEMPLATE.read_text(encoding="utf-8"))
    assert m is not None, f"no markdown fence in {GAP_TEMPLATE}"
    return m.group(1)


def _spec_without_files(root: Path, slug: str, state: str) -> Path:
    """A spec in the flagged population whose body has every heading EXCEPT `## Files`.

    Deliberately the exact body shape the auditor's template produced before this fix, so the
    fixture fails for the corpus's reason rather than for being empty.
    """
    body = (
        f"---\nslug: {slug}\ntitle: {slug} title\ncurrent_state: {state}\n---\n\n"
        f"# {slug} title\n\n"
        "## Gap (one sentence)\n\nintent unmet\n\n"
        "## Evidence\n\nthe audit saw X\n\n"
        "## Proposed scope\n\nclose it\n\n"
        "## Use case continuity\n\nreuse UC-0147\n"
    )
    return _write(root / "projects" / "proj" / "design" / "specs" / f"{slug}.md", body)


def _files_warnings(root: Path, caplog) -> list[str]:
    caplog.clear()
    with caplog.at_level("WARNING"):
        survey_epics(root, PAST_CUTOFF)
    return [m for m in (r.getMessage() for r in caplog.records) if "not promotable" in m]


def _assert_one_warning_naming(root: Path, caplog, spec: Path, slug: str) -> None:
    """The assertion the mutation control below has to be able to BREAK."""
    msgs = _files_warnings(root, caplog)
    assert len(msgs) == 1, f"expected exactly one warning, got {msgs}"
    assert slug in msgs[0], msgs[0]
    assert str(spec) in msgs[0], msgs[0]


@pytest.mark.parametrize("state", sorted(FILES_GATED_CURRENT_STATES))
def test_a_spec_with_no_files_section_warns_ONCE_and_names_the_file(
    tmp_path: Path, caplog, state: str
):
    """(i) The offender is reported, once, by path.

    Both gated states, for different reasons. `drafted` is where a human promotes FROM, so the
    warning is what lets them skip the ineligible ones without opening each. Past that gate,
    `approved-for-autonomous` with no section is the harm the Directive was written from: an
    epic claimable in principle, unpublishable in fact, and silent about why.
    """
    spec = _spec_without_files(tmp_path, "gap", state)
    _assert_one_warning_naming(tmp_path, caplog, spec, "gap")


@pytest.mark.parametrize(
    "suffix, label",
    [
        ("\n## Files\n\n- `agentflow/epic_survey.py` -- the warning\n", "has-files"),
        ("\n### Files\n\n- `agentflow/epic_survey.py` -- deeper heading\n", "h3-files"),
        (
            "\n## Files\n\nTo be settled in refine - the placement decision IS the scope,\n"
            "so naming files now would prejudge it. Candidate surfaces:\n"
            "\n- `docs/REGISTRY.md`\n",
            "candidate-surfaces",
        ),
    ],
)
def test_a_spec_that_declares_files_is_silent(tmp_path: Path, caplog, suffix: str, label: str):
    """(ii) The false-positive control, including the honest-placeholder shape.

    `h3-files` is not an accident: `files_section` accepts two-or-more hashes, and this warning
    MUST agree with the gate rather than being a second, stricter definition of it - a survey
    line that contradicts the seat it advises is worse than no line at all.

    `candidate-surfaces` pins the shape the template now prescribes for an epic whose scope IS
    a placement decision. It carries no concrete path list on purpose: presence is the
    promotion precondition, and a section that says something true beats an empty heading.
    """
    spec = _spec_without_files(tmp_path, "gap", "drafted")
    spec.write_text(spec.read_text(encoding="utf-8") + suffix, encoding="utf-8")
    assert _files_warnings(tmp_path, caplog) == [], label


@pytest.mark.parametrize("state", ["speccing", "done", "archived", "superseded"])
def test_a_spec_outside_the_promotion_gate_is_silent(tmp_path: Path, caplog, state: str):
    """The other half of (ii): the population is a judgement call, so pin it.

    `speccing` has not reached the gate - warning there is premature and trains the reader to
    skip the channel, the same judgement `test_a_non_approved_drop_is_silent...` already pins.
    The terminal states are never promoted again, so their section is nobody's business; they
    are also the majority of the corpus, whose files this check therefore never opens.

    THAT LAST CLAUSE IS NOT TESTED HERE and never was: this test passes for the warn-time
    reason - `_warn_specs_without_files` filters on FILES_GATED_CURRENT_STATES - whatever pass 1
    did with the file. `test_a_terminal_spec_costs_fewer_reads_than_a_non_terminal_one` is the
    one that can fail when the read guard goes (gh#358).
    """
    _spec_without_files(tmp_path, "gap", state)
    assert _files_warnings(tmp_path, caplog) == [], state


def test_the_files_warning_is_produced_by_the_new_check_and_nothing_else(
    tmp_path: Path, caplog, monkeypatch
):
    """(iii) MUTATION CONTROL. Reconstruct the pre-fix condition and prove test (i) FAILS.

    THREE arms, because there are three ways (i) could have been a test of nothing. The survey
    might already have warned about this file for some unrelated reason (arm 1 removes the warn
    call). The presence fact might have been arriving from somewhere other than the spec read
    wired into pass 1 (arm 2 makes that read always answer "declared"). Or the survey might be
    reporting whatever that read fails to call True (arm 3 makes it answer "could not decode",
    which must stay SILENT).

    Arms 1 and 2 are the original pair, unweakened: each still breaks the guard and demands the
    suite go red, rather than merely observing a changed value.

    ARM 3 IS NEW WITH gh#311 and pins what the collapse to `files_missing` put at risk. Before
    it, the tri-state survived pass 1 and warn time re-tested it as `declared[0] is not False`;
    now pass 1 folds it, and the fold is one `is False` at the single `_spec_declares_files`
    call site in pass 1. Written as a falsiness test
    - `if not _spec_declares_files(f)` - that fold would report an UNDECODABLE spec as one with
    no `## Files` section, sending the reader to add a heading to a file whose real problem is
    its encoding. Real bytes cannot demonstrate that (the parser rejects them ~50 lines earlier
    and the record never exists), so the only way to hold the fold to `is False` at the survey
    level is to inject the answer, which is what this arm does. It asserts silence rather than a
    red, so arm 1 and the bracketing greens are what stop it from passing vacuously.
    """
    spec = _spec_without_files(tmp_path, "gap", "drafted")
    _assert_one_warning_naming(tmp_path, caplog, spec, "gap")  # green WITH the fix

    with monkeypatch.context() as m:
        m.setattr(_survey, "_warn_specs_without_files", lambda records, files_missing: None)
        with pytest.raises(AssertionError):
            _assert_one_warning_naming(tmp_path, caplog, spec, "gap")

    with monkeypatch.context() as m:
        m.setattr(_survey, "_spec_declares_files", lambda path: True)
        with pytest.raises(AssertionError):
            _assert_one_warning_naming(tmp_path, caplog, spec, "gap")

    with monkeypatch.context() as m:
        m.setattr(_survey, "_spec_declares_files", lambda path: None)
        assert _files_warnings(tmp_path, caplog) == [], "unreadable was reported as missing"

    _assert_one_warning_naming(tmp_path, caplog, spec, "gap")  # and green again afterwards


@pytest.mark.parametrize("order", ["offender-first", "offender-second"])
def test_a_declaring_spec_does_not_silence_a_DIFFERENT_slug_that_lacks_one(
    tmp_path: Path, caplog, order: str
):
    """TWO GATED SLUGS, which nothing in this block had before (gh#311).

    Every other test here builds a board whose gated population is exactly ONE record: the
    fixture writes one spec, the declares-silent cases mutate that same spec, and the dup test
    writes two FILES sharing one SLUG, so it too merges to one record. That left the dict's
    cross-slug isolation and the warn loop's per-record skip completely unpinned, and a
    mutation hunt found three survivors living in that hole. Two of them silence the entire
    feature on a real board:

      `files_missing.pop(slug, None)` -> `files_missing.clear()`
          any spec that DOES declare a section erases every offender recorded before it, so
          only offenders sorting after the last declaring spec are ever reported. On the live
          corpus, where most specs now carry the section, that is nearly all of them.

      the warn loop's `continue` -> `return`
          one gated non-offender anywhere in `records` suppresses every warning, because the
          logging loop sits after it. A real board always has such a record.

    Both stayed green across all 2778 tests. Neither is exotic: they are the two most obvious
    ways to typo this exact pair of lines, and the reason they survived is that a
    one-record board cannot distinguish "skip this record" from "stop".

    Both orders run because the failures are order-sensitive - `clear()` needs the declaring
    spec to sort AFTER the offender to do its damage, and a fixture that only ever put them
    one way round would half-pin it.
    """
    specs = tmp_path / "projects" / "proj" / "design" / "specs"

    def _spec(name: str, slug: str, declares: bool) -> Path:
        body = (
            f"---\nslug: {slug}\ntitle: {slug} title\n"
            f"current_state: drafted\n---\n\n# {slug} title\n"
        )
        if declares:
            body += (
                "\n## Files\n\n"
                "- `agentflow/epic_survey.py` -- the warning\n"
            )
        return _write(specs / name, body)

    if order == "offender-first":
        offender = _spec("a-gap.md", "gap", declares=False)
        declaring = _spec("b-fine.md", "fine", declares=True)
    else:
        declaring = _spec("a-fine.md", "fine", declares=True)
        offender = _spec("b-gap.md", "gap", declares=False)

    msgs = _files_warnings(tmp_path, caplog)
    assert len(msgs) == 1, f"expected exactly the one offender, got {msgs}"
    assert str(offender) in msgs[0], msgs[0]
    # By PATH, not by slug substring: the message text itself contains the word "has", so a
    # naive `"has" not in msgs[0]` fails on the warning's own prose rather than on behaviour.
    assert str(declaring) not in msgs[0], msgs[0]


def test_an_unreadable_later_sibling_still_clears_an_earlier_slugs_omission(
    tmp_path: Path, caplog, monkeypatch
):
    """The `else` arm erases on None, not only on True (gh#311).

    The pre-change dict was assigned unconditionally for ALL THREE outcomes, so a later
    sibling answering "could not look" erased an earlier one's omission just as a declaring
    sibling did. Narrowing the new erase arm to `elif ... is True` keeps that omission instead,
    and warns about a slug whose deciding file is the one nobody could read. Nothing caught
    that: the dup test only covers False/True and True/False, so the None column is unpinned.

    The answer is injected because real bytes cannot reach it - `parse_spec_frontmatter` rejects
    undecodable content ~50 lines earlier and `continue`s, so the reachable route to None is an
    OS error racing the two reads, which a fixture cannot stage deterministically.
    """
    specs = tmp_path / "projects" / "proj" / "design" / "specs"
    head = (
        "---\nslug: dup\ntitle: dup title\n"
        "current_state: drafted\n---\n\n# dup title\n"
    )
    _write(specs / "a-dup.md", head)          # earlier: no `## Files`
    later = _write(specs / "b-dup.md", head)  # later: also none, but answers None

    real = _survey._spec_declares_files
    monkeypatch.setattr(
        _survey,
        "_spec_declares_files",
        lambda path: None if path.name == "b-dup.md" else real(path),
    )
    assert _files_warnings(tmp_path, caplog) == [], (
        "an unreadable LATER sibling must erase the earlier omission, as the pre-change dict did"
    )
    assert later.exists()


def test_the_files_answer_follows_the_MERGE_winner_not_glob_order(tmp_path: Path, caplog):
    """gh#359. Two rules picked different winners and the survey went silent on a real offender.

    `files_missing` is maintained in glob order; `_merge_spec_record` picks by lifecycle
    precedence. For one spec file they agree. Here they do not:

        a-dup.md   approved-for-autonomous, NO `## Files`   <- survives the merge
        b-dup.md   drafted,                 HAS `## Files`  <- wins glob order

    Before the fix the later file's section erased the earlier file's omission, so the record the
    survey RETURNS was gated, declared nothing, and produced no warning at all. The standing
    "no `## Files`, no promotion" Directive depends on that warning, so silence is the harm --
    not merely a wrong path in the message.
    """
    specs = tmp_path / "projects" / "proj" / "design" / "specs"
    head = "---\nslug: dup\ntitle: dup title\ncurrent_state: %s\n---\n\n# dup title\n"
    files = "\n## Files\n\n- `skills/_shared/agentflow/epic_survey.py` -- the section\n"

    offender = _write(specs / "a-dup.md", head % "approved-for-autonomous")
    _write(specs / "b-dup.md", (head % "drafted") + files)

    warnings = _files_warnings(tmp_path, caplog)
    assert warnings, (
        "the surviving record is gated and its own file declares no `## Files`, and the survey "
        "said nothing -- the promotion Directive has no signal here"
    )
    assert offender.name in warnings[0], (
        "the warning names a file other than the one the merge kept: " + warnings[0]
    )


def test_the_reverse_pairing_stays_silent_because_the_winner_DOES_declare(tmp_path: Path, caplog):
    """The other direction, which is what stops the fix over-reporting: precedence again picks
    the earlier file, but that file declares, so there is nothing to say -- even though the
    later, non-declaring sibling is the one glob order would have named."""
    specs = tmp_path / "projects" / "proj" / "design" / "specs"
    head = "---\nslug: dup\ntitle: dup title\ncurrent_state: %s\n---\n\n# dup title\n"
    files = "\n## Files\n\n- `skills/_shared/agentflow/epic_survey.py` -- the section\n"

    _write(specs / "a-dup.md", (head % "approved-for-autonomous") + files)
    _write(specs / "b-dup.md", head % "drafted")

    assert _files_warnings(tmp_path, caplog) == [], (
        "warned about a slug whose surviving record declares a `## Files` section"
    )


def test_a_tie_on_state_still_falls_to_glob_order(tmp_path: Path, caplog):
    """The boundary of the change, stated as a test so it cannot drift. When the two states are
    equal `_merge_spec_record` itself falls back to read order, so the later file IS the winner
    and gh#311's pinned behaviour is untouched. Without this, a future reader cannot tell which
    of the two rules the fix was meant to move."""
    specs = tmp_path / "projects" / "proj" / "design" / "specs"
    head = "---\nslug: dup\ntitle: dup title\ncurrent_state: drafted\n---\n\n# dup title\n"
    files = "\n## Files\n\n- `skills/_shared/agentflow/epic_survey.py` -- the section\n"

    _write(specs / "a-dup.md", head)              # earlier: no section
    _write(specs / "b-dup.md", head + files)      # later: declares -> erases, as before

    assert _files_warnings(tmp_path, caplog) == []


@pytest.mark.parametrize(
    "first_declares, expect_warning",
    [(False, False), (True, True), (None, True)],
    ids=["earlier-lacks--later-declares", "earlier-declares--later-lacks",
         "both-lack--later-is-named"],
)
def test_two_spec_files_sharing_a_slug_are_judged_by_the_LATER_file(
    tmp_path: Path, caplog, first_declares: bool | None, expect_warning: bool
):
    """The `files_missing.pop` arm, in both directions (gh#311).

    Two spec files can declare the same slug; pass 1 merges them into one record, and the
    presence dict was written UNCONDITIONALLY per file, so the last file read decided the
    answer. Recording offenders only breaks that by default: the second file's silence is now
    a no-op rather than an overwrite, so an earlier sibling's omission would stick and warn
    about a slug the pre-change code was silent on. The non-offending arm therefore erases, and
    what it restores is the OLD dict's semantics exactly - not a claim about the merged
    record, which `_merge_spec_record` picks by lifecycle precedence and not by read order.

    Deleting `files_missing.pop(slug, None)` turns `earlier-lacks--later-declares` red and
    leaves `earlier-declares--later-lacks` green, which is why both directions are here: the
    second case alone passes on the broken code.

    The third case is BOTH LACKING, added after a mutation hunt: with only the mixed pair,
    `files_missing[slug] = f` could be weakened to `setdefault` and nothing noticed, because
    the two cases that disagree about which of two offending files is named never both existed.

    Filenames are `a-` / `b-` prefixed so `sorted(root.glob(...))` reads them in that order:
    the fixture's whole meaning is the read order, so it is spelled rather than assumed.
    """
    specs = tmp_path / "projects" / "proj" / "design" / "specs"
    head = "---\nslug: dup\ntitle: dup title\ncurrent_state: drafted\n---\n\n# dup title\n"
    files = "\n## Files\n\n- `agentflow/epic_survey.py` -- the warning\n"
    # None means NEITHER declares - the column the original pair left out, where "last file
    # wins" is the only thing deciding which path the message names.
    first_body = head + (files if first_declares is True else "")
    second_body = head + (files if first_declares is False else "")
    first = _write(specs / "a-dup.md", first_body)
    second = _write(specs / "b-dup.md", second_body)

    msgs = _files_warnings(tmp_path, caplog)
    if not expect_warning:
        assert msgs == [], msgs
        return
    assert len(msgs) == 1, msgs
    assert str(second) in msgs[0], msgs[0]
    assert str(first) not in msgs[0], msgs[0]


def test_an_unreadable_spec_is_not_reported_as_missing_a_files_section(tmp_path: Path):
    """"Could not look" is not "has none", and a broken file already has its own line.

    A spec whose bytes will not decode is named through the `, skipping:` channel by the
    parser, which reads the file EARLIER and whose handler then skips the record entirely - so
    the UNDECODABLE-BYTES half of this arm is unreachable through `survey_epics`, and both
    assertions below pin it as a direct-caller guard rather than as an integration guarantee.
    Answering False here would complain about one file twice in two different vocabularies, and
    send the reader to add a heading to a file whose actual problem is its encoding.

    THE ARM AS A WHOLE IS NOT UNREACHABLE, and this docstring used to say it was. The same
    `None` is returned on `OSError`, and the parse read and the declares read are two separate
    reads of one path - so a spec locked by another lane between them reaches it from inside
    `survey_epics`, which is why the fold at the call site is `is False` and not falsiness.
    Undecodable bytes are the half no caller can reach; a failed read is not (gh#311).
    """
    assert _survey._spec_declares_files(tmp_path / "nope.md") is None
    p = _write(tmp_path / "bytes.md", "x")
    p.write_bytes(b"\xff\xfe---\x00\x81\x8f\xfe\xff current_state: drafted\n")
    assert _survey._spec_declares_files(p) is None


def test_a_missing_section_is_reported_against_the_SPEC_even_when_a_plan_wins_source_path(
    tmp_path: Path, caplog
):
    """The reason presence is decided in pass 1 rather than re-read at warn time.

    The plan pass overwrites `source_path`, and it supplies `current_state` too - so a spec
    sitting at `speccing` can be carried INTO the flagged population by its plan. A warn-time
    re-read of `source_path` would open a PLAN for those records and report the plan as a spec
    with no `## Files`, which is a lie the reader cannot act on.
    """
    spec = _write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "gap.md",
        "---\nslug: gap\ntitle: gap title\ncurrent_state: speccing\n---\n\nno files heading\n",
    )
    plan = make_plan(tmp_path, "proj", "gap", frontmatter=APPROVED)

    msgs = _files_warnings(tmp_path, caplog)
    assert len(msgs) == 1, msgs
    assert str(spec) in msgs[0], msgs[0]
    assert str(plan) not in msgs[0], msgs[0]


# --------------------------------------------------------------------------- #
# The template half of the pair: a spec written from the template must SATISFY
# the check above. Nothing else tests that the two changes agree.
# --------------------------------------------------------------------------- #
def test_the_gap_template_SKELETON_carries_a_files_section():
    """The edit has to be inside the fence the auditor copies, not in the prose about it."""
    skeleton = _gap_template_skeleton()
    assert files_section(skeleton).present, (
        f"{GAP_TEMPLATE} skeleton has no Files heading, so every spec written from it is "
        "unpromotable by construction (gh#294)"
    )


def test_a_spec_written_from_the_gap_template_surveys_WITHOUT_the_files_warning(
    tmp_path: Path, caplog
):
    """End-to-end: template -> spec on disk -> survey. The whole point of shipping the pair.

    The skeleton is written verbatim apart from the one placeholder the survey reads, so this
    fails if the heading is ever dropped, renamed, or moved out of the fence.
    """
    skeleton = _gap_template_skeleton().replace("<gap-slug>", "gap-from-template")
    spec = _write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "gap-from-template.md",
        skeleton,
    )

    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert _by_slug(recs)["gap-from-template"].current_state == "drafted"
    assert _files_warnings(tmp_path, caplog) == []

    # ...and the same spec with the template's new section removed DOES warn, so the silence
    # above is the section doing work rather than the fixture being unreachable.
    spec.write_text(skeleton[: skeleton.index("## Files")], encoding="utf-8")
    msgs = _files_warnings(tmp_path, caplog)
    assert len(msgs) == 1, msgs
    assert "gap-from-template" in msgs[0], msgs[0]


# --------------------------------------------------------------------------- #
# ONE TERMINAL VOCABULARY (gh#261)
#
# `_done_slugs` restated a SUBSET of `TERMINAL_CURRENT_STATES` - ("done", "archived") - so a
# prerequisite at `superseded` or `shipped` was terminal to every other reader in the module
# and invisible to the dependency math. Its dependents stranded silently and permanently, and
# `blocked_status` reported the strand as CHAINED: "deeper queue ahead, it will unblock".
#
# ZERO LIVE VICTIMS were measured when this was fixed (578 records across all projects, 40 of
# them terminal-but-not-done, nothing newly ready either way). These tests exist because the
# TRIGGER is the sanctioned remedy - split a too-wide parent, mark the parent `superseded` -
# so the defect recurs by doing the right thing, and it is silent when it does.
#
# Every test here is parametrized over the vocabulary READ FROM the module rather than a list
# copied into this file: a guard that hand-copies the set it guards is only ever as wide as
# the day it was written, which is how this hole opened.
# --------------------------------------------------------------------------- #
# The terminal states that were NOT in the old hand-written done-set. Derived by subtraction
# so that adding a fifth terminal value to the module puts it under test here automatically.
NEWLY_COUNTED_TERMINAL_STATES = sorted(TERMINAL_CURRENT_STATES - {"done", "archived"})

# States that are genuinely unfinished - the false-positive control's material. `drafted` and
# `approved-for-autonomous` are here on purpose: they are real corpus values that reach
# `blocked_status`'s CHAINED fall-through, where CHAINED is the HONEST answer, and nothing in
# this fix may start counting them satisfied.
UNFINISHED_STATES = ["speccing", "planning", "running", "drafted", APPROVED_CURRENT_STATE]


def _narrow_done_slugs(records):
    """`_done_slugs` exactly as it stood before gh#261 - the mutation, kept in one place."""
    return {r.slug for r in records if r.lifecycle_state in ("done", "archived")}


@pytest.mark.parametrize("dep_state", NEWLY_COUNTED_TERMINAL_STATES)
def test_a_terminal_but_not_done_prerequisite_satisfies_its_dependent(dep_state: str):
    """(i) The defect, pinned on the states the old subset omitted.

    Asserted on all three surfaces the strand appeared on, because they are three different
    readers of the same set and the bug reached the operator through the third.
    """
    recs = _dep_pair(dep_state)
    assert "kid" in [r.slug for r in ready_set(recs)]
    assert [r.slug for r in selectable(recs)] == ["kid"]
    assert blocked_status(recs[1], recs) == "READY"


@pytest.mark.parametrize("dep_state", FINISHED_STATES)
def test_blocked_status_calls_a_terminal_dependency_READY_and_never_CHAINED(dep_state: str):
    """The REPORTING half, stated as the thing the operator actually reads.

    CHAINED promises "this unblocks when the queue ahead drains". For a terminal dep that is
    not merely imprecise, it is false in the direction that costs the most: it reads as
    ordinary sequencing, so nobody investigates. Pinned for the whole vocabulary at once.
    """
    recs = _dep_pair(dep_state)
    assert blocked_status(recs[1], recs) == "READY"


@pytest.mark.parametrize("dep_state", UNFINISHED_STATES)
def test_an_unfinished_prerequisite_is_still_blocked(dep_state: str):
    """(ii) THE FALSE-POSITIVE CONTROL, without which (i) is satisfied by satisfying everything.

    A done-set that returned every slug would pass every assertion above. This is what makes
    those assertions mean "the vocabulary widened" instead of "the gate opened".
    """
    recs = _dep_pair(dep_state)
    # Asserted on `kid` rather than on an empty result: an `approved-for-autonomous` dep is
    # itself claimable and legitimately appears in both lists. What must never appear is the
    # epic waiting on it.
    assert "kid" not in [r.slug for r in ready_set(recs)]
    assert "kid" not in [r.slug for r in selectable(recs)]
    assert blocked_status(recs[1], recs) != "READY"


@pytest.mark.parametrize("dep_state", NEWLY_COUNTED_TERMINAL_STATES)
def test_MUTATION_CONTROL_the_old_narrow_done_set_strands_the_dependent(
    dep_state: str, monkeypatch
):
    """(iii) Revert the fix in-process and show the strand return, on all three surfaces.

    A green suite proves the tests pass; it does not prove they would have FAILED before. This
    restores the pre-gh#261 `_done_slugs` and asserts the exact bug back into existence -
    including the CHAINED misreport, verbatim - so a future edit that narrows the vocabulary
    again cannot pass by simply making these tests unreachable.
    """
    recs = _dep_pair(dep_state)
    assert [r.slug for r in selectable(recs)] == ["kid"]      # fixed behaviour, same objects

    monkeypatch.setattr(_survey, "_done_slugs", _narrow_done_slugs)

    assert "kid" not in [r.slug for r in ready_set(recs)]     # stranded again
    assert selectable(recs) == []
    assert blocked_status(recs[1], recs) == "CHAINED"         # ...and reported as merely queued


def test_the_done_set_is_the_terminal_vocabulary_and_not_a_copy_of_it():
    """The rule itself, not one of its consequences.

    Every other test here can be satisfied by widening the literal tuple by hand once. This one
    fails the next time the two definitions are allowed to be two - which is the actual defect
    gh#261 named, and the reason `shipped` is in the set even though nothing depends on one.
    """
    recs = [
        EpicRecord(slug=state, title=state, lifecycle_state=state, current_state=state)
        for state in sorted(TERMINAL_CURRENT_STATES)
    ]
    assert _survey._done_slugs(recs) == set(TERMINAL_CURRENT_STATES)


# --------------------------------------------------------------------------- #
# warn_stranded_terminal_dependencies - the strand becomes audible
#
# The gh#261 victim was found by hand-resolving six dependency slugs, because no seat reported
# it. This channel is SILENT on a healthy corpus by construction (`lifecycle_state` equals the
# normalised `current_state` for 552 of 578 live records), and speaks the day the two diverge -
# which is the one way the done-set fix can be quietly undone.
# --------------------------------------------------------------------------- #
STRANDED = "stranded depends_on: "


def _stranded_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if STRANDED in r.getMessage()]


def _diverged_pair(current: str = "superseded", lifecycle: str = "running"):
    """A dep whose frontmatter says FINISHED while its reconciled lifecycle says otherwise.

    This is pass 3's shape, not an invented one: a status.md drives `lifecycle_state` while
    `current_state` is carried through untouched, so a superseded spec with a live status board
    reads exactly like this - and strands every dependent, silently, as before the fix.
    """
    return [
        EpicRecord(slug="dep", title="d", lifecycle_state=lifecycle, current_state=current),
        _approved("kid", depends_on=["dep"]),
    ]


@pytest.mark.parametrize("current", NEWLY_COUNTED_TERMINAL_STATES)
def test_a_stranded_epic_is_named_once_with_both_states_and_the_dep(current: str, caplog):
    """(iv-a) It FIRES, and the line carries everything needed to act without re-deriving it."""
    recs = _diverged_pair(current=current)
    assert warn_stranded_terminal_dependencies(recs) == [("kid", "dep")]
    msgs = _stranded_warnings(caplog)
    assert len(msgs) == 1, msgs
    assert "'kid'" in msgs[0] and "'dep'" in msgs[0]
    assert repr(current) in msgs[0] and "'running'" in msgs[0]


@pytest.mark.parametrize("dep_state", FINISHED_STATES)
def test_the_channel_is_SILENT_on_a_healthy_terminal_dependency(dep_state: str, caplog):
    """(iv-b) The whole design. A dep whose two states AGREE is satisfied, not stranded.

    This is why the channel measures zero on the live corpus: no record there is terminal by
    `current_state` while non-terminal by `lifecycle_state` (0 of 578). General divergence is
    26 of 578 - 25 with `current_state` unset, and one record at
    `current=approved-for-autonomous` / `lifecycle=running`. That single record is pass 3's
    shape exactly, which is why the mechanism this channel guards is live today rather than
    hypothetical: it is one terminal value away. A warning
    that fired on ordinary finished work would be the noise that trains a reader to skip it.
    """
    recs = _dep_pair(dep_state)
    assert warn_stranded_terminal_dependencies(recs) == []
    assert _stranded_warnings(caplog) == []


@pytest.mark.parametrize("dep_state", UNFINISHED_STATES)
def test_the_channel_is_SILENT_on_an_ordinary_unfinished_dependency(dep_state: str, caplog):
    """(iv-b, control) Ordinary sequencing is not a strand and must never be reported as one.

    `drafted` and `approved-for-autonomous` are live corpus values that reach the same CHAINED
    fall-through this channel exists to explain; ten deps sit there today. CHAINED is the
    honest answer for them, so silence here is what keeps the channel worth reading.
    """
    recs = _dep_pair(dep_state)
    assert warn_stranded_terminal_dependencies(recs) == []
    assert _stranded_warnings(caplog) == []


def test_a_terminal_carrier_is_not_reported_as_stranded(caplog):
    """A finished epic's stale `depends_on` strands nothing, and would print forever."""
    recs = _diverged_pair()
    recs[1] = EpicRecord(slug="kid", title="k", lifecycle_state="done",
                         current_state="done", depends_on=["dep"])
    assert warn_stranded_terminal_dependencies(recs) == []
    assert _stranded_warnings(caplog) == []


def test_a_foreign_dep_is_left_to_the_forbidden_channel(caplog):
    """One misfiling, one line.

    A cross-project dep is unsatisfiable whatever its state and
    `forbidden_cross_project_dependencies` already names it - reporting it here as well would
    call a scoping problem a vocabulary drift.
    """
    dep = EpicRecord(slug="dep", title="d", lifecycle_state="running",
                     current_state="superseded", project="beta")
    kid = _approved("kid", depends_on=["dep"], project="alpha")
    assert warn_stranded_terminal_dependencies([dep, kid]) == []
    assert _stranded_warnings(caplog) == []


def test_an_absent_dep_is_left_to_the_unresolvable_channel(caplog):
    """The third neighbour: a dep not in the records handed in is not this channel's case."""
    kid = _approved("kid", depends_on=["never-surveyed"])
    assert warn_stranded_terminal_dependencies([kid]) == []
    assert _stranded_warnings(caplog) == []


def test_the_stranded_channel_caps_its_lines_and_summarises_the_rest(caplog):
    """The neighbours' budget, for the neighbours' reason - and the overflow names who is hidden.

    The colon in STRANDED is load-bearing: it matches the per-pair lines and not the summary,
    so the two can be counted apart.
    """
    dep = EpicRecord(slug="dep", title="d", lifecycle_state="running",
                     current_state="superseded")
    kids = [_approved(f"kid{i}", depends_on=["dep"]) for i in range(7)]
    assert len(warn_stranded_terminal_dependencies([dep, *kids])) == 7
    assert len(_stranded_warnings(caplog)) == 5
    overflow = [
        r.getMessage() for r in caplog.records if "more stranded depends_on" in r.getMessage()
    ]
    assert len(overflow) == 1, overflow
    assert "kid5" in overflow[0] and "kid6" in overflow[0]


def test_selectable_speaks_the_stranded_channel_over_the_WHOLE_corpus(caplog):
    """The call site, pinned. `ready_set` is about to hide `kid`; the warning has to precede it.

    Without this the channel exists and nothing calls it, which is the state the defect was
    found in - correct code, no reader.
    """
    recs = _diverged_pair()
    assert selectable(recs) == []          # the strand is real here, not hypothetical
    msgs = _stranded_warnings(caplog)
    assert len(msgs) == 1, msgs
    assert "'kid'" in msgs[0]


# --------------------------------------------------------------------------- #
# Pass 3 and the board statuses that end a run (gh#678)
#
# Pass 3 read every board status except `done` as `running`, so an abandoned or failed epic
# stayed "in flight" forever and its dependants read NEAR-READY against a parent that would
# never land. Those statuses now pass through as `lifecycle_state` and strand their dependants
# audibly; `done_on_hold` passes through too, and simply stops claiming to be running.
# --------------------------------------------------------------------------- #
def _ended_board_corpus(tmp_path: Path, status: str) -> list[EpicRecord]:
    make_spec(tmp_path, "proj", "alpha", frontmatter=APPROVED)
    make_spec(tmp_path, "proj", "eps", frontmatter=APPROVED + "depends_on: [alpha]\n")
    make_status(tmp_path, "proj", "alpha", status=status)
    return survey_epics(tmp_path, PAST_CUTOFF)


@pytest.mark.parametrize("status", ["abandoned", "failed", "done_on_hold"])
def test_pass3_carries_an_ended_board_status_through_instead_of_running(tmp_path, status):
    recs = _by_slug(_ended_board_corpus(tmp_path, status))
    assert recs["alpha"].lifecycle_state == status
    assert "alpha" not in _survey._done_slugs(list(recs.values()))


def test_pass3_still_reads_a_live_board_as_running(tmp_path):
    """Control: only the ended statuses changed; every other board status is still in flight."""
    for status in ("running", "escalation", "blocked", "awaiting_merge_decision"):
        recs = _by_slug(_ended_board_corpus(tmp_path, status))
        assert recs["alpha"].lifecycle_state == "running", status


@pytest.mark.parametrize("status", sorted(_survey.TERMINAL_NOT_DONE_BOARD_STATUSES))
def test_an_abandoned_or_failed_parent_strands_its_dependant_audibly(tmp_path, status, caplog):
    recs = _ended_board_corpus(tmp_path, status)
    eps = _by_slug(recs)["eps"]
    assert blocked_status(eps, recs) == "MISSING-DEP"      # never NEAR-READY / CHAINED
    assert "eps" not in [r.slug for r in ready_set(recs)]
    assert warn_stranded_terminal_dependencies(recs) == [("eps", "alpha")]
    msgs = _stranded_warnings(caplog)
    assert len(msgs) == 1, msgs
    assert "'eps'" in msgs[0] and "'alpha'" in msgs[0] and repr(status) in msgs[0]


def test_an_abandoned_parent_outranks_a_running_sibling_dep_in_blocked_status(tmp_path):
    """One dep that will never land makes the whole epic stranded, whatever the others do."""
    _ended_board_corpus(tmp_path, "abandoned")
    make_spec(tmp_path, "proj", "beta", frontmatter=APPROVED)
    make_status(tmp_path, "proj", "beta", status="running")
    make_spec(tmp_path, "proj", "eps", frontmatter=APPROVED + "depends_on: [alpha, beta]\n")
    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert blocked_status(_by_slug(recs)["eps"], recs) == "MISSING-DEP"


def test_a_done_on_hold_parent_is_neither_running_nor_stranded(tmp_path, caplog):
    """A held PR can still merge, so its dependant waits quietly - but not as NEAR-READY."""
    recs = _ended_board_corpus(tmp_path, "done_on_hold")
    assert blocked_status(_by_slug(recs)["eps"], recs) == "CHAINED"
    assert warn_stranded_terminal_dependencies(recs) == []
    assert _stranded_warnings(caplog) == []


# --------------------------------------------------------------------------- #
# Undecodable bytes that DECODE ANYWAY (issue #239)
#
# The block above (#80) is about bytes that RAISE. This one is about the strictly worse
# neighbour: bytes that do not. NUL is a legal UTF-8 byte, so a BOM-less UTF-16LE spec decodes
# without error into mojibake and never reaches a single decode-layer guard in this module. It
# then misses the `^---` regex and surveys as an ordinary frontmatter-less `speccing` record,
# with NO warning emitted anywhere - and a plan sidecar promotes it. Issue #80 crashed, loudly.
# This produced a dispatchable record wearing a state nobody wrote, off a file that reads
# `current_state: done` in any editor.
#
# Three items, all three measured to reproduce on the tree these tests were written against.
# --------------------------------------------------------------------------- #
def _utf16_bytes(text: str) -> bytes:
    """*text* as BOM-LESS UTF-16LE - AN encoding that defeats every strict reader here.

    Not the only one: BOM-less UTF-16BE of ASCII text is also all bytes below 0x80 and decodes
    just as cleanly as UTF-8. The NUL sniff is endian-agnostic and catches both; LE is used here
    because it is what a Windows editor writes.

    BOM-bearing UTF-16 is already handled: `\\xff\\xfe` is not valid UTF-8, so the parsers raise
    and the file is named in the skip channel. Drop the BOM and the same file decodes CLEANLY as
    UTF-8, which is the whole defect. `write_bytes` rather than `_write`, which encodes UTF-8
    and so cannot express this.
    """
    return text.encode("utf-16-le")


def _utf16_spec(root: Path, slug: str, *, current_state: str = "done") -> Path:
    """A perfectly well-formed spec - correct frontmatter, `## Files` section - written UTF-16LE.

    Nothing about the CONTENT is broken. Every field a human reads in this file is the field
    they typed. Only the encoding is wrong, and on this Windows fleet that is one `>` redirect.
    """
    text = (
        f"---\nslug: {slug}\ntitle: {slug} title\n"
        f"current_state: {current_state}\n---\n\n## Files\n- `x.py`\n"
    )
    path = root / "projects" / "proj" / "design" / "specs" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_utf16_bytes(text))
    return path


def _skip_lines(caplog) -> list:
    """The records the fleet report counts: the `, skipping:` template, structurally."""
    return [r for r in caplog.records if ", skipping:" in str(r.msg)]


def test_a_BOMLESS_utf16_spec_DECODES_CLEAN_and_is_broken_anyway(tmp_path: Path):
    """The premise, pinned first, because it is the part that surprises every reader.

    If these bytes raised, #80's machinery would already have caught them and there would be no
    issue #239. They do not raise. So no `except` anywhere can be the fix, and the question has
    to be asked of the BYTES by the function that already owns it: is this file broken, or is it
    merely frontmatter-less.
    """
    spec = _utf16_spec(tmp_path, "quiet")

    # It decodes. That is the defect in one line.
    assert spec.read_text(encoding="utf-8")

    # And the parser still refuses it, so pass 1 lands in the `SpecFormatError` arm...
    with pytest.raises(Exception):
        parse_spec_frontmatter(spec)
    # ...where this is the function that decides which of the two unlike files it is.
    assert _survey._opens_a_frontmatter_block(spec) is True


def test_a_BOMLESS_utf16_spec_CANNOT_be_promoted_by_its_plan_sidecar(tmp_path: Path, caplog):
    """THE consequence the issue is actually about - not the warning, the dispatchable record.

    The spec says `current_state: done`. Before the widening it surveyed as a frontmatter-less
    `speccing` record with `current_state=None`, so the terminal-state freeze in the plan pass
    (which reads `existing.current_state`) did not fire, the sidecar's `approved-for-autonomous`
    won unconditionally, and the epic came back SELECTABLE and DISPATCHABLE. Shipped work
    re-offered to a lane, off a file every human reads as finished: issue #42 through a door
    that raises no exception on the way in.

    It is dropped instead, through the path that already existed - `_opens_a_frontmatter_block`
    answers True, pass 1 registers the file in `broken_spec_states`, and the plan pass fails
    closed on it. And it is NAMED: a bare drop would be issue #122's stranding class wearing
    this fix's clothes.
    """
    spec = _utf16_spec(tmp_path, "shipped")
    make_plan(tmp_path, "proj", "shipped", frontmatter="current_state: approved-for-autonomous\n")
    make_spec(tmp_path, "proj", "healthy", frontmatter="current_state: drafted\n")
    make_plan(tmp_path, "proj", "healthy", frontmatter="current_state: approved-for-autonomous\n")

    with caplog.at_level("WARNING"):
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    assert "shipped" not in _by_slug(recs)
    assert {r.slug for r in selectable(recs)} == {"healthy"}
    assert {r.slug for r in dispatchable(recs)} == {"healthy"}

    # One line, in the shape the fleet report parses structurally (marker + exactly two args),
    # and once for the spec rather than once per sidecar pointing at it.
    skips = _skip_lines(caplog)
    assert len(skips) == 1, [r.getMessage() for r in skips]
    assert str(skips[0].msg).startswith("unusable spec frontmatter")
    assert isinstance(skips[0].args, tuple) and len(skips[0].args) == 2
    assert Path(str(skips[0].args[0])) == spec


def test_the_utf16_widening_does_not_fire_on_ordinary_specs(tmp_path: Path, caplog):
    """The false-positive control: the widened byte test must not eat the corpus.

    Two shapes it would be easy to break. A frontmatter-less spec is an ordinary corpus shape
    and has always been a promotable `speccing` record - failing closed on prose would strand
    real work, which is the bug pointing the other way. And a healthy UTF-8 spec must be
    untouched, warning included: a fix that reports every file is a fix nobody can read.
    """
    _write(tmp_path / "projects" / "proj" / "design" / "specs" / "prose.md", "just prose, no yaml\n")
    make_plan(tmp_path, "proj", "prose", frontmatter="current_state: approved-for-autonomous\n")
    make_spec(tmp_path, "proj", "utf8", frontmatter="current_state: approved-for-autonomous\n")

    with caplog.at_level("WARNING"):
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    assert _by_slug(recs)["prose"].current_state == "approved-for-autonomous"
    assert {r.slug for r in selectable(recs)} == {"prose", "utf8"}
    assert _skip_lines(caplog) == []


def test_an_EMPTY_spec_is_still_frontmatterless_not_utf16(tmp_path: Path):
    """The boundary the sniff reads past by one byte if it is written carelessly.

    An empty file has no NUL and no BOM, so it must stay False. It shares the `SpecFormatError`
    arm with the UTF-16 file and nothing else about it is broken.
    """
    p = tmp_path / "empty.md"
    p.write_bytes(b"")
    assert _survey._opens_a_frontmatter_block(p) is False


# --------------------------------------------------------------------------- #
# Item 2: a status board nobody could read (issue #239)
# --------------------------------------------------------------------------- #
def _poison_status(root: Path, slug: str, payload: bytes) -> Path:
    path = root / "projects" / "proj" / "status" / f"{slug}.status.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


_HEALTHY_STATUS = (
    "---\nepic: parent epic\nplan: /plans/parent.md\n"
    "started: 2026-01-01T00:00:00+00:00\nstatus: done\ncurrent_phase: phase-1\n"
    "paused: false\ntasks:\n  1:\n    state: done\n---\n\nstatus body\n"
)


@pytest.mark.parametrize(
    "payload, label",
    [
        (_utf16_bytes(_HEALTHY_STATUS), "utf16-bomless"),
        (codecs.BOM_UTF16_LE + _utf16_bytes(_HEALTHY_STATUS), "utf16-bom"),
        (b"\xff\xfe\x80\x81---\nepic: x\n", "raw-bad-bytes"),
    ],
)
def test_an_unreadable_status_file_is_NAMED_not_silently_dropped(
    tmp_path: Path, caplog, payload: bytes, label: str
):
    """"Not an orchestrator status" and "could not read it" were the same answer. They are not.

    `is_orchestrator_status` decoded with `errors="replace"`, so undecodable bytes became
    mojibake, the mojibake failed the `---` test, and the file was dropped before `read_status`
    was ever called - so the `malformed status, skipping` channel could not fire either. Zero
    lines, for a file the survey silently refused to look at.

    Reported through the same `, skipping: %s (%s)` template every other skip uses, because the
    fleet report reads these records structurally: a message saying the same thing in another
    shape counts as zero skips, which is invisibility wearing the channel's clothes.
    """
    bad = _poison_status(tmp_path, "parent", payload)

    with caplog.at_level("WARNING"):
        assert is_orchestrator_status(bad) is False, label

    skips = _skip_lines(caplog)
    assert len(skips) == 1, (label, [r.getMessage() for r in skips])
    assert str(skips[0].msg).startswith("unreadable status file"), label
    assert isinstance(skips[0].args, tuple) and len(skips[0].args) == 2, label
    assert Path(str(skips[0].args[0])) == bad, label


def test_an_unreadable_status_board_no_longer_strands_its_dependants_SILENTLY(
    tmp_path: Path, caplog
):
    """The cost is a stranded dependant, not a missing progress bar - so the file gets named.

    A status board is the only thing that can make an epic `done` WHEN ITS SPEC DOES NOT
    ALREADY SAY SO - which is why this fixture pins `parent` at `current_state: drafted`.
    A spec reading `current_state: done` is terminal with no status board involved. For the
    drafted population, drop the board and the epic is not terminal, `_done_slugs` does not
    contain it, and every dependant is held back with nothing anywhere saying why - issue
    #122's class, re-entered through the decode layer.

    The strand is still real here, and this fix does not repair it: an unreadable file cannot be
    made to say `done`. What changes is that it is ATTRIBUTABLE - the line names the exact file
    to re-save, instead of leaving a lane staring at a child that will not go ready.
    """
    bad = _poison_status(tmp_path, "parent", _utf16_bytes(_HEALTHY_STATUS))
    make_spec(tmp_path, "proj", "parent", frontmatter="current_state: drafted\n")
    make_spec(
        tmp_path, "proj", "child",
        frontmatter="current_state: approved-for-autonomous\ndepends_on: [parent]\n",
    )

    with caplog.at_level("WARNING"):
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    assert _by_slug(recs)["parent"].lifecycle_state != "done"      # the strand, unrepaired
    assert "child" not in {r.slug for r in ready_set(recs)}
    skips = _skip_lines(caplog)
    assert len(skips) == 1, [r.getMessage() for r in skips]
    assert Path(str(skips[0].args[0])) == bad


def test_a_BOM_PREFIXED_status_board_is_READ_and_releases_its_dependant(tmp_path: Path, caplog):
    """The likeliest real shape on this fleet, asserted by its CONSEQUENCE (gh#336).

    A UTF-8 BOM decodes fine, so none of the unreadable arms fire - but the decoded BOM is a
    `U+FEFF` sitting ahead of the `---`. It used to defeat the signature gate (the board dropped
    with no line at all), and once that gate stripped it, it still defeated `read_status`'s own
    frontmatter parse one layer down: the board was named as `malformed status, skipping`, and
    `parent` stayed non-terminal with `child` held out of `ready_set`. Named, and still stranded.

    So this asserts what the board is FOR - `parent` goes `done`, `child` goes ready - rather than
    that a line mentions the file. A test counting skip lines passes on a fix that names the
    board instead of reading it, which is exactly what the first fix shipped.
    """
    _poison_status(tmp_path, "parent", b"\xef\xbb\xbf" + _HEALTHY_STATUS.encode("utf-8"))
    make_spec(tmp_path, "proj", "parent", frontmatter="current_state: drafted\n")
    make_spec(
        tmp_path, "proj", "child",
        frontmatter="current_state: approved-for-autonomous\ndepends_on: [parent]\n",
    )

    with caplog.at_level("WARNING"):
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    assert _by_slug(recs)["parent"].lifecycle_state == "done"
    assert "child" in {r.slug for r in ready_set(recs)}
    assert _skip_lines(caplog) == [], [r.getMessage() for r in caplog.records]


def test_read_status_strips_the_BOM_itself_not_only_behind_the_survey_gate(tmp_path: Path):
    """The parser's own half, pinned without the survey in front of it (gh#336).

    `is_orchestrator_status` and `read_status` both read status text, and the defect was that only
    the first stripped the BOM. A direct read is the case with no gate ahead of it at all.
    """
    from agentflow.status_writer import read_status

    board = _poison_status(tmp_path, "parent", b"\xef\xbb\xbf" + _HEALTHY_STATUS.encode("utf-8"))
    state = read_status(board)
    assert state.status == "done"
    assert state.epic == "parent epic"


def test_a_HEALTHY_status_board_is_never_called_unreadable(tmp_path: Path, caplog):
    """The false-positive control, and the one a naive strict decode would fail.

    The signature test reads a FIXED 2KB slice, so a multi-byte character straddling that
    boundary is a truncation of the READ, not corruption of the file. A plain
    `raw.decode("utf-8")` would raise on it and report a perfectly good status board as
    unreadable - which is the stranding failure again, manufactured by the fix.
    """
    head = (
        "---\nepic: big epic\nplan: /plans/big.md\nstarted: 2026-01-01T00:00:00+00:00\n"
        "status: done\ncurrent_phase: phase-1\npaused: false\ntasks:\n  1:\n    state: done\n"
        "---\n\n"
    )
    pad = _survey._STATUS_HEAD_BYTES - 1 - len(head.encode("utf-8"))
    assert pad > 0
    # The last byte of the slice is the FIRST byte of a two-byte character.
    body = head + ("x" * pad) + "\u00e9" + " tail\n"
    raw = body.encode("utf-8")
    # `write_bytes`, not `_write`: text mode translates `\n` to `\r\n` on this fleet's platform,
    # which would silently move the boundary this test exists to land on and leave the naive
    # strict decode passing. The offset is then asserted rather than assumed.
    big = tmp_path / "projects" / "proj" / "status" / "big.status.md"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(raw)
    head_slice = raw[: _survey._STATUS_HEAD_BYTES]
    assert len(head_slice) == _survey._STATUS_HEAD_BYTES
    assert head_slice[-1] == 0xC3  # a lead byte, with its continuation byte outside the slice
    with pytest.raises(UnicodeDecodeError):
        head_slice.decode("utf-8")

    with caplog.at_level("WARNING"):
        assert is_orchestrator_status(big) is True
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    assert _by_slug(recs)["big"].lifecycle_state == "done"
    assert _skip_lines(caplog) == []


# --------------------------------------------------------------------------- #
# Item 3: the pin `_read_sidecar`'s narrow `except` clause never had (issue #239)
# --------------------------------------------------------------------------- #
def test_a_MemoryError_from_the_sidecar_read_PROPAGATES(tmp_path: Path, monkeypatch):
    """Mutating the clause to `except BaseException` left the whole suite green. It does not now.

    `_read_sidecar` catches `(OSError, UnicodeDecodeError)` on purpose, and the narrowness is
    the safety property: this handler does NOT report. It returns "no priority, no domain, no
    project, no queue_class, no approver", so anything it swallows that the following parse
    would not also raise becomes a confidently wrong survey answer rather than a skip line.

    The damage is specific and worth naming. The sidecar read happens ABOVE the per-file `try`,
    while `current_state` arrives from a separate parse below it - so a swallowed error costs
    the record its gates and nothing else: `queue_class=None` passes `dispatchable()`'s
    allow-list and `domain=None` / `project=None` defeat the exclusion filter. A retired-class
    or banned-domain epic comes back approved and dispatchable, with no line anywhere.

    `MemoryError` is the issue's named case; `KeyboardInterrupt` is the likelier one on a fleet
    that runs long surveys. Injected as a ONE-SHOT failure so the retry succeeds: that is what
    makes the mutant's answer a plausible-looking record instead of an obvious crash, and it is
    the state the assertion has to be able to tell apart.
    """
    spec = make_spec(tmp_path, "proj", "huge", frontmatter="current_state: drafted\npriority: 2\n")
    real_read_text = Path.read_text
    fired: list[Path] = []

    def one_shot_memory_error(self, *args, **kwargs):
        if self == spec and not fired:
            fired.append(self)
            raise MemoryError("injected: the file did not fit in memory")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", one_shot_memory_error)

    with pytest.raises(MemoryError):
        survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert fired, "the injection never fired - the test proves nothing about the clause"


def test_the_sidecar_clause_still_ABSORBS_what_the_parser_will_re_raise(tmp_path: Path, caplog):
    """The other half of the pin: narrow, not absent.

    The two classes it does catch are the two the parse one line later raises again, so the file
    is named and COUNTED rather than crashing the survey (issue #80). Tightening the clause to
    fix the mutant above would re-open that, so both directions are asserted together.
    """
    make_spec(tmp_path, "proj", "healthy", frontmatter="current_state: drafted\n")
    bad = _undecodable(tmp_path / "projects" / "proj" / "design" / "specs" / "bad.md")

    with caplog.at_level("WARNING"):
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    assert sorted(_by_slug(recs)) == ["healthy"]
    skips = _skip_lines(caplog)
    assert len(skips) == 1
    assert Path(str(skips[0].args[0])) == bad


@pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt])
def test_the_spec_files_clause_PROPAGATES_what_it_must_not_swallow(
    tmp_path: Path, monkeypatch, exc_type: type
):
    """gh#337: `_spec_declares_files`' narrow `except` had no test behind it.

    The sidecar pin above fires on the FIRST read of a spec, so it never reaches this clause: the
    survey is already dead by then. Widening this one to `except BaseException` left the suite
    green, and what it would swallow is specific - a `KeyboardInterrupt` during a corpus-wide
    survey becomes `None`, "could not look", so an interrupted run reads as a finished one with
    some specs quietly unjudged.

    Called directly, so the injection lands on the read this clause guards and no other.
    """
    spec = _write(tmp_path / "s.md", "---\nslug: s\n---\n\n## Files\n\n- `a/b.py`\n")
    real_read_text = Path.read_text

    def raising(self, *args, **kwargs):
        if self == spec:
            raise exc_type("injected")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", raising)
    with pytest.raises(exc_type):
        _survey._spec_declares_files(spec)


def test_the_spec_files_clause_still_answers_None_for_a_failed_read(
    tmp_path: Path, monkeypatch
):
    """The other half of gh#337's pin: narrow, not absent. A read that fails with `OSError` - a
    spec locked by another lane between the parse and this check - is "could not look", which
    the survey's fold keeps apart from "has no section"."""
    spec = _write(tmp_path / "s.md", "---\nslug: s\n---\n\n## Files\n\n- `a/b.py`\n")
    real_read_text = Path.read_text

    def locked(self, *args, **kwargs):
        if self == spec:
            raise PermissionError("injected: locked by another lane")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked)
    assert _survey._spec_declares_files(spec) is None


# --------------------------------------------------------------------------- #
# gh#326 — the lifecycle vocabulary is open, and the annotation now says so
# --------------------------------------------------------------------------- #
def test_the_lifecycle_annotation_is_open_because_the_runtime_always_was(tmp_path):
    """gh#326: `LifecycleState` declared five values while the corpus held seventeen.

    The declaration was wrong in BOTH directions — 13 observed values were undeclared, and
    `archived` was declared and never observed — so this asserts the property rather than a
    list: whatever a human writes in `current_state` normalises straight through to
    `lifecycle_state`, and no fixed set can be pinned here without changing behaviour.

    `drafted` is the fixture because it is the second commonest value in the live corpus (112
    records), not an exotic edge, and it was one of the thirteen the Literal excluded.
    """
    assert _survey.LifecycleState is str

    make_spec(tmp_path, "proj", "novel", frontmatter="current_state: drafted\n")
    make_spec(tmp_path, "proj", "invented", frontmatter="current_state: mothballed\n")

    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))

    assert recs["novel"].lifecycle_state == "drafted"
    assert recs["invented"].lifecycle_state == "mothballed"


def test_no_suppression_stands_between_the_declaration_and_the_assignment():
    """The `# type: ignore` comments were the defect's disguise, not an incidental tidy-up.

    Four of them (one `arg-type`, three `return-value`) existed for exactly one reason: the
    annotation claimed a closed set the runtime never honoured. With the alias honest they are
    unnecessary, and this asserts they are GONE rather than trusting that nobody re-adds one to
    quiet a future mismatch instead of fixing it.

    Comment-only lines are excluded so the prose above the alias — which quotes the suppression
    it removed — does not make this test assert against itself.
    """
    src = Path(_survey.__file__).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in src.splitlines()
        if "type: ignore" in line and not line.strip().startswith("#")
    ]
    assert offenders == [], offenders


def test_a_state_the_precedence_table_does_not_name_keeps_the_prior_value():
    """The gap gh#326 says the closed-vocabulary decision would settle, pinned as behaviour.

    `_LIFECYCLE_PRECEDENCE` names five values; thirteen observed ones are absent, so for two
    unnamed states the comparison is `-999 > -999` and `_promote` keeps `prior`. That is the
    documented consequence, not an accident — `_merge_spec_record` guards the case that matters
    with an explicit terminal check precisely because this default cannot rank them.

    Pinned so that a future closed-vocabulary mapping, which WOULD change this, has to change a
    test that says why rather than silently altering merge outcomes.
    """
    assert set(_survey._LIFECYCLE_PRECEDENCE) == {
        "speccing", "planning", "running", "done", "archived",
    }
    assert _survey._promote("drafted", "pinned") == "drafted"
    assert _survey._promote("pinned", "drafted") == "pinned"
    # ...while a named value still outranks an unnamed one in the direction the table defines.
    assert _survey._promote("speccing", "done") == "done"


# --------------------------------------------------------------------------- #
# warn_dependencies_satisfied_by_a_superseded_parent - the stale pointer gh#261 traded for
#
# gh#261 made `superseded` satisfy a dependency, which promises "the story is over", not "the
# work happened". Those come apart for that one value: a superseded parent was REPLACED BY
# CHILDREN, so a dependent still naming it goes READY holding a pointer to work that may be
# unbuilt. Deliberate trade - loud beats the silent permanent strand it replaced - and this is
# the cover. Zero pairs in the live corpus, re-measured: 588 records, 39 superseded, 113 edges
# at a terminal parent, none at a superseded one.
# --------------------------------------------------------------------------- #
REPLACED = "depends_on satisfied by a REPLACED parent: "


def _replaced_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if REPLACED in r.getMessage()]


def test_a_dependent_on_a_superseded_parent_is_named_once_with_the_parent_and_the_state(caplog):
    """It FIRES, and the line carries what is needed to act: both slugs and why."""
    recs = _dep_pair("superseded")

    assert warn_dependencies_satisfied_by_a_superseded_parent(recs) == [("kid", "dep")]

    msgs = _replaced_warnings(caplog)
    assert len(msgs) == 1, msgs
    assert "'kid'" in msgs[0] and "'dep'" in msgs[0]
    assert "'superseded'" in msgs[0]
    assert "children" in msgs[0]


@pytest.mark.parametrize(
    "dep_state", [s for s in FINISHED_STATES if s != "superseded"]
)
def test_the_channel_is_SILENT_on_a_parent_whose_work_actually_happened(dep_state: str, caplog):
    """THE WHOLE POINT, and parametrised over the constant so each value is its own case.

    `done`, `shipped` and `archived` all mean the work happened, so a dependent naming one holds
    a pointer to something real. Only `superseded` means replaced-by-children. A channel that
    fired on ordinary finished work would put a line on every survey forever, which is the noise
    that trains a reader to skip the channel the real line will one day arrive on.

    Derived from `TERMINAL_CURRENT_STATES` minus the one value under test, so a fifth terminal
    value added to the vocabulary gains a silence case here for free rather than passing
    unnoticed.
    """
    recs = _dep_pair(dep_state)

    assert warn_dependencies_satisfied_by_a_superseded_parent(recs) == []
    assert _replaced_warnings(caplog) == []


def test_a_finished_dependent_is_not_reported_however_stale_its_pointer(caplog):
    """The carrier filter, same rule as the neighbour channel's.

    A finished epic's `depends_on` sends nobody anywhere: nothing will be claimed against it, so
    the stale pointer costs nothing and reporting it is pure recurring noise.
    """
    recs = [
        EpicRecord(slug="dep", title="d", lifecycle_state="superseded",
                   current_state="superseded"),
        EpicRecord(slug="kid", title="k", lifecycle_state="done", current_state="done",
                   depends_on=["dep"]),
    ]

    assert warn_dependencies_satisfied_by_a_superseded_parent(recs) == []
    assert _replaced_warnings(caplog) == []


def test_a_foreign_superseded_parent_is_left_to_the_channel_that_owns_it(caplog):
    """A cross-project edge is unsatisfiable whatever its state, and
    `forbidden_cross_project_dependencies` already names it. Reporting it here too would call a
    scoping problem a stale pointer and log one misfiling twice in a wake.
    """
    recs = [
        EpicRecord(slug="dep", title="d", lifecycle_state="superseded",
                   current_state="superseded", project="other"),
        _approved("kid", depends_on=["dep"], project="mine"),
    ]

    assert warn_dependencies_satisfied_by_a_superseded_parent(recs) == []
    assert _replaced_warnings(caplog) == []


def test_selectable_actually_invokes_the_channel(caplog):
    """WIRED, not merely defined — the failure this suite has watched happen elsewhere.

    A channel nobody calls is silent in the way that reads as coverage, so this asserts the
    warning arrives through the real entry point rather than a direct call. `selectable` is
    where its three neighbours already speak, over the full corpus, before `ready_set` narrows
    it to the records the line is about.
    """
    recs = _dep_pair("superseded")

    with caplog.at_level("WARNING"):
        selectable(recs)

    assert len(_replaced_warnings(caplog)) == 1, _replaced_warnings(caplog)


# --------------------------------------------------------------------------- #
# gh#358 - the read guard is tested as READS, because it changes nothing else
#
# Pass 1 skips `_spec_declares_files` for a terminal spec. Replacing that guard with `if True:`
# left the ENTIRE _shared suite green: `_warn_specs_without_files` filters on
# FILES_GATED_CURRENT_STATES at warn time, so removing the guard changes no warning - it only
# makes the survey open files it did not need to open. The guard is a pure I/O claim, and only
# an I/O assertion can fail when it goes.
#
# `test_a_spec_outside_the_promotion_gate_is_silent` asserts the sentence "whose files this
# check therefore never opens" in PROSE, and passes for the warn-time reason either way.
# --------------------------------------------------------------------------- #
def _count_reads_by_filename(monkeypatch) -> dict[str, int]:
    """Count `Path.read_text` calls per filename, changing no behaviour.

    Same shape as `_vanish_after_first_read` above - patch the one method the survey reads
    through, delegate to the real one, and observe. Keyed on `.name` because the fixture's
    filenames are unique and the absolute path is a tmp dir that varies per run.
    """
    real = Path.read_text
    counts: dict[str, int] = {}

    def patched(self, *args, **kwargs):
        counts[self.name] = counts.get(self.name, 0) + 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", patched)
    return counts


@pytest.mark.parametrize("terminal", [s for s in FINISHED_STATES if s != "archived"])
def test_a_terminal_spec_costs_fewer_reads_than_a_non_terminal_one(
    tmp_path: Path, monkeypatch, terminal: str
):
    """THE GUARD'S ACTUAL CLAIM, and the only assertion that can fail when it is removed.

    Measured during gh#311 on the live board: 534 spec files, 1216 reads, histogram
    `{2: 386, 3: 148}` - a non-terminal spec costs three reads and a terminal one costs two.
    Deleting the guard moves every terminal spec to three, which on that board is ~386 extra
    file reads on every lane's wake.

    ASSERTS THE INEQUALITY, NOT THE NUMBERS. Pinning 2 and 3 would go red for any unrelated read
    added anywhere in pass 1, which is a different change failing this test for a reason it does
    not own. The property the guard buys is that terminal costs strictly less, and that is what
    `if True:` destroys - it makes the two equal rather than making either wrong.

    `archived` is excluded because those records leave the corpus rather than surviving with
    counts to compare.
    """
    make_spec(tmp_path, "proj", "finished", frontmatter=f"current_state: {terminal}\n")
    make_spec(tmp_path, "proj", "live", frontmatter="current_state: drafted\n")

    counts = _count_reads_by_filename(monkeypatch)
    recs = _by_slug(survey_epics(tmp_path, PAST_CUTOFF))

    # Non-vacuity, both directions: the fixture really was surveyed, and both files really were
    # read. Without these, "fewer reads" would pass just as well for a spec nothing opened.
    assert sorted(recs) == ["finished", "live"], "both specs must reach the corpus"
    assert counts.get("finished.md", 0) >= 1, "the terminal spec was never read at all"
    assert counts["finished.md"] < counts["live.md"], counts


# ---------------------------------------------------------------------------
# issue #204's live remainder: a spec that PARSES and is semantically gutted
#
# The issue says "survey_epics skips an unparseable record silently". Measured at c87b907,
# that half is false and already shipped: an unparseable spec is named at WARNING by three
# handlers, registered against sidecar promotion, and counted by name in `counted N,
# skipped M`. What no handler can see is the opposite case - a completion-truth reconcile
# composes a quoted scalar out of free prose, the closing quote lands in the wrong place, the
# value swallows the keys beneath it, and the file PARSES. Nothing raises, so nothing logs.
#
# The corruption shape below is the one recovered from the real incident: a `completion-truth:`
# double-quoted scalar whose closing quote never arrives on its own line.
# ---------------------------------------------------------------------------

def _gutted_spec(root: Path, slug: str, *, swallows: list[str], lead: str = "") -> Path:
    """A spec whose `completion-truth` scalar opens a quote that closes too late.

    THE FIXTURE MUST STILL PARSE, and that is the whole point of this class. The opening `"`
    runs past the end of its line, every key in *swallows* is absorbed into the value, and the
    closing `"` arrives at the end of the last one - so `yaml.safe_load` SUCCEEDS, no handler
    fires, and the swallowed keys simply do not exist. An invalid variant would land in the
    already-shipped `malformed spec yaml, skipping` channel and prove nothing about this one.
    """
    eaten = "\n".join(swallows)
    body = (
        "---\n"
        f"slug: {slug}\n"
        f"title: {slug} title\n"
        f"{lead}"
        'completion-truth: "2026-09-22 lane reconcile: shipped - PR #1\n'
        f'{eaten}"\n'
        "---\n\n"
        f"# {slug}\n\n## Files\n\n- `skills/_shared/agentflow/epic_survey.py`\n"
    )
    return _write(root / "projects" / "proj" / "design" / "specs" / f"{slug}.md", body)


def _swallow_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if "did not return" in r.getMessage() and "swallowed" in r.getMessage()
    ]


def _swallowed_keys_named(caplog) -> list[list[str]]:
    """The KEY LIST out of each warning, never the whole message.

    A reviewer caught the first version of these tests passing for the wrong reason: the
    message ends with the file path, pytest builds `tmp_path` out of the test's own name
    truncated to 30 characters, and both `test_a_swallowed_current_state_...` and
    `test_a_swallowed_queue_class_...` therefore put the very key they assert INTO the path.
    `"current_state" in message` was satisfied by the directory name, so that test passed
    while the detector named zero keys. Parsing the trailing parenthesised group is what makes
    the assertion depend on the detector's output and nothing else.
    """
    out = []
    for msg in _swallow_warnings(caplog):
        assert msg.endswith(")"), msg
        out.append([k.strip() for k in msg[msg.rfind("(") + 1 : -1].split(",") if k.strip()])
    return out


def test_a_swallowed_queue_class_and_priority_is_named_not_silent(tmp_path: Path, caplog):
    """The softer half: the epic stays selectable but sorts behind every healthy epic forever.

    `priority` is UNSET (+inf) because the key never reached the parser, so this epic is last
    in every queue for the rest of its life - and before this warning existed, nothing said so.
    """
    _gutted_spec(
        tmp_path,
        "gutted-tail",
        lead="current_state: approved-for-autonomous\n",
        swallows=["queue_class: fleet-buildable", "priority: 500", "domain: infra"],
    )
    caplog.clear()
    with caplog.at_level("WARNING"):
        survey_epics(tmp_path, PAST_CUTOFF, project="proj")
    named = _swallowed_keys_named(caplog)
    assert len(named) == 1, named
    assert sorted(named[0]) == ["domain", "priority", "queue_class"], named[0]


def test_a_swallowed_current_state_is_named_even_though_the_file_parses(tmp_path: Path, caplog):
    """The harmful half, and the one the incident hit.

    An `approved-for-autonomous` epic whose `current_state` is eaten is demoted to `speccing`,
    which drops it out of `selectable()` entirely: approved on every board, claimable by
    nobody. Nothing raises, so before this line the only evidence was its absence.
    """
    _gutted_spec(
        tmp_path,
        "gutted-state",
        swallows=["current_state: approved-for-autonomous", "queue_class: fleet-buildable"],
    )
    caplog.clear()
    with caplog.at_level("WARNING"):
        records = survey_epics(tmp_path, PAST_CUTOFF, project="proj")
    named = _swallowed_keys_named(caplog)
    assert len(named) == 1, named
    assert sorted(named[0]) == ["current_state", "queue_class"], named[0]
    # The harm itself, asserted so the warning is not mistaken for a repair: the record is
    # still demoted. This change ends the silence; it does not change what the parser returns.
    gutted = [r for r in records if r.slug == "gutted-state"]
    assert gutted and gutted[0].current_state != "approved-for-autonomous"


def test_a_healthy_spec_warns_about_nothing(tmp_path: Path, caplog):
    make_spec(
        tmp_path,
        "proj",
        "healthy",
        frontmatter="current_state: approved-for-autonomous\nqueue_class: fleet-buildable\npriority: 500\n",
    )
    caplog.clear()
    with caplog.at_level("WARNING"):
        survey_epics(tmp_path, PAST_CUTOFF, project="proj")
    assert _swallow_warnings(caplog) == []


def test_prose_inside_a_block_scalar_is_not_read_as_a_swallowed_key(tmp_path: Path, caplog):
    """THE FALSE-POSITIVE GUARD, and the reason the check is anchored at column 0.

    `progress: |` blocks in this corpus are long free prose, and YAML strips their common
    indentation - so a prose line that happens to read `priority: 245` arrives in the VALUE
    looking exactly like a key. It is not one. The detector compares keys the BLOCK declares
    at the left margin against the mapping, so indented prose can never enter the comparison.

    Without the column-0 anchor this fixture warns, and every long progress note in the real
    corpus warns with it.
    """
    body = (
        "---\n"
        "slug: blocky\ntitle: blocky title\n"
        "current_state: approved-for-autonomous\n"
        "queue_class: fleet-buildable\n"
        "priority: 500\n"
        "progress: |\n"
        "  The epic was rebanded, so its priority: 245 line moved to the title.\n"
        "  current_state: done was NOT written, deliberately - item 3 is still open.\n"
        "  domain: infra-meta is where it sits.\n"
        "---\n\n# blocky\n\n## Files\n\n- `x.py`\n"
    )
    _write(tmp_path / "projects" / "proj" / "design" / "specs" / "blocky.md", body)
    caplog.clear()
    with caplog.at_level("WARNING"):
        survey_epics(tmp_path, PAST_CUTOFF, project="proj")
    assert _swallow_warnings(caplog) == [], "indented prose was read as a top-level key"


def test_a_multi_line_quoted_value_that_closes_properly_is_not_a_finding(tmp_path: Path, caplog):
    """A legitimately long `completion-truth` - the very field that caused the incident - is
    not itself suspicious. Only a value that eats a declared key is."""
    body = (
        "---\n"
        "slug: longval\ntitle: longval title\n"
        "current_state: done\n"
        "queue_class: fleet-buildable\n"
        "priority: 500\n"
        'completion-truth: "2026-09-22 lane reconcile: shipped - PR #429 squash 19466e8,\n'
        '  gates green, provisioned, deployed path verified"\n'
        "---\n\n# longval\n\n## Files\n\n- `x.py`\n"
    )
    _write(tmp_path / "projects" / "proj" / "design" / "specs" / "longval.md", body)
    caplog.clear()
    with caplog.at_level("WARNING"):
        survey_epics(tmp_path, PAST_CUTOFF, project="proj")
    assert _swallow_warnings(caplog) == []


# ---------------------------------------------------------------------------
# The same corruption on a spec OLDER than the mtime cutoff.
#
# The warning above lives in `_read_sidecar`, which only fresh specs reach. A stale spec is
# re-read by `_stale_spec_state` only when a plan asks for its slug, and that read used to take
# whatever the parser returned: a swallowed `current_state: done` read as "no state", so a
# fresh approved plan promoted the shipped epic straight to the top of the queue with zero
# log lines. The stale path now fails closed when `current_state` is among the lost keys.
# ---------------------------------------------------------------------------

_APPROVED_BUILDABLE_PLAN = (
    "current_state: approved-for-autonomous\nqueue_class: fleet-buildable\npriority: 10\n"
)


def _stale_gutted_probe(root: Path, *, swallows: list[str], lead: str = "") -> Path:
    """The measured probe: an aged gutted spec beside a fresh approved, buildable plan."""
    spec = _age(_gutted_spec(root, "gutted-old", swallows=swallows, lead=lead))
    make_plan(root, "proj", "gutted-old", frontmatter=_APPROVED_BUILDABLE_PLAN)
    return spec


def test_a_STALE_spec_whose_current_state_was_swallowed_is_not_promoted(tmp_path: Path, caplog):
    """THE PROBE. Before the fix: one record at approved-for-autonomous, priority 10, top of the
    queue, and not one log line. A stale spec that cannot be shown non-terminal now takes the
    same fail-closed branch as any unreadable stale spec - and is counted through its line.
    """
    spec = _stale_gutted_probe(tmp_path, swallows=["current_state: done"])

    caplog.clear()
    with caplog.at_level("WARNING"):
        recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)

    assert "gutted-old" not in _by_slug(recs)
    assert selectable(recs) == []

    # Exactly one WARNING, and it is the existing skip line - the fleet report counts that
    # shape structurally, so a second, differently-shaped line would read as a second file.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    skip = warnings[0]
    assert "unreadable stale spec, plan not promoted, skipping:" in str(skip.msg)
    assert isinstance(skip.args, tuple) and len(skip.args) == 2
    assert Path(str(skip.args[0])) == spec
    # The key list is parsed out of the trailing group, never matched against the whole text:
    # the path carries this test's own name, and that name contains `current_state`.
    reason = str(skip.args[1])
    assert str(spec) in reason
    assert reason.endswith(")"), reason
    assert [k.strip() for k in reason[reason.rfind("(") + 1 : -1].split(",")] == ["current_state"]


def test_the_stale_probe_WITHOUT_the_swallow_is_still_promoted(tmp_path: Path, caplog):
    """THE CONTROL. Identical layout, same unclosed-looking quote, but the line it absorbs is
    prose rather than a key - so nothing is lost and the spec simply carries no state. That
    record must still be promoted, or the test above could pass on the fixture's shape alone.
    """
    _stale_gutted_probe(tmp_path, swallows=["reconciled by hand after the merge"])

    caplog.clear()
    with caplog.at_level("WARNING"):
        recs = survey_epics(tmp_path, PAST_CUTOFF)

    rec = _by_slug(recs)["gutted-old"]
    assert rec.current_state == "approved-for-autonomous"
    assert rec.priority == 10
    assert [r.slug for r in selectable(recs)] == ["gutted-old"]
    assert caplog.records == []


def test_a_stale_spec_that_lost_only_OTHER_keys_changes_nothing_and_says_nothing(
    tmp_path: Path, caplog
):
    """The stale path reads `current_state` and nothing else, so a lost `queue_class` or
    `priority` there has no consumer to mislead: the plan supplies both, as before."""
    _stale_gutted_probe(
        tmp_path,
        lead="current_state: drafted\n",
        swallows=["queue_class: fleet-buildable", "priority: 500"],
    )

    caplog.clear()
    with caplog.at_level("DEBUG"):
        recs = survey_epics(tmp_path, PAST_CUTOFF)

    rec = _by_slug(recs)["gutted-old"]
    assert rec.current_state == "approved-for-autonomous"
    assert rec.priority == 10
    assert [r.slug for r in selectable(recs)] == ["gutted-old"]
    assert caplog.records == []


def test_a_gutted_stale_spec_NO_plan_names_is_silent(tmp_path: Path, caplog, monkeypatch):
    """The check is rationed exactly as the read is: only a plan asking opens a stale spec.

    Two halves. With no plan at all the file is never opened. With a plan for a DIFFERENT,
    spec-less slug the full pass does read it - and still says nothing, because the only line
    the stale path can produce is the plan pass's skip line, and nobody asked for this slug.
    """
    seen = _count_stale_reads(monkeypatch)
    spec = _age(_gutted_spec(tmp_path, "gutted-old", swallows=["current_state: done"]))

    caplog.clear()
    with caplog.at_level("DEBUG"):
        assert survey_epics(tmp_path, PAST_CUTOFF, include_archived=True) == []
    assert seen == []
    assert caplog.records == []

    make_plan(tmp_path, "proj", "unrelated", frontmatter=_APPROVED_BUILDABLE_PLAN)
    caplog.clear()
    with caplog.at_level("DEBUG"):
        recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert seen == [spec], "the full pass should have read the stale file"
    assert [r.slug for r in selectable(recs)] == ["unrelated"]
    assert caplog.records == []


def test_an_abandoned_carrier_is_not_reported_as_stranded(tmp_path, caplog):
    """Two abandoned epics in a chain: the child waits on nothing, so nothing is stranded."""
    make_spec(tmp_path, "proj", "alpha", frontmatter=APPROVED)
    make_spec(tmp_path, "proj", "eps", frontmatter=APPROVED + "depends_on: [alpha]\n")
    make_status(tmp_path, "proj", "alpha", status="abandoned")
    make_status(tmp_path, "proj", "eps", status="failed")
    recs = survey_epics(tmp_path, PAST_CUTOFF)
    assert warn_stranded_terminal_dependencies(recs) == []
    assert _stranded_warnings(caplog) == []


# --------------------------------------------------------------------------- #
# A `## Files` section that is PRESENT but declares no path. `files_section` extracts only
# backticked paths, so `- src/widget.py (new)` is a section with zero entries: the missing-
# heading warning above stays silent on it, and without a second warning the human gate passes
# a spec the seam check cannot use.
# --------------------------------------------------------------------------- #
def _spec_with_files_body(root: Path, slug: str, state: str, files_body: str) -> Path:
    body = (
        f"---\nslug: {slug}\ntitle: {slug} title\ncurrent_state: {state}\n---\n\n"
        f"# {slug} title\n\n"
        "## Proposed scope\n\nclose it\n\n"
        f"## Files\n{files_body}\n"
        "## Use case continuity\n\nreuse UC-0147\n"
    )
    return _write(root / "projects" / "proj" / "design" / "specs" / f"{slug}.md", body)


def _pathless_warnings(root: Path, caplog) -> list[str]:
    caplog.clear()
    with caplog.at_level("WARNING"):
        survey_epics(root, PAST_CUTOFF)
    return [m for m in (r.getMessage() for r in caplog.records) if "declares no path" in m]


@pytest.mark.parametrize("state", ["drafted", "approved-for-autonomous"])
def test_an_UNBACKTICKED_files_section_warns_at_the_gated_states(
    tmp_path: Path, caplog, state: str
):
    """The reported repro: the section is present, extracts zero paths, and must not survey clean."""
    spec = _spec_with_files_body(tmp_path, "widget", state, "- src/widget.py (new)\n")
    assert files_section(spec.read_text(encoding="utf-8")).present
    assert not files_section(spec.read_text(encoding="utf-8")).seam_checkable

    msgs = _pathless_warnings(tmp_path, caplog)
    assert len(msgs) == 1, msgs
    assert "widget" in msgs[0] and str(spec) in msgs[0] and state in msgs[0], msgs[0]
    # The warning names the rule the reader has to apply, not only the symptom.
    assert "one backticked, repo-relative path per entry" in msgs[0], msgs[0]
    # And it is NOT the missing-heading warning: the heading is there.
    assert _files_warnings(tmp_path, caplog) == []


def test_a_BACKTICKED_files_section_warns_about_nothing(tmp_path: Path, caplog):
    _spec_with_files_body(
        tmp_path, "widget", "approved-for-autonomous", "- `src/widget.py` (new)\n"
    )
    assert _pathless_warnings(tmp_path, caplog) == []
    assert _files_warnings(tmp_path, caplog) == []


def test_a_spec_with_NO_files_heading_keeps_only_its_own_warning(tmp_path: Path, caplog):
    """The two warnings stay distinct: a missing heading is not reported as an empty section."""
    spec = _spec_without_files(tmp_path, "gap", "drafted")
    _assert_one_warning_naming(tmp_path, caplog, spec, "gap")
    assert _pathless_warnings(tmp_path, caplog) == []


def test_an_unbackticked_section_before_the_gate_is_silent(tmp_path: Path, caplog):
    """`speccing` has not reached the promotion gate, the same population rule as the heading."""
    _spec_with_files_body(tmp_path, "widget", "speccing", "- src/widget.py (new)\n")
    assert _pathless_warnings(tmp_path, caplog) == []


def test_the_pathless_answer_follows_the_MERGE_winner(tmp_path: Path, caplog):
    """Same-slug siblings: the warning describes the record the survey returns, as the
    missing-heading warning does."""
    specs = tmp_path / "projects" / "proj" / "design" / "specs"

    def _spec(name: str, state: str, files_body: str) -> Path:
        return _write(
            specs / name,
            f"---\nslug: dup\ntitle: dup title\ncurrent_state: {state}\n---\n\n"
            f"# dup title\n\n## Files\n{files_body}\n",
        )

    winner = _spec("a-dup.md", "approved-for-autonomous", "- src/widget.py\n")
    _spec("b-dup.md", "drafted", "- `src/widget.py`\n")
    msgs = _pathless_warnings(tmp_path, caplog)
    assert len(msgs) == 1, msgs
    assert str(winner) in msgs[0], msgs[0]


def test_a_BACKTICKED_merge_winner_clears_a_pathless_loser_read_later(tmp_path: Path, caplog):
    """The mirror of the merge-winner test: the loser is read AFTER the winner and is the
    pathless one. The warning describes the returned record, so nothing is reported."""
    specs = tmp_path / "projects" / "proj" / "design" / "specs"

    def _spec(name: str, state: str, files_body: str) -> Path:
        return _write(
            specs / name,
            f"---\nslug: dup\ntitle: dup title\ncurrent_state: {state}\n---\n\n"
            f"# dup title\n\n## Files\n{files_body}\n",
        )

    winner = _spec("a-dup.md", "approved-for-autonomous", "- `src/widget.py`\n")
    _spec("b-dup.md", "drafted", "- src/widget.py\n")
    (rec,) = [r for r in survey_epics(tmp_path, PAST_CUTOFF) if r.slug == "dup"]
    assert rec.source_path == str(winner), rec.source_path
    assert _pathless_warnings(tmp_path, caplog) == []


def test_several_pathless_offenders_are_reported_in_slug_order(tmp_path: Path, caplog):
    """Read order is glob order (`aproj` before `bproj`), so `zeta` is read first; the warnings
    still come out sorted by slug, one line each."""
    for proj, slug in (("aproj", "zeta"), ("bproj", "alpha")):
        _write(
            tmp_path / "projects" / proj / "design" / "specs" / f"{slug}.md",
            f"---\nslug: {slug}\ntitle: {slug} title\ncurrent_state: drafted\n---\n\n"
            f"# {slug} title\n\n## Files\n- src/{slug}.py\n",
        )
    msgs = _pathless_warnings(tmp_path, caplog)
    assert len(msgs) == 2, msgs
    assert ": alpha (drafted" in msgs[0] and ": zeta (drafted" in msgs[1], msgs


# --------------------------------------------------------------------------- #
# gh#338 - both `## Files` warnings describe the DISPATCHABLE population, as their sibling
# `warn_unresolvable_dependencies` does. A spec no loop will ever dispatch is not a spec a
# promoting seat has to refuse for a missing section.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("queue_class", sorted(NON_DISPATCHABLE_QUEUE_CLASSES))
def test_a_files_gap_on_a_NON_DISPATCHABLE_queue_class_does_not_warn(
    tmp_path: Path, caplog, queue_class: str
):
    """The measured case was a `brainstorm-pin` whose missing files are its open fork."""
    _write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "pin.md",
        f"---\nslug: pin\ntitle: pin\ncurrent_state: drafted\nqueue_class: {queue_class}\n---\n\n"
        "# pin\n\n## Proposed scope\n\nfork open\n",
    )
    _write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "widget.md",
        f"---\nslug: widget\ntitle: widget\ncurrent_state: drafted\nqueue_class: {queue_class}\n"
        "---\n\n# widget\n\n## Files\n- src/widget.py (new)\n",
    )
    assert _files_warnings(tmp_path, caplog) == []
    assert _pathless_warnings(tmp_path, caplog) == []


@pytest.mark.parametrize("queue_class", [None, CURRENT_QUEUE_CLASS, "bug"])
def test_a_files_gap_on_a_DISPATCHABLE_or_unknown_queue_class_still_warns(
    tmp_path: Path, caplog, queue_class
):
    """The control: the exclusion is the three known non-dispatchable values, nothing wider.
    An out-of-vocabulary `bug` keeps its line - it has its own warning, and the spec is still
    headed for dispatch once the value is fixed."""
    qc = "" if queue_class is None else f"queue_class: {queue_class}\n"
    _write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "gap.md",
        f"---\nslug: gap\ntitle: gap\ncurrent_state: drafted\n{qc}---\n\n# gap\n",
    )
    _write(
        tmp_path / "projects" / "proj" / "design" / "specs" / "widget.md",
        f"---\nslug: widget\ntitle: widget\ncurrent_state: drafted\n{qc}---\n\n"
        "# widget\n\n## Files\n- src/widget.py (new)\n",
    )
    assert len(_files_warnings(tmp_path, caplog)) == 1
    assert len(_pathless_warnings(tmp_path, caplog)) == 1


# --------------------------------------------------------------------------- #
# gh#365 - reconciling a spec into done/ moves it one directory down, and every file-relative
# pointer in it goes one `../` short. `warn_unresolved_done_spec_paths` is what reports that.
# --------------------------------------------------------------------------- #
def _pointer_spec(
    root: Path, slug: str, *, state: str, where: str = "", evidence: str = "", files: str = ""
) -> Path:
    specs = root / "projects" / "proj" / "design" / "specs"
    prov = f"provenance:\n  evidence: {evidence}\n" if evidence else ""
    body = f"---\nslug: {slug}\ntitle: {slug}\ncurrent_state: {state}\n{prov}---\n\n# {slug}\n"
    if files:
        body += f"\n## Files\n\n{files}\n"
    return _write((specs / where if where else specs) / f"{slug}.md", body)


def _pointer_warnings(caplog) -> list[str]:
    return [
        m for m in (r.getMessage() for r in caplog.records)
        if m.startswith("done spec pointer does not resolve")
    ]


@pytest.mark.parametrize("where, reported", [("", False), ("done", True)])
def test_evidence_that_resolved_before_the_move_into_done_is_a_DEPTH_SHIFT_after_it(
    tmp_path: Path, caplog, where: str, reported: bool
):
    """The measured cliff: 41/41 resolving in `design/specs`, 0/21 in `done/`. Same spec, same
    pointer, one directory apart - so the unmoved copy is the control that it resolved at all."""
    _write(tmp_path / "projects" / "proj" / "status" / "audits" / "a.md", "audit\n")
    spec = _pointer_spec(
        tmp_path, "shipped", state="done", where=where, evidence="../../status/audits/a.md#shipped"
    )

    with caplog.at_level("WARNING"):
        found = _survey.warn_unresolved_done_spec_paths(tmp_path)

    if not reported:
        assert found == [] and _pointer_warnings(caplog) == []
        return
    assert [(u.slug, u.source, u.kind) for u in found] == [
        ("shipped", "provenance.evidence", _survey.POINTER_DEPTH_SHIFT)
    ]
    (msg,) = _pointer_warnings(caplog)
    assert "depth-shift" in msg and str(spec) in msg and "../../status/audits/a.md" in msg, msg


def test_a_NOT_DONE_spec_is_never_judged_not_even_its_new_files(tmp_path: Path, caplog):
    """The false-positive class the issue measured: six not-done specs whose non-resolving paths
    are all files the epic will CREATE. Scope is the check - drop it and these report."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _pointer_spec(
        tmp_path, "building", state="approved-for-autonomous",
        evidence="../../status/audits/missing.md",
        files="- `skills/_shared/agentflow/initiative.py` (new) - the module\n",
    )
    with caplog.at_level("WARNING"):
        found = _survey.warn_unresolved_done_spec_paths(tmp_path, repo_root=repo, project="proj")
    assert found == []
    assert _pointer_warnings(caplog) == []


def test_a_mistyped_files_entry_in_a_done_spec_is_UNRESOLVED_against_the_repo(
    tmp_path: Path, caplog
):
    """The second class: `skills/_shared/agentflow/tests/...` where the real path drops the
    `agentflow/` segment. Reported as `unresolved`, not "typo": on the live corpus that class is
    mostly renames, deletions and paths written against another root, and only a human can tell
    which. Judged against the REPO, since `## Files` is repo-relative. Prose after
    an entry is not a declared path and is not judged - including a path-shaped one, like the
    retired file an entry says it replaces."""
    repo = tmp_path / "repo"
    _write(repo / "skills" / "_shared" / "tests" / "test_x.py", "")
    _write(repo / "skills" / "_shared" / "agentflow" / "x.py", "")
    _pointer_spec(
        tmp_path, "shipped", state="done", where="done",
        files=(
            "- `skills/_shared/agentflow/x.py` - replaces `docs/retired/old.md`; "
            "`Finding.status`, see `registry.py`\n"
            "- `skills/_shared/agentflow/tests/test_x.py` - the tests\n"
        ),
    )
    with caplog.at_level("WARNING"):
        found = _survey.warn_unresolved_done_spec_paths(tmp_path, repo_root=repo, project="proj")

    assert [(u.source, u.pointer, u.kind) for u in found] == [
        ("## Files", "skills/_shared/agentflow/tests/test_x.py", _survey.POINTER_UNRESOLVED)
    ]
    (msg,) = _pointer_warnings(caplog)
    assert "(unresolved)" in msg, msg


def test_files_entries_are_not_judged_without_a_repo_to_resolve_them_against(tmp_path: Path):
    """Specs live under the data root, not in the repo: resolved against the spec alone, every
    repo-relative entry would read as broken."""
    _pointer_spec(
        tmp_path, "shipped", state="done", where="done", files="- `skills/nowhere.py`\n"
    )
    assert _survey.warn_unresolved_done_spec_paths(tmp_path) == []


@pytest.mark.parametrize(
    "evidence", ["measured on the live board", "https://example.invalid/x.md", "gh#365"]
)
def test_prose_and_urls_in_evidence_are_not_pointers(tmp_path: Path, evidence: str):
    """`provenance.evidence` is free text in the corpus; a sentence is not a broken path."""
    _pointer_spec(tmp_path, "shipped", state="done", where="done", evidence=f'"{evidence}"')
    assert _survey.warn_unresolved_done_spec_paths(tmp_path) == []


@pytest.mark.parametrize(
    "evidence, label",
    [
        ("42", "int"),
        ("true", "bool"),
        ("2026-09-01", "date"),
        ("{../../status/audits/gone.md: 1}", "dict"),
    ],
)
def test_evidence_that_is_not_text_or_a_list_is_skipped_never_raised_on(
    tmp_path: Path, caplog, evidence: str, label: str
):
    """YAML hands back what the author typed: an unquoted date, a number, a bool, a mapping. None
    is a pointer. Iterating them raised `TypeError` - or, for a mapping, walked its keys as if
    they were declared paths - from a function that promises never to raise."""
    _pointer_spec(tmp_path, "shipped", state="done", where="done", evidence=evidence)
    with caplog.at_level("WARNING"):
        assert _survey.warn_unresolved_done_spec_paths(tmp_path) == [], label
    assert _pointer_warnings(caplog) == [], label
    assert any("neither text nor a list" in r.getMessage() for r in caplog.records), label


def test_a_LIST_of_evidence_is_judged_item_by_item(tmp_path: Path):
    """The shape normalisation keeps: a list is read as a list, and a non-text item in it is
    dropped rather than raised on."""
    _write(tmp_path / "projects" / "proj" / "status" / "audits" / "a.md", "audit\n")
    _pointer_spec(
        tmp_path, "shipped", state="done", where="done",
        evidence="[../../status/audits/a.md, 7, ../../status/audits/gone.md]",
    )
    found = _survey.warn_unresolved_done_spec_paths(tmp_path)
    assert [(u.pointer, u.kind) for u in found] == [
        ("../../status/audits/a.md", _survey.POINTER_DEPTH_SHIFT),
        ("../../status/audits/gone.md", _survey.POINTER_UNRESOLVED),
    ]


def test_repo_root_without_a_project_is_REFUSED(tmp_path: Path):
    """`repo_root` is one project's checkout. Unscoped, every other project's `## Files` would be
    resolved against it - measured at 100 findings where the scoped run gives 36."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="project"):
        _survey.warn_unresolved_done_spec_paths(tmp_path, repo_root=repo)
    assert _survey.warn_unresolved_done_spec_paths(tmp_path, repo_root=repo, project="proj") == []


def test_a_repo_root_scoped_to_one_project_never_judges_anothers_files(tmp_path: Path):
    """The scope is what makes the repo the right one: a sibling project's done spec, whose
    entries name files in ITS repo, is not walked at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "projects" / "other" / "design" / "specs" / "done" / "theirs.md"
    _write(other, "---\nslug: theirs\ncurrent_state: done\n---\n\n## Files\n\n- `their/x.py`\n")
    assert _survey.warn_unresolved_done_spec_paths(tmp_path, repo_root=repo, project="proj") == []


@pytest.mark.parametrize(
    "evidence, files, label",
    [
        # `_POINTER_LINE_SUFFIX_RE`: `:120` is a line inside the file, not part of its name.
        ("", "- `skills/x.py:120` - the hook\n", "line-suffix"),
        ("", "- `skills/x.py:120-140` - the hook\n", "line-range-suffix"),
        # The glob / placeholder filter: a pattern is not one path, a `<slot>` is not a path yet.
        ("../../status/audits/*.md", "", "glob"),
        ("../../status/<audit>.md", "", "placeholder"),
        ("", "- `skills/<name>/SKILL.md` - each loop\n", "files-placeholder"),
        # The anchored filter on `## Files`: home- or root-anchored names another tree.
        ("", "- `~/.agentflow/config/projects/x.md` - config\n", "files-home"),
        ("", "- `/etc/agentflow/x.conf` - host config\n", "files-root"),
    ],
)
def test_what_is_not_a_repo_pointer_is_never_reported(
    tmp_path: Path, evidence: str, files: str, label: str
):
    repo = tmp_path / "repo"
    _write(repo / "skills" / "x.py", "")
    _pointer_spec(
        tmp_path, "shipped", state="done", where="done", evidence=evidence, files=files
    )
    found = _survey.warn_unresolved_done_spec_paths(tmp_path, repo_root=repo, project="proj")
    assert found == [], (label, found)


@pytest.mark.parametrize(
    "evidence", ["/abs/audits/a.md", "C:/abs/audits/a.md", "~/notes/a.md", r"\\host\share\a.md"]
)
def test_anchored_evidence_is_never_STATTED(tmp_path: Path, monkeypatch, evidence: str):
    """A drive-, root- or home-anchored evidence path names something outside the data root. It is
    filtered BEFORE resolution, as `## Files` is, so the check never probes the reading machine."""
    _pointer_spec(tmp_path, "shipped", state="done", where="done", evidence=f"'{evidence}'")
    real_exists = Path.exists
    probed: list[str] = []

    def recording(self, *args, **kwargs):
        probed.append(str(self))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", recording)
    assert _survey.warn_unresolved_done_spec_paths(tmp_path) == []
    assert not any("abs" in p or "notes" in p or "host" in p for p in probed), probed


def test_a_BOM_prefixed_done_spec_is_still_judged(tmp_path: Path):
    """The function's own `strip_bom`: without it a BOM'd spec misses the anchored frontmatter
    match and its broken pointers are silently never looked at."""
    _write(tmp_path / "projects" / "proj" / "status" / "audits" / "a.md", "audit\n")
    spec = _pointer_spec(
        tmp_path, "shipped", state="done", where="done", evidence="../../status/audits/a.md"
    )
    spec.write_bytes(b"\xef\xbb\xbf" + spec.read_bytes())
    found = _survey.warn_unresolved_done_spec_paths(tmp_path)
    assert [(u.slug, u.kind) for u in found] == [("shipped", _survey.POINTER_DEPTH_SHIFT)]
