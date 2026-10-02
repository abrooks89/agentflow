"""Rewrite the retired `queue_class` value across one project's surveyed corpus.

The hard cut in `retire-the-autoloop-queue-class-value` removes the retired value from
`DISPATCHABLE_QUEUE_CLASSES` with no dual-accept window, so between the merge and this
script running, every spec still carrying the old value surveys as out-of-vocabulary and
dispatches nothing. That window is the cost the decision chose; this script is what closes
it.

WHAT IT TOUCHES, AND WHY THAT SET AND NOT ANOTHER. The reach is DERIVED, not re-spelled:
it is `SurveyPatterns.for_project(project)`, the same object `survey_epics()` globs with.
A hand-written glob here would be a second definition of "the corpus", and the two would
drift the first time the layout moved - which is gh#122's exact shape, a writer filing
into directories the reader could not see. Deriving it means this script's reach cannot be
wrong unless the survey's reach is wrong too, and the survey has the whole suite testing
it.

Of the survey's three glob families it uses TWO - `spec_globs` and `plan_globs`. Those are
precisely the two passes that call `_read_sidecar`, which is the only reader of
`queue_class` anywhere. The third family, `status_dir_globs`, walks status boards whose
frontmatter carries `epic:`, never `queue_class`; including it would widen the write
surface to files that cannot contain the field. It also happens to be the family that
contains `status/legacy-loop-state/`, so leaving it out is what makes that exclusion
structural rather than a special case someone has to remember.

Three exclusions the spec names, and how each is achieved WITHOUT a bespoke rule:

* `.bak*` siblings - a backup is named `<file>.md.bak-<who>-<when>` or `<file>.md.bak`, so
  it does not end in `.md` and the `*.md` glob never yields it. Measured on the live tree:
  every one of the 30 backups under `<data_root>` ends past the `.md`, and none matches.
* `status/legacy-loop-state/` - under the `status` family, which is not globbed at all.
* archived plans - `<data_root>/archive/<project>/`, a SIBLING of `projects/`, outside
  every `projects/*/...` pattern. Note this is NOT the same thing as
  `design/plans/archived/`: that one is a `TERMINAL_PARTITIONS` folder, `survey_epics()`
  reads it, its records still carry dependency math, and it IS rewritten. Two different
  senses of the word "archived" sit one directory apart, and conflating them would either
  strand terminal records on a dead value or vandalize the archive. The distinction is
  asserted by test, in both directions.

WHAT IT CHANGES INSIDE A FILE: the `queue_class` line of the YAML frontmatter block, and
nothing else. Not prose, not code fences, not a second `queue_class:` further down the
body - a spec ABOUT queue_class vocabulary quotes the retired value in its own text, and
rewriting those sentences would silently edit the historical record a spec is. The file is
read and written as BYTES, so line endings, BOM, trailing whitespace and every unrelated
character survive untouched; on a Windows fleet a text-mode round-trip would rewrite every
LF in the file to CRLF and land a one-token change as a whole-file diff.

It drops no `.bak` of its own. The corpus is in git; a backup file beside a tracked file
is a second copy with no owner, and this fleet already carries 30 of them.

HOW TO RUN IT. The mode is never inferred - `--dry-run` reports and writes nothing,
`--apply` writes, and a bare `--project <name>` REFUSES with a nonzero exit:

    python -m agentflow.migrate_queue_class --project <name> --dry-run
    python -m agentflow.migrate_queue_class --project <name> --apply

Both halves of that are deliberate. Defaulting to the write would put a human's live
corpus one forgotten flag away from a rewrite nobody asked for; defaulting to the dry run
would print `to_rewrite=N` to an operator who believed they had just migrated, and a
migration that never ran is indistinguishable in that output from one that finished.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .epic_survey import DEFAULT_SURVEY_PATTERNS, _FRONTMATTER_RE, SurveyPatterns
from .vocabulary import QUEUE_CLASS_REWRITES


def _value_re(old: str) -> re.Pattern[str]:
    """Match a frontmatter `queue_class:` line whose value is exactly *old*.

    Quoting is preserved rather than normalized: the backreference makes `'x'` and `"x"`
    and bare `x` all match, and only the value token is replaced, so a file that quoted
    its value keeps its quotes and a file that did not keeps that too. The trailing
    lookahead is what stops the retired value from matching inside a LONGER token that
    merely starts with it (`<retired>-v2`) - without it such a value would be silently
    truncated to the new one. The retired spelling is deliberately not written here even
    as an example: `vocabulary.py` is its one tracked home, and an illustration in a
    docstring is exactly how a second home starts.
    """
    return re.compile(
        r"(?m)^(?P<lead>[ \t]*queue_class[ \t]*:[ \t]*)"
        r"(?P<q>['\"]?)" + re.escape(old) + r"(?P=q)(?=[ \t\r\n#]|$)"
    )


_VALUE_RES: dict[str, re.Pattern[str]] = {old: _value_re(old) for old in QUEUE_CLASS_REWRITES}


@dataclass(frozen=True)
class Rewrite:
    """One file that carries a retired value in its frontmatter."""

    path: Path
    old: str
    new: str


@dataclass
class MigrationPlan:
    """What a run would do. `scanned` counts every file the survey would parse."""

    project: str
    data_root: Path
    scanned: int = 0
    unreadable: list[tuple[Path, str]] = field(default_factory=list)
    rewrites: list[Rewrite] = field(default_factory=list)


def survey_parsed_files(
    data_root: Path | str,
    project: str,
    patterns: SurveyPatterns = DEFAULT_SURVEY_PATTERNS,
) -> list[Path]:
    """Every file `survey_epics()` would parse for `queue_class`, for *project*.

    The `.status.md` skip mirrors the survey's own, at both call sites, for the same
    reason: a status board is not a spec even when it sits in a spec bucket.
    """
    root = Path(data_root)
    scoped = patterns.for_project(project)
    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in (*scoped.spec_globs, *scoped.plan_globs):
        for f in sorted(root.glob(pattern)):
            if f.name.endswith(".status.md"):
                continue
            if f in seen or not f.is_file():
                continue
            seen.add(f)
            out.append(f)
    return out


def rewrite_text(text: str) -> tuple[str, Optional[tuple[str, str]]]:
    """Return `(new_text, (old, new))`, or `(text, None)` when nothing matched.

    Scoped to the OPENING frontmatter block by slicing on `_FRONTMATTER_RE` - the same
    pattern `_read_sidecar` uses to decide what counts as frontmatter, so this script can
    only ever edit a line the survey would actually have read. A file with no frontmatter
    block is returned unchanged even if its body says the retired word a hundred times.
    """
    block = _FRONTMATTER_RE.match(text)
    if block is None:
        return text, None
    head, tail = text[: block.end()], text[block.end() :]
    for old, new in QUEUE_CLASS_REWRITES.items():
        patched, count = _VALUE_RES[old].subn(
            lambda m: f"{m.group('lead')}{m.group('q')}{new}{m.group('q')}", head
        )
        if count:
            return patched + tail, (old, new)
    return text, None


def plan_migration(
    data_root: Path | str,
    project: str,
    patterns: SurveyPatterns = DEFAULT_SURVEY_PATTERNS,
) -> MigrationPlan:
    """Scan without writing. Reports rather than raises on a file it cannot decode.

    An undecodable file is NAMED, not skipped silently: it is a file the survey also
    cannot read, and the operator running this at merge is exactly the person who should
    be told the corpus has one.
    """
    plan = MigrationPlan(project=project, data_root=Path(data_root))
    for path in survey_parsed_files(data_root, project, patterns):
        plan.scanned += 1
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            plan.unreadable.append((path, f"{type(exc).__name__}: {exc}"))
            continue
        _, hit = rewrite_text(text)
        if hit is not None:
            plan.rewrites.append(Rewrite(path=path, old=hit[0], new=hit[1]))
    return plan


def apply_plan(plan: MigrationPlan) -> int:
    """Write the rewrites. Returns how many files changed.

    Re-reads and re-derives each file rather than trusting the scan's decision: the plan
    may be seconds or minutes old, and a file edited in between must not be written from
    a stale premise. A file that no longer matches is simply not written.
    """
    written = 0
    for rw in plan.rewrites:
        raw = rw.path.read_bytes()
        new_text, hit = rewrite_text(raw.decode("utf-8"))
        if hit is None:
            continue
        rw.path.write_bytes(new_text.encode("utf-8"))
        written += 1
    return written


def render(plan: MigrationPlan, dry_run: bool) -> str:
    prefix = "[DRY-RUN]" if dry_run else "[APPLY]"
    root = plan.data_root
    lines: list[str] = []
    for rw in plan.rewrites:
        try:
            shown = rw.path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - path is always under root
            shown = rw.path.as_posix()
        lines.append(f"{prefix} {shown}: queue_class {rw.old} -> {rw.new}")
    for path, reason in plan.unreadable:
        lines.append(f"UNREADABLE {path.as_posix()} - {reason}")
    lines.append("")
    lines.append(
        f"project={plan.project} scanned={plan.scanned} "
        f"to_rewrite={len(plan.rewrites)} unreadable={len(plan.unreadable)}"
    )
    if dry_run:
        lines.append("Dry run: no file was written.")
    return "\n".join(lines)


def main(argv=None) -> int:
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    ap = argparse.ArgumentParser(
        prog="python -m agentflow.migrate_queue_class",
        description="Rewrite retired queue_class values across one project's surveyed corpus.",
    )
    ap.add_argument("--project", required=True, help="project name under <data_root>/projects/")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change and write nothing",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write the rewrites; the destructive mode, and it must be asked for by name",
    )
    ap.add_argument(
        "--data-root",
        help="override the configured data_root (tests and one-off trees)",
    )
    args = ap.parse_args(argv)

    # The mode is never inferred. Writing by default puts a live corpus one forgotten flag
    # from an unasked-for rewrite; dry-running by default prints `to_rewrite=N` to an
    # operator who believed they had migrated, which reads exactly like a finished run.
    if args.dry_run and args.apply:
        print(
            "--dry-run and --apply contradict each other; pass exactly one.",
            file=sys.stderr,
        )
        return 2
    if not (args.dry_run or args.apply):
        print(
            "Refusing to run: pass --dry-run to report what would change (writes nothing) "
            "or --apply to write it. This script rewrites files in the live corpus, so it "
            "will not guess which you meant.",
            file=sys.stderr,
        )
        return 2

    if args.data_root:
        data_root = Path(args.data_root)
    else:
        from .config import load_environment

        data_root = Path(load_environment().data_root)

    plan = plan_migration(data_root, args.project)
    print(render(plan, args.dry_run))
    if args.apply and plan.rewrites:
        print(f"rewrote {apply_plan(plan)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
