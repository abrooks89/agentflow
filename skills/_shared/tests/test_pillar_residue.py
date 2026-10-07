"""The pillar residue ratchet: direction, two classes kept apart, and a derived pattern.

EVERY TEST HERE IS MEANT TO FAIL WHEN ITS GUARDED LINE IS REVERTED, and the ones that are easy
to write green-either-way are called out where they sit.

NO LITERAL FROM EITHER CLASS APPEARS IN THIS FILE. The install-specific half of the sanitize
class lives in a local-only extra pattern file this tree never carries (gh#821), so the probes
for it are neutral placeholder words supplied through a TEST-LOCAL extra pattern file - the same
mechanism and reader, different content. The one generic probe, a home path, is assembled from
adjacent Python string literals, which the interpreter concatenates and a line-oriented grep
does not; spelled whole, this file would be a finding in the gate it exists to check.

NO WALK OVER THE WORKING TREE EITHER. Every corpus here is a ``tmp_path`` fixture, so
``test_corpus_from_index`` has nothing to object to and needs no exemption for this module. The
one place a real repo-rooted path is touched is ``DEFAULT_CI_YML``, and that is a single
``read_text`` of one named file, not an enumeration.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

import pytest

from agentflow import pillar_residue as pr
from agentflow import sanitize_extra
from agentflow.wiki import refresh_projection

# --------------------------------------------------------------------------------------------
# Probes. The extra-pattern ones are placeholders that name nothing; the home path is assembled
# so this file is not itself a finding.
# --------------------------------------------------------------------------------------------
#: The test-local extra pattern file, in the real file's format: a comment, a blank line and two
#: alternatives, each carrying a lookaround exemption the way an install-specific term can.
EXTRA_FILE_TEXT = (
    "# test-local extra pattern: placeholders only, never a real term\n"
    "\n"
    "(?<!super)acmeproduct\n"
    "zephyrhost(?!ed)\n"
)
PRODUCT = "acmeproduct"
EXEMPT_PRODUCT = "superacmeproduct"
HOST = "zephyrhost"
EXEMPT_HOST = "zephyrhosted"
HOME_PATH = "/home/" "someacct"
PLACEHOLDER_HOME = "/home/you"
FAKE_PERSON = "Tester"

#: Filled by the autouse fixture below: where this module's test-local extra file was written.
_EXTRA: dict[str, Path] = {}


@pytest.fixture(autouse=True, scope="module")
def _extra_pattern_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("extra") / "sanitize-extra-pattern.txt"
    path.write_text(EXTRA_FILE_TEXT, encoding="utf-8")
    _EXTRA["path"] = path
    return path


def _extra() -> sanitize_extra.ExtraPattern:
    return sanitize_extra.read_extra_pattern(_EXTRA["path"])


def _ns(**kw) -> argparse.Namespace:
    base = dict(project=None, wiki_root=None, person_name=FAKE_PERSON,
                ci_yml=None, update_baseline=None, accept_worsening=False,
                sanitize_extra=str(_EXTRA["path"]))
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def pillar(tmp_path: Path) -> Path:
    """An empty wiki root with the partitions the real one has."""
    root = tmp_path / "wiki"
    (root / "pages" / "done").mkdir(parents=True)
    (root / "pages" / "archived").mkdir(parents=True)
    return root


def _page(pillar: Path, slug: str, body: str) -> Path:
    path = pillar / "pages" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _matchers() -> dict:
    return pr.compile_classes(person_name=FAKE_PERSON, extra=_extra())


def _scan(pillar: Path) -> pr.Report:
    return pr.scan_pillar(pillar, _matchers())


def _record(pillar: Path, date: str = "2026-01-01") -> pr.Baseline:
    path = pr.baseline_path(pillar)
    history = pr.load_baseline(path).history if path.exists() else ()
    pr.write_baseline(path, _scan(pillar), recorded=date, history=history)
    return pr.load_baseline(path)


# --------------------------------------------------------------------------------------------
# The ratchet: direction
# --------------------------------------------------------------------------------------------
def test_a_new_page_with_residue_is_a_violation(pillar: Path) -> None:
    """The half that makes this useful on a corpus that grows every time a lane saves a page."""
    _page(pillar, "old", f"carries {PRODUCT} already")
    _record(pillar)

    _page(pillar, "brand-new", f"mentions {FAKE_PERSON} in passing")
    found = pr.violations(_scan(pillar), pr.load_baseline(pr.baseline_path(pillar)))
    assert len(found) == 1 and "brand-new" in found[0] and "NEW PAGE" in found[0]
    assert pr.run(_ns(), pillar) == 1


def test_a_new_page_without_residue_is_not_a_violation(pillar: Path) -> None:
    """The control beside the test above: `new` alone is not the finding, `new and dirty` is."""
    _page(pillar, "old", f"carries {PRODUCT} already")
    _record(pillar)

    _page(pillar, "brand-new", "says nothing anybody has to decide about")
    assert pr.violations(_scan(pillar), pr.load_baseline(pr.baseline_path(pillar))) == []
    assert pr.run(_ns(), pillar) == 0


def test_removing_residue_passes_and_lowers_the_baseline(pillar: Path) -> None:
    """Down is always allowed, and `--update-baseline` is what makes the lower number stick."""
    page = _page(pillar, "dirty", f"{PRODUCT} and {PRODUCT} again")
    before = _record(pillar)
    assert before.count("dirty", pr.CLASS_SANITIZE) == 2

    page.write_text("cleaned up entirely", encoding="utf-8")
    assert pr.violations(_scan(pillar), before) == []
    assert pr.run(_ns(), pillar) == 0

    assert pr.run(_ns(update_baseline="2026-02-02"), pillar) == 0
    after = pr.load_baseline(pr.baseline_path(pillar))
    assert "dirty" not in after.pages
    assert after.pages == {}


def test_a_baselined_page_getting_worse_is_a_violation(pillar: Path) -> None:
    """The baseline is a CEILING per page, not a licence for the page to keep collecting."""
    page = _page(pillar, "dirty", f"{PRODUCT}")
    _record(pillar)

    page.write_text(f"{PRODUCT} {PRODUCT} {PRODUCT}", encoding="utf-8")
    found = pr.violations(_scan(pillar), pr.load_baseline(pr.baseline_path(pillar)))
    assert len(found) == 1 and "1 -> 3" in found[0]
    assert pr.run(_ns(), pillar) == 1


# --------------------------------------------------------------------------------------------
# The two classes stay apart
# --------------------------------------------------------------------------------------------
def test_the_two_classes_are_counted_separately(pillar: Path) -> None:
    _page(pillar, "both", f"{PRODUCT} and {FAKE_PERSON} and {FAKE_PERSON}")
    _page(pillar, "only-sanitize", f"{HOST}")
    _page(pillar, "only-person", f"{FAKE_PERSON}")
    report = _scan(pillar)

    assert report.class_pages(pr.CLASS_SANITIZE) == ["both", "only-sanitize"]
    assert report.class_pages(pr.CLASS_PERSON) == ["both", "only-person"]
    assert report.class_total(pr.CLASS_SANITIZE) == 2
    assert report.class_total(pr.CLASS_PERSON) == 3
    assert len(report.pages) == 3, "three pages, not five: the classes share a page index"


def test_a_page_that_trades_one_class_for_the_other_is_a_violation(pillar: Path) -> None:
    """THE discriminating test for per-class comparison.

    Totals are unchanged -- one hit before, one hit after. If `violations` compared page TOTALS
    instead of per-class counts this reads as a wash and returns green, which is exactly how a
    page quietly acquires the class that is still an open human question.
    """
    page = _page(pillar, "traded", f"{PRODUCT}")
    _record(pillar)

    page.write_text(f"{FAKE_PERSON}", encoding="utf-8")
    report = _scan(pillar)
    assert report.pages["traded"].total() == 1, "the totals really are equal"
    found = pr.violations(report, pr.load_baseline(pr.baseline_path(pillar)))
    assert len(found) == 1 and pr.CLASS_PERSON in found[0] and "0 -> 1" in found[0]


def test_the_sanitize_pattern_does_not_already_cover_the_person_class(pillar: Path) -> None:
    """Why the person class exists at all. Against the REAL shipped ci.yml pattern, unioned with
    the test-local extra pattern the way a run unions the real one.

    If the two were merged -- or if the personal name were added to `ci.yml` against the
    recorded decision without collapsing the classes here -- this goes red and the next reader is
    told why rather than finding two numbers that silently became one.
    """
    sanitize = _matchers()[pr.CLASS_SANITIZE]
    assert not sanitize.search(FAKE_PERSON)
    assert not sanitize.search("a sentence about " + FAKE_PERSON + " and nothing else")


def test_the_person_pattern_is_word_bounded_and_escaped() -> None:
    person = _matchers()[pr.CLASS_PERSON]
    assert person.search(f"asked {FAKE_PERSON} directly")
    assert person.search(FAKE_PERSON.lower()), "the gate greps -i; so does this"
    assert not person.search(FAKE_PERSON + "ation"), "unbounded would inflate an open question"

    # The name is config DATA. Unescaped, an operator's config could change what is measured.
    assert pr.person_pattern("a.c") == r"\ba\.c\b"
    assert not re.search(pr.person_pattern("a.c"), "abc")


# --------------------------------------------------------------------------------------------
# The ci.yml derivation pin
# --------------------------------------------------------------------------------------------
def test_the_shipped_pattern_still_covers_what_this_check_claims_it_covers() -> None:
    """Behaviour pin, both halves. Reverting the home-path term in `ci.yml`, or dropping the
    extra pattern from the union, makes THIS red, not a silent undercount."""
    rx = _matchers()[pr.CLASS_SANITIZE]
    for probe in (PRODUCT, HOST, HOME_PATH):
        assert rx.search(probe), f"the sanitize class no longer covers {probe!r}"
    for probe in (EXEMPT_PRODUCT, EXEMPT_HOST, PLACEHOLDER_HOME):
        assert not rx.search(probe), f"the sanitize class now fires on the exempt {probe!r}"


def test_the_ci_pattern_line_is_single_quoted_and_never_matches_itself() -> None:
    """Shape pin. `sanitize_pattern` DECODES a shell single-quoted value; if that spelling goes,
    so must the decoder. Two ways this could rot silently, and both are red here:

    * the line stops being single-quoted, and `_PATTERN_LINE_RE` matches nothing -- which the
      module reports as UNMEASURABLE, but only if somebody runs it;
    * the line starts matching itself, and the gate fails every build on its own workflow. The
      shipped generic shapes are written so their source text cannot match them; a literal term
      would need the shell `''` split, and belongs in the extra pattern anyway.
    """
    line = pr.sanitize_pattern_line()
    assert line.lstrip().startswith("pattern='") and line.rstrip().endswith("'")
    pattern = pr.sanitize_pattern()
    assert "''" not in pattern, "every join must be consumed by the reconstruction"
    assert not re.search(pattern, line, re.IGNORECASE), "the shipped pattern matches its own line"


def test_the_sanitize_pattern_is_the_ci_pattern_unioned_with_the_extra_pattern() -> None:
    """gh#821. The private alternatives left ci.yml for a local file; the class must still see
    them, or every pillar page carrying one would read as cleaner overnight."""
    base = pr.sanitize_pattern()
    extra = _extra()
    assert extra.configured and extra.alternatives == 2
    assert pr.sanitize_pattern(extra=extra) == base + "|" + extra.pattern
    assert not re.search(base, PRODUCT, re.IGNORECASE), "the shipped pattern names a placeholder"
    rx = re.compile(pr.sanitize_pattern(extra=extra), re.IGNORECASE)
    assert rx.search(PRODUCT) and rx.search(HOST) and rx.search(HOME_PATH)


def test_an_unconfigured_extra_pattern_leaves_the_ci_pattern_alone(tmp_path: Path) -> None:
    """Absent is "not configured", and the class is the generic shapes - said, not guessed."""
    missing = sanitize_extra.read_extra_pattern(tmp_path / "nowhere.txt")
    assert not missing.configured
    assert pr.sanitize_pattern(extra=missing) == pr.sanitize_pattern()
    assert missing.sources() == (
        f"ci.yml pattern; extra pattern not configured: {tmp_path / 'nowhere.txt'} does not exist"
    )


def test_no_extra_pattern_source_at_all_is_unmeasurable() -> None:
    """No path and no reason is not "not configured": nothing was looked at, so the reader
    refuses and the ratchet cannot measure, rather than scan the generic shapes alone and
    report the drop as a cleaner pillar."""
    with pytest.raises(sanitize_extra.ExtraPatternUnreadable, match="no extra pattern path"):
        sanitize_extra.read_extra_pattern(None)
    with pytest.raises(pr.ResidueUnmeasurable, match="no sanitize extra pattern source"):
        pr.resolve_extra(_ns(sanitize_extra=None))


def test_the_report_names_the_sources_it_applied(pillar: Path, capsys) -> None:
    """A run without the extra file scans less, and must say so rather than read as cleaner."""
    _page(pillar, "dirty", PRODUCT)
    _record(pillar)
    assert pr.run(_ns(), pillar) == 0
    out = capsys.readouterr().out
    assert f"ci.yml pattern + extra pattern from {_EXTRA['path']}" in out

    # With --wiki-root and no explicit file, the pattern is looked for beside the pillar, and a
    # run that finds none names where it looked.
    derived = pr.pillar_extra_path(pillar)
    assert not derived.exists()
    # Fewer hits is never a violation, so the run still holds.
    assert pr.run(_ns(sanitize_extra=None, wiki_root=str(pillar)), pillar) == 0
    out = capsys.readouterr().out
    assert f"extra pattern not configured: {derived} does not exist" in out
    assert "  sanitize     0 pages,    0 hits" in out

    # No source at all is not "not configured": the run cannot measure.
    with pytest.raises(pr.ResidueUnmeasurable, match="no sanitize extra pattern source"):
        pr.run(_ns(sanitize_extra=None), pillar)


def test_the_extra_pattern_resolves_from_the_projects_data_root(tmp_path: Path, monkeypatch) -> None:
    """With `--project`, the file is found beside the sensitive-terms list, under the data root."""
    from types import SimpleNamespace

    from agentflow import config

    data_root = tmp_path / "data"
    want = data_root / "projects" / "demo" / sanitize_extra.EXTRA_PATTERN_RELPATH
    want.parent.mkdir(parents=True)
    want.write_text(EXTRA_FILE_TEXT, encoding="utf-8")
    monkeypatch.setattr(
        config, "load_environment", lambda: SimpleNamespace(data_root=str(data_root))
    )
    got = pr.resolve_extra(_ns(project="demo", sanitize_extra=None))
    assert got.configured and got.path == want


@pytest.mark.parametrize("shape", ["directory", "not-utf8", "bad-regex", "bad-union"])
def test_an_unusable_extra_file_is_exit_two_and_never_prints_it(
    pillar: Path, tmp_path: Path, capsys, shape: str
) -> None:
    """Mirrors the cut's sensitive-terms list: a file that exists and cannot be used is CANNOT
    MEASURE, never a quiet fall-back to the generic shapes. The message carries a path, a line
    number and an offset - never the alternative it could not use."""
    _page(pillar, "clean", "nothing to see")
    _record(pillar)
    bad = tmp_path / "extra-bad"
    secret = "qqsecretalt"
    if shape == "directory":
        bad.mkdir()
    elif shape == "not-utf8":
        bad.write_bytes(b"caf\xe9\n")
    elif shape == "bad-regex":
        bad.write_text(f"# fine\n{secret}(\n", encoding="utf-8")
    else:  # each line compiles alone; a global flag past the start of the union does not
        bad.write_text(f"{secret}\n(?i){secret}x\n", encoding="utf-8")
    assert pr.run_cli(_ns(sanitize_extra=str(bad)), pillar) == 2
    err = capsys.readouterr().err
    assert "CANNOT MEASURE" in err and str(bad) in err
    assert secret not in err and "caf" not in err
    if shape == "bad-regex":
        assert "line 2" in err


def test_the_parse_is_anchored_to_the_sanitize_job(tmp_path: Path) -> None:
    """`ci.yml` holds more than one `pattern=`. Taking the first would check the wrong rule."""
    ci = tmp_path / "ci.yml"
    ci.write_text(
        "jobs:\n"
        "  other-gate:\n"
        "    steps:\n"
        "      - run: |\n"
        "          pattern='DECOY'\n"
        "  sanitize-gate:\n"
        "    steps:\n"
        "      - run: |\n"
        "          pattern='RE''AL'\n"
        "  later-gate:\n"
        "    steps:\n"
        "      - run: |\n"
        "          pattern='ALSO-DECOY'\n",
        encoding="utf-8",
    )
    assert pr.sanitize_pattern(ci) == "REAL"


def test_two_patterns_in_the_sanitize_job_is_unmeasurable(tmp_path: Path) -> None:
    """Ambiguity is refused, not resolved by position. Picking one would be an unowned choice."""
    ci = tmp_path / "ci.yml"
    ci.write_text(
        "jobs:\n"
        "  sanitize-gate:\n"
        "    steps:\n"
        "      - run: |\n"
        "          pattern='ONE'\n"
        "          pattern='TWO'\n",
        encoding="utf-8",
    )
    with pytest.raises(pr.ResidueUnmeasurable, match="exactly one"):
        pr.sanitize_pattern(ci)


def test_a_renamed_sanitize_job_is_unmeasurable(tmp_path: Path) -> None:
    ci = tmp_path / "ci.yml"
    ci.write_text("jobs:\n  something-else:\n    steps: []\n", encoding="utf-8")
    with pytest.raises(pr.ResidueUnmeasurable, match="sanitize-gate"):
        pr.sanitize_pattern(ci)


# --------------------------------------------------------------------------------------------
# Cannot-measure is distinguishable from clean -- each case beside its clean control
# --------------------------------------------------------------------------------------------
def test_a_clean_corpus_with_a_baseline_exits_zero(pillar: Path) -> None:
    """The control every case below is measured against."""
    _page(pillar, "clean", "nothing to see")
    _record(pillar)
    assert pr.run_cli(_ns(), pillar) == 0


def test_no_baseline_is_exit_two_not_zero(pillar: Path, capsys) -> None:
    _page(pillar, "clean", "nothing to see")
    assert pr.run_cli(_ns(), pillar) == 2
    assert "CANNOT MEASURE" in capsys.readouterr().err


def test_a_missing_ci_yml_is_exit_two_not_zero(pillar: Path, tmp_path: Path, capsys) -> None:
    """The class this check refuses to keep a second copy of is the class it cannot fake."""
    _page(pillar, "clean", "nothing to see")
    _record(pillar)
    assert pr.run_cli(_ns(ci_yml=str(tmp_path / "gone.yml")), pillar) == 2
    assert "CANNOT MEASURE" in capsys.readouterr().err


def test_an_undecodable_page_is_exit_two_not_zero(pillar: Path, capsys) -> None:
    """A page nobody could read was neither cleared nor flagged, and must not read as cleared."""
    _page(pillar, "fine", "nothing to see")
    _record(pillar)
    (pillar / "pages" / "bad.md").write_bytes(b"\xff\xfe\x00 not utf-8 \xc3\x28")
    assert pr.run_cli(_ns(), pillar) == 2
    assert "bad" in capsys.readouterr().err


def test_a_missing_pages_dir_is_unmeasurable(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    with pytest.raises(pr.ResidueUnmeasurable, match="no page set"):
        pr.scan_pillar(tmp_path / "wiki", _matchers())


def test_an_empty_person_name_is_refused_not_skipped() -> None:
    """An empty pattern matches everywhere and a skipped class reports zero. Neither is a result."""
    with pytest.raises(pr.ResidueUnmeasurable, match="cannot be measured"):
        pr.person_pattern("   ")


def test_a_baseline_from_an_unknown_schema_is_refused(pillar: Path) -> None:
    _page(pillar, "dirty", PRODUCT)
    _record(pillar)
    path = pr.baseline_path(pillar)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["schema_version"] = 99
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(pr.ResidueUnmeasurable, match="schema_version"):
        pr.load_baseline(path)


# --------------------------------------------------------------------------------------------
# Corpus rules, imported rather than restated
# --------------------------------------------------------------------------------------------
def test_terminal_partitions_are_not_scanned(pillar: Path) -> None:
    """`done/` and `archived/` are the projection's rule, imported so the two cannot diverge."""
    (pillar / "pages" / "done" / "retired.md").write_text(PRODUCT, encoding="utf-8")
    (pillar / "pages" / "archived" / "old.md").write_text(PRODUCT, encoding="utf-8")
    _page(pillar, "live", "clean")
    report = _scan(pillar)
    assert report.pages_scanned == 1 and report.pages == {}


