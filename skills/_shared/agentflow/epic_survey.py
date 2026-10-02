"""Filesystem epic-survey — the disk replacement for an earlier HTTP work-discovery service.

Behavior-frozen re-home + extension of the legacy lifecycle
reconciliation core (the former merge half of ``agentflow/lifecycle.py``, removed
by gh#378; that module now holds only wave-timeout detection) plus the
status-file discovery of
``server/dist/discoverEpics.js`` (two filename conventions + SKIP_DIRS walk +
``epic:`` frontmatter gate). It reproduces the current discovery result from
disk instead of the HTTP dashboard.

Precedence ladder (higher wins in merge):
    archived(-1) < speccing(0) < planning(1) < running(2) < done(3)

Three passes:
    1. design/specs files  → state = ``current_state`` else ``speccing``
    2. design/plans files  → state = ``current_state`` else ``planning``, then
       ``_promote`` against any existing spec record for that slug. When there is NO spec
       record — pass 1 skipped that spec on mtime, or read and rejected it — the missing
       spec is consulted for its terminal state alone, so the terminal freeze engages as it
       would have — see the comment at that call site for why that is not a cutoff bypass.
    3. status.md files     → ``done`` if ``status == done``; the status itself when it is
       ``abandoned`` / ``failed`` / ``done_on_hold`` (see ``_ENDED_BOARD_STATUSES``); else
       ``running`` — status.md ALWAYS wins (it is filesystem evidence the epic is/was in flight)

Two fields are exposed separately so consumers can tell approval from progress:
    * ``current_state``  — raw frontmatter approval signal (e.g. ``approved-for-autonomous``)
    * ``lifecycle_state`` — progress signal: a status.md when one exists, otherwise the
      NORMALISED FRONTMATTER STATE

**They are NOT independent for a project that writes no status board.**
This block used to read "only a status.md with ``status: done`` sets ``done``". That was false,
and its own test could not catch it: `test_completion_truth_is_status_never_frontmatter` fed a
fixture saying ``approved-for-autonomous`` — a non-terminal value that cannot produce ``done``
by any path — so it passed without exercising the claim. A spec carrying ``current_state: done``
with no status board yields ``lifecycle_state == "done"``.

MEASURED 2026-09-08, per project, because the answer differs by project and an aggregate hides
that. Status boards are named ``<epic>.status.md``:

    agentflow   0 boards, 138 records,  70 read ``done`` — every one frontmatter-derived
    the other  75 boards, 452 records, 247 read ``done`` — pass 3 genuinely runs

So pass 3 is live code exercised by a real corpus; what is inert is pass 3 FOR AGENTFLOW, whose
epics are built by lanes that write no board. An earlier draft of this paragraph said zero
boards existed for either project. That was an artefact of searching for a file named exactly
``status.md``, which never matches ``<epic>.status.md``.

The consequence reaches one other file. ``skills/lead-dev/SKILL.md`` — and only that one — states
a dispatch safeguard, "cross-check the epic's status.md (if one exists)", whose hedge does all
the work for agentflow: the lane writes ``current_state: done`` at merge, the survey derives
``lifecycle_state`` from that same frontmatter, and the lane reads it back as independent
evidence. **Decided and live: route 2** (gh#319, registry Q-10081, which supersedes the open
question Q-10066 carried). Every builder lane now writes the epic's ``<slug>.status.md`` at merge
through ``python -m agentflow.status_writer mark-done``, so pass 3 has a board to read for
agentflow too and the cross-check reads something the frontmatter did not write. The writer
validates its output through ``parse_status_text`` (the parser behind ``read_status``) because a
hand-written record this pass cannot parse is skipped with one log line;
``test_status_merge_record.py`` pins that pass 3 reads it as done. The 2026-09-08 counts above
are the before picture.

Two sidecar fields consumers already read off disk are added as pure
passthroughs (no new behavior): ``priority`` (int, unset = +inf, sorts last) and
``domain`` (str, nullable).

Glob patterns come from CONFIG (root + patterns) so Phase 1 points at the
current layout and Phase 2 only re-points config — no code change.
"""

from __future__ import annotations

import codecs
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping, Optional, Union

import yaml

from agentflow.plan_parser import (
    DAGError,
    SpecFormatError,
    _SLUG_FILENAME_RE,
    _slug_from_filename,
    files_section,
    parse_plan,
    parse_spec_frontmatter,
)
from agentflow.status_writer import read_status, strip_bom

_LOG = logging.getLogger(__name__)

#: `lifecycle_state` is an OPEN PASSTHROUGH of the normalised `current_state`, and this alias
#: now says so. It read `Literal["speccing", "planning", "running", "done", "archived"]` while
#: the live corpus held **17 distinct values** (gh#326), and the declaration was wrong in both
#: directions: 13 observed values were undeclared — `drafted` is the SECOND commonest at 112
#: records, so not an exotic edge — while `archived` was declared and never observed at all.
#:
#: THE RUNTIME WAS NEVER CLOSED, so the annotation was the thing out of step. Pass 1 assigns
#: `_norm_state(sf.current_state) or "speccing"` — whatever a human wrote in frontmatter; pass 2
#: freezes the spec's value; pass 3 maps a board to `done`, an ended status, or `running`. Declaring five values and
#: suppressing the assignment with `# type: ignore[arg-type]` kept the two from arguing, which is
#: how a correctness argument came to rest on runtime behaviour the declaration contradicted:
#: gh#261's `_done_slugs` fix WORKS ONLY BECAUSE `superseded` and `shipped` really do reach this
#: field, which the Literal said was impossible.
#:
#: THE CLOSED-VOCABULARY OPTION IS NOT TAKEN HERE AND IS NOT DEAD. Mapping arbitrary frontmatter
#: into a fixed set with a default is a behaviour change over 175 records across 13 values, and it
#: decides what `_LIFECYCLE_PRECEDENCE` should do with the values it does not name — the same gap
#: `_merge_spec_record` records below. That is a product call with its own blast radius, filed
#: rather than smuggled in behind a type annotation.
LifecycleState = str
BlockedStatus = Literal["READY", "NEAR-READY", "CHAINED", "MISSING-DEP"]

# Priority sentinel: an epic with no `priority:` frontmatter sorts LAST
# (lead-dev section 2 step 3 rule "unset ⇒ +∞").
UNSET_PRIORITY: float = math.inf

# Lifecycle precedence score (higher wins in merge). archived is special: -1 so
# it is always overridden by a real filesystem signal from another source.
_LIFECYCLE_PRECEDENCE: dict[str, int] = {
    "speccing": 0,
    "planning": 1,
    "running": 2,
    "done": 3,
    "archived": -1,
}

# `current_state` values that mean "this epic's story is over". They are TERMINAL in the
# approval sense: no later edit to a sibling file should be able to reopen one. Grounded in
# the corpus rather than invented - across projects/ and archive/ the observed terminal values
# are done (278), archived (67) and superseded (27). `shipped` is included because the
# reconcile prose in several completion-truth notes uses it, so a spec may yet carry it.
#
# Load-bearing because a plan sidecar merges OVER a spec (see `survey_epics`): without this,
# a spec reconciled to `done` after shipping is re-reported with the plan's stale
# `approved-for-autonomous`, and a selector that trusts `current_state` re-dispatches work that
# already merged. That is not hypothetical - it put 10 shipped epics back in the ready set.
TERMINAL_CURRENT_STATES: frozenset[str] = frozenset({
    "done",
    "archived",
    "superseded",
    "shipped",
})

# Board `status:` values (status_writer.StatusEnum) that END a run WITHOUT delivering it. Pass 3
# carries them through as the record's `lifecycle_state` rather than folding them into
# `running`: nothing is in flight, and nothing will ever land, so a dependant waiting on one is
# stranded rather than queued. They are deliberately NOT in `TERMINAL_CURRENT_STATES`, which
# means "satisfies a `depends_on`" - an abandoned parent never shipped its work.
TERMINAL_NOT_DONE_BOARD_STATUSES: frozenset[str] = frozenset({"abandoned", "failed"})

# Board statuses pass 3 passes through verbatim instead of reading as `running`: the two above,
# plus `done_on_hold` - the run ended with its PR left open for a human merge decision. That
# one is not stranded (a merge still delivers it) and not done (the work is not on main yet),
# so it satisfies nothing and warns nothing; it simply stops claiming to be in flight.
_ENDED_BOARD_STATUSES: frozenset[str] = TERMINAL_NOT_DONE_BOARD_STATUSES | {"done_on_hold"}

# The subdirectories of a `design/` bucket that hold TERMINAL records. `reconcile` MOVES a
# file into one of these the moment its `current_state` goes terminal, so this tuple is the
# codomain of that pass and the reach the globs below must have.
#
# ONE tuple, two readers, and the read side DERIVES from it (see `_bucket_globs`). A shared
# constant both sides merely consult would still let a third partition be added to the write
# side alone; deriving makes widening one half the same edit as widening the other. This is
# the directory half of the rule `reconcile` already states for the state vocabulary - a
# second list is a second definition.
#
# Load-bearing because the survey's globs were single-level: every record reconcile moved
# left the survey, and with it `_done_slugs`, so a FINISHED prerequisite read as unsatisfied
# and stranded every dependent that named it (gh#122). On the live tree that was 440 records
# across two projects waiting on the next `--apply`.
TERMINAL_PARTITIONS: tuple[str, ...] = ("done", "archived")

# The ONE `current_state` value that means a human has standing-approved this epic for
# autonomous build. It is the single human gate in the whole lifecycle - what makes gateless
# auto-merge-to-main safe - so `dispatchable()` requires it rather than trusting a builder's
# prose to remember it.
#
# ASYMMETRY, deliberate: an unset/None `current_state` does NOT dispatch, whereas an unset
# `project:` still does (see `dispatchable`). `project:` postdates the earliest specs, so its
# absence is legitimately "unknown" and failing closed on it would strand real work. Approval
# is not a field a spec can predate: no spec was ever approved before the concept existed, and
# an unset approval is simply not an approval. Unknown ⇒ dispatch for `project:`; unknown ⇒
# do not dispatch for approval.
APPROVED_CURRENT_STATE: str = "approved-for-autonomous"

# The `current_state` values at which a spec's `## Files` section is load-bearing.
# `drafted` is the state a human promotes FROM, so it is the state the standing Directive of
# 2026-08-31 gates ("a spec without a `## Files` section may not be promoted").
# `approved-for-autonomous` is included even though it is past that gate, because a spec that
# reached it without a section is the harm the Directive was written from: claimable in
# principle, unpublishable in fact, and silent about why (see `plan_parser.files_section`).
#
# Deliberately NOT every non-terminal state. A `speccing` spec has not reached the gate, so
# warning about it is premature, and a channel that warns about work nobody is promoting yet
# is a channel the reader learns to skip.
FILES_GATED_CURRENT_STATES: frozenset[str] = frozenset({"drafted", APPROVED_CURRENT_STATE})


def _norm_state(state: object) -> Optional[str]:
    """Canonical lower/stripped form of a frontmatter state, or None if unusable.

    NEVER RAISES. `current_state` arrives straight from YAML with no coercion
    (`plan_parser.parse_spec_frontmatter` passes `data.get("current_state")` through raw), so it
    can be a bool, an int or a date: YAML 1.1 turns a bare `current_state: yes` / `on` / `true`
    into a bool, and `2026-07-24` into a date. Calling a str method on those raised straight out
    of `survey_epics` - and the plan-pass call site sits OUTSIDE the per-file try/except, so one
    typo'd character destroyed the WHOLE survey rather than skipping one file, taking every other
    epic with it. That is the opposite of this module's posture everywhere else, which is
    log-and-skip (see `_warn_unknown_queue_classes`: "A warning, never a raise").
    """
    if not isinstance(state, str):
        if state is not None:
            _LOG.warning("ignoring non-string current_state %r (%s)", state, type(state).__name__)
        return None
    norm = state.strip().lower()
    return norm or None


def _is_terminal(state: object) -> bool:
    """True when a state value means the epic is closed.

    Tolerant of casing and stray whitespace: this value gates dispatch, so a `Done ` that failed
    an exact-match test would silently reopen a shipped epic. (Unquoted YAML trailing comments -
    `current_state: archived  # cancelled by ...`, which the corpus does use - are already
    stripped by the YAML parser, so they need no handling here.)
    """
    return _norm_state(state) in TERMINAL_CURRENT_STATES


def _is_approved(state: object) -> bool:
    """True when a state value is the standing human approval for autonomous build.

    Tolerant of casing and stray whitespace for the same reason `_is_terminal` is, in the
    opposite direction: an exact-match test would read `  Approved-For-Autonomous  ` as
    unapproved and silently strand work a human did approve. Reuses `_norm_state`, so a
    non-string YAML value (a bool from `current_state: yes`, a date) is not approved and does
    not raise. Unset/None is not approved - see `APPROVED_CURRENT_STATE` for why that differs
    from the unset-`project:` rule.
    """
    return _norm_state(state) == APPROVED_CURRENT_STATE


# Directories the recursive status walker refuses to descend into. The sync
# walker wedges on node_modules / .git / vendored trees otherwise.
SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules",
    ".git",
    ".venv",
    "dist",
    "build",
    "__pycache__",
    ".next",
    ".turbo",
    ".yarn",
    ".pnpm-store",
    "vendor",
    ".claude",
    "worktrees",
})

# First 2KB of a status.md must start with `---` and contain `\nepic:` to count
# as an orchestrator status file (is_orchestrator_status). Filters out
# non-orchestrator status.md files (e.g. a doc-resync index).
_STATUS_HEAD_BYTES = 2048
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# --------------------------------------------------------------------------- #
# Config: glob patterns (root-relative). Defaults describe the Phase-2 target
# layout (projects/<name>/{refine,solution,status}); Phase 1 re-points these via
# config with no code change.
# --------------------------------------------------------------------------- #
def _scope_glob(pattern: str, project: str) -> str:
    """Return *pattern* with its wildcard PROJECT segment replaced by *project*.

    The project segment is the first path segment that is a bare ``*`` (the
    ``projects/*/design/specs/*.md`` shape) — the trailing ``*.md`` filename wildcard
    is left alone. A pattern with no such segment cannot be project-scoped: it
    is returned unchanged and warned about rather than silently narrowed to
    nothing.
    """
    parts = pattern.split("/")
    for i, part in enumerate(parts):
        if part == "*":
            parts[i] = project
            return "/".join(parts)
    _LOG.warning("glob has no wildcard project segment, not project-scoped: %r", pattern)
    return pattern


def _bucket_globs(bucket: str) -> tuple[str, ...]:
    """The bucket root, plus ONE glob per terminal partition. Deliberately not ``**``.

    A bucket is where reconcile writes AND where humans put working material, and those
    are different sets. Recursive globbing conflates them: on the live tree today
    ``design/plans/`` already carries a ``grounding/`` directory of 36 notes and a
    ``.scratch/`` of partial fragments (``pathlib``'s ``**`` descends into dotted
    directories, and ``SKIP_DIRS`` is consulted only by ``_rwalk_match``, which serves the
    STATUS walk - not this one). Surveyed recursively, one of those grounding notes enters
    the planning queue as a live epic and 48 fragments print a skip warning per survey.

    Naming the partitions instead makes the reach exactly the codomain of the pass that
    moves the files, so "everything under a bucket is an epic" never becomes the contract.
    """
    base = f"projects/*/design/{bucket}"
    return (f"{base}/*.md", *(f"{base}/{p}/*.md" for p in TERMINAL_PARTITIONS))


