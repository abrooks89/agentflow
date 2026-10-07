"""Status board read/write — atomic I/O, frontmatter serialization, auto body render."""

from __future__ import annotations

import logging
import os
from agentflow import atomic_io
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import yaml

from agentflow.plan_parser import (
    _SLUG_FILENAME_RE,
    BoardReconcile,
    SpecFormatError,
    orphan_escalation_text,
    parse_spec_frontmatter,
)

logger = logging.getLogger(__name__)

StatusEnum = Literal[
    "running", "escalation", "blocked", "done", "failed", "spec_quality_review", "abandoned",
    "preflight_audit_blocked", "awaiting_merge_decision", "done_on_hold",
    "awaiting_external_resolution",
]
TaskStateEnum = Literal[
    "pending", "ready", "in_flight", "spec_review", "code_review", "done", "blocked"
]
VerifierVerdict = Literal["clean", "warnings", "blocker_persistent"]


@dataclass
class Finding:
    """A single verification finding with severity and location."""
    severity: Literal["low", "medium", "blocker"]
    description: str
    location: Optional[str] = None


@dataclass
class VerifierSnapshot:
    """A snapshot of verifier state at a specific iteration."""
    at: str  # ISO timestamp
    iteration: int
    verdict: VerifierVerdict
    findings_count: dict[str, int] = field(default_factory=dict)


@dataclass
class TaskState:
    state: TaskStateEnum
    phase: Optional[str] = None
    commit: Optional[str] = None
    completed: Optional[str] = None
    role: Optional[str] = None
    since: Optional[str] = None
    model: Optional[str] = None
    verifier_iterations: int = 0
    verifier_verdict: Optional[VerifierVerdict] = None
    verifier_findings_count: dict[str, int] = field(default_factory=dict)
    verifier_findings: list[Finding] = field(default_factory=list)
    verifier_history: list[VerifierSnapshot] = field(default_factory=list)
    # Number of times this task has timed out while in_flight (T8)
    timeout_count: int = 0
    # Per-wave worktree fields (per-wave-worktrees epic). Set by Step 7.0 dispatch
    # when plan.per_wave_worktrees is True; cleared after successful merge_back.
    wave_worktree_path: Optional[str] = None
    wave_branch: Optional[str] = None


@dataclass
class Escalation:
    id: str
    task: int
    asked: str
    question: str
    answer: Optional[str] = None


@dataclass
class Decision:
    at: str
    task: int
    tier: int
    summary: str
    evidence_path: Optional[str] = None


@dataclass
class TestStatus:
    last_run: Optional[str] = None
    result: str = "unknown"
    regression_count: int = 0


@dataclass
class RestartHistoryEntry:
    """Entry in restart_history tracking auto-restart attempts."""
    at: datetime
    reason: str
    spawned_by: str
    pid: int


@dataclass
class StatusState:
    epic: str
    plan: str
    started: str
    status: StatusEnum
    current_phase: str
    paused: bool = False
    last_wake: Optional[str] = None
    next_wake: Optional[str] = None
    last_green_commit: Optional[str] = None
    project: Optional[str] = None
    tasks: dict[int, TaskState] = field(default_factory=dict)
    escalations: list[Escalation] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    test_status: TestStatus = field(default_factory=TestStatus)
    # Session-level tracking fields (added T2)
    ended: Optional[str] = None
    abandoned_reason: Optional[str] = None
    total_waves: int = 0
    total_wakes: int = 0
    test_status_history: list[TestStatus] = field(default_factory=list)
    notifications_sent: dict[str, str] = field(default_factory=dict)
    # Pre-flight audit + merge gate + blocker watcher fields (added T4)
    preflight_audit_status: Optional[str] = None  # None | "clean" | "findings_open" | "acknowledged"
    merge_decision: Optional[str] = None           # None | "merge" | "hold" | "abandon"
    blocker_watcher: Optional[dict] = None         # free-form dict: kind/condition/reason/interval/max_wait/poll_count/last_polled_at/next_wake
    # Liveness hardening fields (added T2)
    halt_reason: Optional[str] = None
    liveness_threshold_minutes: Optional[int] = None
    restart_cap_24h: Optional[int] = None
    restart_history: list[RestartHistoryEntry] = field(default_factory=list)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_AUTO_BLOCK_RE = re.compile(
    r"<!-- AUTO:start.*?-->.*?<!-- AUTO:end -->", re.DOTALL
)


def strip_bom(text: str) -> str:
    """*text* without a leading decoded UTF-8 BOM (U+FEFF), unchanged otherwise.

    THE ONE PLACE STATUS TEXT LOSES ITS BOM (gh#336). A Windows `Out-File` writes one by default,
    `utf-8` decodes it into a U+FEFF ahead of the `---`, and the anchored `_FRONTMATTER_RE` then
    misses. The survey's signature gate (`epic_survey.is_orchestrator_status`) stripped it on its
    own while this parser did not, so a BOM'd board was named as malformed and its epic stayed
    non-terminal: two frontmatter readers each carrying their own BOM handling is how one of them
    was missed. Every frontmatter read in this module goes through it - the parser, the
    read-modify-write in `write_status`, the plan read in `_lazy_backfill_project`, `_read_doc`,
    and `mark_done`'s existing-record read - because making `read_status` succeed on a BOM'd
    board is what sends that board into the writers: a writer that still missed the BOM kept the
    old frontmatter as body text and wrote a board with two `epic:` blocks.
    """
    return text.lstrip("\ufeff")


def parse_status_text(text: str) -> StatusState:
    """Parse status.md content from a string. Used by the aggregator for peer-fetched markdown."""
    match = _FRONTMATTER_RE.match(strip_bom(text))
    if not match:
        raise ValueError("No YAML frontmatter in status text")
    data = yaml.safe_load(match.group(1))

    tasks = {}
    for tid, td in (data.get("tasks") or {}).items():
        # Wrap verifier_findings list into Finding dataclass instances
        if "verifier_findings" in td and td["verifier_findings"]:
            td["verifier_findings"] = [
                Finding(**f) if isinstance(f, dict) else f
                for f in td["verifier_findings"]
            ]
        # Wrap verifier_history list into VerifierSnapshot dataclass instances
        if "verifier_history" in td and td["verifier_history"]:
            td["verifier_history"] = [
                VerifierSnapshot(**v) if isinstance(v, dict) else v
                for v in td["verifier_history"]
            ]
        tasks[int(tid)] = TaskState(**td)

    escalations = [Escalation(**e) for e in data.get("escalations") or []]
    decisions = [Decision(**d) for d in data.get("decisions") or []]
    test_status = TestStatus(**(data.get("test_status") or {}))

    # Wrap test_status_history list into TestStatus dataclass instances
    raw_history = data.get("test_status_history") or []
    test_status_history = [
        TestStatus(**h) if isinstance(h, dict) else h
        for h in raw_history
    ]

    return StatusState(
        epic=data["epic"],
        plan=data["plan"],
        started=data["started"],
        last_wake=data.get("last_wake"),
        next_wake=data.get("next_wake"),
        paused=data.get("paused", False),
        status=data["status"],
        current_phase=data["current_phase"],
        last_green_commit=data.get("last_green_commit"),
        project=data.get("project"),
        tasks=tasks,
        escalations=escalations,
        decisions=decisions,
        test_status=test_status,
        ended=data.get("ended"),
        abandoned_reason=data.get("abandoned_reason"),
        total_waves=data.get("total_waves", 0),
        total_wakes=data.get("total_wakes", 0),
        test_status_history=test_status_history,
        notifications_sent=data.get("notifications_sent") or {},
        preflight_audit_status=data.get("preflight_audit_status"),
        merge_decision=data.get("merge_decision"),
        blocker_watcher=data.get("blocker_watcher"),
        halt_reason=data.get("halt_reason"),
        liveness_threshold_minutes=data.get("liveness_threshold_minutes"),
        restart_cap_24h=data.get("restart_cap_24h"),
        restart_history=[
            RestartHistoryEntry(**e) if isinstance(e, dict) else e
            for e in (data.get("restart_history") or [])
        ],
    )


