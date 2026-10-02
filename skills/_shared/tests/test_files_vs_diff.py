"""gh#161: the declaration a seam check trusts, measured against what the epic actually touched.

Every test drives REAL git against a real local repository - no mock stands in for the diff,
because the whole point of this module is that the ARTIFACT, not a hand-written list, is the
source of truth about what an epic touched. There is exactly ONE exception, and it says why in
its own docstring: `touched_paths`'s sort-and-dedup contract cannot be shown against real git,
because git's tree-walk order coincides with a byte sort of the full paths, so a real fixture
passes whether the module sorts or merely inherits.

Every spec fixture is shaped like a real agentflow spec (frontmatter, Problem, a `## Files`
section written in the house style, a `## Test` section after it) rather than a bare heading.
The section is not the only markdown in the file in production, and a parser that only works
on a stub would be measuring something nobody writes. Where a fixture reproduces a real
declaration it says which epic it came from.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from agentflow import files_vs_diff as fvd

BT = chr(96)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _spec(files_body: str, *, heading: str = "## Files") -> str:
    """A spec of the shape the fleet actually writes, with `files_body` as its Files section."""
    return (
        "---\n"
        "slug: a-real-shaped-epic\n"
        "domain: infra-meta\n"
        "current_state: done\n"
        "queue_class: fleet-buildable\n"
        "priority: 220\n"
        "---\n\n"
        "# A real-shaped epic\n\n"
        "## Problem\n\n"
        "Two lanes can edit one file and the only thing standing between them is the listing\n"
        "below.\n\n"
        f"{heading}\n\n"
        f"{files_body}\n\n"
        "## Test\n\n"
        f"- {BT}skills/_shared/tests/test_nothing.py{BT} proves nothing; this section exists so\n"
        "  the Files section is bounded by a following heading, as it is in every real spec.\n"
    )


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout shaped like this one: a base commit, then one 'merged' commit on top.

    The head commit touches five paths across three directories, which is the ordinary size
    of a merged epic here - the measured fleet median is six.
    """
    r = tmp_path / "checkout"
    subprocess.run(["git", "init", "-b", "master", str(r)], check=True, capture_output=True)
    for k, v in (("user.email", "t@e.st"), ("user.name", "t")):
        _git(r, "config", k, v)

    for rel in ("docs/REGISTRY.md", "skills/_shared/agentflow/registry.py",
                "skills/_shared/agentflow/preflight.py", "skills/_shared/tests/test_registry.py",
                "skills/_shared/tests/test_missed_wake_alarm.py", "skills/steward/SKILL.md",
                "skills/dev-manager/SKILL.md", "harness/check.py", "README.md"):
        _write(r, rel)
    _commit(r, "base")

    for rel in ("docs/REGISTRY.md", "skills/_shared/agentflow/registry.py",
                "skills/_shared/tests/test_registry.py",
                "skills/_shared/tests/test_missed_wake_alarm.py", "skills/steward/SKILL.md"):
        _write(r, rel, "changed\n")
    _commit(r, "the merged epic")
    return r


def _run(repo: Path, files_body: str, **kw) -> fvd.FilesDelta:
    return fvd.compare(repo, _spec(files_body), "HEAD^", "HEAD", **kw)


# --------------------------------------------------------------------------- #
# The two deltas
# --------------------------------------------------------------------------- #
def test_a_touched_file_the_spec_never_named_is_undeclared(repo):
    """gh#161's live instance, in miniature.

    E-0220 declared `registry.py` and `test_registry.py` and touched
    `test_missed_wake_alarm.py` as well. The omission sat inside a directory ANOTHER epic held,
    so it hid exactly the collision the disjointness check exists to find.
    """
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the ownership table these reason strings cite by name",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - repoint the actor gates",
        f"- {BT}skills/_shared/tests/test_registry.py{BT} - the actor-gate tests",
        f"- {BT}skills/steward/SKILL.md{BT} - absorb the pump",
    ]))
    assert d.undeclared == ("skills/_shared/tests/test_missed_wake_alarm.py",)
    assert d.over_declared == ()
    assert d.clean is False
    assert all(e.kind == fvd.FILE for e in d.entries)
    assert all(e.decidable is True for e in d.entries), (
        "an exact path is the one shape whose completeness is a question with an answer"
    )


def test_a_declared_file_nothing_touched_is_over_declared_and_not_undeclared(repo):
    """The opposite failure: a seam other lanes are parked behind that was never used."""
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the publish contract",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - the actor gates",
        f"- {BT}skills/_shared/agentflow/preflight.py{BT} - the shared preflight helper",
        f"- {BT}skills/_shared/tests/test_registry.py{BT} - the gate tests",
        f"- {BT}skills/_shared/tests/test_missed_wake_alarm.py{BT} - the alarm tests",
        f"- {BT}skills/steward/SKILL.md{BT} - the pump",
    ]))
    assert [e.path for e in d.over_declared] == ["skills/_shared/agentflow/preflight.py"]
    assert d.undeclared == ()


def test_the_two_deltas_are_reported_apart(repo):
    """One run can carry both, and merging them into a single 'mismatch' count loses which."""
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the publish contract",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - the actor gates",
        f"- {BT}skills/_shared/agentflow/preflight.py{BT} - NOT actually touched",
        f"- {BT}skills/_shared/tests/test_registry.py{BT} - the gate tests",
        f"- {BT}skills/steward/SKILL.md{BT} - the pump",
    ]))
    assert d.undeclared == ("skills/_shared/tests/test_missed_wake_alarm.py",)
    assert [e.path for e in d.over_declared] == ["skills/_shared/agentflow/preflight.py"]


def test_no_path_is_reported_in_both_deltas_at_once(repo):
    """Pinned where NEITHER set is a literal, so the assertion can actually fail.

    Written as three literals it could not: two exact-set asserts already decide the third,
    and a disjointness claim that cannot fail is decoration. Here the declaration is wrong in
    both directions at once and neither list is spelled out.
    """
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - touched",
        f"- {BT}skills/_shared/agentflow/preflight.py{BT} - never touched",
        f"- {BT}harness/check.py{BT} - never touched",
        f"- {BT}README.md{BT} - never touched",
    ]))
    assert len(d.undeclared) == 4 and len(d.over_declared) == 3
    assert set(d.undeclared).isdisjoint({e.path for e in d.over_declared})


def test_a_rename_is_checked_on_both_of_its_names(repo):
    """`--no-renames`: the OLD path was edited too, and a seam is about paths, not identities."""
    _git(repo, "mv", "harness/check.py", "harness/check_renamed.py")
    _git(repo, "commit", "-m", "rename")
    d = fvd.compare(repo, _spec(f"- {BT}harness/check_renamed.py{BT} - the new name"),
                    "HEAD^", "HEAD")
    assert "harness/check.py" in d.undeclared


def test_a_declared_file_the_range_deleted_is_covered_not_a_phantom_over_declaration(repo):
    """A file the epic REMOVED is not in the tree at head, and is still a real declaration.

    Without the `or p in touched` arm the entry classifies as a path that does not exist, the
    epic reads as having over-declared the file it deliberately deleted, and the deletion
    itself reads as an undeclared touch - two findings from one correct declaration.
    """
    (repo / "harness" / "check.py").unlink()
    _commit(repo, "delete the drill")
    d = fvd.compare(repo, _spec(f"- {BT}harness/check.py{BT} - removed by this epic"),
                    "HEAD^", "HEAD")
    assert [e.kind for e in d.entries] == [fvd.FILE]
    assert d.entries[0].covered == ("harness/check.py",)
    assert d.undeclared == () and d.over_declared == () and d.unresolvable == ()


# --------------------------------------------------------------------------- #
# Pattern coverage is defined here, not inherited from the host platform
# --------------------------------------------------------------------------- #
def test_glob_coverage_is_case_sensitive_and_stops_at_a_directory_boundary(repo):
    """`fnmatch.fnmatch` was wrong twice, and this pins both.

    It folds case on Windows and not elsewhere, so `DOCS/*.md` covered `docs/REGISTRY.md` on a
    daily driver and not in CI - the same spec and the same range giving a different answer on
    a different machine. And its `*` crosses `/`, so `skills/*/SKILL.md` silently absorbed a
    file two levels down, biasing the whole measurement toward UNDER-reporting. Under the old
    semantics this test is red on Windows for both reasons and on Linux for the second.
    """
    _write(repo, "skills/steward/nested/SKILL.md", "changed\n")
    _write(repo, "skills/steward/SKILL.md", "changed again\n")
    _write(repo, "docs/REGISTRY.md", "changed again\n")
    _commit(repo, "a nested skill file, its parent, and one more doc touch")
    d = fvd.compare(repo, _spec("\n".join([
        f"- {BT}skills/*/SKILL.md{BT} - the loop skills, exactly one level down",
        f"- {BT}DOCS/*.md{BT} - the wrong case for a real directory",
    ])), "HEAD^", "HEAD")
    assert d.undeclared == ("docs/REGISTRY.md", "skills/steward/nested/SKILL.md")
    assert [e.path for e in d.over_declared] == ["DOCS/*.md"]