@dataclass(frozen=True)
class SurveyPatterns:
    """Root-relative glob patterns for the three record sources.

    ``spec_globs`` / ``plan_globs`` glob individual ``*.md`` files. Plan globs
    exclude ``*.status.md`` (those are status boards, not plans). ``status_dir_globs``
    glob DIRECTORIES that are then walked recursively (SKIP_DIRS-respecting) for
    both status filename conventions.

    Each authored family is THREE globs, not one: the bucket root where live work sits,
    plus each of ``TERMINAL_PARTITIONS`` where ``reconcile`` files a record once its
    ``current_state`` goes terminal. Both halves are required - a terminal record is still
    a record, and dropping it is what breaks dependency math rather than merely tidying
    the board (see ``_bucket_globs``).

    The defaults encode the three-pillar layout: authored documents live under
    ``design/`` (``specs/``, ``plans/``, ``use-cases/``, ``suggestions/``) and machine
    coordination stays in ``status/``. They name ONE layout deliberately - a default
    that globbed the pre-three-pillar ``refine/`` and ``solution/`` as well would make
    the on-disk shape permanently ambiguous to every reader, to keep faith with a
    population of old installs that does not exist. The single machine that had the old
    layout was migrated once, by runbook.
    """

    spec_globs: tuple[str, ...] = _bucket_globs("specs")
    plan_globs: tuple[str, ...] = _bucket_globs("plans")
    status_dir_globs: tuple[str, ...] = ("projects/*/status",)

    def for_project(self, name: str) -> "SurveyPatterns":
        """Return these patterns narrowed to the ``name`` project subtree.

        Substitutes *name* for the wildcard project segment in all three glob
        families, so this pattern config stays the single source of the on-disk
        layout — no caller hardcodes ``projects/<name>/...`` itself. ``self`` is
        frozen and untouched.

        Raises
        ------
        ValueError
            When *name* is blank or is not a single literal path segment
            (contains a separator or a glob metacharacter) — a name like
            ``a/b`` or ``a*`` would silently widen the scope it is meant to narrow.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"project name must be a non-empty string, got {name!r}")
        if any(ch in name for ch in "/\\*?[]") or any(c.isspace() for c in name):
            raise ValueError(f"project name must be a single literal path segment, got {name!r}")
        if name in (".", ".."):
            # A single segment, but one that walks out of projects/ entirely:
            # for_project("..") would survey <data_root>/design/specs/*.md.
            raise ValueError(f"project name must name a project, not a relative path: {name!r}")
        return SurveyPatterns(
            spec_globs=tuple(_scope_glob(g, name) for g in self.spec_globs),
            plan_globs=tuple(_scope_glob(g, name) for g in self.plan_globs),
            status_dir_globs=tuple(_scope_glob(g, name) for g in self.status_dir_globs),
        )


DEFAULT_SURVEY_PATTERNS = SurveyPatterns()


# --------------------------------------------------------------------------- #
# Record
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EpicRecord:
    """Merged view of a single epic across refine / solution / status sources.

    ``current_state`` (raw approval signal) and ``lifecycle_state`` (progress signal)
    are separate fields, but NOT independent sources: a ``current_state`` of
    ``approved-for-autonomous`` does not mark the epic done, while a TERMINAL
    ``current_state`` does when no ``<epic>.status.md`` exists — see the module docstring
    for the per-project measurement and gh#319 for the open question. This sentence used to
    end "only a status.md with ``status: done`` does", which was false and is the claim
    gh#319 was filed about.
    """

    slug: str
    title: str
    lifecycle_state: LifecycleState
    current_state: Optional[str] = None   # raw frontmatter approval signal
    depends_on: list[str] = field(default_factory=list)
    priority: float = UNSET_PRIORITY      # int; unset = +inf (sorts last)
    domain: Optional[str] = None
    project: Optional[str] = None
    queue_class: Optional[str] = None     # who-builds gate, ALLOW-list: only None or a value in
                                          # DISPATCHABLE_QUEUE_CLASSES dispatches (see dispatchable())
    build_approved_by: Optional[str] = None  # per-epic override of project.excluded_domains (who + when)
    last_modified: str = ""               # ISO 8601 UTC string
    source_path: str = ""                 # most-authoritative source file
    progress: Optional[dict] = None       # only for running / done
    current_phase: Optional[str] = None   # only for running
    paused: bool = False


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _promote(prior: Optional[str], new: str) -> LifecycleState:
    """Return whichever lifecycle state has higher precedence."""
    if prior is None:
        return new
    if _LIFECYCLE_PRECEDENCE.get(new, -999) > _LIFECYCLE_PRECEDENCE.get(prior, -999):
        return new
    return prior


def _normalize_slug_stem(s: str) -> str:
    """Strip an optional date prefix and ``-design`` suffix from a slug string.

    Mirrors ``normalizeSlugStem`` (planParser.js) — the string-input twin of
    ``_slug_from_filename`` used by the status discovery.
    """
    m = _SLUG_FILENAME_RE.match(s)
    return m.group(1) if m and m.group(1) else s


#: A key declared at column 0 inside the frontmatter block. Column 0 does most of the work:
#: YAML requires block-scalar content (`progress: |`) to be indented, so a `word:` at the left
#: margin is normally a top-level key rather than prose inside a value.
#:
#: THREE SHAPES DEFEAT IT, all measured and all absent from this corpus, so the claim is
#: "normally" and not "never": a multi-line FLOW collection (`meta: {` ... `owner: bob,`) puts
#: nested keys at column 0; a YAML 1.1 boolean-ish key name (`on:`, `no:`, `off:`) parses to a
#: bool so the string never matches the mapping; and a legitimately-closing quoted scalar whose
#: continuation line is UNindented reads the same as one that never closed. Swept 2026-09-22
#: over every frontmatter block under the data root: 2338 blocks, 2321 parsed as mappings,
#: ZERO flagged, and zero files opening a flow collection at end of line. A false positive here
#: costs one advisory line and no behaviour, which is why the cheap check ships over a parser.
_TOP_LEVEL_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):", re.MULTILINE)


def _swallowed_keys(block: str, data: Mapping[str, object]) -> list[str]:
    """Keys *block* declares at column 0 that *data* does not carry. Pure; logs nothing.

    Split out of `_warn_swallowed_keys` so the stale path can ask the same question without
    the WARNING: there the answer feeds the plan pass's fail-closed branch, which already owns
    the one line a dropped record gets.
    """
    declared = list(dict.fromkeys(m.group(1) for m in _TOP_LEVEL_KEY_RE.finditer(block)))
    return [key for key in declared if key not in data]


def _warn_swallowed_keys(path: Path, block: str, data: Mapping[str, object]) -> list[str]:
    """Report keys the frontmatter DECLARES but the parser did not return.

    THE SILENT HALF OF issue #204, and it is not the half that issue describes. An
    *unparseable* spec is already the loud case: three handlers name it at WARNING, register
    the slug against sidecar promotion, and it lands in `counted N, skipped M` by name. What
    nothing could see is a spec that PARSES and is semantically gutted — a completion-truth
    reconcile composes a quoted scalar out of free prose (PR titles, paths, punctuation),
    the closing quote lands in the wrong place, and the value swallows the key lines beneath
    it. Nothing raises, so nothing logs.

    Measured on a synthetic corpus at c87b907, both with ZERO log lines at any level:

    * a swallowed ``queue_class``/``priority`` leaves the epic selectable with priority +inf,
      so it sorts behind every healthy epic forever;
    * a swallowed ``current_state`` demotes an ``approved-for-autonomous`` epic to
      ``speccing``, which drops it out of ``selectable()`` entirely — approved on every board,
      claimable by nobody, and no line anywhere says why.

    The check is a comparison, not a parse: whatever the block declares at column 0 should
    survive into the mapping. A key that does not is evidence the value above it did not
    close. That is why this cannot be done by catching an exception — there is no exception.

    A WARNING, NEVER A RAISE, and never a change to what is returned. The record keeps
    whatever the parser gave it; this only ends the silence. Returning the lost keys (rather
    than logging and discarding them) is what lets a caller assert the detection in a test
    without reading log text.

    WHERE THE CHECK RUNS, AND WHERE IT DELIBERATELY DOES NOT. This function's only caller is
    `_read_sidecar`, which both passes reach AFTER the mtime cutoff, so it covers fresh specs:
    every lost key is named at WARNING and the record keeps what the parser gave it. A spec
    older than the cutoff is covered differently, by `_stale_spec_state` running the same
    comparison (`_swallowed_keys`) with no log of its own. There it matters for one key only -
    a stale spec whose terminal `current_state` was eaten would otherwise read as "no state"
    and let a fresh approved plan promote a shipped epic back into the queue with zero log
    lines. So a lost `current_state` becomes the stale state's `error`, the plan pass takes its
    existing fail-closed branch, and its skip line is the single line that says so. Stale specs
    are opened only when some plan asks for a slug (a miss can read every skipped spec in one
    full pass), and the comparison rides those reads without logging, so a stale spec whose slug
    no plan names never adds a line; any other lost key on a stale spec changes nothing and logs
    nothing, since the stale path reads `current_state` and nothing else.
    """
    lost = _swallowed_keys(block, data)
    if lost:
        _LOG.warning(
            "frontmatter declares %d key(s) the parser did not return, so a quoted value "
            "above them did not close and swallowed them: %s (%s)",
            len(lost),
            path,
            ", ".join(lost),
        )
    return lost


def _read_sidecar(path: Path) -> tuple[float, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Read the sidecar frontmatter fields (+ project, queue_class, build_approved_by) off a spec/plan file.

    Returns ``(priority, domain, project, queue_class, build_approved_by)``. ``priority`` is ``UNSET_PRIORITY``
    (+inf) when the field is absent or not an int. Pure passthrough — no
    validation beyond type coercion (behavior frozen: consumers already read
    these two fields off disk).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UNDECODABLE BYTES ARE NOT AN `OSError`, AND THAT COST THE WHOLE SURVEY (issue #80).
        # `UnicodeDecodeError` subclasses `ValueError`, so a single UTF-16 spec - which is what
        # a PowerShell `>` redirect leaves behind on this fleet - escaped this handler, and
        # BOTH call sites read the file ABOVE their own per-file `try`, so it aborted
        # `survey_epics` outright. Not a degraded record: no records at all, on the surface
        # every lane reads to decide what it may build. `_opens_a_frontmatter_block` below
        # already states the module's stance ("an unreadable file must not turn a fail-closed
        # verdict into a crash"); this arm is that stance applied to the reader that missed it.
        #
        # THE SENTINEL IS A DEFERRAL, NOT A SWALLOW, and only because of an invariant the next
        # caller must keep: both call sites re-read the SAME file one line later through a full
        # parser (`parse_spec_frontmatter` / `parse_plan`) inside a broad `except Exception`,
        # which raises on exactly these bytes and lands in the `, skipping: %s (%s)` channel -
        # so the file is named and COUNTED in `counted N, skipped M`, not silently dropped
        # (issue #122's class). A future caller that reads the sidecar WITHOUT that second
        # parse would make the file vanish with no line anywhere.
        #
        # Narrow on purpose. Not `ValueError`: `SpecFormatError` and `DAGError` are both
        # `ValueError` subclasses (`plan_parser.py`), and not `Exception`: this handler does not
        # report, it returns "no priority, no domain, no project, no queue_class, no approver",
        # so any class the following parse would NOT also raise becomes a confidently wrong
        # survey answer instead of a skip line - and `MemoryError` on a huge file would read as
        # a missing priority field.
        return UNSET_PRIORITY, None, None, None, None
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return UNSET_PRIORITY, None, None, None, None
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return UNSET_PRIORITY, None, None, None, None
    if not isinstance(data, dict):
        return UNSET_PRIORITY, None, None, None, None
    _warn_swallowed_keys(path, match.group(1), data)

    raw_priority = data.get("priority")
    priority: float = (
        float(raw_priority)
        if isinstance(raw_priority, int) and not isinstance(raw_priority, bool)
        else UNSET_PRIORITY
    )
    raw_domain = data.get("domain")
    domain = raw_domain if isinstance(raw_domain, str) else None
    raw_project = data.get("project")
    project = raw_project if isinstance(raw_project, str) else None
    raw_queue_class = data.get("queue_class")
    queue_class = raw_queue_class if isinstance(raw_queue_class, str) else None
    raw_approved = data.get("build_approved_by")
    build_approved_by = raw_approved if isinstance(raw_approved, str) and raw_approved.strip() else None
    return priority, domain, project, queue_class, build_approved_by


#: `_spec_declares_files`'s answer for a ``## Files`` heading that declares no path.
#: `plan_parser.files_section` extracts only backticked paths, so a section listing
#: `- src/widget.py` is present and names nothing a seam check can use. A distinct value, not
#: False, so the missing-heading warning and this one stay two different messages.
#: It is a TRUTHY string: callers compare with `is` and never test it for truthiness, since
#: `if declares:` would read a pathless section as a declared one.
DECLARES_NO_PATHS: str = "declares-no-paths"


def _spec_declares_files(path: Path) -> Optional[Union[bool, str]]:
    """Whether the spec at *path* carries a usable ``## Files`` section. None when unreadable.

    False: no heading. `DECLARES_NO_PATHS`: the heading is there and zero backticked paths are
    under it (`present` and not `seam_checkable`). True: the section names at least one path.

    DELEGATES to `plan_parser.files_section` rather than re-testing the heading here. That
    function IS the promotion precondition a promoting seat runs, so a hand-rolled second
    heading test would be a second definition of the gate this warning exists to predict, free
    to drift out of agreement with it - the failure this module already names elsewhere as "a
    second list is a second definition". Both halves of the answer come from that one call and
    that one read, so the heading test and the path test cannot disagree about which text they
    judged.

    NONE, NOT FALSE, on anything that cannot be read. No file CONTENT reaches this arm through
    `survey_epics`: `parse_spec_frontmatter` reads the same file ~50 lines EARLIER, raises on
    exactly these bytes, and its handler logs `, skipping: %s (%s)` and `continue`s, so an
    undecodable spec never becomes a record and never arrives here. The `OSError` half IS
    reachable from that caller: the parse and this check are two separate reads of one path (of
    three per non-terminal spec, counting `_read_sidecar`), and a spec locked by another lane
    between them raises here. Deletion and rename land on this arm too, and since gh#361 they
    are observable through it: the record no longer re-stats the path, so the survey survives
    to report a spec that went away mid-read. Kept for all three, because "could
    not look" is not "has none": a caller handed False for a file nobody managed to read would
    report a missing heading against a file whose real problem is that it was not there.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fs = files_section(text)
    if not fs.present:
        return False
    return True if fs.seam_checkable else DECLARES_NO_PATHS


#: How many leading bytes decide "is this file UTF-8 text at all". Eight is enough for every
#: shape that matters - a BOM is at most four bytes, and a UTF-16 file carries a NUL inside its
#: window - a NUL appears within the first few ASCII characters, not necessarily inside the
#: first one, which is why the window is 8 bytes and not 2 - and staying small is deliberate:
#: the further into a file this sniff reads,
#: the more chances a legitimately odd byte has to be mistaken for a broken encoding.
_ENCODING_SNIFF_BYTES = 8


def _looks_like_utf16(head: bytes) -> bool:
    """True when *head* opens a UTF-16 file rather than a UTF-8 one.

    THE BOM IS THE EASY HALF AND NOT THE DANGEROUS ONE. UTF-16 bytes carrying a BOM
    (``\\xff\\xfe`` / ``\\xfe\\xff``) are not valid UTF-8, so every strict reader in this module
    already raises on them and every such file is already named in a skip line.

    A BOM-LESS UTF-16LE FILE IS THE ONE THAT GETS THROUGH, because NUL is a perfectly legal
    UTF-8 byte: `---\\nslug: x` written UTF-16LE decodes WITHOUT ERROR into mojibake, so no
    decode-layer guard fires anywhere. It then misses the `^---` frontmatter regex and surveys
    as an ordinary frontmatter-less `speccing` record - a record wearing a state nobody wrote,
    which a plan sidecar can then promote (issue #42's shape, reached with no exception raised
    and no warning emitted). Strictly worse than the crash issue #80 fixed: that one was loud.

    So the test is "is there a NUL in the head", not "did decoding fail". No UTF-8 markdown file
    reaches this predicate by accident - a NUL byte inside the first eight bytes of a text file
    is not a thing a human editor writes, and the corpus shape this must NOT catch (a spec that
    is simply prose with no frontmatter) contains none.
    """
    return (
        head.startswith(codecs.BOM_UTF16_LE)
        or head.startswith(codecs.BOM_UTF16_BE)
        or b"\x00" in head
    )


