"""Residue check for the ``wiki/`` pillar, with a ratchet - the thing CI structurally cannot do.

WHY THIS IS NOT A CI JOB, and why nobody should try again. The sanitize gate
(``.github/workflows/ci.yml``, job ``sanitize-gate``) does ``actions/checkout@v4`` and greps
``.``. The pillar is not in that checkout: it lives under ``<data_root>/projects/<name>/wiki/``,
which is outside the repository and is not a git repository at all. ``ci.yml`` already says in
its own words that CI cannot resolve the data root. So "extend the CI gate to walk the data
root" is not a task that was skipped - it is unbuildable, and the pillar is out of that gate's
reach BY CONSTRUCTION. This module is what covers it instead, and it runs where the pillar is:
on the machine that owns the data root, from the same entry point the steward already calls
(``python -m agentflow.wiki residue --project <name>``).

WHY A RATCHET AND NOT A GATE. Measured 2026-09-22 over 507 live pages: 48 pages carry a term
the CI sanitize pattern already covers, 32 carry the bare personal name, and the union needing
a decision is 71 pages - 14% of the corpus, and 42 of those 71 carry exactly one hit. A plain
gate over that corpus is red on the day it ships, and a gate that is red on the day it ships is
a gate somebody turns off. So the recorded baseline is the contract: **the number can only go
down, and any page that did not exist at baseline time must be clean.** That is a backstop
against the corpus getting worse while the open question about the existing 71 is decided by a
human. Do not "fix" this into a hard failure - doing so does not clean a single page, it only
removes the signal that the next page is dirty.

WHY TWO CLASSES, KEPT APART. They are different kinds of fact and merging them buries one:

* ``sanitize`` - terms the sanitize pattern covers: ``ci.yml``'s generic shapes, plus the
  install-specific alternatives of the local extra pattern file when it is configured. On a
  repo file these are a build failure today; on a pillar page they are simply unreached. This
  class is a REACH gap.
* ``person`` - the bare personal name, which that pattern does **not** contain. Whether it
  ever joins the pattern is an open human decision (registry Q-10079), not a defect. This class
  is an OPEN QUESTION, and a single merged number would report it as though it were settled.

NEITHER CLASS IS SPELLED OUT IN THIS FILE, deliberately, and not only to keep the sanitize gate
green on this module. A second copy of a pattern is the read/write skew this fleet keeps
re-learning: the copy drifts, and the checker then reports on a rule nobody edits.

* the ``sanitize`` terms are PARSED OUT OF ``ci.yml`` at run time (:func:`sanitize_pattern`), so
  the gate's reach and this check's reach cannot disagree - and the parse is pinned by a test
  that fails when that line changes shape. The alternatives that must never ship are not in
  ``ci.yml`` at all: they come from the local extra pattern file, read by the one shared reader
  (:mod:`agentflow.sanitize_extra`) and unioned in, and the report names which sources it
  applied, so a run without that file says so instead of quietly scanning less;
* the ``person`` term is derived from ``config.load_environment().person.name``, so it is
  correct per install and this repo never carries anybody's name.

STDLIB ONLY, no clock, no network, no model call - the same contract as :mod:`agentflow.wiki`,
whose ``PAGES_DIRNAME`` and ``TERMINAL_PARTITIONS`` this module imports rather than restates so
the two cannot disagree about which files are live pages.

READ-ONLY OVER THE PILLAR. This module never writes, moves or edits a page. Rewriting pillar
pages is barred by standing decision and gated on Q-10079; the only file it can write is its own
baseline, and only when asked with ``--update-baseline``.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import datetime
import json
from pathlib import Path
import re
import sys
from typing import Iterator, Sequence

from agentflow.atomic_io import atomic_write
from agentflow.sanitize_extra import (
    ExtraPattern,
    ExtraPatternUnreadable,
    read_extra_pattern,
    resolve_extra_path,
    union,
)
from agentflow.wiki import _CREATED_RE, PAGES_DIRNAME, TERMINAL_PARTITIONS

#: The two classes, kept separate on purpose (see the banner). Order is report order.
CLASS_SANITIZE = "sanitize"
CLASS_PERSON = "person"
CLASSES: tuple[str, ...] = (CLASS_SANITIZE, CLASS_PERSON)

#: Where the baseline lives: in the DATA ROOT, beside the pillar it describes, not in the repo.
#:
#: THE TRADE, stated where the next reader meets it. A repo copy would be versioned and
#: reviewable, which is the better property in the abstract. It is the wrong answer here for a
#: reason that is not a preference: this file is a statement about ONE install's data root. The
#: repo is about to be handed to somebody else, whose pillar shares none of these pages and whose
#: `person.name` is a different string - a repo-side baseline would ship to them as a lie on day
#: one, and the first thing it would do is either fail their clean pillar or excuse 71 pages they
#: do not have. Co-location also keeps the ratchet honest without a commit: removing residue is
#: data-root work, and requiring a pull request to record it would couple vault hygiene to repo
#: review and guarantee the baseline lags the corpus.
#:
#: WHAT THAT COSTS, named rather than glossed: no version history and no review. Two things pay
#: it down - the file carries a `history` list that this module APPENDS to and never rewrites, so
#: each lowering leaves a dated record in the file itself, and the baseline is never written
#: except on an explicit `--update-baseline` (or seeded EMPTY by install/provision.py for a
#: pillar that has no pages yet, which has no residue to excuse).
BASELINE_FILENAME = "residue-baseline.json"

#: Bumped when the on-disk shape changes. A baseline from an unknown version is refused as
#: UNMEASURABLE rather than read optimistically - a mis-read baseline reports clean.
BASELINE_SCHEMA_VERSION = 1

#: ``ci.yml``'s job key, at its YAML indent. The parse is anchored to the JOB and not to the
#: first `pattern=` in the file, because there is a second one (the worktree tool gate) and a
#: parser that took the first match would silently check the wrong rule.
_JOB_KEY = "  sanitize-gate:"
_JOB_KEY_RE = re.compile(r"^  [A-Za-z0-9_-]+:\s*$")
_PATTERN_LINE_RE = re.compile(r"^\s*pattern='(?P<body>.*)'\s*$")

#: The repo root, from this module's own location: ``skills/_shared/agentflow/`` -> up three.
#: This resolves the ci.yml that SHIPPED WITH THIS CODE, which is the only one whose reach this
#: module is entitled to claim. A single file read, not a walk.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


class ResidueUnmeasurable(Exception):
    """The check could not do its job.

    Raised, never swallowed, and mapped to its own exit code. This is the failure mode that
    matters most here: a residue checker that cannot read its pattern, cannot decode a page, or
    cannot find its baseline has NOT observed a clean corpus, and reporting zero would be worse
    than reporting red - a zero nobody can trust is indistinguishable from a working check.
    """


@dataclass(frozen=True)
class PageResidue:
    """Per-page hit counts, by class. Only pages with at least one hit are kept."""

    slug: str
    counts: dict[str, int]

    def total(self) -> int:
        return sum(self.counts.values())


@dataclass
class Report:
    """What one scan saw. ``unreadable`` is why a report can be a refusal rather than a verdict."""

    pages_scanned: int = 0
    pages: dict[str, PageResidue] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)

    def class_pages(self, cls: str) -> list[str]:
        return sorted(s for s, p in self.pages.items() if p.counts.get(cls))

    def class_total(self, cls: str) -> int:
        return sum(p.counts.get(cls, 0) for p in self.pages.values())

    def pages_new_vs(self, baseline: "Baseline") -> list[str]:
        """Residue-carrying pages the baseline does not list - the ratchet's first rule."""
        return sorted(s for s in self.pages if s not in baseline.pages)