def test_a_double_star_is_the_way_to_cross_a_directory_boundary(repo):
    """`*` refusing to cross `/` would be useless without a spelling that does."""
    _write(repo, "skills/steward/nested/SKILL.md", "changed\n")
    _commit(repo, "a nested skill file")
    d = fvd.compare(repo, _spec(f"- {BT}skills/**/SKILL.md{BT} - every skill file, any depth"),
                    "HEAD^", "HEAD")
    assert d.undeclared == ()
    assert d.patterns_used[0].covered == ("skills/steward/nested/SKILL.md",)


def test_a_brace_alternation_covers_each_path_it_names(repo):
    """`{` was missing from the glob character set, so a brace entry contributed ZERO coverage
    and every path it named was reported undeclared - a false positive in the headline number.

    Supported rather than refused: a brace names an exact set, so treating it as a pattern
    reports those paths as declared. It is still NOT DECIDABLE, because the entry bounds a set
    rather than enumerating one.
    """
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - the gates",
        f"- {BT}skills/_shared/tests/{BT} - the tests",
        f"- {BT}skills/{{steward,dev-manager}}/SKILL.md{BT} - both loop skills",
    ]))
    assert d.undeclared == ()
    brace = [e for e in d.patterns_used if "{" in e.path]
    assert [e.covered for e in brace] == [("skills/steward/SKILL.md",)]
    assert brace[0].decidable is False


# --------------------------------------------------------------------------- #
# The third class: entries that are not resolvable paths
# --------------------------------------------------------------------------- #
def test_a_directory_entry_absorbs_what_falls_under_it_and_is_flagged_not_decidable(repo):
    """`skills/_shared/tests/` is the real shape from the in-flight epic in gh#161's story.

    It legitimately covers the two touched test files, so they are NOT undeclared - and the
    entry is named in `patterns_used` because completeness cannot be decided for it even in
    principle, which is the caveat on the zero.
    """
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the publish contract",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - the actor gates",
        f"- {BT}skills/_shared/tests/{BT} - whatever the gate tests need",
        f"- {BT}skills/steward/SKILL.md{BT} - the pump",
    ]))
    assert d.undeclared == ()
    assert [e.path for e in d.patterns_used] == ["skills/_shared/tests/"]
    assert d.patterns_used[0].covered == (
        "skills/_shared/tests/test_missed_wake_alarm.py",
        "skills/_shared/tests/test_registry.py",
    )
    assert d.patterns_used[0].decidable is False
    assert d.clean is False, "a zero reached through a directory entry is not a clean declaration"


def test_a_directory_entry_written_without_its_trailing_slash_is_still_a_directory(repo):
    """`harness` and `harness/` mean the same thing to a human; the tree settles it, not the string."""
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/_shared{BT} - everything under the shared package",
        f"- {BT}skills/steward/SKILL.md{BT} - the pump",
    ]))
    assert [e.path for e in d.patterns_used] == ["skills/_shared/"]
    assert d.undeclared == ()


def test_a_glob_entry_covers_by_pattern_and_is_also_not_decidable(repo):
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - the gates",
        f"- {BT}skills/_shared/tests/test_*.py{BT} - the tests that move with them",
        f"- {BT}skills/*/SKILL.md{BT} - the loop skills that cite the moved verbs",
    ]))
    assert d.undeclared == ()
    assert {e.path for e in d.patterns_used} == {"skills/_shared/tests/test_*.py",
                                                "skills/*/SKILL.md"}
    assert all(e.decidable is False for e in d.patterns_used)


def test_a_directory_entry_nothing_touched_is_over_declared(repo):
    """A directory can be over-declared even though it can never be under-declared."""
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/_shared/{BT} - the shared package",
        f"- {BT}skills/steward/SKILL.md{BT} - the pump",
        f"- {BT}harness/{BT} - a scenario making the drill citable rather than asserted",
    ]))
    assert [e.path for e in d.over_declared] == ["harness/"]


def test_a_trailing_slash_naming_no_directory_here_is_unresolvable_not_over_declared(repo):
    """The one branch that used to take the string on its word.

    Any entry ending in `/` was called a directory without asking the tree, so five ghost
    directories were reported OVER-DECLARED across the corpus. "You claimed a seam you never
    used" and "you named something that is not there" are different repairs, and the second
    one is the true statement about a path that exists nowhere.
    """
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - the gates",
        f"- {BT}skills/_shared/tests/{BT} - the tests",
        f"- {BT}skills/steward/SKILL.md{BT} - the pump",
        f"- {BT}skills/ghost/{BT} - a directory that exists nowhere in this repository",
    ]))
    assert [e.raw for e in d.unresolvable] == ["skills/ghost/"]
    assert d.unresolvable[0].code == fvd.R_MISPLACED
    assert "names no directory in the tree" in d.unresolvable[0].reason
    assert "skills/ghost/" not in [e.path for e in d.over_declared]


def test_a_data_root_placeholder_is_unresolvable_and_counts_in_neither_delta(repo):
    """Taken verbatim from the retire-the-autoloop-queue-class-value spec.

    It names a real file that is genuinely outside this repository. Calling it over-declared
    would cry wolf on every spec that touches the data root, and matching it against anything
    would be a fiction.
    """
    ph = "<data_root>/projects/agentflow/design/plans/2026-08-05-public-release-waterfall.md"
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - the gates",
        f"- {BT}skills/_shared/tests/{BT} - the tests",
        f"- {BT}skills/steward/SKILL.md{BT} - the pump",
        f"- {BT}{ph}{BT} - the NFR-4 pointer",
    ]))
    assert [e.raw for e in d.unresolvable] == [ph]
    assert "placeholder" in d.unresolvable[0].reason
    assert [e.raw for e in d.out_of_repo] == [ph]
    assert ph not in [e.path for e in d.over_declared]
    assert d.undeclared == ()


def test_every_unresolvable_reason_is_pinned_and_carries_its_class(repo):
    """The reason strings ARE the product: `render` quotes them per entry and a spec author
    repairs what the reason names. Pinning one branch and leaving five to a mutation is the
    same gap as not testing them at all, so all six shapes are pinned in one table.

    The `code` column is the other half of gh#161's fifth finding: 32 of 52 unresolvable
    entries across the corpus wore the same generic reason, mixing parser artefacts with real
    mis-pathed declarations. A reader cannot repair a bucket that cannot tell a typo from a
    footnote, so noise, spec defect, and out-of-repo-by-design are now separate classes.
    """
    shapes = {
        "./": (fvd.R_EMPTY, "empty once the citation markers are removed"),
        "/": (fvd.R_SEPARATOR, "no path characters"),
        "and nothing anywhere reported it.": (fvd.R_PROSE, "prose, not a path"),
        "registry.py": (fvd.R_BARE_MENTION, "a bare name with no separator"),
        "<data_root>/projects/agentflow/status/registry.md":
            (fvd.R_PLACEHOLDER, "carries a <placeholder>"),
        "/srv/notes/plan.md": (fvd.R_ABSOLUTE, "absolute path, not repo-relative"),
        "../shared/x.py": (fvd.R_PARENT_RELATIVE, "parent-relative"),
        "skills/_shared/agentflow/nonesuch.py": (fvd.R_MISPLACED, "names no path in the tree"),
    }
    d = _run(repo, "\n".join(f"- {BT}{raw}{BT} - one shape" for raw in shapes))
    assert len(d.unresolvable) == len(shapes), "every one of these is unplaceable"
    for e in d.unresolvable:
        code, fragment = shapes[e.raw]
        assert e.code == code, e.raw
        assert fragment in e.reason, e.raw

    assert {e.code for e in d.noise} == {
        fvd.R_EMPTY, fvd.R_SEPARATOR, fvd.R_PROSE, fvd.R_BARE_MENTION}
    assert {e.code for e in d.spec_defects} == {
        fvd.R_ABSOLUTE, fvd.R_PARENT_RELATIVE, fvd.R_MISPLACED}
    assert [e.code for e in d.out_of_repo] == [fvd.R_PLACEHOLDER]


