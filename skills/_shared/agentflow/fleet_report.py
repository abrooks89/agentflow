"""agentflow.fleet_report — the ONE data pass behind every rendering of the fleet report.

The report has three renderings (artifact, vault page, push) and they must never disagree:
"a chat report, a push and an archive that disagree are worse than any one alone." So the
collection lives here, once, and returns a structured snapshot; each renderer reads the
snapshot and formats it. No renderer re-reads the registry or re-runs the survey.

**THE REPORT NEVER ACTS.** This module is a pure read: no labelling, no promoting, no
reaping, no notes, no file writes of any kind — it does not even create the archive
directory it looks in. The spec's non-goal is quoted rather than paraphrased because it is
the rule most likely to erode: "Anything that both reports and mutates will eventually be
run for its side effect." ``test_fleet_report`` asserts the data root is byte-identical
before and after a collection, so a write added here fails the suite rather than surprising
a reader.

**SLUGS, NEVER NUMBERS.** Every epic-shaped row this module emits carries ``slug``. There is
no epic number and no priority integer on any human-facing field; ``priority`` exists only as
a sort key. Numbers survive exactly where they ARE the identifier — tracker issues
(``#51``), registry row ids (``Q-0009``, ``B-0001``), commits and PRs — and those are typed
as such. This is a product requirement, not a preference: "there's simply too many epic
numbers to remember, especially when the fleet is moving fast."

**NEVER A COUNT WITHOUT ITS SKIP COUNT.** Every number that comes from a scan is carried as a
``ScanCount`` (``counted N, skipped M``) with the skipped file NAMES, and a non-zero skip is
a WARNING for the renderer, not a footnote. ``CountedSkippedSection.skips`` is deliberately
NOT capped by ``rows_per_section``: "Skipped — files the survey could not parse and silently
ignored. Always name them." A truncated skip list would recreate the exact defect the rule
exists to close.

**HOW THE SKIPS ARE CAPTURED — a design choice, stated because it is not obvious.**
``epic_survey.survey_epics()`` returns ``list[EpicRecord]`` and nothing else; its
``"malformed ... skipping: <path> (<reason>)"`` lines go to ``_LOG.warning`` on the logger
``agentflow.epic_survey`` and no caller can reach them. That is precisely the spec's hard-rule
problem — "every count taken through it was quietly partial". The fix here does NOT change
``survey_epics``'s signature: every loop in the fleet calls it, and widening a return type used
by five skills to serve one reader is the wrong trade. Instead ``capture_survey_skips()``
attaches a logging handler to that logger for the duration of the call and reads the emitted
records back into ``SkippedFile`` rows. It reads ``LogRecord.args`` — where the path and the
reason are still separate objects — and falls back to parsing the rendered message only when
the record does not carry them; parsing the rendered line first named the wrong file for any
path containing a parenthesis. The coupling is still weaker than a function signature, so
``test_fleet_report`` feeds a genuinely malformed spec through the real survey and asserts the
file name comes back, rather than asserting against a hand-written log string. If the message
shape changes, that test fails loudly.

**The registry is a scan too.** ``parse_registry`` silently drops a table row with the wrong
cell count, an unknown agent name or a malformed id, and silently truncates an over-wide row —
so ``_read_registry`` diffs the raw rows against the parsed ones and returns a second
``ScanCount``. It also REPORTS rather than raises: a zero-byte, half-written or CRLF registry
makes ``parse_registry`` raise, and a traceback is the worst possible output for the one
artefact a human reads *instead of* the registry.

**Sections stay present when empty**, in the LOCKED order — BLOCKED ON YOU / DECIDED BY
AGENTS / INTAKE / WORK / LANES / HEALTH / COUNTED-SKIPPED (``SECTION_ORDER``). An empty
section reads as "checked, nothing there"; an absent one is indistinguishable from one that
failed to run, so every section is always constructed.

STDLIB + the package's own modules only. Reuses ``epic_survey``, ``registry`` and
``issue_intake`` rather than re-reading their files; none of them is modified.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from . import epic_survey as _survey
from . import issue_intake as _intake
from . import plan_parser as _plan
from . import registry as _registry
from .epic_survey import EpicRecord
from .stream_report import describe_streams

# --------------------------------------------------------------------------- #
# Vocabulary and defaults
# --------------------------------------------------------------------------- #
#: The locked section order (spec, "Section order (LOCKED)"). Asks first, then what the fleet
#: decided without you, then the queues, then machine state. Renderers iterate THIS, so the
#: order cannot drift between the artifact, the vault page and the push.
SECTION_ORDER: tuple[str, ...] = (
    "blocked",
    "decided",
    "intake",
    "work",
    # DONE sits between WORK and LANES because it reads as the natural other half of WORK,
    # and it answers the question a status board is most often opened for. The order is
    # LOCKED, and this entry is a deliberate revision of that lock from seven to eight -
    # the rule exists so the order does not drift casually, not so it can never be revised.
    "done",
    "lanes",
    "health",
    "counted_skipped",
)

#: `report.rows_per_section` default. List sections render this many rows and then say
#: `showing 10 of N`.
DEFAULT_ROWS_PER_SECTION = 10

#: Where archived reports live under `vault.root`, and how they are named. The name is
#: chronologically sortable on purpose: "the most recent archived report" is then a filename
#: sort rather than a stat of every file, and a human browsing the folder reads it in order.
DEFAULT_ARCHIVE_SUBDIR = "fleet-reports"
ARCHIVE_STAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"
ARCHIVE_SUFFIX = "-fleet-report.md"

#: Renderers embed `FleetReport.snapshot()` in the archived page inside a fence tagged with
#: this string. The archive is a markdown page for humans; the diff needs machine-readable
#: prior state, and re-parsing rendered prose to recover it would make every wording change a
#: silent diff regression.
SNAPSHOT_FENCE_TAG = "agentflow-report-snapshot"

#: Schema version of the embedded snapshot. The diff REFUSES a prior at any other version and
#: says so, rather than diffing against a shape it does not understand: a prior missing the
#: ``epics`` key silently makes every epic ``was="new"`` and every done epic freshly SHIPPED,
#: which is the "render the whole corpus as new" failure the no-prior branch exists to prevent,
#: arriving through a different door.
SNAPSHOT_VERSION = 2

#: `current_state` values the lifecycle actually defines (dev-manager: "speccing → drafted →
#: approved-for-autonomous → planning → running → done") plus the terminal set epic_survey
#: already owns. Anything else is out-of-vocabulary: not an error the report may fix — it
#: never acts — but a health count with the offending slugs named.
KNOWN_CURRENT_STATES: frozenset[str] = frozenset({
    "speccing",
    "drafted",
    _survey.APPROVED_CURRENT_STATE,
    "planning",
    "running",
}) | _survey.TERMINAL_CURRENT_STATES

#: The `current_state` a spec sits at while it waits for a human to promote it.
DRAFTED_CURRENT_STATE = "drafted"

#: Blocker owners that mean "a person has to resolve this". ``dev-manager`` is included
#: because it IS the human surface — "the only human surface, and nothing else asks a
#: person directly" — so a blocker assigned there is a blocker on the human, not on a loop.
HUMAN_BLOCKER_OWNERS: frozenset[str] = frozenset({"human", "dev-manager"})

#: Lane conditions. THREE conditions with THREE different fixes, "repeatedly confused" on
#: 2026-08-31 — a halted lane needs its question answered, a reaped lane needs relaunching,
#: a silent lane needs diagnosing before anything else is safe to do to it.
LANE_LIVE = "LIVE"
LANE_HALTED = "HALTED-BY-DESIGN"
LANE_REAPED = "REAPED"
#: A lane a human deliberately stopped (gh#98). Distinct from REAPED, which means DIED and
#: had its claims returned: a stood-down lane keeps its claim and may be resumed with its
#: work intact, so rendering the two the same would erase the difference the report exists
#: to draw. Distinct from SILENT for the reason this state was added at all -- SILENT says
#: "undiagnosed", and this row diagnoses itself.
LANE_STOOD_DOWN = "STOOD-DOWN"
LANE_SILENT = "SILENT"
# Two DERIVED conditions, not new agent states - nothing writes them to a row.
# p190 and p195 removed the only signal a dry lane had: before them a lane with
# nothing to do announced itself by halting, and after them it re-checks hourly
# forever and renders as a green LIVE node identical to a lane shipping code.
# Retiring the halt was right; it also deleted the indicator, and this puts the
# indicator back in the REPORT rather than in the lane's behaviour.
LANE_DRY = "DRY"
LANE_BLOCKED = "BLOCKED"

#: A reap ages out of attention: burnt orange for the first 24h, grey after. "Recent death is
#: actionable; a week-old one is history and must stop competing for attention."
REAPED_FRESH_HOURS = 24.0

#: Health metrics whose crossing is worth reporting in CHANGED SINCE, and the value at which
#: they cross. All four are "zero is healthy" counts, so the threshold is 1: the interesting
#: event is the first one appearing or the last one clearing, not the arithmetic in between.
HEALTH_THRESHOLDS: dict[str, int] = {
    "inert_promotions": 1,
    "out_of_vocabulary_states": 1,
    "silent_lanes": 1,
    "survey_skips": 1,
    # THE ONLY THRESHOLD HERE THAT IS NOT 1, and it is not 1 on purpose. The other four count
    # things that should never happen at all, so one occurrence is the signal. Live notes are
    # the fleet's normal traffic: routing one is healthy, so at 1 this metric would report
    # "over" on every board that has ever sent a note - a permanent red light beside six
    # healthy lanes, which a reader learns to skip.
    #
    # 40 is a DRAIN RATE, not a size. A seven-seat fleet routing a note apiece per wake puts a
    # day's ordinary traffic near 30, and every seat is instructed to strike what it consumed
    # at its wake read — so a backlog past 40 is not "a busy day", it is a channel that has
    # stopped being drained. The count was already collected and rendered with no threshold
    # beside it (142 live / 0 struck when this was measured, since grown past 200), which is
    # the difference between a number and a signal.
    "note_backlog": 40,
}

_GIT_TIMEOUT = 30  # seconds; a wedged git must never hang a report

# git_runner(argv) -> (returncode, stdout, stderr) — same shape as issue_intake.Runner.
GitRunner = Callable[[list], tuple]


# --------------------------------------------------------------------------- #
# Skip capture
# --------------------------------------------------------------------------- #
#: `survey_epics` emits "malformed <what>, skipping: <path> (<reason>)" for every file it
#: could not parse. Anchored on the ", skipping: " marker and the trailing parenthesised
#: reason rather than on any one of the five message variants, so a new variant is captured
#: without a code change here.
#:
#: This is the FALLBACK only. The preferred read is ``LogRecord.args``, because the formatted
#: message is ambiguous in a way the args are not: ``(?P<path>.+?)`` is non-greedy and stops at
#: the FIRST ``(`` in the line, which is a path component whenever one exists. A data root under
#: ``Docs (old)``, a spec named ``2026-08-31-foo (draft).md``, or any Windows ``… (2)`` duplicate
#: made the capture name a file that does not exist and never name the one it missed — worse
#: than a bare count, against "Always name them."
_SKIP_MESSAGE_RE = re.compile(
    r"^(?P<what>.*?),\s*skipping:\s*(?P<path>.+?)\s*\((?P<reason>.*)\)\s*\Z",
    re.DOTALL,
)

#: The marker every ``survey_epics`` skip line carries, in its FORMAT STRING (before `%s`
#: substitution). Matching the template rather than the rendered message is what lets the
#: structured ``record.args`` be trusted: a two-arg record whose template carries this marker
#: is a skip, and its args are exactly ``(path, reason)``.
_SKIP_TEMPLATE_MARKER = ", skipping:"


@dataclass(frozen=True)
class SkippedFile:
    """One file a scan could not parse, and therefore could not count.

    ``name`` is the file name, never its absolute path: the report is a human-facing artifact
    and an absolute path leaks a machine layout into a page that gets archived and shared.
    ``parent`` keeps enough context to find the file (the project subtree it sat in).

    ``reason`` is held to the same rule, and it is the harder half: it is a third-party
    exception's own text, and an ``OSError``'s carries the full path while a YAML error's
    carries a multi-line dump. Both are normalised at construction by ``normalise_reason`` —
    whitespace folded to single spaces (the Ntfy body is "one fact per line") and the data
    root replaced by ``<data_root>``. Redacting the FIELD would be the wrong shape here: this
    is the only place the raw text is admitted, so it is normalised on the way in rather than
    scrubbed on the way out.
    """

    scan: str
    name: str
    parent: str
    reason: str
    kind: str = ""

    def __str__(self) -> str:  # pragma: no cover — convenience for renderers
        return f"{self.name} ({self.reason})"


@dataclass(frozen=True)
class ScanCount:
    """`counted N, skipped M` — the only shape a scan-derived number is allowed to take.

    A bare count is a confident-looking partial fact. This type makes the partiality
    un-droppable: a renderer that wants the number has the skip list in its hand.
    """

    label: str
    counted: int
    skipped: tuple[SkippedFile, ...] = ()

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def is_warning(self) -> bool:
        """A non-zero skip renders as a WARNING, not a footnote."""
        return bool(self.skipped)

    def __str__(self) -> str:
        return f"counted {self.counted}, skipped {self.skipped_count}"


def redaction_prefixes(root) -> tuple:
    """Every spelling of ``root`` a third-party exception might print, longest first.

    ``str(OSError)`` renders its filename through ``repr``, so on Windows the backslash
    separators arrive DOUBLED and a plain ``str(root)`` replace misses the path entirely. POSIX
    form shows up in messages that went through ``pathlib``. All three spellings are replaced,
    longest first so a shorter one cannot eat the prefix of a longer one.
    """
    if root is None:
        return ()
    raw = str(root)
    candidates = {raw, raw.replace("\\", "\\\\"), Path(root).as_posix()}
    return tuple(sorted((c for c in candidates if c), key=len, reverse=True))


def normalise_reason(reason, redact: Sequence[str] = ()) -> str:
    """Fold a third-party error into one machine-path-free line.

    Two separate leaks, one place to close them: a YAML error is a multi-line dump that breaks
    the push's "one fact per line", and an ``OSError`` carries the absolute path this repo
    runs a CI gate to keep out of anything archived or shared.
    """
    text = " ".join(str(reason).split())
    for prefix in redact:
        if prefix:
            text = text.replace(prefix, "<data_root>")
    return text


class _SkipHandler(logging.Handler):
    """Collects `survey_epics`'s skip warnings without touching its signature."""

    def __init__(self, scan: str, *, redact: Sequence[str] = ()) -> None:
        super().__init__(level=logging.WARNING)
        self.scan = scan
        self.redact = tuple(redact)
        self.rows: list[SkippedFile] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            template = record.msg if isinstance(record.msg, str) else ""
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a broken log record must not break the report
            self.handleError(record)
            return
        args = record.args
        if _SKIP_TEMPLATE_MARKER in template and isinstance(args, tuple) and len(args) == 2:
            # Structured read. `survey_epics` logs `"... skipping: %s (%s)", path, exc`, so the
            # path and the reason are separate objects here and no parenthesis in either can be
            # mistaken for the other's delimiter.
            raw = Path(str(args[0]))
            reason = args[1]
            kind = template.split(_SKIP_TEMPLATE_MARKER, 1)[0].strip()
        else:
            m = _SKIP_MESSAGE_RE.match(message)
            if not m:
                return  # not a skip line (queue-class / collision warnings pass through)
            raw = Path(m.group("path").strip())
            reason = m.group("reason")
            kind = m.group("what").strip()
        self.rows.append(
            SkippedFile(
                scan=self.scan,
                name=raw.name,
                parent=raw.parent.name,
                reason=normalise_reason(reason, self.redact),
                kind=kind,
            )
        )


@contextmanager
def capture_survey_skips(scan: str, *, redact: Sequence[str] = ()) -> Iterator[list[SkippedFile]]:
    """Capture the files ``epic_survey`` skipped during the wrapped call.

    Attaches a handler to the ``agentflow.epic_survey`` logger, forces that logger to let
    WARNING through for the duration, and restores both afterwards. Propagation is left
    alone so an operator's own logging still sees the lines.

    ``redact`` is the set of path spellings to fold out of the captured reasons — pass
    ``redaction_prefixes(data_root)``; ``survey_with_skips`` does it for you.

    The list is populated as the body runs, so read it after the ``with`` block.
    """
    logger = logging.getLogger(_survey.__name__)
    handler = _SkipHandler(scan, redact=redact)
    previous_level = logger.level
    if previous_level > logging.WARNING or previous_level == logging.NOTSET:
        # NOTSET means "inherit", and an inherited level above WARNING would swallow the very
        # records we are here to read. Pinning it is the only way to be sure.
        logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        yield handler.rows
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def survey_with_skips(
    data_root,
    mtime_cutoff_iso: str,
    *,
    project: Optional[str] = None,
    label: str = "epics",
    include_archived: bool = False,
) -> tuple[list[EpicRecord], ScanCount]:
    """``survey_epics`` plus the ``counted N, skipped M`` its return value cannot carry."""
    with capture_survey_skips(label, redact=redaction_prefixes(data_root)) as rows:
        records = _survey.survey_epics(
            data_root, mtime_cutoff_iso, project=project, include_archived=include_archived
        )
    return records, ScanCount(label=label, counted=len(records), skipped=tuple(rows))


# --------------------------------------------------------------------------- #
# Truncation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rows:
    """A capped list plus the total it was capped from, so `showing 10 of N` is renderable."""

    items: tuple = ()
    total: int = 0

    @property
    def shown(self) -> int:
        return len(self.items)

    @property
    def truncated(self) -> bool:
        return self.total > len(self.items)

    @property
    def showing(self) -> str:
        return f"showing {self.shown} of {self.total}" if self.truncated else ""

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)


def _cap(items: Sequence, cap: int) -> Rows:
    total = len(items)
    if cap is None or cap < 0:
        return Rows(items=tuple(items), total=total)
    return Rows(items=tuple(items[:cap]), total=total)


def _by_priority_then_slug(records: Sequence[EpicRecord]) -> list[EpicRecord]:
    """Priority ascending (unset = +inf sorts last), slug as the tie-break.

    ``epic_survey.by_priority`` sorts on priority alone; ties then keep whatever order the
    filesystem glob produced, which differs between machines and makes "the top 10" unstable
    between two reports of the same corpus. A report whose truncation flickers teaches the
    reader to distrust it, so the tie-break is added here rather than in ``by_priority``
    (which other loops depend on unchanged).
    """
    return sorted(records, key=lambda r: (r.priority, r.slug))


# --------------------------------------------------------------------------- #
# Section rows
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class QuestionRow:
    """One open registry Question. ``qid`` is a registry row id — a number that IS the
    identifier — so it stays a number; everything epic-shaped on it is a slug."""

    qid: str
    urgency: str
    raised_by: str
    question: str
    raised_at: str
    age_hours: Optional[float] = None
    default: str = ""
    slug: str = ""

    @property
    def age(self) -> str:
        return format_age(self.age_hours)


@dataclass(frozen=True)
class BlockerRow:
    bid: str
    raised_by: str
    owner: str
    work: str
    description: str


#: The `registry.unjudgeable_halts` reasons whose referent is DEAD - nothing will ever answer the
#: wait - each with the fix a human applies. The keys are the registry's own reason names
#: (`UNJUDGEABLE_REASONS`), and a test pins that they stay a subset of it. The other reasons are
#: left out on purpose: `blocker-referent` is a live wait on a blocker with an owner, and the
#: `raised-at-*` pair are live open questions whose age cannot be read, which BLOCKED ON YOU
#: already lists as open questions.
DEAD_REFERENT_FIXES = {
    "question-closed": "the question is answered or applied - relaunch the lane or apply the answer",
    "blocker-resolved": "the blocker is resolved - relaunch the lane or apply the resolution",
    "question-retracted": "the question was retracted - raise a new question or resume the lane",
    "referent-missing": "the id exists nowhere - repair the lane's waiting_on",
    "no-referent": "the halt names no id - repair the lane's waiting_on",
}


@dataclass(frozen=True)
class StrandedLaneRow:
    """A lane halted on a referent that is dead, so no answer will ever arrive.

    The same classification `registry read --stranded` prints, so the report and the CLI never
    disagree about which lanes are stuck. ``reason`` is the registry's reason name and ``fix``
    the action that unsticks it.
    """

    agent: str
    waiting_on: str
    reason: str
    fix: str


#: The `answered_by` values that are the fleet deciding without the human. Derived from the
#: registry's enum so a new delegated seat reaches DECIDED without a second list to update.
DELEGATED_ANSWERERS = tuple(v for v in _registry.ANSWERED_BY if v != "human")


@dataclass(frozen=True)
class DecidedRow:
    """A Question answered under delegated authority — any `answered_by` in `DELEGATED_ANSWERERS`.

    ``answered_by`` names the seat that decided (``steward`` or ``dev-manager``), and is the
    label that tells one delegated answer from another.

    "This is where the fleet's judgement is visible and the human otherwise never sees it."
    """

    qid: str
    question: str
    answer: str
    answered_by: str
    answered_at: str
    raised_by: str = ""


@dataclass(frozen=True)
class EpicRow:
    """An epic on a human-facing surface: named by SLUG, never by a number.

    ``priority`` is present as a sort key and carries no display contract; renderers order by
    it and print the slug.
    """

    slug: str
    title: str
    current_state: Optional[str]
    lifecycle_state: str
    priority: float = _survey.UNSET_PRIORITY
    domain: Optional[str] = None
    eta: str = ""
    lane: str = ""
    #: Why this row is where it is, when the section needs one. Set on HEALTH's inert list: an
    #: inert epic is one frontmatter fix from claimable, and the fix is unguessable without it.
    reason: str = ""

    @classmethod
    def of(cls, record: EpicRecord, *, eta: str = "", lane: str = "", reason: str = "") -> "EpicRow":
        return cls(
            slug=record.slug,
            title=record.title,
            current_state=record.current_state,
            lifecycle_state=record.lifecycle_state,
            priority=record.priority,
            domain=record.domain,
            eta=eta,
            lane=lane,
            reason=reason,
        )


@dataclass(frozen=True)
class PublishedRow:
    """One epic or bug published into a Queue lane and not yet claimed from it.

    ``name`` is the epic's slug or the bug's ``gh#N``, the same names the rest of WORK prints;
    ``lane`` is the Queue lane it waits in and ``row_id`` the registry row that put it there.
    """

    name: str
    lane: str
    row_id: str
    kind: str  # "epic" or "bug"


@dataclass(frozen=True)
class SeamBlock:
    """One claimable item that cannot start, and the in-flight work holding its files.

    ``blocked_by`` and ``paths`` are both carried because either alone is unactionable: the
    holder without the paths says "wait for something", and the paths without the holder say
    "these files are busy" with nothing to wait on.
    """

    name: str
    blocked_by: tuple = ()
    paths: tuple = ()


@dataclass(frozen=True)
class ShippedRow:
    """An epic that reached ``done`` inside the report window. Slug is the name; sha and PR
    are numbers only because a commit and a PR have no slug."""

    slug: str
    sha: str = ""
    pr: str = ""


@dataclass(frozen=True)
class MergedPR:
    """One merged pull request. NOT a ``ShippedRow``: that carries an epic slug, and a PR
    has a number and a title instead. Conflating them would put a PR title in a field every
    other reader treats as a slug."""

    number: str
    title: str
    sha: str = ""
    merged_at: str = ""


