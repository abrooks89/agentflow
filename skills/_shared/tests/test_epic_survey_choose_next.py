"""choose-next's view of the survey: who is a candidate, who a candidate blocks, what /project gets.

Behaviour of `is_candidate`, `blocks_count` and `handoff_path`; the prose that tells choose-next
to use them is pinned in test_choose_next_bucket_prose.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentflow import epic_survey as s
from agentflow.epic_survey import EpicRecord, survey_epics

PAST_CUTOFF = "2020-01-01T00:00:00+00:00"


def _rec(slug: str, state: str, deps=(), project: str = "otherproj") -> EpicRecord:
    return EpicRecord(
        slug=slug, title=slug, lifecycle_state=state, depends_on=list(deps), project=project
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Candidate set
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state", ["speccing", "drafted", "approved-for-autonomous", "planning"])
def test_every_pre_run_stage_is_a_candidate(state):
    assert s.is_candidate(_rec("x", state))


@pytest.mark.parametrize(
    "state",
    ["done", "archived", "superseded", "running", "abandoned", "done_on_hold",
     "pinned", "parked", "reference"],
)
def test_finished_running_and_placeholder_states_are_not_candidates(state):
    assert not s.is_candidate(_rec("x", state))


@pytest.mark.parametrize("dep_state", ["drafted", "approved-for-autonomous"])
def test_blocked_status_calls_a_drafted_or_approved_dep_a_candidate(dep_state):
    # A candidate dep outranks an unknown one, so a child waiting on a drafted parent and a
    # ghost reads CHAINED. Before the shared predicate the drafted parent was not a candidate
    # and the ghost made it MISSING-DEP, while a speccing parent gave CHAINED.
    recs = [_rec("parent", dep_state), _rec("child", "speccing", ["parent", "ghost"])]
    assert s.blocked_status(recs[1], recs) == "CHAINED"


def test_blocked_status_control_a_placeholder_dep_is_not_a_candidate():
    recs = [_rec("parent", "pinned"), _rec("child", "speccing", ["parent", "ghost"])]
    assert s.blocked_status(recs[1], recs) == "MISSING-DEP"


def test_drafted_and_approved_specs_reach_the_candidate_set_from_disk(tmp_path):
    specs = tmp_path / "projects" / "otherproj" / "design" / "specs"
    for slug, state in (("a", "drafted"), ("b", "approved-for-autonomous"), ("c", "done")):
        _write(specs / f"{slug}.md", f"---\nslug: {slug}\ncurrent_state: {state}\n---\n\nbody\n")
    recs = survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)
    assert sorted(r.slug for r in recs if s.is_candidate(r)) == ["a", "b"]


# --------------------------------------------------------------------------- #
# blocks_count
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("dependent_state", "expected"),
    [("done", 0), ("superseded", 0), ("abandoned", 0), ("done_on_hold", 0),
     ("speccing", 1), ("drafted", 1), ("running", 1)],
)
def test_blocks_count_counts_only_dependents_still_to_run(dependent_state, expected):
    recs = [_rec("core", "speccing"), _rec("downstream", dependent_state, ["core"])]
    assert s.blocks_count(recs[0], recs) == expected


# --------------------------------------------------------------------------- #
# handoff_path
# --------------------------------------------------------------------------- #
def _plan(slug: str, extra: str = "") -> str:
    return (
        f"---\nepic: {slug} epic\nslug: {slug}\nphases: [phase-1]\n"
        "tasks:\n  1:\n    phase: phase-1\n    deps: []\ntest_commands: {}\n"
        f"{extra}---\n\nplan body\n"
    )


def test_handoff_path_exists_for_plan_only_unset_project_and_design_suffixed(tmp_path):
    root = tmp_path / "projects" / "otherproj" / "design"
    _write(root / "plans" / "2026-01-01-planonly.md", _plan("planonly", "project: otherproj\n"))
    _write(root / "plans" / "2026-01-01-noproject.md", _plan("noproject"))
    _write(
        root / "specs" / "2026-01-01-suffixed-design.md",
        "---\ntitle: suffixed\nproject: otherproj\n---\n\nbody\n",
    )
    recs = {r.slug: r for r in survey_epics(tmp_path, PAST_CUTOFF, include_archived=True)}
    assert recs["noproject"].project is None
    for slug in ("planonly", "noproject", "suffixed"):
        path = s.handoff_path(recs[slug], tmp_path)
        assert path is not None and path.is_file(), slug
        assert s._normalize_slug_stem(path.stem) == slug


def _moved(slug: str, project, source: Path) -> EpicRecord:
    """A record whose surveyed source file is *source* (which may not exist)."""
    return EpicRecord(
        slug=slug, title=slug, lifecycle_state="speccing", project=project,
        source_path=str(source),
    )


def test_handoff_path_falls_back_when_the_source_moved(tmp_path):
    specs = tmp_path / "projects" / "otherproj" / "design" / "specs"
    only = _write(specs / "2026-02-01-moved.md", "---\ntitle: new\n---\n")
    gone = _moved("moved", "otherproj", specs / "vanished.md")
    assert s.handoff_path(gone, tmp_path) == only.resolve()
    assert s.handoff_path(_rec("absent", "speccing"), tmp_path) is None


def test_handoff_path_refuses_two_matches_rather_than_picking_by_name(tmp_path):
    specs = tmp_path / "projects" / "otherproj" / "design" / "specs"
    _write(specs / "2026-01-01-moved.md", "---\ntitle: old\n---\n")
    _write(specs / "2026-02-01-moved.md", "---\ntitle: new\n---\n")
    assert s.handoff_path(_moved("moved", "otherproj", specs / "gone.md"), tmp_path) is None


def test_handoff_path_never_searches_other_projects_for_an_unset_project(tmp_path):
    _write(tmp_path / "projects" / "elsewhere" / "design" / "specs" / "x.md", "---\n---\n")
    rec = _moved("x", None, tmp_path / "projects" / "gone" / "x.md")
    assert s.handoff_path(rec, tmp_path) is None


@pytest.mark.parametrize("project", ["*", "else*", "[eo]lsewhere", "?lsewhere"])
def test_handoff_path_refuses_a_glob_character_project(tmp_path, project):
    _write(tmp_path / "projects" / "elsewhere" / "design" / "specs" / "x.md", "---\n---\n")
    rec = _moved("x", project, tmp_path / "projects" / "gone" / "x.md")
    assert s.handoff_path(rec, tmp_path) is None


@pytest.mark.parametrize("project", ["../../outside", "..", "otherproj/../../../outside"])
def test_handoff_path_refuses_a_traversal_project(tmp_path, project):
    root = tmp_path / "root"
    (root / "projects" / "otherproj").mkdir(parents=True)
    # Where each traversal lands once `projects/<project>/design/specs` is joined.
    for target in (tmp_path / "outside", root):
        _write(target / "design" / "specs" / "x.md", "---\n---\n")
    rec = _moved("x", project, root / "projects" / "gone" / "x.md")
    assert s.handoff_path(rec, root) is None


def test_handoff_path_refuses_a_source_path_outside_data_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = _write(tmp_path / "outside" / "x.md", "---\n---\n")
    assert s.handoff_path(_moved("x", "otherproj", outside), root) is None
    # The same check through `..` segments that start inside the root.
    dotted = root / "projects" / ".." / ".." / "outside" / "x.md"
    assert s.handoff_path(_moved("x", "otherproj", dotted), root) is None


def test_handoff_path_refuses_a_symlink_out_of_data_root(tmp_path):
    root = tmp_path / "root"
    outside = _write(tmp_path / "outside" / "x.md", "---\n---\n")
    link = root / "projects" / "otherproj" / "design" / "specs" / "x.md"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError as exc:  # unprivileged Windows; the `..` case above covers the resolve check
        pytest.skip(f"cannot create a symlink here: {exc}")
    assert s.handoff_path(_moved("x", "otherproj", link), root) is None
    gone = _moved("x", "otherproj", root / "projects" / "otherproj" / "design" / "specs" / "g.md")
    assert s.handoff_path(gone, root) is None


def test_handoff_path_returns_none_for_a_nul_source_path(tmp_path):
    # `Path.resolve()` raises ValueError on an embedded NUL; the contract is None, not a raise.
    root = tmp_path / "root"
    specs = root / "projects" / "otherproj" / "design" / "specs"
    nul = _moved("x", "otherproj", specs / "x\x00.md")
    assert s.handoff_path(nul, root) is None
    # A refused source still falls back to the record's own project, like any other miss.
    only = _write(specs / "2026-01-01-x.md", "---\n---\n")
    assert s.handoff_path(nul, root) == only.resolve()


def test_handoff_path_returns_none_for_a_symlink_loop(tmp_path):
    root = tmp_path / "root"
    specs = root / "projects" / "otherproj" / "design" / "specs"
    specs.mkdir(parents=True)
    a, b = specs / "2026-01-01-x.md", specs / "loop-b.txt"
    try:
        a.symlink_to(b)
        b.symlink_to(a)
    except OSError as exc:  # unprivileged Windows; the monkeypatched test below covers it
        pytest.skip(f"cannot create a symlink here: {exc}")
    # As the source path, and again as the only fallback match for slug "x".
    assert s.handoff_path(_moved("x", "otherproj", a), root) is None
    gone = _moved("x", "otherproj", specs / "gone.md")
    assert s.handoff_path(gone, root) is None


def test_handoff_path_treats_a_resolve_runtime_error_as_no_file(tmp_path, monkeypatch):
    # Python <3.13 raises RuntimeError("Symlink loop from ...") from non-strict resolve().
    real = Path.resolve

    def resolve(self, *args, **kwargs):
        if self.name in ("loop.md", "2026-01-01-x.md"):
            raise RuntimeError(f"Symlink loop from {self!s}")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    root = tmp_path / "root"
    specs = root / "projects" / "otherproj" / "design" / "specs"
    specs.mkdir(parents=True)
    assert s.handoff_path(_moved("x", "otherproj", specs / "loop.md"), root) is None
    (specs / "2026-01-01-x.md").write_text("---\n---\n", encoding="utf-8")  # the only fallback match
    assert s.handoff_path(_moved("x", "otherproj", specs / "gone.md"), root) is None


def test_handoff_path_lets_a_recursion_error_through(tmp_path, monkeypatch):
    # RecursionError subclasses RuntimeError; only the loop RuntimeError itself means "no file".
    real = Path.resolve

    def resolve(self, *args, **kwargs):
        if self.name == "deep.md":
            raise RecursionError(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(RecursionError):
        s.handoff_path(_moved("x", "otherproj", tmp_path / "deep.md"), tmp_path)


def test_handoff_path_lets_any_other_resolve_error_through(tmp_path, monkeypatch):
    # Only the NUL ValueError means "no such file"; anything else is a fault to surface, not a None.
    real = Path.resolve

    def resolve(self, *args, **kwargs):
        if self.name == "denied.md":
            raise PermissionError(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    rec = _moved("x", "otherproj", tmp_path / "denied.md")
    with pytest.raises(PermissionError):
        s.handoff_path(rec, tmp_path)


SKILL = Path(__file__).resolve().parents[2] / "choose-next" / "SKILL.md"


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_skill_handles_a_none_handoff_on_the_custom_slug_path():
    # "Other" + a custom slug calls handoff_path too; with no None step an agent runs `/project None`.
    custom = _skill().split('If the user picks "Other" with a custom slug:', 1)[1].split("\n\n", 1)[0]
    assert "handoff_path" in custom and "returns None" in custom


def test_skill_lists_the_same_none_causes_in_step_5_and_failure_modes():
    # Both passages explain None; no file matching is the commonest cause and must be in each.
    text = _skill()
    causes = "a record with no plain `project:` name, two matching files, or none"
    step5 = text.split("### Step 5:", 1)[1].split("\n### ", 1)[0]
    row = next(ln for ln in text.splitlines() if ln.startswith("| `handoff_path` returns None"))
    assert causes in step5 and causes in row