def test_a_separator_between_two_inline_code_spans_is_not_a_declared_file(repo):
    """The borrowed parser reads the `/` in ``reap`/`stand-down`` as a pathlike. Real: it fires
    twice on E-0220's own Files section. It has to land somewhere, and the honest somewhere is
    the unresolvable list with the reason, not a phantom over-declaration.

    It fires TWICE and is reported ONCE: two identical raw strings are one declaration, and
    counting them separately made "declared N entries" not a count of declarations.
    """
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - the reason strings for",
        f"  {BT}reap{BT}/{BT}stand-down{BT}/{BT}repair{BT}",
        f"- {BT}skills/_shared/tests/{BT} - the tests",
        f"- {BT}skills/steward/SKILL.md{BT} - the pump",
    ]))
    assert [e.raw for e in d.unresolvable] == ["/"]
    assert "separator" in d.unresolvable[0].reason
    assert d.duplicate_entries == ("/",)
    assert d.declared_count == 5, "six pathlikes parsed, one of them twice"


def test_the_same_path_declared_twice_is_one_declaration(repo):
    """8 duplicate raw strings across 6 epics. The dropped string is KEPT and reported, not
    discarded - a spec listing one path twice is worth seeing, it just is not two seams."""
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - the gates",
        f"- {BT}docs/REGISTRY.md{BT} - listed again under a second bullet",
    ]))
    assert d.declared_count == 2
    assert d.duplicate_entries == ("docs/REGISTRY.md",)
    assert [e.raw for e in d.entries].count("docs/REGISTRY.md") == 1
    assert "1 duplicate raw entry deduped" in "\n".join(fvd.render(d))


def test_a_line_citation_names_the_file_it_cites(repo):
    """`registry.py:1267` is a pointer into a declared file, not a second file."""
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/_shared/agentflow/registry.py:1267{BT} - two merged comment blocks",
        f"- {BT}skills/_shared/tests/{BT} - the tests",
        f"- {BT}skills/steward/SKILL.md{BT} - the pump",
    ]))
    assert d.undeclared == ()
    assert d.unresolvable == ()


def test_an_unresolvable_entry_can_never_hide_a_touched_path(repo):
    """The property that makes the third class safe: it contributes NO coverage."""
    d = _run(repo, "\n".join([
        f"- {BT}<data_root>/projects/agentflow/status/registry.md{BT} - the board",
        f"- {BT}skills/_shared/agentflow/nonesuch.py{BT} - a path that does not exist",
    ]))
    assert set(d.undeclared) == set(d.touched)
    assert len(d.unresolvable) == 2
    assert d.mis_pathed == ()


# --------------------------------------------------------------------------- #
# One typo is one finding
# --------------------------------------------------------------------------- #
def test_a_mis_pathed_declaration_is_reported_once_naming_both_strings(repo):
    """E-0220 declared `skills/_shared/agentflow/tests/test_registry.py`; the tests live one
    level up. That produced TWO findings - the entry unresolvable AND the real file undeclared
    - and 8 of 61 undeclared paths in the corpus were paths the spec HAD declared, one segment
    off. The number being quoted was the inflated one.

    Reported once, naming the string the spec wrote and the path the range touched, and held
    out of both lists rather than dropped from either quietly: `render` says the count.
    """
    wrong = "skills/_shared/agentflow/tests/test_registry.py"
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - the gates",
        f"- {BT}{wrong}{BT} - the actor-gate tests",
        f"- {BT}skills/steward/SKILL.md{BT} - the pump",
    ]))
    assert [(m.declared, m.actual) for m in d.mis_pathed] == [
        (wrong, "skills/_shared/tests/test_registry.py")]
    assert "skills/_shared/tests/test_registry.py" not in d.undeclared
    assert wrong not in [e.raw for e in d.unresolvable]
    assert wrong not in [e.path for e in d.over_declared]
    assert d.undeclared == ("skills/_shared/tests/test_missed_wake_alarm.py",)
    report = "\n".join(fvd.render(d))
    assert "MIS-PATHED (1)" in report
    assert f"{wrong}  ->  skills/_shared/tests/test_registry.py" in report
    assert "excluding 1 reported as MIS-PATHED below" in report, (
        "the smaller UNDECLARED count must never be a quiet one"
    )


def test_an_ambiguous_basename_is_not_paired(repo):
    """Uniqueness on BOTH sides, because a guess here erases a real undeclared path.

    `SKILL.md` is the basename that makes this concrete: the range touched two files carrying
    it, so nothing about the declaration says which one it meant.
    """
    _write(repo, "skills/dev-manager/SKILL.md", "changed\n")
    _write(repo, "skills/steward/SKILL.md", "changed again\n")
    _commit(repo, "touch both skill files")
    d = fvd.compare(repo, _spec("\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/loop/SKILL.md{BT} - a skill directory that does not exist",
    ])), "HEAD^", "HEAD")
    assert d.mis_pathed == ()
    assert "skills/dev-manager/SKILL.md" in d.undeclared
    assert [e.raw for e in d.unresolvable] == ["skills/loop/SKILL.md"]


def test_a_bare_module_mention_never_absorbs_a_touched_path(repo):
    """`registry.py` shares a basename with a touched file and is still not a declaration.

    Only the `misplaced` class is eligible to pair. A bare name is parser noise - the section
    parser promoted a word out of prose - and letting noise absorb a touched file would
    launder an under-declaration into a typo.
    """
    d = _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}registry.py{BT} - mentioned by name in a sentence, not declared as a path",
    ]))
    assert d.mis_pathed == ()
    assert "skills/_shared/agentflow/registry.py" in d.undeclared
    assert [e.code for e in d.unresolvable] == [fvd.R_BARE_MENTION]


# --------------------------------------------------------------------------- #
# "Told you nothing" is a third outcome, not a maximal miss
# --------------------------------------------------------------------------- #
def test_a_spec_with_no_files_section_is_not_an_epic_that_under_declared_everything(repo):
    d = fvd.compare(repo, _spec("- nothing", heading="## Scope"), "HEAD^", "HEAD")
    assert d.section_present is False
    assert d.declaration_usable is False
    assert d.undeclared == ()
    assert len(d.touched) == 5
    assert "no `## Files` section" in "\n".join(fvd.render(d))


def test_a_files_section_naming_no_placeable_path_is_that_same_third_outcome(repo):
    """"the loop skills" is present and unusable; `files_section` already refuses it.

    The report has to SAY which of the two it is. They are different repairs - write a section,
    versus rewrite the one you wrote - and a test that never reads the report leaves the two
    messages swappable with the suite still green.
    """
    d = _run(repo, "- the loop skills\n- the vault side")
    assert d.section_present is True
    assert d.declaration_usable is False
    assert d.undeclared == () and d.over_declared == ()
    report = "\n".join(fvd.render(d))
    assert "names no placeable path" in report
    assert "no `## Files` section" not in report


# --------------------------------------------------------------------------- #
# One epic, several merged commits
# --------------------------------------------------------------------------- #
def test_an_epic_that_landed_in_two_commits_is_measured_as_one_epic(repo):
    """Per-commit runs would report each commit's files over-declared against the other's."""
    _write(repo, "harness/check.py", "second slice\n")
    _commit(repo, "second slice of the same epic")
    first, second = _git(repo, "rev-parse", "HEAD^").strip(), _git(repo, "rev-parse", "HEAD").strip()

    touched = tuple(sorted(set(fvd.touched_paths(repo, f"{first}^", first))
                           | set(fvd.touched_paths(repo, f"{second}^", second))))
    body = "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - the contract",
        f"- {BT}skills/_shared/agentflow/registry.py{BT} - the gates",
        f"- {BT}skills/_shared/tests/{BT} - the tests",
        f"- {BT}skills/steward/SKILL.md{BT} - the pump",
        f"- {BT}harness/check.py{BT} - the drill",
    ])
    united = fvd.compare_paths(repo, _spec(body), touched, second, base="union")
    assert united.undeclared == () and united.over_declared == ()

    per_commit = fvd.compare(repo, _spec(body), f"{first}^", first)
    assert [e.path for e in per_commit.over_declared] == ["harness/check.py"], (
        "the split, not the spec, is what makes harness/check.py look over-declared"
    )


def test_touched_paths_fixes_the_order_and_deduplicates_at_the_seam(repo, monkeypatch):
    """The ONE test in this file that does not drive real git, and it has to be.

    Today `git diff --name-only` walks the tree in an order that coincides with a byte sort of
    the full paths, so a real-git fixture passes whether this function sorts or merely relays.
    Deleting the `sorted({...})` left the whole file green. The contract is that downstream
    order does not depend on git's, so it is asserted where git's answer is the input.
    """
    monkeypatch.setattr(fvd, "_git", lambda *a, **k: "z/b.py\na.py\nz/b.py\n\n  \n")
    assert fvd.touched_paths(repo, "BASE", "HEAD") == ("a.py", "z/b.py")