@dataclass(frozen=True)
class OpenPR:
    """One OPEN pull request, with the facts a reader needs to see it is stuck (gh#300).

    NOTHING IN THE SWEEP READ OPEN PRs, so "finished, green and never landed" had no reader at
    all. The steward's domain B watches queue rows whose PR has MERGED - the case where work
    landed and the board lagged. The opposite case was invisible: measured on the live board,
    a PR sat 122 minutes, mergeable and 14 of 14 checks green, untouched since it opened, while
    its lane's row still read `building` and the whole merge stream stood still behind it.

    EVERY EXISTING DETECTOR WAS INDIVIDUALLY CORRECT AND ALL OF THEM WERE SILENT. The
    missed-wake alarm needs a lapsed eta; the lane's was hours ahead. The stale sweep needs a
    3-hour heartbeat; this one was 57 minutes. The strand check needs an observed `halted`; the
    row read `building`. Domain B needs the PR to have merged; it had not. **The signal is the
    CONJUNCTION**, which is why no single-cell detector can produce it and why this carries the
    parts rather than a verdict.

    NO THRESHOLD LIVES HERE, DELIBERATELY. The issue that asked for this said the threshold is
    a judgement someone has to make - twenty idle minutes is normal, two hours is not, and the
    line between them is a call. Rather than pick it, this reports the MEASUREMENTS and lets the
    reader judge, exactly as the worktree and branch drift fields already do. A number a reader
    can weigh needs no owner; a threshold does, and picking one unasked is how a report starts
    making decisions it was never given.
    """

    number: str
    title: str
    branch: str = ""
    created_at: str = ""
    updated_at: str = ""
    mergeable: str = ""
    merge_state: str = ""
    checks: str = ""
    draft: bool = False
    #: The lane whose non-terminal Queue row owns this branch, or "" when none does. An open PR
    #: no row claims is its own finding: nobody's board says that work exists.
    lane: str = ""


@dataclass(frozen=True)
class DoneSection:
    """5. DONE — what MERGED, which is a different fact from WORK's SHIPPED.

    THE TWO READINGS ARE BOTH TRUE AND THEY DISAGREE, so this section says which one it is
    showing. WORK's ``shipped`` comes from the snapshot diff — correct, but narrow: it can
    only ever show what changed since the LAST report, so on a ten-minute cadence it is
    almost always empty while ten things shipped that day. DONE reads merged pull requests
    over a window instead. A reader who sees "0 shipped" in WORK and six rows here is not
    looking at a contradiction, and ``source_note`` is what makes that legible rather than
    alarming.

    ``available`` is False when there is no tracker CLI or the call failed, and ``reason``
    says which — a section that silently prints nothing for an unreachable tracker is worse
    than one that says it could not look.
    """

    available: bool = False
    reason: str = ""
    rows: Rows = field(default_factory=Rows)
    source_note: str = ""
    window: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.rows.total


@dataclass(frozen=True)
class LaneRow:
    """One loop, its condition, and WHY it is not working.

    ``state`` is one of ``LANE_LIVE`` / ``LANE_HALTED`` / ``LANE_REAPED`` /
    ``LANE_STOOD_DOWN`` / ``LANE_SILENT``.
    ``why`` is never blank for a non-live lane: the whole point of splitting the three
    conditions is that each names a different fix, and a state with no reason next to it
    collapses back into "not working". A LIVE row is blank except in one case: lead-dev with
    startable work and an empty lane carries the count of what it can start.
    """

    agent: str
    state: str
    work: str = ""
    phase: str = ""
    heartbeat: str = ""
    heartbeat_age_hours: Optional[float] = None
    eta: str = ""
    waiting_on: str = ""
    why: str = ""
    resume_condition: str = ""
    reaped_recent: bool = False
    #: PRODUCTION, not liveness. How many merged pull requests in the reporting window are
    #: attributable to this seat, or None when the question could not be asked at all (no
    #: tracker, or the call failed). ``production_basis`` says which, in words, because a bare
    #: 0 beside an unreachable tracker reads as "shipped nothing" — the same confident-and-wrong
    #: shape the whole spec is about. Every liveness signal on this row answers *is it alive*;
    #: this is the only one that answers *is it producing*.
    merged_in_window: Optional[int] = None
    production_basis: str = ""

    @property
    def beat(self) -> str:
        return format_age(self.heartbeat_age_hours)


@dataclass(frozen=True)
class DirectiveRow:
    """A standing human instruction, re-read by every builder before any irreversible action.

    Rendered with its NAME, not just counted — a bare "3 directives in force" is a number a
    reader cannot act on, and this was one of the two rows a human actually asked about on
    first read of the report.
    """

    name: str
    text: str


@dataclass(frozen=True)
class DeployLockRow:
    holder: str = "none"
    taken_at: str = "-"
    age_hours: Optional[float] = None
    held: bool = False
    stale: bool = False


@dataclass(frozen=True)
class WorktreeDrift:
    """Worktree directories on disk versus the lanes that claim them.

    Matching is a documented heuristic: a worktree directory is "claimed" when a non-reaped
    Agents row's ``work`` cell names it, in either direction (the directory name usually
    carries a queue prefix the work cell does not). It is reported as drift, never acted on.
    """

    available: bool = False
    reason: str = ""
    directories: tuple = ()
    unclaimed: tuple = ()
    #: REGISTRATION DRIFT, which is a different question from the claim heuristic above and was
    #: missing entirely (gh#286). `unclaimed` asks "does a lane say it is working here"; these two
    #: ask "does git know this worktree exists". A directory git never registered is invisible to
    #: `git worktree list`, which is what the steward's sweep enumerates with, so the sweep reports
    #: clean while the root is not — and it will keep reporting clean however many accumulate.
    #:
    #: THE TWO DIRECTIONS ARE NOT THE SAME FINDING and are never merged into one count. An
    #: unregistered directory is a shell `git worktree remove` cannot take (it fails on a path git
    #: has no registration for), and an empty one at an epic's intended path makes a later
    #: `worktree add` for that slug fail — which presents as a build error rather than as leftover
    #: state. A registration whose directory is gone is the opposite: git is holding administrative
    #: state for a tree that no longer exists, and `git worktree prune` is what clears it.
    unregistered: tuple = ()
    #: Registered by git, but the directory is not on disk.
    missing: tuple = ()
    #: Why the git side could not be read, when it could not. Empty when it was read. The git half
    #: failing must NOT take the disk half down with it: a report that says nothing because one of
    #: its two probes failed is how a sweep goes quiet at the moment it has something to say.
    registration_note: str = ""


@dataclass(frozen=True)
class BranchDrift:
    available: bool = False
    reason: str = ""
    base_branch: str = ""
    branches: tuple = ()

    @property
    def extra(self) -> tuple:
        return tuple(b for b in self.branches if b != self.base_branch)


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BlockedSection:
    """1. BLOCKED ON YOU — first because it is the only section that asks for anything."""

    questions: Rows = field(default_factory=Rows)
    blockers: Rows = field(default_factory=Rows)
    drafted_awaiting_promotion: int = 0
    drafted_slugs: tuple = ()
    scan: Optional[ScanCount] = None
    #: Lanes halted on a dead referent (`StrandedLaneRow`). They wait on the human as surely as
    #: an open question does - only a relaunch, an apply or a repair moves them - and listing
    #: only open questions left a wedged lane invisible on the one surface that asks for action.
    stranded: Rows = field(default_factory=Rows)

    @property
    def is_empty(self) -> bool:
        return (
            not self.questions.total
            and not self.blockers.total
            and not self.drafted_awaiting_promotion
            and not self.stranded.total
        )


@dataclass(frozen=True)
class DecidedSection:
    """2. DECIDED BY AGENTS — answered under delegated authority since the last report."""

    questions: Rows = field(default_factory=Rows)
    window_start: str = ""
    windowed: bool = True

    @property
    def is_empty(self) -> bool:
        return not self.questions.total


@dataclass(frozen=True)
class IntakeSection:
    """3. INTAKE — untriaged AND selectable. **The pair is the signal.**

    "20 untriaged / 0 selectable" told the whole story of the 08-31 stall and either number
    alone told nothing, so neither is ever emitted without the other. ``available`` is False
    when there is no tracker or the tracker CLI failed; ``reason`` says which, because a
    report that silently prints 0/0 for an unreachable tracker is worse than one that says it
    could not look.
    """

    available: bool = False
    reason: str = ""
    #: Whether this project HAS a tracker to read. Separates the two ways ``available`` is
    #: False, which mean opposite things to the no-work alert: no tracker configured is an
    #: absent source (the product's work lives on disk and the registry alone), while a
    #: configured tracker that could not be read is an UNREAD one, and "no work" cannot be
    #: asserted over a source nobody looked at.
    tracker_configured: bool = False
    untriaged_count: int = 0
    selectable_count: int = 0
    untriaged: Rows = field(default_factory=Rows)
    selectable: Rows = field(default_factory=Rows)
    #: Bug work carrying the `in-progress` marker — exactly what `selectable` now drops (gh#56).
    #: Carried separately rather than folded into `selectable`, because it is NOT claimable and
    #: must not be counted as such; carried AT ALL because WORK reassembles the whole bug set
    #: from this section, and an issue missing from both halves is invisible, not merely busy.
    claimed: Rows = field(default_factory=Rows)
    #: Bug work carrying `needs-decision` and no `in-progress` marker - also dropped from
    #: `selectable` (gh#446). Registry-blind like `selectable`: one a lane holds through a
    #: registry row is counted here AND lands in WORK's bug_in_progress. Carried for the same
    #: reason as `claimed`: it waits on a human's product call, not
    #: on a builder, so it is neither claimable nor absent. Its own count, because a human
    #: reads it as "calls owed", never as build work.
    awaiting_count: int = 0
    awaiting: Rows = field(default_factory=Rows)
    #: The tracker page came back exactly full, so the pair may be a partial count.
    capped: bool = False
    limit: int = 0
    #: The UNCAPPED halves WORK re-slices. The `Rows` above are cut to `rows_per_section` for
    #: rendering, and WORK's bug totals are counts, not renderings: splitting the capped rows
    #: printed "10 claimable" beside INTAKE's "48 selectable" with nothing saying either was
    #: partial. Not rendered and not compared - they are the same issues as the rows above.
    all_selectable: tuple = field(default=(), repr=False, compare=False)
    all_claimed: tuple = field(default=(), repr=False, compare=False)
    all_awaiting: tuple = field(default=(), repr=False, compare=False)
    #: Open issues a merged PR or a done spec CLAIMS fixed (``issue_intake.fix_claims``, E-0913).
    #: A claim, never a verdict: no comment or registry is read here, so it names no fixer and
    #: does not know which claims the auditor already checked. ``None`` when the merged-PR read
    #: could not run or failed, and ``fix_claims_note`` then says why - an unread source is not
    #: a zero. The rest of the section never depends on it.
    fix_claimed_count: Optional[int] = None
    fix_claimed: Rows = field(default_factory=Rows)
    fix_claims_note: str = ""

    @staticmethod
    def _whole(rows: Rows, full: tuple) -> tuple:
        # A hand-built section carries only its Rows; the Rows are then the whole of it.
        return full if full or not rows.items else rows.items

    @property
    def bug_work_halves(self) -> tuple:
        """``(selectable, claimed, awaiting)`` uncapped - what WORK's bug columns count over."""
        return (
            self._whole(self.selectable, self.all_selectable),
            self._whole(self.claimed, self.all_claimed),
            self._whole(self.awaiting, self.all_awaiting),
        )

    @property
    def is_empty(self) -> bool:
        """Empty means "looked, found nothing" — an unreachable tracker is NOT empty.

        A renderer told a section is empty prints "checked, nothing there" and drops the rest,
        which would hide the ``reason`` that says the look never happened.
        """
        return (
            self.available
            and not self.capped
            and not self.untriaged_count
            and not self.selectable_count
            and not self.claimed.total
            and not self.awaiting_count
            and not self.fix_claimed_count
            # An unread or partial count carries a note, and the note must stay on screen.
            and not self.fix_claims_note
        )


@dataclass(frozen=True)
class WorkSection:
    """4. WORK — epics by slug and bugs by number, side by side (E-0212).

    Bugs reach builders through a fully built path — ``builder_selectable``, lead-dev section 2 step 4 —
    but that path was invisible here, so a reader could not see "three bugs are being worked
    right now" at a glance. Extending WORK rather than adding a ninth section is deliberate:
    the section order is locked at eight, and bugs are straightforwardly work.

    The bug fields mirror the epic states one for one, so the two columns read as one table
    rather than as a table with an appendix.
    """

    approved_claimable: Rows = field(default_factory=Rows)
    #: What a lane could START this second: `approved_claimable` minus everything a registry
    #: claim says is already taken. The two answer different questions and are printed together
    #: (gh#298) - "how much approved work exists" is not "how much is startable", and one number
    #: standing for both is how a starved board reads healthy. `selectable()` is right to know
    #: nothing about the Queue; the defect was building a stall alert on it.
    claimable_now: Rows = field(default_factory=Rows)
    in_progress: Rows = field(default_factory=Rows)
    #: Claimed rows parked on a question. COUNTED SEPARATELY AND NEVER FOLDED INTO EITHER
    #: number above: a parked epic is arguably more alarming than an absent one, so hiding it
    #: inside a subtraction is the one reading that loses the signal. A parked epic leaves
    #: `claimable_now` (it IS claimed) and does NOT enter `in_progress` (nobody is building it).
    parked_count: int = 0
    #: Epics AND bugs published into a builder's Queue lane that nobody has claimed yet - a
    #: non-terminal row whose `claimed_by` is `-`. Each is `PublishedRow` carrying its lane.
    #: Like `parked_count`, it folds into neither number: the work is not startable by any
    #: seat but the lane's owner ("each junior claims only from its own lane", and a second
    #: publish of the same row is refused `[id_duplicate]`), and nobody is building it yet.
    #: Held out of `claimable_now` and `bug_claimable`, and `no_work_anywhere` stays silent
    #: while it is non-empty, because work waiting in a lane is not an empty board.
    published_unclaimed: Rows = field(default_factory=Rows)
    #: Approved epics a claim MIGHT be holding and we cannot tell which. An epic id carries the
    #: first four hyphen-tokens of its slug, and that name is not unique - nine names collide
    #: across one live project's corpus. Joining on it alone lets ONE claim suppress every spec
    #: sharing it, which is how a board with startable work prints "no work anywhere". These are
    #: held out of `claimable_now` (never invite a lane into occupied work) but NAMED here rather
    #: than silently subtracted, and `no_work_anywhere` refuses to fire while this is non-empty.
    #:
    #: EVERY approved epic lands here when the registry could not be read at all: no claim was
    #: checked, so any of them may be held, and a `claimable_now` equal to `approved_claimable`
    #: would be a claim check reported as run when it never did. ``claim_note`` says why.
    claim_ambiguous: Rows = field(default_factory=Rows)
    #: Why the claim check did not run, when it did not. Empty when it did.
    claim_note: str = ""
    drafted_count: int = 0
    shipped: Rows = field(default_factory=Rows)
    shipped_note: str = ""
    scan: Optional[ScanCount] = None

    #: Bug side. `triaged`, not `epic`, not `needs-decision`, no `in-progress` marker, and
    #: not named by a registry claim - `builder_selectable` minus whatever a lane is building.
    bug_claimable: Rows = field(default_factory=Rows)
    #: Selectable bugs named by `gh#N` in a claimed registry row - derived from the claim,
    #: never from a label, because a label is a second thing to update and the copy is what
    #: goes stale.
    bug_in_progress: Rows = field(default_factory=Rows)
    #: Selectable bugs a registry claim MIGHT hold, when the registry could not be read: the bug
    #: half of `claim_ambiguous`. Held out of `bug_claimable` and named; `bug_note` says why.
    bug_claim_ambiguous: Rows = field(default_factory=Rows)
    #: Untriaged bugs: the bug equivalent of `drafted`, work that exists but cannot start.
    bug_untriaged_count: int = 0
    bug_shipped: Rows = field(default_factory=Rows)
    #: Why the bug side is absent, when it is. An empty bug column for an unreachable
    #: tracker must not read the same as an empty one for a tracker with no open bugs.
    bug_note: str = ""

    #: Seam signal (gh#121). Claimable epics and bugs whose declared files intersect the
    #: files an in-flight item declares - reported, never enforced. The disjointness rule
    #: is doing its job; what was missing is that "3 claimable" and "0 a free lane can
    #: actually start" printed as the same number.
    seam_blocked: Rows = field(default_factory=Rows)
    #: Claimable items whose file set could not be read at all. A THIRD state on purpose:
    #: an item with no ``## Files`` section is not evidence that nothing overlaps, and
    #: counting it as clear is how a blocked lane reads as an available one.
    seam_unknown_count: int = 0
    #: In-flight items that declare no files. Each one weakens every `not blocked` verdict
    #: in this section, because work nobody can locate cannot be found to overlap anything.
    seam_unknown_holders: int = 0
    #: True when either input was capped before the check saw it, so a clear verdict covers
    #: only the rows that fit. `rows_per_section` defaults to 10 and boards routinely exceed
    #: it, which makes this the common case rather than the corner one.
    seam_partial: bool = False
    #: What weakens THIS section's verdicts - unreadable holders, a capped view - rather than
    #: why the check did not run. The check always runs; what varies is how much it could see.
    seam_note: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.approved_claimable.total
            or self.in_progress.total
            or self.drafted_count
            or self.shipped.total
            or self.bug_claimable.total
            or self.bug_in_progress.total
            or self.bug_claim_ambiguous.total
            or self.published_unclaimed.total
            or self.bug_untriaged_count
            or self.bug_shipped.total
        )


@dataclass(frozen=True)
class LanesSection:
    """5. LANES — rendered as a Mermaid org chart by the artifact renderer, not a table.

    ``silent_count`` is carried explicitly even when it is zero: "0 silent" is the reassuring
    half, and it only means something because the other categories exist beside it.
    """

    lanes: tuple = ()
    live_count: int = 0
    halted_count: int = 0
    reaped_count: int = 0
    silent_count: int = 0
    #: Carried for the same reason ``silent_count`` is, and it could not be carried honestly
    #: before ``_lane_has_work`` became total: half the roster was structurally incapable of
    #: counting toward it, so "0 dry" was a fact about the code rather than about the fleet.
    dry_count: int = 0
    #: The other half of the DRY/BLOCKED pair, and the half a reader acts on: a dry lane wants
    #: work published to it, a blocked one wants a question answered. Tallied beside ``dry_count``
    #: since the states were added, but discarded at the constructor until now — so the renderer
    #: could draw a BLOCKED node and no section could say how many there were.
    blocked_count: int = 0
    #: Lanes a human deliberately stopped. Counted on their own because they are neither live
    #: nor failing, and a fully paused fleet must read as paused rather than as a fleet with
    #: nothing live and nothing wrong.
    stood_down_count: int = 0
    #: The production signal's own availability, kept beside the counts rather than inferred
    #: from them. ``production_available`` False means the tracker could not be read at all —
    #: every row's ``merged_in_window`` is None and NOT zero.
    production_available: bool = False
    production_window: str = ""
    #: Merged PRs in the window that name no epic this board can resolve. Reported, never
    #: silently dropped: it is the denominator that tells a reader whether "lead-dev: 0" means
    #: the lane shipped nothing or the attribution key missed.
    production_unattributed: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.lanes


@dataclass(frozen=True)
class HealthSection:
    """6. HEALTH — machine state, each number with the names behind it."""

    inert_promotions: Rows = field(default_factory=Rows)
    inert_count: int = 0
    out_of_vocabulary: Rows = field(default_factory=Rows)
    out_of_vocabulary_count: int = 0
    deploy_lock: DeployLockRow = field(default_factory=DeployLockRow)
    directives: Rows = field(default_factory=Rows)
    directive_count: int = 0
    registry_bytes: int = 0
    notes_live: int = 0
    notes_struck: int = 0
    halt_events: tuple = ()
    reap_events: tuple = ()
    worktrees: WorktreeDrift = field(default_factory=WorktreeDrift)
    branches: BranchDrift = field(default_factory=BranchDrift)
    #: OPEN pull requests (gh#300). Nothing in the sweep read these, so a PR that is finished,
    #: green and simply never merged had no reader at all - the mirror image of the case domain B
    #: already watches. Carries the parts, not a verdict: see `OpenPR` for why no threshold lives
    #: here.
    open_prs: tuple = ()
    #: Why the open-PR read failed, when it did. Empty when it succeeded. A report that prints an
    #: empty list because it could not look is indistinguishable from one that looked and found
    #: nothing, and only one of those is good news.
    open_prs_note: str = ""
    #: `inert_count` and `out_of_vocabulary_count` both come from the epic survey, so they are
    #: scan-derived numbers and owe `counted N, skipped M` exactly like WORK's and BLOCKED's.
    scan: Optional[ScanCount] = None

    @property
    def is_empty(self) -> bool:
        """Every field this section renders, not just the three alarming ones.

        Directives, halts, reaps and unclaimed worktrees are all rendered here; a section that
        called itself empty while carrying three directives in force invited a renderer to
        print "checked, nothing there" over the row Step 8 mandates by name.
        """
        return not (
            self.inert_count
            or self.out_of_vocabulary_count
            or self.deploy_lock.held
            or self.directive_count
            or self.halt_events
            or self.reap_events
            or self.worktrees.unclaimed
            # The registration findings render here too, so they decide emptiness here too. Added
            # with them and not after: a board whose ONLY finding was four unregistered shells
            # rendered HEALTH as "checked, nothing there", which is the exact silent zero this
            # property's own docstring was written against.
            or self.worktrees.unregistered
            or self.worktrees.missing
            # An open PR is a finding this section renders, so it decides emptiness here too -
            # added WITH the field, not after, because the last time a field was added without
            # this line a board whose only finding was four unregistered shells rendered as
            # "checked, nothing there".
            or self.open_prs
            or self.open_prs_note
            # A probe that FAILED is a finding, not an absence. Without this the section reports
            # itself empty on the one board where it could not look.
            or self.worktrees.registration_note
            or self.branches.extra
        )


@dataclass(frozen=True)
class CountedSkippedSection:
    """7. COUNTED / SKIPPED — every scan's `counted N, skipped M`, skipped files NAMED.

    Never truncated by ``rows_per_section``. "Always name them" is unconditional; a capped
    skip list would be a partial account of what the report could not see, which is the exact
    defect the rule exists to close.
    """

    scans: tuple = ()

    @property
    def skips(self) -> tuple:
        return tuple(s for scan in self.scans for s in scan.skipped)

    @property
    def total_skipped(self) -> int:
        return len(self.skips)

    @property
    def is_warning(self) -> bool:
        return any(scan.is_warning for scan in self.scans)

    @property
    def is_empty(self) -> bool:
        return not self.scans