def read_status(path: Path) -> StatusState:
    return parse_status_text(path.read_text(encoding="utf-8"))


def extract_user_notes(text: str) -> str:
    """Return the user-notes block — content after the AUTO:end marker.
    Preserved across wakes; safe to render as markdown."""
    end = "<!-- AUTO:end -->"
    idx = text.find(end)
    if idx == -1:
        return ""
    return text[idx + len(end):].strip()


def render_body(state: StatusState) -> str:
    lines: list[str] = []
    lines.append(f"# Epic Status: {state.epic}")
    lines.append("")
    lines.append("## Progress")
    by_phase: dict[str, list[tuple[int, TaskState]]] = {}
    for tid, ts in sorted(state.tasks.items()):
        phase = ts.phase or state.current_phase or "unphased"
        by_phase.setdefault(phase, []).append((tid, ts))
    for phase, items in by_phase.items():
        done_n = sum(1 for _, t in items if t.state == "done")
        total_n = len(items)
        marker = "[x]" if done_n == total_n else "[ ]"
        lines.append(f"- {marker} **{phase}** — {done_n}/{total_n} done")
    lines.append("")

    in_flight = [(tid, t) for tid, t in state.tasks.items() if t.state == "in_flight"]
    if in_flight:
        lines.append("## Current activity")
        for tid, t in in_flight:
            lines.append(
                f"- Task {tid}: {t.role} subagent dispatched {t.since} (model={t.model})"
            )
        lines.append("")

    if state.escalations:
        open_esc = [e for e in state.escalations if e.answer is None]
        if open_esc:
            lines.append("## Escalations awaiting you")
            for e in open_esc:
                lines.append(f"- **{e.id}** (task {e.task}, {e.asked}): {e.question}")
                lines.append(
                    "  - Unblock: edit escalations[].answer in frontmatter, or reply when next wake fires."
                )
            lines.append("")

    lines.append("## Test status")
    lines.append(
        f"Last run {state.test_status.last_run} — {state.test_status.result}. "
        f"Regressions since epic started: {state.test_status.regression_count}."
    )
    lines.append("")

    if state.decisions:
        lines.append("## Decisions log (autonomous)")
        for d in state.decisions[-20:]:
            lines.append(f"- {d.at} (task {d.task}, tier {d.tier}) — {d.summary}")
        lines.append("")

    if state.next_wake:
        lines.append(f"## Next wake: {state.next_wake}")
    elif state.status in ("done", "blocked", "failed"):
        lines.append(f"## No next wake (status: {state.status})")

    return "\n".join(lines)


def _lazy_backfill_project(state: StatusState) -> None:
    """Best-effort: if state.project is None, try to read it from state.plan's frontmatter.

    Failures (missing file, no frontmatter, no project, parse error) are silent —
    the field stays None and write_status proceeds normally. This auto-heals
    legacy status files (written before the project field existed) without a
    one-shot migration script.
    """
    if state.project is not None:
        return
    plan_path = Path(state.plan)
    if not plan_path.exists():
        return
    try:
        text = strip_bom(plan_path.read_text(encoding="utf-8"))
        match = _FRONTMATTER_RE.match(text)
        if not match:
            return
        data = yaml.safe_load(match.group(1)) or {}
        project = data.get("project")
        if project:
            state.project = str(project)
    except (OSError, yaml.YAMLError):
        return


def _auto_block(state: StatusState) -> str:
    """The AUTO block for *state*, markers included - the one shape every writer emits."""
    return (
        "<!-- AUTO:start - regenerated on every wake. Edit frontmatter above. -->\n\n"
        f"{render_body(state)}\n\n"
        "<!-- AUTO:end -->"
    )


def _replace_auto_block(body: str, state: StatusState) -> str:
    """*body* with its first AUTO block re-rendered from *state*; every byte outside it kept."""
    new_auto = _auto_block(state)
    # Use a lambda so re.sub doesn't interpret backslashes in `new_auto` as
    # escape sequences. Otherwise Windows paths in the rendered body (e.g.
    # `C:\Users\...\stabilization-tests\...`) explode at sub time on the
    # `\U` / `\x` byte sequences. The lambda returns the literal string.
    return _AUTO_BLOCK_RE.sub(lambda _m: new_auto, body, count=1)


def write_status(path: Path, state: StatusState) -> None:
    """Atomically write status.md: serialize frontmatter, regenerate AUTO body, preserve user notes."""

    _lazy_backfill_project(state)

    # BOM stripped BEFORE the anchored sub (gh#336): otherwise the old frontmatter survives as
    # body text and the rewritten board carries two `epic:` blocks.
    existing = strip_bom(path.read_text(encoding="utf-8")) if path.exists() else ""
    body_after_frontmatter = _FRONTMATTER_RE.sub("", existing, count=1)

    if _AUTO_BLOCK_RE.search(body_after_frontmatter):
        new_body = _replace_auto_block(body_after_frontmatter, state)
    else:
        new_body = _auto_block(state) + "\n\n" + body_after_frontmatter

    fm = _state_to_frontmatter_dict(state)
    fm_yaml = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)

    full = f"---\n{fm_yaml}---\n\n{new_body.lstrip()}"

    # ONE atomic write for the package (gh#57). This path had the same WinError 5 exposure as
    # the registry's and MORE of it: `status.md` is read by `epic_survey` while it walks every
    # board in the fleet, and unlike the registry there is no edit-lock serialising writers
    # here at all. It also cleaned up on `Exception` rather than `BaseException`, so an
    # interrupt between mkstemp and replace stranded a temp beside the board.
    atomic_io.atomic_write(path, full, newline=None)


