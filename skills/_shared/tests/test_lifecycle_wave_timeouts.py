"""Wave-timeout contract between `agentflow.lifecycle` and the orchestrator SKILL (gh#675).

Step 4.0 of `skills/orchestrator/SKILL.md` is a prose program that calls
`check_wave_timeouts` and reads attributes off each `TimeoutEvent`. The two drifted: the
SKILL read `event.summary` and `event.dispatch_record_path`, which the event never had, and
said "after the helper increments" of a helper documented as pure, so nothing incremented
`timeout_count` and the requeue -> block ladder never escalated. Nothing called the helper
in a test, so none of that could fail.

These tests pin the three halves: the status.md round trip the SKILL actually runs, the
attribute and keyword contract parsed out of the SKILL text itself, and the ladder under
the caller-increments protocol.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from agentflow.lifecycle import TimeoutEvent, check_wave_timeouts
from agentflow.plan_parser import PlanFrontmatter, PlanVerificationGlobals, Task, parse_plan
from agentflow.status_writer import (
    StatusState,
    TaskState,
    append_decision,
    read_status,
    write_status,
)

SKILLS_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _state(tasks: dict[int, TaskState]) -> StatusState:
    # project is set so write_status does not go looking for a plan file to backfill it.
    return StatusState(
        epic="Test Epic",
        plan="plan.md",
        started="2026-09-01T00:00:00Z",
        status="running",
        current_phase="p1",
        project="demo",
        tasks=tasks,
    )


def _plan(task_deadline: int | None = None, plan_deadline: int | None = None) -> PlanFrontmatter:
    return PlanFrontmatter(
        epic="Test Epic",
        spec="spec.md",
        phases=["p1"],
        tasks={
            1: Task(id=1, phase="p1", deps=[], wave_deadline_minutes=task_deadline),
            2: Task(id=2, phase="p1", deps=[]),
        },
        test_commands={},
        verification=PlanVerificationGlobals(wave_deadline_minutes=plan_deadline),
    )


def _round_trip(tmp_path: Path, state: StatusState) -> StatusState:
    path = tmp_path / "status.md"
    write_status(path, state)
    return read_status(path)


def _step_4_0() -> str:
    text = (SKILLS_ROOT / "orchestrator" / "SKILL.md").read_text(encoding="utf-8")
    start = text.index("**Sub-step 4.0")
    end = text.index("This sub-step MUST run", start)
    return text[start:end]


def _apply(state: StatusState, event: TimeoutEvent) -> None:
    """Step 4.0's caller protocol, transcribed: log, increment, then act."""
    task = state.tasks[event.task_id]
    append_decision(
        state,
        task=event.task_id,
        tier=2,
        summary=event.summary,
        evidence_path=f"status.md tasks.{event.task_id}.since={event.dispatched_at}",
    )
    task.timeout_count += 1
    if event.suggested_action == "requeue":
        assert task.timeout_count == 1
        task.state = "ready"
        task.since = None
    else:
        assert task.timeout_count >= 2
        task.state = "blocked"


# --------------------------------------------------------------------------- #
# The persisted board the SKILL reads yields a complete event.
# --------------------------------------------------------------------------- #


def test_overdue_task_survives_the_status_round_trip_and_yields_a_full_event(tmp_path) -> None:
    dispatched = _iso(NOW - timedelta(hours=5))
    state = _round_trip(
        tmp_path,
        _state({
            1: TaskState(state="in_flight", since=dispatched),
            2: TaskState(state="in_flight", since=_iso(NOW - timedelta(minutes=5))),
        }),
    )

    events = check_wave_timeouts(state, _plan(), NOW)

    assert [e.task_id for e in events] == [1]
    event = events[0]
    assert event.dispatched_at == dispatched
    assert event.elapsed_minutes == pytest.approx(300.0)
    assert event.deadline_minutes == 30
    assert event.suggested_action == "requeue"
    assert "task 1" in event.summary and dispatched in event.summary
    for attr in _event_attrs_read_by_skill():
        assert getattr(event, attr) not in (None, ""), attr


def test_the_helper_is_pure(tmp_path) -> None:
    state = _round_trip(
        tmp_path, _state({1: TaskState(state="in_flight", since=_iso(NOW - timedelta(hours=5)))})
    )
    before = dataclasses.asdict(state)

    check_wave_timeouts(state, _plan(), NOW)

    assert dataclasses.asdict(state) == before


def test_an_unquoted_hand_edited_since_loads_as_datetime_and_still_times_out(tmp_path) -> None:
    path = tmp_path / "status.md"
    write_status(
        path, _state({1: TaskState(state="in_flight", since=_iso(NOW - timedelta(hours=5)))})
    )
    # A human editing the frontmatter drops the quotes; YAML then yields a datetime.
    text = path.read_text(encoding="utf-8")
    edited = re.sub(r"since: '([^']+)'", r"since: \1", text)
    assert edited != text
    path.write_text(edited, encoding="utf-8")

    state = read_status(path)
    assert isinstance(state.tasks[1].since, datetime)

    (event,) = check_wave_timeouts(state, _plan(), NOW)
    assert isinstance(event.dispatched_at, str)
    assert datetime.fromisoformat(event.dispatched_at) == NOW - timedelta(hours=5)
    assert event.elapsed_minutes == pytest.approx(300.0)