def _health_metrics(
    health: HealthSection,
    lanes: LanesSection,
    counted_skipped: CountedSkippedSection,
) -> dict:
    """The ONE health dict. Both spellings of the snapshot core call this and nothing else.

    ``diff_since`` compares this session's core against the PRIOR ARCHIVED snapshot, so ``was``
    comes from ``FleetReport.snapshot()`` one report ago and ``now`` comes from ``collect``'s
    core dict this report. They were two hand-written dicts over two sets of locals, a matched
    pair by convention with nothing asserting it — and they had already drifted on
    ``survey_skips``: the archived side counted every scan, the diffed side counted the epic
    survey only. A metric compared against a differently-computed version of itself is not
    comparable with itself. It reports a crossing on a board where nothing moved (a single
    unescaped ``|`` in a registry row prints "survey_skips cleared: was 1, now 0" on every run
    forever) and it can never report one in the other direction, because the side that grew is
    the side that cannot see it.

    THAT CONDITION IS UNMET TODAY, and saying so is the difference between a repair and a
    rescue: registry skips measured 0 on both configured boards when this landed, so the two
    spellings coincided and no report was actually wrong. This closes the defect before it
    costs anything, which is the only time it is cheap to close.

    ``survey_skips`` counts EVERY scan's skips, not just the epic survey's. A registry row the
    parser dropped is exactly as much "something the report could not see" as a spec file the
    survey dropped. A dropped AGENT row takes a lane out of LANES; a dropped BLOCKER row takes
    a human-owned blocker out of BLOCKED ON YOU. Both are registry rows, both are registry
    skips, and neither is visible to the epic survey. (Measured, because the tempting stronger
    claim is false: dropping an agent row does NOT take the blocker under it with it -
    blockers parse from their own section and are not filtered by the agent roster.)
    And the renderer contract already reads both scans for both consequences: the
    top-of-report WARNING is ``report.counted_skipped.is_warning`` and the ESCALATION push
    fires when the skip count is non-zero. Neither has a call site in this module - that
    contract lives as prose in ``skills/report/SKILL.md``, so it binds the renderer, not the
    type checker. A threshold firing on
    a strictly narrower population than the warning printed two sections below it is the same
    two-counts-two-predicates defect one layer up. It is also the reading the archived
    snapshots on disk were written under, so it is the one that leaves an already-written
    ``survey_skips: 0`` meaning what it meant when it was written.

    The key NAME still argues epic-only — "survey" is this module's word for the epic survey,
    and ``HealthSection.scan`` is epic-only by docstring. Renaming it to ``scan_skips`` is
    defensible and is NOT free: the key would be absent from every archive already written, and
    ``diff_since`` reads an absent prior as 0, so the first report after a rename prints one
    spurious ``crossed`` on any board currently over. That is a second decision, not this one.
    """
    return {
        "inert_promotions": health.inert_count,
        "out_of_vocabulary_states": health.out_of_vocabulary_count,
        "silent_lanes": lanes.silent_count,
        "survey_skips": counted_skipped.total_skipped,
        "note_backlog": health.notes_live,
    }


# --------------------------------------------------------------------------- #
# CHANGED SINCE
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LaneTransition:
    agent: str
    was: str
    now: str


@dataclass(frozen=True)
class EpicMove:
    slug: str
    was: str
    now: str


@dataclass(frozen=True)
class HealthCrossing:
    metric: str
    was: int
    now: int
    threshold: int
    direction: str  # "crossed" (healthy -> not) | "cleared" (not -> healthy)


@dataclass(frozen=True)
class ChangedSince:
    """The diff contract. With report N-1 on disk the window is exact rather than guessed.

    ``no_prior`` is the load-bearing field: with no archived report the honest output is
    "first report - no prior to diff", NOT a diff that renders the entire corpus as new.
    Every other field stays empty in that case, and ``note`` says why.
    """

    no_prior: bool = True
    note: str = "first report - no prior to diff"
    prior_path: str = ""
    prior_generated_at: str = ""
    lane_transitions: tuple = ()
    #: Approval-state moves (spec frontmatter): promotions, drafts, out-of-vocabulary edits.
    epic_moves: tuple = ()
    #: Reconciled progress moves (status.md): this is what SHIPPED is derived from.
    lifecycle_moves: tuple = ()
    questions_opened: tuple = ()
    questions_answered: tuple = ()
    questions_applied: tuple = ()
    #: A question its RAISER withdrew, which is a different event from one being answered and has
    #: to read as one. Without this field a question going open -> retracted appeared in no line
    #: of CHANGED SINCE at all: the row simply vanished between two reports, which is the
    #: "a new state one reader does not know about" failure the epic that added it is about.
    questions_retracted: tuple = ()
    issues_triaged: tuple = ()
    health_crossings: tuple = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.lane_transitions
            or self.epic_moves
            or self.lifecycle_moves
            or self.questions_opened
            or self.questions_answered
            or self.questions_applied
            # LOAD-BEARING: without it a window in which the only change was a retraction still
            # renders "nothing changed since <stamp>", which is a demonstrably false sentence.
            or self.questions_retracted
            or self.issues_triaged
            or self.health_crossings
        )


@dataclass(frozen=True)
class ArchiveInfo:
    """Where this report should be archived — resolved from config, never hardcoded.

    ``available`` is False when the environment has no ``vault`` block. The report still
    renders in that case and says the archive was skipped: a missing vault costs the diff,
    not the report.

    **``path`` is the field to write to, and ``directory`` is the FULL directory.** They were
    the leaf name once, and the skill's ``<vault.root>/<directory>/<filename>`` then dropped
    the ``fleet-reports`` component that ``archive_directory`` puts in and
    ``find_latest_archive`` reads back. Every run wrote where no run looked: the diff had no
    prior on run 1, run 2 and run N, the SHIPPED table never existed, and nothing errored. The
    writer's path and the reader's path are one value here so they cannot drift again.
    """

    available: bool = False
    reason: str = ""
    directory: str = ""
    filename: str = ""
    path: str = ""
    prior_path: str = ""


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReportInputs:
    """Everything the collection reads, resolved once so the collector takes no config.

    Built from config by ``inputs_from_config``; built by hand in tests, against tmp-dir
    fixtures. Nothing here is a default path: the archive location comes from the config
    ``vault`` block or it is absent, and absent renders as "archive skipped".
    """

    data_root: Path
    project: str
    mtime_cutoff_iso: str
    registry_path: Optional[Path] = None
    vault_root: Optional[Path] = None
    archive_subdir: str = DEFAULT_ARCHIVE_SUBDIR
    worktree_root: Optional[Path] = None
    repo_path: Optional[Path] = None
    base_branch: str = "main"
    excluded_domains: tuple = ()
    rows_per_section: int = DEFAULT_ROWS_PER_SECTION
    tracker_host_ref: str = ""
    tracker_kind: str = ""
    #: ``environment.git_host.cli`` as config resolved it. INTAKE reads it with ``tracker_kind``;
    #: the pull request readers read it with ``git_host_kind``.
    tracker_cli: str = ""
    #: ``environment.git_host.kind`` as config resolved it. Empty on a hand-built input, which
    #: the pull request readers report as an unset host rather than assuming GitHub.
    git_host_kind: str = ""
    #: Explicit, so `IntakeSection.capped` can say when the pair may be partial. Left at
    #: `list_open_issues`'s own default, but named here rather than inherited invisibly.
    issue_limit: int = 200
    stale_hours: float = _registry.DEFAULT_STALE_HOURS


def inputs_from_config(
    project_name: Optional[str] = None,
    *,
    rows_per_section: int = DEFAULT_ROWS_PER_SECTION,
) -> ReportInputs:
    """Resolve ``ReportInputs`` from the configured environment and project.

    The archive path comes from ``environment.vault.root`` — an OPTIONAL block. When it is
    absent ``vault_root`` is None and the report says the archive was skipped rather than
    inventing a location.
    """
    from . import config as _config

    env = _config.load_environment()
    name = _config.resolve_project_name(project_name)
    proj = _config.load_project(name)
    data_root = Path(env.data_root)
    return ReportInputs(
        data_root=data_root,
        project=name,
        mtime_cutoff_iso=env.mtime_cutoff_iso,
        registry_path=data_root / "projects" / name / "status" / "registry.md",
        vault_root=Path(env.vault.root) if env.vault is not None else None,
        worktree_root=Path(env.worktree_root) if env.worktree_root else None,
        repo_path=Path(proj.canonical_repo.local_checkout),
        base_branch=proj.canonical_repo.base_branch,
        excluded_domains=tuple(proj.excluded_domains or ()),
        rows_per_section=rows_per_section,
        tracker_host_ref=proj.canonical_repo.host_ref,
        tracker_kind=proj.work_source.kind,
        tracker_cli=env.git_host.cli,
        git_host_kind=env.git_host.kind,
    )


# --------------------------------------------------------------------------- #
# The snapshot
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FleetReport:
    """One collection. Every rendering reads this and nothing else."""

    generated_at: str
    project: str
    rows_per_section: int
    blocked: BlockedSection
    decided: DecidedSection
    intake: IntakeSection
    work: WorkSection
    done: DoneSection
    lanes: LanesSection
    health: HealthSection
    counted_skipped: CountedSkippedSection
    changed_since: ChangedSince
    archive: ArchiveInfo
    registry_available: bool = True
    registry_reason: str = ""

    NO_WORK_SENTENCE = (
        "The product has no work anywhere: nothing claimable, nothing drafted, "
        "and nothing selectable in the tracker."
    )
    #: The same fact for a project with no issue tracker. Its work lives on disk and in the
    #: registry alone, so a sentence naming "the tracker" points the reader at a source that
    #: does not exist.
    NO_WORK_SENTENCE_NO_TRACKER = (
        "The product has no work anywhere: nothing claimable and nothing drafted "
        "(this project has no issue tracker)."
    )

    @property
    def unread_work_sources(self) -> tuple:
        """Each work source that exists but could not be read, with its reason.

        A tracker that is not CONFIGURED is not here - that source is absent, not unread, and
        the no-work alert may fire without it. A configured one that failed is, and so is the
        registry, which is where every claim lives.
        """
        out = []
        if not self.registry_available:
            # The collector's own reasons already name the registry; wrapping one again read
            # "the registry (could not read the registry: ...)".
            reason = self.registry_reason
            out.append(reason if "registry" in reason else
                       f"the registry was not read ({reason or 'unavailable'})")
        if self.intake.tracker_configured and not self.intake.available:
            out.append(f"the tracker was not read ({self.intake.reason or 'unavailable'})")
        return tuple(out)

    @property
    def _nothing_found(self) -> bool:
        """Every work count this report could read is zero - the evidence, not the verdict."""
        return (
            # `claimable_now`, NOT `approved_claimable` (gh#298). The question this predicate
            # answers is "can any lane start anything", and approved work that a lane is
            # already building cannot be started by a second one - so counting it here delays
            # the alert by however long the in-flight epics take, which is exactly the window
            # the alert exists to catch.
            #
            # IT DOES NOT ONLY FIRE EARLIER, and an earlier draft of this comment claimed it
            # did. Reading a SMALLER number can fire the alert on a board that has startable
            # work, which is the opposite failure and the worse one: a stall that is not
            # reported wastes a wake, a stall reported falsely stops the seat that feeds every
            # other lane. `claim_ambiguous` is the guard - an epic a claim MIGHT be holding is
            # not evidence of an empty board, so the alert stays silent until the ambiguity is
            # resolved and the report names the epics instead.
            self.work.claimable_now.total == 0
            and self.work.claim_ambiguous.total == 0
            # Work published into a lane and not yet claimed is work, held out of both
            # claimable counts only because one seat alone may take it. Same guard as above.
            and self.work.published_unclaimed.total == 0
            and self.work.drafted_count == 0
            and self.intake.selectable_count == 0
            # A bug awaiting a human's product call is work waiting on a human, as a drafted
            # spec is (gh#446); before it left `selectable` it defeated this alert there.
            and self.intake.awaiting_count == 0
        )

    @property
    def no_work_anywhere(self) -> bool:
        """All three work sources are empty at once — state it, never imply it.

        p195 made this the report's job. Deciding lead-dev's empty-queue halt as option A
        removed the only channel that seat had for telling a human the product had run out
        of work, and **the report is now that channel** — so it has to actually carry the
        signal rather than leave it to be reconstructed.

        Today a reader has to combine three zeros — claimable 0, drafted 0, nothing
        selectable — and infer it. That inference is exactly the sort this report exists to
        stop people making by hand, and it is the inference nobody makes at a glance on a
        phone.

        DELIBERATELY NOT ``work.is_empty``, which also reads ``shipped``: a day on which
        something shipped but no work remains is precisely the case that matters, and
        ``is_empty`` answers False for it. Shipped is a diff window over the last report;
        it says nothing about whether there is work left.

        Rare by construction, which is why it must be unmissable when it happens rather
        than reconstructed from an absence.

        TWO THINGS KEEP IT FALSE ON A BOARD OF ZEROS, and ``work_sentence`` says which:

        * **A source that could not be read** (``unread_work_sources``). Zeros counted over an
          unreachable tracker or a missing registry are the absence of a look, not a finding,
          and the loudest sentence in the report - go write new work - must not rest on them.
        * **An inert approved epic** (``health.inert_count``). It is one frontmatter fix from
          claimable; the fix is to correct its queue_class or domain, not to write new work.
        """
        return (
            self._nothing_found
            and not self.unread_work_sources
            and self.health.inert_count == 0
        )

    @property
    def work_sentence(self) -> str:
        """The sentence WORK prints about whether work exists, or "" when the counts say it.

        Either the no-work sentence (tracker or no-tracker form), or one or both of: a
        could-not-determine sentence naming each unread source, and a sentence naming the inert
        approved epics that are all that stands between this board and no work.
        """
        if self.no_work_anywhere:
            if self.intake.tracker_configured:
                return self.NO_WORK_SENTENCE
            return self.NO_WORK_SENTENCE_NO_TRACKER
        if not self._nothing_found:
            return ""
        parts = []
        unread = self.unread_work_sources
        if unread:
            parts.append(
                "Could not determine whether the product has work: "
                f"{'; '.join(unread)}. Nothing claimable or drafted was found in what "
                "could be read."
            )
        n = self.health.inert_count
        if n:
            slugs = ", ".join(r.slug for r in self.health.inert_promotions)
            if self.health.inert_promotions.truncated:
                slugs += f" and {n - self.health.inert_promotions.shown} more"
            verb = "is" if n == 1 else "are"
            parts.append(
                f"Nothing is claimable, but {_plural(n, 'approved epic')} {verb} inert: {slugs} "
                "- see HEALTH for why each cannot dispatch."
            )
        return " ".join(parts)

    def section(self, name: str):
        """Look a section up by its ``SECTION_ORDER`` name — how renderers iterate."""
        if name not in SECTION_ORDER:
            raise KeyError(f"unknown section {name!r}; the locked order is {SECTION_ORDER}")
        return getattr(self, name)

    def sections(self) -> list:
        """(name, section) in the LOCKED order, every section present even when empty."""
        return [(name, self.section(name)) for name in SECTION_ORDER]

    def snapshot(self) -> dict:
        """The machine-readable core the archived page embeds, and the next diff reads.

        Deliberately NOT the whole report: it carries the state each diffable thing is IN,
        not its prose. That keeps the archived fence small and stable, so a wording change in
        a renderer can never read as a state change on the next run.
        """
        return {
            "version": SNAPSHOT_VERSION,
            "generated_at": self.generated_at,
            "project": self.project,
            "lanes": {lane.agent: lane.state for lane in self.lanes.lanes},
            "epics": dict(self._epic_states),
            # The RECONCILED progress signal, kept beside the approval signal because they
            # answer different questions and SHIPPED needs this one. "Only a status.md with
            # `status: done` does" mark an epic done; spec frontmatter lagging merged reality
            # is the fleet's documented normal drift, so an `epics` map keyed on
            # `current_state` recorded a shipped epic identically before and after it shipped.
            "lifecycle": dict(self._lifecycle_states),
            "questions": dict(self._question_states),
            "issues": {
                "available": self.intake.available,
                "untriaged": self.intake.untriaged_count,
                "selectable": self.intake.selectable_count,
                "fix_claimed": self.intake.fix_claimed_count,
                "triaged_numbers": sorted(self._triaged_numbers),
            },
            # ONE builder, called here and at `collect`'s `core["health"]`. Not two dicts that
            # agree: the diff reads THIS method's output one report ago as `was` and `collect`'s
            # core as `now`, so a key spelled differently on the two sides is a metric being
            # compared against a different metric. See `_health_metrics`.
            "health": _health_metrics(self.health, self.lanes, self.counted_skipped),
        }

    # The three raw maps the snapshot needs are collected alongside the sections and stashed
    # here rather than recomputed: recomputing from capped section rows would silently diff
    # only the top `rows_per_section` epics.
    _epic_states: dict = field(default_factory=dict, repr=False, compare=False)
    _lifecycle_states: dict = field(default_factory=dict, repr=False, compare=False)
    _question_states: dict = field(default_factory=dict, repr=False, compare=False)
    _triaged_numbers: tuple = field(default=(), repr=False, compare=False)
    #: The exact dict `collect` handed to `diff_since` as the NOW side, stashed for the same
    #: reason the three maps above are — so the pair can be asserted EQUAL rather than assumed
    #: equal. The two calls to `_health_metrics` do not see the same `LanesSection`: `collect`
    #: builds the core before `_attach_production_signal` rebinds `lanes`, and `snapshot()`
    #: reads the rebound one. `silent_count` survives that `replace()` today by omission, not
    #: by intent, which is precisely the kind of agreement worth a test rather than a comment.
    _core_health: dict = field(default_factory=dict, repr=False, compare=False)

    def snapshot_json(self) -> str:
        return json.dumps(self.snapshot(), indent=2, sort_keys=True)

    def snapshot_fence(self) -> str:
        """The fenced block a renderer embeds in the archived page."""
        return f"```{SNAPSHOT_FENCE_TAG}\n{self.snapshot_json()}\n```"


# --------------------------------------------------------------------------- #
# Age formatting
# --------------------------------------------------------------------------- #
def format_age(hours: Optional[float]) -> str:
    """Render an age the way the lanes diagram does: ``1h51m`` / ``15m`` / ``3d 2h``.

    None renders as ``unknown`` rather than ``0`` — an unreadable timestamp is not a fresh
    one, and a report that shows a dead lane as "beat 0m" is actively misleading.
    """
    if hours is None or (isinstance(hours, float) and math.isnan(hours)):
        return "unknown"
    total_minutes = int(round(hours * 60))
    if total_minutes < 0:
        return "in the future"
    days, rem = divmod(total_minutes, 1440)
    hrs, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hrs}h"
    if hrs:
        return f"{hrs}h{mins:02d}m"
    return f"{mins}m"


def _age_hours(stamp: str, now: datetime) -> Optional[float]:
    parsed = _registry._parse_iso(stamp)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# Health primitives
# --------------------------------------------------------------------------- #
def approved_epics(records: Sequence[EpicRecord]) -> list[EpicRecord]:
    """Records carrying the standing human approval, whatever else is true of them."""
    return [r for r in records if _survey._is_approved(r.current_state)]


def inert_promotions(
    records: Sequence[EpicRecord],
    *,
    project: Optional[str],
    excluded_domains: Sequence[str] = (),
) -> list[EpicRecord]:
    """Approved epics that no builder can pick up — SCOPED ON BOTH SIDES to one project.

    **The scoping is the whole contract.** ``survey_epics()`` is project-WIDE while
    ``dispatchable(..., project=X)`` drops foreign-project records by design, so comparing a
    wide approved set against a narrow dispatchable set "reports every other project's
    approved work as inert". Running it naively on 2026-08-31 flagged five epics; three were
    another project's perfectly healthy specs. Both sides here come from the SAME record list
    and the same ``project``, and the approved side drops foreign records itself, so the
    asymmetry cannot be reintroduced by a caller passing an unscoped list.

    Note what is NOT asserted anywhere: a specific finding count. That number is live-corpus
    state — ``steward`` measured zero against the same corpus the "three" came from — and it
    changes with every promotion. The property worth testing is the asymmetry, not the
    arithmetic.
    """
    scoped = [r for r in records if not _survey._is_foreign(r, project)]
    claimable = {
        r.slug
        for r in _survey.dispatchable(
            scoped, tuple(excluded_domains), project=project, warn_foreign=False
        )
    }
    return [r for r in approved_epics(scoped) if r.slug not in claimable]


def inert_reason(record: EpicRecord, excluded_domains: Sequence[str] = ()) -> str:
    """Which of ``dispatchable``'s gates an approved epic failed, in words, every one named.

    Mirrors the queue_class and excluded-domain filters rather than calling them, because
    ``dispatchable`` answers only yes or no. The foreign-project gate never appears here:
    ``inert_promotions`` drops foreign records before it asks.
    """
    reasons = []
    qc = record.queue_class
    if qc is not None and qc not in _survey.DISPATCHABLE_QUEUE_CLASSES:
        if qc in _survey.NON_DISPATCHABLE_QUEUE_CLASSES:
            reasons.append(f"queue_class {qc!r} is known but not dispatchable")
        else:
            reasons.append(f"queue_class {qc!r} is out of vocabulary")
    excluded = {d for d in excluded_domains if d}
    if excluded and not record.build_approved_by:
        hit = sorted({v for v in (record.domain, record.project) if v in excluded})
        if hit:
            reasons.append(f"excluded domain {', '.join(hit)} with no build_approved_by")
    return "; ".join(reasons) or "not dispatchable for a reason this report cannot name"


def out_of_vocabulary_states(records: Sequence[EpicRecord]) -> list[tuple]:
    """(slug, current_state) for every record whose ``current_state`` is off the lifecycle.

    Unset is NOT out-of-vocabulary: legacy specs predate the field, and reporting every one
    of them would bury the handful of real typos this count exists to surface.
    """
    out = []
    for r in records:
        norm = _survey._norm_state(r.current_state)
        if norm is None:
            if r.current_state is not None and not isinstance(r.current_state, str):
                out.append((r.slug, str(r.current_state)))
            continue
        if norm not in KNOWN_CURRENT_STATES:
            out.append((r.slug, norm))
    return sorted(out)


