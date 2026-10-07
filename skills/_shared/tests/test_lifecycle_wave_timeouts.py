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
# A requeue stops the group's wave run before it releases the tree (gh#1054).
# --------------------------------------------------------------------------- #

# Each constant is the phrase that IS the step, not a noun the rule shares with its neighbours.
_RULE_HEADING = "**Stopping a wave run before a requeue (the Step 4.0 timeout `requeue`"
_REFERENCE = "apply **stopping a wave run before a requeue** (step 1.2.5)"
_DISPATCH_NOTE = "`wave <n> dispatched: tasks [...] run <runId>`"
_OWNER = "A wave run belongs to the whole wave group"
_PARSE = "it is the token after the final ` run ` in the summary (`run (\\S+)$`)"
_DEFER = "do not stop the run: defer"
_DEFER_KEEPS = "Keep the task's tree and its fields and leave the task as it is, not `ready`"
_GROUP = (
    "When every task the note lists that is still `in_flight` is being requeued in this same pass"
)
_STOP = "call `TaskStop` on the run id"
_NOT_FOUND = 'fails with "No task found"'
_RELEASE = "release the task's wave tree and clear its fields"
_TO_READY = "reset the task to `ready`"
_KEEP_BRANCH = "If no run id is readable, or the stop fails with any other error"
_KEEP = "keep the tree and its fields"
_NOT_READY = "do not reset the task to `ready`"
_ESCALATE = "open a tier-3 escalation"
_HOLDS = "The open escalation holds the task until it is answered"
_NO_SINCE = "a blocking sibling with no readable dispatch time"
_NO_SINCE_ESCALATES = "Do not defer then; take the last case below and open its tier-3 escalation"
_KEEPS_COUNT = "Unlike a defer, this case keeps any increment Step 4.0 made this pass"
_DEFERS = "When it defers"
_TAKE_BACK = "`state.tasks[tid].timeout_count -= 1`"


def _skill_text() -> str:
    return (SKILLS_ROOT / "orchestrator" / "SKILL.md").read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _stop_rule() -> str:
    # The rule's own heading, not the first mention of its name (a forward reference precedes it).
    text = _skill_text()
    assert text.count(_RULE_HEADING) == 1
    start = text.index(_RULE_HEADING)
    return _flat(text[start:text.index("### Step 2:", start)])


def _bullet(text: str, opener: str) -> str:
    (line,) = [line for line in text.splitlines() if line.lstrip().startswith(opener)]
    return line


def _requeue_bullets() -> dict[str, str]:
    return {
        "step 4.0": _bullet(_step_4_0(), "- `requeue`"),
        "reconcile": _bullet(_skill_text(), '- For `actions[tid] == "requeue"`'),
    }


def _rule_is_sound(rule: str) -> bool:
    """Parse the note; defer while a sibling still builds; stop only when the whole group is
    requeued; release on success or "No task found"; otherwise keep, stay off `ready`, escalate."""
    phrases = (_OWNER, _DISPATCH_NOTE, _PARSE, _DEFER, _DEFER_KEEPS, _NO_SINCE,
               _NO_SINCE_ESCALATES, _GROUP, _STOP, _NOT_FOUND, _RELEASE, _KEEP_BRANCH, _KEEP,
               _NOT_READY, _ESCALATE, _HOLDS, _KEEPS_COUNT)
    if not all(p in rule for p in phrases):
        return False
    note, parse, defer, group, stop, not_found, release, keep_branch = (
        rule.index(p) for p in
        (_DISPATCH_NOTE, _PARSE, _DEFER, _GROUP, _STOP, _NOT_FOUND, _RELEASE, _KEEP_BRANCH)
    )
    deferred = rule[defer:group]
    released = rule[release:keep_branch]
    kept = rule[keep_branch:]
    return (
        note < parse < defer < group < stop < not_found < release < keep_branch
        and _DEFER_KEEPS in deferred
        # A sibling that can never time out must escalate, or the defer loops silently.
        and deferred.index(_DEFER) < deferred.find(_NO_SINCE) < deferred.find(_NO_SINCE_ESCALATES)
        and "TaskStop" not in deferred
        and _RELEASE not in deferred
        and _TO_READY in released
        and kept.index(_KEEP) < kept.index(_NOT_READY) < kept.index(_ESCALATE) < kept.index(_HOLDS)
        and kept.index(_ESCALATE) < kept.index(_KEEPS_COUNT)
        and _RELEASE not in kept
        and _TO_READY not in kept.replace(_NOT_READY, "")
    )