def test_an_offset_since_is_normalized_to_utc() -> None:
    offset = (NOW - timedelta(hours=5)).astimezone(timezone(timedelta(hours=-4)))
    state = _state({1: TaskState(state="in_flight", since=offset.isoformat())})

    (event,) = check_wave_timeouts(state, _plan(), NOW)
    assert event.elapsed_minutes == pytest.approx(300.0)


@pytest.mark.parametrize("garbage", ["not a timestamp", 12345])
def test_an_unparseable_since_is_skipped_with_a_warning(garbage, caplog) -> None:
    state = _state({1: TaskState(state="in_flight", since=garbage)})

    with caplog.at_level("WARNING", logger="agentflow.lifecycle"):
        assert check_wave_timeouts(state, _plan(), NOW) == []
    assert "unparseable since" in caplog.text


def test_the_templates_per_task_override_is_what_the_parser_reads(tmp_path) -> None:
    """The plan template once nested the override under `verification:`, where it was dropped."""
    template = (SKILLS_ROOT / "orchestrator" / "templates" / "plan-frontmatter.yaml").read_text(
        encoding="utf-8"
    )
    lines = template.splitlines()
    start = lines.index("# Example task with verification block:") + 1
    example = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        example.append(line[2:] if line.startswith("# ") else line[1:])
    tasks = yaml.safe_load("\n".join(example))["tasks"]
    (tid,) = tasks
    tasks[int(tid)] = tasks.pop(tid)
    for dep in tasks[int(tid)]["deps"]:
        tasks[dep] = {"phase": tasks[int(tid)]["phase"], "deps": []}

    plan_path = tmp_path / "plan.md"
    frontmatter = {
        "epic": "Template Epic",
        "spec": "spec.md",
        "phases": [tasks[int(tid)]["phase"]],
        "tasks": tasks,
        "test_commands": {},
    }
    plan_path.write_text(f"---\n{yaml.safe_dump(frontmatter)}---\n", encoding="utf-8")

    plan = parse_plan(plan_path)
    task = plan.tasks[int(tid)]
    assert task.wave_deadline_minutes == 45
    assert task.verification is not None

    since = _iso(NOW - timedelta(minutes=46))
    (event,) = check_wave_timeouts(
        _state({int(tid): TaskState(state="in_flight", since=since)}), plan, NOW
    )
    assert event.deadline_minutes == 45


@pytest.mark.parametrize(
    ("task_deadline", "plan_deadline", "expected"),
    [(None, None, 30), (None, 90, 90), (45, 90, 45)],
)
def test_deadline_resolution_order(task_deadline, plan_deadline, expected) -> None:
    since = _iso(NOW - timedelta(minutes=expected + 1))
    state = _state({1: TaskState(state="in_flight", since=since)})

    (event,) = check_wave_timeouts(state, _plan(task_deadline, plan_deadline), NOW)

    assert event.deadline_minutes == expected
    # One minute inside the same deadline is not a timeout.
    state.tasks[1].since = _iso(NOW - timedelta(minutes=expected - 1))
    assert check_wave_timeouts(state, _plan(task_deadline, plan_deadline), NOW) == []


# --------------------------------------------------------------------------- #
# The SKILL text and the code agree, read from the SKILL itself.
# --------------------------------------------------------------------------- #


def _event_attrs_read_by_skill() -> set[str]:
    return set(re.findall(r"\bevent\.(\w+)", _step_4_0()))


def test_every_event_attribute_step_4_0_reads_exists_on_timeout_event() -> None:
    attrs = _event_attrs_read_by_skill()
    fields = {f.name for f in dataclasses.fields(TimeoutEvent)}

    # Guard the guard: an empty parse would pass the subset check vacuously.
    assert {"task_id", "summary", "suggested_action"} <= attrs
    assert attrs <= fields, f"SKILL reads attributes TimeoutEvent lacks: {sorted(attrs - fields)}"


def test_step_4_0_append_decision_call_binds_to_the_real_signature() -> None:
    calls = re.findall(r"`(append_decision\(.*?\))`", _step_4_0())
    assert len(calls) == 1, calls

    call = ast.parse(calls[0], mode="eval").body
    assert isinstance(call, ast.Call)
    kwargs = {kw.arg: None for kw in call.keywords}
    # Raises TypeError on any keyword append_decision does not accept.
    inspect.signature(append_decision).bind(*[None] * len(call.args), **kwargs)