def classify_lane(
    row,
    *,
    now: datetime,
    silent_agents: frozenset,
    lane_has_work: Optional[bool] = None,
    work_basis: str = "",
    awaiting: str = "",
    selection: str = "",
    dead_referent: str = "",
) -> LaneRow:
    """Classify one Agents row into LIVE / HALTED-BY-DESIGN / REAPED / SILENT.

    ``dead_referent`` is the `registry.unjudgeable_halts` reason when this row is halted on a
    referent nothing will answer (a key of ``DEAD_REFERENT_FIXES``), computed once per report
    like ``silent_agents``. It changes the HALTED why-string, never the state: the lane is
    still halted, and it reaches the human through BLOCKED ON YOU.

    Order of tests is load-bearing:

    1. **REAPED** first — the reap stamps ``phase`` and deliberately PRESERVES the dead row's
       heartbeat and eta as evidence, so a reaped row looks stale on every other signal
       forever. Testing staleness first would report every reap as a fresh silent lane.
    2. **HALTED-BY-DESIGN** next — a self-declared halt is blocked on an answer. It keeps
       waking on schedule, but no wake of its own unblocks it, so whatever its heartbeat says,
       it needs its question answered, not a diagnosis.
    3. **SILENT** last — dead on BOTH liveness signals (``registry.stale_rows``: heartbeat
       older than the threshold AND an eta that has lapsed or is absent) while claiming to be
       neither halted nor reaped. This is the only one of the three that is undiagnosed, and
       the only one where the next step is "find out why" rather than a known fix.

    **An unreadable or absent heartbeat is SILENT, not LIVE, on a row that claims work.**
    ``stale_rows`` deliberately ``continue``s past a heartbeat it cannot parse, and says why:
    "a fresh install must not report its whole roster as dead". That is a *reaping* default,
    and inheriting it as a *health verdict* rendered a lane that has never stamped a beat as a
    green node with "0 silent" beside it — the reassuring half of exactly the stall this
    report was written for. ``format_age`` already refuses to render ``None`` as ``0m`` for
    this reason; the state field must not undo that honesty. The fresh-install shape the
    reaping default protects is narrower than "no heartbeat": it is an ``idle`` row holding no
    work, and only that stays non-alarming.

    ``silent_agents`` is computed once per report from ``stale_rows`` and injected, so this
    function reads no clock of its own beyond the ``now`` it is handed.

    TWO FURTHER STATES REFINE WHAT WOULD OTHERWISE BE LIVE, and they are tested after the
    three problem states because a reaped or silent lane is not merely dry:

    4. **BLOCKED** — the seat has an open Question it raised or an open Blocker it owns.
       It may still be building something else; the point is that a decision of ITS is
       waiting. Applies to every seat, since any seat can raise a question. Tested
       before DRY: a decision waiting on a human is more actionable than "nothing
       published yet", and a lane can honestly be both.
    5. **DRY** — nothing arriving through the channel this seat's work actually arrives
       through. Nothing is wrong; there is simply nothing to do, and the fix is *work
       published*, not diagnosis. **DEFINED FOR ALL SIX SEATS.** It once was not:
       ``registry.LANE_OF`` maps only the builder lanes, so the three lane-less seats were
       passed ``lane_has_work=None`` and could never render unproductive at all.
       ``_lane_has_work`` is now total and answers each seat on its own channel — the builders
       on their Queue lane, `auditor` / `steward` / `dev-manager` on the routed-note inbox —
       and ``work_basis`` names which one replied, so DRY says what is empty instead of only
       that something is, and never prescribes a remedy the seat cannot act on.

       lead-dev's channel is its Queue lane AND the claimable epics and bugs it selects from
       (``SELECTION_BASIS``), because its lane holds what it has already picked. With startable
       work, or approved epics whose claim state is ambiguous, and an empty lane it stays LIVE
       and ``selection`` becomes its why - what it can start, or might - since a seat about to
       pick is neither dry nor healthy-and-silent.

       ``lane_has_work=None`` is still honoured and still means "not applicable, never no
       work". Nothing in the collector passes it any more; it stays because a caller with a
       seat this function cannot ask about must be able to say so rather than guess.

    Both are DERIVED. Nothing writes them to an Agents row, and the derivation lives here
    rather than in each renderer so every surface gets the same answer.
    """
    age = _age_hours(row.heartbeat, now)
    common = dict(
        agent=row.agent,
        work=row.work,
        phase=row.phase,
        heartbeat=row.heartbeat,
        heartbeat_age_hours=age,
        eta=row.eta,
        waiting_on=row.waiting_on,
    )
    if row.phase.strip().startswith(_registry.STOOD_DOWN_PHASE):
        # BEFORE the reap and halt branches, and long before SILENT. Without it a stood-down
        # lane falls all the way through to SILENT, whose why-string reads "the row claims
        # neither halt nor reap - undiagnosed" -- the report saying it cannot tell why the lane
        # is quiet, printed beside a cell that says so in words. The reason travels with the
        # mark, so the human reads WHY off the report instead of asking.
        reason = row.phase.strip()[len(_registry.STOOD_DOWN_PHASE):].lstrip(": ").strip()
        # Only a row that HOLDS work keeps a claim. Saying so over `work: -` told the human to
        # expect work intact on resume where there was none to keep.
        held = row.work.strip() not in ("", "-")
        return LaneRow(
            state=LANE_STOOD_DOWN,
            why=(
                f"a human stopped this lane on purpose: {reason or 'no reason recorded'}. "
                + ("It keeps its claim; " if held else "It holds no claim; ")
                + "relaunching it clears the mark."
            ),
            **common,
        )
    if _registry.is_reaped_phase(row.phase):
        fresh = age is not None and age <= REAPED_FRESH_HOURS
        return LaneRow(
            state=LANE_REAPED,
            why="killed by the stale sweep; relaunch it",
            reaped_recent=fresh,
            **common,
        )
    if row.status == "halted":
        resume = row.waiting_on.strip()
        if resume in ("", "-"):
            resume = "unstated - the halt names no resume condition"
        why = f"self-declared halt; resumes when: {resume}"
        if dead_referent in DEAD_REFERENT_FIXES:
            # "resumes when: Q-0001" over an answered or absent Q-0001 promises a resume that
            # will never come. The reason and the fix travel with the row, and the lane is
            # listed in BLOCKED ON YOU, which is where the human acts on it.
            why = (
                f"halted on {resume}, which is dead ({dead_referent}): "
                f"{DEAD_REFERENT_FIXES[dead_referent]}"
            )
        return LaneRow(
            state=LANE_HALTED,
            why=why,
            resume_condition=resume,
            **common,
        )
    if age is None and not (row.status == "idle" and row.work.strip() in ("", "-")):
        return LaneRow(
            state=LANE_SILENT,
            why=(
                f"heartbeat unreadable or absent ({row.heartbeat!r}) while the row claims "
                f"status {row.status!r} - undiagnosed"
            ),
            **common,
        )
    if row.agent in silent_agents:
        return LaneRow(
            state=LANE_SILENT,
            why=(
                f"last beat {format_age(age)} ago; {_eta_account(row.eta, now)}. The row "
                "claims neither halt nor reap - undiagnosed"
            ),
            **common,
        )
    if awaiting:
        return LaneRow(
            state=LANE_BLOCKED,
            why=f"a decision of its own is waiting: {awaiting}",
            **common,
        )
    if lane_has_work is False:
        empty = work_basis or LANE_BASIS
        # THE REMEDY IS NOT THE SAME FOR BOTH CHANNELS, and saying it were shipped a confident
        # falsehood on the first real run. "needs work, not diagnosis" is right for a builder:
        # its lane is filled by lead-dev, so an empty one really is a call to publish. It is
        # WRONG for a lane-less seat -- `steward` is a self-feeding sweeper whose work is
        # generated by time passing, so telling a human to feed it work through its inbox
        # names an action nobody can take. Worse, that seat renders DRY precisely when it has
        # DRAINED its inbox, which is the hygiene the fleet asks of it. The empty half of the
        # sentence stays true and useful; the prescription is replaced by what that seat's
        # output actually is, mirroring the abstention `_credit` makes one column over.
        if work_basis == INBOX_BASIS:
            own = _NON_PR_OUTPUT.get(row.agent, "not measured here")
            why = (
                f"nothing waiting for it in {empty} - which is a drained inbox, not idleness. "
                f"Its own output is {own}, which this report does not measure"
            )
        else:
            why = f"nothing waiting for it in {empty}; needs work, not diagnosis"
            if selection:
                why += f" ({selection})"
        return LaneRow(state=LANE_DRY, why=why, **common)
    if work_basis == SELECTION_BASIS and selection:
        return LaneRow(state=LANE_LIVE, why=f"{row.status} with {selection}", **common)
    return LaneRow(state=LANE_LIVE, **common)


def _eta_account(eta: str, now: datetime) -> str:
    """What the eta cell says about a silent lane, which is its most actionable fact.

    ``stale_rows`` admits a row whose eta LAPSED or is ABSENT, and those read differently to a
    human deciding what to do: "4h overdue" is a promise broken by a known margin, "no eta
    recorded" is a row that never promised anything. One fixed phrase for both hid the margin.
    """
    if eta.strip() in ("", "-"):
        return "no eta recorded"
    parsed = _registry._parse_iso(eta)
    if parsed is None:
        return f"eta unreadable ({eta!r})"
    parsed = parsed.astimezone(timezone.utc)
    shown = parsed.strftime("%H:%MZ" if parsed.date() == now.astimezone(timezone.utc).date()
                            else "%Y-%m-%dT%H:%MZ")
    overdue = (now - parsed).total_seconds() / 3600.0
    if overdue <= 0:
        return f"promised back by {shown}, not yet due"
    return f"promised back by {shown}, {format_age(overdue)} overdue"


def directive_name(text: str) -> str:
    """A short name for a directive bullet: its leading ``**bold**`` span, else its first
    clause. Rendering the count with names was an explicit ask — "3 directives in force" is a
    number nobody can act on.

    THE MINTED STAMP COMES OFF FIRST, through the registry's own helper (gh#202). ``_cmd_directive``
    writes every Directive as ``[<ISO>] <text>``, and the clause split below breaks on ``[.;:]`` --
    so the colon inside ``T05:12Z`` fired before a single content character was reached and every
    live Directive was named after its own timestamp.

    The names were DISTINCT, which is what let this survive: nothing looked duplicated, so no
    reader saw a collision to report. They were merely content-free. An acceptance criterion of
    "the names differ" passes on the broken code, and this docstring says so because the issue
    that filed it says "every Directive renders as the same name" -- measured over the ten live
    ones, all ten differ.

    Routing through ``directive_match_text`` rather than re-stripping here is the point of the
    fix. gh#127 was this same stamp in the MATCHING surface, where it let the ack guard be
    satisfied with metadata the CLI generated itself; that fix put one named helper in charge of
    saying where the machine's prefix ends and the human's words begin. A second private stripper
    in this module is how the two surfaces drift apart again. It also strips by CONTENT rather
    than shape -- the bracket comes off only when the registry's own parser reads it as a
    timestamp -- so a human bullet opening ``[URGENT]`` keeps its words.
    """
    stripped = _registry.directive_match_text(text).strip()
    m = re.match(r"\*\*(?P<name>[^*]+)\*\*", stripped)
    if m:
        return m.group("name").strip()
    head = re.split(r"[.;:]| - |—", stripped, maxsplit=1)[0].strip()
    return head if len(head) <= 72 else head[:69].rstrip() + "..."


# --------------------------------------------------------------------------- #
# Archive reads
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(
    rf"^```{re.escape(SNAPSHOT_FENCE_TAG)}\s*\n(?P<body>.*?)\n```",
    re.MULTILINE | re.DOTALL,
)


def archive_directory(inputs: ReportInputs) -> Optional[Path]:
    """Where this project's reports are archived, or None when no vault is configured."""
    if inputs.vault_root is None:
        return None
    return Path(inputs.vault_root) / inputs.archive_subdir / inputs.project


def archive_filename(now: datetime) -> str:
    """Chronologically sortable, so "the latest report" is a filename sort."""
    return now.astimezone(timezone.utc).strftime(ARCHIVE_STAMP_FORMAT) + ARCHIVE_SUFFIX


def find_latest_archive(directory: Optional[Path]) -> Optional[Path]:
    """The most recent archived report, by filename. Never creates the directory."""
    if directory is None:
        return None
    d = Path(directory)
    if not d.is_dir():
        return None
    pages = sorted(p for p in d.glob("*" + ARCHIVE_SUFFIX) if p.is_file())
    return pages[-1] if pages else None


def parse_archived_snapshot(text: str) -> Optional[dict]:
    """Pull the embedded snapshot out of an archived page, or None if it carries none.

    A page with no fence (hand-written, or written by an older renderer) is not an error: it
    simply cannot be diffed against, and the caller reports that rather than guessing.
    """
    m = _FENCE_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group("body"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def load_prior_snapshot(inputs: ReportInputs) -> tuple[Optional[dict], str, str]:
    """(snapshot, path, reason). Pure read — the archive directory is never created."""
    directory = archive_directory(inputs)
    if directory is None:
        return None, "", "no vault configured - archive skipped, so there is nothing to diff"
    latest = find_latest_archive(directory)
    if latest is None:
        return None, "", "no archived report found"
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return None, str(latest.name), f"could not read the latest archived report: {e}"
    snapshot = parse_archived_snapshot(text)
    if snapshot is None:
        return None, latest.name, "the latest archived report carries no snapshot block"
    return snapshot, latest.name, ""


# --------------------------------------------------------------------------- #
# The diff
# --------------------------------------------------------------------------- #
def diff_since(prior: Optional[dict], current: dict, *, prior_path: str = "", reason: str = "") -> ChangedSince:
    """Compute CHANGED SINCE, or say plainly that there is nothing to compare against.

    "If no prior report exists, say so explicitly rather than silently rendering everything
    as new." Every collection field stays empty in that case — the alternative, treating the
    whole corpus as newly-changed, makes the first report of any install look like a crisis.
    """
    if not prior:
        return ChangedSince(
            no_prior=True,
            note=f"first report - no prior to diff ({reason})" if reason else "first report - no prior to diff",
            prior_path=prior_path,
        )
    if prior.get("version") != SNAPSHOT_VERSION:
        return ChangedSince(
            no_prior=True,
            note=(
                "first report - no prior to diff (the latest archived report carries a version "
                f"{prior.get('version')!r} snapshot and this report reads version "
                f"{SNAPSHOT_VERSION})"
            ),
            prior_path=prior_path,
        )

    prior_lanes = prior.get("lanes") or {}
    now_lanes = current.get("lanes") or {}
    lane_transitions = tuple(
        LaneTransition(agent=a, was=prior_lanes.get(a, "absent"), now=now_lanes[a])
        for a in sorted(now_lanes)
        if prior_lanes.get(a, "absent") != now_lanes[a]
    )

    prior_epics = prior.get("epics") or {}
    now_epics = current.get("epics") or {}
    epic_moves = tuple(
        EpicMove(slug=s, was=prior_epics.get(s, "new"), now=now_epics[s])
        for s in sorted(now_epics)
        if prior_epics.get(s, "new") != now_epics[s]
    )

    prior_life = prior.get("lifecycle") or {}
    now_life = current.get("lifecycle") or {}
    lifecycle_moves = tuple(
        EpicMove(slug=s, was=prior_life.get(s, "new"), now=now_life[s])
        for s in sorted(now_life)
        if prior_life.get(s, "new") != now_life[s]
    )

    prior_q = prior.get("questions") or {}
    now_q = current.get("questions") or {}
    opened = tuple(q for q in sorted(now_q) if q not in prior_q and now_q[q] == "open")
    answered = tuple(
        q for q in sorted(now_q) if now_q[q] == "answered" and prior_q.get(q) != "answered"
    )
    applied = tuple(
        q for q in sorted(now_q) if now_q[q] == "applied" and prior_q.get(q) != "applied"
    )
    retracted = tuple(
        q for q in sorted(now_q)
        if now_q[q] == _registry.Q_RETRACTED and prior_q.get(q) != _registry.Q_RETRACTED
    )

    prior_triaged = set((prior.get("issues") or {}).get("triaged_numbers") or [])
    now_triaged = set((current.get("issues") or {}).get("triaged_numbers") or [])
    issues_triaged = tuple(sorted(now_triaged - prior_triaged))

    prior_health = prior.get("health") or {}
    now_health = current.get("health") or {}
    crossings = []
    for metric, threshold in sorted(HEALTH_THRESHOLDS.items()):
        was = prior_health.get(metric)
        now_value = now_health.get(metric)
        if not isinstance(now_value, int):
            continue
        # A METRIC ALREADY OVER ITS THRESHOLD USED TO BE SILENT FOREVER. This was a pure edge
        # detector: `crossed` only on the report where the value first went over. That is a
        # reasonable design for a metric that returns to zero, and useless for one that does
        # not. Measured: the live note backlog has been over 40 continuously and has never
        # been under it, so an edge detector fires ZERO times on it - a threshold that cannot
        # produce a signal, which is the "number rather than a signal" this epic set out to
        # fix. The same silence applies to a standing `inert_promotions` or `survey_skips`.
        #
        # So it reports the LEVEL and labels the EDGE: `crossed` the report it goes over,
        # `over` every report it stays over, `cleared` when it comes back. A renderer that
        # wants only the transitions filters on the direction; one that wants the current
        # state reads them all. The reverse - dropping the label - would lose the "this is
        # new" signal, which is the more urgent of the two.
        #
        # An ABSENT prior reading counts as 0 rather than as "unknown". A metric added by a
        # release has no key in the previous release's archived snapshot, so treating absence
        # as unknown made every new threshold silent on the one report where it is most worth
        # seeing.
        if not isinstance(was, int):
            was = 0
        if now_value >= threshold:
            crossings.append(HealthCrossing(
                metric, was, now_value, threshold, "crossed" if was < threshold else "over"))
        elif was >= threshold:
            crossings.append(HealthCrossing(metric, was, now_value, threshold, "cleared"))

    changed = ChangedSince(
        no_prior=False,
        note="",
        prior_path=prior_path,
        prior_generated_at=str(prior.get("generated_at") or ""),
        lane_transitions=lane_transitions,
        epic_moves=epic_moves,
        lifecycle_moves=lifecycle_moves,
        questions_opened=opened,
        questions_answered=answered,
        questions_applied=applied,
        questions_retracted=retracted,
        issues_triaged=issues_triaged,
        health_crossings=tuple(crossings),
    )
    if changed.is_empty:
        # "The report can prove a quiet period" — a real and useful sentence that could
        # previously only be asserted, never demonstrated.
        stamp = changed.prior_generated_at or "the last report"
        changed = replace(changed, note=f"nothing changed since {stamp}")
    return changed


# --------------------------------------------------------------------------- #
# Git / filesystem drift (pure reads)
# --------------------------------------------------------------------------- #
def _default_git_runner(argv: list) -> tuple:
    # The same runner serves `git` and `gh`, so a failure names argv[0]: "git not found" for a
    # missing gh sends the reader to fix a tool that is working.
    binary = Path(argv[0]).name if argv else "command"
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_GIT_TIMEOUT, encoding="utf-8",
        )
    except FileNotFoundError as e:
        return 127, "", f"{binary} not found: {e}"
    except subprocess.SubprocessError as e:
        return 1, "", f"{binary} failed to run: {e}"
    return proc.returncode, proc.stdout, proc.stderr


def _branch_drift(inputs: ReportInputs, runner: Optional[GitRunner]) -> BranchDrift:
    """Local branches in the canonical checkout. Read-only (`git branch --format`).

    A failure is RECORDED, never swallowed: `available=False` plus the reason git gave, so
    the report says "could not look" instead of printing an empty list that reads as clean.
    """
    if inputs.repo_path is None:
        return BranchDrift(reason="no canonical checkout configured", base_branch=inputs.base_branch)
    repo = Path(inputs.repo_path)
    if not repo.is_dir():
        return BranchDrift(
            reason=f"canonical checkout not present ({repo.name})", base_branch=inputs.base_branch
        )
    active = runner if runner is not None else _default_git_runner
    argv = ["git", "-C", str(repo), "branch", "--format=%(refname:short)"]
    code, out, err = active(argv)
    if code != 0:
        # BOTH streams, labelled (gh#478). The runner synthesises `git not found` onto err with
        # an empty out, while a git that RAN and failed can put its advisories on err and the
        # refusal on out -- so no precedence is right and the reader is handed both.
        return BranchDrift(
            reason=f"git branch exited {code}; {describe_streams(out, err)}",
            base_branch=inputs.base_branch,
        )
    branches = tuple(line.strip() for line in out.splitlines() if line.strip())
    return BranchDrift(available=True, base_branch=inputs.base_branch, branches=branches)


def _worktree_drift(inputs: ReportInputs, agent_rows, runner: Optional[GitRunner] = None) -> WorktreeDrift:
    """Worktree directories on disk vs the lanes that claim them. Directory listing only.

    **Listed at ``<worktree_root>/<project>``, one level down.** The layout is
    ``<worktree_root>/<project>/<epic-slug>`` — config.py says so in its own remediation and
    six SKILL.md files write it out. Listing the ROOT yielded the project directories, so every
    project in the data root was reported as a permanently unclaimed worktree (noise that
    trains the reader to ignore the row, growing by one per project) while the actual abandoned
    per-epic worktree the check exists to find was never seen. Scoping to the project also
    keeps this section consistent with the rest of the report, which is project-scoped by
    contract.
    """
    if inputs.worktree_root is None:
        return WorktreeDrift(reason="no worktree root configured")
    root = Path(inputs.worktree_root) / inputs.project
    if not root.is_dir():
        return WorktreeDrift(
            reason=f"no worktree directory for this project yet ({inputs.project})"
        )
    try:
        dirs = tuple(sorted(p.name for p in root.iterdir() if p.is_dir()))
    except OSError as e:
        return WorktreeDrift(reason=f"could not list the worktree root: {type(e).__name__}: {e}")
    claims = [
        row.work.strip()
        for row in agent_rows
        if not _registry.is_reaped_phase(row.phase) and row.work.strip() not in ("", "-")
    ]
    unclaimed = tuple(d for d in dirs if not _claims_worktree(d, claims))
    registered, registration_note = _registered_worktrees(inputs, root, runner)
    if registration_note:
        # The git half failed. Report the disk half and say why the other is absent, rather than
        # emitting two empty tuples that a reader cannot tell from "nothing is drifting".
        return WorktreeDrift(
            available=True, directories=dirs, unclaimed=unclaimed,
            registration_note=registration_note,
        )
    unregistered = tuple(d for d in dirs if d not in registered)
    missing = tuple(sorted(n for n in registered if n not in set(dirs)))
    return WorktreeDrift(
        available=True, directories=dirs, unclaimed=unclaimed,
        unregistered=unregistered, missing=missing,
    )


#: `e0768-account-limit-keystone` and `E-0768-the-fleet-cannot-survive` are the same epic, and no
#: substring of either appears in the other. The number is the only thing both sides carry.
_WT_EPIC_NUM = re.compile(r"(?i)\be-?(\d{3,})")


def _claims_worktree(directory: str, claims: Sequence[str]) -> bool:
    """Does any live claim name this worktree directory?

    THE SUBSTRING TEST ALONE REPORTS A BUILDING WORKTREE AS UNCLAIMED (gh#456), measured on the
    live board: the directory is `e0768-account-limit-keystone`, the lane's work cell is
    `E-0768-the-fleet-cannot-survive`, and neither string contains the other, so every directory
    on the root came back unclaimed -- including the one a lane was actively building in.

    That was survivable while the section only listed directories. It is not survivable beside
    the registration columns added here: a row reading *registered, present, unclaimed* invites
    exactly the removal that destroys in-flight work, and the new columns are what make the
    removal look safe.

    SO THE EPIC NUMBER IS TRIED FIRST AND THE SUBSTRING TEST IS KEPT AS A FALLBACK, rather than
    replacing it. The number is the one token both spellings carry. Keeping the fallback makes
    this change PURELY ADDITIVE -- it can only ever find a claim the old test missed, never lose
    one it found -- and that asymmetry is the point rather than caution: a false *claimed* says
    "someone may own this" and stays a reader's hand, while a false *unclaimed* is what invites
    the delete. Only one of those two errors destroys work, so the fallback is allowed to be
    generous in the direction that cannot.
    """
    m = _WT_EPIC_NUM.search(directory)
    if m:
        want = m.group(1).lstrip("0") or "0"
        for c in claims:
            cm = _WT_EPIC_NUM.search(c)
            if cm and (cm.group(1).lstrip("0") or "0") == want:
                return True
    return any(directory in c or (c and c in directory) for c in claims)