def test_the_stop_rule_stops_the_group_run_before_any_release() -> None:
    assert _rule_is_sound(_stop_rule())


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r.replace(_GROUP, "Then"), id="no all-group condition"),
        pytest.param(lambda r: r.replace(_DEFER, "stop the run anyway"), id="no defer branch"),
        pytest.param(lambda r: r.replace(_PARSE, "it is in the summary"), id="no parse rule"),
        pytest.param(
            lambda r: r.replace(_STOP, "do the release step").replace(
                _OWNER, f"{_OWNER}. First {_STOP}"
            ),
            id="release before the stop",
        ),
        pytest.param(
            lambda r: r.replace(_ESCALATE, f"{_ESCALATE}, then {_TO_READY}"),
            id="keep branch resets to ready",
        ),
        pytest.param(lambda r: r.replace(_NOT_READY, "leave the task"), id="keep branch not off ready"),
        pytest.param(lambda r: r.replace(_HOLDS, "It is noted"), id="escalation does not hold"),
        pytest.param(
            lambda r: r.replace(_NO_SINCE_ESCALATES, "Defer anyway."),
            id="sibling with no dispatch time defers forever",
        ),
        pytest.param(lambda r: r.replace(_KEEPS_COUNT, "It is noted"), id="keep branch count unstated"),
    ],
)
def test_the_stop_rule_check_rejects_a_broken_rule(mutate) -> None:
    rule = _stop_rule()
    broken = mutate(rule)
    assert broken != rule
    assert not _rule_is_sound(broken)


def _bullet_applies_the_rule(bullet: str, *, takes_back: bool) -> bool:
    """The bullet names the rule before any release, does not restate it, and (Step 4.0 only)
    takes back the increment when the rule defers."""
    lower = bullet.lower()
    if _REFERENCE not in lower or "releas" not in lower:
        return False
    if lower.index(_REFERENCE) > lower.index("releas"):
        return False
    # A bullet that inlines the rule drifts from it; the rule is stated once.
    if any(r in bullet for r in ("TaskStop", "No task found", "run (\\S+)", _ESCALATE)):
        return False
    if takes_back:
        return -1 < bullet.find(_DEFERS) < bullet.find(_TAKE_BACK)
    return True


@pytest.mark.parametrize("where", ["step 4.0", "reconcile"])
def test_each_requeue_applies_the_stop_rule_by_name_and_does_not_restate_it(where: str) -> None:
    assert _bullet_applies_the_rule(_requeue_bullets()[where], takes_back=where == "step 4.0")


@pytest.mark.parametrize(
    "where,mutate",
    [
        pytest.param(
            "step 4.0",
            lambda b: b[:b.index(" " + _DEFERS)] + b[b.index("the ladder has not moved.") + 25:],
            id="step 4.0 drops the take-back",
        ),
        pytest.param(
            "step 4.0",
            lambda b: b.replace("- `requeue` (", "- `requeue` (release its wave tree "
                                "(**Releasing a wave tree**) first; "),
            id="step 4.0 releases before the rule",
        ),
        pytest.param(
            "reconcile",
            lambda b: b.replace("Apply **Stopping", "Release its wave tree and clear its fields "
                                "(**Releasing a wave tree**), then apply **Stopping"),
            id="reconcile releases before the rule",
        ),
    ],
)
def test_the_bullet_check_rejects_a_broken_bullet(where: str, mutate) -> None:
    bullet = _requeue_bullets()[where]
    broken = mutate(bullet)
    assert broken != bullet
    assert not _bullet_applies_the_rule(broken, takes_back=where == "step 4.0")


def test_a_deferred_requeue_that_takes_back_its_increment_still_requeues_next_time() -> None:
    """Step 4.0's defer: increment, then take back. The next pass must see a first timeout again,
    or a group that outlasts one sibling's deadline would block that sibling on its next wake."""
    state = _state({1: TaskState(state="in_flight", since=_iso(NOW - timedelta(hours=1)))})

    (event,) = check_wave_timeouts(state, _plan(), NOW)
    assert event.suggested_action == "requeue"
    state.tasks[1].timeout_count += 1
    state.tasks[1].timeout_count -= 1  # the rule deferred: nothing was requeued

    (event,) = check_wave_timeouts(state, _plan(), NOW + timedelta(minutes=10))
    assert event.suggested_action == "requeue"
    assert state.tasks[1].timeout_count == 0


def test_the_dispatch_note_records_the_run_id_after_the_launch() -> None:
    text = _skill_text()
    sub3 = text[text.index("   3. Mark each group task"):text.index("   4. **Fire-and-reconcile.**")]
    record = sub3.index("Record the returned `runId` in the dispatch decision note")
    assert sub3.index("call the **Workflow tool**") < record

    (call,) = re.findall(r"`(append_decision\(.*?\))`", sub3[record:])
    parsed = ast.parse(call, mode="eval").body
    inspect.signature(append_decision).bind(
        *[None] * len(parsed.args), **{kw.arg: None for kw in parsed.keywords}
    )
    summary = next(kw.value for kw in parsed.keywords if kw.arg == "summary")
    assert ast.unparse(summary) == "f'wave {n} dispatched: tasks [{ids}] run {runId}'"

    # One format: the old entry and the field TaskState never had are both gone.
    assert "Wave N dispatch:" not in text
    assert "wave_runid" not in text