# --------------------------------------------------------------------------- #
# Pattern derivation - the ONE copy of each rule, read from where it is owned
# --------------------------------------------------------------------------- #
def sanitize_pattern_line(ci_yml: Path = DEFAULT_CI_YML) -> str:
    """The RAW ``pattern='...'`` source line from ``ci.yml``'s sanitize job, unreconstructed.

    Returned separately from the reconstructed pattern so a test can pin the SHAPE of the line -
    that it is still one shell single-quoted value, which is the assumption
    :func:`sanitize_pattern` decodes, and that it does not match itself.
    """
    try:
        lines = ci_yml.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ResidueUnmeasurable(
            f"cannot read the sanitize pattern from {ci_yml}: {exc!r}. This check derives the "
            f"sanitize class from that file rather than keeping a second copy, so without it the "
            f"class cannot be measured at all - which is not the same as measuring it clean"
        ) from exc

    try:
        start = lines.index(_JOB_KEY)
    except ValueError:
        raise ResidueUnmeasurable(
            f"{ci_yml} has no {_JOB_KEY.strip()!r} job at a two-space indent. Either the job was "
            f"renamed or the file was reformatted; this parse is anchored to the job on purpose, "
            f"because the file holds more than one `pattern=` and the wrong one reads as green"
        ) from None

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _JOB_KEY_RE.match(lines[i]):
            end = i
            break

    found = [ln for ln in lines[start:end] if _PATTERN_LINE_RE.match(ln)]
    if len(found) != 1:
        raise ResidueUnmeasurable(
            f"expected exactly one single-quoted `pattern='...'` line inside the "
            f"{_JOB_KEY.strip()!r} job of {ci_yml}; found {len(found)}. Picking one of several, "
            f"or none, would make this check report on a rule nobody chose"
        )
    return found[0]