def _registered_worktrees(
    inputs: ReportInputs, root: Path, runner: Optional[GitRunner]
) -> tuple:
    """``(names registered under root, note)`` from ``git worktree list --porcelain``.

    Only worktrees whose parent IS this project's root are returned, so the canonical checkout —
    always the first entry — and any sibling project's trees are not mistaken for this one's.
    """
    if inputs.repo_path is None:
        return frozenset(), "no canonical checkout configured, so registration was not read"
    repo = Path(inputs.repo_path)
    if not repo.is_dir():
        return frozenset(), f"canonical checkout not present ({repo.name})"
    active = runner if runner is not None else _default_git_runner
    code, out, err = active(["git", "-C", str(repo), "worktree", "list", "--porcelain"])
    if code != 0:
        # Both streams, labelled (gh#478) -- see `_branch_drift` for why neither wins.
        return frozenset(), f"git worktree list exited {code}; {describe_streams(out, err)}"
    names = set()
    root_resolved = _norm_wt_path(root)
    for line in (out or "").splitlines():
        if not line.startswith("worktree "):
            continue
        raw = line[len("worktree "):].strip()
        if not raw:
            continue
        norm = raw.replace("\\", "/").rstrip("/")
        parent, _, leaf = norm.rpartition("/")
        if leaf and _norm_wt_path(parent) == root_resolved:
            names.add(leaf)
    return frozenset(names), ""


def _norm_wt_path(value) -> str:
    """One spelling for a path both git and the config can name.

    GIT PRINTS THE FULLY RESOLVED REAL PATH and the config does not have to. `_require_dir`
    expands `~` and nothing else, so a `worktree_root` spelled through a junction or a symlink,
    or carrying a `..` segment, or written relative, reaches this comparison in a spelling git
    will never emit. Compared textually, NOTHING matches: the registered set comes back empty,
    every live worktree is printed as unregistered, and `registration_note` stays empty — so the
    report states the git side was read successfully while saying the opposite of the truth, and
    the remedy the docs attach to `unregistered` points at a directory a lane is building in.

    `missing` cannot catch it either, because an empty registered set has nothing to be missing.

    Reproduced three times independently during review, on a junction, on a `..` spelling and on
    a relative one. It does not fire on today's configuration, which is exactly why it is worth
    closing now rather than the day someone moves the root behind a link.

    Resolution failures are swallowed deliberately: a path that cannot be resolved falls back to
    its literal normalised form, which is the behaviour this replaces, so the worst case is what
    already shipped rather than a report that raises.
    """
    p = Path(value)
    try:
        p = p.resolve()
    except OSError:
        pass
    return str(p).replace("\\", "/").rstrip("/").lower()


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
#: (section heading, header cell, minimum cells, id predicate) for each registry table
#: `parse_registry` filters. Every one of these filters DROPS a row it does not like, in
#: silence — that is a scan, so it owes a skip count.
_REGISTRY_TABLES: tuple = (
    ("Agents", "agent", 7, lambda c: c in _registry.AGENTS, "agent name is not one of the fleet's loops"),
    ("Queue", "id", 7, lambda c: bool(_registry._E_ID.match(c)), "id is not an E-NNNN row id"),
    ("Blockers", "id", 6, lambda c: bool(_registry._B_ID.match(c)), "id is not a B-NNNN row id"),
)


def _registry_skips(text: str, reg: _registry.Registry, redact: Sequence[str]) -> list:
    """Rows the registry parser dropped or mis-columned, as ``SkippedFile`` rows.

    ``parse_registry`` is a scan of a 183 KB file and it filters silently in four ways: a row
    with too few cells, an unknown agent name, an id that does not match its pattern, and — the
    quiet one — a row with too MANY cells, which it truncates to the first 7, shifting every
    column after the stray ``|``. An epic title or a note fragment carrying an unescaped pipe
    therefore drops a lane out of LANES and the human-owned blocker under it out of BLOCKED ON
    YOU, while COUNTED / SKIPPED said `counted 0, skipped 0`. That is the exact defect the HARD
    RULE exists to close, on the one file the whole report leans on.
    """
    skips: list[SkippedFile] = []
    for section, header0, minimum, id_ok, id_reason in _REGISTRY_TABLES:
        try:
            body = _registry._get_body(text, section)
        except _registry.RegistryError:
            continue  # the section is missing; the caller already reports that
        for index, cells in enumerate(_registry._table_rows(body, header0), start=1):
            if not any(c for c in cells):
                continue
            name = cells[0] or f"<row {index}>"
            if len(cells) < minimum:
                reason = f"row has {len(cells)} cells, expected at least {minimum}"
            elif not id_ok(cells[0]):
                reason = id_reason
            elif len(cells) > minimum:
                reason = (
                    f"row has {len(cells)} cells, expected {minimum} - an unescaped '|' shifts "
                    "every column after it, so this row parsed with the wrong values"
                )
            else:
                continue
            skips.append(
                SkippedFile(
                    scan="registry",
                    name=name,
                    parent=section.lower(),
                    reason=normalise_reason(reason, redact),
                    kind=f"unparseable {section} row",
                )
            )
    try:
        q_body = _registry._get_body(text, "Questions")
    except _registry.RegistryError:
        q_body = ""
    for line in q_body.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("### "):
            continue
        heading = stripped[4:].strip()
        if heading in reg.questions:
            continue
        skips.append(
            SkippedFile(
                scan="registry",
                name=heading or "<blank heading>",
                parent="questions",
                reason="heading is not a Q-NNNN block id, so the block was not parsed",
                kind="unparseable Questions block",
            )
        )
    return skips


def _read_registry(
    inputs: ReportInputs,
) -> tuple[Optional[_registry.Registry], int, str, ScanCount]:
    """(registry, bytes, reason, scan). A degenerate registry is REPORTED, never raised.

    ``parse_registry`` raises ``RegistryError`` on a file missing any ``## <Section>`` heading
    — which a zero-byte, half-written, or **CRLF** registry all are, because its section regex
    is ``^## Agents[ \\t]*$`` and a stray ``\\r`` defeats it. Letting that escape killed the
    whole report with a traceback in the one state where the human most needs the picture: the
    report is what they read *instead of* the registry.
    """
    label = "registry"
    if inputs.registry_path is None:
        return None, 0, "no registry path configured", ScanCount(label=label, counted=0)
    path = Path(inputs.registry_path)
    redact = redaction_prefixes(inputs.data_root)
    try:
        raw = path.read_bytes()
    except OSError as e:
        reason = normalise_reason(f"could not read the registry: {type(e).__name__}: {e}", redact)
        return None, 0, reason, ScanCount(
            label=label,
            counted=0,
            skipped=(SkippedFile(label, path.name, path.parent.name, reason, "unreadable registry"),),
        )
    text = raw.decode("utf-8", errors="replace")
    try:
        reg = _registry.parse_registry(text)
    except _registry.RegistryError as e:
        reason = normalise_reason(f"could not parse the registry: {e}", redact)
        return None, len(raw), reason, ScanCount(
            label=label,
            counted=0,
            skipped=(SkippedFile(label, path.name, path.parent.name, reason, "unparseable registry"),),
        )
    counted = len(reg.agents) + len(reg.queue) + len(reg.questions) + len(reg.blockers)
    return reg, len(raw), "", ScanCount(
        label=label, counted=counted, skipped=tuple(_registry_skips(text, reg, redact))
    )


def _collect_intake(
    inputs: ReportInputs,
    *,
    issues: Optional[Sequence],
    runner,
    cap: int,
) -> tuple[IntakeSection, tuple]:
    """INTAKE, and the triaged issue numbers the diff needs.

    Untriaged and selectable are computed from ONE issue list. Two separate tracker calls
    could disagree with each other mid-report, and the pair being consistent is the whole
    point of the pair.
    """
    capped = False
    issues_supplied = issues is not None
    if issues is None:
        # The guard is `issue_intake`'s OWN no-op rule, not a falsiness test. `work_source.kind`
        # DEFAULTS to "filesystem" and "none" is a valid value: both are truthy strings, so a
        # falsiness guard let them through, `list_open_issues` short-circuited on its own rule
        # and returned [] without shelling out, and INTAKE printed "0 untriaged / 0 selectable"
        # as a MEASUREMENT for a tracker nobody looked at. That pair is the section's whole
        # reason to exist - "either number alone told nothing" - so a fabricated one is worse
        # than none.
        if not _intake._tracker_active(inputs.tracker_kind, inputs.tracker_cli):
            if inputs.tracker_kind in _intake._TRACKER_KINDS:
                # The project DOES keep its work in a tracker; only the CLI to read it is
                # missing. That is an unread source, never "no tracker", or a broken CLI
                # config would earn the no-tracker all-clear.
                return IntakeSection(
                    reason=(
                        f"tracker CLI unusable (work_source.kind={inputs.tracker_kind}, "
                        f"cli={inputs.tracker_cli or 'unset'})"
                    ),
                    tracker_configured=True,
                ), ()
            return IntakeSection(
                reason=(
                    "no issue tracker for this project "
                    f"(work_source.kind={inputs.tracker_kind or 'unset'}, "
                    f"cli={inputs.tracker_cli or 'unset'})"
                )
            ), ()
        if not inputs.tracker_host_ref:
            return IntakeSection(
                reason="no tracker host_ref configured for this project", tracker_configured=True
            ), ()
        try:
            issues = _intake.list_open_issues(
                inputs.tracker_host_ref,
                kind=inputs.tracker_kind,
                cli=inputs.tracker_cli,
                runner=runner,
                limit=inputs.issue_limit,
            )
        except _intake.IssueIntakeError as e:
            return IntakeSection(reason=f"tracker unreachable: {e}", tracker_configured=True), ()
        # A tracker page that came back exactly full is indistinguishable from one that was
        # truncated, and the untriaged/selectable pair would then be a partial count with no
        # skip beside it.
        capped = len(issues) >= inputs.issue_limit
    specs_dir = Path(inputs.data_root) / "projects" / inputs.project / "design" / "specs"
    try:
        linked = _intake.spec_linked_issue_numbers(specs_dir)
    except _intake.IssueIntakeError as e:
        return IntakeSection(
            reason=f"could not read carved-spec cross-links: {e}",
            tracker_configured=True,
        ), ()
    untriaged = _intake.untriaged(issues, linked)
    selectable = _intake.builder_selectable(issues)
    claimed = _intake.label_claimed(issues)
    awaiting = _intake.awaiting_decision(issues)
    triaged = tuple(sorted(i.number for i in issues if _intake.TRIAGED_LABEL in i.labels))
    fix_count, fix_claimed, fix_note = _intake_fix_claims(
        inputs, issues, runner=runner, specs_dir=specs_dir, issues_supplied=issues_supplied
    )
    if capped and fix_count is not None:
        # fix_claims ran over a truncated open-issue page; say so beside the number.
        fix_note = "; ".join(
            n for n in (fix_note, "open-issue page capped; the count may be partial") if n
        )
    return (
        IntakeSection(
            available=True,
            tracker_configured=True,
            untriaged_count=len(untriaged),
            selectable_count=len(selectable),
            untriaged=_cap(untriaged, cap),
            selectable=_cap(selectable, cap),
            claimed=_cap(claimed, cap),
            awaiting_count=len(awaiting),
            awaiting=_cap(awaiting, cap),
            capped=capped,
            limit=inputs.issue_limit,
            all_selectable=tuple(selectable),
            all_claimed=tuple(claimed),
            all_awaiting=tuple(awaiting),
            fix_claimed_count=fix_count,
            fix_claimed=_cap(fix_claimed, cap),
            fix_claims_note=fix_note,
        ),
        triaged,
    )


def _intake_fix_claims(
    inputs: ReportInputs, issues: Sequence, *, runner, specs_dir: Path, issues_supplied: bool
) -> tuple:
    """``(count, claims, note)`` - INTAKE's fix-claimed count over the issue list it already read.

    One more tracker read (the merged PRs) and the done specs, then ``fix_claims`` alone: no
    comments, no registry. Any read that cannot run gives ``count=None`` and a note, and never
    touches the untriaged/selectable pair the caller already computed.
    """
    kind, cli = inputs.tracker_kind, inputs.tracker_cli
    if not _intake._tracker_active(kind, cli):
        return None, [], (
            f"merged PRs not read: no issue tracker (work_source.kind={kind or 'unset'}, "
            f"cli={cli or 'unset'})"
        )
    if not inputs.tracker_host_ref:
        return None, [], "merged PRs not read: no tracker host_ref configured"
    if issues_supplied and runner is None:
        # A supplied issue list is a fixture; reading the live tracker beside it would mix a
        # fixture's issues with real PRs.
        return None, [], "merged PRs not read: the issue list was supplied without a runner"
    try:
        prs = _intake.list_merged_prs(
            inputs.tracker_host_ref, kind=kind, cli=cli, runner=runner,
            limit=_intake.MERGED_PR_LIMIT + 1,
        )
    except _intake.IssueIntakeError as e:
        return None, [], f"merged PRs not read: {e}"
    prs, prs_capped = _intake._probe_page(prs, _intake.MERGED_PR_LIMIT)
    try:
        links = _intake.done_spec_links(specs_dir)
    except _intake.IssueIntakeError as e:
        return None, [], f"done specs not read: {e}"
    claims = _intake.fix_claims(issues, prs, links)
    note = (
        f"merged PR read capped at {_intake.MERGED_PR_LIMIT}; the count may be partial"
        if prs_capped else ""
    )
    return len(claims), claims, note


def collect(
    inputs: ReportInputs,
    *,
    now: datetime,
    issues: Optional[Sequence] = None,
    issue_runner=None,
    git_runner: Optional[GitRunner] = None,
    prior: Optional[dict] = None,
) -> FleetReport:
    """Collect the fleet snapshot. ONE data pass; every rendering reads the result.

    PURE READ. Nothing here writes, labels, promotes, reaps or notes — see the module
    docstring. ``now`` is injected so the report is testable at an exact boundary and this
    function never reads a clock of its own, matching ``registry.stale_rows`` /
    ``registry.missed_wakes``.

    ``issues`` / ``issue_runner`` / ``git_runner`` / ``prior`` are seams: supply them to
    collect against fixtures, omit them to collect against the live environment.
    """
    cap = inputs.rows_per_section
    generated_at = now.astimezone(timezone.utc).isoformat()

    # ---- raw reads ------------------------------------------------------- #
    # `include_archived=True` is load-bearing here for the same reason it is at every other
    # survey call site in the fleet: without it an epic archived between two reports simply
    # drops out of the corpus, produces no move, never appears in SHIPPED, and vanishes from
    # the report in silence. The WORK section does the excluding itself, on `lifecycle_state`,
    # where it is visible.
    scoped_records, epic_scan = survey_with_skips(
        inputs.data_root,
        inputs.mtime_cutoff_iso,
        project=inputs.project,
        label="epics",
        include_archived=True,
    )
    reg, registry_bytes, registry_reason, registry_scan = _read_registry(inputs)
    # Built HERE rather than at the constructor 100 lines down, because `core["health"]` needs
    # it: `survey_skips` is every scan's skips, and the epic survey is only one of the two.
    counted_skipped = CountedSkippedSection(scans=(epic_scan, registry_scan))
    intake, triaged_numbers = _collect_intake(
        inputs, issues=issues, runner=issue_runner, cap=cap
    )

    # ---- prior + the diff, computed before the sections that depend on it -- #
    prior_path = ""
    prior_reason = ""
    if prior is None:
        prior, prior_path, prior_reason = load_prior_snapshot(inputs)

    epic_states = {
        r.slug: (_survey._norm_state(r.current_state) or r.lifecycle_state)
        for r in scoped_records
    }
    lifecycle_states = {r.slug: r.lifecycle_state for r in scoped_records}
    question_states = (
        {qid: q.fields.get("state", "") for qid, q in reg.questions.items()} if reg else {}
    )

    # WORK's startable half is built BEFORE LANES, because lead-dev's work arrives through it:
    # lead-dev picks from the claimable epics and bugs and only publishes into its Queue lane
    # afterwards, so an empty lane beside startable work is a seat about to pick, not a dry one.
    # Only SHIPPED needs the diff, and it is attached once the diff exists.
    claims_unread = "" if reg is not None else (registry_reason or "the registry was not read")
    work = _work_section(
        reg, scoped_records, None, inputs=inputs, cap=cap, scan=epic_scan,
        claims_unread=claims_unread,
    )
    # WORK's bug side is a RE-SLICE of the issue list INTAKE already read, never a second
    # tracker call: "two separate tracker calls could disagree with each other mid-report".
    # Same one-read discipline, one section further along.
    work = _attach_bug_work(work, intake, reg, claims_unread=claims_unread, cap=cap)
    # Third pass rather than a branch inside either half: the signal spans BOTH columns
    # (an epic can be held by a bug's files and the reverse), so it cannot be computed
    # until both are assembled.
    work = _attach_seam_signal(work, scoped_records, inputs)

    lanes = _lanes_section(
        reg, now=now, stale_hours=inputs.stale_hours, selection=_lead_selection(work, intake)
    )
    core = {
        "version": SNAPSHOT_VERSION,
        "generated_at": generated_at,
        "project": inputs.project,
        "lanes": {lane.agent: lane.state for lane in lanes.lanes},
        "epics": epic_states,
        "lifecycle": lifecycle_states,
        "questions": question_states,
        "issues": {
            "available": intake.available,
            "untriaged": intake.untriaged_count,
            "selectable": intake.selectable_count,
            "fix_claimed": intake.fix_claimed_count,
            "triaged_numbers": sorted(triaged_numbers),
        },
        "health": {},  # filled below, once the health counts exist
    }

    # ---- sections -------------------------------------------------------- #
    blocked = _blocked_section(reg, scoped_records, now=now, cap=cap, scan=epic_scan)
    health = _health_section(
        reg,
        scoped_records,
        inputs=inputs,
        now=now,
        cap=cap,
        registry_bytes=registry_bytes,
        git_runner=git_runner,
        scan=epic_scan,
    )
    # The NOW side of every health comparison. Same builder as `FleetReport.snapshot()`, which
    # produced the WAS side one report ago — see `_health_metrics` for why they cannot be two
    # dicts. `lanes` here is the pre-attach binding; the test pins the pair end to end.
    core["health"] = _health_metrics(health, lanes, counted_skipped)
    changed = diff_since(prior, core, prior_path=prior_path, reason=prior_reason)
    decided = _decided_section(reg, changed, cap=cap)
    work = _attach_shipped(work, changed, cap)

    # One read, two consumers: DONE renders the merged PRs, and WORK's diff-derived rows
    # borrow them for their sha/pr evidence. Reading twice would be two answers.
    done, merged_prs = _done_section(inputs, git_runner, cap)
    # Third consumer of the SAME read. LANES gets the production half of its answer here rather
    # than in `_lanes_section`, because every liveness signal that section computes answers
    # "is it alive" and none of them answers "is it producing" — and a lane green on liveness
    # and zero on production for N wakes is the exact shape nobody caught.
    lanes = _attach_production_signal(
        lanes,
        merged_prs,
        reg,
        inputs=inputs,
        available=done.available,
        reason=done.reason,
        window=done.window or f"last {DONE_WINDOW_LIMIT} merged PRs",
    )
    if merged_prs and work.shipped.items:
        work = replace(
            work,
            shipped=Rows(
                items=tuple(_attach_pr_evidence(list(work.shipped.items), merged_prs)),
                total=work.shipped.total,
            ),
        )

    directory = archive_directory(inputs)
    filename = archive_filename(now)
    archive = ArchiveInfo(
        available=directory is not None,
        reason="" if directory is not None else "no vault configured in the environment - archive skipped",
        # The FULL directory, and the full file path beside it. `find_latest_archive` reads
        # `archive_directory(inputs)`; anything less than the whole path here lets the writer
        # and the reader disagree, and a disagreement is silent forever.
        directory=str(directory) if directory is not None else "",
        filename=filename,
        path=str(directory / filename) if directory is not None else "",
        prior_path=prior_path,
    )

    return FleetReport(
        generated_at=generated_at,
        project=inputs.project,
        rows_per_section=cap,
        blocked=blocked,
        decided=decided,
        intake=intake,
        work=work,
        done=done,
        lanes=lanes,
        health=health,
        counted_skipped=counted_skipped,
        changed_since=changed,
        archive=archive,
        registry_available=reg is not None,
        registry_reason=registry_reason,
        _epic_states=epic_states,
        _lifecycle_states=lifecycle_states,
        _question_states=question_states,
        _triaged_numbers=tuple(sorted(triaged_numbers)),
        _core_health=core["health"],
    )


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #
def _blocked_section(reg, records, *, now: datetime, cap: int, scan: ScanCount) -> BlockedSection:
    """1. BLOCKED ON YOU — open Questions with AGE, oldest first; human-owned Blockers;
    the count of drafted specs awaiting promotion.

    Ordering here is oldest-first by explicit instruction, NOT `order_open_questions`'s
    urgency-then-age triage order: this section answers "what has been waiting on me
    longest", and a fresh halted question jumping a week-old one hides exactly the row a
    human needs to feel bad about.
    """
    questions: list[QuestionRow] = []
    blockers: list[BlockerRow] = []
    if reg is not None:
        for qid, q in reg.questions.items():
            if q.fields.get("state") != "open":
                continue
            raised_at = q.fields.get("raised_at", "")
            questions.append(
                QuestionRow(
                    qid=qid,
                    urgency=q.fields.get("urgency", ""),
                    raised_by=q.fields.get("raised_by", ""),
                    question=q.fields.get("question", ""),
                    raised_at=raised_at,
                    age_hours=_age_hours(raised_at, now),
                    default=q.fields.get("default", ""),
                    slug=q.fields.get("slug", ""),
                )
            )
        # An unreadable raised_at cannot claim to be the oldest, so it sorts last within the
        # section rather than jumping the queue on a `None`.
        questions.sort(key=lambda r: (r.age_hours is None, -(r.age_hours or 0.0), r.qid))
        for bid, b in reg.blockers.items():
            if b.state != "open" or b.owner.strip().lower() not in HUMAN_BLOCKER_OWNERS:
                continue
            blockers.append(
                BlockerRow(
                    bid=bid,
                    raised_by=b.raised_by,
                    owner=b.owner,
                    work=b.work,
                    description=b.description,
                )
            )
        blockers.sort(key=lambda r: r.bid)

    drafted = [
        r for r in records if _survey._norm_state(r.current_state) == DRAFTED_CURRENT_STATE
    ]
    drafted_sorted = _by_priority_then_slug(drafted)
    stranded = [
        StrandedLaneRow(
            agent=u.agent, waiting_on=u.waiting_on, reason=u.reason,
            fix=DEAD_REFERENT_FIXES[u.reason],
        )
        for u in _dead_referent_halts(reg, now).values()
    ]
    return BlockedSection(
        questions=_cap(questions, cap),
        blockers=_cap(blockers, cap),
        drafted_awaiting_promotion=len(drafted),
        drafted_slugs=tuple(r.slug for r in drafted_sorted),
        scan=scan,
        stranded=_cap(stranded, cap),
    )


def _dead_referent_halts(reg, now: datetime) -> dict:
    """``{agent: UnjudgeableHalt}`` for the halted lanes whose referent is dead.

    ONE classifier: `registry.unjudgeable_halts`, the function behind `read --stranded`, filtered
    to `DEAD_REFERENT_FIXES`. Both BLOCKED ON YOU and the HALTED row's why-string read this, so
    the two surfaces cannot name different lanes, and neither can disagree with the CLI.
    """
    if reg is None:
        return {}
    return {
        u.agent: u
        for u in _registry.unjudgeable_halts(reg, now)
        if u.reason in DEAD_REFERENT_FIXES
    }


