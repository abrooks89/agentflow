"""The backup filename contract, and the reaper that bounds it.

WHY THIS MODULE EXISTS. `skills/dev-manager/SKILL.md` requires a backup before any
programmatic edit under `data_root`, and that rule is CORRECT and is not up for
negotiation: the project subtree is not git-managed, so a bad edit there is
unrecoverable. The rule was not designed - it was improvised mid-incident, by an agent
that had just lost an unbacked-up edit - and it shipped as one sentence of prose with no
naming convention, no owner and nothing that ever deletes one.

Prose is why it grew. Every firing that read that sentence invented its own suffix, so the
live tree carries scores of distinct suffix shapes - `-flip3`, `-f7`, `-refinement-N`,
`-reconcile-N`, plus hex-looking fragments with no recoverable meaning - each one a
separate firing making the same decision independently. That is the signature of a rule
living in prose instead of in code. So the format lives HERE, once. Restating a filename
pattern in two prose files is the same failure as restating it in a hundred firings, only
slower.

SCOPE OF THAT CLAIM, STATED SO IT IS NOT MISTAKEN FOR COVERAGE. The files this epic owns -
`skills/dev-manager/SKILL.md`, `skills/steward/SKILL.md` and `docs/DATA-LAYOUT.md` - point
here instead of restating the pattern, and `test_backups.py` holds them to it.
`skills/autodoc-update/SKILL.md` has since moved its manifest backup here too (gh#184): it
claims the name with `new` and hands it to `write-manifest.js`. The one hand-written backup
name left in the tree is autodoc-init's directory, named below under WHAT IT DOES NOT REACH.

THE FORMAT PUTS ITS TAIL AFTER THE PRIMARY'S EXTENSION, ON PURPOSE. The disk epic-survey
globs `*.md` and `reconcile` uses `rglob("*.md")`; not one backup on the live tree ends in
`.md` today, so none of them pollute the queue. A format of the shape
`foo.bak.<stamp>.md` would put every backup ever taken into the survey as a phantom epic.
The tail goes last.

DRY-RUN IS THE DEFAULT SHAPE OF THE FUNCTION, NOT A FLAG. `plan_reap` is pure and
read-only: it returns a plan that can be printed, diffed or discarded without every code
path having to honour a boolean. `apply_plan` is a separate function that executes an
already-computed plan. The reaper deletes files under a tree git cannot contradict, so the
only safe arrangement is one where doing nothing requires no correctness on anyone's part.

WHAT THIS MODULE REFUSES TO DO. It never deletes a backup whose primary is missing or
zero-length. In that state the backup is USUALLY the only surviving copy of the file, and a
retention rule that eats the last copy is not retention, it is the incident that started
all this. A group in that condition is a NO-OP and is named in the report as a refusal -
a third outcome, modelled on `reconcile.Unplaceable`, not an error and not a default.

USUALLY, NOT ALWAYS, AND THE EXCEPTION IS DATED (gh#288). Until `reconcile.apply_plan` was
taught to move a spec's backup group with the spec, every `reconcile --apply` that relocated
a spec left its backups behind and manufactured one permanent refusal here whose primary was
alive one directory over. Those groups were 76% of this list when the fix landed. The fix
stops NEW ones; it cannot reunite the ones already split, because moving live backup files is
a data mutation this module has no business making on its own. Reuniting the pre-fix orphans
is a steward action against the live tree, not a rule.

WHAT THE REPORT CAN NOW SAY, AND WHERE THAT KNOWLEDGE LIVES (E-0708). This module used to
state that it "cannot tell which, by design", and that sentence is retired rather than moved:
it is no longer true of the file. `plan_reap` still cannot - it takes an OPAQUE callable and
records whatever it answers - but the CLI in this same module builds that callable, and doing
so means reading frontmatter and knowing that `slug:` is a document's identity. Calling that a
separation because a function boundary sits between them would be the kind of claim this
module exists to refuse. So, plainly: the RULE is ignorant and the COMMAND is not, the
knowledge is 70 lines below this paragraph, and if it ever needs to be ignorant again the
resolver moves to its own module. What it buys: a refusal whose slug resolves to exactly one
live file is captioned MOVED, and this tree's human-facing call-list went 68 to 10.

WHAT IT DOES NOT REACH, NAMED SO IT IS NOT MISTAKEN FOR COVERAGE. `skills/autodoc-init/SKILL.md`
mandates `mv "$AUTODOC" "${AUTODOC}.pre-store.bak"` - a marker-last shape whose subject is a
DIRECTORY. `iter_backups` filters on `is_file()`, so the reaper never sees it: it is safe from
this module, and equally it is not bounded by it or by the format above. Leave it that way.
Extending the reaper to directories is new destructive reach - a wrong removal there takes a
whole tree, not a file - and the destructive half of this module is human-gated on purpose.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import re
import shutil
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Iterable, Optional

#: The marker every backup carries, legacy and canonical alike. It is the substring every
#: existing tool and every human already greps for, so it does not change.
BACKUP_MARKER = ".bak"

#: UTC, basic ISO-8601, fixed width. Basic form (no colons) because a colon is illegal in a
#: Windows filename and this fleet runs on Windows; fixed width because that is what makes a
#: lexical sort a chronological sort.
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: How many backups of one file survive a reap, newest first.
#:
#: Module-level rather than a config field, following `atomic_io`'s precedent: so tests can
#: pin it and a future tuner has a single place to look. Making it an `environment.*` field would
#: cost a row in the FROZEN schema, a validation rule, a dataclass field and a `doctor` row -
#: because the schema does unknown-key rejection, a field cannot be half-added - to let a
#: number vary across the one machine that has a `data_root`. `--keep` covers the
#: "sweep harder this once" case without a schema change, and promoting this to a config
#: field later is additive.
#:
#: The default is small because the first sweep of an ungoverned tree is a one-way door. It
#: covers the recorded need: the copy from immediately before the bad edit, plus enough depth
#: for a corruption that predates the last firing and that nobody noticed.
KEEP_DEFAULT = 3

#: THE canonical name: `<primary-with-its-extension>.bak.<stamp>.<firing>.<seq>`.
#:
#: `(?P<primary>.+)` is GREEDY on purpose. The tail is rigid and anchored, so a greedy match
#: backtracks to the last valid split - which is what makes backing up a file that itself
#: contains `.bak` in its name (a legacy backup, during a sweep) parse correctly.
BACKUP_RE = re.compile(
    r"^(?P<primary>.+)\.bak"
    r"\.(?P<stamp>\d{8}T\d{6}Z)"
    r"\.(?P<firing>[a-z0-9]+(?:-[a-z0-9]+)*)"
    r"\.(?P<seq>\d{2})$"
)

#: The permissive pattern that recognises EVERY backup on the tree, canonical or legacy.
#:
#: Two patterns rather than one widened pattern, and the distinction is worth stating:
#: `ANY_BACKUP_RE` is a GROUPING KEY, `BACKUP_RE` is a CONTRACT. If the reaper used the
#: permissive one for both, a malformed canonical name - a firing that wrote a stamp with
#: dashes in it - would be silently absorbed as "legacy" instead of failing visibly. Legacy
#: is a closed set that only shrinks; new malformation has to stay loud.
#:
#: `.+?` is non-greedy, which is load-bearing: it takes the text up to the FIRST `.bak`. That
#: is right for a legacy name, whose one marker is also its first, and WRONG for a backup OF a
#: backup, where the first marker belongs to the primary and the last one to this file. So this
#: pattern is the FALLBACK grouping key, not the primary one: `plan_reap` asks `BACKUP_RE`
#: first and only falls back here when the name is not canonical. Reversing that order files a
#: canonical backup of a legacy backup under the legacy backup's OWN primary, and the sweep
#: then deletes the file the backup exists to protect.
#:
#: After the marker it accepts end-of-name, a digit run (`.bak2` - a family that separates
#: with nothing at all), or `.`/`-` and any tail. It deliberately does NOT accept a letter,
#: so an ordinary file whose name merely contains the sequence (`recipe.baked.md`) is not
#: mistaken for a backup of `recipe` and then reaped. That boundary was derived by running
#: this pattern against every `.bak`-containing filename on the live tree and reconciling the
#: two counts until they matched - the `.bak2` family - which separates with nothing at all -
#: was found exactly that way.
ANY_BACKUP_RE = re.compile(r"^(?P<primary>.+?)\.bak(?:\d+|[-.].*)?$")

#: `<actor>-f<N>`, and the `-f<N>` half is MANDATORY.
#:
#: `BACKUP_RE`'s firing field is only "lowercase alphanumerics with single hyphens", which also
#: accepts a bare `steward`. That matters because the whole collision argument in `backup_name`
#: rests on two firings differing in this field: with the firing number optional, half of that
#: argument lived in caller prose, which is exactly the arrangement this module exists to end.
FIRING_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-f\d+$")


@dataclass(frozen=True)
class BackupName:
    """The four fields of a canonical backup filename, parsed back out."""

    primary: str
    stamp: str
    firing: str
    seq: int

    @property
    def taken_at(self) -> datetime:
        return datetime.strptime(self.stamp, STAMP_FORMAT).replace(tzinfo=timezone.utc)


def format_stamp(when: Optional[datetime] = None) -> str:
    """Render `when` (default: now, UTC) as the fixed-width UTC stamp the format uses.

    AN AWARE DATETIME IS REQUIRED; a naive one raises. This used to `.replace(tzinfo=utc)`,
    which RELABELS rather than converts - a caller passing `datetime.now()` (naive, local)
    got a stamp wrong by the local offset, in the one field that is the name's only
    human-readable provenance and that nothing downstream can cross-check. Converting
    silently would be the same guess with better manners: a naive datetime does not say
    which clock it came from, so this pass does not get to decide.
    """
    moment = datetime.now(timezone.utc) if when is None else when
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"format_stamp needs a timezone-aware datetime, got naive {moment!r}; pass "
            f"datetime.now(timezone.utc), or attach the tzinfo of the clock it came from"
        )
    return moment.astimezone(timezone.utc).strftime(STAMP_FORMAT)


def backup_name(
    primary: str, firing: str, when: Optional[datetime] = None, seq: int = 1
) -> str:
    """The canonical backup filename for `primary`, taken by `firing`.

    `primary` is the COMPLETE original filename, extension included - nothing before the
    marker changes, so the primary is recoverable by truncation. `firing` is
    `<actor>-f<N>` (e.g. `dev-manager-f592`): it carries who and which firing in one token,
    which is the field every improvised legacy suffix was failing to express.

    Collision: two firings differ in `firing` even at the same second, because each lane is
    a single writer and `(actor, firing-number)` is unique fleet-wide. That is why the
    `-f<N>` half is ENFORCED here and not merely conventional - it is the half that carries
    the uniqueness. `seq` exists only for one firing backing the same file up twice within
    one second; without it the format would satisfy the letter of "cannot collide" and still
    lose data.
    """
    if not 1 <= seq <= 99:
        raise ValueError(f"seq must be 1..99, got {seq!r}")
    if FIRING_RE.match(firing) is None:
        raise ValueError(
            f"firing must be <actor>-f<N> (e.g. 'dev-manager-f592'), got {firing!r}: "
            f"lowercase alphanumerics separated by single hyphens, ending in `-f` and the "
            f"firing number"
        )
    name = f"{primary}{BACKUP_MARKER}.{format_stamp(when)}.{firing}.{seq:02d}"
    if BACKUP_RE.match(name) is None:
        raise ValueError(
            f"{name!r} is not a valid backup name - check `primary` ({primary!r}), which must "
            f"be the complete original filename"
        )
    return name


def parse_backup_name(name: str) -> Optional[BackupName]:
    """Parse a CANONICAL backup filename, or None if it is not one.

    None means "not canonical" - it does not mean "not a backup". Use `primary_for` for the
    grouping question, which legacy names also answer.
    """
    m = BACKUP_RE.match(name)
    if m is None:
        return None
    return BackupName(
        primary=m.group("primary"),
        stamp=m.group("stamp"),
        firing=m.group("firing"),
        seq=int(m.group("seq")),
    )


def primary_for(name: str) -> Optional[str]:
    """The filename this backup is a copy of, for any backup shape ever written here.

    Returns None when `name` is not a backup at all. When the parse is wrong the failure
    points the safe way: a name like `foo.md.corrupted.bak` resolves to a primary that does
    not exist, which lands the whole group in the refusal class and means the reaper never
    touches it. For an unrecoverable operation that is the correct direction to be wrong in.
    """
    m = ANY_BACKUP_RE.match(name)
    return m.group("primary") if m else None


def group_primary(path: str | Path) -> Optional[Path]:
    """The PATH of the primary `path` is a backup of, or None if it is not a backup.

    THE ONE DEFINITION OF THE GROUPING RULE, extracted so `plan_reap` and every other
    caller ask the same question of the same parser. The rule has two halves and the ORDER
    between them is the load-bearing part: the CONTRACT parser decides, and the permissive
    one is the fallback for names it does not recognise. Reversing that order files a
    canonical backup of a legacy backup under the legacy backup's own primary, and the
    sweep then deletes the very file that backup exists to protect.

    A second copy of those five lines in a second module is how the two would drift, and a
    drift here is not a cosmetic one - it decides which file gets deleted.

    The primary is always a SIBLING: a backup names its primary, never a path to it, so the
    directory comes from `path` and the name from the parse. That is also why groups are
    per-directory - one file's backups can never influence another's across a move.
    """
    p = Path(path)
    canonical = parse_backup_name(p.name)
    if canonical is not None:
        return p.with_name(canonical.primary)
    stem = primary_for(p.name)
    return p.with_name(stem) if stem is not None else None


def backup_group(primary: str | Path) -> list[Path]:
    """Every backup file in `primary`'s directory that belongs to it, TRANSITIVELY.

    Transitive because a backup can itself have backups: `a.md.bak-flip3` groups to `a.md`,
    while its own canonical backup groups to `a.md.bak-flip3`. A caller that took only the
    first level would move `a.md` and `a.md.bak-flip3` and leave the second file behind -
    the same split one level down, which is exactly the defect this function exists to let
    a caller avoid.

    Only regular files, and only siblings: a group is `(directory, primary name)`, so this
    never reaches into a subdirectory or up out of one. Returns [] for a primary whose
    directory does not exist, rather than raising - a caller asking "what travels with this
    file" about a file that is not there wants an empty answer, not an exception.
    """
    p = Path(primary)
    if not p.parent.is_dir():
        return []
    children: dict[Path, list[Path]] = {}
    for candidate in p.parent.iterdir():
        if not candidate.is_file():
            continue
        owner = group_primary(candidate)
        if owner is not None:
            children.setdefault(owner, []).append(candidate)

    found: list[Path] = []
    seen = {p}
    queue = [p]
    while queue:
        for child in children.get(queue.pop(), ()):
            if child not in seen:
                seen.add(child)
                found.append(child)
                queue.append(child)
    return sorted(found)


def next_backup_path(
    path: str | Path, firing: str, when: Optional[datetime] = None
) -> Path:
    """CLAIM and return the path to write the next backup of `path` to.

    THIS CREATES THE FILE - empty, zero bytes - and the side effect is the guarantee. The
    previous version tested `candidate.exists()` and returned a name the caller wrote some
    time later, so two writers racing on the same file both saw "free" and both got the SAME
    path: the second write overwrote the first and one backup vanished with nothing logged.
    `seq` cannot close that window, because `seq` only distinguishes names once one of them
    exists. `O_CREAT | O_EXCL` is the one operation the filesystem serialises, so the winner
    owns the name and the loser moves to the next `seq`.

    WHAT IS NOW GUARANTEED: the returned path exists, is empty, and no other caller of this
    function will be handed it. The caller must write the backup's content into it.
    WHAT IS NOT: nothing here copies `path` - an abandoned claim leaves a zero-length backup,
    which the reaper removes without letting it hold a retention slot. A caller that wants a
    backup rather than a name calls `take_backup`, which claims, copies and verifies (gh#712).
    """
    src = Path(path)
    stamp_at = datetime.now(timezone.utc) if when is None else when
    for seq in range(1, 100):
        candidate = src.with_name(backup_name(src.name, firing, stamp_at, seq))
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise RuntimeError(
        f"{firing} has already taken 99 backups of {src.name} in one second; that is a "
        f"runaway loop, not a retention problem"
    )


class BackupError(Exception):
    """A backup that could not be taken, or was taken and did not verify. Always loud."""


def take_backup(path: str | Path, firing: str, when: Optional[datetime] = None) -> Path:
    """Claim a backup name for `path`, copy `path` into it, verify the copy, return it.

    WHY THIS EXISTS (gh#712). `next_backup_path` hands back an EMPTY claimed file, and the rule
    that the caller then copies into it lived only in prose. A seat that followed the prose
    literally held a zero-byte "backup" and believed the spec was safe. A rule in prose is the
    thing that failed, so the copy and the check live here.

    Refuses BEFORE claiming when the source is missing, not a regular file, or empty: there is
    nothing to back up, and claiming first would leave exactly the empty file this function
    exists to stop producing. A copy that does not verify - short, empty, or different bytes -
    empties and removes its own claim and raises, so a failure never leaves a plausible-looking
    backup. EVERY failure is a `BackupError`, the cause chained, so a caller catches one type.

    `shutil.copyfile`, not `copy2`: the reaper orders a group by the BACKUP's mtime, so a copy
    that inherited the primary's older mtime would sort as older than backups taken before it.
    """
    src = Path(path)
    if not src.is_file():
        raise BackupError(f"cannot back up {src}: it is not an existing regular file")
    size = src.stat().st_size
    if size == 0:
        raise BackupError(f"cannot back up {src}: it is empty, so a backup of it would be too")
    try:
        claim = next_backup_path(src, firing, when)
    except (ValueError, OSError, RuntimeError) as exc:
        # One exception type for Python callers: a malformed firing (ValueError), an
        # unclaimable directory (OSError) and a runaway claim loop (RuntimeError) are all
        # "no backup was taken", and nothing was claimed, so there is nothing to clean up.
        raise BackupError(f"cannot claim a backup name for {src}: {exc}") from exc
    try:
        shutil.copyfile(src, claim)
        copied = claim.stat().st_size
        if copied == 0 or copied != size or not filecmp.cmp(src, claim, shallow=False):
            raise BackupError(
                f"backup of {src} did not verify: source {size} bytes, copy {copied} bytes "
                f"at {claim} (did the file change during the copy?)"
            )
    except (OSError, BackupError) as exc:
        failure = exc if isinstance(exc, BackupError) else BackupError(
            f"copying {src} to {claim} failed: {exc}"
        )
        leftover = _discard_failed_claim(claim)
        if leftover:
            raise BackupError(f"{failure}; ALSO {leftover}") from exc
        if failure is exc:
            raise
        raise failure from exc
    return claim


def _discard_failed_claim(claim: Path) -> Optional[str]:
    """Remove a claim whose copy failed; return what was left behind, or None if nothing was.

    TRUNCATE FIRST, THEN UNLINK. An unlink can fail while the file is merely held open (an
    antivirus scanner on Windows is enough), and a short copy left under a canonical name is a
    NON-EMPTY member the reaper would give a keep slot - a corrupt file ranked as a real backup.
    Emptied first, the worst leftover is a zero-byte file the reaper already discounts.
    """
    emptied = True
    try:
        os.truncate(claim, 0)
    except FileNotFoundError:
        return None
    except OSError as exc:
        emptied = False
        truncate_error = exc
    try:
        claim.unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        if emptied:
            return (
                f"could not remove the failed claim {claim} ({exc}); it is left EMPTY, which "
                f"the reaper discounts"
            )
        return (
            f"could neither empty ({truncate_error}) nor remove ({exc}) the failed claim "
            f"{claim}; it may hold a partial copy - delete it by hand, it is not a valid backup"
        )
    return None


@dataclass(frozen=True)
class Removal:
    """One backup the reap would delete."""

    path: Path
    primary: Path
    size: int
    canonical: bool


@dataclass(frozen=True)
class Refusal:
    """A group the reap will not touch, and why. Reported, never deleted.

    `live_elsewhere` is the ONE thing a refusal could never say before: the group's primary is
    gone from this path, but a live file somewhere else in the tree carries the same spec slug,
    so these backups are stale copies of a file that still exists rather than the last copies of
    a file that does not. It stays `None` unless a resolver was supplied AND answered
    unambiguously - see `plan_reap`. A refusal with it set is still a REFUSAL: nothing is
    deleted, nothing leaves `ReapPlan.refusals`, and the exit code does not move. The field
    changes what the report can SAY, never what the reap does.
    """

    primary: Path
    reason: str
    count: int
    live_elsewhere: Path | None = None


@dataclass
class ReapPlan:
    keep: int = KEEP_DEFAULT
    removals: list = field(default_factory=list)
    refusals: list = field(default_factory=list)
    #: Groups whose resolver raised. A resolver that fails on EVERY group produces a
    #: report byte-identical to one with no resolver at all, so the count is carried
    #: and rendered: silence about a broken helper is the failure this fleet keeps
    #: re-finding. It is deliberately NOT part of `is_clean` - a missing caption is a
    #: degraded report, not an unbounded tree.
    resolver_failures: int = 0
    kept: int = 0
    groups: int = 0

    @property
    def bytes_removed(self) -> int:
        return sum(r.size for r in self.removals)

    @property
    def refused_backups(self) -> int:
        """Backups left untouched because their GROUP was refused.

        Counted separately from `kept` and never folded into it. `kept` is the retention rule
        doing its job; a refused backup is the retention rule declining to run at all, on a
        group that needs a human. Adding the two produced a summary that read as healthy
        retention over exactly the files nothing had decided anything about.
        """
        return sum(r.count for r in self.refusals)

    @property
    def is_clean(self) -> bool:
        """True when no group exceeds the keep-count AND nothing was refused.

        Both halves matter: a tree with refusals and no pending removals is not bounded,
        it is unexamined - those groups hold the only copy of something.
        """
        return not self.removals and not self.refusals


def iter_backups(root: str | Path) -> Iterable[Path]:
    """Every backup file under `root`, in any shape."""
    base = Path(root)
    if not base.is_dir():
        return []
    return (
        p
        for p in sorted(base.rglob("*"))
        if p.is_file() and primary_for(p.name) is not None
    )


def _sort_key(path: Path) -> tuple:
    """Order a group newest-last by `(mtime, name)`.

    ONE key for a mixed group, deliberately - mtime for canonical and stamp for legacy would
    be a two-key merge whose correctness depends on the two agreeing. Legacy names have no
    parseable time in them anyway: a digit run means a counter in one family, a date in
    another and a hex fragment in a third, so any heuristic there is a hundred guesses
    wearing one function's name. The `name` tiebreak means a filesystem with coarse
    timestamps degrades into "deterministic" rather than "arbitrary".
    """
    return (path.stat().st_mtime, path.name)


def plan_reap(
    root: str | Path,
    keep: int = KEEP_DEFAULT,
    resolve_live: Callable[[Path, list[Path]], Path | None] | None = None,
) -> ReapPlan:
    """Compute which backups a reap would delete. Pure and read-only - nothing is written.

    Groups by `(directory, primary filename)`, so one file's backups can never influence
    another's even when they share a directory.

    `resolve_live` is an OPAQUE callable and the only way spec knowledge reaches this function.
    It is handed `(primary, members)` for a group whose primary is ABSENT, and answers either a
    live path that is the same document under a new name, or `None`. This module never inspects
    a file's contents and still does not know the spec layout; it calls what the CLI hands it
    and records the answer. Omit it and every behaviour here is byte-identical to before.

    It is asked ONLY about `primary is absent`. A directory, a broken symlink and a
    zero-length file are all cases where something IS at that path - they are not renames, and
    asking would invite a resolver to explain away a truncation.

    The answer never changes what is deleted. A resolved group stays in `refusals`, keeps its
    reason, and keeps every backup: `live_elsewhere` is a caption, not a verdict.
    """
    if keep < 1:
        raise ValueError(
            f"keep must be at least 1, got {keep!r}; a keep of 0 is not retention, it is "
            f"deletion with extra steps"
        )
    plan = ReapPlan(keep=keep)
    groups: dict[Path, list[Path]] = {}
    for path in iter_backups(root):
        # `group_primary` holds the contract-parser-first rule and the reason for it. It is
        # called rather than restated because `reconcile` asks the same question when it
        # moves a primary between partitions, and two copies of that rule would disagree
        # about which file a backup protects.
        primary = group_primary(path)
        if primary is None:
            # Unreachable: `iter_backups` yields only names `primary_for` accepted, and
            # `group_primary` falls back to the same parser. Loud rather than skipped -
            # silently dropping a backup here would under-count a group and let the
            # retention rule delete one more file than it was allowed to.
            raise RuntimeError(
                f"{path} was yielded as a backup but has no resolvable primary; "
                f"iter_backups and group_primary disagree, which is a bug in this module"
            )
        groups.setdefault(primary, []).append(path)

    for primary in sorted(groups):
        members = groups[primary]
        plan.groups += 1

        # THE INVARIANT. A missing or empty primary means these backups are the only
        # surviving copy of the file. The group is a NO-OP - not "keep one", not "keep the
        # newest": untouched, and named in the report so a human decides.
        #
        # `is_file()` is the SINGLE first check, deliberately: `exists()` answers True for a
        # DIRECTORY, so a primary that is a directory read as live and its backups became
        # reapable. That is masked on Windows (where a name cannot be both) and real on POSIX.
        # `is_file()` is False for absent, directory and broken symlink alike, and the reason
        # string then says which - a human triaging a refusal needs the difference.
        if not primary.is_file():
            if primary.is_dir():
                reason = "primary is a directory"
            elif primary.is_symlink():
                reason = "primary is a broken symlink"
            elif primary.exists():
                reason = "primary is not a regular file"
            else:
                reason = "primary is absent"
            live = None
            if resolve_live is not None and reason == "primary is absent":
                try:
                    live = resolve_live(primary, members)
                except Exception:  # noqa: BLE001 - width is the point, see below
                    # A resolver that raises must not take the sweep down with it. Retention
                    # is the load-bearing behaviour here and it does not need this answer;
                    # losing the caption is the correct degradation.
                    #
                    # THE WIDTH IS DELIBERATE, and it was argued. A narrower tuple was proposed
                    # on the grounds that `Exception` swallows MemoryError and RecursionError.
                    # It does - and the alternative is worse: `resolve_live` is arbitrary
                    # caller-supplied code, so any exception type it invents would take down a
                    # retention sweep that never needed the answer. KeyboardInterrupt and
                    # SystemExit are BaseException and still propagate, which is the only
                    # escape that matters. The real complaint behind the narrowing was that a
                    # systematically-failing resolver was INVISIBLE; that is fixed by counting,
                    # not by crashing.
                    live = None
                    plan.resolver_failures += 1
            plan.refusals.append(
                Refusal(
                    primary=primary,
                    reason=reason,
                    count=len(members),
                    live_elsewhere=live,
                )
            )
            continue
        if primary.stat().st_size == 0:
            plan.refusals.append(
                Refusal(
                    primary=primary, reason="primary is zero-length", count=len(members)
                )
            )
            continue

        # ZERO-BYTE MEMBERS DO NOT HOLD A RETENTION SLOT while a real backup exists (gh#712).
        # An empty member is an abandoned `next_backup_path` claim, not a copy of anything, and
        # it is usually the NEWEST file in its group - so ranking by mtime alone let a run of
        # empty claims push the one real backup out of the keep window and delete it. The keep
        # window is therefore filled from the non-empty members only, which also makes the
        # newest non-empty backup unremovable (`keep` is at least 1). With no non-empty member
        # at all there is nothing of value to protect and the group ranks as before.
        ordered = sorted(members, key=_sort_key, reverse=True)
        sized = {path: path.stat().st_size for path in ordered}
        real = [path for path in ordered if sized[path] > 0]
        if real:
            survivors = set(real[:keep])
        else:
            survivors = set(ordered[:keep])
        plan.kept += len(survivors)
        for path in ordered:
            if path in survivors:
                continue
            plan.removals.append(
                Removal(
                    path=path,
                    primary=primary,
                    size=sized[path],
                    canonical=parse_backup_name(path.name) is not None,
                )
            )
    # THE OTHER HALF OF UNIQUENESS. The resolver enforces "one live file per slug"; it
    # cannot see that TWO refused groups resolved to the SAME live file, because it is handed
    # one group at a time. On this tree three live files are each named by two different
    # absent primaries, and at most one of each pair can be the relocation the report asserts.
    # Captioning both would tell a human two different last copies are disposable on the
    # strength of one surviving file. Computed here, over the finished list, so no ordering of
    # groups can defeat it.
    claims: dict = {}
    for ref in plan.refusals:
        if ref.live_elsewhere is not None:
            claims[ref.live_elsewhere] = claims.get(ref.live_elsewhere, 0) + 1
    contested = {live for live, n in claims.items() if n > 1}
    if contested:
        plan.refusals = [
            replace(ref, live_elsewhere=None)
            if ref.live_elsewhere in contested
            else ref
            for ref in plan.refusals
        ]
    return plan


@dataclass(frozen=True)
class ApplyResult:
    """What `apply_plan` actually DID, as distinct from what the plan predicted.

    A count would not do here (`reconcile.apply_plan` returns one; this is the case that
    differs). A sweep of 118 files that dies on file 50 leaves the operator a report claiming
    118 removals, a traceback, and no record of which 50 happened - against a tree git cannot
    reconstruct. So the outcome is a value, and the caller renders both halves.
    """

    removed: list = field(default_factory=list)
    failures: list = field(default_factory=list)  # (path, message) per file that did not go

    @property
    def ok(self) -> bool:
        return not self.failures


def apply_plan(plan: ReapPlan) -> ApplyResult:
    """Delete the planned backups. Returns what was and was not removed.

    Takes an already-computed plan and does nothing else - it re-derives no decisions, so
    what a caller printed is exactly what a caller then executed.

    A per-file `OSError` (a permission denial, a file already gone, a lock) is RECORDED and
    the sweep continues; it is neither swallowed nor allowed to abort the rest. The catch is
    `OSError` and nothing wider: anything else here is a bug in this module, and a bug must
    still leave the process loudly.
    """
    result = ApplyResult()
    for item in plan.removals:
        try:
            item.path.unlink()
        except OSError as exc:
            result.failures.append((item.path, f"{type(exc).__name__}: {exc}"))
            continue
        result.removed.append(item.path)
    return result


def _dominant_dirs(plan: ReapPlan, limit: int = 10) -> list[tuple[Path, int, int]]:
    tally: dict[Path, list[int]] = {}
    for item in plan.removals:
        row = tally.setdefault(item.path.parent, [0, 0])
        row[0] += 1
        row[1] += item.size
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1][0], str(kv[0])))
    return [(d, c, b) for d, (c, b) in ranked[:limit]]


def render(
    plan: ReapPlan,
    apply: bool = False,
    root: Optional[Path] = None,
    result: Optional[ApplyResult] = None,
) -> str:
    """The report. `apply=False` (the default) prefixes every line `[DRY-RUN]`.

    Pass `result` (from `apply_plan`) to report what HAPPENED rather than what was planned.
    Without it an `[APPLY]` report is a prediction printed before the fact - which is what it
    was, and which is why a mid-sweep failure used to leave a report claiming removals that
    never occurred.
    """
    prefix = "[APPLY]" if apply else "[DRY-RUN]"
    failed = {path: message for path, message in result.failures} if result else {}
    lines: list[str] = []

    def rel(p: Path) -> str:
        if root is None:
            return str(p)
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)

    for item in sorted(plan.removals, key=lambda r: str(r.path)):
        shape = "canonical" if item.canonical else "legacy"
        if item.path in failed:
            lines.append(
                f"[FAILED] NOT removed {rel(item.path)}  [{shape}, {item.size} bytes] - "
                f"{failed[item.path]}"
            )
        else:
            verb = "removed" if result else "remove"
            lines.append(f"{prefix} {verb} {rel(item.path)}  [{shape}, {item.size} bytes]")
    for ref in sorted(plan.refusals, key=lambda r: str(r.primary)):
        if ref.live_elsewhere is not None:
            lines.append(
                f"REFUSE (moved) {rel(ref.primary)} - {ref.reason}, but a live file "
                f"carries this slug: {rel(ref.live_elsewhere)}; "
                f"{ref.count} backup(s) left untouched"
            )
        else:
            lines.append(
                f"REFUSE {rel(ref.primary)} - {ref.reason}; {ref.count} backup(s) left untouched"
            )

    lines.append("")
    lines.append(
        f"keep={plan.keep}: {len(plan.removals)} to remove "
        f"({plan.bytes_removed} bytes) across {plan.groups} group(s); "
        f"{plan.kept} kept by the retention rule; "
        f"{len(plan.refusals)} group(s) refused ({plan.refused_backups} backup(s) untouched, "
        f"NOT counted as kept)"
    )
    if plan.resolver_failures:
        lines.append(
            f"  WARNING: the slug resolver failed on {plan.resolver_failures} "
            f"group(s); captions below are incomplete, not absent-by-answer."
        )
    moved = [r for r in plan.refusals if r.live_elsewhere is not None]
    if moved:
        lines.append(
            f"  of those refusals, {len(moved)} are MOVED: a live file carries the same "
            f"slug, so they are stale copies rather than last copies. "
            f"{len(plan.refusals) - len(moved)} remain unexplained."
        )
    if result is not None:
        lines.append(
            f"applied: {len(result.removed)} removed, {len(result.failures)} FAILED to remove"
        )
    if plan.removals:
        lines.append("")
        lines.append("Directories dominating the removals:")
        for directory, count, size in _dominant_dirs(plan):
            lines.append(f"  {count:>5} files  {size:>12} bytes  {rel(directory)}")
    if plan.refusals:
        lines.append("")
        lines.append(
            "A REFUSED group is one whose primary is missing or zero-length. That usually "
            "makes its backups the only surviving copy of that file. A refusal marked "
            "MOVED is the exception the CLI can now name: a live file elsewhere carries "
            "the same spec slug, so the primary was relocated by a pre-gh#288 reconcile "
            "or renamed, and these backups are stale rather than last. The retention "
            "rule itself still does not know the spec layout - the CLI hands it a "
            "resolver - and an UNMARKED refusal is not proof of a last copy, only "
            "absence of a unique live slug. Either way every group is left exactly as "
            "it is: deciding is a human's call, not a retention rule's."
        )
    lines.append("")
    lines.append(
        "Count-based retention bounds the depth PER FILE, not the size of the tree: the "
        "steady state is (number of primaries x keep) and it grows as files are written. "
        "Bounding the total needs an age rule or an archive-on-close rule, which this is "
        "not."
    )
    return "\n".join(lines)


NEWLINE = chr(10)

_FRONTMATTER_SLUG_RE = re.compile(
    "^slug:[ \t]*(?P<value>[^\n]*)$", re.MULTILINE
)
#: What a slug may contain. Anything else is not a slug we will caption a human with.
_SLUG_SHAPE_RE = re.compile("^[A-Za-z0-9._-]+$")


def _explicit_slug(path: Path) -> str | None:
    """The `slug:` written INSIDE a file's frontmatter block, or None.

    Strict on purpose, and the strictness is measured rather than stylistic. Across the 115
    backups in this tree's refusal list, eight carry a `slug:`-shaped line in the BODY: two are
    a TypeScript field (`slug: string;`), one is a documentation example (`slug: foo`), and the
    rest are `**Slug:**` prose that still advertises a name the frontmatter has already moved
    on from. A whole-file grep matches every one of them, and the stale-prose class is
    positively misleading - it is how a document that was renamed keeps insisting on its old
    identity. So: only the leading `---` block is read, and only an explicit key counts.

    No filename fallback here, deliberately. A backup's filename is the OLD name by
    construction; deriving identity from it would answer the question with its own premise.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rows = text.split(NEWLINE)
    if not rows or rows[0].strip() != "---":
        return None
    block = []
    for row in rows[1:]:
        if row.strip() == "---":
            break
        block.append(row)
    else:
        return None  # frontmatter never closed; not a document we can read identity from
    match = _FRONTMATTER_SLUG_RE.search(NEWLINE.join(block))
    if match is None:
        return None
    raw = match.group("value").strip()
    if raw[:1] in (chr(34), chr(39)) and raw[-1:] == raw[:1] and len(raw) > 1:
        raw = raw[1:-1].strip()          # a MATCHED pair only; an unterminated quote is garbage
    else:
        raw = raw.split(" #", 1)[0].strip()   # an unquoted trailing YAML comment is not identity
    return raw if _SLUG_SHAPE_RE.match(raw) else None


def _live_slug_index(root: Path) -> dict:
    """Map every slug held by a LIVE (non-backup) file under `root` to the paths holding it.

    A list, not a single path, because slug is NOT a unique key in this data root: 83 slugs are
    held by more than one live file, and the spec and plan pillars deliberately share one. A
    resolver that assumed uniqueness would pick an arbitrary winner.
    """
    index: dict = {}
    for path in root.rglob("*"):
        if not path.is_file() or ANY_BACKUP_RE.search(path.name):
            continue
        if path.suffix.lower() != ".md":
            continue
        slug = _explicit_slug(path)
        if slug:
            index.setdefault(slug, []).append(path)
    return index


def _make_live_resolver(root: Path):
    """Build the `resolve_live` callable the CLI hands to `plan_reap`.

    THE RULE IS UNIQUENESS, AND IT IS THE WHOLE SAFETY ARGUMENT. A group resolves only when
    exactly one live file carries its slug. Five of this tree's refusals resolve to two or
    three live files, and three slugs are claimed by two different refused groups each -
    captioning either kind would assert a lineage nobody verified, on a report a human reads to
    decide what to delete. Ambiguous, absent and unparseable all mean the same thing here: no
    caption, and the group reads exactly as it did before.
    """
    index = _live_slug_index(root)

    def resolve(primary: Path, members: list) -> Path | None:
        slugs = {s for s in (_explicit_slug(m) for m in members) if s}
        if len(slugs) != 1:
            return None
        # No `p != primary` filter: `primary` is absent by construction here, and the
        # index holds only files that exist, so it could never be in there.
        matches = index.get(next(iter(slugs)), [])
        return matches[0] if len(matches) == 1 else None

    return resolve


def main(argv=None) -> int:
    """The CLI. Report-only unless `--apply` is typed.

    EXIT CODE (the steward's machine-readable answer, and the reason `ReapPlan.is_clean` is
    not dead code):

    * ``0`` - the tree is bounded: nothing pending, nothing refused.
    * ``1`` - it is not. On a dry run that means removals are pending or a group is refused;
      on an ``--apply`` run it means a removal failed or a refusal is still standing. A
      refusal is deliberately non-zero in BOTH modes: those groups hold the only copy of a
      file and are the half that needs a human.
    * ``2`` - argparse rejected the command line, or the resolved root does not exist, or
      ``new`` refused (a malformed ``--firing``, an unclaimable path, or with ``--copy`` a
      source that is missing or empty, or a copy that did not verify).
    """
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    ap = argparse.ArgumentParser(
        prog="python -m agentflow.backups", description=__doc__
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser(
        "new",
        help="claim and print the canonical backup path for a file; with --copy, also copy "
        "the file into it and verify the copy",
    )
    new.add_argument("path")
    new.add_argument(
        "--firing",
        required=True,
        help="<actor>-f<N>, e.g. dev-manager-f592; a seat with no firing counter uses its "
        "sitting's start as UTC epoch seconds for N",
    )
    new.add_argument(
        "--copy",
        action="store_true",
        help="take the backup, not just the name: copy PATH into the claim and verify it is "
        "non-empty and identical, or remove the claim and exit non-zero",
    )

    reap = sub.add_parser("reap", help="report (or, with --apply, perform) a retention sweep")
    reap.add_argument(
        "--project",
        help="narrow to projects/<name>; it does NOT reach archive/<name>, so a bare "
        "`reap` over all of data_root is the sweep that sees everything (default)",
    )
    reap.add_argument(
        "--keep",
        type=int,
        default=KEEP_DEFAULT,
        help=f"backups to keep per file, newest first (default {KEEP_DEFAULT})",
    )
    reap.add_argument(
        "--apply",
        action="store_true",
        help="delete the planned backups (default: report only)",
    )
    args = ap.parse_args(argv)

    if args.cmd == "new":
        # Every refusal here is a readable line and exit 2, never a traceback (gh#712): a
        # malformed --firing is ValueError from `backup_name`, a missing directory is OSError
        # from the claim, and a source that cannot be backed up is BackupError.
        try:
            if args.copy:
                print(take_backup(args.path, args.firing))
            else:
                print(next_backup_path(args.path, args.firing))
        except (ValueError, OSError, RuntimeError, BackupError) as exc:
            print(f"backups new: {exc}", file=sys.stderr)
            return 2
        return 0

    from .config import load_environment

    env = load_environment()
    root = Path(env.data_root)
    if args.project:
        # The same single-literal-segment discipline `epic_survey.SurveyPatterns.for_project`
        # applies. Duplicated rather than imported because that guard lives inside a frozen
        # dataclass method and is not callable on its own; the shape it rejects is identical.
        # `--project ..` resolves to `data_root`, which EXISTS - so the directory check below
        # would pass it, and a flag meant to narrow the sweep would silently widen it.
        if (
            any(ch in args.project for ch in "/\\*?[]")
            or any(ch.isspace() for ch in args.project)
            or args.project in (".", "..")
        ):
            print(
                f"reap: --project must be a single project name, got {args.project!r}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        root = root / "projects" / args.project
    if not root.is_dir():
        # A typo'd --project used to plan over a non-directory, report "0 to remove, 0
        # refused" and exit 0 - indistinguishable from a genuinely bounded tree, in a command
        # the steward runs unattended and routes as evidence. Exit 2 (a bad invocation), not
        # 1: "you asked the wrong question" must not read as "the tree is not bounded".
        print(
            f"reap: no such directory: {root}"
            + (f" (--project {args.project!r})" if args.project else ""),
            file=sys.stderr,
        )
        raise SystemExit(2)

    # Indexed over the FULL data_root even when --project narrows the sweep. Judging
    # uniqueness inside a narrowed scope makes LESS information produce a MORE
    # confident caption: a slug held by two projects looks unique inside one of them.
    plan = plan_reap(
        root, args.keep, resolve_live=_make_live_resolver(Path(env.data_root))
    )
    result = apply_plan(plan) if (args.apply and plan.removals) else None
    print(f"root: {root}")
    print(render(plan, args.apply, root, result))

    # EXIT CODE IS THE MACHINE-READABLE HALF. Non-zero means the tree is NOT bounded: on a
    # dry run, `plan.is_clean` is false (removals pending, or a group nothing can decide); on
    # an --apply run, a removal failed or a refusal is still standing. The steward reads this
    # rather than parsing the report prose.
    if result is not None:
        return 1 if (result.failures or plan.refusals) else 0
    return 0 if plan.is_clean else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
