"""Reconcile a project's `design/` buckets against the `current_state` in each file.

`done/` and `archived/` are a MATERIALIZED VIEW of `current_state`, never an independent
fact. Nothing decides where a file goes; this pass reads frontmatter and moves the file to
match, and reports any file it cannot place. The moment a location can disagree with
frontmatter you have two states and no rule for which wins.

WHY THIS EXISTS AT ALL: flat is unnavigable. Re-measured 2026-09-05 on the largest live
project: one `specs/` bucket holds 404 files of which 313 are closed - so the folder a
human opens drops to 91 once the closed ones move outward. Every file is born active and
most move exactly once, outward. These figures are a snapshot and will drift; the reason
does not.

THE VOCABULARY IS BORROWED, NEVER RESTATED. `TERMINAL_CURRENT_STATES`, `_norm_state` and
`TERMINAL_PARTITIONS` come from `epic_survey`, which is the module the whole suite already
trusts to decide whether an epic's story is over. A second list here would be a second
definition of "done" - exactly the drift the layout spec forbids when it insists on `done/`
rather than `delivered/`: a second word for the same state is how drift starts.

The DIRECTORY half of that rule was added late, and its absence was gh#122: the partition
names lived here while the survey's globs named the bucket roots only, so this pass filed
terminal records into folders the survey could not read. Borrowing the names is not enough
on its own - the survey DERIVES its globs from that tuple, so a partition cannot be added
to this pass without the read side following it.

A MOVE CARRIES THE WHOLE FILE, NOT JUST ITS PRIMARY. The `.bak` siblings other loops wrote
beside a spec belong to the file, not to the path, so they travel with it; the grouping rule
is `backups.group_primary`, called rather than re-derived. Moving only the primary was gh#288:
`backups reap` refuses a group whose primary is absent, on the correct rule that its backups
are then the only surviving copy - and this pass manufactured that state on every sweep,
burying sixteen real refusals under fifty false ones.

UNPLACEABLE IS A THIRD OUTCOME, not an error and not a default. A file whose
`current_state` cannot be read is NOT assumed active and NOT assumed closed; it is left
exactly where it is and named in the report. 170 files on the live tree are in this
condition, and quietly treating them as active would hide every one of them.

A GENERATED FILE DECLARES ITSELF, AND IS COUNTED RATHER THAN REFUSED (gh#176). The bug
mirror writes `design/bugs/index.md` and `design/bugs/closed-<YYYY-MM>.md`, which are a
rendering of the tracker and correctly carry no frontmatter - so this pass refused them,
correctly and forever, and gained a new permanent refusal every month as the closed-month
file rolled over. Two of three unplaceables were the mirror the day it shipped. A list whose
job is to demand attention cannot have a growing majority of entries that need none.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .backups import backup_group
from .epic_survey import TERMINAL_CURRENT_STATES, TERMINAL_PARTITIONS, _norm_state
# The banner a generated file opens with, BORROWED from the module that writes it for the
# same reason the state vocabulary is borrowed from the survey: a copy here would be a
# second definition of "generated", and the two would drift the first time the banner was
# reworded. A private-name import is the convention this module already follows for
# `_norm_state`. Plain `#` and not `#:` on purpose - this explains an IMPORT, and a `#:`
# block binds to the assignment that follows it, which would attach this to the next
# constant instead.
from .issue_intake import _DO_NOT_EDIT as GENERATED_BANNER
from .plan_parser import parse_spec_frontmatter


#: Terminal states that mean "delivered" and belong in `done/`.
DONE_STATES: frozenset[str] = frozenset({"done", "shipped"})

#: Terminal states that mean "closed without delivering" and belong in `archived/`.
ARCHIVED_STATES: frozenset[str] = TERMINAL_CURRENT_STATES - DONE_STATES

#: The subfolders a file can be moved into come from `epic_survey.TERMINAL_PARTITIONS`, for
#: the same reason the states do: this pass WRITES those directories and the survey READS
#: them, and a local copy here is how the two halves drift apart. That drift was gh#122 -
#: this pass moved terminal files into subfolders the survey's globs could not reach, so
#: every record it filed vanished from the board and stranded its dependents. A file that
#: belongs in no partition lives in the bucket root, which is the whole point: opening
#: `specs/` shows what is live.


@dataclass(frozen=True)
class Move:
    """One planned relocation. `dest` is "" for the bucket root (the active position).

    `backups` is the file's backup group - the `.bak` siblings other loops wrote beside it.
    They travel WITH the primary because they belong to the file, not to the path; see
    `apply_plan`.
    """

    path: Path
    project: str
    bucket: str
    source: str
    dest: str
    state: str
    backups: tuple = ()

    @property
    def target(self) -> Path:
        base = self.path.parent
        while base.name in TERMINAL_PARTITIONS:
            base = base.parent
        return (base / self.dest / self.path.name) if self.dest else (base / self.path.name)

    @property
    def backup_moves(self) -> list:
        """`(source, target)` for each backup, landing beside the relocated primary."""
        base = self.target.parent
        return [(b, base / b.name) for b in self.backups]


@dataclass(frozen=True)
class Unplaceable:
    """A file the pass refuses to move, and why. Reported, never relocated."""

    path: Path
    project: str
    bucket: str
    reason: str


@dataclass
class ReconcilePlan:
    moves: list = field(default_factory=list)
    unplaceable: list = field(default_factory=list)
    already_correct: int = 0
    collisions: list = field(default_factory=list)
    #: Files that declared themselves generated and were skipped before parsing. COUNTED, not
    #: listed: naming each one every sweep rebuilds the noise gh#176 removed, but dropping
    #: them silently would make "no mirror on this tree" and "mirror skipped" the same output -
    #: the absence-vs-success confusion this repository keeps re-learning.
    generated_skipped: int = 0

    @property
    def is_clean(self) -> bool:
        """True when nothing needs moving AND nothing was unreadable.

        Both halves matter. A tree with 170 unreadable files and no pending moves is not
        reconciled; it is unexamined.

        `generated_skipped` is deliberately NOT a term here. A skipped generated file is the
        expected steady state, not a pending decision; counting it as dirt would make a
        correctly-mirrored tree permanently unclean, which is gh#176 wearing a different hat.
        """
        return not self.moves and not self.unplaceable and not self.collisions


def _partition_for(state: Optional[str]) -> Optional[str]:
    """The subfolder a state belongs in, "" for active, or None when it cannot be placed."""
    norm = _norm_state(state)
    if norm is None:
        return None
    if norm in DONE_STATES:
        return "done"
    if norm in ARCHIVED_STATES:
        return "archived"
    return ""


#: How much of a file's head is read looking for the banner. The mirror emits it on the third
#: line; a couple of KB is far more than that, and bounding the read keeps this cheap on a
#: bucket that turns out to hold something large.
_BANNER_WINDOW = 4096


def _is_generated(path: Path) -> bool:
    """True when the file SAYS it is generated, in the words of the module that writes it.

    A SELF-DECLARATION, NOT A BUCKET LIST, and the difference is the whole point of gh#176.
    The two shapes the issue offered both restate a directory layout somewhere: an allow-list
    of authored buckets fails closed (a new authored bucket is silently unreconciled until
    someone enrols it), an exclusion of `bugs/` fails open (every future derived bucket is
    noise until someone excludes it). This module already refuses to restate a vocabulary it
    can borrow - see the header - and a bucket tuple here would have been the THIRD enumeration
    of the design layout in a tree that exports no constant for it at all.

    Deriving it from the artifact gets the properties of both forks and the cost of neither:
    a derived bucket nobody has invented yet needs no enrolment, and a MALFORMED AUTHORED FILE
    still has no banner, so it is still walked and still reported - which is the property the
    unplaceable list exists for.

    WHAT THIS DELIBERATELY DOES NOT DO: it does not exclude a directory. An authored file
    dropped into `bugs/` carries no banner, so it is walked, parsed and reported exactly as it
    would be anywhere else. The unit is the file, not the folder it happens to sit in.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(_BANNER_WINDOW)
    except OSError:
        # Unreadable is NOT "generated". Returning False hands the file to the parse below,
        # which reports the real error as an Unplaceable - so an I/O failure can never be the
        # thing that silences a file.
        return False
    return GENERATED_BANNER in head