def sanitize_pattern(ci_yml: Path = DEFAULT_CI_YML, extra: ExtraPattern | None = None) -> str:
    """``ci.yml``'s sanitize pattern, reconstructed as the shell would build it, UNIONED with
    the local extra pattern when ``extra`` is a configured one.

    A literal in that line would have to be split with adjacent single-quoted strings
    (``'ab''cd'``) so the workflow file never matched itself. ``sh`` concatenates adjacent
    quoted strings, and inside single quotes there is no escaping - so an empty pair ``''`` can
    only ever be such a join, and removing every one of them reconstructs exactly what ``grep``
    receives. The shipped line carries generic shapes only, which need no join; the decoding
    stays so a future generic term that does need one is read correctly.

    ``extra`` is :func:`agentflow.sanitize_extra.read_extra_pattern`'s result. It is appended
    the way the CI step appends the repository variable, so CI, the cut's check and this module
    all build the same expression.
    """
    body = _PATTERN_LINE_RE.match(sanitize_pattern_line(ci_yml)).group("body")
    base = body.replace("''", "")
    try:
        re.compile(base)
    except re.error as exc:
        raise ResidueUnmeasurable(
            f"the pattern reconstructed from {ci_yml} does not compile: {exc}. The line's shape "
            f"changed in a way this parser does not understand; fix the parser rather than "
            f"keeping a second copy of the pattern here"
        ) from exc
    pattern = union(base, extra)
    try:
        re.compile(pattern)
    except re.error as exc:
        # The extra pattern compiled on its own, so only the join can fail here. Offset only:
        # the message must not carry the private alternatives.
        raise ResidueUnmeasurable(
            f"the {ci_yml} pattern and the extra pattern from {extra.path} do not compile once "
            f"OR-ed together (error at offset {exc.pos})"
        ) from None
    return pattern