def _decided_section(reg, changed: ChangedSince, *, cap: int) -> DecidedSection:
    """2. DECIDED BY AGENTS — Questions answered under delegated authority since the last
    report.

    "Delegated authority" is every ``answered_by`` value in ``DELEGATED_ANSWERERS``, which is the
    registry's own ``ANSWERED_BY`` less ``human``: the steward answers as the primary delegate
    and dev-manager as the fallback, and a question the human answered is not the fleet
    exercising judgement. Each row keeps ``answered_by`` so the reader sees which seat decided.
    The window comes from the diff: with a prior report on disk only the newly-answered rows
    show, and with no prior every currently-answered row shows once (``windowed=False``), which
    the renderer says out loud rather than implying a window it does not have.
    """
    rows: list[DecidedRow] = []
    if reg is not None:
        if changed.no_prior:
            qids = [
                qid for qid, q in reg.questions.items()
                if q.fields.get("state") in ("answered", "applied")
            ]
        else:
            # BOTH halves of the window. `diff_since` partitions questions by their CURRENT
            # state, so a question that went open -> answered -> applied inside the window lands
            # in `questions_applied` and never in `questions_answered`. The raiser applies an
            # answer on its next wake (~30 min) while the report's ceiling is 24h, so that is
            # the normal completion, not an edge case: reading only `questions_answered` dropped
            # most delegated decisions from the section whose whole purpose is that the fleet's
            # judgement is otherwise never seen. The no-prior branch above already accepted both.
            qids = list(
                dict.fromkeys(
                    tuple(changed.questions_answered) + tuple(changed.questions_applied)
                )
            )
        for qid in qids:
            q = reg.questions.get(qid)
            if q is None or q.fields.get("answered_by") not in DELEGATED_ANSWERERS:
                continue
            rows.append(
                DecidedRow(
                    qid=qid,
                    question=q.fields.get("question", ""),
                    answer=q.fields.get("answer", ""),
                    answered_by=q.fields.get("answered_by", ""),
                    answered_at=q.fields.get("answered_at", ""),
                    raised_by=q.fields.get("raised_by", ""),
                )
            )
        rows.sort(key=lambda r: (r.answered_at, r.qid), reverse=True)
    return DecidedSection(
        questions=_cap(rows, cap),
        window_start=changed.prior_generated_at,
        windowed=not changed.no_prior,
    )


def _work_section(
    reg,
    records,
    changed: Optional[ChangedSince],
    *,
    inputs: ReportInputs,
    cap: int,
    scan: ScanCount,
    claims_unread: str = "",
) -> WorkSection:
    """4. WORK — approved-and-claimable, in-progress with eta, drafted count, SHIPPED.

    SHIPPED is derived from the diff, not from a session sweep: "the first draft swept the
    whole session for shipped - right once and wrong forever after." With no prior report the
    table is empty and ``shipped_note`` says the window does not exist yet, which is the
    honest answer rather than a list of everything ever done.

    ``changed`` may be None, and ``collect`` passes None: LANES needs this section's startable
    counts before the diff exists (the diff reads LANES), so SHIPPED is attached afterwards by
    ``_attach_shipped`` - the only part of WORK the diff feeds.

    ``claims_unread`` is the reason the registry could not be read, when it could not. A
    ``reg`` of None alone is not that signal: a direct caller with no board has no claims, which
    is a different fact from a board nobody could open.
    """
    # `dispatchable` IS the claimable rule (approval + queue_class allow-list + project +
    # excluded domains). Re-deriving it here would be a second, drifting copy of the one gate
    # that makes gateless auto-merge safe. The extra lifecycle filter catches the one case
    # dispatchable cannot see: it never reads status.md, so an epic whose status says `done`
    # while a merged plan sidecar still carries `approved-for-autonomous` would otherwise be
    # offered to the human as claimable work that already shipped.
    #
    # THE TERMINAL SET IS BORROWED, NEVER RESTATED. This spelled `("done", "archived")` inline —
    # a THIRD subset of the same vocabulary, after the one gh#261 removed from `_done_slugs`
    # and the canonical `TERMINAL_CURRENT_STATES` it was a subset of (gh#325).
    #
    # WHICH LIFECYCLE WORDS CAN REACH THIS FILTER. Pass 1 derives `lifecycle_state` FROM
    # `current_state`, and pass 2 freezes both to the spec's value together when the spec is
    # terminal, so a `superseded` record is refused by `dispatchable` on approval alone. Pass 3
    # reads the board and is the only pass that can set a lifecycle word beside a spec that
    # still reads approved: `done`, `running`, or one of the ended board statuses
    # (`_ENDED_BOARD_STATUSES`: abandoned, failed, done_on_hold). `done` is caught by the
    # terminal set. The ended statuses are deliberately NOT in that set - it means "satisfies a
    # `depends_on`", and an ended run put nothing on main - so they are excluded here in their
    # own right: a run that ended is not work a lane can start, whatever its spec still says.
    # An ended board therefore counts toward no WORK list, so a board whose only epics have
    # ended does not suppress the no-work alert. Both sets are read through the module, never bound or restated.
    # `test_a_terminal_lifecycle_cannot_diverge_from_its_current_state` pins the pass 1/2
    # property; `test_an_ended_board_is_not_claimable_while_its_spec_still_reads_approved` pins
    # the pass 3 one.
    #
    # It still earns the fix for gh#261's reason rather than a victim: a subset of a vocabulary
    # is how two answers drift, each list looking reasonable alone, and the drift is invisible
    # until something depends on it. Deriving costs one attribute lookup.
    #
    # THE OTHER TWO `done` SPELLINGS IN THIS FILE ARE NOT THIS DEFECT and must not be widened
    # with it. `SECTION_ORDER`'s `"done"` is a section name, not a state. The SHIPPED filter
    # below asks `now == "done" and was != "done"` — a TRANSITION, not set membership; widening
    # it would print a spec reconciled to `superseded` as an epic that shipped.
    claimable = _by_priority_then_slug([
        r
        for r in _survey.dispatchable(
            list(records),
            tuple(inputs.excluded_domains),
            project=inputs.project,
            warn_foreign=False,
        )
        if r.lifecycle_state not in _survey.TERMINAL_CURRENT_STATES
        and r.lifecycle_state not in _survey._ENDED_BOARD_STATUSES
    ])
    running = _by_priority_then_slug([r for r in records if r.lifecycle_state == "running"])
    drafted = [r for r in records if _survey._norm_state(r.current_state) == DRAFTED_CURRENT_STATE]

    # THE JOIN KEY IS THE ID'S NAME, and before E-0260 there was no key at all. This dict was
    # built from the Agents `work` cell — an epic id — and then read with `r.slug`, so it never
    # matched anything: an epic a lane was actively building rendered with a blank eta and a
    # blank lane on every report ever produced, silently, because a missing eta and an unstarted
    # epic look identical in the output. Nothing failed; the columns were simply always empty.
    #
    # An id now carries its name, so the two sides finally have something in common. The name is
    # a TRUNCATION of the slug, not the slug, so the lookup is keyed on the same `_short_name`
    # both sides derive from — never on a substring test, which would let `acme-3-5-chat` match
    # a longer slug that merely starts the same way. A legacy bare `E-0220` has no name and
    # resolves to nothing, exactly as it did before: it costs a blank cell, not a wrong one.
    eta_by_name: dict = {}
    lane_by_name: dict = {}
    if reg is not None:
        for row in reg.agents.values():
            name = _registry._epic_name(row.work)
            if name:
                eta_by_name.setdefault(name, row.eta)
                lane_by_name.setdefault(name, row.agent)

    def _key(slug: str) -> str:
        try:
            return _registry._short_name(slug)
        except _registry.RegistryError:
            return ""  # a slug with no derivable name matches no id, which is the honest answer

    # IN FLIGHT IS DERIVED FROM THE CLAIM, exactly as the bug column already does it
    # (`_attach_bug_work`, gh#56/E-0212). `running` above reads `lifecycle_state == "running"`,
    # and NO SPEC FRONTMATTER IN THIS CORPUS CARRIES THAT VALUE - measured 2026-09-22, with an
    # epic actively being built: `in_progress` was EMPTY while a lane held it. One absent column
    # degraded three signals, which is why this is not cosmetic: the stall alert counted the
    # held epic as claimable (gh#298), and the gh#121 seam check builds its holder list from
    # `work.in_progress`, so with no epic holders `seam_blocked` could only ever name bugs.
    #
    # A CLAIM, not a label. The registry is written by the lane that took the work, and a
    # terminal row's `claimed_by` is provenance rather than a claim - so terminal rows are
    # excluded here, or a shipped epic would read as in flight forever.
    # THE NAME IS NOT AN IDENTITY, so the join cannot end at the name. `_epic_name` yields the
    # first four hyphen-tokens of the slug and `_short_name` yields the same four from the spec
    # side, which is exactly why they join - but `_short_name`'s own docstring records that seven
    # such names are shared by more than one spec on this data root, and a survey of it while
    # writing this found nine in one project. The recurring shape is `<slug>` beside
    # `<slug>-plan` / `<slug>-followups`, which this fleet generates routinely.
    #
    # Keyed on the name alone, ONE claim suppresses EVERY approved spec sharing it: the untouched
    # sibling drops out of `claimable_now` and prints as in flight under the other lane's name and
    # eta, and with nothing else claimable the stall alert fires on a board that has startable
    # work. That is the gh#298 failure inverted, and worse - it halts the pump instead of hiding a
    # stall. Measured against this code with two colliding specs and one claim, then confirmed by
    # three independent refuters who each reproduced it.
    #
    # So a claim resolves to at most one epic. A Queue row carries the full slug in its
    # `work_item` title, which disambiguates exactly; an `## Agents` row carries only the id and
    # cannot. When a name resolves to several candidates and nothing separates them, NONE is
    # claimed as held - they go to `claim_ambiguous`, are kept out of `claimable_now`, and are
    # named. Silently subtracting them is what produced the false halt.
    # QUEUE CLAIMS ARE READ FIRST AND THE AGENTS ROW ONLY FILLS WHAT THEY LEAVE, because the two
    # carry different kinds of fact and the board routinely disagrees with itself. A Queue row IS
    # the state machine - it has a `state` column that says `building` or `parked`. An `## Agents`
    # row is a heartbeat: it says a lane is alive and names what it holds, and it carries no state
    # for the EPIC at all. Measured on the live board while writing this: jr-dev-2's Agents row
    # read `building` against an epic its own Queue row had `parked`, and reading both as claims
    # put that one epic into `in_progress` AND into `parked_count` - the same double-count this
    # commit set out to remove, arriving by the other door.
    queue_claims: list = []  # (name, work_item title, is_parked)
    agent_claims: list = []  # (name,)
    # A PUBLISHED row is not a claim, but it is not free work either: it sits in one junior's
    # lane and only that junior may take it. Skipping it with the unclaimed rows left it in
    # `claimable_now`, credited to lead-dev as something it picks from, when lead-dev cannot
    # select, publish or build it. Resolved to an epic by the same name-then-title join as a
    # claim, below, so a collision still falls to `claim_ambiguous`.
    published: list = []  # (name, work_item title, lane, row id)
    if reg is not None:
        for row in getattr(reg, "queue", {}).values():
            state = getattr(row, "state", "")
            if state in _registry.TERMINAL_QUEUE_STATES:
                continue
            if getattr(row, "claimed_by", "-") in ("-", ""):
                name = _registry._epic_name(getattr(row, "id", ""))
                # `ready` only, the same test LANES uses, so the two sections never disagree
                # about whether a lane has work.
                if name and state == "ready":
                    published.append((
                        name, getattr(row, "work_item", "") or "",
                        getattr(row, "lane", "") or "", getattr(row, "id", ""),
                    ))
                continue
            name = _registry._epic_name(getattr(row, "id", ""))
            if name:
                queue_claims.append((name, getattr(row, "work_item", "") or "", state == "parked"))
        for row in reg.agents.values():
            name = _registry._epic_name(row.work)
            if name and not any(name == q[0] for q in queue_claims):
                agent_claims.append((name, "", False))
    claims = queue_claims + agent_claims

    by_key: dict = {}
    for r in claimable:
        k = _key(r.slug)
        if k:
            by_key.setdefault(k, []).append(r)

    held_building: dict = {}  # slug -> record
    held_parked: dict = {}
    ambiguous: dict = {}

    def _resolve(name: str, hint: str) -> list:
        """The approved epics a row's id could mean, narrowed by its title when that separates them."""
        candidates = by_key.get(name, [])
        if len(candidates) > 1:
            # The title is the only thing carrying the full slug; use it when it separates them.
            narrowed = [r for r in candidates if hint and r.slug in hint]
            candidates = narrowed if len(narrowed) == 1 else candidates
        return candidates

    for name, hint, is_parked in claims:
        candidates = _resolve(name, hint)
        if not candidates:
            continue
        if len(candidates) != 1:
            for r in candidates:
                ambiguous.setdefault(r.slug, r)
            continue
        record = candidates[0]
        (held_parked if is_parked else held_building)[record.slug] = record

    published_rows: dict = {}  # slug -> PublishedRow
    for name, hint, lane, row_id in published:
        candidates = _resolve(name, hint)
        if not candidates:
            continue
        if len(candidates) != 1:
            for r in candidates:
                ambiguous.setdefault(r.slug, r)
            continue
        published_rows.setdefault(
            candidates[0].slug,
            PublishedRow(name=candidates[0].slug, lane=lane, row_id=row_id, kind="epic"),
        )

    # A parked row is claimed but nobody is building it. It leaves `claimable_now` and does NOT
    # enter `in_progress`; `parked_count` is where it is reported. Counting it in both - which
    # this commit did until the review caught it - contradicts the rule the same commit wrote
    # into report/SKILL.md and overstates how much of the board is moving.
    parked_count = len(held_parked)
    for slug in held_building:
        ambiguous.pop(slug, None)
    for slug in held_parked:
        ambiguous.pop(slug, None)
    # A claim outranks a publish: once a row holds it, the epic is in flight or parked, and
    # naming it as waiting in a lane too would count one epic in two buckets. An epic a claim
    # MIGHT hold stays in `claim_ambiguous` alone, the conservative reading.
    for slug in [*held_building, *held_parked, *ambiguous]:
        published_rows.pop(slug, None)

    running_names = {r.slug for r in running}
    in_progress_records = list(running) + [
        r for slug, r in held_building.items() if slug not in running_names
    ]
    in_progress = [
        EpicRow.of(r, eta=eta_by_name.get(_key(r.slug), ""), lane=lane_by_name.get(_key(r.slug), ""))
        for r in _by_priority_then_slug(in_progress_records)
    ]
    unstartable = (
        {r.slug for r in in_progress_records} | set(held_parked) | set(ambiguous)
        | set(published_rows)
    )
    claimable_now = [r for r in claimable if r.slug not in unstartable]
    if claims_unread:
        # No claim was read, so none was subtracted: every startable-looking epic MIGHT be held.
        # Same discipline as a name collision - kept out of `claimable_now` and named - because
        # an unreadable board is the widest ambiguity there is, not the absence of one.
        for r in claimable_now:
            ambiguous.setdefault(r.slug, r)
        claimable_now = []
    claim_ambiguous = _by_priority_then_slug(list(ambiguous.values()))

    work = WorkSection(
        approved_claimable=_cap([EpicRow.of(r) for r in claimable], cap),
        claimable_now=_cap([EpicRow.of(r) for r in claimable_now], cap),
        in_progress=_cap(in_progress, cap),
        parked_count=parked_count,
        published_unclaimed=_cap(
            [published_rows[r.slug] for r in claimable if r.slug in published_rows], cap
        ),
        claim_ambiguous=_cap([EpicRow.of(r) for r in claim_ambiguous], cap),
        claim_note=(
            f"no claim could be checked - {claims_unread}" if claims_unread else ""
        ),
        drafted_count=len(drafted),
        scan=scan,
    )
    return work if changed is None else _attach_shipped(work, changed, cap)


def _attach_shipped(work: WorkSection, changed: ChangedSince, cap: int) -> WorkSection:
    """WORK's SHIPPED table, from the diff - the one part of WORK that needs it."""
    if changed.no_prior:
        shipped_rows: list[ShippedRow] = []
        shipped_note = "no prior report - the shipped window does not exist yet"
    else:
        # From `lifecycle_moves`, never `epic_moves`. THE KEYING IS RIGHT AND THE OLD REASON
        # WAS WRONG (gh#319): this used to cite "only a status.md with `status: done` marks an
        # epic done", which is false — a terminal frontmatter marks it done too when no status
        # board exists, and for agentflow none does. The keying survives on a better reason:
        # `lifecycle_moves` catches BOTH routes to done, so it reports a ship whichever way the
        # epic got there, while the approval map catches only the frontmatter one.
        # An epic that merges with its spec frontmatter still reading
        # `approved-for-autonomous` — the fleet's documented normal lag — moves on the
        # lifecycle map and not on the approval map. Keyed on the approval map, a wake on which
        # three epics shipped printed an empty SHIPPED table under the sentence "nothing changed
        # since <stamp>".
        shipped_rows = [
            ShippedRow(slug=m.slug)
            for m in changed.lifecycle_moves
            if m.now == "done" and m.was != "done"
        ]
        shipped_note = ""
    return replace(work, shipped=_cap(shipped_rows, cap), shipped_note=shipped_note)


def _attach_bug_work(
    work: WorkSection,
    intake: IntakeSection,
    reg,
    *,
    claims_unread: str = "",
    cap: Optional[int] = None,
) -> WorkSection:
    """Split INTAKE's already-read issues into WORK's bug columns.

    in-flight is decided by EITHER claim route: a `gh#N` appearing in a claimed registry row
    (E-0212's route, and the one with a reaper behind it), or the `in-progress` marker on the
    issue itself (gh#56's route, and the only one issue selection can see). The registry claim
    is the record of who is building what; the marker is the gate that stops a selector
    offering the issue - written at different moments by different mechanisms, so they are not
    two copies of one fact.

    The union with `intake.claimed` is load-bearing, not tidiness. `intake.selectable` no
    longer contains marked issues, so splitting it alone would drop a bug out of BOTH columns
    at the moment a lane took it - a board reading `0 in progress` while a lane builds, which
    is exactly what an idle fleet reads like. `intake.awaiting` joins the union for the same
    reason (gh#446): a registry row can hold a `needs-decision` bug that carries no marker.
    Unclaimed, it stays out of `bug_claimable` - a human call comes before any build.
    """
    if not intake.available:
        # Say WHY the column is empty. "no open bugs" and "we could not look" are different
        # facts and a reader acts differently on each; INTAKE already refuses to conflate
        # them and WORK must not undo that one section later.
        return replace(work, bug_note=intake.reason or "tracker not available")

    claimed_text = []
    published_rows = []  # (row id, lane, text) for `ready` rows nobody has claimed
    if reg is not None:
        for row in reg.agents.values():
            claimed_text.append(f"{row.work} {row.phase}")
        for row in getattr(reg, "queue", {}).values():
            if getattr(row, "claimed_by", "-") not in ("-", ""):
                claimed_text.append(f"{row.id} {row.work_item}")
            elif getattr(row, "state", "") == "ready":
                published_rows.append((row.id, row.lane, f"{row.id} {row.work_item}"))

    # Split the UNCAPPED halves and cap only what is rendered. Splitting INTAKE's capped rows
    # made every total below `len()` of at most `rows_per_section` items, and a `Rows` whose
    # total equals its length says nothing was cut - so the seam check's `seam_partial` stayed
    # false over a view it never saw most of.
    selectable_items, claimed_items, awaiting_items = intake.bug_work_halves
    selectable_items = list(selectable_items)
    bug_work = list(selectable_items)
    for i in (*claimed_items, *awaiting_items):
        if i not in bug_work:
            bug_work.append(i)
    flying_numbers = {i.number for i in _intake.in_flight(
        [i for i in bug_work if hasattr(i, "number")], claimed_text)}

    in_flight_items = [i for i in bug_work if getattr(i, "number", None) in flying_numbers]
    claimable_items = [i for i in selectable_items if getattr(i, "number", None) not in flying_numbers]

    if claims_unread:
        # No registry claim was read, so only the `in-progress` marker separated held bugs from
        # free ones. A bug held through a registry row alone is indistinguishable from a free
        # one here - the same widest ambiguity WORK's epic side already refuses to count.
        return replace(
            work,
            bug_claimable=Rows(),
            bug_claim_ambiguous=_cap(claimable_items, cap),
            bug_in_progress=_cap(in_flight_items, cap),
            bug_untriaged_count=intake.untriaged_count,
            bug_note=(
                "only the in-progress label was checked; the registry claim check never ran - "
                f"{claims_unread}"
            ),
        )

    # A bug published into a lane and not yet claimed leaves `bug_claimable` for the reason the
    # epic side's does: only that lane's owner may take it. Matched by the same `gh#N` reading
    # as a claim, one row at a time so each bug carries the lane it waits in.
    published_bugs: list = []
    seen: set = set()
    for row_id, lane, text in published_rows:
        for i in _intake.in_flight(
            [i for i in claimable_items if hasattr(i, "number")], [text]
        ):
            if i.number not in seen:
                seen.add(i.number)
                published_bugs.append(
                    PublishedRow(name=_issue_name(i), lane=lane, row_id=row_id, kind="bug")
                )
    claimable_items = [i for i in claimable_items if getattr(i, "number", None) not in seen]
    epic_published = work.published_unclaimed
    published = list(epic_published.items) + published_bugs
    return replace(
        work,
        bug_claimable=_cap(claimable_items, cap),
        bug_in_progress=_cap(in_flight_items, cap),
        bug_untriaged_count=intake.untriaged_count,
        published_unclaimed=Rows(
            items=tuple(published if cap is None or cap < 0 else published[:cap]),
            total=epic_published.total + len(published_bugs),
        ),
    )


def _cannot_denote_a_file(p: str) -> bool:
    """Whether an entry the section parser returned could not name a file at all.

    NOT a spelling check and not a tree check - it drops only strings that no repository
    could ever contain. An absolute or parent-relative path stays: two specs naming the same
    file outside this repo really do overlap on it, and dropping those would lose a true
    signal to fix a false one.

    WHY THIS IS NOT `files_vs_diff.classify`. That function answers the same question far
    better, by placing every entry in a real tree at a real revision. It cannot be borrowed
    here: `_seam_paths` reads a spec for files that MAY NOT EXIST YET, so there is no tree to
    place them in, and the two branches below are exactly the subset that a tree cannot help
    with. `classify` keeps a tree-grounded escape on the prose branch (a repo-root filename
    containing a space) which this one has no way to check, so this predicate is the strictly
    weaker of the two by construction.
    """
    if not any(c.isalnum() for c in p):
        # ``reap`/`stand-down`` yields the bare "/" between the two adjacent inline-code
        # spans. Measured across the live corpus: 20 specs declare it, and because every one
        # of them declares the SAME string it is the corpus's most-shared "path".
        return True
    # A prose sentence promoted because it carries a "." - the parser fires on any backticked
    # token with a separator or an extension. Requiring no "/" is what keeps a real path out
    # of this branch, and it is why the predicate under-reports rather than over-reports.
    return any(c.isspace() for c in p) and "/" not in p