def test_a_page_that_will_not_parse_as_frontmatter_is_still_scanned(pillar: Path) -> None:
    """Deliberately NOT built on `read_pages`.

    `read_pages` raises on the first malformed page, which for a residue check would mean one bad
    page hides the residue on all 500 others. Residue is a property of the BYTES.
    """
    _page(pillar, "no-frontmatter-at-all", f"just prose mentioning {PRODUCT}")
    report = _scan(pillar)
    assert report.pages["no-frontmatter-at-all"].counts[pr.CLASS_SANITIZE] == 1


def test_a_projection_refresh_does_not_delete_the_baseline(pillar: Path) -> None:
    """The baseline shares a directory with files `refresh` sweeps. It must survive one.

    The sweep is scoped to `hot-*.md`; this is the test that keeps that scoping honest, because
    a baseline deleted by an unrelated command comes back as exit 2 and reads like a bug here.
    """
    (pillar / "pages" / "p.md").write_text(
        "---\ntitle: P\ncreated: 2026-01-01\n---\n\nbody with a [[link]]\n", encoding="utf-8"
    )
    _record(pillar)
    path = pr.baseline_path(pillar)
    before = path.read_bytes()
    refresh_projection(pillar)
    assert path.exists() and path.read_bytes() == before


# --------------------------------------------------------------------------------------------
# The baseline file itself
# --------------------------------------------------------------------------------------------
def test_the_baseline_tells_the_next_reader_why_it_is_a_ratchet(pillar: Path) -> None:
    """The reasoning lives where somebody would otherwise "fix" the ratchet into a hard gate."""
    _page(pillar, "dirty", PRODUCT)
    _record(pillar)
    doc = json.loads(pr.baseline_path(pillar).read_text(encoding="utf-8"))
    note = doc["note"]
    assert "RATCHET, NOT A GATE" in note
    assert "does not join the sanitize pattern" in note, (
        "the recorded decision has to be stated where the number is met"
    )
    assert "hard failure" in note