def person_pattern(name: str) -> str:
    """The bare personal name, word-bounded and escaped.

    Word-bounded because an unbounded match would fire inside unrelated words and inflate the one
    class whose size is an input to a human decision. Escaped because the name is DATA out of the
    operator's config, and a config value reaching ``re.compile`` unescaped is a config value that
    can change what this check measures.
    """
    if not isinstance(name, str) or not name.strip():
        raise ResidueUnmeasurable(
            "the configured person name is empty, so the `person` class cannot be measured. "
            "This is a refusal, not a clean result: an empty pattern matches everywhere and a "
            "skipped class reports zero"
        )
    return r"\b" + re.escape(name.strip()) + r"\b"


def compile_classes(
    *, ci_yml: Path = DEFAULT_CI_YML, person_name: str, extra: ExtraPattern | None = None
) -> dict[str, re.Pattern]:
    """One compiled expression per class. ``IGNORECASE`` to match the gate, which greps ``-i``.

    ``extra`` joins the ``sanitize`` class (:func:`sanitize_pattern`); None leaves that class
    at ``ci.yml``'s generic shapes."""
    return {
        CLASS_SANITIZE: re.compile(sanitize_pattern(ci_yml, extra), re.IGNORECASE),
        CLASS_PERSON: re.compile(person_pattern(person_name), re.IGNORECASE),
    }


def resolve_extra(a: argparse.Namespace) -> ExtraPattern:
    """The extra pattern this run unions into the ``sanitize`` class.

    ``--sanitize-extra-pattern`` wins; else the project's file under the data root, found the
    way :mod:`agentflow.publish` finds it; else not configured. A file that exists and cannot be
    used, or a project whose data root will not resolve, is UNMEASURABLE - falling back to the
    generic shapes would count fewer terms and report the drop as a cleaner pillar.
    """
    explicit = getattr(a, "sanitize_extra", None)
    project = getattr(a, "project", None)
    path = Path(explicit) if explicit else None
    if path is None and project:
        from . import config

        try:
            path = resolve_extra_path(project)
        except (config.ConfigError, OSError, UnicodeDecodeError, ValueError) as exc:
            raise ResidueUnmeasurable(
                "the sanitize extra pattern path was not resolved: "
                f"{config.error_line(exc, config.environment_path())} "
                "(or pass --sanitize-extra-pattern)"
            ) from exc
    try:
        return read_extra_pattern(path)
    except ExtraPatternUnreadable as exc:
        raise ResidueUnmeasurable(str(exc)) from None


# --------------------------------------------------------------------------- #
# The scan (READ-ONLY over the pillar)
# --------------------------------------------------------------------------- #
def iter_live_pages(wiki_root: Path) -> Iterator[tuple[str, Path]]:
    """``(slug, path)`` for every live page under ``wiki_root/pages/``, sorted by path.

    The ONE definition of the page set the ratchet measures: ``*.md`` under ``pages/`` outside the
    terminal partitions. Anything else that must agree with :func:`scan_pillar` about which pages
    exist (provision's decision to seed an empty baseline) calls this rather than restating it.
    A missing ``pages/`` yields nothing; :func:`scan_pillar` refuses that case itself.
    """
    pages_dir = wiki_root / PAGES_DIRNAME
    if not pages_dir.is_dir():
        return
    for path in sorted(pages_dir.rglob("*.md")):
        rel = path.relative_to(pages_dir)
        if TERMINAL_PARTITIONS.intersection(rel.parts[:-1]):
            continue
        yield rel.with_suffix("").as_posix(), path


