"""Wave-timeout detection for the orchestrator's Step 4.0.

``check_wave_timeouts`` reads a persisted status board and a parsed plan and
returns one ``TimeoutEvent`` per in-flight task whose wave has run past its
deadline. It is pure: the orchestrator applies each event itself, and that
includes incrementing ``TaskState.timeout_count`` before choosing between
requeue and block (see the orchestrator SKILL, Step 4.0).

The module once also carried a spec/plan/status merge core. That half was
re-homed into ``epic_survey.py`` and removed here (gh#378).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from agentflow.plan_parser import PlanFrontmatter
from agentflow.status_writer import StatusState

_LOG = logging.getLogger(__name__)

_DEFAULT_WAVE_DEADLINE_MINUTES = 30


@dataclass(frozen=True)
class TimeoutEvent:
    """A wave timeout event for a single in-flight task.

    Produced by check_wave_timeouts when a task has been in_flight past its
    deadline. The caller is responsible for applying the event: it increments
    the task's timeout_count, then requeues or blocks the task.

    Every attribute the orchestrator SKILL's Step 4.0 reads off an event must
    exist here; ``test_lifecycle_wave_timeouts`` pins that (gh#675).
    """

    task_id: int
    dispatched_at: str          # ISO 8601 UTC string from task.since
    elapsed_minutes: float      # wall-clock minutes since dispatch
    suggested_action: Literal["requeue", "block"]
    deadline_minutes: int       # the deadline this task was measured against
    summary: str                # decision-log line for the tier-2 decision


def check_wave_timeouts(
    state: StatusState,
    plan: PlanFrontmatter,
    now: datetime,
) -> list[TimeoutEvent]:
    """Return timeout events for every in_flight task that has exceeded its deadline.

    Pure function — does NOT mutate state.  The caller is responsible for
    applying the returned events (incrementing timeout_count, requeuing, etc.).

    Deadline resolution (first wins):
      1. task-level ``wave_deadline_minutes`` on the Task in *plan*
      2. plan-level ``plan.verification.wave_deadline_minutes``
      3. hardcoded default (30 minutes)

    Action resolution, on the timeout_count read BEFORE the caller increments:
      - timeout_count == 0 (first timeout) -> ``"requeue"``
      - timeout_count >= 1 (already timed out once) -> ``"block"``

    ``since`` may be an ISO 8601 string or a datetime (what YAML yields for an
    unquoted timestamp); either is normalized to aware UTC. A missing ``since``
    is skipped (no event); an unparseable one is skipped with a WARNING.

    *now* must be timezone-aware: a naive *now* raises TypeError on subtraction.
    """
    events: list[TimeoutEvent] = []

    for tid, task_state in state.tasks.items():
        if task_state.state != "in_flight":
            continue

        since_raw = task_state.since
        if not since_raw:
            continue

        # An unquoted timestamp hand-edited into status.md loads from YAML as a
        # datetime, not a str; string-only parsing crashed Step 4.0 on every wake.
        if isinstance(since_raw, datetime):
            since_dt = since_raw
        elif isinstance(since_raw, str):
            try:
                since_dt = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
            except ValueError:
                _LOG.warning("task %s has unparseable since=%r; skipping timeout check", tid, since_raw)
                continue
        else:
            _LOG.warning("task %s has unparseable since=%r; skipping timeout check", tid, since_raw)
            continue

        # Enforce aware UTC: a naive value is taken as UTC, an offset one is converted.
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        else:
            since_dt = since_dt.astimezone(timezone.utc)
        if not isinstance(since_raw, str):
            # TimeoutEvent.dispatched_at is documented as an ISO string.
            since_raw = since_dt.isoformat()

        elapsed = (now - since_dt).total_seconds() / 60.0

        # Resolve effective deadline
        plan_task = plan.tasks.get(tid)
        deadline: int = _DEFAULT_WAVE_DEADLINE_MINUTES
        if plan_task is not None and plan_task.wave_deadline_minutes is not None:
            deadline = plan_task.wave_deadline_minutes
        elif plan.verification is not None and plan.verification.wave_deadline_minutes is not None:
            deadline = plan.verification.wave_deadline_minutes

        if elapsed <= deadline:
            continue

        # Determine action: first timeout -> requeue, second+ -> block
        timeout_count = task_state.timeout_count  # read-only; pure
        action: Literal["requeue", "block"] = "requeue" if timeout_count == 0 else "block"

        events.append(TimeoutEvent(
            task_id=tid,
            dispatched_at=since_raw,
            elapsed_minutes=elapsed,
            suggested_action=action,
            deadline_minutes=deadline,
            summary=(
                f"wave timeout: task {tid} in_flight {elapsed:.0f}m against a "
                f"{deadline}m deadline (dispatched {since_raw}, prior timeouts "
                f"{timeout_count}) -> {action}"
            ),
        ))

    return events
