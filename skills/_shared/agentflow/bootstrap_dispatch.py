"""orc-bootstrap return parser — Component 6.

Parses the JSON payload returned by the orc-bootstrap subagent into
typed dataclasses. Raises BootstrapMalformed (with a precise field_path)
on any schema violation so callers get actionable diagnostics rather than
raw KeyError / TypeError messages.

Unknown top-level keys are rejected so typos in subagent output are caught
immediately rather than silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

from agentflow.status_writer import Escalation

# Required top-level keys — must be exactly this set (no more, no less).
_REQUIRED_KEYS: frozenset[str] = frozenset({
    "ready_tasks",
    "in_flight_reconcile",
    "escalations",
    "preflight_audit_status",
    "deadlock",
})

PreflightAuditStatus = Literal["clean", "findings", "blocked"]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class ReadyTaskInstruction:
    """Instruction for a single task the orchestrator should dispatch."""

    id: int
    model: str
    complexity: str
    phase: str
    file_allowlist: list[str]
    regression_guard: list[str]
    wave_deadline_minutes: int


@dataclass
class ReconcileAction:
    """Action to take on a currently in-flight task."""

    task_id: int
    action: str
    reason: str


@dataclass
class BootstrapResult:
    """Parsed result of an orc-bootstrap subagent run."""

    ready_tasks: list[ReadyTaskInstruction]
    in_flight_reconcile: list[ReconcileAction]
    escalations: list[Escalation]
    preflight_audit_status: PreflightAuditStatus
    deadlock: bool


class BootstrapMalformed(Exception):
    """Raised when the orc-bootstrap JSON fails schema validation.

    Attributes
    ----------
    field_path:
        Dot-separated path to the offending field (e.g. ``"ready_tasks[0].id"``).
    message:
        Human-readable description of the violation. Must NOT include the full
        raw JSON (context-bloat / PII guard).
    """

    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(f"{field_path}: {message}")
        self.field_path = field_path
        self.message = message


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_task(raw: object, idx: int) -> ReadyTaskInstruction:
    prefix = f"ready_tasks[{idx}]"
    if not isinstance(raw, dict):
        raise BootstrapMalformed(prefix, f"expected dict, got {type(raw).__name__}")
    required = ("id", "model", "complexity", "phase", "file_allowlist",
                "regression_guard", "wave_deadline_minutes")
    for key in required:
        if key not in raw:
            raise BootstrapMalformed(f"{prefix}.{key}", "required field missing")
    return ReadyTaskInstruction(
        id=raw["id"],
        model=raw["model"],
        complexity=raw["complexity"],
        phase=raw["phase"],
        file_allowlist=raw["file_allowlist"],
        regression_guard=raw["regression_guard"],
        wave_deadline_minutes=raw["wave_deadline_minutes"],
    )


def _parse_reconcile(raw: object, idx: int) -> ReconcileAction:
    prefix = f"in_flight_reconcile[{idx}]"
    if not isinstance(raw, dict):
        raise BootstrapMalformed(prefix, f"expected dict, got {type(raw).__name__}")
    for key in ("task_id", "action", "reason"):
        if key not in raw:
            raise BootstrapMalformed(f"{prefix}.{key}", "required field missing")
    return ReconcileAction(
        task_id=raw["task_id"],
        action=raw["action"],
        reason=raw["reason"],
    )


def _parse_escalation(raw: object, idx: int) -> Escalation:
    prefix = f"escalations[{idx}]"
    if not isinstance(raw, dict):
        raise BootstrapMalformed(prefix, f"expected dict, got {type(raw).__name__}")
    for key in ("id", "task", "asked", "question"):
        if key not in raw:
            raise BootstrapMalformed(f"{prefix}.{key}", "required field missing")
    return Escalation(
        id=raw["id"],
        task=raw["task"],
        asked=raw["asked"],
        question=raw["question"],
        answer=raw.get("answer"),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_bootstrap_return(raw_json: str) -> BootstrapResult:
    """Parse the JSON string returned by orc-bootstrap into a BootstrapResult.

    Parameters
    ----------
    raw_json:
        JSON string produced by the orc-bootstrap subagent.

    Returns
    -------
    BootstrapResult

    Raises
    ------
    BootstrapMalformed
        On any schema violation.  ``field_path`` points to the offending key;
        ``message`` describes the violation without including the full payload.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise BootstrapMalformed(
            "json", f"invalid JSON: {exc.msg} at line {exc.lineno} col {exc.colno}"
        ) from exc

    if not isinstance(data, dict):
        raise BootstrapMalformed("json", f"expected top-level object, got {type(data).__name__}")

    # Reject unknown keys (catches subagent typos immediately)
    unknown = set(data.keys()) - _REQUIRED_KEYS
    if unknown:
        key = next(iter(sorted(unknown)))
        raise BootstrapMalformed(key, f"unknown key '{key}' — only {sorted(_REQUIRED_KEYS)} are valid")

    # Check all required keys present
    for key in _REQUIRED_KEYS:
        if key not in data:
            raise BootstrapMalformed(key, "required field missing")

    # ready_tasks
    raw_tasks = data["ready_tasks"]
    if not isinstance(raw_tasks, list):
        raise BootstrapMalformed("ready_tasks", f"expected list, got {type(raw_tasks).__name__}")
    ready_tasks = [_parse_task(t, i) for i, t in enumerate(raw_tasks)]

    # in_flight_reconcile
    raw_reconcile = data["in_flight_reconcile"]
    if not isinstance(raw_reconcile, list):
        raise BootstrapMalformed(
            "in_flight_reconcile", f"expected list, got {type(raw_reconcile).__name__}"
        )
    in_flight_reconcile = [_parse_reconcile(r, i) for i, r in enumerate(raw_reconcile)]

    # escalations
    raw_escalations = data["escalations"]
    if not isinstance(raw_escalations, list):
        raise BootstrapMalformed(
            "escalations", f"expected list, got {type(raw_escalations).__name__}"
        )
    escalations = [_parse_escalation(e, i) for i, e in enumerate(raw_escalations)]

    # preflight_audit_status
    pf_status = data["preflight_audit_status"]
    valid_pf = {"clean", "findings", "blocked"}
    if pf_status not in valid_pf:
        raise BootstrapMalformed(
            "preflight_audit_status",
            f"expected one of {sorted(valid_pf)}, got {pf_status!r}"
        )

    # deadlock
    deadlock = data["deadlock"]
    if not isinstance(deadlock, bool):
        raise BootstrapMalformed("deadlock", f"expected bool, got {type(deadlock).__name__}")

    return BootstrapResult(
        ready_tasks=ready_tasks,
        in_flight_reconcile=in_flight_reconcile,
        escalations=escalations,
        preflight_audit_status=pf_status,
        deadlock=deadlock,
    )