# --------------------------------------------------------------------------- #
# The `block` rung and every reset of a blocked task stop the run first (gh#1144).
# --------------------------------------------------------------------------- #

_RULE_NAME = "**Stopping a wave run before a requeue**"


_RESET = "A human who resets this task"
_DEFERRED_NOTE = "at most one tier-1 `deferred: sibling <ids> still in flight` note per dispatch"
_PLACES = ["block bullet", "block reset", "reopen as running", "deadlock reset"]


def _block_places(text: str | None = None) -> dict[str, str]:
    text = _skill_text() if text is None else text
    step = text[text.index("**Sub-step 4.0"):text.index("This sub-step MUST run")]
    block = _flat(_bullet(step, "- `block`"))
    reopen = text[text.index("  - **Reopen as running**"):text.index("  - **Abandon**")]
    deadlock = text[text.index("Deadlock check: if no ready tasks"):text.index("### Step 7:")]
    return {
        "block bullet": block,
        "block reset": block[block.index(_RESET):],
        "reopen as running": _flat(reopen),
        "deadlock reset": _flat(deadlock),
    }


def _names_the_rule_before_any_release(place: str) -> bool:
    # The rule's bold name, not a bare "stop rule", which "no stop rule applies" would satisfy.
    return -1 < place.find(_RULE_NAME) < place.lower().find("releas")


@pytest.mark.parametrize("where", _PLACES)
def test_a_blocked_task_keeps_its_tree_until_the_stop_rule_runs(where: str) -> None:
    assert _names_the_rule_before_any_release(_block_places()[where])


# Each inserts a release inside its own place, after the rule's mentions earlier in the file and,
# for the reset, after the bullet's own reference: only a per-place check catches it.
_EARLY_RELEASE = {
    "block bullet": ("  - `block` (", "  - `block` (release its wave tree first; "),
    "block reset": (_RESET, f"{_RESET} releases its wave tree first, and"),
    "reopen as running": ("  - **Reopen as running** →", "  - **Reopen as running** → release each wave tree;"),
    "deadlock reset": ("Deadlock check: if no ready tasks",
                       "Deadlock check: if no ready tasks, release every blocked tree, and"),
}


@pytest.mark.parametrize("where", _PLACES)
def test_the_block_check_rejects_a_release_before_the_rule(where: str) -> None:
    text = _skill_text()
    old, new = _EARLY_RELEASE[where]
    assert text.count(old) == 1
    broken = _block_places(text.replace(old, new))
    assert not _names_the_rule_before_any_release(broken[where])
    if where == "block reset":
        # The whole bullet still names the rule first; only the reset's own slice is broken.
        assert _names_the_rule_before_any_release(broken["block bullet"])


def test_the_block_rung_stops_the_run_before_it_marks_the_task_blocked() -> None:
    bullet = _block_places()["block bullet"]
    assert _bullet_applies_the_rule(bullet, takes_back=True)
    assert bullet.lower().index(_REFERENCE) < bullet.index('`state.tasks[tid].state = "blocked"`')
    # A deferring block records the defer once, as the requeue rung does (gh#1144 review M1).
    assert -1 < bullet.find(_DEFERS) < bullet.find(_DEFERRED_NOTE)
    assert _DEFERRED_NOTE in _requeue_bullets()["step 4.0"]
    # The rule names the rung it now governs, and a sibling's block counts as a requeue for both
    # sides of the defer, or a requeue and a block in one group defer each other forever (S1).
    rule = _stop_rule()
    assert "the Step 4.0 timeout `requeue` and `block` rungs" in rule
    assert 'Throughout, "requeued" includes a Step 4.0 `block` in the same pass' in rule
    assert "For the `block` rung" not in rule
    assert _rule_is_sound(rule)


def test_a_deferring_block_skips_the_per_pass_tier_2_entry() -> None:
    (tier2,) = [line for line in _step_4_0().splitlines() if line.startswith("- Append a tier-2 decision")]
    assert "A pass whose `requeue` or `block` defers (below) skips this entry." in tier2


def test_the_release_gate_covers_a_reset_of_a_ladder_blocked_task() -> None:
    release = _flat(_skill_text())
    assert ("A requeue, or a human reset of a task the timeout ladder blocked, releases a tree only "
            "after **Stopping a wave run before a requeue** (below) has stopped the run") in release


def test_the_block_reset_names_who_confirms_the_run_is_over() -> None:
    # No orchestrator step notices a hand reset, so the text must put the check on the resetter.
    reset = _block_places()["block reset"]
    assert "No step notices a reset, so whoever makes it first confirms the run is over" in reset


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