def scan_pillar(wiki_root: Path, matchers: dict[str, re.Pattern]) -> Report:
    """Count residue per live page under ``wiki_root/pages/``.

    ``done/`` and ``archived/`` are skipped for the same reason :func:`agentflow.wiki.read_pages`
    skips them - they are terminal partitions, not live pages - and the rule is IMPORTED from
    there rather than restated, so the ratchet and the projection cannot come to disagree about
    which pages exist.

    A page that will not decode goes into ``unreadable`` and does NOT count as clean. This is the
    whole of the difference between "the corpus is clean" and "I could not look".

    Decoded ``utf-8-sig``, matching the reader: a BOM'd page decoded as plain ``utf-8`` still
    scans, but keeping the two decoders identical means a page this check calls clean is the same
    bytes the projection calls parseable.
    """
    pages_dir = wiki_root / PAGES_DIRNAME
    if not pages_dir.is_dir():
        raise ResidueUnmeasurable(
            f"{pages_dir} does not exist, so there is no page set to measure. A project with no "
            f"pillar is not a project with a clean pillar"
        )

    report = Report()
    for slug, path in iter_live_pages(wiki_root):
        report.pages_scanned += 1
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError) as exc:
            report.unreadable.append(f"{slug}: {exc!r}")
            continue
        counts = {cls: len(rx.findall(text)) for cls, rx in matchers.items()}
        counts = {cls: n for cls, n in counts.items() if n}
        if counts:
            report.pages[slug] = PageResidue(slug=slug, counts=counts)
    return report


# --------------------------------------------------------------------------- #
# The baseline and the ratchet
# --------------------------------------------------------------------------- #
#: Written into every baseline file. The next reader meets the ratchet here, in the file, rather
#: than having to find this module - which is where somebody would otherwise "fix" it.
BASELINE_NOTE = (
    "RATCHET, NOT A GATE. Every page listed here already carried residue when this baseline was "
    "recorded. A plain gate over them would be red on the day it shipped, and a gate that is red "
    "on the day it ships is a gate somebody turns off - so the contract is direction, not zero: "
    "a page not listed here must be clean, and a page listed here may not get worse. Do NOT "
    "convert this into a hard failure; that cleans no page and only removes the signal that the "
    "NEXT page is dirty. Whether the `person` class should ever have been residue at all is an "
    "open human decision (registry Q-10079) - that is why it is counted apart from `sanitize` "
    "instead of summed with it. Lower it with `--update-baseline` after removing residue; never "
    "hand-edit a count upward."
)


@dataclass(frozen=True)
class Baseline:
    pages: dict[str, dict[str, int]]
    recorded: str
    pages_scanned: int
    history: list[dict]

    def count(self, slug: str, cls: str) -> int:
        return self.pages.get(slug, {}).get(cls, 0)


def baseline_path(wiki_root: Path) -> Path:
    return wiki_root / BASELINE_FILENAME