def test_touched_paths_agrees_with_real_git_on_the_real_range(repo):
    """The companion to the seam test: the fixed order is also the true set."""
    raw = {ln.strip() for ln in _git(repo, "diff", "--name-only", "HEAD^..HEAD").splitlines()
           if ln.strip()}
    got = fvd.touched_paths(repo, "HEAD^", "HEAD")
    assert set(got) == raw
    assert list(got) == sorted(raw)


# --------------------------------------------------------------------------- #
# gh#263: a base that MOVED under the branch
# --------------------------------------------------------------------------- #
@pytest.fixture
def diverged(tmp_path: Path) -> Path:
    """A checkout where `master` advanced after the branch left it.

    The `repo` fixture above is one line of history, and in a linear repo `A..B` and `A...B`
    name the same set - so a case built on it passes against the two-dot bug and against its
    fix alike, which is worse than no case. Real divergence is the only thing that separates
    them, and the absence of a fixture that had it is why a one-character bug reached master
    with 34 tests green.

    Two paths carry the two failure directions, in one history:

        master   base ──────────────── docs/REGISTRY.md, README.md
                   \\                   (master's own work, and its own route to the fix)
        feature     ──── skills/lead-dev/SKILL.md, README.md
                         (the branch's work, and the SAME BYTES for README.md)
    """
    r = tmp_path / "diverged"
    subprocess.run(["git", "init", "-b", "master", str(r)], check=True, capture_output=True)
    for k, v in (("user.email", "t@e.st"), ("user.name", "t")):
        _git(r, "config", k, v)

    for rel in ("docs/REGISTRY.md", "skills/lead-dev/SKILL.md", "README.md"):
        _write(r, rel)
    _commit(r, "base")

    _git(r, "checkout", "-b", "feature")
    _write(r, "skills/lead-dev/SKILL.md", "the branch's own work\n")
    _write(r, "README.md", "one fix, reached twice\n")
    _commit(r, "the branch's epic")

    _git(r, "checkout", "master")
    _write(r, "docs/REGISTRY.md", "a sibling epic's work, nothing to do with the branch\n")
    _write(r, "README.md", "one fix, reached twice\n")
    _commit(r, "master moves on")
    return r


def test_a_file_only_the_base_touched_is_not_attributed_to_the_branch(diverged):
    """gh#263 in its loud direction, and the one that cost a false scope alarm on four files.

    `master..feature` compares TIPS, so everything master gained after the branch left reads as
    the branch's work - manufacturing UNDECLARED findings for another epic's files. The range
    has to start at the merge base for the answer to be about this branch at all.
    """
    assert "docs/REGISTRY.md" not in fvd.touched_paths(diverged, "master", "feature")


def test_the_branchs_own_edit_is_still_reported(diverged):
    """The control the test above needs: returning NOTHING would satisfy it just as well."""
    assert "skills/lead-dev/SKILL.md" in fvd.touched_paths(diverged, "master", "feature")


def test_a_path_the_base_reached_identical_content_for_does_not_vanish(diverged):
    """gh#263 in its QUIET direction - rarer, and the dangerous one.

    A tip comparison drops a path the branch really did edit whenever the base independently
    arrived at the same bytes: an upstream merge of the same edit, a cherry-pick, a revert.
    Over-reporting raises an alarm somebody investigates; this direction silently omits an
    overlap, which is precisely what a disjointness check exists to catch. It is not a
    corollary of the loud case either - any fix that merely subtracted master's files from a
    tip comparison would still lose README.md here.
    """
    assert "README.md" in fvd.touched_paths(diverged, "master", "feature")


def test_the_report_names_the_range_that_was_actually_compared(diverged):
    """`render` printed `{base}..{head}` from its own f-string, so once the diff resolved from
    the merge base that line described a comparison that had not happened. The delta assertion
    pins both directions at the reported surface at once: the sibling epic's file is absent
    from UNDECLARED and the branch's own README.md is present."""
    body = f"- {BT}skills/lead-dev/SKILL.md{BT} - the branch's work"
    d = fvd.compare(diverged, _spec(body), "master", "feature")
    assert "range: master...feature" in fvd.render(d)[1]
    assert d.undeclared == ("README.md",)


def test_the_commit_range_is_unmoved_by_resolving_from_the_merge_base(repo):
    """An INVARIANCE pin, not a regression test: it passes on both trees, by construction.

    It exists because the obvious worry about this fix is that it also changes `--commit`, the
    mode the retroactive census was published from. It does not, and the reason is a property
    worth asserting rather than arguing: `commit_range` returns `(sha^, sha)`, sha descends
    from its first parent, so `merge-base(sha^, sha)` IS `sha^` and the two forms name one set.
    """
    base, head = fvd.commit_range("HEAD")
    assert _git(repo, "merge-base", base, head).strip() == _git(repo, "rev-parse", base).strip()
    two_dot = {ln.strip() for ln in _git(repo, "diff", "--name-only", "HEAD^..HEAD").splitlines()
               if ln.strip()}
    assert set(fvd.touched_paths(repo, base, head)) == two_dot


# --------------------------------------------------------------------------- #
# gh#316: a range that cannot answer, and the honest empty one it must not flag
# --------------------------------------------------------------------------- #
@pytest.fixture
def ancestor(tmp_path: Path) -> Path:
    """A checkout where `feature` is an ANCESTOR of `master`, plus an honest-empty control.

    Neither history already in this file can tell the bug from the fix. `repo` is linear with
    head after base, and `diverged` has commits on both sides - both give a non-empty range, so
    both pass whether or not the range is checked at all. The shape that breaks it is the third
    one: `master` fast-forwarded onto the branch and then moved on, so `merge-base(master,
    feature)` IS feature, the three-dot diff is empty, and git exits 0 the whole way.

        base ── the branch's epic (feature) ── master moves on (master)
                                            └─ an empty commit (emptyhead)

    `emptyhead` is the control that stops the fix becoming a false-alarm generator: it descends
    from `master`, it honestly touched no files, and its diff is empty for a reason a report is
    supposed to state rather than suppress. A check keyed on "the touched set came back empty"
    cannot tell it from `feature`.
    """
    r = tmp_path / "ancestor"
    subprocess.run(["git", "init", "-b", "master", str(r)], check=True, capture_output=True)
    for k, v in (("user.email", "t@e.st"), ("user.name", "t")):
        _git(r, "config", k, v)

    for rel in ("docs/REGISTRY.md", "skills/lead-dev/SKILL.md"):
        _write(r, rel)
    _commit(r, "base")

    _git(r, "checkout", "-b", "feature")
    _write(r, "skills/lead-dev/SKILL.md", "the branch's own work\n")
    _write(r, "docs/REGISTRY.md", "the branch's own work\n")
    _commit(r, "the branch's epic")

    _git(r, "checkout", "master")
    _git(r, "merge", "--ff-only", "feature")
    _write(r, "README.md", "master moves on after the fast-forward\n")
    _commit(r, "master moves on")

    _git(r, "checkout", "-b", "emptyhead")
    _git(r, "commit", "--allow-empty", "-m", "a head that honestly touched nothing")
    _git(r, "checkout", "master")
    return r


_ANCESTOR_BODY = "\n".join([
    f"- {BT}docs/REGISTRY.md{BT} - the contract",
    f"- {BT}skills/lead-dev/SKILL.md{BT} - the pump",
])


def test_a_head_that_is_an_ancestor_of_base_is_not_an_epic_that_over_declared_everything(ancestor):
    """gh#316, at the surface a caller actually reads.

    `master...feature` resolves, git exits 0, and the diff is empty because there is no range -
    so every declared entry came back OVER-DECLARED and nothing came back undeclared, which the
    module's own `_git` docstring names as the most confident wrong answer it can give. Both
    declared paths were touched by the work in question; the report said neither was.
    """
    d = fvd.compare(ancestor, _spec(_ANCESTOR_BODY), "master", "feature")
    assert d.range_usable is False
    assert d.over_declared == (), (
        "both declared paths were touched by feature's own commit; reporting them as declared-"
        "and-never-touched is the wrong answer this flag exists to withhold"
    )
    assert d.undeclared == () and d.touched == ()
    assert d.clean is False, "nothing was compared, so nothing can be certified clean"