def plan_reconcile(data_root, project: Optional[str] = None) -> ReconcilePlan:
    """Compute the moves that would make location agree with `current_state`.

    Pure and read-only: it opens files and returns a plan. Nothing is written here, so the
    plan can be printed, diffed or discarded without a dry-run flag having to be honoured
    by every code path.
    """
    root = Path(data_root) / "projects"
    plan = ReconcilePlan()
    if not root.is_dir():
        return plan

    projects = [root / project] if project else sorted(p for p in root.iterdir() if p.is_dir())
    for proj_dir in projects:
        design = proj_dir / "design"
        if not design.is_dir():
            continue
        for bucket_dir in sorted(d for d in design.iterdir() if d.is_dir()):
            for path in sorted(bucket_dir.rglob("*.md")):
                if _is_generated(path):
                    plan.generated_skipped += 1
                    continue
                here = path.parent.name if path.parent.name in TERMINAL_PARTITIONS else ""
                common = dict(project=proj_dir.name, bucket=bucket_dir.name)
                try:
                    state = parse_spec_frontmatter(path).current_state
                except Exception as exc:  # noqa: BLE001 - any parse failure is unplaceable
                    plan.unplaceable.append(
                        Unplaceable(path=path, reason=f"{type(exc).__name__}: {exc}", **common)
                    )
                    continue

                dest = _partition_for(state)
                if dest is None:
                    plan.unplaceable.append(
                        Unplaceable(path=path, reason="no readable current_state", **common)
                    )
                    continue
                if dest == here:
                    plan.already_correct += 1
                    continue

                move = Move(path=path, source=here, dest=dest,
                            state=_norm_state(state) or "",
                            backups=tuple(backup_group(path)), **common)
                # A destination that already exists is a COLLISION, not an overwrite. Two
                # files with one name is a fact somebody has to look at; silently clobbering
                # one of them destroys the evidence that it happened.
                if move.target.exists():
                    plan.collisions.append(
                        Unplaceable(path=path, reason=f"target exists: {move.target}", **common)
                    )
                    continue
                # A COLLIDING BACKUP REFUSES THE WHOLE MOVE, primary included. The
                # alternative - move what fits and leave the rest - is a half-moved group,
                # which is strictly worse than an unmoved one: it is the split state this
                # pass exists to stop producing, arrived at deliberately instead of by
                # accident. Detected here rather than in `apply_plan` so the refusal is
                # reported and exit-coded like every other, and so `apply_plan` still
                # re-derives no decisions of its own.
                taken = [dst for _, dst in move.backup_moves if dst.exists()]
                if taken:
                    plan.collisions.append(
                        Unplaceable(
                            path=path,
                            reason=f"backup target exists: {taken[0]}"
                                   + (f" (+{len(taken) - 1} more)" if len(taken) > 1 else ""),
                            **common,
                        )
                    )
                    continue
                plan.moves.append(move)

    # A PATH MAY NOT BE PLANNED TWICE, and the sweep may not discover that by dying.
    #
    # This loop globs `*.md`, so a backup whose own name ends in `.md` is BOTH a move
    # candidate in its own right - it carries a copy of the spec's frontmatter, so it parses
    # to the same destination - AND a member of `backup_group(primary)`. `apply_plan` then
    # renames it as part of the group and reaches its own Move with the source already gone,
    # aborting with `FileNotFoundError` PART WAY THROUGH a tree-wide move. A hard abort in
    # place of a reported refusal, leaving the half-reconciled tree this pass exists to
    # prevent - and produced BY the fix for it, since a plan without backups never carried a
    # path twice.
    #
    # Refusing the WHOLE move is the rule one level up, applied unchanged: a group that
    # cannot move cleanly does not move at all. Which role the file is playing - spec or
    # backup - is exactly the ambiguity a human should resolve, so it is reported rather
    # than guessed. Order-independent by construction: computed over the finished plan, not
    # as the loop goes, so it cannot be defeated by the sort putting a backup first.
    planned = {m.path for m in plan.moves}
    doubled = {b for m in plan.moves for b in m.backups if b in planned}
    if doubled:
        kept = []
        for m in plan.moves:
            offenders = sorted(n.name for n in ({m.path} | set(m.backups)) & doubled)
            if not offenders:
                kept.append(m)
                continue
            plan.collisions.append(
                Unplaceable(
                    path=m.path,
                    reason=f"backup planned twice: {offenders[0]} parses as a spec AND "
                           "resolves as a backup, so the same path is in two moves",
                    project=m.project,
                    bucket=m.bucket,
                )
            )
        plan.moves = kept
    return plan