def test_history_is_appended_not_replaced(pillar: Path) -> None:
    """The one thing paying down "no version history" -- so it cannot quietly stop happening."""
    _page(pillar, "dirty", f"{PRODUCT} {PRODUCT}")
    _record(pillar, "2026-01-01")
    (pillar / "pages" / "dirty.md").write_text(PRODUCT, encoding="utf-8")
    _record(pillar, "2026-02-02")

    history = pr.load_baseline(pr.baseline_path(pillar)).history
    assert [h["recorded"] for h in history] == ["2026-01-01", "2026-02-02"]
    assert [h["totals"][pr.CLASS_SANITIZE] for h in history] == [2, 1]


def test_the_cli_appends_history_it_loaded(pillar: Path) -> None:
    """`write_baseline` takes the history it is given; the CLI is what must pass it through."""
    _page(pillar, "dirty", f"{PRODUCT} {PRODUCT}")
    assert pr.run_cli(_ns(update_baseline="2026-01-01"), pillar) == 0
    (pillar / "pages" / "dirty.md").write_text(PRODUCT, encoding="utf-8")
    assert pr.run_cli(_ns(update_baseline="2026-02-02"), pillar) == 0

    history = pr.load_baseline(pr.baseline_path(pillar)).history
    assert [h["recorded"] for h in history] == ["2026-01-01", "2026-02-02"]


