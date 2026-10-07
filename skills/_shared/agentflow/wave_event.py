"""Wave-event reporter — lets an in-flight wave workflow update status.md mid-run.

A wave workflow runs while the orchestrator (the governor) is idle between wakes,
so status.md / any status dashboard rendered from it would otherwise go quiet for the whole wave.
This module is the narrow, serialized write path the workflow's agents call to
keep status.md ticking live, via the CLI:

    python -m agentflow.wave_event note       --status <path> --task N --tier T --summary "..."
    python -m agentflow.wave_event task-state --status <path> --task N --state code_review \
        [--verifier-verdict warnings] [--findings '{"low":1,"medium":0,"blocker":0}'] \
        [--commit SHA] [--model sonnet] [--role implementer] [--verifier-iterations 2]

Scope (deliberately tight — the governor keeps sole authority over session state):
  * appends decision-log notes (task-scoped or wave-scoped via task=0), and
  * sets TASK-level state + verifier verdict/findings on an EXISTING task.
It NEVER mutates session-level `state.status`. There is no code path here that can.

Concurrency: multiple wave agents may report at once. Each write takes a short
blocking lock on `<status>.wave-event.lock` (separate from the orchestrator's
turn-lock so it never deadlocks against a blocking in-turn wave) and does a full
read-modify-write. (write_status no longer takes a `_mirror` kwarg; high-frequency mid-wave events
do not spam the wiki vault git; the orchestrator's boundary writes keep mirroring.
"""
from __future__ import annotations

import argparse
from collections.abc import Iterator
import contextlib
import json
from pathlib import Path
import sys
import time
from typing import get_args, get_type_hints

import yaml

from agentflow import lock_file
from agentflow.status_writer import (
    Finding,
    StatusState,
    VerifierVerdict,
    append_decision,
    read_status,
    write_status,
)

# Valid TASK lifecycle states (mirrors status_writer.TaskStateEnum). Kept as a
# runtime set so we can reject session statuses ("running", "escalation", ...)
# that must never be applied to a task by the reporter.
TASK_STATES = frozenset(
    {"pending", "ready", "in_flight", "spec_review", "code_review", "done", "blocked"}
)

# Derived from status_writer's types, never restated, so the reporter cannot accept a value
# the board's own schema does not name.
VERIFIER_VERDICTS = frozenset(get_args(VerifierVerdict))
FINDING_SEVERITIES = frozenset(get_args(get_type_hints(Finding)["severity"]))


def _validate_verdict(verdict: str) -> None:
    if verdict not in VERIFIER_VERDICTS:
        raise ValueError(
            f"invalid verifier verdict {verdict!r}; must be one of {sorted(VERIFIER_VERDICTS)}"
        )


def _validate_findings(findings: object) -> None:
    """A findings count is a severity -> non-negative int mapping.

    Keys outside the severity vocabulary are refused; an absent severity is allowed, because
    every reader of `verifier_findings_count` treats a missing key as zero.
    """
    if not isinstance(findings, dict):
        raise ValueError(
            f"findings must be a JSON object keyed by {sorted(FINDING_SEVERITIES)}, "
            f"got {type(findings).__name__}"
        )
    unknown = sorted(str(k) for k in findings if k not in FINDING_SEVERITIES)
    if unknown:
        raise ValueError(
            f"unknown findings severity {unknown}; allowed: {sorted(FINDING_SEVERITIES)}"
        )
    for key, count in findings.items():
        # bool is an int subclass; `true` is not a count.
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"findings[{key!r}] must be a non-negative integer, got {count!r}")


def _require_board(status_path: Path) -> None:
    """Refuse a missing board before the lock is taken, so no stray lock file is left beside it."""
    if not status_path.is_file():
        raise ValueError(f"status board not found: {status_path}")


def _read_board(status_path: Path) -> StatusState:
    """read_status, with every way a board can be unreadable reported as a ValueError."""
    try:
        return read_status(status_path)
    except FileNotFoundError:
        raise ValueError(f"status board not found: {status_path}") from None
    except (OSError, yaml.YAMLError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(
            f"status board unreadable: {status_path} ({type(exc).__name__}: {exc})"
        ) from exc
    except ValueError as exc:
        raise ValueError(f"status board unreadable: {status_path} ({exc})") from exc


class _LockOutcome:
    """Whether the wave-event lock was released, read by the caller after the `with` exits."""

    released: bool = True


@contextlib.contextmanager
def _wave_lock(
    status_path: Path, *, timeout: float = 15.0, poll: float = 0.1
) -> Iterator[_LockOutcome]:
    """Short blocking lock around a status.md read-modify-write.

    Reuses lock_file.acquire (O_EXCL + dead-PID steal) but retries on
    LockHeldError so concurrent reporters serialize instead of failing. A
    PermissionError is retried too: on Windows a just-released lock that a peer
    still has open sits delete-pending, and opening it fails until that peer
    closes it.

    Release never raises (lock_file says why on stderr). The yielded outcome
    records an unclean release - the lock leaked, or was no longer ours - so the
    CLI can say the write landed, rather than calling a landed write a failure.
    """
    lock_path = Path(str(status_path) + ".wave-event.lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = lock_file.acquire(lock_path)
            break
        except lock_file.LockHeldError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"wave-event lock busy >{timeout}s at {lock_path}"
                ) from None
            time.sleep(poll)
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"wave-event lock unopenable >{timeout}s at {lock_path} ({exc})"
                ) from exc
            time.sleep(poll)
    outcome = _LockOutcome()
    try:
        yield outcome
    finally:
        outcome.released = handle.release()