def test_an_honest_empty_range_is_still_compared_and_still_reports_its_findings(ancestor):
    """The control that decides whether this fix is an improvement at all.

    `emptyhead` descends from `master` and honestly changed no files, so its touched set is
    empty for a reason worth reporting. A guard keyed on the empty touched set - or on the
    empty diff - fires here too, and trades a silent wrong answer for a noisy false alarm on
    legitimate input. The predicate has to be "the range holds no commits", which this range
    fails: it holds one.
    """
    d = fvd.compare(ancestor, _spec(_ANCESTOR_BODY), "master", "emptyhead")
    assert d.range_usable is True
    assert [e.path for e in d.over_declared] == ["docs/REGISTRY.md", "skills/lead-dev/SKILL.md"], (
        "an honest empty range is a comparison that ran; its findings must survive the guard"
    )
    assert "NO USABLE RANGE" not in "\n".join(fvd.render(d))


def test_range_commits_separates_the_two_empty_ranges_from_each_other(ancestor):
    """The predicate itself, against both shapes, so the choice cannot be quietly widened.

    Zero on the RIGHT is the whole test: it is what says the range holds no commits. The left
    count is not part of the verdict - it only names which repair the report should suggest.
    """
    assert fvd.range_commits(ancestor, "master", "feature") == (1, 0)
    assert fvd.range_commits(ancestor, "master", "emptyhead") == (0, 1)
    assert fvd.range_commits(ancestor, "master", "master") == (0, 0)


def test_base_and_head_naming_one_commit_is_a_different_repair_and_says_so(ancestor):
    """Both directions pinned, or the two messages stay swappable with the suite green.

    "The refs are the wrong way round" is true of the ancestor case and FALSE here - swapping
    one commit for itself changes nothing - so the two shapes get their own wording, the way
    `declaration_usable` branches on `section_present`.
    """
    same = fvd.compare(ancestor, _spec(_ANCESTOR_BODY), "master", "master")
    anc = fvd.compare(ancestor, _spec(_ANCESTOR_BODY), "master", "feature")
    assert (same.range_usable, same.same_commit) == (False, True)
    assert (anc.range_usable, anc.same_commit) == (False, False)

    same_report, anc_report = "\n".join(fvd.render(same)), "\n".join(fvd.render(anc))
    assert "the same commit" in same_report and "wrong way round" not in same_report
    assert "ancestor of" in anc_report and "the same commit" not in anc_report


def test_an_unusable_range_outranks_an_unusable_declaration_in_the_report(ancestor):
    """Both flags false at once: the RANGE is named, not the declaration. Kills mutant M5.

    `render` carries a comment saying the range guard sits first on purpose - with nothing
    compared, naming the declaration reports the weaker fact and leaves the reader believing a
    comparison happened. That decision had a comment defending it and no test defending it: an
    adversarial mutant that swapped the two guards left the suite green.

    Both messages are honest, so this pins an ORDERING rather than a correctness boundary -
    which is the kind of deliberate choice this module pins everywhere else.
    """
    no_section = (
        "---\n"
        "slug: a-real-shaped-epic\n"
        "current_state: done\n"
        "---\n\n"
        "This spec has no Files section at all.\n"
    )
    d = fvd.compare(ancestor, no_section, "master", "feature")

    assert d.range_usable is False
    assert d.declaration_usable is False
    body = "\n".join(fvd.render(d))
    assert "NO USABLE RANGE" in body
    assert "NO USABLE DECLARATION" not in body


def test_the_report_of_an_unusable_range_prints_no_delta_blocks_at_all(ancestor):
    """The delta blocks, not just the banner. A report that printed the warning and then
    OVER-DECLARED (2) underneath would teach a reader to believe the second half."""
    report = fvd.render(fvd.compare(ancestor, _spec(_ANCESTOR_BODY), "master", "feature"))
    assert report[1] == "range: master...feature   touched: not measured", (
        "the range is stated before the guard, but the touched count is NOT: `compare` "
        "returns early without running the diff, so a `0` here would be an unmeasured "
        "empty set printed as a count - the exact confusion this guard exists to end. The "
        "unusable-DECLARATION path keeps its number because there the diff really ran."
    )
    joined = "\n".join(report)
    for block in ("UNDECLARED", "OVER-DECLARED", "MIS-PATHED", "NOT DECIDABLE"):
        assert block not in joined


def test_two_histories_with_no_merge_base_still_raise_rather_than_reading_as_no_range(ancestor):
    """The neighbouring case the new probe must not absorb.

    An unrelated history has commits on BOTH sides, so it is not an empty range - it is a range
    with no merge base, which `touched_paths` deliberately fails loudly on. Answering it here
    would convert an exit-2 malformed invocation into a quiet report.
    """
    _git(ancestor, "checkout", "--orphan", "unrelated")
    _write(ancestor, "elsewhere.py")
    _commit(ancestor, "a history of its own")
    _git(ancestor, "checkout", "master")
    assert fvd.range_commits(ancestor, "master", "unrelated")[1] > 0
    with pytest.raises(fvd.GitError):
        fvd.compare(ancestor, _spec(_ANCESTOR_BODY), "master", "unrelated")