def pulse(status_path: Path) -> None:
    """Atomically update only the last_wake field in status.md.

    Reads the current state, updates last_wake to now, and writes back
    using atomic rename semantics. Does not modify any other fields.

    Args:
        status_path: Path to the status.md file to pulse.
    """
    status_path = Path(status_path)
    state = read_status(status_path)
    state.last_wake = datetime.now(tz=timezone.utc).isoformat()
    write_status(status_path, state)


def _state_to_frontmatter_dict(state: StatusState) -> dict:
    d: dict = {
        "epic": state.epic,
        "plan": state.plan,
        "started": state.started,
        "last_wake": state.last_wake,
        "next_wake": state.next_wake,
        "paused": state.paused,
        "status": state.status,
        "current_phase": state.current_phase,
        "last_green_commit": state.last_green_commit,
        "project": state.project,
        "tasks": {
            tid: _drop_none(asdict(ts)) for tid, ts in sorted(state.tasks.items())
        },
        "escalations": [asdict(e) for e in state.escalations],
        "decisions": [asdict(d) for d in state.decisions],
        "test_status": asdict(state.test_status),
        # Session-level tracking fields — always emitted (None filtered later)
        "ended": state.ended,
        "abandoned_reason": state.abandoned_reason,
        # Counters: always emit as integers (0 is valid, not null)
        "total_waves": state.total_waves,
        "total_wakes": state.total_wakes,
        # Empty containers are valid — emit as-is ([] / {})
        "test_status_history": [asdict(h) for h in state.test_status_history],
        "notifications_sent": state.notifications_sent,
        # T4: new optional fields (omitted when None so legacy readers don't see them)
        "preflight_audit_status": state.preflight_audit_status,
        "merge_decision": state.merge_decision,
        "blocker_watcher": state.blocker_watcher,
        # T2 liveness fields — simple optionals emitted as null when None; list always emitted
        "halt_reason": state.halt_reason,
        "liveness_threshold_minutes": state.liveness_threshold_minutes,
        "restart_cap_24h": state.restart_cap_24h,
        "restart_history": [asdict(e) for e in state.restart_history],
    }
    # Only drop top-level None values; do NOT drop 0, [], or {}
    return {k: v for k, v in d.items() if v is not None}


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def append_decision(
    state: StatusState,
    task: int,
    tier: int,
    summary: str,
    at: Optional[str] = None,
    evidence_path: Optional[str] = None,
) -> None:
    # Tier-2+ decisions carry evidence_path (the SKILL's evidence_path convention).
    state.decisions.append(
        Decision(
            at=at or datetime.now(tz=timezone.utc).isoformat(),
            task=task,
            tier=tier,
            summary=summary,
            evidence_path=evidence_path,
        )
    )


def open_escalation(
    state: StatusState, task: int, question: str, eid: Optional[str] = None
) -> Escalation:
    """Open an escalation record and halt the epic on it.

    Side effects:
      - Appends an Escalation to state.escalations (id defaults to e<N+1>).
      - Appends a session_escalation_opened: <id> task <N>: <question> event
        (decision-log), per the orchestrator's event-emission contract.
      - Sets state.status = "escalation", so no caller needs a status assignment
        BEFORE the call. A caller that wants a different terminal status (the
        bootstrap path's "preflight_audit_blocked") assigns it AFTER.

    task is 0 for escalations not tied to a specific task (bootstrap failures,
    deadlock); otherwise it is the id of the task that raised the question.

    The event summary collapses whitespace in the question. Two documented
    callers pass a whole multi-line ESCALATION block, and the Decisions log
    renders one line per entry ("- <at> (task N, tier T) — <summary>"), so raw
    newlines spill into the body and a "- " line inside the block becomes a
    sibling bullet. The escalation record itself keeps the question verbatim —
    that is what the unblock path shows the operator — so nothing is lost.

    Emission is ordered exactly as in answer_escalation: the record lands in
    state first, the event second, the status transition last. For any string
    question the summary build and the event append are pure in-memory work
    (no I/O, no parsing), so this adds no new way for the escalation path to
    raise — which matters here, because this is the code that runs when the run
    has already hit trouble.

    Does NOT call write_status — the caller is responsible for persisting.
    """
    next_id = eid or f"e{len(state.escalations) + 1}"
    esc = Escalation(
        id=next_id,
        task=task,
        asked=datetime.now(tz=timezone.utc).isoformat(),
        question=question,
        answer=None,
    )
    state.escalations.append(esc)
    one_line = " ".join(question.split())
    append_session_event(state, "escalation_opened", f"{next_id} task {task}: {one_line}")
    state.status = "escalation"
    return esc


def all_escalations_answered(state: StatusState) -> bool:
    return all(e.answer is not None for e in state.escalations)


def answer_escalation(
    state: StatusState, eid: str, answer: str
) -> Escalation:
    """Record a user's answer to an open escalation.

    Side effects:
      - Sets state.escalations[<eid>].answer = answer.
      - Appends a session_escalation_answered: <eid> answered event (decision-log).
      - If state.status == "escalation" AND all escalations are now answered,
        transitions state.status to "running" (no decision-log reason; the
        per-escalation event already carries the why).

    Raises KeyError if no escalation with the given id exists.

    Re-answering an already-answered escalation overwrites the prior answer
    and emits a fresh session_escalation_answered event. This is intentional:
    users may revise. Callers that want strict idempotency should check
    e.answer is None before calling.

    Does NOT call write_status — the caller is responsible for persisting.
    """
    esc = next((e for e in state.escalations if e.id == eid), None)
    if esc is None:
        raise KeyError(f"no escalation with id={eid!r}")
    esc.answer = answer
    append_session_event(state, "escalation_answered", f"{eid} answered")
    if state.status == "escalation" and all_escalations_answered(state):
        state.status = "running"
    return esc


ORPHAN_RESOLVED_ANSWER = "resolved: board matches plan"


def sync_orphan_escalations(state: StatusState, board: BoardReconcile) -> bool:
    """Make the open orphan escalations match `board.orphaned_open`. True when anything changed.

    - Each orphaned-open task has exactly one open escalation carrying
      `orphan_escalation_text(tid)`: one is opened only when none is open already, so a wake
      that finds the same orphan again does not stack a duplicate.
    - An open orphan escalation whose task is no longer orphaned (the operator fixed the board
      or the plan) is answered through `answer_escalation`, which returns the epic to
      `running` once nothing else is open. Left open, it would hold the epic on a question
      nobody needs to answer.
    - Opening one keeps a status other than `running`: `open_escalation` sets `escalation`,
      and, as its contract says for the bootstrap path, a caller that wants a different
      status assigns it after the call. A `blocked` or held epic stays that way.

    Does NOT call write_status — the caller persists.
    """
    changed = False
    for esc in list(state.escalations):
        if (
            esc.answer is None
            and esc.question == orphan_escalation_text(esc.task)
            and esc.task not in board.orphaned_open
        ):
            answer_escalation(state, esc.id, ORPHAN_RESOLVED_ANSWER)
            changed = True
    for tid in board.orphaned_open:
        text = orphan_escalation_text(tid)
        if any(e.answer is None and e.question == text for e in state.escalations):
            continue
        prior = state.status
        open_escalation(state, task=tid, question=text)
        if prior != "running":
            state.status = prior
        changed = True
    return changed