def _seam_paths(text: str) -> Optional[frozenset]:
    """Repo-relative paths a spec or issue body declares, or ``None`` when it does not say.

    ``None`` is a third state and never collapses into the empty set. A document with no
    ``## Files`` section, or one whose section names no real path ("the loop skills"), has
    told you nothing about what it touches - which is a different fact from telling you it
    touches nothing. Treating the two alike reports a blocked item as clear, and a wrong
    "available" is the thing nobody notices.

    Paths are normalised for separator and a single leading ``./`` only; nothing resolves
    them against a tree, because a spec names files that may not exist yet. A glob entry
    (``skills/*/SKILL.md``) is kept verbatim and therefore matches only an identical glob -
    one of the under-reporting shapes this signal accepts by design.

    ENTRIES THAT CANNOT NAME A FILE ARE DROPPED, and that is not tidiness - it is the
    difference between this signal's two error directions. The section parser promotes any
    backticked token carrying a separator or an extension, so ``reap`/`stand-down``
    contributes the bare ``/`` between the two spans. Every spec that does it contributes the
    IDENTICAL string, so the noise collides with itself: measured over the live corpus, 20
    specs declared ``/`` and it manufactured 141 pairs whose entire overlap was that one
    character - each of them a claimable item reported blocked behind an item it shares
    nothing with. gh#257 reasoned that junk rarely collides with junk; junk that is always
    spelled the same way collides with itself every time. Dropping it removed those 141 and
    added none, and corrected the ``paths`` list of 30 real blocks that were reporting more
    than they compared.
    """
    fs = _plan.files_section(text)
    if not fs.seam_checkable:
        return None
    out = set()
    for raw in fs.paths:
        p = raw.strip().replace(chr(92), "/")
        # A prefix strip, not `lstrip("./")`: that takes a character SET and would eat every
        # leading dot and slash, turning `..\shared\x.py` into `shared/x.py` and silently
        # equating a parent-relative path with a repo-relative one.
        while p.startswith("./"):
            p = p[2:]
        p = p.strip()
        if p and not _cannot_denote_a_file(p):
            out.add(p)
    # A section whose entries were ALL noise lands here with an empty set, and ``None`` is
    # the honest answer for it: the spec has said nothing locatable, which is the same third
    # state as "the loop skills" - not a claim that it touches nothing.
    return frozenset(out) if out else None


def _spec_index(inputs: "ReportInputs") -> dict:
    """slug -> spec path, built from the SURVEY'S OWN globs rather than a second guess.

    WHY THIS EXISTS, and it is the defect that made the first draft of this signal useless
    for the case gh#121 is actually about: ``survey_epics`` overwrites ``source_path`` with
    the epic's ``status.md`` for anything ``running`` (its third pass). Status bodies carry
    Progress / Current activity / Decisions log and never a ``## Files`` heading - verified
    against every status file on this machine - so reading ``record.source_path`` for an
    in-flight epic returns ``None`` every single time. The holder set was permanently empty
    and epic-blocks-epic, the exact scenario in the issue, could never be reported. The unit
    tests missed it because they hand-built records the real survey never produces.

    Reusing ``SurveyPatterns`` keeps the on-disk layout stated in one place; re-globbing by
    hand here would be a second copy that drifts the next time the layout moves.
    """
    index: dict = {}
    root = Path(getattr(inputs, "data_root", "") or "")
    project = getattr(inputs, "project", None)
    if not project or not root.is_dir():
        return index
    try:
        patterns = _survey.SurveyPatterns().for_project(project)
    except (ValueError, AttributeError):
        return index
    for pattern in patterns.spec_globs:
        for path in sorted(root.glob(pattern)):
            index.setdefault(_plan._slug_from_filename(path), path)
    return index


def _read(path) -> str:
    """File text, or ``""`` when it cannot be read.

    Unreadable is deliberately indistinguishable from absent HERE and only here: both send
    ``_seam_paths`` down its ``None`` branch, which is the honest answer either way. The
    caller is what must not conflate them with "no overlap".
    """
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def _issue_name(item) -> str:
    number = getattr(item, "number", None)
    return f"gh#{number}" if number is not None else str(getattr(item, "title", "?"))


def _attach_seam_signal(
    work: WorkSection, records: Sequence[EpicRecord], inputs: "ReportInputs"
) -> WorkSection:
    """gh#121 - say how much of WORK a free lane could actually start.

    The disjointness rule is right and this does not touch it: three builders in one seam
    produce merge conflicts even with worktrees. What was missing is a channel. On 2026-09-02
    every lane behaved correctly while the lead lane could claim nothing, because the board
    showed one healthy `building` row, three selectable bugs and non-zero WORK - and the only
    way to see it was to cross-reference a 33-file ``## Files`` section against three bug
    fixes by hand, one at a time.

    IN-FLIGHT IS NOT RE-DERIVED. It is exactly what this section already computed for its own
    two columns; a second definition of "being worked on" would drift from the first, and the
    drift would be invisible because both would look plausible.

    THREE THINGS IT REFUSES TO PRETEND, each of which the first draft got wrong:

    * An item whose file set cannot be read is UNKNOWN, never clear.
    * An item never blocks itself. A running epic whose frontmatter still says
      ``approved-for-autonomous`` appears in BOTH columns - the fleet's documented normal lag -
      and intersecting it with itself manufactures a block that reads as real.
    * The verdict covers only what the caps let it see. ``rows_per_section`` defaults to 10,
      so on a busy board the inputs are already truncated, and a clear answer over a truncated
      view is exactly the false confidence this signal exists to remove.

    THE SIGNAL UNDER-REPORTS AND THAT IS THE SAFE DIRECTION. Paths match as exact strings, so
    a directory on one side and a file inside it on the other do not intersect, and neither do
    a glob and the files it covers. A missed overlap costs a lane one bounced claim; a
    fabricated one parks real work behind nothing.
    """
    specs = _spec_index(inputs)

    def epic_paths(row) -> Optional[frozenset]:
        slug = getattr(row, "slug", None)
        if slug is None:
            return None
        # The spec first, because a RUNNING record's own source_path is its status.md.
        path = specs.get(slug)
        if path is None:
            record = next((r for r in records if r.slug == slug), None)
            path = getattr(record, "source_path", None) if record is not None else None
        return _seam_paths(_read(path)) if path else None

    def bug_paths(item) -> Optional[frozenset]:
        return _seam_paths(getattr(item, "body", "") or "")

    holders: list[tuple] = []
    unknown_holders = 0
    in_flight = [(getattr(r, "slug", "?"), epic_paths(r)) for r in work.in_progress.items]
    in_flight += [(_issue_name(i), bug_paths(i)) for i in work.bug_in_progress.items]
    for name, paths in in_flight:
        if paths:
            holders.append((name, paths))
        else:
            unknown_holders += 1

    held_names = {name for name, _ in in_flight}

    blocked: list[SeamBlock] = []
    unknown = 0
    candidates = [(getattr(r, "slug", "?"), epic_paths(r)) for r in work.approved_claimable.items]
    candidates += [(_issue_name(i), bug_paths(i)) for i in work.bug_claimable.items]
    for name, paths in candidates:
        if name in held_names:
            continue  # it is already being built; it does not block itself
        if paths is None:
            unknown += 1
            continue
        names = tuple(sorted(h for h, held in holders if h != name and paths & held))
        if names:
            hits = sorted({p for h, held in holders if h != name for p in (paths & held)})
            blocked.append(SeamBlock(name=name, blocked_by=names, paths=tuple(hits)))

    partial = any(r.truncated for r in (
        work.approved_claimable, work.bug_claimable, work.in_progress, work.bug_in_progress
    ))

    reasons = []
    if unknown_holders:
        reasons.append(
            f"{unknown_holders} in-flight item(s) declare no files - work nobody can locate "
            f"cannot be found to overlap anything"
        )
    if partial:
        reasons.append(
            "the row lists were capped before this check saw them, so it covers only the "
            "rows shown"
        )
    note = ""
    if reasons:
        note = "a clear verdict here is weaker than it looks: " + "; ".join(reasons)

    return replace(
        work,
        seam_blocked=Rows(items=tuple(blocked), total=len(blocked)),
        seam_unknown_count=unknown,
        seam_unknown_holders=unknown_holders,
        seam_partial=partial,
        seam_note=note,
    )


DONE_WINDOW_LIMIT = 20

#: OPEN pull requests get their OWN limit, and it is not the merged window's (gh#300 review).
#: `gh pr list` returns newest-first, so a cap drops the OLDEST open pull requests - which are
#: exactly the population this signal exists to find. A pull request that opened days ago, is
#: green and never landed is by construction among the oldest, so borrowing the merged window's
#: 20 made the stall detector discard the stalls first and keep the freshest rows nobody worries
#: about. A bound on MERGED pull requests is a window; the same bound on OPEN ones is a blind
#: spot. Raised, and saturation is reported rather than assumed away.
OPEN_PR_LIMIT = 200


#: The one git_host the pull request readers can read: `gh pr list` against GitHub.
_PR_READER_HOST = ("github", "gh")


def _pr_reader_refusal(inputs: "ReportInputs") -> str:
    """Why the pull request readers cannot read this project's git_host, or "" when they can.

    The readers are keyed to ``canonical_repo.host_ref`` on ``git_host``, so the guard is
    ``git_host.kind`` and ``git_host.cli`` as config resolved them, not INTAKE's work-source rule.
    Any other host gets a stated reason: shelling out to ``gh`` for a gitea project reads
    whatever GitHub repository happens to share its owner/repo and reports it as this project's.
    """
    if not inputs.tracker_host_ref:
        return "no tracker configured"
    kind = inputs.git_host_kind or "unset"
    cli = inputs.tracker_cli or "none"
    if inputs.git_host_kind != _PR_READER_HOST[0]:
        return f"no pull request reader for git_host {kind} (cli: {cli})"
    if inputs.tracker_cli != _PR_READER_HOST[1]:
        return f"no gh-compatible CLI for git_host {kind} (cli: {cli})"
    return ""


def _merged_prs(inputs: "ReportInputs", runner, limit: int) -> tuple:
    """Merged pull requests, newest first, or ``(None, reason)`` when unreachable.

    Reuses the existing ``git_runner`` seam rather than adding a second one - it already
    takes an argv list and returns ``(rc, out, err)``, which is exactly the shape a ``gh``
    call needs, and a second seam would be a second thing tests have to stub.
    """
    refusal = _pr_reader_refusal(inputs)
    if refusal:
        return None, refusal
    active = runner if runner is not None else _default_git_runner
    rc, out, err = active([
        "gh", "pr", "list",
        "--repo", inputs.tracker_host_ref,
        "--state", "merged",
        "--limit", str(limit),
        "--json", "number,title,mergedAt,mergeCommit",
    ])
    if rc != 0:
        return None, (err or "gh pr list failed").strip().splitlines()[0][:200]
    try:
        raw = json.loads(out or "[]")
    except ValueError as exc:
        return None, f"gh returned unreadable JSON: {exc}"
    prs = []
    for item in raw:
        commit = item.get("mergeCommit") or {}
        prs.append(MergedPR(
            number=str(item.get("number", "")),
            title=str(item.get("title") or ""),
            sha=str(commit.get("oid") or "")[:7],
            merged_at=str(item.get("mergedAt") or ""),
        ))
    return tuple(prs), ""


def _open_prs(inputs: "ReportInputs", runner, agent_rows, queue_rows, limit: int) -> tuple:
    """``(tuple[OpenPR], reason)`` - open pull requests with the facts that make a stall visible.

    Reuses the SAME `git_runner` seam `_merged_prs` already uses, for the reason recorded there:
    it takes an argv list and returns `(rc, out, err)`, which is exactly a `gh` call's shape, and
    a second seam is a second thing every test has to stub.

    A FAILURE IS RECORDED, NEVER SWALLOWED. `(None, reason)` means the report says "could not
    look" rather than printing an empty list a reader takes for "nothing is stuck" - the same
    rule `_branch_drift` states in its own docstring.
    """
    refusal = _pr_reader_refusal(inputs)
    if refusal:
        return None, refusal
    active = runner if runner is not None else _default_git_runner
    rc, out, err = active([
        "gh", "pr", "list",
        "--repo", inputs.tracker_host_ref,
        "--state", "open",
        "--limit", str(limit),
        "--json", "number,title,headRefName,createdAt,updatedAt,isDraft,mergeable,"
                  "mergeStateStatus,statusCheckRollup",
    ])
    if rc != 0:
        return None, (err or "gh pr list failed").strip().splitlines()[0][:200]
    try:
        raw = json.loads(out or "[]")
    except ValueError as exc:
        return None, f"gh returned unreadable JSON: {exc}"
    lanes = _branch_lane_map(agent_rows, queue_rows)
    prs = []
    for item in raw:
        branch = str(item.get("headRefName") or "")
        prs.append(OpenPR(
            number=str(item.get("number", "")),
            title=str(item.get("title") or ""),
            branch=branch,
            created_at=str(item.get("createdAt") or ""),
            updated_at=str(item.get("updatedAt") or ""),
            mergeable=str(item.get("mergeable") or ""),
            merge_state=str(item.get("mergeStateStatus") or ""),
            checks=_check_rollup(item.get("statusCheckRollup")),
            draft=bool(item.get("isDraft")),
            lane=lanes.get(_wt_epic_key(branch), ""),
        ))
    if len(raw) >= limit:
        # SATURATION IS REPORTED, NEVER SILENT. gh returns newest-first, so a full page means the
        # rows that fell off are the OLDEST - the stalled ones. A list that looks complete while
        # hiding exactly the population it exists to surface is worse than no list.
        return tuple(prs), (
            f"showing {len(prs)} open PRs and the limit is {limit}: older ones may be missing, "
            "and gh returns newest-first so those are the most likely to be stuck"
        )
    return tuple(prs), ""


#: The ONLY states that count as passing. Everything else fails, INCLUDING a label not listed
#: anywhere here - see `_check_rollup` for why that direction is the load-bearing one.
_CHECK_OK = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

#: Severity order for the states worth ranking, worst first. Anything absent ranks WORSE than all
#: of these, not better.
_CHECK_RANK = {
    s: i for i, s in enumerate((
        "FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "CANCELLED", "ACTION_REQUIRED", "ERROR",
        "STALE", "EXPECTED", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED",
    ))
}


_JOB_ID = re.compile(r"/job/(\d+)/?$")


def _check_identity(c: dict):
    """``(workflowName, name)`` for a CheckRun, ``context`` for a StatusContext, else ``None``."""
    if c.get("context"):
        return ("status", str(c["context"]))
    if c.get("name"):
        return ("run", str(c.get("workflowName") or ""), str(c["name"]))
    return None


def _check_age(c: dict):
    """``(job id or None, startedAt or None)`` - the two keys that can order two runs of a check."""
    m = _JOB_ID.search(str(c.get("detailsUrl") or ""))
    job = int(m.group(1)) if m else None
    started = None
    raw = str(c.get("startedAt") or "")
    if raw:
        try:
            started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            started = None
        # An offset-less stamp would raise on comparison with an aware one and crash the render.
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        # gh renders a not-yet-started run as year 1; that is "no start time", not "oldest".
        if started is not None and started.year <= 1:
            started = None
    return job, started


def _newest_per_check(checks: list) -> list:
    """Keep only the newest entry of each check, preserving order.

    Job ids follow creation order and a queued re-run has its id before it has a `startedAt`, so
    a group is ordered by job id when EVERY entry in it carries one, otherwise by `startedAt` when
    every entry carries that. A group where neither key is on every entry keeps all its entries:
    mixing the two keys is not a consistent order (A newer than B by id, B newer than C by start,
    C newer than A by start drops all three and hides a red), and when the newest cannot be named,
    guessing could pick the green one. An entry with no identity is never collapsed.
    """
    ages = [_check_age(c) for c in checks]
    groups: dict = {}
    for i, c in enumerate(checks):
        key = _check_identity(c)
        if key is not None:
            groups.setdefault(key, []).append(i)
    dropped = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        for slot in (0, 1):
            vals = [ages[i][slot] for i in members]
            if all(v is not None for v in vals):
                top = max(vals)
                dropped.update(i for i, v in zip(members, vals) if v != top)
                break
    return [c for i, c in enumerate(checks) if i not in dropped]


def _check_rollup(rollup) -> str:
    """``"N/M <worst>"`` from gh's per-check list, or ``""`` when there are no checks.

    A PR with NO checks is not a PR whose checks passed, and the empty string says so rather
    than reporting a vacuous 0/0 SUCCESS - the distinction gh#412 already paid for once.

    THE DEFAULT IS FAILURE, NOT SUCCESS, AND THAT INVERSION IS THE WHOLE FIX. An earlier version
    started at `SUCCESS` and lowered it only when one of seven hardcoded labels appeared, so every
    label it had not been told about fell through to the most reassuring answer. GitHub emits more
    than seven: a CheckRun can conclude `STARTUP_FAILURE` or `STALE`, and a legacy StatusContext -
    the very shape the `conclusion or state` fallback exists to read - carries `ERROR` and
    `EXPECTED`. Measured against the previous code, all four rendered `13/14 SUCCESS`: a required
    status that errored, or one that never posted at all, printed beside `mergeable=MERGEABLE` with
    the word SUCCESS next to it. `EXPECTED` is the single most common way a green-looking pull
    request silently cannot merge.

    So membership decides, and the ranking only orders what is already known to be failing. A state
    this code has never seen sorts ahead of every ranked one, because the one thing a reader must
    never be told is that an unrecognised check passed.

    ONLY THE NEWEST ENTRY PER CHECK COUNTS, because that is what GitHub's merge gate reads, and
    gh's rollup lists every run on the head SHA. Counting superseded runs made three PRs that merged
    green read as failing (measured on ba40318, 2026-09-26): #542 `14/28 FAILURE`, #581
    `35/36 FAILURE`, #477 `41/42 FAILURE` - each carried a failed or cancelled older run beside the
    green newer one. `_newest_per_check` does the collapse; it keeps the newest entry, never the
    best one, so a newer red is never hidden behind an older green.
    """
    if not isinstance(rollup, list) or not rollup:
        return ""
    states = []
    for c in _newest_per_check([c for c in rollup if isinstance(c, dict)]):
        states.append(str(c.get("conclusion") or c.get("state") or "").upper() or "PENDING")
    if not states:
        return ""
    passing = [s for s in states if s in _CHECK_OK]
    failing = [s for s in states if s not in _CHECK_OK]
    if not failing:
        return f"{len(passing)}/{len(states)} SUCCESS"
    # RANKED WHERE KNOWN, WORST WHERE NOT. An unrecognised state sorts ahead of every ranked one
    # rather than behind them, so a label this code has never seen is reported instead of hidden.
    worst = min(failing, key=lambda s: (_CHECK_RANK.get(s, -1), s))
    return f"{len(passing)}/{len(states)} {worst}"


def _branch_lane_map(agent_rows, queue_rows) -> dict:
    """``{epic-number: lane}`` for every NON-TERMINAL claim, so a PR can name whose row owns it.

    Keyed on the epic NUMBER rather than the whole string, for the reason gh#456 established:
    a branch is spelled `e0768-account-limit-keystone` and a work cell `E-0768-the-fleet-...`,
    and neither contains the other. Terminal rows are excluded because a shipped epic's number
    must not attach a lane to some later PR that happens to reuse it.
    """
    out = {}
    for row in queue_rows or []:
        if getattr(row, "state", "") in _registry.TERMINAL_QUEUE_STATES:
            continue
        lane = getattr(row, "claimed_by", "-")
        key = _wt_epic_key(getattr(row, "id", ""))
        if key and lane not in ("-", ""):
            out.setdefault(key, lane)
    for row in agent_rows or []:
        if _registry.is_reaped_phase(row.phase):
            continue
        key = _wt_epic_key(row.work)
        if key:
            out.setdefault(key, row.agent)
    return out


def _wt_epic_key(value: str) -> str:
    """The epic NUMBER in ``value``, zero-stripped, or ``""``. Shared by the lane map above."""
    m = _WT_EPIC_NUM.search(value or "")
    return (m.group(1).lstrip("0") or "0") if m else ""


def _done_section(inputs: "ReportInputs", runner, cap: int) -> tuple:
    """The DONE section, and the merged PRs the WORK wiring also needs."""
    prs, reason = _merged_prs(inputs, runner, DONE_WINDOW_LIMIT)
    if prs is None:
        return DoneSection(available=False, reason=reason), ()
    return (
        DoneSection(
            available=True,
            rows=_cap(list(prs), cap),
            source_note=(
                f"the last {len(prs)} merged pull requests in {inputs.tracker_host_ref} - "
                "NOT the same reading as WORK's SHIPPED, which counts only what moved to "
                "done since the previous report"
            ),
            window=f"last {DONE_WINDOW_LIMIT} merged PRs",
        ),
        prs,
    )


def _attach_pr_evidence(rows: list, prs: tuple) -> list:
    """Fill ``ShippedRow.sha``/``.pr`` where a merged PR names the slug, and ONLY there.

    Matched on the slug appearing in the PR title, which is how this fleet titles its PRs.
    A near-match is left blank rather than guessed: a wrong sha beside a slug is worse than
    no sha, because the reader has no way to tell a guess from a lookup.
    """
    if not prs:
        return rows
    out = []
    for row in rows:
        hit = next((p for p in prs if row.slug and row.slug in p.title), None)
        out.append(ShippedRow(slug=row.slug, sha=hit.sha, pr=hit.number) if hit else row)
    return out


#: What answers "does this seat have work" for a seat with NO Queue lane. The lane-shaped
#: predicate below covers ``lead-dev`` / ``jr-dev-1`` / ``jr-dev-2``; the other three seats are
#: fed by the routed-note inbox instead, and that is a real predicate rather than a stand-in -
#: a note addressed to the auditor, the steward or dev-manager IS that seat's work item, which
#: is why every one of them is instructed to drain its inbox at the wake read.
INBOX_BASIS = "routed Notes addressed to it"
LANE_BASIS = "its Queue lane"
#: lead-dev's channel is TWO things: its Queue lane, and the claimable epics and bugs it selects
#: from and publishes into that lane. Named in full so a DRY verdict says both were empty.
SELECTION_BASIS = "its Queue lane or the claimable epics and bugs it selects from"
SELECTING_SEAT = "lead-dev"


def _routed_note_count(reg, agent: str) -> int:
    """Live (unstruck) Notes addressed specifically to *agent*.

    ``-> fleet`` IS EXCLUDED, matching ``registry._notes_routed_to``: `fleet` is a broadcast
    address, not an assignment, so counting it would make all three lane-less seats busy the
    moment anyone broadcasts anything - and a predicate that is always True reports nothing.

    AN UNPARSEABLE STAMP DOES NOT HIDE A NOTE, for the same reason it does not there: routing is
    knowable without a readable timestamp, so this reads ``_NOTE_PREFIX_RE`` directly rather
    than going through ``_parse_note_prefix``, which returns None on a stamp it cannot parse and
    would silently drop the note from the count.
    """
    n = 0
    for content in reg.notes:
        if _registry._is_struck_note(content):
            continue
        m = _registry._NOTE_PREFIX_RE.match(content.strip())
        if m is not None and m.group(3) == agent:
            n += 1
    return n