def test_the_cli_reports_an_unusable_range_at_exit_zero(ancestor, tmp_path, capsys):
    """It is a fact about the inputs, not a malformed argv: git resolved the range and exited
    0. Reddening it here would make a report-only instrument a gate, which is the recorded
    incident the module docstring cites."""
    spec = tmp_path / "epic.md"
    spec.write_text(_spec(_ANCESTOR_BODY), encoding="utf-8")
    argv = ["--repo", str(ancestor), "--spec", str(spec), "--base", "master", "--head", "feature"]
    assert fvd.main(argv) == 0
    assert "NO USABLE RANGE" in capsys.readouterr().out

    assert fvd.main([*argv, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["range_usable"] is False and payload["same_commit"] is False
    assert payload["over_declared"] == [] and payload["clean"] is False, (
        "a sweep that filters on the flag must not find range-derived findings underneath it"
    )


# One spec carrying every declaration-side finding class at once, beside two entries the
# range would have answered for and one that names nothing in the tree.
_SPEC_SIDE_BODY = "\n".join([
    f"- {BT}docs/REGISTRY.md{BT} - the contract",
    f"- {BT}docs/REGISTRY.md{BT} - listed twice",
    f"- {BT}skills/lead-dev/SKILL.md{BT} - the pump",
    f"- {BT}skills/lead-dev/{BT} - the pump's directory",
    f"- {BT}does/not/exist.py{BT} - names nothing in the tree",
    f"- {BT}/etc/passwd{BT} - absolute",
    f"- {BT}../outside/the/repo.py{BT} - parent-relative",
    f"- {BT}registry.py{BT} - a bare mention the section parser promoted",
    f"- {BT}<data_root>/projects/agentflow/status/registry.md{BT} - the board",
])


@pytest.mark.parametrize("head", ["feature", "master"], ids=["ancestor", "same-commit"])
def test_an_unusable_range_still_reports_the_findings_that_are_parses_of_the_spec(ancestor, head):
    """gh#347. The range check suppresses what the RANGE would have answered, and only that.

    A duplicate raw entry, an absolute path and a `<data_root>` placeholder are text parses of
    the spec; no range, usable or not, changes them. Before this they came back empty on an
    unusable range and `declared_count` read 0 for a spec declaring eight distinct entries -
    not an omitted number but a wrong one, since the count is knowable without any range.
    """
    d = fvd.compare(ancestor, _spec(_SPEC_SIDE_BODY), "master", head)
    assert d.range_usable is False
    assert d.declaration_usable is True
    assert d.declared_count == 8
    assert d.duplicate_entries == ("docs/REGISTRY.md",)
    assert [e.raw for e in d.spec_defects] == ["/etc/passwd", "../outside/the/repo.py"]
    assert [e.raw for e in d.noise] == ["registry.py"]
    assert [e.raw for e in d.out_of_repo] == ["<data_root>/projects/agentflow/status/registry.md"]
    # The range-derived side stays exactly as gh#316 left it.
    assert (d.touched, d.undeclared, d.over_declared, d.mis_pathed, d.patterns_used) == (
        (), (), (), (), ())
    assert d.clean is False


def test_a_misplaced_entry_stays_suppressed_on_an_unusable_range(ancestor):
    """The one declaration-side class that is NOT a pure parse: its reason says "and the range
    never touched it", so emitting it on a range that touched nothing would restate the same
    false confidence gh#316 removed, in a different field. It is still COUNTED - it is a
    distinct entry the spec wrote - but it is classified nowhere."""
    d = fvd.compare(ancestor, _spec(_SPEC_SIDE_BODY), "master", "feature")
    assert "does/not/exist.py" in [e.raw for e in d.entries]
    assert all(e.code != fvd.R_MISPLACED for e in d.unresolvable)
    payload = json.dumps(d.as_dict())
    assert "does/not/exist.py" not in payload
    assert "never touched" not in payload
    assert "does/not/exist.py" not in "\n".join(fvd.render(d))


def test_the_json_of_an_unusable_range_carries_the_spec_side_findings(ancestor, tmp_path, capsys):
    """The sweep surface, end to end: a reader filtering on `range_usable` loses the deltas it
    must lose and keeps the count and the defect lists it was always owed."""
    spec = tmp_path / "epic.md"
    spec.write_text(_spec(_SPEC_SIDE_BODY), encoding="utf-8")
    argv = ["--repo", str(ancestor), "--spec", str(spec), "--base", "master", "--head", "feature",
            "--json"]
    assert fvd.main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["range_usable"] is False
    assert payload["declared_count"] == 8
    assert payload["duplicate_entries"] == ["docs/REGISTRY.md"]
    assert payload["spec_defects"] == ["/etc/passwd", "../outside/the/repo.py"]
    assert payload["noise"] == ["registry.py"]
    assert payload["out_of_repo"] == ["<data_root>/projects/agentflow/status/registry.md"]
    assert payload["undeclared"] == [] and payload["over_declared"] == []
    assert payload["mis_pathed"] == [] and payload["patterns_used"] == []
    assert payload["clean"] is False


def test_the_report_of_an_unusable_range_says_the_spec_side_findings_still_hold(ancestor):
    """The banner comes first and the delta blocks stay absent; underneath it, the declaration
    count and the three spec-side classes are printed, with a line saying they are parses of
    the spec and so hold whatever the range."""
    report = fvd.render(fvd.compare(ancestor, _spec(_SPEC_SIDE_BODY), "master", "feature"))
    joined = "\n".join(report)
    assert report[2].startswith("NO USABLE RANGE")
    assert "declared: 8 distinct entries (1 duplicate raw entry deduped: docs/REGISTRY.md)" in (
        report)
    assert any("still hold" in line for line in report)
    assert "UNRESOLVABLE / SPEC DEFECT (2)" in joined and "/etc/passwd" in joined
    assert "UNRESOLVABLE / PARSER NOISE (1)" in joined
    assert "UNRESOLVABLE / OUT OF REPO BY DESIGN (1)" in joined
    for block in ("UNDECLARED", "OVER-DECLARED", "MIS-PATHED", "NOT DECIDABLE"):
        assert block not in joined
    assert joined.index("NO USABLE RANGE") < joined.index("declared:")


def test_the_report_of_an_unusable_range_counts_the_withheld_misplaced_entries(ancestor):
    """`declared: 8` over blocks listing four entries leaves the reader to guess where the rest
    went. Most are plain files a range would have answered for; the misplaced entry is the one
    that is a finding, so its count is printed while its range-shaped reason is not. With none
    withheld the parenthetical is left off entirely rather than printed as `(0 withheld ...)`."""
    report = fvd.render(fvd.compare(ancestor, _spec(_SPEC_SIDE_BODY), "master", "feature"))
    [hold] = [line for line in report if "still hold" in line]
    assert hold.endswith("(1 withheld: a path with a separator that is not in the tree)")
    assert "does/not/exist.py" not in "\n".join(report)

    clean = fvd.render(fvd.compare(ancestor, _spec(_ANCESTOR_BODY), "master", "feature"))
    [hold] = [line for line in clean if "still hold" in line]
    assert "withheld" not in hold


# --------------------------------------------------------------------------- #
# The machine surface the retroactive numbers were quoted from
# --------------------------------------------------------------------------- #
def _busy(repo: Path) -> fvd.FilesDelta:
    """One run that lands something in every bucket the report has."""
    return _run(repo, "\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - touched, exact",
        f"- {BT}skills/_shared/agentflow/preflight.py{BT} - never touched",
        f"- {BT}skills/_shared/agentflow/tests/test_registry.py{BT} - one segment off",
        f"- {BT}skills/_shared/agentflow/nonesuch.py{BT} - names nothing here",
        f"- {BT}registry.py{BT} - a bare mention the section parser promoted",
        f"- {BT}<data_root>/projects/agentflow/status/registry.md{BT} - the board",
        f"- {BT}skills/steward/{BT} - the pump directory",
    ]))


def test_as_dict_carries_the_whole_schema(repo):
    """`as_dict` and `--json` are how the retroactive numbers were produced and neither had a
    test. A measurement quoted in a PR should not come out of an unobserved surface."""
    d = _busy(repo)
    payload = d.as_dict()
    assert set(payload) == {
        "spec", "base", "head", "range", "section_present", "declaration_usable", "range_usable",
        "same_commit", "declared_count", "duplicate_entries", "touched", "undeclared",
        "over_declared", "mis_pathed", "unresolvable", "spec_defects", "noise", "out_of_repo",
        "patterns_used", "clean",
    }
    assert payload["range_usable"] is True and payload["same_commit"] is False
    assert payload["declared_count"] == 7
    assert payload["undeclared"] == ["skills/_shared/agentflow/registry.py",
                                     "skills/_shared/tests/test_missed_wake_alarm.py"]
    assert payload["over_declared"] == ["skills/_shared/agentflow/preflight.py"]
    assert payload["mis_pathed"] == [{
        "declared": "skills/_shared/agentflow/tests/test_registry.py",
        "actual": "skills/_shared/tests/test_registry.py"}]
    assert payload["spec_defects"] == ["skills/_shared/agentflow/nonesuch.py"]
    assert payload["noise"] == ["registry.py"]
    assert payload["out_of_repo"] == ["<data_root>/projects/agentflow/status/registry.md"]
    assert payload["patterns_used"] == [
        {"path": "skills/steward/", "covered": ["skills/steward/SKILL.md"]}]
    assert payload["clean"] is False
    assert all(set(u) == {"raw", "reason", "code"} for u in payload["unresolvable"])
    assert json.loads(json.dumps(payload)) == payload, "the --json path must round-trip"