def transition_status(
    state: StatusState, new_status: StatusEnum, reason: Optional[str] = None
) -> None:
    state.status = new_status
    if reason:
        append_decision(
            state, task=0, tier=2, summary=f"status→{new_status}: {reason}"
        )


def coerce_wave_outcome(
    outcome: str,
    findings_count: dict[str, int],
    verdict: Optional[VerifierVerdict],
) -> tuple[str, Optional[VerifierVerdict], str]:
    """Defense-in-depth: if a wave returns outcome=done but its own
    findings_count carries blocker-severity items, override to
    blocker_persistent so the existing Step 7 escalation path runs.
    Returns (effective_outcome, effective_verdict, reason).
    """
    if outcome == "done" and (findings_count or {}).get("blocker", 0) > 0:
        n = findings_count["blocker"]
        return (
            "verifier_blocker_persistent",
            "blocker_persistent",
            f"wave returned done with {n} blocker-severity finding(s); "
            "overriding to blocker_persistent",
        )
    return (outcome, verdict, "")


def append_session_event(state: StatusState, kind: str, detail: str) -> None:
    """Append a Decision recording an epic-level session event.

    Writes a Decision with:
      - summary = f"session_{kind}: {detail}"
      - tier = 1
      - task = 0 (epic-level, not tied to a specific task)

    Does NOT call write_status — the caller is responsible for persisting the state.
    """
    state.decisions.append(
        Decision(
            at=datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            task=0,
            tier=1,
            summary=f"session_{kind}: {detail}",
        )
    )


def mark_ended(state: StatusState, reason: Optional[str] = None) -> None:
    """Set state.ended to the current UTC time (ISO 8601 with Z suffix).

    If reason is provided:
      - sets state.abandoned_reason = reason
      - transitions state.status = "abandoned"

    Does NOT call write_status — the caller is responsible for persisting the state.
    """
    state.ended = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if reason is not None:
        state.abandoned_reason = reason
        state.status = "abandoned"


def increment_wave(state: StatusState) -> None:
    """Increment state.total_waves by exactly 1.

    Does NOT call write_status — the caller is responsible for persisting the state.
    """
    state.total_waves += 1


def increment_wake(state: StatusState) -> None:
    """Increment state.total_wakes by exactly 1.

    Does NOT call write_status — the caller is responsible for persisting the state.
    """
    state.total_wakes += 1


def record_test_run(
    state: StatusState,
    result: str,
    regression_count: int,
    last_run: Optional[str] = None,
) -> None:
    """Record a test run snapshot.

    Updates state.test_status to a new TestStatus AND appends the same snapshot to
    state.test_status_history. Both references point to independently created objects.

    If last_run is None, uses the current UTC time with Z suffix.

    Does NOT call write_status — the caller is responsible for persisting the state.
    Does NOT deduplicate — if called from two code paths both calls land in history.
    That is intentional: caller must avoid double-calling if dedup is needed.
    """
    ts_str = last_run or datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot = TestStatus(last_run=ts_str, result=result, regression_count=regression_count)
    state.test_status = snapshot
    state.test_status_history.append(
        TestStatus(last_run=ts_str, result=result, regression_count=regression_count)
    )


def record_notification_sent(
    state: StatusState, key: str, at: Optional[str] = None
) -> None:
    """Record that a notification with the given key was sent.

    Sets state.notifications_sent[key] = at (or current UTC time if at is None).
    Timestamps use ISO 8601 with Z suffix.

    Does NOT call write_status — the caller is responsible for persisting the state.
    """
    state.notifications_sent[key] = at or datetime.now(tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def was_notification_sent_recently(
    state: StatusState, key: str, within_seconds: int
) -> bool:
    """Return True iff notifications_sent[key] is set and within the given window.

    Returns False (never raises) when:
      - key is absent from notifications_sent
      - the stored value is None or empty
      - the timestamp cannot be parsed

    Uses timezone-aware UTC datetimes throughout; the trailing Z is normalized to
    +00:00 before parsing with datetime.fromisoformat.
    """
    raw = state.notifications_sent.get(key)
    if not raw:
        return False
    try:
        # fromisoformat in Python < 3.11 does not accept trailing Z
        normalized = raw.replace("Z", "+00:00")
        sent_at = datetime.fromisoformat(normalized)
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        delta = datetime.now(tz=timezone.utc) - sent_at
        return delta.total_seconds() <= within_seconds
    except (ValueError, AttributeError):
        return False


def compute_phase_escalation_rate(state: StatusState, phase: str) -> float:
    """Return the percentage of tasks in `phase` whose verifier verdict was
    `blocker_persistent`.

    Returns 0.0 when the phase has no tasks (vacuous — no signal). Used by Step 8
    phase-transition guard against `plan.verification.escalation_threshold_pct`
    (default 15) to detect spec-quality drift before it spreads across the epic.
    """
    phase_tasks = [t for t in state.tasks.values() if t.phase == phase]
    if not phase_tasks:
        return 0.0
    blockers = sum(1 for t in phase_tasks if t.verifier_verdict == "blocker_persistent")
    return (blockers / len(phase_tasks)) * 100


def write_decision_evidence(
    plan_path: Path, task_id: int, content: str
) -> Path:
    """Write decision evidence to <plan_path>.evidence/<iso-timestamp>-task-<N>.txt.

    Creates the .evidence/ sibling directory if it doesn't exist (idempotent).
    Returns the path RELATIVE to plan_path's parent (portable across machines).

    Args:
        plan_path: Path to the plan.md file (used to locate evidence dir).
        task_id: Task number for the filename.
        content: Evidence content to write.

    Returns:
        Path to the evidence file, relative to plan_path.parent.
    """
    plan_path = Path(plan_path)
    evidence_dir = plan_path.parent / ".evidence"
    os.makedirs(evidence_dir, exist_ok=True)

    # ISO timestamp with task ID in filename (Windows-safe: no colons in filenames)
    iso_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    filename = f"{iso_ts}-task-{task_id}.txt"
    evidence_file = evidence_dir / filename

    # Write with UTF-8 encoding (important on Windows)
    with open(evidence_file, "w", encoding="utf-8") as f:
        f.write(content)

    # Return relative path: .evidence/filename (relative to plan_path.parent)
    return Path(".evidence") / filename


# --------------------------------------------------------------------------- #
# Merge record: the status.md a builder lane writes at merge (gh#319 route 2, registry Q-10081).
#
# WHY IT LIVES HERE AND NOT IN A NEW MODULE. The only reader that matters is `read_status` via
# `epic_survey`'s pass 3, and a merge record that it cannot parse is skipped with one log line -
# which is exactly what happened to the first hand-written ones (`KeyError('plan')`). Writing it
# beside the parser, and validating the composed text through `parse_status_text` before it
# touches disk, makes "written" imply "readable".
#
# PATH CONVENTION 2 (`<status dir>/<slug>.status.md`), not Convention 1 (`<dir>/status.md`). The
# project status dir is flat and holds lane state and the registry beside these records; one file
# per epic named by slug needs no per-epic directory, and the survey keys it by the same stem.
# --------------------------------------------------------------------------- #


class MergeRecordRefused(ValueError):
    """The merge record cannot be written without clobbering or mis-keying something."""


_MERGE_REQUIRED = ("epic", "plan", "started", "status", "current_phase")

# Illegal in a Windows filename or significant to a path or a glob. The slug is a filename stem
# here, so any of these either fails the write with a raw OSError (`a<b`) or names a different
# file than the one the survey keys by the stem (`a:b` is a drive-relative path on Windows).
_SEGMENT_FORBIDDEN = frozenset('<>:"|?*[]/\\')
_HEADING_RE = re.compile(r"^# +(.+?)\s*$", re.MULTILINE)

# What parse_status_text raises on a board it cannot build a StatusState from: a missing key
# (KeyError), a wrong type or unknown task field (TypeError, AttributeError), a bad task id or
# no frontmatter (ValueError). The CLI turns each into a refusal, never a traceback.
_PARSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError)