def load_baseline(path: Path) -> Baseline:
    """Read a baseline, or refuse. Never returns a permissive default.

    A missing or unreadable baseline is UNMEASURABLE, not "everything is new" and not
    "everything is allowed". Both of those defaults are a silent verdict about 500 files.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ResidueUnmeasurable(
            f"no baseline at {path}. The ratchet compares against a recorded state, so without "
            f"one there is nothing to compare and this run is not a verdict. Record the current "
            f"corpus with `--update-baseline` - read the report first, because recording is "
            f"accepting every page it lists"
        ) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ResidueUnmeasurable(f"baseline at {path} is unreadable: {exc!r}") from exc

    version = raw.get("schema_version")
    if version != BASELINE_SCHEMA_VERSION:
        raise ResidueUnmeasurable(
            f"baseline at {path} is schema_version {version!r}, this code speaks "
            f"{BASELINE_SCHEMA_VERSION}. Refusing rather than reading it optimistically: a "
            f"misread baseline excuses pages nobody excused"
        )
    pages = raw.get("pages")
    if not isinstance(pages, dict):
        raise ResidueUnmeasurable(f"baseline at {path} has no `pages` object")
    return Baseline(
        pages={str(k): {str(c): int(n) for c, n in v.items()} for k, v in pages.items()},
        recorded=str(raw.get("recorded", "unknown")),
        pages_scanned=int(raw.get("pages_scanned", 0)),
        history=list(raw.get("history", [])),
    )


def write_baseline(path: Path, report: Report, *, recorded: str) -> None:
    """Record ``report`` as the new baseline, APPENDING to the history rather than replacing it.

    ``recorded`` is passed in, never read from a clock: this module has no clock, and a caller
    that must supply the date cannot accidentally stamp a run it did not make.
    """
    history = []
    if path.exists():
        try:
            history = list(load_baseline(path).history)
        except ResidueUnmeasurable:
            # An unreadable OLD baseline must not block recording a new one, but it must not be
            # silently dropped either: the gap is written into the history it interrupts.
            history = [{"recorded": "unknown", "note": "previous baseline was unreadable"}]
    history.append(
        {
            "recorded": recorded,
            "pages_scanned": report.pages_scanned,
            "pages_with_residue": len(report.pages),
            "totals": {cls: report.class_total(cls) for cls in CLASSES},
        }
    )
    doc = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "note": BASELINE_NOTE,
        "recorded": recorded,
        "pages_scanned": report.pages_scanned,
        "classes": list(CLASSES),
        "totals": {cls: report.class_total(cls) for cls in CLASSES},
        "pages_with_residue": len(report.pages),
        "pages": {
            slug: dict(sorted(res.counts.items()))
            for slug, res in sorted(report.pages.items())
        },
        "history": history,
    }
    atomic_write(path, json.dumps(doc, indent=2, sort_keys=False) + "\n")


def violations(report: Report, baseline: Baseline) -> list[str]:
    """Where the corpus got WORSE than the baseline. Empty means the ratchet held.

    Two rules, and only two:

    * a page the baseline does not list may not carry residue at all - that is the "any NEW page
      must be clean" half, and it is what makes this useful on a corpus that grows every time a
      lane saves a page;
    * a page the baseline does list may not carry MORE of any class than it did - per class, not
      in total, so a page that loses one covered term and gains a personal name is a finding
      rather than a wash.

    A page that got cleaner, or disappeared, is never a violation; it is what `--update-baseline`
    is for.
    """
    out: list[str] = []
    for slug in sorted(report.pages):
        counts = report.pages[slug].counts
        if slug not in baseline.pages:
            detail = ", ".join(f"{cls}={counts[cls]}" for cls in CLASSES if cls in counts)
            out.append(
                f"NEW PAGE WITH RESIDUE  {slug}  ({detail}) - this page is not in the baseline, "
                f"so it was written after the ratchet was set and had to be clean"
            )
            continue
        for cls in CLASSES:
            now, was = counts.get(cls, 0), baseline.count(slug, cls)
            if now > was:
                out.append(
                    f"RESIDUE INCREASED      {slug}  {cls}: {was} -> {now} - the baseline is a "
                    f"ceiling per page per class"
                )
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(
    report: Report, baseline: Baseline | None, found: list[str], sources: str | None = None
) -> str:
    """The on-demand report. Classes side by side and never summed.

    ``sources`` is :meth:`ExtraPattern.sources` - which patterns the ``sanitize`` class applied,
    so a run without the extra pattern says it scanned less rather than reading as cleaner."""
    lines = [
        f"pillar residue: {report.pages_scanned} live pages scanned, "
        f"{len(report.pages)} carrying residue",
        "",
    ]
    for cls in CLASSES:
        pages = report.class_pages(cls)
        gloss = {
            CLASS_SANITIZE: "terms the CI sanitize pattern already covers "
                            f"({sources or 'parsed from ci.yml'}; "
                            "CI cannot reach these pages, this check is their only cover)",
            CLASS_PERSON: "the bare personal name, which that pattern does NOT contain; whether "
                          "it should is an open human decision (registry Q-10079)",
        }[cls]
        lines.append(f"  {cls:<9} {len(pages):>4} pages, {report.class_total(cls):>4} hits")
        lines.append(f"            {gloss}")
    lines.append("")
    if baseline is not None:
        lines.append(
            f"  baseline recorded {baseline.recorded}: {len(baseline.pages)} pages with residue "
            f"over {baseline.pages_scanned} scanned"
        )
        lines.append(f"  {len(report.pages_new_vs(baseline))} pages are new since then")
        lines.append("")
    if found:
        lines.append("RATCHET VIOLATED - the corpus is dirtier than the recorded baseline:")
        lines.extend(f"  {v}" for v in found)
    elif baseline is not None:
        lines.append("ratchet held: no new page carries residue and no page got worse.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI - reached as `python -m agentflow.wiki residue`, the entry point the steward already calls
# --------------------------------------------------------------------------- #
def iso_date(value: str) -> str:
    """argparse type for ``--update-baseline``: a real ``YYYY-MM-DD`` date, or a usage error.

    The value is stored verbatim as the baseline's ``recorded`` stamp, so free text such as
    ``'last tuesday'`` would land in the file as-is. Shape is the same ``_CREATED_RE`` a wiki
    page's ``created`` must match; the parse on top rejects shapes that are not dates
    (``2026-13-01``).
    """
    if not _CREATED_RE.match(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date")
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid date: {exc}") from exc
    return value


def add_arguments(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Wire the ``residue`` options onto a subparser owned by :mod:`agentflow.wiki`."""
    sp.add_argument("--project", help="project routing key (derives the path via config)")
    sp.add_argument("--wiki-root", dest="wiki_root", help="explicit wiki/ path (wins over --project)")
    sp.add_argument("--person-name", dest="person_name",
                    help="override the personal name (default: config person.name)")
    sp.add_argument("--ci-yml", dest="ci_yml",
                    help="explicit ci.yml to derive the sanitize pattern from "
                         "(default: the one shipped beside this code)")
    sp.add_argument("--sanitize-extra-pattern", dest="sanitize_extra",
                    help="explicit local-only extra sanitize pattern file, unioned into the "
                         "sanitize class (default: <data_root>/projects/<project>/publish/"
                         "sanitize-extra-pattern.txt; absent = not configured)")
    sp.add_argument("--update-baseline", dest="update_baseline", metavar="YYYY-MM-DD",
                    type=iso_date,
                    help="RECORD the current corpus as the new baseline, stamped with this date. "
                         "Accepts every page in the report; read it first. Lowering the baseline "
                         "is free; raising it is REFUSED unless --accept-worsening is given.")
    sp.add_argument("--accept-worsening", dest="accept_worsening", action="store_true",
                    help="allow --update-baseline to record a corpus that is WORSE than the "
                         "recorded baseline. Without it, recording a worsening is refused: the "
                         "one command that can write is otherwise the one whose exit code never "
                         "reports the corpus, so a scripted run could absorb a regression and "
                         "still log success.")
    return sp