def report_note(
    status_path: str | Path,
    task: int = 0,
    tier: int = 2,
    summary: str = "",
) -> bool:
    """Append a decision-log note. task=0 is a wave-level note.

    Returns False when the note landed but the lock release was unclean.
    """
    if not summary:
        raise ValueError("summary is required and must be non-empty")
    status_path = Path(status_path)
    _require_board(status_path)
    with _wave_lock(status_path) as lock:
        state = _read_board(status_path)
        append_decision(state, task=task, tier=tier, summary=summary)
        write_status(status_path, state)
    return lock.released


def report_task_state(
    status_path: str | Path,
    task: int,
    state: str,
    *,
    verifier_verdict: str | None = None,
    findings: dict[str, int] | None = None,
    commit: str | None = None,
    model: str | None = None,
    role: str | None = None,
    verifier_iterations: int | None = None,
) -> bool:
    """Set an existing task's state (+ optional verdict fields). Never touches session status.

    Returns False when the write landed but the lock release was unclean.
    """
    if state not in TASK_STATES:
        raise ValueError(
            f"invalid task state {state!r}; must be one of {sorted(TASK_STATES)}"
        )
    if verifier_verdict is not None:
        _validate_verdict(verifier_verdict)
    if findings is not None:
        _validate_findings(findings)
    status_path = Path(status_path)
    _require_board(status_path)
    with _wave_lock(status_path) as lock:
        st = _read_board(status_path)
        if task not in st.tasks:
            raise ValueError(f"unknown task {task}; not present in {status_path}")
        ts = st.tasks[task]
        ts.state = state
        if verifier_verdict is not None:
            ts.verifier_verdict = verifier_verdict
        if findings is not None:
            ts.verifier_findings_count = findings
        if commit is not None:
            ts.commit = commit
        if model is not None:
            ts.model = model
        if role is not None:
            ts.role = role
        if verifier_iterations is not None:
            ts.verifier_iterations = verifier_iterations
        write_status(status_path, st)
    return lock.released


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m agentflow.wave_event",
        description="Report wave progress into status.md (task-level only; never session status).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pn = sub.add_parser("note", help="append a decision-log note")
    pn.add_argument("--status", required=True)
    pn.add_argument("--task", type=int, default=0)
    pn.add_argument("--tier", type=int, default=2)
    pn.add_argument("--summary", required=True)

    pt = sub.add_parser("task-state", help="set an existing task's state + verdict")
    pt.add_argument("--status", required=True)
    pt.add_argument("--task", type=int, required=True)
    pt.add_argument("--state", required=True)  # validated in report_task_state, not by argparse
    pt.add_argument("--verifier-verdict", dest="verifier_verdict")
    pt.add_argument("--findings", help='JSON dict, e.g. {"low":1,"medium":0,"blocker":0}')
    pt.add_argument("--commit")
    pt.add_argument("--model")
    pt.add_argument("--role")
    pt.add_argument("--verifier-iterations", dest="verifier_iterations", type=int)
    return p


def main(argv: list[str] | None = None) -> int:
    """Exit 0 when the write landed, 2 when it was refused or failed.

    A landed write whose lock release was unclean (leaked, or the lock was no
    longer ours) still exits 0, with a `wave-event warning:` line naming the
    lock: callers re-issue any write reported as failed, so a non-zero exit
    here would duplicate it.
    """
    # First, before any output: a legacy cp1252 stdout cannot carry a non-cp1252
    # character a lane wrote (gh#574).
    from agentflow._stdio import utf8_stdio

    utf8_stdio()
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "note":
            released = report_note(args.status, task=args.task, tier=args.tier, summary=args.summary)
        elif args.cmd == "task-state":
            findings = json.loads(args.findings) if args.findings else None
            released = report_task_state(
                args.status,
                task=args.task,
                state=args.state,
                verifier_verdict=args.verifier_verdict,
                findings=findings,
                commit=args.commit,
                model=args.model,
                role=args.role,
                verifier_iterations=args.verifier_iterations,
            )
    except (ValueError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"wave-event error: {exc}", file=sys.stderr)
        return 2
    if not released:
        print(
            "wave-event warning: write landed; lock release failed (leaked, or no "
            f"longer ours) for {args.status}.wave-event.lock",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