def apply_plan(plan: ReconcilePlan) -> int:
    """Execute the moves. Returns the number of PRIMARIES moved.

    THE BACKUP GROUP MOVES WITH THE PRIMARY (gh#288). A `.bak` file belongs to the file, not
    to the path, so a move that relocated only the primary left the group SPLIT: `backups
    reap` then found a group whose primary was absent from that directory and refused it, on
    the rule that its backups are "the only surviving copy" - a sentence that is false for a
    moved spec, whose primary is one directory over and alive. Measured before the fix: 66
    refused groups, 50 of them manufactured this way, and climbing by one per CORRECT
    reconcile sweep, so the number rose precisely because this pass was working. Sixteen
    real refusals - the ones a human must decide - were buried in them.

    PRIMARY FIRST, BACKUPS AFTER, and the order is the recovery argument. Per-file `rename`
    is not one transaction and nothing here pretends otherwise. If a backup rename fails
    after the primary has moved, the view is already CORRECT (the spec is where its
    `current_state` says) and the stragglers are orphaned - which is exactly today's steady
    state, for one or two files instead of all of them, and it is visible in the reap report
    as a refusal. Backups-first would fail the other way: the primary left behind, the view
    wrong, and the next sweep permanently refused by its own already-moved backups. An
    `OSError` still propagates, as it always has, rather than being counted and continued -
    a tree half-reconciled by an unexplained error is not a state to keep sweeping through.
    """
    done = 0
    for move in plan.moves:
        move.target.parent.mkdir(parents=True, exist_ok=True)
        move.path.rename(move.target)
        done += 1
        for source, target in move.backup_moves:
            source.rename(target)
    return done