def merge_record_path(data_root: Path | str, project: str, slug: str) -> Path:
    """``<data_root>/projects/<project>/status/<slug>.status.md`` (survey Convention 2)."""
    for label, value in (("project", project), ("slug", slug)):
        if (
            not isinstance(value, str)
            or not value.strip()
            or value in (".", "..")
            or any(ch in _SEGMENT_FORBIDDEN for ch in value)
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise MergeRecordRefused(
                f"{label} must be a single literal path segment, got {value!r}"
            )
    return Path(data_root) / "projects" / project / "status" / f"{slug}.status.md"


def _read_doc(path: Path) -> Optional[tuple[dict, str]]:
    """``(frontmatter, body)`` of a markdown doc, or ``None`` - with a log line - when unreadable.

    Only ever a source of NAMES for the ownership cross-check: a doc that cannot be read adds no
    name, so the check can only get stricter, never looser.
    """
    try:
        text = strip_bom(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("mark-done: cannot read %s for epic titles, ignoring it (%s)", path, exc)
        return None
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        fm = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logger.warning("mark-done: unparseable frontmatter in %s, ignoring it (%s)", path, exc)
        return None
    return (fm if isinstance(fm, dict) else {}), text[match.end() :]


def _first_heading(path: Path) -> list[str]:
    doc = _read_doc(path)
    if doc is None:
        return []
    heading = _HEADING_RE.search(doc[1])
    return [heading.group(1)] if heading else []


def _plan_names(
    data_root: Path | str, project: str, slug: str, board_plan: object
) -> set[str]:
    """Every title this epic's PLAN gives it: frontmatter ``epic:``/``title:`` and first heading.

    The orchestrator copies the plan's ``epic:`` into the board, and that title is usually not the
    spec's: all three live orchestrator boards on 2026-09-24 differ from their spec titles.
    Candidates are the plan files the survey globs whose filename slug is ``slug``, plus the plan
    the board itself names when that path still exists; each counts only if its own slug is
    ``slug``.
    """
    from agentflow.epic_survey import SurveyPatterns

    root = Path(data_root)
    candidates = [
        p
        for pattern in SurveyPatterns().for_project(project).plan_globs
        for p in sorted(root.glob(pattern))
        if _SLUG_FILENAME_RE.match(p.stem).group(1) == slug
    ]
    if isinstance(board_plan, str) and board_plan.strip():
        bp = Path(board_plan)
        if bp.is_file():
            candidates.append(bp)
    names: set[str] = set()
    for p in candidates:
        doc = _read_doc(p)
        if doc is None:
            continue
        fm, body = doc
        own_slug = str(fm.get("slug") or _SLUG_FILENAME_RE.match(p.stem).group(1))
        if own_slug != slug:
            continue
        names.update(str(fm[k]) for k in ("epic", "title") if fm.get(k))
        heading = _HEADING_RE.search(body)
        if heading:
            names.add(heading.group(1))
    return names


def _terminal_spec_dirs(data_root: Path | str, project: str) -> list[Path]:
    """The project's spec partitions where ``reconcile`` files a terminal spec (gh#891).

    Read off the survey's own spec globs rather than spelled here, so the layout and the
    partition names (``epic_survey.TERMINAL_PARTITIONS``) stay single-sourced: a glob whose
    directory is one of those partitions is exactly a place reconcile moves specs to.
    """
    from agentflow.epic_survey import TERMINAL_PARTITIONS, SurveyPatterns

    root = Path(data_root)
    return [
        root / Path(pattern).parent
        for pattern in SurveyPatterns().for_project(project).spec_globs
        if Path(pattern).parent.name in TERMINAL_PARTITIONS
    ]


def resolve_plan(data_root: Path | str, project: str, plan: Path | str) -> tuple[Path, bool]:
    """The spec file ``mark_done`` reads for ``plan``, and whether it was followed (gh#891).

    ``plan`` itself when it exists. Otherwise ``reconcile`` has probably filed the spec into a
    terminal partition since the lane noted its path - a retried mark-done after a merge is the
    usual case - so the ONE file with the same name in the project's ``design/specs/{done,
    archived}/`` is used instead, and ``(that path, True)`` returned. The slug and project checks
    still run on whatever file this returns, so following can never mark a different epic done.

    Refuses when nothing is there either (the message says where else it looked, so the lane
    knows the move was considered) and when more than one partition holds that name: two
    candidates are two different histories, and picking one would be a guess.
    """
    plan_path = Path(plan)
    if plan_path.exists():
        return plan_path, False
    dirs = _terminal_spec_dirs(data_root, project)
    found = [d / plan_path.name for d in dirs if (d / plan_path.name).is_file()]
    if len(found) == 1:
        return found[0], True
    if not found:
        raise MergeRecordRefused(
            f"--plan {plan_path} is not a readable spec: no such file, and no "
            f"{plan_path.name!r} in {' or '.join(str(d) for d in dirs)} either "
            "(where reconcile files a done or archived spec)"
        )
    raise MergeRecordRefused(
        f"--plan {plan_path} does not exist and {plan_path.name!r} is in more than one terminal "
        f"partition ({', '.join(str(p) for p in found)}); pass the one this epic shipped"
    )


def _is_spec_location(path: Path) -> bool:
    """``path`` sits where a spec lives: ``.../specs/<file>`` or ``.../specs/<terminal>/<file>``."""
    from agentflow.epic_survey import TERMINAL_PARTITIONS

    parent = path.parent
    return parent.name == "specs" or (
        parent.name in TERMINAL_PARTITIONS and parent.parent.name == "specs"
    )


def _refuse_plan_outside_design(data_root: Path | str, project: str, plan_path: Path) -> None:
    """Refuse a ``project:``-less spec that is not under ``<data_root>/projects/<project>/design/``.

    The plan and the project's own design directory are each resolved, so a junction or symlink
    anywhere on either path (above ``projects/`` or at ``projects/<project>/design`` itself)
    compares by where the files really are. The project name must also appear on disk exactly as
    given, case-sensitively like the ``project:`` field check: on a case-insensitive filesystem
    ``--project Toy`` would otherwise reach ``projects/toy``.
    """
    projects = Path(data_root) / "projects"
    try:
        real = plan_path.resolve(strict=True)
        names = os.listdir(projects)
    except OSError as exc:
        raise MergeRecordRefused(
            f"--plan {plan_path} could not be resolved to check which project owns it: {exc}"
        ) from exc
    design = None
    if project in names:
        try:
            design = (projects / project / "design").resolve(strict=True)
        except OSError:
            design = None  # the project has no design tree, so it owns no spec
    if design is not None and real.is_relative_to(design) and real != design:
        return
    try:
        rel = real.relative_to(projects.resolve(strict=True)).parts
    except (OSError, ValueError):
        rel = ()
    if len(rel) > 1:
        where = f"is in project {rel[0]!r}'s tree"
    elif rel:
        where = "is directly in the projects directory"
    else:
        where = "is outside the data root's projects directory"
    raise MergeRecordRefused(
        f"--plan {plan_path} (resolved: {real}) has no project: field and {where}, "
        f"not under {projects / project / 'design'}; a record here would mark an epic done on a "
        "board that has no spec for it"
    )


def mark_done(
    data_root: Path | str,
    *,
    project: str,
    slug: str,
    plan: Path | str,
    shipped: str,
    queue_id: Optional[str] = None,
    actor: Optional[str] = None,
    now: Optional[str] = None,
) -> tuple[Path, bool]:
    """Write (or complete) the epic's merge record with ``status: done``. Returns ``(path, changed)``.

    Idempotent: the same inputs a second time change no byte and return ``changed=False``. An
    existing record for the SAME epic is updated in place - its other keys and tasks are kept,
    and a missing required key (a hand-written record's ``plan``/``started``) is filled.

    Completing a board also finishes it, so it no longer waits on a wake or a human:
    ``next_wake`` is removed; ``ended`` is set to ``now`` (or the current UTC time) when absent
    or null and never moved once set; every escalation still open is answered
    ``closed by mark-done at merge: <shipped>`` through ``answer_escalation``, which logs the
    close as a decision stamped with ``ended``, while an answered one is left as it is; and a
    body carrying an AUTO block has that block re-rendered from the completed state, with every
    byte outside it - the user notes after ``AUTO:end`` included - kept. Tasks are kept exactly
    as the board had them, so a task left ``in_flight`` still renders as current activity. A
    body with no AUTO block is kept verbatim. Each of these is a no-op on a board already
    finished, which is what keeps a re-run byte-identical.
    That includes the orchestrator's own running board at this path, whose ``epic:`` is the plan's
    human title rather than the slug: the file is ours by its stem, as the survey decides it.
    Refuses (``MergeRecordRefused``) rather than overwrite when ``epic:`` is some third value that
    is neither the slug nor a title of this epic's spec or plan, when the file has no parseable
    frontmatter, or when ``plan``'s spec slug is not ``slug`` - the survey joins
    the record to the spec by slug, so a mismatched one would mark the wrong epic done or none.

    Also refuses when the spec's ``project:`` frontmatter names a project other than ``project``
    (gh#888): the record would land on the other board as a done epic with no spec there, while
    the real epic stays selectable on its own. When the field is present it decides, not the
    path. A spec with NO ``project:`` (older specs predate the field) is decided by location
    instead: it must resolve to a file under ``<data_root>/projects/<project>/design/`` (which
    includes the ``done/`` and ``archived/`` partitions ``resolve_plan`` follows into), and is
    refused otherwise - inside another project's tree or outside the data root entirely. Both
    sides are resolved before the compare, because ``data_root`` and spec paths are routinely
    reached through junctions. Both matches are case-sensitive on purpose (``Toy`` is refused
    under ``toy``): project names are directory names, and a near-miss refusing is the safe way
    to be wrong.

    A fresh record's ``epic:`` and heading are the spec's ``title`` (its slug when it has none),
    because the survey shows the board's ``epic:`` as the epic's title once a board exists
    (gh#892); the ownership cross-check already counts the spec title as ours.

    ``plan`` is resolved through ``resolve_plan`` (gh#891): a spec path that reconcile has since
    filed into ``done/`` or ``archived/`` is followed there when exactly one file of that name
    exists, and every check above runs on the followed file. A stored ``plan:`` naming the
    spec's old, now-missing location is rewritten to the file read; one that exists is kept.
    """
    if not shipped or not shipped.strip():
        raise MergeRecordRefused("shipped must name the evidence, e.g. '<sha> (PR N)'")
    path = merge_record_path(data_root, project, slug)
    m = _SLUG_FILENAME_RE.match(slug)
    if m and m.group(1) != slug:
        raise MergeRecordRefused(
            f"slug {slug!r} carries a date prefix or -design suffix; the survey keys this file "
            f"as {m.group(1)!r}, so pass the spec slug"
        )
    if not path.parent.is_dir():
        raise MergeRecordRefused(f"status dir does not exist: {path.parent} (wrong --project?)")
    plan_path, _followed = resolve_plan(data_root, project, plan)
    try:
        spec = parse_spec_frontmatter(plan_path)
    except (OSError, SpecFormatError, yaml.YAMLError, UnicodeDecodeError) as exc:
        raise MergeRecordRefused(f"--plan {plan_path} is not a readable spec: {exc}") from exc
    if spec.slug != slug:
        raise MergeRecordRefused(f"--plan {plan_path} is spec {spec.slug!r}, not {slug!r}")
    spec_doc = _read_doc(plan_path)
    if spec_doc is None:  # it parsed a moment ago, so this is a race; do not guess past it
        raise MergeRecordRefused(f"--plan {plan_path} could not be re-read for its project:")
    spec_project = spec_doc[0].get("project")
    if spec_project not in (None, "") and str(spec_project) != project:
        raise MergeRecordRefused(
            f"--plan {plan_path} is a spec of project {str(spec_project)!r}, not {project!r}; "
            "a record here would mark an epic done on a board that has no spec for it"
        )
    if spec_project in (None, ""):
        _refuse_plan_outside_design(data_root, project, plan_path)

    try:
        existing_text = path.read_text(encoding="utf-8") if path.exists() else None
    except (OSError, UnicodeDecodeError) as exc:
        raise MergeRecordRefused(
            f"{path} exists but cannot be read; not overwriting it: {exc}"
        ) from exc
    if existing_text is None:
        data: dict = {}
        body = f"# {' '.join(spec.title.split())}: done\n"
    else:
        # Parsed BOM-stripped; `existing_text` itself stays raw so the idempotence compare below
        # sees the BOM and rewrites the record without it rather than calling it unchanged.
        existing_doc = strip_bom(existing_text)
        match = _FRONTMATTER_RE.match(existing_doc)
        if not match:
            raise MergeRecordRefused(f"{path} exists with no YAML frontmatter; not overwriting it")
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise MergeRecordRefused(
                f"{path} has unparseable frontmatter; not overwriting it: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise MergeRecordRefused(f"{path} frontmatter is not a mapping; not overwriting it")
        # Ownership is the filename stem, which is how the survey keys the board and is already
        # `slug`. `epic:` is only a cross-check: the orchestrator writes the epic's human title
        # there, so the slug and every title this epic's own spec and plan give are all "ours";
        # only a third value means the file was mis-keyed for some other epic.
        names = {slug, spec.title, *_first_heading(plan_path)}
        names.update(_plan_names(data_root, project, slug, data.get("plan")))
        epic_value = data.get("epic")
        if epic_value is not None and not isinstance(epic_value, str):
            raise MergeRecordRefused(
                f"{path} has a non-text epic: {epic_value!r}; not overwriting it"
            )
        body = existing_doc[match.end() :]
        # A record this writer completed (done, with merge evidence) at this slug's own path is
        # ours whatever its epic: says - the spec may have been retitled since, and a re-run must
        # stay idempotent rather than refuse its own output. Its stale title is refreshed, and so
        # is the heading when it is still exactly the one this writer generated.
        ours_by_record = data.get("status") == "done" and bool(data.get("shipped"))
        if epic_value is None or not epic_value.strip():
            # No `epic:` (absent, blank, or a key with no value) names no other epic, so there is
            # nothing to cross-check: the filename already makes the record ours. Fill the title
            # rather than refuse (gh#1162); setdefault below would keep a null value as it is.
            data["epic"] = spec.title
        elif epic_value not in names:
            if not ours_by_record:
                raise MergeRecordRefused(
                    f"{path} is the record for epic {data.get('epic')!r}, which is neither "
                    f"{slug!r} nor a title of its spec or plan; not overwriting it"
                )
            if body.strip() == f"# {' '.join(epic_value.split())}: done":
                body = f"# {' '.join(spec.title.split())}: done\n"
            data["epic"] = spec.title

    started = now or datetime.now(tz=timezone.utc).isoformat()
    data.setdefault("epic", spec.title)  # parse_spec_frontmatter falls back to the slug
    # A stored `plan:` that names this spec's OLD location (same file name, a spec directory,
    # gone from disk) is refreshed to the file just read, or the record keeps pointing at a path
    # reconcile removed (gh#891). One that still exists is kept: an orchestrator board's `plan:`
    # names its plan file, not the spec. The spec-directory test keeps a relative plan path -
    # which cannot be checked from here and may share the spec's file name - from being taken
    # for a dead spec path. After the rewrite the stored path exists, so a re-run keeps it.
    stored_plan = data.get("plan")
    if (
        isinstance(stored_plan, str)
        and stored_plan.strip()
        and Path(stored_plan).name == plan_path.name
        and _is_spec_location(Path(stored_plan))
        and not Path(stored_plan).exists()
    ):
        data["plan"] = str(plan_path)
    data.setdefault("plan", str(plan_path))
    data.setdefault("started", started)
    data["status"] = "done"
    data["current_phase"] = "done"
    data["project"] = project
    if queue_id:
        data["queue_id"] = queue_id
    data["shipped"] = shipped
    if actor:
        data["written_by"] = actor
    # A done board has no next wake and has ended (gh#890). `ended` is set once and kept, so a
    # re-run - with or without `now` - finds it present and changes no byte.
    # `ended: null` counts as absent: real boards carry the key with no value, and a setdefault
    # would leave a done board that never ended.
    data.pop("next_wake", None)
    if data.get("ended") is None:
        data["ended"] = now or datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Key order: the five the parser requires first, so a reader sees what makes it a board.
    ordered = {k: data[k] for k in _MERGE_REQUIRED}
    ordered.update((k, v) for k, v in data.items() if k not in ordered)

    def _render(fm: dict, text: str) -> str:
        fm_yaml = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False, allow_unicode=True)
        return f"---\n{fm_yaml}---\n\n{text.lstrip()}".replace("\r\n", "\n")

    def _parsed(text: str) -> StatusState:
        # A record the survey would skip is a refusal here, not a warning there. The usual cause
        # is an existing board carrying something the parser rejects (an unknown task field, a
        # non-integer task id), which this writer keeps verbatim and so cannot repair.
        try:
            return parse_status_text(text)
        except _PARSE_ERRORS as exc:
            raise MergeRecordRefused(
                f"{path}: the completed record would not parse ({type(exc).__name__}: {exc}); "
                "not writing it"
            ) from exc

    full = _render(ordered, body)
    state = _parsed(full)
    # An escalation still open at merge would keep the board asking a question nobody needs to
    # answer. Each is closed through `answer_escalation`, so the decision log records the close
    # like any other answer. Only `escalations`/`decisions` are taken back from the state: every
    # other key - tasks above all - stays exactly as the board had it. A re-run finds nothing
    # open, adds no event and so changes no byte.
    # The close is stamped with the board's `ended`, not the wall clock `answer_escalation` would
    # use, so the log never shows a question closed after the board ended and a fixed `now`
    # gives fixed output.
    open_ids = [e.id for e in state.escalations if e.answer is None]
    logged_before = len(state.decisions)
    for eid in open_ids:
        answer_escalation(state, eid, f"closed by mark-done at merge: {shipped}")
    ended_at = ordered["ended"]
    if isinstance(ended_at, datetime):  # an unquoted YAML timestamp parses to a datetime
        ended_at = ended_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for decision in state.decisions[logged_before:]:
        decision.at = str(ended_at)
    if open_ids:
        answered = _state_to_frontmatter_dict(state)
        ordered["escalations"] = answered["escalations"]
        ordered["decisions"] = answered["decisions"]
    # The orchestrator's AUTO block still shows the running board (open escalations, a next
    # wake, current activity); re-render it from the completed state. Only the block changes -
    # the user notes after `AUTO:end` and anything before `AUTO:start` are kept byte for byte.
    # A body with no AUTO block (this writer's own `# <title>: done`) is not ours to restyle.
    has_auto = bool(_AUTO_BLOCK_RE.search(body))
    if has_auto:
        body = _replace_auto_block(body, state)
    if open_ids or has_auto:
        full = _render(ordered, body)
        _parsed(full)
    if existing_text == full:
        return path, False
    try:
        atomic_io.atomic_write(path, full, newline="")  # newline="" keeps LF on every platform
    except OSError as exc:
        raise MergeRecordRefused(f"could not write {path}: {exc}") from exc
    return path, True


def _survey_boards_for(status_dir: Path, slug: str) -> list[Path]:
    """Every board survey pass 3 counts for ``slug`` under ``status_dir``, sorted.

    Delegates to the survey's own gatherer (``epic_survey._status_boards_under``, both filename
    conventions, signature-gated) and its stat step (``_stat_status_boards``, which drops a board
    that vanished since the walk), rather than copying either: a copy is what drifted the last
    time the rule changed. Staleness and parse checks do not apply, matching the survey, for
    which a stale or malformed board beside a fresh one still makes a duplicate.
    """
    # Local import: epic_survey imports StatusEnum and read_status from this module at its top
    # level, so a top-level import here would be a cycle.
    from agentflow.epic_survey import _stat_status_boards, _status_boards_under

    boards = _status_boards_under(status_dir).get(slug, set())
    return [p for p, _ in _stat_status_boards(boards)]


def find_duplicate_boards(data_root: Path | str, project: str, slug: str) -> list[Path]:
    """The OTHER boards keyed as ``slug`` beside the merge record, in sorted order.

    Non-empty means the survey refuses this epic: a slug with more than one board reads as
    ``DUPLICATE_STATUS_BOARDS`` - neither done nor selectable - until one board remains, so the
    record just written does not make it survey as done. Empty when the record is the only board.
    """
    own = merge_record_path(data_root, project, slug)
    boards = _survey_boards_for(own.parent, slug)
    return [] if len(boards) < 2 else [b for b in boards if b != own]


def find_title_keyed_boards(
    data_root: Path | str, project: str, slug: str, titles: set[str]
) -> list[Path]:
    """Boards the survey keys as some OTHER slug whose ``epic:`` is one of this epic's *titles*.

    A board named from the epic's title instead of its plan slug is a second record for the same
    epic under a different key: no join by slug finds it, so it reads as a separate epic that
    stays ``running`` after this one is marked done. The inverse of ``find_duplicate_boards``,
    which catches the same key under a different name.

    A board keyed as a slug that has its OWN spec in the project is skipped: that is a real epic
    which happens to share a title (two specs both called "Polish pass"), not a duplicate, and
    warning there would tell a lane to delete a correct record.
    """
    from agentflow.epic_survey import (
        SurveyPatterns,
        _normalize_slug_stem,
        _rwalk_match,
        is_orchestrator_status,
    )

    # The spec parser falls back to the slug when a spec has no title, and a slug is not a title.
    wanted = {t for t in titles if t and t != slug}
    status_dir = merge_record_path(data_root, project, slug).parent
    root = Path(data_root)
    spec_slugs: set[str] = set()
    for pattern in SurveyPatterns().for_project(project).spec_globs:
        for sp in root.glob(pattern):
            doc = _read_doc(sp)
            fm_slug = doc[0].get("slug") if doc is not None else None
            spec_slugs.add(str(fm_slug) if fm_slug else _SLUG_FILENAME_RE.match(sp.stem).group(1))
    out = []
    for p in sorted(_rwalk_match(status_dir, lambda n: n.endswith("status.md"))):
        if p.name == "status.md":
            keyed_as = _normalize_slug_stem(p.parent.name)  # Convention 1
        elif p.name.endswith(".status.md"):
            keyed_as = _normalize_slug_stem(p.name[: -len(".status.md")])  # Convention 2
        else:
            continue
        if keyed_as == slug or keyed_as in spec_slugs or not is_orchestrator_status(p):
            continue
        doc = _read_doc(p)
        epic = doc[0].get("epic") if doc is not None else None
        if isinstance(epic, str) and epic in wanted:
            out.append(p)
    return out


def main(argv: Optional[list] = None) -> int:
    """``python -m agentflow.status_writer mark-done ...`` - the merge-step writer."""
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python -m agentflow.status_writer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    md = sub.add_parser("mark-done", help="write the epic's status.md with status: done at merge")
    md.add_argument("--project", required=True)
    md.add_argument("--slug", required=True, help="the spec slug (no date prefix)")
    md.add_argument(
        "--plan",
        required=True,
        help="the epic's spec path; if it is gone, the one same-named file in the project's "
        "design/specs/done/ or archived/ is used",
    )
    md.add_argument("--shipped", required=True, help='merge evidence, e.g. "<sha> (PR N)"')
    md.add_argument("--queue-id", help="the registry Queue id, E-NNNN-...")
    md.add_argument("--actor", help="the lane writing it, e.g. jr-dev-1")
    md.add_argument("--data-root", help="override environment.data_root (tests, drills)")
    args = ap.parse_args(argv)

    if args.data_root:
        data_root = args.data_root
    else:
        from agentflow import config

        # A lane raises a refusal as a Blocker row, so it is one line that keeps the
        # remediation; a traceback reads as a crash in this writer rather than a config it
        # could not resolve (gh#1161). OSError and UnicodeDecodeError are an environment.md
        # that cannot be read at all, which load_environment reports before any ConfigError.
        try:
            data_root = config.load_environment().data_root
        except (config.ConfigError, OSError, UnicodeDecodeError) as exc:
            line = config.error_line(exc, config.environment_path())
            print(f"refused: cannot resolve data_root: {line}", file=sys.stderr)
            return 2
    try:
        path, changed = mark_done(
            data_root,
            project=args.project,
            slug=args.slug,
            plan=args.plan,
            shipped=args.shipped,
            queue_id=args.queue_id,
            actor=args.actor,
        )
    except MergeRecordRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"{'written' if changed else 'unchanged'}: {path}")
    # A WARNING, not a refusal: the record above is correct and already on disk, and the other
    # board is not this writer's to delete. It is on stderr so a lane raises it like a refusal.
    for other in find_duplicate_boards(data_root, args.project, args.slug):
        print(
            f"WARNING: {other} is also keyed as {args.slug!r}, so this epic has duplicate status "
            f"boards beside {path}; the survey refuses it (neither done nor selectable) until "
            "one board remains - move or remove the stale one",
            file=sys.stderr,
        )
    # mark_done resolved the same --plan a moment ago, so this cannot refuse now. The note goes
    # to stderr: stdout is the one `written:`/`unchanged:` line lanes parse, and this is not a
    # WARNING - nothing is wrong, the lane just learns where its spec went.
    plan_path, followed = resolve_plan(data_root, args.project, args.plan)
    if followed:
        print(
            f"note: --plan {args.plan} does not exist; followed it to {plan_path}, where "
            "reconcile filed the spec",
            file=sys.stderr,
        )
    titles = {
        parse_spec_frontmatter(plan_path).title,  # mark_done parsed it a moment ago
        *_first_heading(plan_path),
        *_plan_names(data_root, args.project, args.slug, None),
    }
    for other in find_title_keyed_boards(data_root, args.project, args.slug, titles):
        print(
            f"WARNING: {other} names this epic in its epic: field but the survey keys it by its "
            f"filename, not as {args.slug!r}, so it reads as a separate epic that is never "
            f"marked done; fold it into {path} or remove it",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