def run(a: argparse.Namespace, wiki_root: Path) -> int:
    """Exit codes, and what each one does and does NOT mean.

    * **0 - the ratchet held.** No page outside the baseline carries residue and no page got
      worse. It does NOT mean the pillar is clean: the baseline lists pages that still carry
      residue, on purpose, and the report prints their count every run.
    * **1 - the ratchet was violated.** A page written since the baseline carries residue, or a
      baselined page got worse. The corpus is dirtier than it was. This is the ONLY code that
      says anything about the corpus.
    * **2 - the check could not do its job (UNMEASURABLE).** No baseline, no ``ci.yml``, a
      pattern this parser cannot read, an extra pattern file that exists but cannot be used,
      an undecodable page, no project, an environment config that will not load (for
      ``person.name`` or ``data_root``). Nothing has been observed
      about the corpus, in either direction. A caller must not read this as clean, and must not
      read it as dirty either.
    * **3 - the filesystem refused** while recording a baseline.

    The split between 1 and 2 is the point. A checker that returned 0 when it could not read its
    own pattern would report a clean corpus it never looked at, which is worse than red.
    """
    person_name = a.person_name
    if not person_name:
        from . import config

        # An environment that will not load says nothing about the corpus. Uncaught, it exits 1
        # with a traceback, and 1 is the code that reports the corpus got dirtier.
        try:
            person_name = config.load_environment().person.name
        except (config.ConfigError, OSError, UnicodeDecodeError) as exc:
            raise ResidueUnmeasurable(
                "person.name not resolved: the environment config did not load: "
                f"{config.error_line(exc, config.environment_path())} (or pass --person-name)"
            ) from exc

    ci_yml = Path(a.ci_yml) if a.ci_yml else DEFAULT_CI_YML
    extra = resolve_extra(a)
    matchers = compile_classes(ci_yml=ci_yml, person_name=person_name, extra=extra)
    report = scan_pillar(wiki_root, matchers)

    if report.unreadable:
        raise ResidueUnmeasurable(
            "these pages could not be decoded, so they were neither cleared nor flagged; a "
            "verdict that silently skips a page is not a verdict:\n  "
            + "\n  ".join(report.unreadable)
        )

    path = baseline_path(wiki_root)
    if a.update_baseline:
        existing = None
        try:
            existing = load_baseline(path)
        except ResidueUnmeasurable:
            pass
        found = violations(report, existing) if existing else []
        print(render(report, existing, found, extra.sources()))
        # LOWERING the baseline is always free; RAISING it needs the word. Recording is
        # accepting, so this is the one command that can loosen the ratchet - and it used to
        # return 0 unconditionally, which made the only command that writes also the only one
        # whose exit code never reported the corpus. A scripted --update-baseline could absorb
        # a worsening and log success. Refusing here keeps the ratchet's DIRECTION a property
        # of the tool rather than of the caller's good intentions.
        if found and not a.accept_worsening:
            print(
                "\nREFUSED: this run is WORSE than the recorded baseline, so recording it "
                "would raise the ratchet rather than lower it. Clear the residue, or pass "
                "--accept-worsening to say in one word that you mean to accept it.",
                file=sys.stderr,
            )
            return 1
        write_baseline(path, report, recorded=a.update_baseline)
        print(f"\nbaseline recorded at {path} ({a.update_baseline})")
        if found:
            print(
                f"RAISED the ratchet: {len(found)} violation(s) accepted via "
                "--accept-worsening.",
                file=sys.stderr,
            )
        return 0

    baseline = load_baseline(path)
    found = violations(report, baseline)
    print(render(report, baseline, found, extra.sources()))
    return 1 if found else 0