def render(plan: ReconcilePlan, apply: bool) -> str:
    lines = []
    prefix = "[APPLY]" if apply else "[DRY-RUN]"
    for m in plan.moves:
        # The backup count is on the line because the move is of a GROUP, and an operator
        # reading a sweep that relocated four files needs to see that it relocated eleven.
        carried = f" +{len(m.backups)} backup(s)" if m.backups else ""
        lines.append(
            f"{prefix} {m.project}/{m.bucket}: {m.path.name} "
            f"{m.source or '(active)'} -> {m.dest or '(active)'}  [{m.state}]{carried}"
        )
    for u in plan.collisions:
        lines.append(f"COLLISION {u.project}/{u.bucket}: {u.path.name} - {u.reason}")
    for u in plan.unplaceable:
        lines.append(f"SKIP {u.project}/{u.bucket}: {u.path.name} - {u.reason}")
    lines.append("")
    # The generated count is UNCONDITIONAL, printed even at zero. Emitted only when non-zero
    # it would still leave a reader unable to tell "this tree has no mirror" from "the mirror
    # was skipped"; at zero it says the first, at two it says the second.
    lines.append(
        f"{len(plan.moves)} to move, {plan.already_correct} already correct, "
        f"{len(plan.collisions)} collision(s), {plan.generated_skipped} generated (skipped), "
        f"{len(plan.unplaceable)} unplaceable"
    )
    if plan.unplaceable:
        lines.append(
            "An unplaceable file is left exactly where it is. It has no readable "
            "current_state, so it is neither active nor closed as far as this pass can "
            "tell, and guessing is what a materialized view must never do."
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    ap = argparse.ArgumentParser(prog="python -m agentflow.reconcile", description=__doc__)
    ap.add_argument("--project", help="one project; default is every project under data_root")
    ap.add_argument(
        "--apply", action="store_true", help="perform the moves (default: report only)"
    )
    args = ap.parse_args(argv)

    from .config import load_environment

    env = load_environment()
    plan = plan_reconcile(env.data_root, args.project)
    print(render(plan, args.apply))
    if args.apply and plan.moves:
        print(f"moved {apply_plan(plan)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