def is_orchestrator_status(path: Path) -> bool:
    """True when *path*'s first 2KB start with ``---`` and contain ``\\nepic:``.

    The YAML-frontmatter signature that distinguishes an orchestrator status.md
    from any other ``status.md`` (ported from the legacy dashboard's discovery logic).

    "NOT AN ORCHESTRATOR STATUS" AND "COULD NOT READ IT" ARE DIFFERENT ANSWERS, and this
    function used to conflate them: it decoded with ``errors="replace"``, so undecodable bytes
    became mojibake, the mojibake failed the ``---`` test, and the file was dropped with no line
    anywhere. The cost is not merely a missing progress bar. A status board is the only thing
    that can make an epic `done` WHEN ITS SPEC DOES NOT ALREADY SAY SO - a spec whose own
    frontmatter reads `current_state: done` is terminal with no status board involved - so an
    unreadable status.md leaves exactly that population non-terminal and every dependant of it
    silently unsatisfied - issue #122's stranding class, re-entered
    through the decode layer. Both unreadable arms are therefore named through the same
    ``, skipping: %s (%s)`` channel the rest of the module reports skips on.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_STATUS_HEAD_BYTES)
    except OSError:
        return False
    if _looks_like_utf16(raw[:_ENCODING_SNIFF_BYTES]):
        _LOG.warning(
            "unreadable status file, skipping: %s (%s)",
            path,
            "not utf-8: utf-16 byte shape in the head",
        )
        return False
    try:
        # `final=False` because this is a FIXED-SIZE SLICE of a longer file: a multi-byte
        # character straddling the 2KB boundary is a truncation of the read, not corruption of
        # the file, and an incremental decoder buffers it instead of calling a healthy status
        # board unreadable.
        head = codecs.getincrementaldecoder("utf-8")().decode(raw, False)
    except UnicodeDecodeError as exc:
        _LOG.warning("unreadable status file, skipping: %s (%s)", path, exc)
        return False
    # A DECODED UTF-8 BOM SITS AHEAD OF THE `---` AND IS INVISIBLE TO startswith. Without
    # this strip, a perfectly readable BOM'd status board fails the signature test and is
    # dropped with NO line at all - the exact silent-stranding this function was just
    # rewritten to end, arriving through the likeliest shape on a Windows fleet whose
    # default `Out-File` writes one. On the spec side only the BYTE gate
    # (`_opens_a_frontmatter_block`) strips `BOM_UTF8` - `parse_spec_frontmatter` itself still
    # raises on a BOM'd spec, which that gate turns into a named skip rather than a read. The
    # status side had neither, and measured identically on master and on the first draft
    # of this fix: parent non-terminal, dependant never ready, zero warnings. THE SAME STRIP
    # `read_status` applies, not a copy of it: this gate stripping while the parser behind it
    # did not left the board named as malformed and its epic still stranded (gh#336).
    head = strip_bom(head)
    if not head.startswith("---"):
        return False
    return "\nepic:" in head or head.startswith("---\nepic:")


def _rwalk_match(root: Path, predicate) -> list[Path]:
    """Recursively walk *root*, returning files whose basename matches *predicate*.

    Refuses to descend into SKIP_DIRS. Ported from ``rwalkMatch`` (discoverEpics.js);
    the SKIP_DIRS guard keeps the walker from wedging on vendored / node_modules trees.
    """
    if not root.exists():
        return []
    out: list[Path] = []
    stack: list[Path] = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if entry.name in SKIP_DIRS:
                    continue
                stack.append(entry)
            elif predicate(entry.name):
                out.append(entry)
    return out


@dataclass(frozen=True)
class _StaleSpecState:
    """The terminal-state question, answered for ONE spec that pass 1 skipped on mtime.

    Deliberately not an ``EpicRecord``: nothing here is merged into the survey's output. The
    only two things a skipped spec is allowed to contribute are "was it terminal" and "could
    it be read at all", which is the whole of this type.
    """

    path: Path
    current_state: Optional[str] = None
    error: Optional[BaseException] = None   # set when the file could not be read or parsed


def _stale_spec_state(path: Path) -> tuple[str, _StaleSpecState]:
    """Return ``(slug, state)`` for one mtime-skipped spec.

    SLUG RESOLUTION IS PASS 1'S, NOT A GLOB. A spec's slug comes from its ``slug:``
    frontmatter and only falls back to the filename when there is no frontmatter to read -
    `2026-06-02-foo.md` may declare `slug: bar`, and every filename-shaped guess about that
    is wrong. So this calls the same ``parse_spec_frontmatter`` / ``_slug_from_filename``
    pair pass 1 does, and matching stays correct by construction rather than by coincidence.

    A file with no frontmatter is NOT an error: pass 1 accepts it as a `speccing` record with
    no ``current_state``, so it is not terminal and it blocks nothing. An unreadable or
    malformed one is an error, carried out for the caller to fail closed on - it cannot be
    shown non-terminal, and its slug is then only as good as its filename, which is exactly
    the fallback pass 1 already trusts in the same situation.

    "NO FRONTMATTER" IS DECIDED ON THE BYTES, NOT ON THE EXCEPTION. ``SpecFormatError`` covers
    two unlike files: one that genuinely carries no `---` block, and one that opens with a
    block the parser could not match - a UTF-8 BOM before the opening `---` is enough, and this
    is a Windows fleet where the default `Out-File` / `Set-Content -Encoding utf8` writes one.
    Reading the second as "no frontmatter" hands back `current_state=None`, which is not
    terminal, so a `current_state: done` that any human can read in the file lets the plan
    promote and issue #42 reopens through a file that merely looks fine. So the head bytes
    decide, and a file that opens a frontmatter block the parser rejects fails closed.

    A FILE THAT PARSES CAN STILL HAVE LOST ITS STATE. A quoted value that never closes swallows
    the keys beneath it and nothing raises, so a stale `current_state: done` eaten that way
    reads as None - not terminal - and the plan promotes. `_read_sidecar` names that shape for
    fresh specs; stale ones never reach it, so the same comparison runs here. Only a lost
    `current_state` counts, because it is the only key this function returns, and it becomes
    an error rather than a log line: the plan pass's fail-closed branch already writes the one
    line a dropped record gets, and a stale file read for some other slug must add none.
    ``parse_spec_frontmatter`` does not hand back the raw block, so the block is re-read here.
    """
    try:
        sf = parse_spec_frontmatter(path)
    except SpecFormatError as exc:
        if _opens_a_frontmatter_block(path):
            return _slug_from_filename(path), _StaleSpecState(path=path, error=exc)
        return _slug_from_filename(path), _StaleSpecState(path=path)
    except Exception as exc:  # noqa: BLE001 — mirrors pass 1: never abort the survey
        return _slug_from_filename(path), _StaleSpecState(path=path, error=exc)
    try:
        lost = _stale_swallowed_keys(path)
    except Exception as exc:  # noqa: BLE001 — the second read of a file the parser just took
        return sf.slug, _StaleSpecState(path=path, error=exc)
    if "current_state" in lost:
        return sf.slug, _StaleSpecState(
            path=path,
            error=SpecFormatError(
                "frontmatter declares key(s) the parser did not return, so a quoted value "
                f"above them did not close and swallowed them: {path} ({', '.join(lost)})"
            ),
        )
    return sf.slug, _StaleSpecState(path=path, current_state=sf.current_state)


def _stale_swallowed_keys(path: Path) -> list[str]:
    """`_swallowed_keys` for a stale spec, reading the block itself. Raises on any read error.

    The block is matched with the same pattern the parser uses, so a file the parser just
    accepted matches here too. A block that no longer matches or no longer parses to a mapping
    can only mean the file changed between the two reads, and that raises rather than answering
    "nothing lost": the caller fails closed on it exactly as it would on any unreadable spec.
    """
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SpecFormatError(f"frontmatter block vanished between reads: {path}")
    data = yaml.safe_load(match.group(1)) or {}
    if not isinstance(data, dict):
        raise SpecFormatError(f"frontmatter is no longer a mapping between reads: {path}")
    return _swallowed_keys(match.group(1), data)


def _opens_a_frontmatter_block(path: Path) -> bool:
    """True when *path*'s first bytes open a `---` frontmatter block.

    Bytes, and only the head of them: this runs on a file the parser has ALREADY refused, so it
    cannot go back through the parser, and an unreadable file must not turn a fail-closed
    verdict into a crash. A leading BOM is stripped because that is the whole point - the BOM is
    invisible in every editor and is exactly what makes a well-formed file look empty of
    frontmatter. Anything unreadable here answers True, which fails closed, which is the same
    answer the caller would have reached anyway.

    A UTF-16 FILE IS BROKEN WHATEVER ITS CONTENT, and asking that question here is the whole of
    issue #239's first item. Stripping only ``BOM_UTF8`` answered False for a BOM-less UTF-16LE
    spec, and that file had already slipped every other guard: NUL is legal UTF-8, so it decodes
    without error into mojibake and no decode-layer handler ever sees it. False here made it an
    ordinary frontmatter-less `speccing` record with NO warning emitted anywhere, and a plan
    sidecar promoted it - a dispatchable record carrying a state nobody wrote, while the file on
    disk plainly reads `current_state: done` in any editor.

    The answer belongs here rather than behind another `except`, because this function is
    already the one that owns "is this file broken or merely frontmatter-less" and it already
    answers it on the bytes. Widening it routes the file into the EXISTING broken-spec path -
    the one that logs `unusable spec frontmatter, skipping: %s (%s)` and registers the slug in
    `broken_spec_states`, which is what makes the plan pass fail closed instead of promoting.

    THE BROADER PHRASING IS DELIBERATE: "is this UTF-16", not "does it open `---` in UTF-16". A
    UTF-16 spec is unusable to every reader in this module regardless of what it says, so a
    UTF-16 file with genuinely no frontmatter is now reported broken where it used to survey as
    a `speccing` record with a mojibake title. That is a behavior change on a file that
    previously produced a record, and it is the intended one - the record was never usable.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(_ENCODING_SNIFF_BYTES)
    except OSError:
        return True
    if _looks_like_utf16(head):
        return True
    return head.lstrip(codecs.BOM_UTF8).lstrip().startswith(b"---")


def _blocks_a_plan(state: _StaleSpecState) -> bool:
    """True when this skipped spec must not let a plan sidecar decide the record's state."""
    return state.error is not None or _is_terminal(state.current_state)


def _fold_spec_state(
    index: dict[str, _StaleSpecState], slug: str, state: _StaleSpecState
) -> None:
    """Merge one answer into a slug index. The BLOCKING answer wins a collision.

    Two skipped specs can declare the same slug (pass 1's own collision case, one cutoff
    later). Blocking wins because it is what pass 1 would have concluded had both files been
    fresh: ``_merge_spec_record`` keeps the more-progressed record, so the terminal one becomes
    the record and freezes the plan. A rule that let the other one answer would make stale and
    fresh disagree, in the reopening direction.
    """
    prior = index.get(slug)
    if prior is None or (_blocks_a_plan(state) and not _blocks_a_plan(prior)):
        index[slug] = state


def _record_broken_spec(
    broken: dict[str, _StaleSpecState],
    reported: set[str],
    path: Path,
    exc: BaseException,
) -> None:
    """Remember a spec pass 1 rejected, and note that its skip line is already on the record.

    The slug is the filename's: a spec whose frontmatter would not parse has no declared slug
    to offer, which is the same fallback pass 1 already trusts for a spec that has none.

    Registering it as REPORTED is the point of the second argument. Pass 1 has just logged this
    file, so the plan pass must not log it again - `counted N, skipped M` counts lines, and one
    broken file named twice reads as two broken files.
    """
    slug = _slug_from_filename(path)
    broken.setdefault(slug, _StaleSpecState(path=path, error=exc))
    reported.add(slug)


class _SkippedSpecs:
    """Answers, for ONE slug, "is there a spec pass 1 could not use that must not be promoted".

    Two populations, both of them specs that never became a record, so both of them leave
    ``existing is None`` and open the same hole:

      * ``broken`` — specs pass 1 read and REJECTED (malformed YAML, non-mapping frontmatter).
        Free: the exception and the path are already in hand, and the slug of a spec whose
        frontmatter would not parse is only ever as good as its filename anyway.
      * ``paths``  — specs pass 1 SKIPPED ON MTIME. These cost a read, so they are rationed.

    READS ARE RATIONED TWICE, because the cutoff exists to avoid exactly this work. Nothing is
    opened until a plan actually asks. An ask is then answered first from the skipped paths
    whose FILENAME already resolves to the asked slug - one read in the shape that matters, a
    stale terminal spec being frozen by its own sidecar - and a blocking answer there settles
    it, since blocking wins a collision regardless of what else declares the slug.

    THE FULL PASS IS THE PRICE OF BEING RIGHT, and it is bounded: built at most once per
    survey, cached, and only when no filename-shaped candidate blocked. It cannot be dropped,
    because a spec's declared slug need not resemble its filename - 25 of the 462 specs in the
    live corpus declare one that does not - so a filename-only rule would answer "nothing
    skipped here" for exactly the renamed, oldest files this fix exists to catch.
    """

    def __init__(self, paths: list[Path], broken: dict[str, _StaleSpecState]) -> None:
        self._paths = paths
        self._broken = broken
        self._read: dict[Path, tuple[str, _StaleSpecState]] = {}
        self._full: Optional[dict[str, _StaleSpecState]] = None

    def _state(self, path: Path) -> tuple[str, _StaleSpecState]:
        got = self._read.get(path)
        if got is None:
            got = _stale_spec_state(path)
            self._read[path] = got
        return got

    def get(self, slug: str) -> Optional[_StaleSpecState]:
        """The answer for *slug*, or None when nothing skipped speaks for it."""
        broken = self._broken.get(slug)
        if broken is not None:
            return broken
        candidates: dict[str, _StaleSpecState] = {}
        for path in self._paths:
            if _slug_from_filename(path) == slug:
                _fold_spec_state(candidates, *self._state(path))
        hit = candidates.get(slug)
        if hit is not None and _blocks_a_plan(hit):
            return hit
        if self._full is None:
            full: dict[str, _StaleSpecState] = {}
            for path in self._paths:
                _fold_spec_state(full, *self._state(path))
            self._full = full
        return self._full.get(slug)


def _merge_spec_record(existing: EpicRecord, new: EpicRecord) -> EpicRecord:
    """Merge two same-slug records (spec-spec collision), keeping more-progressed.

    Emits a WARNING when the source paths differ (a genuine slug collision).
    Ported from the former ``lifecycle._merge_record`` (removed by gh#378), extended to carry the raw
    ``current_state`` + the two sidecar fields.
    """
    # A terminal spec outranks a non-terminal sibling regardless of lifecycle precedence.
    # Without this, the same reopening the plan pass now guards against survives here: both
    # `superseded` and `shipped` are missing from `_LIFECYCLE_PRECEDENCE`, so against another
    # unlisted token (`approved-for-autonomous`) the comparison is -999 > -999 = False and the
    # winner falls out of glob order -- i.e. filename. A later respec could silently reopen a
    # superseded epic because it sorted second.
    existing_terminal = _is_terminal(existing.current_state)
    new_terminal = _is_terminal(new.current_state)
    if existing_terminal != new_terminal:
        winner, loser = (existing, new) if existing_terminal else (new, existing)
        promoted = winner.lifecycle_state
    else:
        promoted = _promote(existing.lifecycle_state, new.lifecycle_state)
        if promoted != new.lifecycle_state:
            winner, loser = existing, new
        else:
            winner, loser = new, existing

    if loser.source_path != winner.source_path:
        # A drafted loser is a spec nobody has promoted yet, so it reaches no queue by any other
        # route: name it as shadowed rather than leave it to read like a routine merge.
        shadowed = ""
        if loser.current_state == "drafted":
            shadowed = (f"; the DRAFTED spec {loser.source_path} is shadowed and never reaches "
                        "the queue - give it a unique slug")
        _LOG.warning(
            "slug collision for %r: keeping %s (lifecycle=%s) over %s (lifecycle=%s)%s",
            existing.slug,
            winner.source_path,
            winner.lifecycle_state,
            loser.source_path,
            loser.lifecycle_state,
            shadowed,
        )

    return EpicRecord(
        slug=existing.slug,
        title=winner.title or loser.title,
        lifecycle_state=promoted,
        current_state=winner.current_state or loser.current_state,
        depends_on=winner.depends_on or loser.depends_on,
        priority=winner.priority if winner.priority != UNSET_PRIORITY else loser.priority,
        domain=winner.domain or loser.domain,
        project=winner.project or loser.project,
        queue_class=winner.queue_class or loser.queue_class,
        build_approved_by=winner.build_approved_by or loser.build_approved_by,
        last_modified=max(existing.last_modified, new.last_modified),
        source_path=winner.source_path,
        progress=new.progress or existing.progress,
        current_phase=new.current_phase or existing.current_phase,
        paused=winner.paused or loser.paused,
    )