def run_cli(a: argparse.Namespace, wiki_root: Path) -> int:
    """:func:`run`, with every exception mapped to its exit code. ONE copy of that mapping.

    Both entry points land here - ``python -m agentflow.wiki residue`` and this module's own
    ``main`` - because two copies of an exit-code table is how ``2`` starts meaning ``0`` on one
    of the two paths, which is precisely the confusion this check exists to avoid.
    """
    try:
        return run(a, wiki_root)
    except (ResidueUnmeasurable, FileNotFoundError, ValueError) as exc:
        # FileNotFoundError and ValueError arrive from path/project resolution; they are the same
        # kind of fact as ResidueUnmeasurable - nothing was observed about the corpus.
        print(f"pillar residue: CANNOT MEASURE - {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"pillar residue failed on the filesystem: {exc!r}", file=sys.stderr)
        return 3


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone entry point, so this module is runnable without going through `wiki`."""
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    p = argparse.ArgumentParser(prog="python -m agentflow.pillar_residue")
    add_arguments(p)
    a = p.parse_args(None if argv is None else list(argv))
    from .wiki import _resolve_wiki_root

    try:
        wiki_root = _resolve_wiki_root(a)
    except (FileNotFoundError, ValueError) as exc:
        print(f"pillar residue: CANNOT MEASURE - {exc}", file=sys.stderr)
        return 2
    return run_cli(a, wiki_root)


if __name__ == "__main__":
    raise SystemExit(main())
