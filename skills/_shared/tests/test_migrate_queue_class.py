"""Tests for `agentflow.migrate_queue_class` - the retired-value rewrite.

This script WRITES INTO THE HUMAN CORPUS, 145 files across two projects, in one operator
action at merge. So the assertions worth having are almost all about restraint: what it
declines to open, what it declines to change inside a file it does open, and the proof
that a dry run is genuinely inert rather than merely quiet. Every one of those is asserted
over a fixture tree that is BUILT here, never by reading the live tree - a test that
inspects the real corpus passes for whatever reason the corpus happens to have today.

The fixture reproduces the shapes that actually exist under `<data_root>`, measured
2026-09-05: `.bak-<who>-<when>` siblings sitting in `design/plans/` next to the file they
back up, a `status/legacy-loop-state/` full of archived loop transcripts, an `archive/`
tree beside `projects/`, and the `done/` + `archived/` terminal partitions that the survey
DOES read.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from agentflow import epic_survey as es
from agentflow import migrate_queue_class as mq
from agentflow.epic_survey import DISPATCHABLE_QUEUE_CLASSES
from agentflow.vocabulary import CURRENT_QUEUE_CLASS, RETIRED_QUEUE_CLASS

OLD = RETIRED_QUEUE_CLASS
NEW = CURRENT_QUEUE_CLASS

REPO_ROOT = Path(__file__).resolve().parents[3]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _spec_text(slug, value=OLD, quoted="", body="# body\n"):
    q = quoted
    line = f"queue_class: {q}{value}{q}\n" if value is not None else ""
    return f"---\nslug: {slug}\ncurrent_state: approved-for-autonomous\n{line}---\n\n{body}"


@pytest.fixture()
def tree(tmp_path):
    """A data_root carrying every shape the reach rules have to separate."""
    p = tmp_path / "projects" / "alpha" / "design"

    # --- IN REACH: the two bucket roots the survey globs -------------------- #
    _write(p / "specs" / "live-spec.md", _spec_text("live-spec"))
    _write(p / "plans" / "live-plan.md", _spec_text("live-plan"))
    # --- IN REACH: the TERMINAL_PARTITIONS the survey also globs ------------ #
    _write(p / "specs" / "done" / "done-spec.md", _spec_text("done-spec"))
    _write(p / "plans" / "archived" / "archived-plan.md", _spec_text("archived-plan"))
    # --- IN REACH but NOT a hit: an untagged spec, the commonest shape in the corpus --- #
    # Present so `scanned` and `len(rewrites)` differ. Without it every in-reach file is a
    # hit by construction, and `scanned` could be counted inside the hit branch unnoticed.
    _write(p / "specs" / "untagged-spec.md", _spec_text("untagged-spec", value=None))

    # --- OUT OF REACH: .bak siblings, both live naming shapes --------------- #
    _write(p / "plans" / "live-plan.md.bak-humantask", _spec_text("live-plan"))
    _write(p / "specs" / "live-spec.md.bak", _spec_text("live-spec"))
    # --- OUT OF REACH: status boards, incl. the legacy loop-state archive --- #
    _write(
        tmp_path / "projects" / "alpha" / "status" / "legacy-loop-state" / "ARCHIVE-firings.md",
        _spec_text("archive-firings"),
    )
    _write(p / "specs" / "board.status.md", _spec_text("board"))
    # --- OUT OF REACH: <data_root>/archive/, a sibling of projects/ --------- #
    # Deliberately given a PERFECTLY well-formed `design/specs` shape: what puts it out of
    # reach is living beside `projects/` rather than under it, not a malformed path.
    _write(tmp_path / "archive" / "alpha" / "design" / "specs" / "old.md", _spec_text("old"))
    # --- OUT OF REACH: another project ------------------------------------- #
    _write(
        tmp_path / "projects" / "beta" / "design" / "specs" / "beta-spec.md",
        _spec_text("beta-spec"),
    )
    return tmp_path


def _digest(root):
    """Every file under *root*, path -> sha256 of its exact bytes."""
    return {
        f.relative_to(root).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(root.rglob("*"))
        if f.is_file()
    }


def _run(root, project="alpha", dry_run=False):
    plan = mq.plan_migration(root, project)
    if not dry_run:
        mq.apply_plan(plan)
    return plan


def _rewritten(plan, root):
    return {rw.path.relative_to(root).as_posix() for rw in plan.rewrites}


# --------------------------------------------------------------------------- #
# Reach: exactly what survey_epics() parses, and nothing beside it
# --------------------------------------------------------------------------- #
def test_reach_is_the_two_survey_glob_families(tree):
    """Bucket roots AND terminal partitions, for the named project only."""
    assert _rewritten(mq.plan_migration(tree, "alpha"), tree) == {
        "projects/alpha/design/specs/live-spec.md",
        "projects/alpha/design/plans/live-plan.md",
        "projects/alpha/design/specs/done/done-spec.md",
        "projects/alpha/design/plans/archived/archived-plan.md",
    }


def test_reach_follows_the_survey_rather_than_a_literal_glob_list(tmp_path, monkeypatch):
    """The DERIVATION, not the outcome: widen the survey and the reach must widen too.

    `test_reach_is_the_two_survey_glob_families` pins the resulting PATHS, which is a
    snapshot of today's globs - replacing `SurveyPatterns.for_project()` with a literal
    tuple equal to them leaves it green, and the module's central safety argument ("this
    script's reach cannot be wrong unless the survey's reach is wrong too") then holds
    only by coincidence. Adding a terminal partition would leave a whole partition of live
    records stranded on the retired value, silently.
    """
    monkeypatch.setattr(es, "TERMINAL_PARTITIONS", (*es.TERMINAL_PARTITIONS, "quarantined"))
    widened = es.SurveyPatterns(
        spec_globs=es._bucket_globs("specs"),
        plan_globs=es._bucket_globs("plans"),
    )
    specs = tmp_path / "projects" / "alpha" / "design" / "specs"
    _write(specs / "live.md", _spec_text("live"))
    _write(specs / "quarantined" / "held.md", _spec_text("held"))

    # Control: the survey shipped today does not know that partition, so neither does this.
    assert _rewritten(mq.plan_migration(tmp_path, "alpha"), tmp_path) == {
        "projects/alpha/design/specs/live.md",
    }
    # The claim: the survey widens and the migration follows it, with no edit here.
    assert _rewritten(mq.plan_migration(tmp_path, "alpha", widened), tmp_path) == {
        "projects/alpha/design/specs/live.md",
        "projects/alpha/design/specs/quarantined/held.md",
    }


@pytest.mark.parametrize(
    "excluded",
    [
        "projects/alpha/design/plans/live-plan.md.bak-humantask",
        "projects/alpha/design/specs/live-spec.md.bak",
        "projects/alpha/status/legacy-loop-state/ARCHIVE-firings.md",
        "projects/alpha/design/specs/board.status.md",
        "archive/alpha/design/specs/old.md",
        "projects/beta/design/specs/beta-spec.md",
    ],
    ids=["bak-suffixed", "bak-plain", "legacy-loop-state", "status-board",
         "data-root-archive", "other-project"],
)
def test_excluded_paths_are_never_written(tree, excluded):
    """The exclusions the spec names, asserted on bytes after a REAL run."""
    before = (tree / excluded).read_bytes()
    _run(tree)
    assert (tree / excluded).read_bytes() == before
    assert OLD in (tree / excluded).read_text(encoding="utf-8")


def test_terminal_partitions_are_in_reach_not_excluded(tree):
    """`design/plans/archived/` is a survey partition, NOT the archive tree.

    The spec's prose says "no archived plans", and the two senses of the word sit one
    directory apart. This is the half that must be rewritten: `survey_epics()` reads it,
    and a terminal record left on a dead value drops out of the vocabulary the dependency
    math is computed from.
    """
    _run(tree)
    partition = tree / "projects/alpha/design/plans/archived/archived-plan.md"
    archive = tree / "archive/alpha/design/specs/old.md"
    assert f"queue_class: {NEW}" in partition.read_text(encoding="utf-8")
    assert f"queue_class: {OLD}" in archive.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Dry run is inert
# --------------------------------------------------------------------------- #
def test_dry_run_leaves_the_tree_byte_identical(tree):
    before = _digest(tree)
    plan = _run(tree, dry_run=True)
    assert plan.rewrites, "fixture must have something to rewrite or this proves nothing"
    assert _digest(tree) == before


def test_apply_drops_no_backup_files_of_its_own(tree):
    before = set(_digest(tree))
    _run(tree)
    assert set(_digest(tree)) == before


# --------------------------------------------------------------------------- #
# What changes inside a file it does open
# --------------------------------------------------------------------------- #
def test_prose_occurrences_outside_frontmatter_survive(tmp_path):
    """A spec ABOUT the retired value quotes it in its own text. That is the record."""
    path = _write(
        tmp_path / "projects/alpha/design/specs/vocab.md",
        _spec_text("vocab", body=f"The old value was `{OLD}` and it is retired.\n"),
    )
    _run(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert f"queue_class: {NEW}" in text
    assert f"The old value was `{OLD}` and it is retired." in text


def test_a_body_line_that_looks_like_frontmatter_is_not_rewritten(tmp_path):
    path = _write(
        tmp_path / "projects/alpha/design/specs/fenced.md",
        _spec_text("fenced", value=None, body=f"```yaml\nqueue_class: {OLD}\n```\n"),
    )
    before = path.read_bytes()
    plan = _run(tmp_path)
    assert plan.rewrites == []
    assert path.read_bytes() == before


def test_crlf_and_bare_lf_files_keep_their_line_endings(tmp_path):
    """A text-mode round-trip on Windows rewrites every LF and buries the change."""
    lf = _write(tmp_path / "projects/alpha/design/specs/lf.md", _spec_text("lf"))
    crlf = _write(
        tmp_path / "projects/alpha/design/specs/crlf.md",
        _spec_text("crlf").replace("\n", "\r\n"),
    )
    _run(tmp_path)
    # BYTES, not read_text: universal newlines would normalize CRLF away and the
    # assertion would pass against exactly the corruption it exists to catch.
    assert b"\r\n" not in lf.read_bytes()
    assert lf.read_bytes().count(b"\n") == crlf.read_bytes().count(b"\r\n")
    assert f"queue_class: {NEW}\n".encode() in lf.read_bytes()
    assert f"queue_class: {NEW}\r\n".encode() in crlf.read_bytes()


@pytest.mark.parametrize("q", ["", "'", '"'], ids=["bare", "single", "double"])
def test_quoting_is_preserved(tmp_path, q):
    path = _write(
        tmp_path / "projects/alpha/design/specs/q.md", _spec_text("q", quoted=q)
    )
    _run(tmp_path)
    assert f"queue_class: {q}{NEW}{q}\n" in path.read_text(encoding="utf-8")


def test_a_longer_value_that_merely_starts_with_the_retired_one_is_untouched(tmp_path):
    path = _write(
        tmp_path / "projects/alpha/design/specs/longer.md",
        _spec_text("longer", value=OLD + "-v2"),
    )
    before = path.read_bytes()
    assert mq.plan_migration(tmp_path, "alpha").rewrites == []
    assert path.read_bytes() == before


def test_trailing_comment_on_the_line_survives(tmp_path):
    path = _write(
        tmp_path / "projects/alpha/design/specs/c.md",
        f"---\nslug: c\nqueue_class: {OLD}  # carried from the parent\n---\n\nbody\n",
    )
    _run(tmp_path)
    assert f"queue_class: {NEW}  # carried from the parent\n" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def test_an_undecodable_file_is_named_not_silently_skipped(tmp_path):
    bad = tmp_path / "projects/alpha/design/specs/utf16.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(_spec_text("utf16").encode("utf-16"))
    plan = mq.plan_migration(tmp_path, "alpha")
    assert [p.name for p, _ in plan.unreadable] == ["utf16.md"]
    assert "UNREADABLE" in mq.render(plan, dry_run=True)


def test_scanned_counts_every_parsed_file_not_only_the_hits(tree):
    """The two numbers must be able to DIFFER, or the operator's record is meaningless.

    `scanned` is the corpus-coverage half of the line pasted into the merge PR
    (`project=... scanned=N to_rewrite=M`). Fused - counted inside the `if hit` branch -
    every run reads `M/M`, and a migration that reached one file in five hundred becomes
    indistinguishable from one that reached the whole corpus. The fixture carries an
    in-reach spec with no `queue_class` line precisely so this is exercised.
    """
    plan = mq.plan_migration(tree, "alpha")
    assert plan.scanned == 5
    assert len(plan.rewrites) == 4
    assert plan.scanned > len(plan.rewrites)


def test_render_marks_a_dry_run_as_such(tree):
    out = mq.render(mq.plan_migration(tree, "alpha"), dry_run=True)
    assert "[DRY-RUN]" in out and "no file was written" in out
    assert "[APPLY]" in mq.render(mq.plan_migration(tree, "alpha"), dry_run=False)


def test_cli_dry_run_writes_nothing_and_reports(tree, capsys):
    before = _digest(tree)
    rc = mq.main(["--project", "alpha", "--dry-run", "--data-root", str(tree)])
    assert rc == 0
    assert _digest(tree) == before
    assert "to_rewrite=4" in capsys.readouterr().out


def test_cli_refuses_a_bare_run_and_names_both_modes(tree, capsys):
    """No mode named = no run. Neither default is safe, so neither is taken.

    Writing by default is a trap: every documented invocation is `--dry-run`, and the bare
    form would rewrite a live corpus for anyone who typed the documented command without
    its last word. Dry-running by default is the SAME trap facing the other way - the run
    prints `to_rewrite=N`, exits 0, changes nothing, and an operator who believed they had
    just migrated reads it as a finished migration.
    """
    before = _digest(tree)
    rc = mq.main(["--project", "alpha", "--data-root", str(tree)])
    assert rc != 0
    assert _digest(tree) == before
    err = capsys.readouterr().err
    assert "--dry-run" in err and "--apply" in err


def test_cli_apply_writes(tree, capsys):
    rc = mq.main(["--project", "alpha", "--apply", "--data-root", str(tree)])
    assert rc == 0
    assert "[APPLY]" in capsys.readouterr().out
    live = tree / "projects/alpha/design/specs/live-spec.md"
    assert f"queue_class: {NEW}" in live.read_text(encoding="utf-8")


def test_cli_refuses_both_modes_at_once(tree):
    before = _digest(tree)
    rc = mq.main(["--project", "alpha", "--dry-run", "--apply", "--data-root", str(tree)])
    assert rc != 0
    assert _digest(tree) == before


# --------------------------------------------------------------------------- #
# The invariant vocabulary.py deliberately guards by test rather than by import
# --------------------------------------------------------------------------- #
def test_current_value_is_dispatchable():
    """The migration's TO value must be one the allow-list accepts.

    `vocabulary.py` explains why this is a test and not an import: the allow-list spells
    `fleet-buildable` literally so a reader gets a one-line answer, and this is what stops
    the two spellings drifting into a migration that rewrites the corpus to a value
    nothing will dispatch.
    """
    assert CURRENT_QUEUE_CLASS in DISPATCHABLE_QUEUE_CLASSES
    assert RETIRED_QUEUE_CLASS not in DISPATCHABLE_QUEUE_CLASSES


def test_retired_value_is_anchored_to_a_record_written_while_it_was_live():
    """Pin the constant to the string that was ACTUALLY retired. Do not delete this.

    Every other reader is satisfied by any string at all: the migration tests GENERATE
    their fixture tree from the constant, so they stay self-consistent under a wrong
    value, and the two membership assertions are NEGATIVE, which a wrong string passes
    more easily than a right one. So a one-character typo here leaves the whole suite
    green while `--dry-run` reports `to_rewrite=0` and `--apply` writes nothing - byte for
    byte the output of a FINISHED migration - and the fleet dispatches nothing at all,
    because after the hard cut every unmigrated spec is out of vocabulary.

    CHANGELOG.md is the anchor rather than the harness fixture: both carry the historical
    spelling and are permanently exempt from the vocabulary sweep for that reason, but the
    CHANGELOG is immutable by declared repository convention, so it cannot be re-carved
    out from under this assertion the way a recorded fixture legitimately could be.
    """
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    # As a WHOLE TOKEN, not a substring: the likeliest way to get this constant wrong is a
    # truncation, and a truncated value is a substring of the correct one, so a plain `in`
    # would pass the exact mutation this test exists to catch. The boundary excludes the
    # hyphen too, since every value in this vocabulary is hyphenated.
    anchored = re.compile(r"(?<![\w-])" + re.escape(RETIRED_QUEUE_CLASS) + r"(?![\w-])")
    assert anchored.search(changelog), (
        "RETIRED_QUEUE_CLASS does not appear in CHANGELOG.md, which recorded the value "
        "while it was still live. This anchor exists because nothing else in the suite "
        "can tell a correct retired spelling from a wrong one - a wrong one makes the "
        "migration a silent no-op that looks exactly like a completed run. Fix the "
        "constant to match the historical record; do not delete this test."
    )