def test_recording_a_WORSENING_is_refused_unless_it_is_asked_for_in_one_word(pillar: Path) -> None:
    """The ratchet's direction has to be a property of the tool, not of the caller's intentions.

    `--update-baseline` is the only command that writes, and it used to return 0 whatever it
    recorded - so the one command that can loosen the ratchet was also the one whose exit code
    never reported the corpus. A scripted nightly `--update-baseline` would then absorb every
    regression and log success forever, which is the ratchet failing in the one direction it
    exists to prevent, silently.
    """
    _record(pillar)
    _page(pillar, "newly-dirty", PRODUCT)
    before = pr.baseline_path(pillar).read_bytes()

    assert pr.run(_ns(update_baseline="2026-09-22"), pillar) == 1
    assert pr.baseline_path(pillar).read_bytes() == before, (
        "a refused recording must leave the baseline byte-identical; a partial write here would "
        "raise the ratchet by exactly the amount the refusal was meant to stop"
    )


def test_accepting_a_worsening_is_possible_but_has_to_be_said(pillar: Path) -> None:
    """The refusal is a speed bump, not a wall - a human who means it must still be able to."""
    _record(pillar)
    _page(pillar, "newly-dirty", PRODUCT)
    before = pr.baseline_path(pillar).read_bytes()

    assert pr.run(_ns(update_baseline="2026-09-22", accept_worsening=True), pillar) == 0
    assert pr.baseline_path(pillar).read_bytes() != before


def test_LOWERING_the_baseline_never_needs_the_flag(pillar: Path) -> None:
    """Clearing residue must stay frictionless, or the tool teaches people not to clear it."""
    _page(pillar, "dirty", PRODUCT)
    _record(pillar)
    (pillar / pr.PAGES_DIRNAME / "dirty.md").unlink()

    assert pr.run(_ns(update_baseline="2026-09-22"), pillar) == 0


# --------------------------------------------------------------------------------------------
# An existing baseline that will not load is not a missing one
# --------------------------------------------------------------------------------------------
CORRUPT_BASELINE = b'{"schema_version": 1, "pages": {"dirty": {"sanitize": 1}'  # truncated
DATE = "2026-10-03"


def _aside_files(pillar: Path) -> list[Path]:
    return sorted(pillar.glob("residue-baseline.unreadable-*"))