def test_the_json_and_the_human_report_of_one_run_state_the_same_numbers(repo, tmp_path, capsys):
    """Two surfaces, one run: every count a reader takes from the report is the length of a
    list in the payload. They were quoted from the second and read from the first."""
    spec = tmp_path / "epic.md"
    spec.write_text(_spec("\n".join([
        f"- {BT}docs/REGISTRY.md{BT} - touched, exact",
        f"- {BT}skills/_shared/agentflow/preflight.py{BT} - never touched",
        f"- {BT}skills/_shared/agentflow/tests/test_registry.py{BT} - one segment off",
        f"- {BT}skills/_shared/agentflow/nonesuch.py{BT} - names nothing here",
        f"- {BT}registry.py{BT} - a bare mention",
        f"- {BT}<data_root>/projects/agentflow/status/registry.md{BT} - the board",
        f"- {BT}skills/steward/{BT} - the pump directory",
    ])), encoding="utf-8")
    argv = ["--repo", str(repo), "--spec", str(spec), "--commit", "HEAD"]
    assert fvd.main(argv) == 0
    human = capsys.readouterr().out
    assert fvd.main([*argv, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    def _count(label: str) -> int:
        m = re.search(re.escape(label) + r" \((\d+)\)", human)
        assert m is not None, f"{label} missing from the human report"
        return int(m.group(1))

    assert _count("UNDECLARED") == len(payload["undeclared"])
    assert _count("OVER-DECLARED") == len(payload["over_declared"])
    assert _count("MIS-PATHED") == len(payload["mis_pathed"])
    assert _count("UNRESOLVABLE / SPEC DEFECT") == len(payload["spec_defects"])
    assert _count("UNRESOLVABLE / PARSER NOISE") == len(payload["noise"])
    assert _count("UNRESOLVABLE / OUT OF REPO BY DESIGN") == len(payload["out_of_repo"])
    assert _count("NOT DECIDABLE") == len(payload["patterns_used"])
    assert f"declared: {payload['declared_count']} distinct entries" in human
    assert len(payload["spec_defects"]) + len(payload["noise"]) + len(payload["out_of_repo"]) \
        == len(payload["unresolvable"])


# --------------------------------------------------------------------------- #
# It reports; it does not judge
# --------------------------------------------------------------------------- #
def test_the_cli_exits_zero_on_a_run_that_found_something(repo, tmp_path, capsys):
    spec = tmp_path / "epic.md"
    spec.write_text(_spec(f"- {BT}docs/REGISTRY.md{BT} - only this one"), encoding="utf-8")
    rc = fvd.main(["--repo", str(repo), "--spec", str(spec), "--commit", "HEAD"])
    out = capsys.readouterr().out
    assert rc == 0, "findings must never redden the exit; this is a measurement, not a gate"
    assert "UNDECLARED (4)" in out


def test_the_cli_exits_two_on_a_malformed_invocation(repo, tmp_path, capsys):
    spec = tmp_path / "epic.md"
    spec.write_text(_spec(f"- {BT}docs/REGISTRY.md{BT} - only this one"), encoding="utf-8")
    both = fvd.main(["--repo", str(repo), "--spec", str(spec),
                     "--commit", "HEAD", "--base", "A", "--head", "B"])
    unreadable = fvd.main(["--repo", str(repo), "--spec", str(tmp_path / "gone.md"),
                           "--commit", "HEAD"])
    bad_rev = fvd.main(["--repo", str(repo), "--spec", str(spec), "--commit", "notarev"])
    assert [both, unreadable, bad_rev] == [2, 2, 2]


def test_a_subdirectory_of_a_real_checkout_is_not_a_repo(repo, tmp_path, capsys):
    """What the `.git` guard is actually FOR, and the only arm that needs it.

    A `--repo` that does not exist exits 2 with or without the guard, so asserting that arm
    distinguishes nothing. A subdirectory of a valid checkout is the near miss: `git -C` would
    answer happily from there, about the wrong root and with paths relative to the wrong root.
    """
    spec = tmp_path / "epic.md"
    spec.write_text(_spec(f"- {BT}docs/REGISTRY.md{BT} - only this one"), encoding="utf-8")
    sub = repo / "skills"
    assert sub.is_dir() and not (sub / ".git").exists()
    rc = fvd.main(["--repo", str(sub), "--spec", str(spec), "--commit", "HEAD"])
    assert rc == 2
    assert "not a git checkout" in capsys.readouterr().out


def test_a_git_failure_raises_rather_than_reporting_an_empty_diff(repo):
    """An empty stdout from a failed `git diff` is indistinguishable from a commit that touched
    nothing - which would report every declared entry over-declared and nothing undeclared."""
    with pytest.raises(fvd.GitError):
        fvd.touched_paths(repo, "no-such-rev", "HEAD")


# --- gh#318: the DOCUMENTED invocation, the one shape no case here exercised -------------


def test_the_documented_invocation_resolves_a_project_instead_of_seeking_None_md(
    repo, monkeypatch, tmp_path, capsys
):
    """No --repo: the exact command docs/REGISTRY.md publishes must resolve a project.

    WHY 999 LINES OF TESTS MISSED IT. Every other case in this file supplies a --repo of its
    own, so the branch a human actually reaches was never entered. It called load_project(None),
    which interpolates its argument into projects/<name>.md and therefore hunted for
    projects/None.md while $AGENTFLOW_PROJECT sat set and unread beside it. The tool has no
    programmatic caller anywhere - its whole reachable surface is someone typing that line - so
    the defect closed its only door, loudly, on every machine.

    This asserts the RESOLUTION rather than a non-2 exit: load_project must RECEIVE the resolved
    name. An exit code alone would pass again the day someone reverts the call site, because
    plenty of other things also return 0.
    """
    from agentflow import config as config_mod

    spec = tmp_path / 'spec.md'
    spec.write_text("""
## Files

- `a.py`
""", encoding='utf-8')

    seen = []

    class _Repo:
        local_checkout = str(repo)

    class _Proj:
        canonical_repo = _Repo()

    def _fake_load_project(name):
        seen.append(name)
        return _Proj()

    monkeypatch.setenv('AGENTFLOW_PROJECT', 'someproject')
    monkeypatch.setattr(config_mod, 'load_project', _fake_load_project)

    rc = fvd.main(['--spec', str(spec), '--commit', 'HEAD'])

    assert seen == ['someproject'], (
        f'load_project got {seen!r}; the documented invocation must route through '
        + 'resolve_project_name(), not hand None to a builder of projects/<name>.md'
    )
    assert rc == 0, capsys.readouterr().out


def test_no_resolvable_project_names_the_ways_out_and_never_a_None_file(
    monkeypatch, tmp_path, capsys
):
    """With nothing to resolve, the message names the three exits - and never says None.

    Environment-only config is legitimate elsewhere, so this path is reachable rather than
    theoretical. What it must not do is fail later against a path built from the word None,
    which is what sent the reporter hunting for a config file nobody ever creates.
    """
    from agentflow import config as config_mod

    spec = tmp_path / 'spec.md'
    spec.write_text("""
## Files

- `a.py`
""", encoding='utf-8')

    monkeypatch.setattr(config_mod, 'resolve_project_name', lambda *a, **k: None)

    rc = fvd.main(['--spec', str(spec), '--commit', 'HEAD'])
    out = capsys.readouterr().out

    assert rc == 2
    assert 'None.md' not in out, f'still names a None file: {out!r}'
    assert '--repo' in out and 'AGENTFLOW_PROJECT' in out, (
        f'the message must name the ways out, got: {out!r}'
    )


# --- gh#317: the machine surface must NAME the comparison, not invite its reconstruction ---


def test_as_dict_names_the_three_dot_range_on_a_DIVERGED_repo(diverged):
    """The JSON surface carries the range, and on a diverged repo it is the three-dot form.

    WHY THIS FIXTURE AND NOT THE LINEAR ONE. On the linear repo the two dot-forms agree, so a
    payload rebuilt as base..head looks correct there and the defect hides. gh#317 records that
    the JSON path never ran against `diverged` at all, which is exactly why nothing caught the
    two surfaces disagreeing about what was compared.

    THE DEFECT THIS CLOSES is one level out, in a caller: with no range on this payload the
    natural reconstruction is f'{base}..{head}', the two-dot form gh#263 removed, rebuilt where
    nothing in this module can observe it. A fix single-sourced into one surface leaves the
    other free to rebuild the bug.
    """
    d = fvd.compare(diverged, _spec(f'- {BT}README.md{BT} - the branch edit'), 'master', 'feature')
    payload = d.as_dict()

    assert payload['range'] == 'master...feature', (
        f"as_dict must name the comparison; got {payload.get('range')!r}"
    )
    assert payload['range'] != 'master..feature', (
        'two-dot form on the machine surface - gh#263 removed it from render and gh#317 '
        + 'exists because a JSON consumer could rebuild it here'
    )


def test_as_dict_range_FOLLOWS_range_expr_when_range_expr_MOVES(diverged, monkeypatch):
    """Move range_expr; the payload's range must move with it.

    THIS IS THE PIN THAT FORBIDS A RETYPED LITERAL, and it replaces one that did not. The
    obvious assertion - payload['range'] == range_expr(base, head) - compares two VALUES, so a
    hand-typed f'{base}...{head}' passes it unchanged while being derived from nothing. Measured
    rather than assumed: with the call replaced by exactly that literal, the whole file stayed
    green at 51 passed.

    That is the same defect one level up that gh#317 is about at the surface level - a value
    that looks right today and drifts the day the semantics change again, which is precisely
    what gh#263 changed once already.
    """
    # Build the delta FIRST, unpatched: `compare` uses `range_expr` for the git invocation
    # itself, so patching before it runs breaks the diff instead of exercising the surface.
    d = fvd.compare(diverged, _spec(f'- {BT}README.md{BT} - the branch edit'), 'master', 'feature')

    sentinel = 'RANGE-MOVED-BY-THE-TEST'
    monkeypatch.setattr(fvd, 'range_expr', lambda *a, **k: sentinel)
    assert d.as_dict()['range'] == sentinel, (
        'as_dict did not call range_expr - the range is a retyped copy, so it will drift '
        + 'from the surface render names the moment the range semantics change'
    )


# --- gh#293: a CLAIMED row's seam is its BRANCH, and the branch has to be findable ---------


@pytest.fixture
def epic_repo(tmp_path: Path) -> Path:
    """A checkout shaped like the fleet's: master, plus one epic branch named `e<NNNN>-<short>`.

    Real git and real refs throughout, for the same reason the rest of this file uses them: the
    thing under test is whether a NAME in the registry can be turned into the WORK on disk, and
    a mock ref store would only prove the mock agrees with itself.
    """
    r = tmp_path / "fleet"
    subprocess.run(["git", "init", "-b", "master", str(r)], check=True, capture_output=True)
    for k, v in (("user.email", "t@e.st"), ("user.name", "t")):
        _git(r, "config", k, v)

    for rel in ("docs/REGISTRY.md", "skills/lead-dev/SKILL.md",
                "skills/_shared/agentflow/files_vs_diff.py", "README.md"):
        _write(r, rel)
    _commit(r, "base")

    _git(r, "checkout", "-b", "e0734-gh293-live-seam")
    _write(r, "skills/_shared/agentflow/files_vs_diff.py", "the resolver\n")
    _write(r, "skills/_shared/tests/test_files_vs_diff.py", "the tests\n")
    _commit(r, "the live seam")
    _git(r, "checkout", "master")
    return r


def test_a_claimed_epics_seam_is_read_from_its_branch_not_from_its_declaration(epic_repo):
    """The whole of gh#293 in one assertion: id in, actually-touched paths out.

    The declaration is a prediction written before the work and the branch IS the work. One
    measured epic declared seven paths and touched twelve, and two of the undeclared five had
    been graded FREE in a scheduling decision twenty minutes earlier - so the seam a claimed row
    was published against had never seen the paths it was asked about.
    """
    assert fvd.epic_touched_paths(epic_repo, "master", "E-0734-gh293-live-seam") == (
        "skills/_shared/agentflow/files_vs_diff.py",
        "skills/_shared/tests/test_files_vs_diff.py",
    )


def test_an_epic_with_no_branch_answers_None(epic_repo):
    """An unclaimed row has no branch, and the answer must say ABSENT rather than EMPTY."""
    assert fvd.epic_touched_paths(epic_repo, "master", "E-0999-never-started") is None


def test_None_for_no_branch_is_DISTINGUISHABLE_from_the_empty_tuple_for_no_edits(epic_repo):
    """Both states are reachable here, and they mean opposite things about a seam.

    NO BRANCH says the work has not started, so the declaration is the only thing there is to
    read and a caller must fall back to it. AN EMPTY DIFF says the work started and moved
    nothing, so the declaration is over-declared and the seam is genuinely free. A single empty
    tuple for both would report an unstarted epic as one that occupies nothing and publish
    straight through its seam - which is the failure this module exists to prevent, re-created
    one level up.

    Asserted as a PAIR on one repository on purpose: `is None` alone passes on an
    implementation that has no empty case at all, and `== ()` alone passes on one that returns
    `()` for everything.
    """
    _git(epic_repo, "checkout", "-b", "e0800-touched-nothing")
    _git(epic_repo, "commit", "--allow-empty", "-m", "a branch that moved no file")
    _git(epic_repo, "checkout", "master")

    absent = fvd.epic_touched_paths(epic_repo, "master", "E-0999-never-started")
    empty = fvd.epic_touched_paths(epic_repo, "master", "E-0800-touched-nothing")

    assert absent is None
    assert empty == ()
    assert absent is not empty
    assert (absent is None) != (empty is None), (
        "no-branch and touched-nothing collapsed onto one answer; a caller can no longer tell "
        + "an unstarted epic from a free seam"
    )


def test_two_branches_on_one_prefix_REFUSE_rather_than_pick_one(epic_repo):
    """Ambiguity is not `None`, because `None` sends the caller quietly to the declared list.

    A guess here produces a seam that LOOKS measured while being read off the wrong branch,
    which is strictly worse than the declared-list fallback it would be mistaken for. The
    message names every candidate so the reader can fix the naming rather than the tooling.
    """
    _git(epic_repo, "branch", "e0734-gh293-live-seam-take-two", "e0734-gh293-live-seam")

    with pytest.raises(fvd.AmbiguousEpicBranch) as exc:
        fvd.epic_touched_paths(epic_repo, "master", "E-0734-gh293-live-seam")

    msg = str(exc.value)
    assert "e0734-gh293-live-seam" in msg and "e0734-gh293-live-seam-take-two" in msg, msg


# --- the derivation itself, since everything above rests on it ---------------------------


def test_the_branch_prefix_is_the_ids_own_digits_lowercased_with_the_hyphen_dropped():
    """`E-0734-gh293-live-seam` -> `e0734`. Pinned because the convention is now published."""
    assert fvd.epic_branch_prefix("E-0734-gh293-live-seam") == "e0734"
    assert fvd.epic_branch_prefix("E-0220") == "e0220", "a legacy bare id still derives"
    assert fvd.epic_branch_prefix("  E-0042-staging-deploy-gate  ") == "e0042"
    assert fvd.epic_branch_prefix("e-0042-staging-deploy-gate") == "e0042", "case-tolerant"


def test_a_string_that_is_not_an_epic_id_RAISES_rather_than_matching_the_board():
    """`e` as a prefix would match most branches on this repo. There is no question to ask."""
    for junk in ("0734-gh293", "E0734-gh293", "", "E-", "not-an-id"):
        with pytest.raises(ValueError):
            fvd.epic_branch_prefix(junk)


def test_the_digits_are_VERBATIM_so_a_padded_id_never_reaches_an_unpadded_branch(epic_repo):
    """`E-0220` does not resolve `e220-...`, and that narrowing is deliberate.

    This repository carries `e0263-two-dot-range` beside `e263-land`, and `e228-land` beside
    `e228-ground-an-escalation`. Normalising the padding away would make questions ambiguous
    that are answerable today, and would do it in the raising direction. The unmatched case
    costs nothing: it returns `None`, the caller reads the declaration, and that is precisely
    the behaviour that already ships.
    """
    _git(epic_repo, "branch", "e220-harness-meta-check-hermetic", "e0734-gh293-live-seam")

    assert fvd.resolve_epic_branch(epic_repo, "E-0220-harness-meta-check") is None
    assert fvd.resolve_epic_branch(epic_repo, "E-220-harness-meta-check") == (
        "refs/heads/e220-harness-meta-check-hermetic"
    )


def test_a_longer_number_sharing_the_leading_digits_is_not_a_match(epic_repo):
    """`e07341-...` is a different epic. The match is bounded by the convention's hyphen."""
    _git(epic_repo, "branch", "e07341-a-different-epic", "e0734-gh293-live-seam")
    _git(epic_repo, "branch", "-D", "e0734-gh293-live-seam")

    assert fvd.resolve_epic_branch(epic_repo, "E-0734-gh293-live-seam") is None


def test_resolve_FOLLOWS_epic_branch_prefix_when_the_derivation_MOVES(epic_repo, monkeypatch):
    """Move the derivation; resolution must move with it.

    The obvious assertion - resolve finds the branch whose name starts with `e0734` - compares
    two values and passes just as well against a prefix retyped inside the resolver, which is a
    second copy of a convention that is about to be published in one place. Same defect class
    the range pin above forbids.
    """
    monkeypatch.setattr(fvd, "epic_branch_prefix", lambda _id: "no-branch-is-named-this")
    assert fvd.resolve_epic_branch(epic_repo, "E-0734-gh293-live-seam") is None


# --- both ref stores, because half the fleet lives in each --------------------------------


def test_a_branch_that_exists_only_as_a_REMOTE_ref_still_resolves(epic_repo):
    """A lane on another machine reaches this repository only as refs/remotes/<remote>/...

    Reading `refs/heads` alone would report every such epic as unstarted, which is the `None`
    answer, which sends the seam check back to the declaration - the exact gap gh#293 closes.
    """
    sha = _git(epic_repo, "rev-parse", "e0734-gh293-live-seam").strip()
    _git(epic_repo, "update-ref", "refs/remotes/origin/e0801-somewhere-else", sha)

    assert fvd.resolve_epic_branch(epic_repo, "E-0801-somewhere-else") == (
        "refs/remotes/origin/e0801-somewhere-else"
    )
    assert fvd.epic_touched_paths(epic_repo, "master", "E-0801-somewhere-else") == (
        "skills/_shared/agentflow/files_vs_diff.py",
        "skills/_shared/tests/test_files_vs_diff.py",
    )


def test_one_name_on_both_sides_is_ONE_branch_and_the_local_ref_is_the_one_read(epic_repo):
    """`e0734-x` and `origin/e0734-x` are the same branch, not two candidates.

    Counting them as two would raise `AmbiguousEpicBranch` on the single most ordinary state a
    pushed branch is ever in, so the deduplication is what makes the ambiguity check usable at
    all. Local wins because these builders are worktrees off one checkout: their newest commits
    are on the local ref and the remote lags it by a push. Pinned against a DIVERGED pair, since
    on an identical pair either choice looks correct.
    """
    _git(epic_repo, "checkout", "e0734-gh293-live-seam")
    _write(epic_repo, "docs/REGISTRY.md", "the claim contract\n")
    _commit(epic_repo, "local-only commit, not yet pushed")
    local_head = _git(epic_repo, "rev-parse", "HEAD").strip()
    _git(epic_repo, "checkout", "master")

    _git(epic_repo, "update-ref", "refs/remotes/origin/e0734-gh293-live-seam",
         local_head + "^")

    assert fvd.resolve_epic_branch(epic_repo, "E-0734-gh293-live-seam") == (
        "refs/heads/e0734-gh293-live-seam"
    )
    assert "docs/REGISTRY.md" in fvd.epic_touched_paths(
        epic_repo, "master", "E-0734-gh293-live-seam"
    ), "read the lagging remote ref; an unpushed commit's paths vanished from the seam"