# --------------------------------------------------------------------------- #
# Public API — the disk survey
# --------------------------------------------------------------------------- #
def survey_epics(
    data_root: Path | str,
    mtime_cutoff_iso: str,
    include_archived: bool = False,
    patterns: SurveyPatterns = DEFAULT_SURVEY_PATTERNS,
    project: Optional[str] = None,
) -> list[EpicRecord]:
    """Survey the filesystem and merge lifecycle records from design/specs, design/plans and status.

    Parameters
    ----------
    data_root:
        Single root under which ``patterns`` glob. Config-resolved (Phase 1
        points at the current layout via config; Phase 2 re-points config only).
    mtime_cutoff_iso:
        ISO 8601 timestamp. Files with mtime strictly before this are skipped.
        Must be timezone-aware (or bare UTC with Z/+00:00).
    include_archived:
        When *False* (default), records whose final ``lifecycle_state`` is
        ``archived`` are excluded from the returned list.
    patterns:
        Root-relative glob patterns for the three sources. Defaults to the
        Phase-2 target layout.
    project:
        When set, glob ONLY that project's subtree (``patterns.for_project(project)``)
        — what a builder loop wants: it must never see, let alone dispatch,
        another project's work. When *None* (default) the survey is
        project-wide, exactly as before: dev-manager's refinement pass,
        ``choose-next`` and ``backlog`` genuinely want the full set.
        Glob scope is only the first of two layers — a misfiled spec still
        needs ``dispatchable(..., project=...)`` to drop it on the record.

    Returns
    -------
    list[EpicRecord]
        One record per unique slug, merged across all sources.
    """
    root = Path(data_root)
    if project is not None:
        patterns = patterns.for_project(project)
    cutoff = datetime.fromisoformat(mtime_cutoff_iso.replace("Z", "+00:00"))
    by_slug: dict[str, EpicRecord] = {}
    # Specs pass 1 skipped on mtime. PATHS ONLY — collecting a path costs no read, and the
    # plan pass reads these lazily and only for the one record a plan would otherwise decide.
    stale_spec_paths: list[Path] = []
    # Specs pass 1 READ and rejected. Same hole, different cause: a rejected spec becomes no
    # record either, so `existing is None` and the plan promotes unconditionally - and this is
    # the half that does NOT need the cutoff to be reachable, since a fresh file is where a
    # typo lives. Costs nothing to collect: the exception is already in hand.
    broken_spec_states: dict[str, _StaleSpecState] = {}
    # OFFENDERS ONLY: fresh, non-terminal specs that were read and carry no `## Files` section,
    # keyed by slug and holding the SPEC's own path. Decided in this pass because this is the
    # only pass holding a spec: `source_path` is overwritten by the plan pass, so a warn-time
    # re-read of it opens a PLAN for a record whose plan won, and would report that plan as a
    # spec with no section.
    #
    # MEMBERSHIP IS THE WHOLE ANSWER, which is why this is a set-shaped dict and not a verdict
    # per slug. The non-offending outcomes - never read (terminal, or no spec behind the record),
    # read and declared, read and undecodable, and read ATTEMPTED AND FAILED - are one thing to
    # the only consumer: nothing to report. Carrying them as distinct values made the warn-time
    # test read `declared is None or declared[0] is not False`, a double negative over a state no
    # file CONTENT can produce - `parse_spec_frontmatter` reads the same bytes ~50 lines earlier
    # and its handler `continue`s - so a reader had to re-derive that before trusting the line.
    # It is `is False` and not falsiness because content is not the only source of `None`. This
    # path is read three times per non-terminal spec (`_read_sidecar`, then the parse, then the
    # declares check); the parse and the declares check are two of those, and a spec LOCKED by
    # another lane between them returns `None` here on a board several lanes write to. Deletion
    # and rename reach the same arm; before gh#361 they were not observable through it, because
    # the record built two statements later re-stat'ed the path and the `FileNotFoundError` took
    # the whole survey with it. The record now reuses the loop-head stat, so those reach the
    # fold too and the survey survives to report them.
    # `_spec_declares_files` still ANSWERS the tri-state - "could not look" is not "has none" -
    # and the fold happens here, at the one call site, rather than being carried onward (gh#311).
    files_missing: dict[str, Path] = {}
    # The same fold for a heading that declares no backticked path (`DECLARES_NO_PATHS`). A
    # separate map, so each offender gets its own message and the two never mask each other.
    files_pathless: dict[str, Path] = {}
    # THE SAME ANSWER, KEYED BY FILE rather than by slug (gh#359). `files_missing` is
    # maintained in glob order; the surviving record is chosen by lifecycle precedence, and
    # for two same-slug specs at different states those disagree. This map lets the merge
    # below re-point the offender entry at whichever file actually won, so the warning
    # describes the record the rest of the survey returns.
    spec_declares: dict[Path, object] = {}
    # Slugs whose skip line is already on the record, so one broken file is reported once
    # rather than once per plan sidecar pointing at it (`counted N, skipped M` counts lines).
    reported_skips: set[str] = set()

    # ------------------------------------------------------------------ #
    # 1) refine / spec files → speccing (unless current_state overrides)
    # ------------------------------------------------------------------ #
    for pattern in patterns.spec_globs:
        for f in sorted(root.glob(pattern)):
            if f.name.endswith(".status.md"):
                continue
            # THE GLOB IS A SNAPSHOT, THE STAT IS A SECOND LOOK. Between them another lane can
            # delete or rename the file, and an unguarded stat took the WHOLE survey down with
            # it (gh#361) - every seat calls this at every wake, so one lane's ordinary cleanup
            # blinded the board. Costs the file, not the board.
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            except OSError as exc:
                _LOG.warning("vanished before stat, skipping: %s (%s)", f, exc)
                # REGISTERED, NOT MERELY SKIPPED - the spec pass is the only pass where a bare
                # `continue` is wrong. A spec that never becomes a record leaves
                # `existing is None` in the plan pass, and that pass then promotes the slug's
                # plan sidecar `approved-for-autonomous` UNCONDITIONALLY (see `_SkippedSpecs`).
                # Every other "spec skipped" branch registers for exactly that reason, and a
                # vanished spec is the same population: skipping registration here would trade
                # a loud crash for a silent wrong promotion of merged work.
                _record_broken_spec(broken_spec_states, reported_skips, f, exc)
                continue
            if mtime < cutoff:
                stale_spec_paths.append(f)
                continue
            priority, domain, project, queue_class, build_approved_by = _read_sidecar(f)
            try:
                sf = parse_spec_frontmatter(f)
                slug = sf.slug
                title = sf.title
                depends_on = list(sf.depends_on)
                current_state = sf.current_state
                # Normalize the value that becomes `lifecycle_state`. It used to be copied
                # raw, so `current_state: Done` produced lifecycle `Done`, which misses both
                # `_LIFECYCLE_PRECEDENCE` and `_done_slugs` -- the epic reads closed for
                # approval and still-open for dependency math, and every dependant is blocked
                # forever. That is issue #1 mirrored: instead of shipped work reappearing,
                # downstream work never appears.
                state: str = _norm_state(sf.current_state) or "speccing"
            except SpecFormatError as exc:
                # TWO UNLIKE FILES ARRIVE HERE, and only the bytes tell them apart. A spec
                # that genuinely carries no `---` block is an ordinary corpus shape and stays
                # a `speccing` record. A spec that OPENS a block the parser then refused - a
                # UTF-8 BOM ahead of the `---` is enough, and this is a Windows fleet whose
                # default `Out-File` writes one - is broken, and reading it as "no
                # frontmatter" hands back `current_state=None`, which is not terminal, so a
                # `current_state: done` that any human can read in the file lets the plan
                # sidecar promote it (issue #42, reached without the cutoff being involved).
                if _opens_a_frontmatter_block(f):
                    _LOG.warning("unusable spec frontmatter, skipping: %s (%s)", f, exc)
                    _record_broken_spec(broken_spec_states, reported_skips, f, exc)
                    continue
                # No frontmatter — derive slug from filename, treat as speccing.
                slug = _slug_from_filename(f)
                title = slug
                depends_on = []
                current_state = None
                state = "speccing"
            except yaml.YAMLError as exc:
                _LOG.warning("malformed spec yaml, skipping: %s (%s)", f, exc)
                # A REJECTED SPEC LEAVES THE SAME HOLE A SKIPPED ONE DOES. `continue` means no
                # record, so the plan pass sees `existing is None` and promotes the sidecar's
                # `approved-for-autonomous` unconditionally - issue #42 again, on the half that
                # needs no cutoff to reach, since a fresh file is where a typo lives.
                _record_broken_spec(broken_spec_states, reported_skips, f, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("malformed spec file, skipping: %s (%s)", f, exc)
                _record_broken_spec(broken_spec_states, reported_skips, f, exc)
                continue

            # READ RATIONED BY STATE, not by hope. A terminal spec is never promoted again, so
            # its `## Files` section is nobody's business and the file is not opened. Every
            # non-terminal spec is read, not only one already sitting in
            # FILES_GATED_CURRENT_STATES, because the plan pass can carry a `speccing` spec up
            # to `approved-for-autonomous` (`pf.current_state` wins over the spec's), and the
            # population is settled on the MERGED record, below.
            if not _is_terminal(current_state):
                # ONE READ, BOUND ONCE, folded here. `is False` and not falsiness: `None` is
                # "could not decode", which must not be reported as a missing section.
                declares = _spec_declares_files(f)
                spec_declares[f] = declares
                files_pathless.pop(slug, None)
                if declares is DECLARES_NO_PATHS:
                    files_pathless[slug] = f
                if declares is False:
                    files_missing[slug] = f
                else:
                    # Declared, pathless (reported separately) or unreadable: none of them is
                    # a missing heading. LAST FILE WINS, and clearing is half of that. Two spec
                    # files can carry the same slug (they merge below), and this dict used to be
                    # assigned unconditionally, so a later sibling that DOES declare a section
                    # overwrote an earlier one that did not. Recording offenders only would
                    # silently make that first sibling stick and warn about a slug the
                    # pre-change code was silent on, so the non-offending arm has to erase, not
                    # skip. What it restores is the old dict's semantics exactly. It is NOT a
                    # claim that the merged record declares a section: `_merge_spec_record`
                    # picks by lifecycle precedence, not read order, so for two same-slug specs
                    # at different states the surviving record can be the earlier file either
                    # way (pre-existing).
                    files_missing.pop(slug, None)

            rec = EpicRecord(
                slug=slug,
                title=title,
                lifecycle_state=state,
                current_state=current_state,
                depends_on=depends_on,
                priority=priority,
                domain=domain,
                project=project,
                queue_class=queue_class,
                build_approved_by=build_approved_by,
                # THE STAT ALREADY IN HAND, not a second one. This used to re-stat `f`, which
                # is the same file the loop head stat'ed a few statements earlier - so a file
                # deleted in between raised `FileNotFoundError` straight out of `survey_epics`
                # (gh#361). Reusing the head-stat value closes that window BY CONSTRUCTION
                # rather than catching it, and the string is byte-identical: same `datetime`,
                # same `.isoformat()`.
                last_modified=mtime.isoformat(),
                source_path=str(f),
            )
            if slug in by_slug:
                previous = by_slug[slug]
                by_slug[slug] = _merge_spec_record(previous, rec)
                # RE-POINT THE OFFENDER ENTRY AT THE WINNER (gh#359). On a TIE the merge
                # itself falls back to read order, so the winner is this file and nothing
                # moves -- gh#311's dup tests pin that and stay pinned. What changes is the
                # case where precedence overrules read order: the survey then returns a
                # record whose own file the glob-order dict never judged.
                winner_path = Path(by_slug[slug].source_path)
                if winner_path in spec_declares:
                    files_pathless.pop(slug, None)
                    if spec_declares[winner_path] is DECLARES_NO_PATHS:
                        files_pathless[slug] = winner_path
                    if spec_declares[winner_path] is False:
                        files_missing[slug] = winner_path
                    else:
                        # Declared, pathless (reported separately) or unreadable. None is a
                        # missing heading, and clearing is half of the answer exactly as above.
                        files_missing.pop(slug, None)
            else:
                by_slug[slug] = rec

    # Every spec that did NOT become a record, in one place: skipped on mtime, or read and
    # rejected. Holds paths and exceptions only — no file is opened until a plan asks.
    skipped_specs = _SkippedSpecs(stale_spec_paths, broken_spec_states)

    # ------------------------------------------------------------------ #
    # 2) solution / plan files → planning, promoted against spec record
    # ------------------------------------------------------------------ #
    for pattern in patterns.plan_globs:
        for f in sorted(root.glob(pattern)):
            if f.name.endswith(".status.md"):
                continue
            # Same glob-then-stat window as pass 1 (gh#361). A plain `continue` IS right here: a
            # plan that never becomes a record leaves the slug to whatever its spec said,
            # which is the pre-plan truth, not a promotion.
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            except OSError as exc:
                _LOG.warning("vanished before stat, skipping: %s (%s)", f, exc)
                continue
            if mtime < cutoff:
                continue
            priority, domain, project, queue_class, build_approved_by = _read_sidecar(f)
            try:
                pf = parse_plan(f)
            except DAGError as exc:
                _LOG.warning("malformed plan, skipping: %s (%s)", f, exc)
                continue
            except yaml.YAMLError as exc:
                _LOG.warning("malformed plan yaml, skipping: %s (%s)", f, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("malformed plan file, skipping: %s (%s)", f, exc)
                continue

            slug = pf.slug or _slug_from_filename(f)
            existing = by_slug.get(slug)
            # Normalized for the same reason as the spec pass: this string is compared against
            # `_LIFECYCLE_PRECEDENCE` keys, which are all lowercase.
            promoted_state: str = _norm_state(pf.current_state) or "planning"

            # A TERMINAL spec state freezes BOTH fields, not just the approval one. Fixing
            # `current_state` alone left the identical reopening live on `lifecycle_state` --
            # and that is the field that actually matters downstream: the archived filter and
            # choose-next's candidate bucket key on it, and `_promote` happily lifts a spec
            # sitting at `archived` (-1) up to a plan's `planning` (1). A plan with NO
            # current_state defaults to "planning" here, which is the common corpus shape, so
            # this fired on real archived specs that were being offered as live candidates.
            spec_is_terminal = existing is not None and _is_terminal(existing.current_state)
            frozen_lifecycle = existing.lifecycle_state if existing else None
            frozen_current = existing.current_state if existing else None

            # THE HOLE THE FREEZE ABOVE CANNOT SEE (issue #42). The guard reads `existing`, so
            # it only engages for a spec pass 1 actually kept. A spec that never became a
            # record — skipped on mtime, or read and rejected — leaves `existing is None`, the
            # freeze never fires, and the plan sidecar's `current_state` wins UNCONDITIONALLY:
            # a shipped epic surveys as `approved-for-autonomous` again, and `dispatchable()`
            # is the advertised single enforcer of the approval gate, so nothing downstream
            # catches it — the record reaches a lane, is claimed, built and auto-merged. It
            # also grows on its own, because `DEFAULT_MTIME_CUTOFF` is a fixed date rather than
            # a rolling window, so specs age past it permanently while their plan sidecars keep
            # being touched.
            #
            # NOT A CUTOFF BYPASS. Asking is free — the rejected specs are already in hand and
            # the skipped ones are paths, not reads. A read happens only on THIS path, a plan
            # about to supply the state of a record with no spec behind it, and is answered
            # from the one skipped file whose name already resolves to the slug wherever that
            # file settles the question (see `_SkippedSpecs`). A skipped spec with no plan is
            # still skipped, and what the survey returns for it is unchanged. The question
            # asked is the narrowest one that closes the hole: was it terminal.
            if existing is None:
                stale = skipped_specs.get(slug)
                if (
                    stale is not None
                    and stale.error is not None
                    and _is_approved(pf.current_state)
                ):
                    # FAIL CLOSED, AND ONLY WHERE FAILING CLOSED BUYS SOMETHING. A spec that
                    # cannot be read cannot be shown non-terminal, and the cost of the two
                    # mistakes is not symmetric: promoting wrongly re-ships merged work with no
                    # second gate, while withholding wrongly leaves an epic unoffered until
                    # someone fixes a file that is already broken.
                    #
                    # THE APPROVAL TEST IS WHAT KEEPS THAT FROM BECOMING PURE LOSS. `existing`
                    # is None here, so this record's `current_state` would be the plan's own -
                    # which means `_is_approved(pf.current_state)` is exactly "would this record
                    # have passed dispatchable()'s approval gate". A plan sitting at `planning`
                    # was never claimable, so dropping it buys no gate safety at all and merely
                    # deletes an in-flight epic from the board because an unrelated file has a
                    # typo. Those fall through unfrozen, exactly as before this fix existed.
                    #
                    # The drop is logged through the same `, skipping: %s (%s)` shape every
                    # other skip uses, so the report's `counted N, skipped M` sees it instead of
                    # losing a record silently - and once per broken spec, not once per plan
                    # sidecar pointing at it.
                    if slug not in reported_skips:
                        reported_skips.add(slug)
                        _LOG.warning(
                            "unreadable stale spec, plan not promoted, skipping: %s (%s)",
                            stale.path,
                            stale.error,
                        )
                    continue
                if stale is not None and _is_terminal(stale.current_state):
                    spec_is_terminal = True
                    # Exactly what pass 1 would have put on the record it skipped, and no more:
                    # `lifecycle_state` is the normalized `current_state` (pass 1's own rule),
                    # so `archived` still falls to the archived filter below just as it would
                    # have. Title, priority, domain, paths and every other field stay the
                    # plan's — the spec is consulted, not resurrected.
                    frozen_lifecycle = _norm_state(stale.current_state)
                    frozen_current = stale.current_state

            merged = EpicRecord(
                slug=slug,
                title=pf.epic or (existing.title if existing else slug),
                lifecycle_state=(
                    frozen_lifecycle
                    if spec_is_terminal
                    else _promote(
                        existing.lifecycle_state if existing else None,
                        promoted_state,
                    )
                ),
                # The spec is the record of intent-complete, so a TERMINAL spec state wins over
                # whatever the plan sidecar still says. Reconciling a shipped epic means editing
                # the spec; nobody also edits the plan, and before this the plan won
                # unconditionally - so `done` specs kept surveying as approved-for-autonomous.
                current_state=(
                    frozen_current
                    if spec_is_terminal
                    else pf.current_state or (existing.current_state if existing else None)
                ),
                depends_on=pf.depends_on or (existing.depends_on if existing else []),
                priority=priority if priority != UNSET_PRIORITY
                else (existing.priority if existing else UNSET_PRIORITY),
                domain=domain or (existing.domain if existing else None),
                project=pf.project or project or (existing.project if existing else None),
                queue_class=queue_class or (existing.queue_class if existing else None),
                build_approved_by=build_approved_by or (existing.build_approved_by if existing else None),
                last_modified=max(
                    mtime.isoformat(),   # the loop-head stat, not a second one (gh#361)
                    existing.last_modified if existing else "",
                ),
                source_path=str(f),
                progress=existing.progress if existing else None,
                current_phase=existing.current_phase if existing else None,
                paused=existing.paused if existing else False,
            )
            by_slug[slug] = merged

    # ------------------------------------------------------------------ #
    # 3) status.md files → done / an ended status / running; status.md ALWAYS wins.
    #    Two filename conventions, both gated on the `epic:` signature,
    #    walked SKIP_DIRS-respecting.
    # ------------------------------------------------------------------ #
    status_paths: dict[str, Path] = {}
    for pattern in patterns.status_dir_globs:
        for status_root in sorted(root.glob(pattern)):
            if not status_root.is_dir():
                continue
            # Convention 1: exact-name `status.md` → slug from parent dir name.
            for p in sorted(_rwalk_match(status_root, lambda n: n == "status.md")):
                if not is_orchestrator_status(p):
                    continue
                slug = _normalize_slug_stem(p.parent.name)
                if slug:
                    status_paths[slug] = p
            # Convention 2: `<slug>.status.md` → slug from filename stem.
            for p in sorted(
                _rwalk_match(status_root, lambda n: n.endswith(".status.md") and n != "status.md")
            ):
                if not is_orchestrator_status(p):
                    continue
                stem = p.name[: -len(".status.md")]
                slug = _normalize_slug_stem(stem)
                if slug:
                    status_paths[slug] = p

    for slug in sorted(status_paths):
        f = status_paths[slug]
        # Same walk-then-stat window as passes 1 and 2 (gh#361). A plain `continue` is right here
        # too: a status board that never becomes a record leaves the slug at whatever its spec
        # and plan already said.
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except OSError as exc:
            _LOG.warning("vanished before stat, skipping: %s (%s)", f, exc)
            continue
        if mtime < cutoff:
            continue
        try:
            state_obj = read_status(f)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("malformed status, skipping: %s (%s)", f, exc)
            continue

        existing = by_slug.get(slug)
        ls: LifecycleState
        if state_obj.status == "done":
            ls = "done"
        elif state_obj.status in _ENDED_BOARD_STATUSES:
            ls = state_obj.status
        else:
            ls = "running"
        task_list = state_obj.tasks
        total = len(task_list) or 1
        done_count = sum(1 for t in task_list.values() if t.state == "done")

        by_slug[slug] = EpicRecord(
            slug=slug,
            title=state_obj.epic or (existing.title if existing else slug),
            lifecycle_state=ls,
            current_state=existing.current_state if existing else None,
            depends_on=existing.depends_on if existing else [],
            priority=existing.priority if existing else UNSET_PRIORITY,
            domain=existing.domain if existing else None,
            project=existing.project if existing else None,
            # Carry the sidecar gates through pass 3. Dropping them here made the
            # queue_class allow-list and the excluded_domains grant unreachable for
            # exactly the epics that already have a status board - i.e. anything
            # in flight or previously dispatched, the class the audit was about.
            queue_class=existing.queue_class if existing else None,
            build_approved_by=existing.build_approved_by if existing else None,
            last_modified=max(
                mtime.isoformat(),   # the loop-head stat, not a second one (gh#361)
                existing.last_modified if existing else "",
            ),
            source_path=str(f),
            progress={
                "done": done_count,
                "total": total,
                "percent": round(done_count * 100 / total),
            },
            current_phase=state_obj.current_phase,
            paused=state_obj.paused,
        )

    # ------------------------------------------------------------------ #
    # 4) Filter archived (unless include_archived=True)
    # ------------------------------------------------------------------ #
    out = list(by_slug.values())
    if not include_archived:
        out = [r for r in out if r.lifecycle_state != "archived"]
    _warn_unknown_queue_classes(out)
    _warn_specs_without_files(out, files_missing)
    _warn_specs_declaring_no_paths(out, files_pathless)
    return out


# --------------------------------------------------------------------------- #
# Thin dep-math helpers — the logic choose-next / lead-dev ran inline, now one
# shared surface (no new behavior).
# --------------------------------------------------------------------------- #
def _done_slugs(records: list[EpicRecord]) -> set[str]:
    """Slugs whose epic is FINISHED, and which therefore satisfy a ``depends_on`` naming them.

    DERIVED FROM `TERMINAL_CURRENT_STATES`, NEVER RESTATED. This spelled `("done", "archived")`
    inline - a subset of the module's own terminal vocabulary - so a prerequisite marked
    `superseded` or `shipped` was terminal to every other reader here and invisible to the
    dependency math. Every epic naming it stranded silently and permanently: `ready_set` hid it
    forever, and `blocked_status` fell past every named bucket to the default `CHAINED`, which
    tells the operator "deeper queue ahead, it will unblock" about a prerequisite that never
    will. The last occurrence was found only by hand-resolving six dependency slugs. A second
    list is how the two answers drifted - the same "borrow the vocabulary, never restate it"
    rule `reconcile` states in its own docstring (gh#261).

    `shipped` IS INCLUDED, DELIBERATELY, and not merely swept up by the widening. It carries
    exposure identical to `superseded` - terminal by the module's own definition, unseen by the
    dep math - and the corpus holds a record at it. Excluding it "because no epic depends on one
    today" would have made this function a THIRD subset of the same vocabulary, which is the
    defect rather than a cautious version of the fix.

    NO LIVE VICTIM WAS BEING RELEASED HERE. Measured at claim time: 578 records across all
    projects, 40 of them terminal-but-not-done, and NOTHING newly ready either way. The defect
    was dormant. It earns a fix because its trigger is the SANCTIONED remedy - split a too-wide
    parent into children, mark the parent `superseded` - so it recurs by doing the right thing,
    and it fails silently when it does.

    THE FIELD STAYS `lifecycle_state`; only the vocabulary widened. Reading `current_state`
    instead looks equivalent (no record today is terminal by `current_state` while
    non-terminal by `lifecycle_state` - 0 of 578) and is
    wrong: pass 3 sets `lifecycle_state` from a status.md while carrying `current_state` through
    untouched, so a superseded spec with a live status board reads `lifecycle=running,
    current=superseded`, and a `current_state`-based done-set would call that finished while a
    lane is actively building it. `warn_stranded_terminal_dependencies` REPORTS that divergence
    rather than resolving it here.
    """
    return {r.slug for r in records if r.lifecycle_state in TERMINAL_CURRENT_STATES}


def ready_set(records: list[EpicRecord]) -> list[EpicRecord]:
    """Records whose every ``depends_on`` slug names a TERMINAL epic (see ``_done_slugs``).

    choose-next "READY"; lead-dev "unblocked".

    PASS THE WHOLE CORPUS. The done-set is derived from *the list handed in*, not from the
    survey, so filtering before this call silently corrupts the dependency math: a
    prerequisite that got dropped by the filter is not "done" as far as this function can
    tell, and every epic depending on it is reported blocked forever. It fails closed - work
    is stranded, never mis-dispatched - which is why it stays invisible.

    That is not hypothetical and it is not new. ``dispatchable()`` has always dropped
    ``local-tooling`` / ``on-server-ops`` / foreign-project records, so a *done* prerequisite
    carrying one of those was already unseeable under ``ready_set(dispatchable(...))``. Adding
    the approval filter to ``dispatchable()`` turned that latent hole into a total one, because
    a ``done`` record is by definition not approved and so the done-set collapsed to empty.

    Builders should not compose this by hand at all - call ``selectable()``, which orders the
    two correctly and is the only expression the skills publish.

    A FORBIDDEN EDGE DOES NOT RESOLVE HERE EITHER, and this is the one project-shaped thing
    this function knows. The rule needs no scope argument - it compares a record's own
    ``project:`` against its dep's, so it reads the same from every survey - which is precisely
    why it belongs here rather than only in ``selectable``. Enforcing it one layer up left the
    human-facing path out: ``choose-next`` calls ``ready_set`` and ``blocked_status`` and never
    ``selectable``, deliberately project-wide, so a cross-project carrier bucketed READY for the
    operator while every builder refused it forever - the same "one record, two answers, decided
    by who was looking" split this feature exists to close, just moved. The done-set itself is
    untouched: exclusion is applied to the OUTPUT, so a carrier that is finished still counts as
    a satisfied prerequisite for everyone else (`_done_slugs` reads *records*, not this return
    value). See `forbidden_cross_project_dependencies` for what makes an edge forbidden and for
    why both sides must be tagged before a difference means anything.
    """
    done = _done_slugs(records)
    forbidden = _forbidden_carriers(records)
    return [
        r for r in records
        if r.slug not in forbidden and all(d in done for d in r.depends_on)
    ]


# queue_class ALLOW-list: the only values a builder loop may dispatch (unset
# counts as dispatchable — legacy specs predate the field).
#
# The value was renamed on 2026-09-05 as a HARD cut with no dual-accept window: the
# retired spelling is not accepted here for a transition period, it is simply gone, and a
# spec still carrying it surveys as out-of-vocabulary and dispatches nothing until
# `agentflow.migrate_queue_class` rewrites it. The retired spelling lives in exactly one
# place, `agentflow.vocabulary.RETIRED_QUEUE_CLASS`, and is deliberately NOT written here
# — this constant is where every reader comes to answer "what value dispatches?", and the
# answer has to be one line long.
DISPATCHABLE_QUEUE_CLASSES = frozenset({"fleet-buildable"})

# queue_class values that are KNOWN and NOT autonomously dispatchable — built by
# hand in a local session, run as manual on-server ops, or need a brainstorm
# first. Kept as its own set so the survey warning can tell "known, deliberately
# not buildable" apart from "out of vocabulary".
NON_DISPATCHABLE_QUEUE_CLASSES = frozenset({"local-tooling", "on-server-ops", "brainstorm-pin"})

# The whole queue_class vocabulary. Anything else is a typo or issue-tracker
# leakage ('bug' / 'feature'): non-dispatchable, and warned about once.
KNOWN_QUEUE_CLASSES = DISPATCHABLE_QUEUE_CLASSES | NON_DISPATCHABLE_QUEUE_CLASSES


def _warn_unknown_queue_classes(records: list[EpicRecord]) -> None:
    """Log ONE warning per out-of-vocabulary ``queue_class`` value in *records*.

    A warning, never a raise: an unknown value makes an epic non-dispatchable,
    but it must not break dev-manager's refinement survey — which is exactly the
    survey that has to show the human the bad value so it can be fixed.
    """
    first_seen: dict[str, str] = {}
    for r in records:
        qc = r.queue_class
        if qc is None or qc in KNOWN_QUEUE_CLASSES:
            continue
        first_seen.setdefault(qc, r.source_path)
    for value in sorted(first_seen):
        _LOG.warning(
            "unknown queue_class, not dispatchable: %r (e.g. %s)", value, first_seen[value]
        )


def _warn_specs_without_files(
    records: list[EpicRecord],
    files_missing: dict[str, Path],
) -> None:
    """Log ONE warning per spec a promoting seat would have to refuse for a missing section.

    The standing Directive of 2026-08-31 says a spec without a ``## Files`` section may not be
    promoted, and nothing anywhere reported a violation. So a spec written from a template that
    carried no such heading surveyed clean, sat at `drafted` looking entirely ordinary, and
    spent an attended promotion pass - the fleet's scarcest resource - to produce nothing. Six
    such specs were in the corpus when this was written: FIVE from that one template, and one
    from the backlog skill, whose pins carry a different heading set and which the template fix
    therefore does not cover (gh#294). Fixing a producer closes the specs it produces, never
    the population - which is the reason this warning reports the population separately.

    A warning, never a raise, for `_warn_unknown_queue_classes`'s reason: a missing section
    makes an epic unpromotable, but it must not break the refinement survey, which is exactly
    the survey that has to SHOW the human the problem.

    ONE LINE PER OFFENDER, uncapped, unlike the neighbouring warnings that print an example and
    summarise the rest. Those report a shared value or a shared cause, where a second instance
    teaches the reader nothing; this one reports a per-file edit, and every line is a distinct
    file somebody has to open. A capped list would hide work rather than summarise it.

    *files_missing* HOLDS ONLY OFFENDERS (gh#311), so absence is the entire non-offending case:
    never read (terminal at spec-read time, or no spec at all behind this record), read and
    declared, read-but-undecodable, and read-attempted-but-failed are alike simply not in it. The survey folds those three
    at the one place that can tell them apart; this function does not re-derive the difference,
    and no reader of this loop has to.

    A NON-DISPATCHABLE `queue_class` IS OUTSIDE THE POPULATION (gh#338) - a subset of the
    exclusion `warn_unresolvable_dependencies` makes, which drops everything `dispatchable` does
    (unknown values, foreign and excluded-domain records too). The gate this predicts is the one in front of
    autonomous dispatch, and a `brainstorm-pin` / `local-tooling` / `on-server-ops` spec never
    passes through it: the whole live output of this warning was once a single `brainstorm-pin`
    whose missing files ARE its open fork. A channel whose only line is correctly-shaped work
    teaches its reader to skip it. An out-of-vocabulary value is NOT excluded - it has its own
    warning, and the spec is still headed for dispatch once the typo is fixed.
    """
    offenders: list[tuple[str, str, str]] = []
    for r in records:
        state = _norm_state(r.current_state)
        if state not in FILES_GATED_CURRENT_STATES:
            continue
        if r.queue_class in NON_DISPATCHABLE_QUEUE_CLASSES:
            continue
        path = files_missing.get(r.slug)
        if path is None:
            continue
        offenders.append((r.slug, state, str(path)))
    for slug, state, path in sorted(offenders):
        _LOG.warning(
            "spec has no `## Files` section, not promotable: %s (%s: %s)", slug, state, path
        )


def _warn_specs_declaring_no_paths(
    records: list[EpicRecord],
    files_pathless: dict[str, Path],
) -> None:
    """Log ONE warning per gated spec whose ``## Files`` section declares no path.

    `plan_parser.files_section` extracts only backticked paths, so `- src/widget.py (new)` is a
    section that is present and names nothing: the missing-heading warning is silent on it, and
    `lead-dev`'s seam check finds no listing to prove disjointness from. The promoting seat reads
    the list by eye and sees a path, so without this line the human gate passes a spec the
    checker cannot use. Same population and same one-line-per-offender shape as
    `_warn_specs_without_files`, and a different message, because the edit is different: add
    backticks, not a heading. Same `queue_class` exclusion too, for the same reason (gh#338).
    """
    offenders: list[tuple[str, str, str]] = []
    for r in records:
        state = _norm_state(r.current_state)
        if state not in FILES_GATED_CURRENT_STATES:
            continue
        if r.queue_class in NON_DISPATCHABLE_QUEUE_CLASSES:
            continue
        path = files_pathless.get(r.slug)
        if path is None:
            continue
        offenders.append((r.slug, state, str(path)))
    for slug, state, path in sorted(offenders):
        _LOG.warning(
            "spec `## Files` section declares no path, seam not checkable: %s (%s: %s) - "
            "list one backticked, repo-relative path per entry; an un-backticked entry "
            "declares nothing",
            slug, state, path,
        )


#: `warn_unresolved_done_spec_paths`' two verdicts on a pointer that does not resolve. Kept
#: apart because the repairs are: a depth shift is a bulk, mechanical `../`; anything else is one
#: file somebody has to look at. NOT "typo": on the live corpus most of that second class is a
#: renamed or deleted file, a directory, or a path written against the data root or an old layout.
POINTER_DEPTH_SHIFT: str = "depth-shift"
POINTER_UNRESOLVED: str = "unresolved"

# A trailing `:120` / `:120-140` line reference is a location INSIDE the file, not part of its path.
_POINTER_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
_POINTER_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]+$")
# Home-, root- or drive-anchored: names something outside both the data root and the repo, so it
# is never stat'ed - resolving it would probe the reading machine, not the spec's own tree.
_ANCHORED_POINTER_RE = re.compile(r"^(?:[~/\\]|[A-Za-z]:)")
# The first backticked token of a bullet line: `- `skills/x.py` - why` declares `skills/x.py`.
_BULLET_LEAD_PATH_RE = re.compile(r"^[ \t]*[-*+][ \t]+`([^`\n]+)`", re.MULTILINE)


@dataclass(frozen=True)
class UnresolvedPointer:
    """One declared path in a `done` spec that names nothing on disk."""

    spec: Path
    slug: str
    source: str     # "provenance.evidence" or "## Files"
    pointer: str    # as written, before the fragment and line suffix are dropped
    kind: str       # POINTER_DEPTH_SHIFT or POINTER_UNRESOLVED


def _as_pointer(raw: str) -> Optional[str]:
    """The path part of *raw*, or None when *raw* is not a path at all.

    `provenance.evidence` is free text in the corpus - most values are a pointer with an anchor
    (`../../status/audits/x.md#slug`), some are a sentence ("measured on the live board"). A
    sentence is not a broken pointer, so anything with whitespace, a URL, a glob or a
    `<placeholder>` is not judged, and neither is an anchored path (`~/`, `/`, `C:`) - it points
    outside the trees this check is about. What is left must carry a separator or an extension.
    """
    value = raw.strip().split("#", 1)[0]
    value = _POINTER_LINE_SUFFIX_RE.sub("", value)
    if not value or any(ch.isspace() for ch in value) or "://" in value:
        return None
    if _ANCHORED_POINTER_RE.match(value):
        return None
    if any(ch in value for ch in "*?<>{}"):
        return None
    if "/" not in value and not _POINTER_EXTENSION_RE.search(value):
        return None
    return value


def _done_spec_pointers(
    spec: Path, data: dict, text: str, repo_root: Optional[Path]
) -> list[tuple[str, str, str]]:
    """``(source, as written, path)`` for every path a done spec declares that can be judged."""
    raw: list[tuple[str, object]] = []
    provenance = data.get("provenance")
    evidence = provenance.get("evidence") if isinstance(provenance, dict) else None
    # YAML hands back whatever the author typed: an unquoted date, a number, a bool or a mapping
    # are all legal frontmatter and none is a pointer. Iterating them raised (or walked a dict's
    # keys), and this function promises never to raise - so only text or a list is read.
    if isinstance(evidence, str):
        evidence_values: list = [evidence]
    elif isinstance(evidence, list):
        evidence_values = evidence
    else:
        evidence_values = []
        if evidence is not None:
            _LOG.warning(
                "done-spec pointer check: provenance.evidence is neither text nor a list, "
                "not judged: %s (%r)",
                spec, evidence,
            )
    raw.extend(("provenance.evidence", value) for value in evidence_values)
    # `## Files` entries are REPO-relative (`files_section`'s extraction rule), and specs live
    # under the data root, not in the repo. Without a repo to resolve against, every entry would
    # read as broken - so none is judged rather than all of them reported.
    #
    # ONLY THE ENTRY'S LEADING PATH, and only a repo-shaped one. `files_section` extracts every
    # backticked token in the section, and the prose after an entry names attributes
    # (`Finding.status`), bare filenames and `~/` config paths. Judged as pointers, the live corpus
    # measured 90 unresolved, nearly all of them that prose; restricted to the first backticked
    # token of a bullet, carrying a `/`, not home- or root-anchored, it measured 13, including
    # the three `skills/_shared/agentflow/tests/` misspellings the auditor found by hand. The
    # section itself is still `files_section`'s: a lead token is judged only if it extracted it.
    if repo_root is not None:
        declared = set(files_section(text).paths)
        raw.extend(
            ("## Files", value)
            for value in _BULLET_LEAD_PATH_RE.findall(text)
            if value in declared and "/" in value
        )
    out: list[tuple[str, str, str]] = []
    for source, value in raw:
        path = _as_pointer(value) if isinstance(value, str) else None
        if path is not None:
            out.append((source, value, path))
    return out


def warn_unresolved_done_spec_paths(
    data_root: Path | str,
    *,
    repo_root: Optional[Path | str] = None,
    patterns: SurveyPatterns = DEFAULT_SURVEY_PATTERNS,
    project: Optional[str] = None,
) -> list[UnresolvedPointer]:
    """Log, and return, every declared path in a `current_state: done` spec that does not resolve.

    gh#365. `provenance.evidence` is FILE-relative, so reconciling a spec into `done/` moves it one
    directory down and leaves every such pointer one `../` short - measured at 0 of 21 resolving
    in `done/` against 41 of 41 in `design/specs`. Nothing re-resolves them and nothing reported
    it, so a spec's evidence trail died at the moment the spec became the historical record.

    DONE SPECS ONLY, and that scope is the check, not a shortcut. A declared path in a finished
    spec names a file that should exist; in an unfinished one it may name a file the epic is
    about to CREATE (`## Files` marks those `(new)`), and the auditor's corpus run found six such
    correct-as-written paths. Reporting them would teach the reader to ignore the channel.

    A POINTER RESOLVES against the spec's own directory or, when given, *repo_root* - the product
    checkout `## Files` entries are written against. `## Files` is judged only when *repo_root*
    is given (see `_done_spec_pointers`). One that does not resolve is a `POINTER_DEPTH_SHIFT` when
    one more `../` finds it - the move broke it - and `POINTER_UNRESOLVED` otherwise: renamed,
    deleted, a directory that went away, a path written against another root, or a real typo.
    Telling those apart needs a human, so this does not guess.

    *repo_root* REQUIRES *project*. It is ONE project's checkout, and an unscoped walk would
    resolve every other project's `## Files` against it - measured at 100 findings where the
    scoped run gives 36. So the pair is refused with `ValueError`. That and `patterns.for_project`'s refusal of a bad project name
    are the only raises here, and both are caller errors rather than corpus findings.

    NOT CALLED FROM `survey_epics`. Every lane surveys on every wake, and pass 1 deliberately does
    not open a terminal spec a second time (gh#358 pins that as a read count). This walks the
    corpus on its own and costs one read per spec only when something asks.

    A warning, never a raise, for `_warn_unknown_queue_classes`' reason - whatever the corpus
    holds. One line per pointer, uncapped, because each is a distinct edit.
    """
    if repo_root is not None and project is None:
        raise ValueError(
            "repo_root is one project's checkout; pass project= so other projects' `## Files` "
            "entries are not resolved against it"
        )
    root = Path(data_root)
    repo = Path(repo_root) if repo_root is not None else None
    if project is not None:
        patterns = patterns.for_project(project)
    found: list[UnresolvedPointer] = []
    seen: set[Path] = set()
    for pattern in patterns.spec_globs:
        for f in sorted(root.glob(pattern)):
            if f.name.endswith(".status.md") or f in seen:
                continue
            seen.add(f)
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                # Named, not swallowed - but NOT through the `, skipping:` channel: the survey
                # already names an unreadable spec there, and that channel is counted.
                _LOG.warning("done-spec pointer check could not read: %s (%s)", f, exc)
                continue
            match = _FRONTMATTER_RE.match(strip_bom(text))
            if not match:
                continue
            try:
                data = yaml.safe_load(match.group(1))
            except yaml.YAMLError as exc:
                _LOG.warning("done-spec pointer check could not parse: %s (%s)", f, exc)
                continue
            if not isinstance(data, dict) or _norm_state(data.get("current_state")) != "done":
                continue
            slug = str(data.get("slug") or _slug_from_filename(f))
            bases = [f.parent] + ([repo] if repo is not None else [])
            for source, raw, pointer in _done_spec_pointers(f, data, text, repo):
                if any((base / pointer).exists() for base in bases):
                    continue
                kind = (
                    POINTER_DEPTH_SHIFT
                    if (f.parent.parent / pointer).exists()
                    else POINTER_UNRESOLVED
                )
                found.append(UnresolvedPointer(f, slug, source, raw, kind))
    found.sort(key=lambda u: (u.slug, str(u.spec), u.source, u.pointer))
    for u in found:
        hint = (
            "one more `../` resolves it - the spec moved into done/ after it was written"
            if u.kind == POINTER_DEPTH_SHIFT
            else "no depth shift resolves it - renamed, deleted or mistyped; check it by hand"
        )
        _LOG.warning(
            "done spec pointer does not resolve (%s): %s %s %r (%s) - %s",
            u.kind, u.slug, u.source, u.pointer, u.spec, hint,
        )
    return found


def _norm_project(project: object) -> Optional[str]:
    """Canonical lower/stripped form of a ``project:`` value, or None when unusable.

    The `_norm_state` treatment, for the same reason and applied late for the same reason.
    ``project:`` used only to pick which records a fleet may claim; it now also decides whether
    a ``depends_on`` edge is FORBIDDEN, so a raw ``!=`` on unnormalized strings turns a casing
    or whitespace typo in ONE record into a permanent stranding of a DIFFERENT, correctly
    tagged record: ``project: Agentflow`` on a parent reads as "set, and different" against a
    child's ``agentflow``, so the child is refused by every builder and the message blames a
    project boundary the two are on the same side of. That is the "assumed the cross-project
    cause and was wrong" failure this feature was written to avoid, reintroduced one field over.

    An EMPTY string is unset, not different. ``project: ""`` survives `_read_sidecar`'s
    ``isinstance(raw_project, str)`` test (unlike ``build_approved_by``, which strips one line
    below), and "set to nothing" carries none of the evidence the both-sides-set rule requires.
    """
    if not isinstance(project, str):
        return None
    return project.strip().lower() or None


def _is_foreign(record: EpicRecord, project: Optional[str]) -> bool:
    """True when *record* demonstrably belongs to a different project than the one surveyed.

    BOTH SIDES MUST BE SET. ``project:`` postdates the earliest specs, so an unset value means
    "unknown", never "different" - 65 of the live corpus's 465 records carry None, and treating
    that as a difference is wrong nine times out of ten (see
    `forbidden_cross_project_dependencies`). Normalized on both sides so the comparison gates on
    the project, not on how the field was typed.
    """
    theirs, ours = _norm_project(record.project), _norm_project(project)
    return ours is not None and theirs is not None and theirs != ours


def warn_foreign_records(records: list[EpicRecord], project: Optional[str]) -> list[str]:
    """Log the foreign-project drop and return the slugs. Extracted so it can be
    called on the WHOLE corpus.

    The sibling `queue_class` drop warns; a silent drop here turns "built by the wrong fleet"
    into "built by nobody, and mentioned nowhere". That is why it must see everything: once
    `selectable()` began running `ready_set` first, a misfiled record whose `depends_on` names
    epics in its OWN project - the typical misfiling, and the shape this warning was written
    for - got dropped as dependency-blocked before `dispatchable` could notice it was foreign.
    The line went silent exactly where it was needed.
    """
    foreign = sorted(r.slug for r in records if _is_foreign(r, project))
    if foreign:
        _LOG.warning(
            "dropping %d record(s) belonging to another project (scope %r): %s",
            len(foreign), project, ", ".join(foreign[:5]),
        )
    return foreign


# How many (record, dep) lines the unresolvable-dependency warning prints before it
# summarises the rest — the same cap warn_foreign_records puts on its example list.
_UNRESOLVABLE_DEP_EXAMPLE_CAP = 5


def _resolves_after_normalization(dep: str, known: set[str]) -> Optional[str]:
    """The slug *dep* would have matched had it been written normalized, or None.

    Record slugs are normalized on the way in — `_slug_from_filename` / `_normalize_slug_stem`
    strip a `YYYY-MM-DD-` prefix and a `-design` suffix — while `depends_on` entries are not:
    both parsers pass them through as raw strings. So a dep written as the FILE STEM misses
    `known` for a reason that has nothing to do with project scope, and the record really is
    stranded (`ready_set` compares raw strings too), so it must still be reported — but it
    must not be reported as the cross-project case. That would send the reader to a deferred
    design decision when the fix is deleting the date prefix from one frontmatter line.

    Not a corner: measured over the sibling corpus, 60 of its 65 unresolvable deps are this
    shape (59 by date prefix, 1 more after also dropping a trailing `.md`), carried by records
    that are one approval away from dispatching.
    """
    stems = [dep, dep[:-3]] if dep.endswith(".md") else [dep]
    for stem in stems:
        normalized = _normalize_slug_stem(stem)
        if normalized in known:
            return normalized
    return None


def _capped_examples(pairs: list[tuple[str, str]], cap: int) -> list[tuple[str, str]]:
    """The capped log slice — every affected epic's FIRST pair before any epic's second.

    A flat ``pairs[:cap]`` slices a slug-sorted list, so one record carrying five junk deps
    eats the whole budget and every other stranded epic leaves the channel with no trace. That
    is a smaller copy of exactly the silence this warning exists to end, and it is reachable:
    the sibling corpus already has a record with four unresolvable deps.

    Shared by both (record, dep) warnings in this module — the fairness rule is a property of
    the pair shape, not of either message.
    """
    first: list[tuple[str, str]] = []
    rest: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pair in pairs:
        (rest if pair[0] in seen else first).append(pair)
        seen.add(pair[0])
    return (first + rest)[:cap]


def _unresolvable_examples(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``_capped_examples`` at the unresolvable-dependency budget."""
    return _capped_examples(pairs, _UNRESOLVABLE_DEP_EXAMPLE_CAP)


def warn_unresolvable_dependencies(
    records: list[EpicRecord],
    excluded_domains=(),
    *,
    project: Optional[str] = None,
) -> list[tuple[str, str]]:
    """Log every ``depends_on`` slug that is absent from *records*, and return the pairs.

    The stranding `selectable()`'s last paragraph describes is real, is not fixed here, and
    used to be TOTALLY SILENT: builders survey glob-scoped, so a prerequisite under another
    project's subtree was never in the list, `ready_set` could not see it finished, and the
    record was gone before `dispatchable` could notice it was foreign. This makes the silence
    loud. It changes no selection behavior whatsoever — the caller ignores the return value
    and `selectable` composes exactly the records it composed before.

    WHAT IT CANNOT KNOW, and therefore says both ways. From inside this function the only
    universe is *records*. A dep slug that is not in it may live under another project's
    subtree or may not exist anywhere at all, and those two are indistinguishable from here;
    the message states both rather than guessing.

    WHICH RECORDS. Only ones that would be dispatchable if the dep resolved — i.e. what
    `dispatchable` keeps on dependency-blindness alone. The cleanest reuse of those four
    predicates is to call it, with ``warn_foreign=False`` so a misfiled record is not reported
    twice in one wake. `ready_set` is deliberately NOT run: the point is to see the records it
    is about to hide.

    That filter is load-bearing, not tidiness. Warning for every record with an unresolvable
    dep measured 1 line for this project and 65 for its sibling, almost all of them terminal
    records whose `depends_on` holds legacy junk — bare dates, `.md` filenames, one full prose
    sentence. Filtered, both projects measure zero: silent today, loud the day the bug bites.

    WHICH DEPS. Only a slug absent from *records*. A dep that is PRESENT but not done is an
    ordinary blocked epic and is never warned about — that is the dependency system working.
    `known` is therefore built from *records* and NOT from `ready_set(records)`: a prerequisite
    that is present but itself dependency-blocked exists, and reporting it as missing would be
    this channel inventing a stranding — the one failure that would make it unreadable.

    WHICH SENTENCE. Two shapes, chosen per pair. A dep that hits `known` once normalized is a
    slug/file-stem mismatch and says so (see `_resolves_after_normalization`); anything else
    gets the cross-project-or-nonexistent line above. Same pairs either way — the return value,
    the count, and selection do not depend on which sentence is printed.

    A NOTE ON DOUBLE-LOGGING. `dispatchable` is re-run here over the records that carry an
    unresolvable dep, so any of THOSE with a non-string `current_state` emits `_norm_state`'s
    type warning twice in a wake. Restricted to that intersection deliberately: passing the
    whole corpus re-warned every malformed record in it, including ones `ready_set` hides.
    """
    known = {r.slug for r in records}
    carriers = [r for r in records if any(dep not in known for dep in r.depends_on)]
    candidates = dispatchable(carriers, excluded_domains, project=project, warn_foreign=False)
    pairs = sorted({
        (r.slug, dep)
        for r in candidates
        for dep in r.depends_on
        if dep not in known
    })
    shown = _unresolvable_examples(pairs)
    for slug, dep in shown:
        normalized = _resolves_after_normalization(dep, known)
        if normalized is not None:
            _LOG.warning(
                "unresolvable depends_on: %r needs %r, which is not among the records handed "
                "in (scope %r) - but %r is, so this is a file stem where a slug belongs, not "
                "a scoping problem; depends_on is matched as a RAW string, so %r should "
                "depend on %r instead",
                slug, dep, project, normalized, slug, normalized,
            )
        else:
            _LOG.warning(
                "unresolvable depends_on: %r needs %r, which is not among the records handed "
                "in (scope %r) - it either lives under another project's subtree or does not "
                "exist at all, and from here those are indistinguishable; %r is approved and "
                "will never be selected while this holds",
                slug, dep, project, slug,
            )
    if len(pairs) > len(shown):
        hidden = sorted({s for s, _ in pairs} - {s for s, _ in shown})
        _LOG.warning(
            "...and %d more unresolvable depends_on pair(s) not listed%s",
            len(pairs) - len(shown),
            f", affecting: {', '.join(hidden)}" if hidden else "",
        )
    return pairs


# How many forbidden-edge lines print before the rest are summarised — the same budget the
# two neighbouring warnings spend, for the same reason (see `_capped_examples`).
_FORBIDDEN_DEP_EXAMPLE_CAP = 5


def _forbidden_edges(records: list[EpicRecord]) -> list[tuple[EpicRecord, str]]:
    """Every (carrier record, dep slug) edge that crosses a tagged project boundary.

    The carrier RECORD is kept, not its slug: the message has to report the project of the
    record that actually carries the edge, and a slug re-looked-up through a by-slug map is
    last-wins over a duplicate slug - which printed ``'x' (project 'alpha') needs 'x' (project 'alpha')`` while asserting the two crossed a boundary.
    """
    by_slug = {r.slug: r for r in records}
    return [
        (r, dep)
        for r in records
        for dep in r.depends_on
        if dep in by_slug and _is_foreign(by_slug[dep], r.project)
    ]


def _forbidden_carriers(records: list[EpicRecord]) -> set[str]:
    """Slugs carrying a forbidden edge - the exclusion set, computed WITHOUT logging.

    Split out so ``ready_set`` can enforce the rule on every call without turning the dependency
    math into a log source: `selectable` already speaks once per survey, over the full corpus,
    and a second voice on the same edge from a function called inside it would double every
    line. Enforcement is silent; `forbidden_cross_project_dependencies` is the one that talks.
    """
    return {r.slug for r, _ in _forbidden_edges(records)}


def forbidden_cross_project_dependencies(records: list[EpicRecord]) -> list[tuple[str, str]]:
    """Log, and return, every ``depends_on`` edge that crosses a project boundary.

    THE OTHER HALF OF THE WARN/FORBID PAIR. `warn_unresolvable_dependencies` (#65) speaks when
    a dep slug is ABSENT from the records handed in; from inside a glob-scoped survey it cannot
    tell "lives under another project" from "does not exist", so it says both. This one speaks
    when the dep is PRESENT and demonstrably belongs to another project — the case only the
    whole corpus can see, and the one registry decision Q-10001 (re-affirmed Q-10007) forbids
    outright: a cross-project ``depends_on`` is not a dependency the model can satisfy, so it
    must not resolve even when every record is in hand.

    THE DEFECT IT CLOSES, measured rather than argued. On the live corpus
    ``agentflow-phase4-use-case-traceability`` (project ``agentflow``) depends on
    ``autodoc-fork-reconcile`` (a record tagged to a DIFFERENT project). Unscoped, ``ready_set`` sees the parent
    finished and calls the child READY; agentflow-scoped, the parent was never globbed and the
    child is blocked. The same record was ready or not ready depending on who looked. Callers
    must remove these carriers from consideration (``selectable`` does) so both views agree.

    BOTH SIDES MUST BE SET, and that is the whole difficulty. ``project:`` postdates the
    earliest specs, so an unset value means "unknown", never "different" — reusing `_is_foreign`
    rather than writing ``a.project != b.project`` is the entire correctness of this function.
    Measured on the live corpus: the naive comparison finds TEN cross-project edges and NINE of
    them are false positives where one side carries ``project: None``. Exactly one edge has both
    sides tagged. A previous change on this file shipped a message that assumed the
    cross-project cause and was wrong about 59 of 65 cases; the both-sides-set rule is what
    keeps this one from repeating that.

    RESOLUTION IS RAW, deliberately. A dep counts as resolving only when it equals a record
    slug exactly — the same string comparison `ready_set` makes. A dep written as a file stem
    (``2026-07-12-parent``) does not resolve for `ready_set` either, so it is not this
    function's case at all; `warn_unresolvable_dependencies` already names it, correctly, as a
    typo rather than a scoping problem. Matching normalized here would report an edge that the
    dependency math never actually satisfied.

    TERMINAL CARRIERS ARE FILTERED OUT OF THE LOG ONLY, and the "only" is the whole point.
    Today's ONE genuine forbidden edge is carried by a ``done`` record, so an unfiltered channel
    would print a line about a finished epic on every survey forever — the noise that trains a
    reader to skip the channel the real line will one day arrive on. That earns silence, and
    nothing more: filtering the RETURN VALUE too made a function named after the rule answer
    ``[]`` on the only corpus that has ever violated it, so any steward sweep or drift check
    asking "does this corpus contain a forbidden edge?" would be told no. On a feature whose
    entire premise is that a silent strand is worse than a noisy one, swallowing the single real
    finding was the wrong default. Callers that want the noise-filtered slice can apply
    `_is_terminal` themselves; callers that want the truth get it by default.

    The log filter is deliberately keyed on ``current_state`` and not ``lifecycle_state``: a
    record whose status.md says done but whose frontmatter still says approved IS still
    selectable (see `selectable`'s stale-approval caveat), so it must stay spoken about. It is
    equally deliberately NOT the sibling's ``dispatchable`` filter. `warn_unresolvable_
    dependencies` restricts to dispatchable carriers because its claim is "this WOULD dispatch
    if the dep resolved". This one's claim is "this spec is illegal", which is true of a
    ``drafted`` or ``planning`` carrier too — and that is the cheapest moment to hear it, while
    the spec is still being written and before a human approves it. Narrowing to dispatchable
    would go silent for exactly that window.

    Returns sorted, de-duplicated ``(record_slug, dep_slug)`` pairs. ``depends_on`` is a list and
    may name the same slug twice; that is one edge, not two.

    THE TERMINAL TEST RUNS LAST, and the order is load-bearing rather than stylistic.
    ``_is_terminal`` goes through ``_norm_state``, which WARNS on a non-string
    ``current_state`` (YAML 1.1 turns ``current_state: yes`` into a bool). Screening every
    record through it would re-emit that line for every malformed record in the corpus,
    including ones nothing was ever going to select - the same double-logging
    `warn_unresolvable_dependencies` restricts its extra ``dispatchable`` pass to avoid.
    Applying it only to records that actually carry a forbidden edge keeps the count where it
    was.
    """
    by_slug = {r.slug: r for r in records}
    edges = _forbidden_edges(records)
    carriers: dict[str, EpicRecord] = {}
    for carrier, _dep in edges:
        carriers.setdefault(carrier.slug, carrier)
    pairs = sorted({(r.slug, dep) for r, dep in edges})
    reportable = [p for p in pairs if not _is_terminal(carriers[p[0]].current_state)]
    shown = _capped_examples(reportable, _FORBIDDEN_DEP_EXAMPLE_CAP)
    for slug, dep in shown:
        _LOG.warning(
            "forbidden cross-project depends_on: %r (project %r) needs %r (project %r) - the "
            "per-project model forbids a dependency across projects, so this edge is never "
            "satisfiable and %r is neither ready nor selectable from this survey; split the "
            "work or move one of the two under a single project",
            slug, carriers[slug].project, dep, by_slug[dep].project, slug,
        )
    if len(reportable) > len(shown):
        hidden = sorted({s for s, _ in reportable} - {s for s, _ in shown})
        _LOG.warning(
            "...and %d more forbidden cross-project depends_on pair(s) not listed%s",
            len(reportable) - len(shown),
            f", affecting: {', '.join(hidden)}" if hidden else "",
        )
    return pairs


# How many stranded-dependency lines print before the rest are summarised - the same budget
# the two neighbouring warnings spend, for the same reason (see `_capped_examples`).
_STRANDED_DEP_EXAMPLE_CAP = 5
#: Same cap, same reason, separate constant: the two channels report different classes and
#: a shared number would couple their volumes for no reason but that they are both five.
_SUPERSEDED_PARENT_EXAMPLE_CAP = 5


def warn_stranded_terminal_dependencies(records: list[EpicRecord]) -> list[tuple[str, str]]:
    """Log, and return, every epic waiting on a dep whose own frontmatter says it is FINISHED.

    THE THIRD WAY A DEPENDENCY IS UNSATISFIABLE, and the only one that was silent. Its two
    neighbours cover a dep that is ABSENT (`warn_unresolvable_dependencies`) and a dep that is
    PRESENT BUT FOREIGN (`forbidden_cross_project_dependencies`). This one covers a dep that is
    present, same-project, and terminal by `_is_terminal` - the module's own answer to "is this
    epic's story over?" - yet NOT in the set `ready_set` treats as satisfied. Nothing can move
    such a dep into that set, so the epic waits forever; and because it falls past every named
    bucket in `blocked_status` to the default `CHAINED`, the operator is told the queue ahead is
    merely deep. That is how the gh#261 victim survived to be found by hand-resolving six
    dependency slugs.

    IT IS SILENT TODAY AND THAT IS THE DESIGN. `_done_slugs` now derives from
    `TERMINAL_CURRENT_STATES`, so for a record whose `lifecycle_state` equals its normalised
    `current_state` - 552 of the 578 in the corpus when this was written - the two tests agree and no
    pair exists. What this reports is the DIVERGENCE between them, which is the one way the
    gh#261 fix can be quietly undone: pass 3 sets `lifecycle_state` from a status.md while
    carrying `current_state` through untouched, so a superseded spec with a live status board
    would read `lifecycle=running, current=superseded` and strand its dependents exactly as
    before. A test pins today's behaviour; only this makes tomorrow's drift audible on the wake
    it happens, which is what the issue asked for.

    WHAT IT DELIBERATELY CANNOT SEE. A dep at a state that MEANS finished but is not in
    `TERMINAL_CURRENT_STATES` - a future `cancelled`, say - is indistinguishable from `drafted`
    to every reader in this module, including this one. Ten deps in the corpus already fall
    through to that same `CHAINED` default at ordinary unfinished states, and warning about
    those would be the noise this module twice refuses. Widen the vocabulary, not this channel.

    AN ABANDONED OR FAILED BOARD IS THE SAME STRAND BY ANOTHER ROUTE. Pass 3 carries those
    statuses through as `lifecycle_state` (`TERMINAL_NOT_DONE_BOARD_STATUSES`): the run is over
    and delivered nothing, so the dep is outside the done-set for good whatever its
    `current_state` says. Those pairs get their own line, naming the board status, because the
    remedy differs - re-plan the parent or re-point the edge, not reconcile a vocabulary.

    WHICH CARRIERS. Not the terminal ones: a finished epic's stale `depends_on` strands nothing,
    and printing it would put a line about closed work on every survey forever - the noise that
    trains a reader to skip the channel the real line will one day arrive on (the same choice,
    for the same reason, as the terminal-carrier filter on the forbidden-edge channel). Nor a
    carrier whose own board ended undelivered: an abandoned epic waits on nothing, so its edges
    to another abandoned parent are not a strand anyone can act on.

    WHICH DEPS. Not the foreign ones. A cross-project dep is unsatisfiable whatever its state
    and `forbidden_cross_project_dependencies` already names it; reporting it here as well would
    call a scoping problem a vocabulary drift and log one misfiling twice in a wake.

    ONE LINE PER STRANDED PAIR, capped: it names the waiting epic, the dep it waits on, and the
    two states whose disagreement is the reason - which is everything needed to act without
    re-deriving anything by hand.
    """
    by_slug = {r.slug: r for r in records}
    # Derived from the dependency math itself, never restated - the rule this whole channel
    # exists to enforce. `dep not in done` IS "ready_set will not count this satisfied".
    done = _done_slugs(records)
    pairs = sorted({
        (r.slug, dep)
        for r in records
        if r.slug not in done
        and r.lifecycle_state not in TERMINAL_NOT_DONE_BOARD_STATUSES
        for dep in r.depends_on
        if dep not in done
        and dep in by_slug
        and not _is_foreign(by_slug[dep], r.project)
        and (
            _is_terminal(by_slug[dep].current_state)
            or by_slug[dep].lifecycle_state in TERMINAL_NOT_DONE_BOARD_STATUSES
        )
    })
    shown = _capped_examples(pairs, _STRANDED_DEP_EXAMPLE_CAP)
    for slug, dep in shown:
        finished = by_slug[dep]
        if finished.lifecycle_state in TERMINAL_NOT_DONE_BOARD_STATUSES:
            _LOG.warning(
                "stranded depends_on: %r waits on %r, whose status board says %r - that run "
                "ended without delivering, so nothing can ever satisfy this edge and %r is "
                "blocked permanently, not queued behind work in flight; re-plan %r or re-point "
                "the dependency (board %s)",
                slug, dep, finished.lifecycle_state, slug, dep, finished.source_path or dep,
            )
            continue
        _LOG.warning(
            "stranded depends_on: %r waits on %r, whose current_state %r is TERMINAL but whose "
            "lifecycle_state %r is not counted as finished - so nothing can ever satisfy this "
            "edge and %r is blocked permanently, not queued behind work in flight; reconcile "
            "%s or widen the terminal vocabulary",
            slug, dep, finished.current_state, finished.lifecycle_state, slug,
            finished.source_path or dep,
        )
    if len(pairs) > len(shown):
        hidden = sorted({s for s, _ in pairs} - {s for s, _ in shown})
        _LOG.warning(
            "...and %d more stranded depends_on pair(s) not listed%s",
            len(pairs) - len(shown),
            f", affecting: {', '.join(hidden)}" if hidden else "",
        )
    return pairs


def warn_dependencies_satisfied_by_a_superseded_parent(
    records: list[EpicRecord],
) -> list[tuple[str, str]]:
    """Log, and return, every epic whose dependency is SATISFIED by a parent that was replaced.

    THE INVERSE OF ITS NEIGHBOUR, which is why it is a separate channel rather than a clause.
    `warn_stranded_terminal_dependencies` reports `dep not in done` — an edge nothing can ever
    satisfy. This reports `dep in done` — an edge that IS satisfied, and may be satisfied on a
    premise that no longer exists. Folding the two into one function would need its predicate
    inverted mid-comprehension and would put two opposite conditions behind one name.

    WHY `superseded` SPECIFICALLY, AND NOT THE TERMINAL SET (gh#324). gh#261 made `_done_slugs`
    derive from `TERMINAL_CURRENT_STATES`, which promises *"the prerequisite's story is over, so
    waiting longer will never help"* — NOT *"the prerequisite's work was delivered."* Those come
    apart for exactly one value. `done`, `shipped` and `archived` all mean the work happened;
    `superseded` means **replaced by children**. After the sanctioned split, a dependent still
    naming the parent holds a stale pointer: its real prerequisite is the children, which may be
    entirely unbuilt, and it now goes READY against a premise that does not exist yet.

    THIS IS COVER FOR A DELIBERATE TRADE, NOT A REVERSAL OF IT. Before gh#261 that dependent
    stranded forever, silently. After it the dependent is claimable and the failure is loud at
    the moment a builder opens the epic — and `blocked-on-stale-premise` is already a live
    lifecycle value in this corpus, so the recovery path exists. Loud beats silent; this makes
    the exposure visible before a builder spends the hour rather than during it.

    ZERO TODAY, RE-MEASURED RATHER THAN CITED. 588 records across both projects at both
    `include_archived` settings, 39 at `superseded`, 113 `depends_on` edges pointing at a
    terminal parent, and **no** edge pointing at a superseded one. gh#324 measured the same zero
    a day earlier; a premise that costs nothing is the cheapest moment to cover it, and the
    channel says nothing until the day it is true.

    THE FIELD IS THE ONE THAT DID THE SATISFYING. `_done_slugs` tests `lifecycle_state`, so this
    tests `lifecycle_state` too — never `current_state`, which would be a second answer to
    "why is this edge satisfied?" and could disagree with the first exactly when it matters.

    NOT THE RE-POINT. gh#324 also asks whether `reconcile` should rewrite `depends_on` at the
    children when it marks a parent superseded, which would remove the class rather than report
    it. That is a different file and a different decision — filed, not silently skipped — and
    the issue is explicit that the two are not exclusive.
    """
    by_slug = {r.slug: r for r in records}
    # Borrowed from the dependency math, never restated: `dep in done` IS "ready_set counts this
    # edge satisfied", and the reason it counts is the parent's `lifecycle_state`.
    done = _done_slugs(records)
    pairs = sorted({
        (r.slug, dep)
        for r in records
        # Carrier filter, same as the neighbour's: a finished epic's stale `depends_on` sends
        # nobody anywhere, and a line about closed work on every survey is the noise that trains
        # a reader to skip the channel.
        if r.slug not in done
        for dep in r.depends_on
        if dep in done
        and dep in by_slug
        and not _is_foreign(by_slug[dep], r.project)
        and by_slug[dep].lifecycle_state == "superseded"
    })
    shown = _capped_examples(pairs, _SUPERSEDED_PARENT_EXAMPLE_CAP)
    for slug, dep in shown:
        parent = by_slug[dep]
        _LOG.warning(
            "depends_on satisfied by a REPLACED parent: %r waits on %r, which is %r - the story "
            "is over so the edge counts as satisfied, but the work moved to that parent's "
            "children rather than shipping; %r may be READY holding a stale pointer, so confirm "
            "it should not name the children instead; parent spec %s",
            slug, dep, parent.lifecycle_state, slug, parent.source_path or dep,
        )
    if len(pairs) > len(shown):
        hidden = sorted({s for s, _ in pairs} - {s for s, _ in shown})
        _LOG.warning(
            "...and %d more superseded-parent depends_on pair(s) not listed%s",
            len(pairs) - len(shown),
            f", affecting: {', '.join(hidden)}" if hidden else "",
        )
    return pairs


def dispatchable(
    records: list[EpicRecord],
    excluded_domains=(),
    *,
    project: Optional[str] = None,
    warn_foreign: bool = True,
) -> list[EpicRecord]:
    """Records eligible for AUTONOMOUS dispatch by a builder loop.

    *warn_foreign* exists for exactly one caller: ``selectable()`` emits the foreign-project
    warning itself, over the whole corpus, before ``ready_set`` narrows it - so it passes
    ``False`` here to avoid logging the same misfiling twice. It defaults to True, so a direct
    caller keeps the warning it has always had.

    Four filters, ALL of which must pass. In order of what they protect:

    1. **Human approval (the primary gate).** ``current_state`` must be
       ``APPROVED_CURRENT_STATE``, normalised via ``_is_approved`` for casing and
       whitespace. This is the single standing human approval on the epic route - the
       thing that makes gateless auto-merge-to-main safe - and until it lived here it was
       enforced only by a sentence in lead-dev's prose, at one call site, with jr-dev-1 /
       jr-dev-2 trusting what lead-dev published and nothing downstream re-checking.
       An unset/None ``current_state`` does NOT dispatch, deliberately unlike the unset
       ``project:`` rule below; the reason for the asymmetry is on ``APPROVED_CURRENT_STATE``.
       Note that every value in ``TERMINAL_CURRENT_STATES`` is excluded as a consequence of
       this rule, not by a separate one - ``done`` / ``archived`` / ``superseded`` /
       ``shipped`` simply are not the approved value.
    2. **``queue_class`` ALLOW-list.** Kept only when the field is unset or in
       ``DISPATCHABLE_QUEUE_CLASSES``, so ``local-tooling`` / ``on-server-ops`` /
       ``brainstorm-pin`` AND any out-of-vocabulary value ('bug', 'feature', a typo) are
       dropped rather than dispatched by default.
    3. **Foreign project.** When *project* is given (keyword-only, so the existing positional
       ``dispatchable(records, project.excluded_domains)`` calls keep working), any record
       whose frontmatter ``project:`` is set and differs is dropped - the record-side half of
       project scoping, which catches a spec misfiled under another project's subtree that
       the glob scope cannot see. A record with ``project`` unset still dispatches (legacy
       specs predate the field).
    4. **Excluded domains.** Drops any record whose ``domain`` or ``project`` is in
       ``excluded_domains`` (``project.excluded_domains`` from config: work the fleet must
       never pick up on its own, e.g. a sibling program built interactively) UNLESS the
       epic's frontmatter carries a non-empty ``build_approved_by:`` (who + when) - the
       per-epic grant that lifts the ban for that one epic.

    LOGGING. No drop this function makes warns, except the foreign-project one. Stated with
    the one exception it has: a ``current_state`` that is not a string at all (YAML 1.1 turns
    a bare ``current_state: yes`` into a bool, ``2026-07-24`` into a date) still emits
    ``_norm_state``'s type warning, because a malformed state IS the abnormal case worth
    reading. A non-approved *string* drop is SILENT, on purpose:
    unlike a foreign-project record - which signals a misfiling that someone has to correct -
    a non-approved record is the normal, expected case and the overwhelming majority of the
    corpus (325 of 329 records at the time this filter was added). One warning per drop would
    bury the foreign-project line that does need reading and train the reader to skip the
    whole channel. Do not "fix" this by adding a per-record warning; a caller that wants to
    show the human unapproved work should survey unfiltered, as dev-manager does.

    What this function still does NOT do:

    * It does not read status.md, so it knows nothing about completion. ``current_state`` is
      the approval signal only (see ``EpicRecord``); completion truth is ``lifecycle_state``
      / the status board, and remains a SEPARATE cross-check the builder performs.
    * It does no dependency math. An unmet ``depends_on`` is ``ready_set()``'s job.

    DO NOT COMPOSE THIS BY HAND - call ``selectable()``. Ordering matters and the intuitive
    order is the wrong one: ``ready_set(dispatchable(...))`` hands ``ready_set`` a list with
    every finished prerequisite already filtered out, so it reports approved work blocked
    forever. ``selectable()`` exists so no caller has to remember that.
    dev-manager (refinement) surveys the FULL set unfiltered - builder-side only.
    """
    excluded = {d for d in (excluded_domains or ()) if d}

    def _excluded(r: EpicRecord) -> bool:
        if not excluded or r.build_approved_by:
            return False
        return (r.domain in excluded) or (r.project in excluded)

    def _foreign(r: EpicRecord) -> bool:
        return _is_foreign(r, project)

    if warn_foreign:
        warn_foreign_records(records, project)
    return [
        r for r in records
        if _is_approved(r.current_state)
        and (r.queue_class is None or r.queue_class in DISPATCHABLE_QUEUE_CLASSES)
        and not _foreign(r)
        and not _excluded(r)
    ]


def selectable(
    records: list[EpicRecord],
    excluded_domains=(),
    *,
    project: Optional[str] = None,
) -> list[EpicRecord]:
    """What a builder loop may claim right now, in the order it should claim it.

    THE ONE EXPRESSION THE BUILDER SKILLS PUBLISH. Pass the full survey; get back the
    dependency-ready, approved, in-scope records ordered by ``priority`` (lowest first, unset
    last). Take ``[0]``.

    "FULL" INCLUDES ARCHIVED - survey with ``include_archived=True``. ``survey_epics`` deletes
    archived records by default, and it does so BEFORE anything here runs, so an archived
    prerequisite is not merely undispatchable (which is correct) but absent from the corpus
    entirely - and ``ready_set`` cannot mark a dependency satisfied by a record it cannot see.
    The child strands forever, silently. Unlike the project-scope question this has NO
    trade-off worth weighing: an archived record is terminal, so ``_is_approved`` is false and
    ``dispatchable`` drops it a line later regardless. Widening the input can only let a
    finished prerequisite be recognised as finished; it can never make one claimable.

    It exists because the composition it replaces had to be written in a specific order that
    nobody could infer from the names, and getting it backwards failed silently in the
    stranding direction. ``ready_set`` must see the WHOLE corpus to know which prerequisites
    finished (see its docstring); ``dispatchable`` must run after, because it drops exactly the
    finished records ``ready_set`` needs. Prose that re-derived this drifted - which is the
    scar that moved selection into this module in the first place - and a two-call expression
    the skills transcribe is still prose. One call cannot be transcribed wrong.

    Every caveat of the parts survives composition and none of them is fixed here: approval is
    not completion (``dispatchable`` reads no status.md, so the builder still cross-checks
    completion truth against the status board before dispatching), and a record whose spec
    frontmatter was never reconciled after shipping stays selectable on its stale approval.

    A CROSS-PROJECT ``depends_on`` IS FORBIDDEN, NOT MERELY UNRESOLVED (registry Q-10001,
    re-affirmed Q-10007). Builders survey with ``survey_epics(..., project=<name>)``, which is
    glob-scoped, so a prerequisite under ANOTHER project's subtree was never in the list handed
    in; ``ready_set`` could not see it finished and stranded the child. Hand the SAME record the
    unscoped corpus and the dep resolved, so dev-manager's refinement view called it ready while
    every builder called it blocked - one record, two answers, decided by who was looking.
    Widening the input was never the fix on its own: a legacy record under a sibling's subtree
    with ``project:`` unset still dispatches, so it only trades a stranding hazard for a
    claim-the-wrong-fleet's-work hazard. The decision taken was to affirm the per-project model
    rather than extend it. So a both-sides-tagged cross-project edge does not count as satisfied
    even with the whole corpus in hand, and scoped and unscoped surveys agree that the record is
    not offered. The exclusion lives in ``ready_set`` (and the matching bucket in
    ``blocked_status``), NOT here: the rule compares a record's own ``project:`` against its
    dep's and so needs no scope argument, and enforcing it only at this call left choose-next -
    which reads ``ready_set`` and ``blocked_status`` directly and never ``selectable`` - still
    calling the carrier READY. This function's remaining job on that rule is to SPEAK, once, over
    the full corpus before ``ready_set`` narrows it.

    The two halves are separate channels on purpose. ``warn_unresolvable_dependencies`` names a
    dep slug ABSENT from the records handed in - which from inside a scoped survey may be a
    foreign parent or may not exist anywhere, indistinguishably.
    ``forbidden_cross_project_dependencies`` names a dep that is PRESENT and provably foreign.
    Neither says anything about a dep that is present, same-project and simply not done yet:
    that is ordinary sequencing and stays silent.
    """
    # All three warnings read the FULL corpus, before ready_set narrows it - see their docstrings.
    warn_foreign_records(records, project)
    warn_unresolvable_dependencies(records, excluded_domains, project=project)
    # Speaks; does not filter. ``ready_set`` enforces the same rule silently, so the exclusion
    # reaches choose-next's human-facing view too rather than only this call.
    forbidden_cross_project_dependencies(records)
    # The third unsatisfiable-dep channel, and the one that used to be silent (gh#261). Runs
    # over the full corpus for its neighbours' reason: `ready_set` is about to hide exactly the
    # records it is about.
    warn_stranded_terminal_dependencies(records)
    # Its inverse (gh#324): an edge that IS satisfied, by a parent replaced rather than
    # delivered. Wired here and not merely defined - a channel nobody invokes is silent in the
    # way that reads as coverage.
    warn_dependencies_satisfied_by_a_superseded_parent(records)
    return by_priority(dispatchable(
        ready_set(records), excluded_domains, project=project, warn_foreign=False,
    ))


def blocked_status(record: EpicRecord, records: list[EpicRecord]) -> BlockedStatus:
    """Classify *record*'s dependency posture (choose-next Step 3 buckets).

    Evaluated in this order (first match wins):
      * any dep is forbidden (cross-project)    → ``MISSING-DEP``
      * every dep terminal (``_done_slugs``)    → ``READY``
      * any dep's board ended undelivered (``abandoned``/``failed``) → ``MISSING-DEP``
      * any dep is a running epic               → ``NEAR-READY``
      * any dep is a candidate (speccing/planning) → ``CHAINED``
      * any dep is unknown (not surveyed)       → ``MISSING-DEP``
      * otherwise, e.g. a ``done_on_hold`` dep awaiting a human merge → ``CHAINED``

    THE FORBIDDEN TEST RUNS FIRST, and it is not a fifth bucket. A cross-project dep is
    unsatisfiable by construction (`forbidden_cross_project_dependencies`), so no later bucket
    can be right about it: ``READY`` is false, and ``NEAR-READY`` / ``CHAINED`` would promise the
    operator it unblocks when the parent lands, which it never will. It reports as
    ``MISSING-DEP`` - the existing "this dep is not one this survey can resolve" bucket, whose
    prose already tells the reader to go look at the dep - rather than a new literal, because
    every consumer of ``BlockedStatus`` renders these four by name and a fifth would need them
    all changed. Without this, a forbidden carrier bucketed ``READY`` for choose-next while
    every builder's ``selectable`` refused it, which is the split this whole feature closes.
    """
    by_slug = {r.slug: r for r in records}
    done = _done_slugs(records)
    running = {r.slug for r in records if r.lifecycle_state == "running"}
    candidates = {r.slug for r in records if r.lifecycle_state in ("speccing", "planning")}
    deps = record.depends_on

    if any(d in by_slug and _is_foreign(by_slug[d], record.project) for d in deps):
        return "MISSING-DEP"
    if all(d in done for d in deps):
        return "READY"
    # An abandoned or failed parent will never land, so NEAR-READY / CHAINED would make the
    # same false promise the forbidden test above refuses; it reports in the same bucket for the
    # same reason, and `warn_stranded_terminal_dependencies` names the edge.
    if any(
        d in by_slug and by_slug[d].lifecycle_state in TERMINAL_NOT_DONE_BOARD_STATUSES
        for d in deps
    ):
        return "MISSING-DEP"
    if any(d in running for d in deps):
        return "NEAR-READY"
    if any(d in candidates for d in deps):
        return "CHAINED"
    if any(d not in by_slug for d in deps):
        return "MISSING-DEP"
    # THE FALL-THROUGH, and the only bucket that can be wrong without anyone noticing: a dep
    # that is present and same-project but at none of the states above lands here and is
    # reported as "deeper queue ahead". That was true of every `superseded` / `shipped`
    # prerequisite until `_done_slugs` stopped restating a subset of the terminal vocabulary
    # (gh#261), and `warn_stranded_terminal_dependencies` now speaks the day it becomes true
    # again. It remains the honest answer for an ordinary unfinished state.
    return "CHAINED"


def blocks_count(record: EpicRecord, records: list[EpicRecord]) -> int:
    """How many OTHER surveyed epics list *record*'s slug in their ``depends_on``.

    The ``depends_on`` inversion — the strategic-value signal choose-next Step 3
    computed inline (the phantom server ``blocks`` field was never stored).
    """
    return sum(1 for r in records if r.slug != record.slug and record.slug in r.depends_on)


def by_priority(records: list[EpicRecord]) -> list[EpicRecord]:
    """Sort ascending by ``priority`` (unset = +inf sorts last). lead-dev section 2 step 3."""
    return sorted(records, key=lambda r: r.priority)