def test_a_corrupt_baseline_refuses_update_with_exit_two_and_is_untouched(
    pillar: Path, capsys
) -> None:
    """The swallow this replaces recorded new residue over a corrupt baseline with exit 0 and
    replaced its dated history with a placeholder. A file that will not parse cannot show the
    run is not a worsening, so it gets the same refusal a worsening does - but as 2, not 1,
    because nothing was learned about whether the corpus got dirtier."""
    _page(pillar, "newly-dirty", PRODUCT)
    path = pr.baseline_path(pillar)
    path.write_bytes(CORRUPT_BASELINE)

    assert pr.run_cli(_ns(update_baseline=DATE), pillar) == 2
    assert path.read_bytes() == CORRUPT_BASELINE
    assert _aside_files(pillar) == []
    out, err = capsys.readouterr()
    assert "REFUSED" in err and "--accept-worsening" in err
    # The remedy accepts every page, so the pages are printed before it is named.
    assert "pages/newly-dirty.md:1 sanitize" in out
    assert f"residue-baseline.unreadable-{DATE}.json" in err


@pytest.mark.parametrize("text", [
    json.dumps([1, 2, 3]),
    json.dumps({"schema_version": 1, "pages": {"dirty": [1]}}),
    json.dumps({"schema_version": 1, "pages": {"dirty": {"sanitize": "many"}}}),
    json.dumps({"schema_version": 1, "pages": {}, "history": "not-a-list"}),
    json.dumps({"schema_version": 1}),
    '{"schema_version": 1, "pages": {"a": {"sanitize": Infinity}}}',
    '{"schema_version": 1, "pages": {}, "pages_scanned": Infinity}',
    '{"schema_version": 1, "pages": {"a": {"sanitize": 1' + "9" * 5000 + "}}}",
    json.dumps({"schema_version": True, "pages": {}}),
    json.dumps({"schema_version": 0, "pages": {}}),
])
def test_a_wrong_shape_baseline_is_unreadable_not_a_traceback(pillar: Path, text: str) -> None:
    """JSON that parses but has the wrong shape used to escape as AttributeError/ValueError/
    OverflowError - a traceback, which exits 1, the code that says the corpus got dirtier."""
    path = pr.baseline_path(pillar)
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(pr.BaselineUnreadable):
        pr.load_baseline(path)
    assert pr.run_cli(_ns(update_baseline=DATE), pillar) == 2
    assert pr.run_cli(_ns(), pillar) == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", [2, 2.0, "2", 1.5])