def test_every_task_field_step_4_0_touches_exists_on_task_state() -> None:
    """gh#675's bug class: prose naming a TaskState field the dataclass does not have."""
    attrs = set(re.findall(r"state\.tasks\[tid\]\.(\w+)", _step_4_0()))
    fields = {f.name for f in dataclasses.fields(TaskState)}

    assert {"since", "timeout_count", "state"} <= attrs
    assert attrs <= fields, f"SKILL touches fields TaskState lacks: {sorted(attrs - fields)}"


_INCREMENT = "timeout_count += 1"
# Words that turn an increment bullet into a prohibition or a description of someone else's job.
_NEGATION = re.compile(r"\b(not|never|don't|do not|without|helper)\b", re.IGNORECASE)


def _increment_precedes_the_actions(section: str) -> bool:
    """True when an instruction bullet orders the increment, ahead of both action bullets."""
    lines = section.splitlines()
    bullets = [(i, line) for i, line in enumerate(lines) if re.match(r"\s*- ", line)]
    increments = [
        i for i, line in bullets
        if _INCREMENT in line and not _NEGATION.search(line.split(_INCREMENT, 1)[0])
    ]
    requeue = [i for i, line in bullets if re.match(r"\s*- `requeue`", line)]
    block = [i for i, line in bullets if re.match(r"\s*- `block`", line)]
    if not (increments and requeue and block):
        return False
    return min(increments) < min(requeue) and min(increments) < min(block)


def test_step_4_0_puts_the_increment_on_the_caller() -> None:
    section = _step_4_0()

    assert _increment_precedes_the_actions(section)
    assert "after the helper increments" not in section
    assert "does NOT mutate state" in inspect.getdoc(check_wave_timeouts)


@pytest.mark.parametrize(
    "prose",
    [
        # Negated instruction.
        "- Do NOT do `timeout_count += 1` here.\n  - `requeue`: reset.\n  - `block`: block.\n",
        # Increment ordered after the actions it gates.
        "  - `requeue`: reset.\n  - `block`: block.\n- Then `timeout_count += 1`.\n",
        # Mentioned in running prose, never as a bullet.
        "The caller does timeout_count += 1.\n  - `requeue`: reset.\n  - `block`: block.\n",
        # The old wording: the helper's job, not the caller's.
        "- The helper does `timeout_count += 1`.\n  - `requeue`: reset.\n  - `block`: block.\n",
    ],
)
def test_the_increment_check_rejects_prose_that_does_not_order_it(prose: str) -> None:
    assert not _increment_precedes_the_actions(prose)


# --------------------------------------------------------------------------- #
# The ladder escalates when the caller increments.
# --------------------------------------------------------------------------- #


def test_the_ladder_requeues_then_blocks_across_persisted_wakes(tmp_path) -> None:
    plan = _plan()

    # Wake 1: first timeout -> requeue, count 1.
    state = _round_trip(
        tmp_path, _state({1: TaskState(state="in_flight", since=_iso(NOW - timedelta(hours=1)))})
    )
    (event,) = check_wave_timeouts(state, plan, NOW)
    assert event.suggested_action == "requeue"
    _apply(state, event)
    state = _round_trip(tmp_path, state)
    assert (state.tasks[1].state, state.tasks[1].timeout_count, state.tasks[1].since) == (
        "ready", 1, None,
    )
    assert state.decisions[-1].evidence_path == (
        f"status.md tasks.1.since={event.dispatched_at}"
    )

    # Step 7 re-dispatches; it wedges again.
    state.tasks[1].state = "in_flight"
    state.tasks[1].since = _iso(NOW)
    state = _round_trip(tmp_path, state)

    # Wake 2: second timeout -> block, count 2.
    later = NOW + timedelta(hours=1)
    (event,) = check_wave_timeouts(state, plan, later)
    assert event.suggested_action == "block"
    _apply(state, event)
    state = _round_trip(tmp_path, state)
    assert (state.tasks[1].state, state.tasks[1].timeout_count) == ("blocked", 2)

    # Blocked is terminal for the helper: no further events.
    assert check_wave_timeouts(state, plan, later + timedelta(hours=5)) == []

    # A human reset per the SKILL (ready + timeout_count 0) starts a fresh ladder.
    state.tasks[1].state = "ready"
    state.tasks[1].timeout_count = 0
    state = _round_trip(tmp_path, state)
    state.tasks[1].state = "in_flight"
    state.tasks[1].since = _iso(later)
    (event,) = check_wave_timeouts(state, plan, later + timedelta(hours=1))
    assert event.suggested_action == "requeue"


def test_without_the_increment_the_ladder_never_blocks() -> None:
    """The gh#675 failure, kept as a control: a caller that skips the increment requeues forever."""
    state = _state({1: TaskState(state="in_flight", since=_iso(NOW - timedelta(hours=1)))})

    for _ in range(3):
        (event,) = check_wave_timeouts(state, _plan(), NOW)
        assert event.suggested_action == "requeue"