def _lane_has_work(reg, agent: str, *, selectable: bool = False) -> tuple:
    """``(has_work, basis)`` for EVERY seat. ``has_work`` is never None.

    THIS USED TO RETURN None FOR THREE OF THE SEVEN SEATS, and None short-circuited the only
    unproductivity branch in ``classify_lane``. ``registry.LANE_OF`` maps only the builder
    lanes, so `auditor`, `steward` and `dev-manager` could not be reported as DRY no matter how
    long they produced nothing - rendered LIVE-green by construction, which is the reassuring
    half of the exact stall this report exists to catch. A seat that cannot fail a check is not
    passing it.

    The fix is not to colour them DRY against a lane they do not have - that would assert
    something false, which is why the old None was written. It is to ask each seat the question
    its OWN work arrives through: the builders have a Queue lane, the other three have the
    routed-note inbox. ``basis`` travels with the answer so the report says WHICH predicate
    replied, and `classify_lane` uses it to keep the DRY verdict from prescribing a remedy the
    seat cannot act on.

    WHAT THIS PREDICATE DOES NOT CLAIM. A drained inbox is not idleness: `steward` in
    particular feeds itself from elapsed time, so its False here means "nothing routed", never
    "nothing to do". That distinction lives in the why-string rather than in a second return
    value, because a boolean with two meanings is how the None it replaced went wrong.
    """
    lane = _registry.LANE_OF.get(agent)
    if lane is None:
        return (_routed_note_count(reg, agent) > 0, INBOX_BASIS)
    for row in reg.queue.values():  # reg.queue is keyed by id, not a list
        # A terminal row is provenance, not a claim - the rule the WORK claim loop and
        # `_branch_lane_map` already keep. Without it one shipped row kept its claimant LIVE
        # with an empty why forever, hiding the dry lane this verdict exists to show.
        if row.state.strip() in _registry.TERMINAL_QUEUE_STATES:
            continue
        if row.claimed_by.strip() == agent:
            return (True, LANE_BASIS)
        if row.lane.strip() == lane and row.state.strip() == "ready":
            return (True, LANE_BASIS)
    if agent == SELECTING_SEAT:
        # The selecting seat's lane is its OUTPUT, not its input: it picks from the claimable
        # epics and bugs and publishes its pick into `lead` afterwards. Reading the lane alone
        # called an idle lead-dev with startable work DRY - "needs work" printed over the one
        # stall this report exists to catch, inverted. Juniors are untouched: their lane really
        # is where their work arrives, so an empty one really is dry.
        return (selectable, SELECTION_BASIS)
    return (False, LANE_BASIS)


#: BOTH renderings, because `-` is a word boundary and a PR title may carry either. E-0260
#: gave epics an `E-<NNNN>-<short-name>` id; a pattern anchored on a trailing word boundary
#: extracts only the NUMBER, while `_seat_by_epic_id` keys its map on the FULL row id -- so the
#: two sides could never meet and every merged PR for a new-shape epic fell into
#: `production_unattributed`, lanes reading `merged_in_window: 0` beside
#: `production_available: True`. That is the exact confidently-wrong reading this signal exists
#: to prevent, and BOTH reviewers found it independently. The map is now keyed under the full id
#: AND its bare number, and this pattern is greedy, so a title carrying either form resolves.
_PR_EPIC_KEY_RE = re.compile(r"\bE-\d{3,}(?:-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)?")
_PR_ISSUE_KEY_RE = re.compile(r"\bgh#(\d+)\b", re.IGNORECASE)

#: Distinguishes "no seat recorded yet" from "recorded as ambiguous", which None alone cannot.
_UNSET = object()


def _seat_by_epic_id(reg, log_text: str) -> dict:
    """``E-NNNN -> the seat that held it``, from the live board FIRST and the cull log after.

    A shipped row is CULLED off the board within a wake or two, so a map built from
    ``reg.queue`` alone resolves almost nothing and the signal would report every lane at zero
    — which is exactly the confidently-wrong reading this is meant to prevent. The log is the
    append-only archive of those rows, verbatim and with their ``claimed_by`` cell intact.

    AN ID HELD BY TWO DIFFERENT SEATS IN THE LOG IS DROPPED, NOT RESOLVED BY RECENCY. E- ids
    are derived from priority numbers and this fleet REUSES them for unrelated epics — seven
    ids in the live log are duplicated and five of those changed hands between incarnations.
    "The newest entry is the truest" is only true if an id is one epic republished, and it is
    not, so recency would credit a merged PR naming the older epic to the newer epic's holder,
    silently and retroactively. Dropping the key sends that PR to ``production_unattributed``,
    where a reader can see the attribution missed instead of reading a wrong seat's count.

    A LIVE row still overrides: a current claim is unambiguous by construction.
    """
    seen: dict = {}
    for line in log_text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4 or not _registry._E_ID.match(cells[0]):
            continue
        if cells[3] not in _registry.AGENTS:
            continue
        prior = seen.get(cells[0], _UNSET)
        if prior is _UNSET:
            seen[cells[0]] = cells[3]
        elif prior != cells[3]:
            seen[cells[0]] = None  # id reused by a different seat: it identifies neither
    out = {k: v for k, v in seen.items() if v is not None}
    for rid, row in reg.queue.items():
        holder = row.claimed_by.strip()
        if holder in _registry.AGENTS:
            out[rid] = holder
    # KEY THE MAP UNDER THE BARE NUMBER TOO. A PR title may name an epic either way -- the full
    # `E-0708-move-the-unattended` or just `E-0708` -- and the caller's key pattern now yields
    # whichever the title carried. Indexing only the full id made every new-shape epic
    # unattributable; indexing only the number would lose the name half the same way. The number
    # alias never OVERWRITES a full-id entry and is dropped when two ids share a number and
    # disagree on the seat, which is the same ambiguity rule the loop above applies -- an alias
    # must not resolve something the id it aliases would refuse.
    alias: dict = {}
    for rid, seat in out.items():
        bare = rid.split("-")[0] + "-" + rid.split("-")[1] if rid.count("-") >= 1 else rid
        if bare == rid:
            continue
        if bare in out:
            continue
        prior = alias.get(bare, _UNSET)
        if prior is _UNSET:
            alias[bare] = seat
        elif prior != seat:
            alias[bare] = None
    out.update({k: v for k, v in alias.items() if v is not None})
    return out


#: A row whose SUBJECT is an issue: the work_item opens with the issue it is there to fix
#: (``BUG gh#134 - ...``). This is a structural ownership claim, unlike a mention of gh#134
#: anywhere else in the same paragraph of seam prose.
_ISSUE_SUBJECT_RE = re.compile(r"^\s*(?:BUG\s+)?gh#(\d+)\b", re.IGNORECASE)


def _seat_by_issue_number(reg, log_text: str) -> dict:
    """``"78" -> the seat that held the row FOR gh#78``, UNIQUE claims only.

    A bug PR titles its issue, not its epic id, so this is the second half of the key.

    TWO LAYERS, BECAUSE A MENTION IS NOT A CLAIM. Work-item cells on this fleet carry seam
    prose that routinely names other issues ("measured during the gh#134 review", "READ gh#252
    BEFORE DESIGNING THE GATE"). Read as ownership, those mentions make two seats "claim" one
    number and the uniqueness rule then discards it — measured on the live log, five keys were
    being thrown away by prose alone, so the seat that actually owned the issue lost its
    credit. But the mention scan cannot simply be replaced by the subject match either: on the
    same data that drops ten keys which resolve correctly today.

    So: scan mentions for reach, then let an unambiguous SUBJECT claim overrule whatever the
    mention layer concluded, and let an ambiguous subject claim delete the key outright.

    Ambiguity that survives both layers lands in ``production_unattributed``. That is the whole
    discipline — a guessed attribution is worse than an unattributed count, because a reader
    cannot tell a guess from a lookup.
    """
    def collect(pattern, subject: bool) -> dict:
        seen: dict = {}

        def scan(work_item: str, holder: str) -> None:
            if holder not in _registry.AGENTS:
                return
            nums = pattern.findall(work_item)
            for num in ({nums[0]} if subject and nums else set(nums)):
                prior = seen.get(num, _UNSET)
                if prior is _UNSET:
                    seen[num] = holder
                elif prior != holder:
                    seen[num] = None
        for line in log_text.splitlines():
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 4 or not _registry._E_ID.match(cells[0]):
                continue
            scan(cells[1], cells[3])
        for row in reg.queue.values():
            scan(row.work_item, row.claimed_by.strip())
        return seen

    out = {k: v for k, v in collect(_PR_ISSUE_KEY_RE, False).items() if v is not None}
    for num, holder in collect(_ISSUE_SUBJECT_RE, True).items():
        if holder is None:
            out.pop(num, None)  # two rows are ABOUT this issue: it identifies neither
        else:
            out[num] = holder   # a structural claim outranks any number of mentions
    return out


def _production_unmeasured(section: LanesSection, reason: str, window: str) -> LanesSection:
    """Every row None with the reason attached, and ``production_available`` False.

    One helper for every route to "we could not measure this", so a new failure mode cannot
    quietly grow a path that leaves the counts at zero instead.
    """
    why = f"unmeasured: {reason}"
    return replace(
        section,
        lanes=tuple(replace(ln, merged_in_window=None, production_basis=why)
                    for ln in section.lanes),
        production_available=False,
        production_window=window,
    )


def _attach_production_signal(
    section: LanesSection,
    prs: tuple,
    reg,
    *,
    inputs: "ReportInputs",
    available: bool,
    reason: str,
    window: str,
) -> LanesSection:
    """Fill each lane's ``merged_in_window``, and the section's window and unattributed count.

    Separate pass rather than a branch inside ``_lanes_section`` for the same reason
    ``_attach_seam_signal`` is: the merged-PR read happens later in ``build_report`` (one read,
    two consumers), and recomputing it here would be a second tracker call that can disagree
    with the first.

    **Unavailable is not zero.** When the tracker could not be read, every row keeps
    ``merged_in_window=None`` and carries the reason, because "0 merged" printed beside a lane
    that shipped four things is the precise failure the spec was written against.
    """
    if not available:
        return _production_unmeasured(
            section, reason or "the tracker could not be read", window
        )

    log_text = ""
    if inputs.registry_path is not None:
        log_path = Path(inputs.registry_path).parent / "registry-log.md"
        try:
            # errors="replace", not strict: one bad byte anywhere in a 900KB append-only
            # archive must not take the whole report down, and a mangled character in a
            # culled work_item cannot change an id or a claimed_by cell.
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            # GENUINELY ABSENT, which is the fresh-install shape. It only narrows the map.
            log_text = ""
        except OSError as exc:
            # ANY OTHER FAILURE IS NOT ABSENCE, AND ON A MATURE FLEET THE LOG *IS* THE MAP -
            # shipped rows are culled within a wake or two, so a denial here collapses every
            # lane to zero. Reporting those zeros beside `production_available: True` would be
            # "0 merged" printed next to a lane that shipped four things, which is the precise
            # failure this signal was written against. Refuse to measure instead.
            return _production_unmeasured(
                section, f"{log_path.name} unreadable ({exc.__class__.__name__})", window
            )

    by_epic = _seat_by_epic_id(reg, log_text) if reg is not None else {}
    by_issue = _seat_by_issue_number(reg, log_text) if reg is not None else {}
    counts: dict = {}
    unattributed = 0
    for pr in prs:
        seat = None
        for key in _PR_EPIC_KEY_RE.findall(pr.title):
            seat = by_epic.get(key)
            if seat:
                break
        if seat is None:
            for num in _PR_ISSUE_KEY_RE.findall(pr.title):
                seat = by_issue.get(num)
                if seat:
                    break
        if seat is None:
            unattributed += 1
        else:
            counts[seat] = counts.get(seat, 0) + 1

    basis = f"merged PRs in {window}, attributed by the E- id or gh# number in the PR title"
    return replace(
        section,
        lanes=tuple(_credit(ln, counts.get(ln.agent, 0), basis) for ln in section.lanes),
        production_available=True,
        production_window=window,
        production_unattributed=unattributed,
    )


#: What a lane-less seat produces instead of pull requests. Named per seat, because "unmeasured"
#: with no referent is the same unhelpful shrug as a bare zero.
_NON_PR_OUTPUT = {
    "auditor": "its audit ledger",
    "steward": "its sweeps",
    "dev-manager": "its digest and promotions",
}


def _credit(lane: LaneRow, n: int, basis: str) -> LaneRow:
    """One lane's production cell. A ZERO IS A VERDICT AND MUST ONLY BE WRITTEN WHERE IT IS TRUE.

    For a builder, 0 merged PRs in the window means it shipped nothing, which is exactly the
    fact this signal exists to surface. For `auditor` / `steward` / `dev-manager` it means no
    such thing: those seats do not ship pull requests at all, so a 0 beside them asserts
    unproductivity that was never measured — the mirror image of the unmeasured-seat-rendered-
    healthy failure this epic is about, and just as confidently wrong.

    So a lane-less seat with nothing attributed reports None plus what its output actually is.
    A lane-less seat that DID land a PR still shows the count: that is an observed fact, and
    hiding it behind "unmeasured" would lose it.
    """
    if n == 0 and lane.agent in _NON_PR_OUTPUT:
        return replace(lane, merged_in_window=None, production_basis=(
            f"unmeasured: this seat does not ship pull requests - its output is "
            f"{_NON_PR_OUTPUT[lane.agent]}"
        ))
    return replace(lane, merged_in_window=n, production_basis=basis)


def _awaiting_a_decision(reg, agent: str) -> str:
    """The ids of open Questions this seat RAISED and open Blockers it OWNS, or "".

    Raised-vs-owned is the asymmetry that matters: a question is answered FOR its
    raiser, and a blocker is resolved BY its owner. Reading either the other way round
    would mark the wrong seat as waiting.
    """
    ids = [
        qid for qid, q in sorted(reg.questions.items())
        if q.fields.get("state", "").strip() == "open"
        and q.fields.get("raised_by", "").strip() == agent
    ]
    # A lane HALTED on a dead referent (retracted, answered, missing) is not read here: the
    # halted branch of `classify_lane` returns before `awaiting` is ever consulted, so nothing
    # added to this list can reach such a lane. `_dead_referent_halts` carries that case to the
    # HALTED why-string and to BLOCKED ON YOU.
    ids += [
        bid for bid, b in sorted(reg.blockers.items())
        if b.state.strip() == "open" and b.owner.strip() == agent
    ]
    return ", ".join(ids)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _lead_selection(work: WorkSection, intake: IntakeSection) -> tuple:
    """``(has_work, sentence)`` for the selecting seat, from what WORK says it could start.

    STARTABLE MEANS A FREE LANE CAN START IT NOW: claimable epics and bugs minus the ones the
    seam signal says overlap in-flight work. Counted over the rows WORK carries, so on a capped
    board a seam-blocked item beyond the cap stays counted - the direction that under-reports
    DRY, which is the safe one for a verdict that says "needs work".

    NOT DRY on ambiguity either. An approved epic whose claim cannot be resolved may be free,
    and "needs work" printed over it would prescribe writing work that may already exist.

    The sentence is the LIVE why when ``has_work``, and the DRY qualifier otherwise.
    """
    blocked = {b.name for b in work.seam_blocked.items}
    # A row published into a junior's lane is already out of both counts read here (WORK holds
    # it in `published_unclaimed`), so lead-dev is never credited with work it cannot select.
    epics = work.claimable_now.total - sum(
        1 for r in work.claimable_now.items if r.slug in blocked
    )
    bugs_unread = intake.reason if intake.tracker_configured and not intake.available else ""
    bugs = 0 if bugs_unread else work.bug_claimable.total - sum(
        1 for i in work.bug_claimable.items if _issue_name(i) in blocked
    )
    seam = sum(1 for r in work.claimable_now.items if r.slug in blocked) + sum(
        1 for i in work.bug_claimable.items if _issue_name(i) in blocked
    )
    ambiguous = work.claim_ambiguous.total
    bug_side = (
        f"bugs not read: {bugs_unread}" if bugs_unread else f"{_plural(bugs, 'bug')} claimable"
    )
    extras = []
    if seam:
        extras.append(f"{_plural(seam, 'claimable item')} seam-blocked behind in-flight work")
    if ambiguous:
        slugs = ", ".join(r.slug for r in work.claim_ambiguous)
        verb = "whose claim state is" if ambiguous == 1 else "whose claim states are"
        extras.append(f"{_plural(ambiguous, 'approved epic')} {verb} ambiguous: {slugs}")
    startable = epics + bugs
    if startable:
        text = (
            f"{_plural(startable, 'startable item')} ({_plural(epics, 'epic')} claimable now, "
            f"{bug_side}) and nothing yet in its Queue lane; it picks from these, so this is "
            "not dry"
        )
        return True, text + "".join(f"; also {e}" for e in extras)
    if ambiguous:
        return True, (
            f"nothing it can provably start, but {'; '.join(extras)} - not dry until the claim "
            "is resolved"
        )
    notes = ([f"the bug side was not read: {bugs_unread}"] if bugs_unread else []) + extras
    return False, "; ".join(notes)


def _lanes_section(
    reg,
    *,
    now: datetime,
    stale_hours: float,
    selection: tuple = (False, ""),
) -> LanesSection:
    """5. LANES — one row per loop, classified, with why it is not working.

    ``selection`` is ``_lead_selection``'s answer: whether the selecting seat has anything to
    pick from, and the sentence that says what.
    """
    if reg is None:
        return LanesSection()
    silent = frozenset(
        s.agent for s in _registry.stale_rows(reg, now, threshold_hours=stale_hours)
    )
    selectable, sentence = selection
    dead = _dead_referent_halts(reg, now)
    # DRY and BLOCKED are DERIVED here, once, rather than in each renderer - the whole
    # reason the derivation lives in the collector is that two surfaces recomputing it
    # is two surfaces that can disagree.
    lanes = []
    for agent, row in sorted(reg.agents.items()):
        has_work, basis = _lane_has_work(reg, agent, selectable=selectable)
        lanes.append(classify_lane(
            row,
            now=now,
            silent_agents=silent,
            lane_has_work=has_work,
            work_basis=basis,
            awaiting=_awaiting_a_decision(reg, agent),
            selection=sentence if basis == SELECTION_BASIS else "",
            dead_referent=dead[agent].reason if agent in dead else "",
        ))
    lanes = tuple(lanes)
    counts = {LANE_LIVE: 0, LANE_HALTED: 0, LANE_REAPED: 0, LANE_SILENT: 0,
              LANE_DRY: 0, LANE_BLOCKED: 0, LANE_STOOD_DOWN: 0}
    for lane in lanes:
        counts[lane.state] = counts.get(lane.state, 0) + 1
    return LanesSection(
        lanes=lanes,
        live_count=counts[LANE_LIVE],
        halted_count=counts[LANE_HALTED],
        reaped_count=counts[LANE_REAPED],
        silent_count=counts[LANE_SILENT],
        dry_count=counts[LANE_DRY],
        blocked_count=counts[LANE_BLOCKED],
        stood_down_count=counts[LANE_STOOD_DOWN],
    )


def _health_section(
    reg,
    records,
    *,
    inputs: ReportInputs,
    now: datetime,
    cap: int,
    registry_bytes: int,
    git_runner: Optional[GitRunner],
    scan: ScanCount,
) -> HealthSection:
    """6. HEALTH — every count carrying the names behind it."""
    inert = _by_priority_then_slug(
        inert_promotions(
            records, project=inputs.project, excluded_domains=inputs.excluded_domains
        )
    )
    oov = out_of_vocabulary_states(records)

    deploy = DeployLockRow()
    directives: list[DirectiveRow] = []
    notes_live = notes_struck = 0
    halt_events: tuple = ()
    reap_events: tuple = ()
    agent_rows = []
    if reg is not None:
        agent_rows = list(reg.agents.values())
        held = reg.deploy_holder not in ("none", "", "-")
        age = _age_hours(reg.deploy_taken_at, now) if held else None
        deploy = DeployLockRow(
            holder=reg.deploy_holder,
            taken_at=reg.deploy_taken_at,
            age_hours=age,
            held=held,
            stale=bool(held and age is not None and age * 3600.0 > _registry.DEPLOY_LOCK_STALE),
        )
        directives = [
            DirectiveRow(name=directive_name(d), text=d)
            for d in _registry.active_directives(reg)
        ]
        for note in reg.notes:
            if _registry._is_struck_note(note):
                notes_struck += 1
            else:
                notes_live += 1
        halt_events = tuple(
            sorted(r.agent for r in agent_rows if r.status == "halted")
        )
        reap_events = tuple(
            sorted(r.agent for r in agent_rows if _registry.is_reaped_phase(r.phase))
        )

    open_prs_tuple, open_prs_note = _open_prs(
        inputs, git_runner, agent_rows,
        getattr(reg, "queue", {}).values() if reg is not None else (),
        OPEN_PR_LIMIT,
    )
    open_prs = open_prs_tuple or ()
    return HealthSection(
        inert_promotions=_cap(
            [EpicRow.of(r, reason=inert_reason(r, inputs.excluded_domains)) for r in inert], cap
        ),
        inert_count=len(inert),
        out_of_vocabulary=_cap(oov, cap),
        out_of_vocabulary_count=len(oov),
        deploy_lock=deploy,
        directives=_cap(directives, cap),
        directive_count=len(directives),
        registry_bytes=registry_bytes,
        notes_live=notes_live,
        notes_struck=notes_struck,
        halt_events=halt_events,
        reap_events=reap_events,
        worktrees=_worktree_drift(inputs, agent_rows, git_runner),
        open_prs=open_prs,
        open_prs_note=open_prs_note,
        branches=_branch_drift(inputs, git_runner),
        scan=scan,
    )


# --------------------------------------------------------------------------- #
# Read-only proof helper
# --------------------------------------------------------------------------- #
def fingerprint_tree(root) -> dict:
    """relpath -> (size, sha256) for every file under ``root``.

    Exists so "the report never acts" is a test rather than a promise: the suite fingerprints
    the data root, collects, and fingerprints again. Any write — a created archive directory,
    a touched status file, a cached index — changes the fingerprint and fails.
    """
    base = Path(root)
    out: dict = {}
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        data = p.read_bytes()
        out[str(p.relative_to(base)).replace("\\", "/")] = (
            len(data),
            hashlib.sha256(data).hexdigest(),
        )
    return out


__all__ = [
    "ARCHIVE_STAMP_FORMAT",
    "ARCHIVE_SUFFIX",
    "ArchiveInfo",
    "BlockedSection",
    "BlockerRow",
    "BranchDrift",
    "ChangedSince",
    "CountedSkippedSection",
    "DEFAULT_ARCHIVE_SUBDIR",
    "DEFAULT_ROWS_PER_SECTION",
    "DRAFTED_CURRENT_STATE",
    "DecidedRow",
    "DecidedSection",
    "DeployLockRow",
    "DirectiveRow",
    "EpicMove",
    "EpicRow",
    "FleetReport",
    "HEALTH_THRESHOLDS",
    "HUMAN_BLOCKER_OWNERS",
    "HealthCrossing",
    "HealthSection",
    "IntakeSection",
    "KNOWN_CURRENT_STATES",
    "LANE_HALTED",
    "LANE_LIVE",
    "LANE_REAPED",
    "LANE_STOOD_DOWN",
    "LANE_SILENT",
    "LaneRow",
    "LaneTransition",
    "LanesSection",
    "QuestionRow",
    "REAPED_FRESH_HOURS",
    "ReportInputs",
    "Rows",
    "SECTION_ORDER",
    "SNAPSHOT_FENCE_TAG",
    "SNAPSHOT_VERSION",
    "ScanCount",
    "ShippedRow",
    "SkippedFile",
    "WorkSection",
    "WorktreeDrift",
    "approved_epics",
    "archive_directory",
    "archive_filename",
    "capture_survey_skips",
    "classify_lane",
    "collect",
    "diff_since",
    "directive_name",
    "find_latest_archive",
    "fingerprint_tree",
    "format_age",
    "inert_promotions",
    "inert_reason",
    "inputs_from_config",
    "load_prior_snapshot",
    "normalise_reason",
    "out_of_vocabulary_states",
    "parse_archived_snapshot",
    "redaction_prefixes",
    "survey_with_skips",
]