def test_a_newer_schema_baseline_is_refused_even_with_accept_worsening(
    pillar: Path, capsys, version
) -> None:
    """Recording over a newer shape would downgrade the file; no flag makes that right."""
    _page(pillar, "dirty", PRODUCT)
    _record(pillar)
    path = pr.baseline_path(pillar)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["schema_version"] = version
    path.write_text(json.dumps(doc), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(pr.BaselineNewerSchema):
        pr.load_baseline(path)
    rc = pr.run_cli(_ns(update_baseline=DATE, accept_worsening=True), pillar)
    assert rc == 2
    assert path.read_bytes() == before
    assert _aside_files(pillar) == []
    assert "Upgrade agentflow" in capsys.readouterr().err


def test_a_newer_schema_on_a_plain_run_does_not_offer_accept_worsening(
    pillar: Path, capsys
) -> None:
    _record(pillar)
    path = pr.baseline_path(pillar)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["schema_version"] = pr.BASELINE_SCHEMA_VERSION + 1
    path.write_text(json.dumps(doc), encoding="utf-8")

    assert pr.run_cli(_ns(), pillar) == 2
    err = capsys.readouterr().err
    assert "Upgrade agentflow" in err and "--accept-worsening" not in err


def test_a_read_error_on_a_good_baseline_cannot_be_bypassed(pillar: Path, monkeypatch) -> None:
    """A sharing violation says nothing about the file's contents. If it were BaselineUnreadable,
    --accept-worsening would reset a good file's history over a transient; it must stay exit 2
    whatever the flags, with the file untouched and nothing kept aside."""
    _page(pillar, "dirty", PRODUCT)
    _record(pillar)
    path = pr.baseline_path(pillar)
    before = path.read_bytes()
    real = Path.read_bytes

    def held(self):
        if self == path:
            raise PermissionError(13, "held by another process", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", held)
    with pytest.raises(pr.ResidueUnmeasurable) as info:
        pr.load_baseline(path)
    assert not isinstance(info.value, pr.BaselineUnreadable)
    assert pr.run_cli(_ns(update_baseline=DATE, accept_worsening=True), pillar) == 2
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert _aside_files(pillar) == []


def test_a_corrupt_baseline_with_accept_worsening_records_and_keeps_the_old_file(
    pillar: Path, capsys
) -> None:
    _page(pillar, "newly-dirty", PRODUCT)
    path = pr.baseline_path(pillar)
    path.write_bytes(CORRUPT_BASELINE)

    assert pr.run_cli(_ns(update_baseline=DATE, accept_worsening=True), pillar) == 0
    baseline = pr.load_baseline(path)
    assert baseline.count("newly-dirty", pr.CLASS_SANITIZE) == 1
    asides = _aside_files(pillar)
    assert [p.name for p in asides] == [pr.aside_path(path, DATE).name]
    assert asides[0].read_bytes() == CORRUPT_BASELINE
    # No fabricated placeholder entry: one real entry, pointing at where the old history went.
    assert len(baseline.history) == 1
    assert baseline.history[0]["recorded"] == DATE
    assert baseline.history[0]["replaced_unreadable_baseline"] == asides[0].name
    assert str(asides[0]) in capsys.readouterr().err


def test_a_second_aside_on_the_same_date_never_overwrites_the_first(pillar: Path) -> None:
    path = pr.baseline_path(pillar)
    path.write_bytes(CORRUPT_BASELINE)
    assert pr.run_cli(_ns(update_baseline=DATE, accept_worsening=True), pillar) == 0
    path.write_bytes(b"also not json")
    assert pr.run_cli(_ns(update_baseline=DATE, accept_worsening=True), pillar) == 0

    assert sorted(p.read_bytes() for p in _aside_files(pillar)) == sorted(
        [CORRUPT_BASELINE, b"also not json"]
    )


def test_a_missing_baseline_still_records_freely(pillar: Path) -> None:
    _page(pillar, "dirty", PRODUCT)
    assert pr.run_cli(_ns(update_baseline=DATE), pillar) == 0
    assert pr.load_baseline(pr.baseline_path(pillar)).count("dirty", pr.CLASS_SANITIZE) == 1
    assert _aside_files(pillar) == []


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are not creatable here: {exc!r}")


def test_a_dangling_symlink_baseline_is_unreadable_not_missing(pillar: Path, tmp_path) -> None:
    """It reads as missing, but recording would replace the link with no copy kept."""
    _page(pillar, "dirty", PRODUCT)
    path = pr.baseline_path(pillar)
    target = tmp_path / "gone.json"
    _symlink_or_skip(path, target)
    pointed_at = path.readlink()

    assert pr.run_cli(_ns(update_baseline=DATE), pillar) == 2
    assert path.is_symlink() and _aside_files(pillar) == []

    assert pr.run_cli(_ns(update_baseline=DATE, accept_worsening=True), pillar) == 0
    asides = _aside_files(pillar)
    assert len(asides) == 1 and asides[0].is_symlink()
    assert asides[0].readlink() == pointed_at
    assert pr.load_baseline(path).count("dirty", pr.CLASS_SANITIZE) == 1


def test_a_failed_write_over_an_unreadable_baseline_leaves_the_old_one_and_no_aside(
    pillar: Path, monkeypatch
) -> None:
    """Exit 3 must never leave no baseline: the old file is copied aside, not moved - and once
    the write has failed, that copy is a stray duplicate and is removed."""
    _page(pillar, "dirty", PRODUCT)
    path = pr.baseline_path(pillar)
    path.write_bytes(CORRUPT_BASELINE)

    def refuse(target, text):
        raise PermissionError(f"denied: {target}")

    monkeypatch.setattr(pr, "atomic_write", refuse)
    assert pr.run_cli(_ns(update_baseline=DATE, accept_worsening=True), pillar) == 3
    assert path.read_bytes() == CORRUPT_BASELINE
    assert _aside_files(pillar) == []


def test_a_failed_aside_copy_leaves_no_partial_file(pillar: Path, monkeypatch) -> None:
    _page(pillar, "dirty", PRODUCT)
    path = pr.baseline_path(pillar)
    path.write_bytes(CORRUPT_BASELINE)

    def no_sync(fd):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(pr.os, "fsync", no_sync)
    assert pr.run_cli(_ns(update_baseline=DATE, accept_worsening=True), pillar) == 3
    assert path.read_bytes() == CORRUPT_BASELINE
    assert _aside_files(pillar) == []


def test_write_baseline_appends_to_the_history_it_is_given_without_reading_the_file(
    pillar: Path,
) -> None:
    """One read, in the caller: the file appended to is the one the caller checked."""
    path = pr.baseline_path(pillar)
    path.write_bytes(CORRUPT_BASELINE)
    prior = [{"recorded": "2026-01-01"}]
    pr.write_baseline(path, pr.Report(), recorded=DATE, history=prior)
    assert [h["recorded"] for h in pr.load_baseline(path).history] == ["2026-01-01", DATE]
    assert prior == [{"recorded": "2026-01-01"}]


def test_recording_a_baseline_never_happens_without_being_asked(pillar: Path) -> None:
    """A check that recorded on every run would ratchet nothing: it would accept every page."""
    _page(pillar, "dirty", PRODUCT)
    _record(pillar)
    _page(pillar, "newly-dirty", PRODUCT)
    before = pr.baseline_path(pillar).read_bytes()
    assert pr.run(_ns(), pillar) == 1
    assert pr.baseline_path(pillar).read_bytes() == before


# --------------------------------------------------------------------------------------------
# gh#651: --update-baseline takes a real YYYY-MM-DD date, never free text
# --------------------------------------------------------------------------------------------
def _residue_parser() -> argparse.ArgumentParser:
    return pr.add_arguments(argparse.ArgumentParser(prog="residue"))


@pytest.mark.parametrize("bad", ["last tuesday", "2026-13-01", "2026-02-30", "20260930",
                                 "2026-09-30\n", ""])
def test_update_baseline_refuses_a_value_that_is_not_a_date(bad: str, capsys) -> None:
    """The value is stored verbatim as the baseline's `recorded` stamp; 'last tuesday' used to
    land in the file as-is because nothing checked the metavar's promise."""
    with pytest.raises(SystemExit) as excinfo:
        _residue_parser().parse_args(["--update-baseline", bad])
    assert excinfo.value.code != 0
    assert "--update-baseline" in capsys.readouterr().err


def test_update_baseline_names_the_bad_value(capsys) -> None:
    with pytest.raises(SystemExit):
        _residue_parser().parse_args(["--update-baseline", "last tuesday"])
    err = capsys.readouterr().err
    assert "--update-baseline" in err
    assert "'last tuesday' is not a YYYY-MM-DD date" in err


def test_update_baseline_accepts_a_real_date() -> None:
    a = _residue_parser().parse_args(["--update-baseline", "2026-09-30"])
    assert a.update_baseline == "2026-09-30"


# --------------------------------------------------------------------------------------------
# A violation says WHERE: path, line and class per hit - never the matched text
# --------------------------------------------------------------------------------------------
def _lines_page(pillar: Path, slug: str, hits: dict[int, str], length: int = 12) -> Path:
    """A page whose line N carries ``hits[N]`` and whose every other line is clean prose."""
    body = "\n".join(hits.get(n, f"clean line {n}") for n in range(1, length + 1)) + "\n"
    return _page(pillar, slug, body)


def _leaks(text: str) -> list[str]:
    """Every matched literal (or a case variant of one) that made it into ``text``."""
    low = text.lower()
    return [lit for lit in (HOME_PATH, FAKE_PERSON, PRODUCT) if lit.lower() in low]


def test_a_new_dirty_page_names_path_line_and_class_for_each_hit(pillar: Path, capsys) -> None:
    """Counts alone left the reader rebuilding ci.yml's regex by hand to find the hit."""
    _record(pillar)
    _lines_page(pillar, "deploy-lesson", {3: f"see {HOME_PATH}/notes", 7: f"asked {FAKE_PERSON}"})

    found = pr.violations(_scan(pillar), pr.load_baseline(pr.baseline_path(pillar)))
    joined = "\n".join(found)
    assert len(found) == 1, "one violation per page; the hit lines belong to it"
    assert "pages/deploy-lesson.md:3 sanitize" in joined
    assert "pages/deploy-lesson.md:7 person" in joined
    assert "sanitize=1, person=1" in joined, "the counts the report already printed stay"

    assert pr.run(_ns(), pillar) == 1, "the exit code is unchanged"
    out = capsys.readouterr().out
    assert "pages/deploy-lesson.md:3 sanitize" in out
    assert "pages/deploy-lesson.md:7 person" in out


def test_two_hits_of_one_class_on_one_line_print_once_with_a_count(pillar: Path) -> None:
    """Two identical path:line class lines read as a duplicated finding, not as two hits."""
    _record(pillar)
    _lines_page(pillar, "twice", {4: f"{FAKE_PERSON} and {FAKE_PERSON} again"})

    joined = "\n".join(pr.violations(_scan(pillar), pr.load_baseline(pr.baseline_path(pillar))))
    assert "person=2" in joined, "the count still says two"
    assert joined.count("pages/twice.md:4 person") == 1
    assert "pages/twice.md:4 person (x2)" in joined


def test_a_worsened_page_names_the_lines_of_the_class_that_grew(pillar: Path) -> None:
    _lines_page(pillar, "grew", {2: f"{PRODUCT} once"})
    _record(pillar)
    _lines_page(pillar, "grew", {2: f"{PRODUCT} once", 9: f"and {FAKE_PERSON}"})

    joined = "\n".join(pr.violations(_scan(pillar), pr.load_baseline(pr.baseline_path(pillar))))
    assert "pages/grew.md:9 person" in joined
    assert "pages/grew.md:2 sanitize" not in joined, "the class that did not grow is not a finding"


def test_the_matched_literal_never_appears_in_the_output(pillar: Path, capsys) -> None:
    """The pattern is deliberately unreadable; printing what it matched would publish the term
    in every log the report lands in."""
    _record(pillar)
    _lines_page(pillar, "leaky", {4: f"path {HOME_PATH}", 5: f"{FAKE_PERSON} said", 6: PRODUCT})

    found = pr.violations(_scan(pillar), pr.load_baseline(pr.baseline_path(pillar)))
    assert found and _leaks("\n".join(found)) == []
    assert pr.run(_ns(), pillar) == 1
    captured = capsys.readouterr()
    assert _leaks(captured.out + captured.err) == []
    pr.write_baseline(pr.baseline_path(pillar), _scan(pillar), recorded="2026-02-02")
    assert _leaks(pr.baseline_path(pillar).read_text(encoding="utf-8")) == []


def test_an_old_format_baseline_still_loads(pillar: Path) -> None:
    """A baseline written before the hit lines existed must keep working, unmodified."""
    _lines_page(pillar, "old-dirty", {1: PRODUCT})
    pr.baseline_path(pillar).write_text(json.dumps({
        "schema_version": 1,
        "recorded": "2026-09-22",
        "pages_scanned": 1,
        "pages": {"old-dirty": {"sanitize": 1}},
    }), encoding="utf-8")

    baseline = pr.load_baseline(pr.baseline_path(pillar))
    assert baseline.count("old-dirty", pr.CLASS_SANITIZE) == 1
    assert pr.run(_ns(), pillar) == 0


# --------------------------------------------------------------------------------------------
# The baseline lines say what they count, and growth of a clean pillar is visible (gh#885)
# --------------------------------------------------------------------------------------------
def test_a_growing_clean_pillar_shows_its_growth_not_zero_new_pages(pillar: Path, capsys) -> None:
    """The repro: an empty baseline, four clean pages written after it. The old line read
    '0 pages are new since then'; the residue count stays 0, and the scanned line shows +4."""
    _record(pillar)
    for n in range(4):
        _page(pillar, f"clean-{n}", "says nothing anybody has to decide about")
    assert pr.run(_ns(), pillar) == 0
    out = capsys.readouterr().out
    assert "pages are new since then" not in out, "the label that miscounted growth is gone"
    assert "0 pages not in the baseline carry residue" in out
    assert "pages scanned: 4 now, 0 at baseline (+4)" in out


def test_the_residue_count_counts_only_new_pages_that_carry_residue(pillar: Path, capsys) -> None:
    """One dirty and one clean page after the baseline: residue says 1, scanned says +2."""
    _page(pillar, "old", "nothing to see")
    _record(pillar)
    _lines_page(pillar, "new-dirty", {1: PRODUCT})
    _page(pillar, "new-clean", "nothing to see")
    assert pr.run(_ns(), pillar) == 1
    out = capsys.readouterr().out
    assert "1 pages not in the baseline carry residue" in out
    assert "pages scanned: 3 now, 1 at baseline (+2)" in out


def test_a_shrinking_pillar_prints_a_negative_delta(pillar: Path, capsys) -> None:
    """The delta is signed: pages moved to a terminal partition are not scanned, so it can fall."""
    _page(pillar, "a", "nothing to see")
    _page(pillar, "b", "nothing to see")
    _record(pillar)
    (pillar / "pages" / "b.md").unlink()
    assert pr.run(_ns(), pillar) == 0
    assert "pages scanned: 1 now, 2 at baseline (-1)" in capsys.readouterr().out


def test_no_registry_id_ships_in_the_note_or_the_report(pillar: Path, capsys) -> None:
    """A registry id belongs to one install; a stranger's baseline must not cite it."""
    _record(pillar)
    note = json.loads(pr.baseline_path(pillar).read_text(encoding="utf-8"))["note"]
    assert not re.search(r"\bQ-\d+", note)
    assert pr.run(_ns(), pillar) == 0
    assert not re.search(r"\bQ-\d+", capsys.readouterr().out)
    assert not re.search(r"\bQ-\d+", Path(pr.__file__).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------
# With no baseline, the report is still printed: every residue page, by path, line and class
# --------------------------------------------------------------------------------------------
def _no_baseline_pillar(pillar: Path) -> None:
    _lines_page(pillar, "old-note", {3: f"see {HOME_PATH}/notes", 5: f"asked {FAKE_PERSON}"})
    _lines_page(pillar, "vendor", {2: f"{PRODUCT} twice {PRODUCT}"})
    _page(pillar, "clean", "says nothing anybody has to decide about")


def test_no_baseline_prints_every_residue_page_and_still_exits_two(pillar: Path, capsys) -> None:
    """The refusal tells the reader to read the report before recording; the report has to exist."""
    _no_baseline_pillar(pillar)
    assert pr.run_cli(_ns(), pillar) == 2, "a missing baseline is still not a verdict"
    captured = capsys.readouterr()
    assert "CANNOT MEASURE" in captured.err and "no baseline" in captured.err
    out = captured.out
    assert "pages/old-note.md:3 sanitize" in out
    assert "pages/old-note.md:5 person" in out
    assert "pages/vendor.md:2 sanitize (x2)" in out
    assert "pages/clean.md" not in out, "a clean page is not something recording accepts"
    assert "ratchet held" not in out, "no baseline, no verdict"
    assert _leaks(captured.out + captured.err) == []
    assert not pr.baseline_path(pillar).exists(), "reading the report never records it"


def test_update_baseline_names_every_page_it_accepts_before_recording(pillar: Path, capsys) -> None:
    """Recording accepts every page listed, so the list comes first, by path - not counts alone."""
    _no_baseline_pillar(pillar)
    assert pr.run(_ns(update_baseline="2026-10-02"), pillar) == 0
    out = capsys.readouterr().out
    recorded_at = out.index("baseline recorded at ")
    for hit in ("pages/old-note.md:3 sanitize", "pages/old-note.md:5 person",
                "pages/vendor.md:2 sanitize (x2)"):
        assert hit in out and out.index(hit) < recorded_at, f"{hit!r} must precede the write"
    assert _leaks(out) == []
    assert set(pr.load_baseline(pr.baseline_path(pillar)).pages) == {"old-note", "vendor"}


def test_update_baseline_over_an_existing_baseline_still_names_the_pages(
    pillar: Path, capsys
) -> None:
    """Re-recording accepts the listed pages again; the list is printed on every recording."""
    _no_baseline_pillar(pillar)
    _record(pillar)
    assert pr.run(_ns(update_baseline="2026-10-02"), pillar) == 0
    out = capsys.readouterr().out
    assert out.index("pages/old-note.md:5 person") < out.index("baseline recorded at ")


# --------------------------------------------------------------------------------------------
# --wiki-root finds the project's extra pattern the way the writer does
# --------------------------------------------------------------------------------------------
#: A placeholder private term for the derived extra pattern file. Names nothing.
DERIVED_TERM = "secretcorp"


@pytest.fixture
def project_pillar(tmp_path: Path, monkeypatch) -> Path:
    """``<data_root>/projects/demo/wiki`` with the project's extra pattern beside it, a page that
    carries the pattern's term, and no baseline entry for that page."""
    from types import SimpleNamespace

    from agentflow import config
    from agentflow.wiki import initial_pillar_files

    data_root = tmp_path / "data"
    root = data_root / "projects" / "demo" / "wiki"
    (root / "pages").mkdir(parents=True)
    for name, text in initial_pillar_files("demo").items():
        (root / name).write_text(text, encoding="utf-8", newline="\n")
    extra = data_root / "projects" / "demo" / sanitize_extra.EXTRA_PATTERN_RELPATH
    extra.parent.mkdir(parents=True)
    extra.write_text(DERIVED_TERM + "\n", encoding="utf-8")
    monkeypatch.setattr(
        config, "load_environment",
        lambda: SimpleNamespace(data_root=str(data_root),
                                person=SimpleNamespace(name=FAKE_PERSON)),
    )
    # gh#794: --project must name a configured project; `demo` is one here.
    monkeypatch.setattr(config, "load_project", lambda name, **_kw: SimpleNamespace(name=name))
    pr.write_baseline(pr.baseline_path(root), pr.Report(pages_scanned=0), recorded="2026-01-01")
    _page(root, "vendor", f"we evaluated {DERIVED_TERM} last week\n")
    return root


def test_wiki_root_applies_the_extra_pattern_beside_the_pillar(project_pillar: Path, capsys) -> None:
    """--wiki-root and --project name the same pillar; they must give the same verdict on it."""
    from agentflow import wiki

    by_project = wiki.main(["residue", "--project", "demo", "--person-name", FAKE_PERSON])
    out_project = capsys.readouterr().out
    by_root = wiki.main(["residue", "--wiki-root", str(project_pillar),
                         "--person-name", FAKE_PERSON])
    captured = capsys.readouterr()
    out_root = captured.out

    assert by_project == 1 and "NEW PAGE WITH RESIDUE  vendor" in out_project
    assert by_root == 1, "the pillar --project fails must not pass under --wiki-root"
    assert "NEW PAGE WITH RESIDUE  vendor  (sanitize=1)" in out_root
    assert "pages/vendor.md:1 sanitize" in out_root
    derived = project_pillar.parent / sanitize_extra.EXTRA_PATTERN_RELPATH
    assert f"extra pattern from {derived}" in out_root
    assert DERIVED_TERM not in (out_project + out_root + captured.err).lower()


def test_wiki_root_and_the_writer_build_the_same_sanitize_class(project_pillar: Path) -> None:
    """One derivation of the extra pattern's path from a wiki root, shared by the CLI and the
    writer's save-time guard, so the two cannot disagree about one pillar."""
    from agentflow.wiki import PageResidueError, write_page

    extra = pr.resolve_extra(_ns(wiki_root=str(project_pillar), sanitize_extra=None))
    assert extra.configured
    cli = pr.compile_classes(person_name=FAKE_PERSON, extra=extra)
    assert cli[pr.CLASS_SANITIZE].pattern == pr.writer_matchers(project_pillar)[
        pr.CLASS_SANITIZE].pattern
    with pytest.raises(PageResidueError):
        write_page(project_pillar, slug="another", title="Another",
                   body=f"{DERIVED_TERM} [[vendor]]", created="2026-09-30")


def test_an_explicit_extra_pattern_beats_the_one_beside_the_wiki_root(project_pillar: Path) -> None:
    """--sanitize-extra-pattern names the file outright; the pillar's own file never displaces it."""
    beside = project_pillar.parent / sanitize_extra.EXTRA_PATTERN_RELPATH
    assert beside.is_file()
    got = pr.resolve_extra(_ns(wiki_root=str(project_pillar)))
    assert got.configured and got.path == _EXTRA["path"]


def test_wiki_root_beats_project_for_the_extra_pattern(project_pillar: Path) -> None:
    """With both flags, the pattern follows the pillar scanned, not the project's data root."""
    elsewhere = pr.resolve_extra(_ns(project="elsewhere", sanitize_extra=None))
    assert not elsewhere.configured
    got = pr.resolve_extra(
        _ns(project="elsewhere", wiki_root=str(project_pillar), sanitize_extra=None)
    )
    assert got.configured
    assert got.path == project_pillar.parent / sanitize_extra.EXTRA_PATTERN_RELPATH


def test_a_relative_wiki_root_finds_the_extra_pattern_beside_the_pillar(
    project_pillar: Path, monkeypatch, capsys
) -> None:
    """`--wiki-root .` from inside the pillar names the same pillar as its absolute path, so it
    finds the same file and gives the same verdict, for the CLI and the writer alike."""
    from agentflow import wiki

    monkeypatch.chdir(project_pillar)
    want = project_pillar.parent / sanitize_extra.EXTRA_PATTERN_RELPATH
    assert pr.pillar_extra_path(Path(".")) == want
    got = pr.resolve_extra(_ns(wiki_root=".", sanitize_extra=None))
    assert got.configured and got.path == want
    assert pr.writer_matchers(Path("."))[pr.CLASS_SANITIZE].pattern == pr.compile_classes(
        person_name=FAKE_PERSON, extra=got)[pr.CLASS_SANITIZE].pattern

    assert wiki.main(["residue", "--wiki-root", ".", "--person-name", FAKE_PERSON]) == 1
    captured = capsys.readouterr()
    assert "NEW PAGE WITH RESIDUE  vendor  (sanitize=1)" in captured.out
    assert DERIVED_TERM not in (captured.out + captured.err).lower()
