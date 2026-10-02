"""Tests for the wave-dispatch seam: contract-prompt wiring and the
bootstrap return contract (post prompt_path retirement)."""

import re
from pathlib import Path

import pytest

from agentflow.bootstrap_dispatch import (
    BootstrapMalformed,
    parse_bootstrap_return,
)
from agentflow import wave_event
from agentflow.plan_parser import PlanFrontmatter, Task
from agentflow.status_writer import Decision, StatusState, TaskState, read_status, write_status
from agentflow.wave_dispatch import (
    UNKNOWN_WAVE_OUTCOME,
    WAVE_NOTE_OUTCOMES,
    WAVE_OUTCOME_NOTE_PREFIX,
    _task_body,
    bake_wave_script,
    build_wave_args,
    reconcile_group_from_status_git,
    remove_wave_script,
    reported_wave_outcome,
    wave_script_dir,
)


def _plan(task_type: str = "standard") -> PlanFrontmatter:
    return PlanFrontmatter(
        epic="Test Epic",
        spec="spec.md",
        phases=["p1"],
        tasks={1: Task(id=1, phase="p1", deps=[], task_type=task_type)},
        test_commands={"regression": "pytest -q"},
        slug="test-epic",
    )


def _args(plan: PlanFrontmatter, tmp_path: Path, **kw) -> dict:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("## Task 1: do it\nbody\n", encoding="utf-8")
    return build_wave_args(
        plan=plan,
        plan_path=plan_path,
        group_task_ids=[1],
        status_path=tmp_path / "status.md",
        wave_n=1,
        base_branch="main",
        repo_path=tmp_path,
        ntfy_url=None,
        ntfy_topic=None,
        **kw,
    )


# ---------------------------------------------------------------------------
# contract_path wiring — templates are the governing wave contract
# ---------------------------------------------------------------------------

def test_contract_path_standard_task(tmp_path):
    args = _args(_plan("standard"), tmp_path, skill_dir=Path("/suite/skills/orchestrator"))
    assert args["tasks"][0]["contract_path"] == (
        "/suite/skills/orchestrator/prompts/implementer-with-verifier-loop.md"
    )


def test_contract_path_tdd_task(tmp_path):
    args = _args(_plan("tdd"), tmp_path, skill_dir=Path("/suite/skills/orchestrator"))
    assert args["tasks"][0]["contract_path"] == (
        "/suite/skills/orchestrator/prompts/implementer-with-tdd-loop.md"
    )


def test_contract_path_none_without_skill_dir(tmp_path):
    # Legacy caller: no skill_dir ⇒ contract line omitted, not a broken path.
    args = _args(_plan(), tmp_path)
    assert args["tasks"][0]["contract_path"] is None


def test_contract_templates_exist_in_repo():
    # The wired templates must actually exist — no ghost references.
    prompts = (
        Path(__file__).resolve().parents[2] / "orchestrator" / "prompts"
    )
    assert (prompts / "implementer-with-verifier-loop.md").is_file()
    assert (prompts / "implementer-with-tdd-loop.md").is_file()


def test_workflow_prompt_consumes_contract_path():
    # wave_group.js must reference t.contract_path in its implementer prompt —
    # this is the consumption edge that makes the templates real.
    wf = (
        Path(__file__).resolve().parents[2]
        / "orchestrator" / "workflows" / "wave_group.js"
    )
    assert "contract_path" in wf.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# bootstrap return contract — prompt_path retired
# ---------------------------------------------------------------------------

_VALID_TASK = {
    "id": 1,
    "model": "sonnet",
    "complexity": "medium",
    "phase": "p1",
    "file_allowlist": ["src/a.py"],
    "regression_guard": [],
    "wave_deadline_minutes": 30,
}


def _payload(task: dict) -> str:
    import json

    return json.dumps({
        "ready_tasks": [task],
        "in_flight_reconcile": [],
        "escalations": [],
        "preflight_audit_status": "clean",
        "deadlock": False,
    })


def test_parse_bootstrap_return_seven_key_task():
    result = parse_bootstrap_return(_payload(_VALID_TASK))
    inst = result.ready_tasks[0]
    assert inst.id == 1
    assert inst.wave_deadline_minutes == 30
    assert not hasattr(inst, "prompt_path")


def test_parse_bootstrap_return_tolerates_legacy_prompt_path():
    # A stale subagent that still emits prompt_path must not break the wake.
    legacy = dict(_VALID_TASK, prompt_path="prompts/implementer-with-verifier-loop.md")
    result = parse_bootstrap_return(_payload(legacy))
    assert result.ready_tasks[0].id == 1


def test_parse_bootstrap_return_missing_required_field():
    broken = {k: v for k, v in _VALID_TASK.items() if k != "wave_deadline_minutes"}
    with pytest.raises(BootstrapMalformed) as exc:
        parse_bootstrap_return(_payload(broken))
    assert "wave_deadline_minutes" in str(exc.value)


# ---------------------------------------------------------------------------
# bake_wave_script — generated variants live under the project data_root,
# never beside the template in the suite tree
# ---------------------------------------------------------------------------

_TEMPLATE = "// header\nconst A = args  // placeholder comment\nreturn A.tasks\n"


def _skill_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "suite" / "skills" / "orchestrator"
    (sd / "workflows").mkdir(parents=True)
    (sd / "workflows" / "wave_group.js").write_text(_TEMPLATE, encoding="utf-8")
    return sd


def test_wave_script_dir_is_project_status_workflows(tmp_path):
    got = wave_script_dir(tmp_path / "data", "alpha")
    assert got == tmp_path / "data" / "projects" / "alpha" / "status" / "workflows"


def test_bake_writes_under_out_dir_never_under_skill_dir(tmp_path):
    sd = _skill_dir(tmp_path)
    out = wave_script_dir(tmp_path / "data", "alpha")
    assert not out.exists()  # bake must create it
    dst = bake_wave_script({"wave_n": 1, "tasks": []}, sd, "ep-w1", out_dir=out)
    assert dst == out / "wave_group_ep-w1.js"
    assert dst.is_file()
    assert 'const A = {"wave_n": 1, "tasks": []}' in dst.read_text(encoding="utf-8")
    # nothing generated next to the template
    assert sorted(p.name for p in (sd / "workflows").iterdir()) == ["wave_group.js"]


def test_bake_leaves_template_unchanged(tmp_path):
    sd = _skill_dir(tmp_path)
    bake_wave_script({"x": 1}, sd, "t", out_dir=tmp_path / "out")
    assert (sd / "workflows" / "wave_group.js").read_text(encoding="utf-8") == _TEMPLATE


def test_bake_two_projects_same_tag_do_not_collide(tmp_path):
    sd = _skill_dir(tmp_path)
    a = bake_wave_script({"p": "alpha"}, sd, "w1", out_dir=wave_script_dir(tmp_path / "d", "alpha"))
    b = bake_wave_script({"p": "beta"}, sd, "w1", out_dir=wave_script_dir(tmp_path / "d", "beta"))
    assert a != b
    assert '"alpha"' in a.read_text(encoding="utf-8")
    assert '"beta"' in b.read_text(encoding="utf-8")


@pytest.mark.parametrize("sub", ["workflows", "workflows/generated", "prompts", ".", "../lead-dev/workflows", ".."])
def test_bake_refuses_anywhere_under_the_skill_dir(tmp_path, sub):
    sd = _skill_dir(tmp_path)
    with pytest.raises(ValueError, match="suite tree"):
        bake_wave_script({}, sd, "t", out_dir=sd / sub)
    assert sorted(p.name for p in (sd / "workflows").iterdir()) == ["wave_group.js"]


@pytest.mark.parametrize("tag", ["../evil", "x/../../evil", "a/b", r"a", "", "t s"])
def test_bake_refuses_tags_that_are_not_a_plain_filename_component(tmp_path, tag):
    sd = _skill_dir(tmp_path)
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="tag must match"):
        bake_wave_script({}, sd, tag, out_dir=out)
    assert not out.exists()


def test_bake_requires_out_dir(tmp_path):
    sd = _skill_dir(tmp_path)
    with pytest.raises(TypeError):
        bake_wave_script({}, sd, "t")  # type: ignore[call-arg]


def test_remove_wave_script_is_idempotent(tmp_path):
    sd = _skill_dir(tmp_path)
    dst = bake_wave_script({}, sd, "t", out_dir=tmp_path / "out")
    assert remove_wave_script(dst, out_dir=tmp_path / "out") is True
    assert not dst.exists()
    assert remove_wave_script(dst, out_dir=tmp_path / "out") is False


def test_remove_wave_script_refuses_non_variant_paths(tmp_path):
    other = tmp_path / "status.md"
    other.write_text("keep me", encoding="utf-8")
    with pytest.raises(ValueError, match="not a baked wave variant"):
        remove_wave_script(other, out_dir=tmp_path)
    assert other.read_text(encoding="utf-8") == "keep me"


def test_remove_wave_script_refuses_paths_outside_out_dir(tmp_path):
    sd = _skill_dir(tmp_path)
    stray = sd / "workflows" / "wave_group_stray.js"
    stray.write_text("// stray", encoding="utf-8")
    with pytest.raises(ValueError, match="not directly under"):
        remove_wave_script(stray, out_dir=tmp_path / "out")
    assert stray.exists()
    nested = tmp_path / "out" / "deeper" / "wave_group_x.js"
    nested.parent.mkdir(parents=True)
    nested.write_text("// nested", encoding="utf-8")
    with pytest.raises(ValueError, match="not directly under"):
        remove_wave_script(nested, out_dir=tmp_path / "out")
    assert nested.exists()


@pytest.mark.parametrize("name", [None, "", "../x", "a/b", "a b"])
def test_wave_script_dir_requires_a_plain_project_slug(tmp_path, name):
    with pytest.raises(ValueError, match="project_name must be"):
        wave_script_dir(tmp_path, name)


# ---------------------------------------------------------------------------
# reconcile_group_from_status_git - a reported block escalates, a wedge requeues
# ---------------------------------------------------------------------------


def _state(tasks: dict[int, str], decisions: list[tuple[int, str]] = ()) -> StatusState:
    return StatusState(
        epic="omega",
        plan="/plans/omega.md",
        started="2026-01-01T00:00:00+00:00",
        status="running",
        current_phase="p1",
        tasks={tid: TaskState(state=st) for tid, st in tasks.items()},
        decisions=[
            Decision(at="2026-01-01T00:00:00+00:00", task=tid, tier=2, summary=summary)
            for tid, summary in decisions
        ],
    )


def test_a_task_the_wave_reported_blocked_escalates_and_is_never_requeued():
    """The repro: task 2 reported `blocked`, and reconcile used to hand it back as ready."""
    actions = reconcile_group_from_status_git(_state({1: "done", 2: "blocked"}), [1, 2], set())
    assert actions == {1: "done", 2: "escalate"}


def test_blocked_escalates_even_when_the_task_committed():
    """A task that committed and then failed its verifier is blocked, not ready for review."""
    actions = reconcile_group_from_status_git(_state({1: "blocked"}), [1], {1})
    assert actions == {1: "escalate"}


@pytest.mark.parametrize("st", ["pending", "ready", "in_flight"])
def test_a_wedge_still_requeues(st):
    """No terminal state and no commit is the genuine wedge, and only that requeues."""
    assert reconcile_group_from_status_git(_state({1: st}), [1], set()) == {1: "requeue"}


def test_a_task_missing_from_the_board_is_a_wedge():
    assert reconcile_group_from_status_git(_state({}), [7], set()) == {7: "requeue"}


@pytest.mark.parametrize("st", ["spec_review", "code_review"])
def test_a_reviewable_task_needs_review(st):
    assert reconcile_group_from_status_git(_state({1: st}), [1], set()) == {1: "needs_review"}


def test_an_unreported_task_with_a_commit_needs_review():
    assert reconcile_group_from_status_git(_state({1: "in_flight"}), [1], {1}) == {
        1: "needs_review"
    }


def test_done_stays_done_with_or_without_a_commit():
    assert reconcile_group_from_status_git(_state({1: "done", 2: "done"}), [1, 2], {1}) == {
        1: "done",
        2: "done",
    }


# ---------------------------------------------------------------------------
# reported_wave_outcome - which non-done outcome the wave reported
# ---------------------------------------------------------------------------


def test_reported_wave_outcome_reads_the_latest_note_for_that_task():
    state = _state(
        {1: "blocked", 2: "blocked"},
        [
            (1, "wave outcome: test_failure_persistent"),
            (2, "wave outcome: verifier_blocker_persistent (adversarial refuter panel)"),
            (1, "unrelated note"),
            (1, "wave outcome: escalation_required"),
        ],
    )
    assert reported_wave_outcome(state, 1) == "escalation_required"
    assert reported_wave_outcome(state, 2) == "verifier_blocker_persistent"


def test_reported_wave_outcome_is_none_when_nothing_was_recorded():
    state = _state({1: "blocked"}, [(0, "wave outcome: blocked"), (1, "something else")])
    assert reported_wave_outcome(state, 1) is None


def test_the_outcome_survives_the_real_reporter(tmp_path):
    """End to end through wave_event: the note the workflow writes is the note reconcile reads."""
    path = tmp_path / "omega.status.md"
    write_status(path, _state({1: "in_flight"}))
    note = ["note", "--status", str(path), "--task", "1", "--tier", "3"]
    assert wave_event.main([*note, "--summary", "wave outcome: escalation_required"]) == 0
    blocked = ["task-state", "--status", str(path), "--task", "1", "--state", "blocked"]
    assert wave_event.main(blocked) == 0
    state = read_status(path)
    assert reconcile_group_from_status_git(state, [1], set()) == {1: "escalate"}
    assert reported_wave_outcome(state, 1) == "escalation_required"


def test_wave_group_writes_the_outcome_note_prefix_reconcile_reads():
    """The workflow cannot import the prefix, so this pins the two spellings together."""
    wf = Path(__file__).resolve().parents[2] / "orchestrator" / "workflows" / "wave_group.js"
    src = wf.read_text(encoding="utf-8")
    assert f"`{WAVE_OUTCOME_NOTE_PREFIX}${{outcome}}`" in src


# ---------------------------------------------------------------------------
# A refused state write must not turn an escalation into a requeue
# ---------------------------------------------------------------------------

_DISPATCHED = "2026-01-01T00:00:00+00:00"


def _dispatched_board(tmp_path: Path) -> Path:
    path = tmp_path / "omega.status.md"
    state = _state({1: "in_flight"})
    state.tasks[1].since = _DISPATCHED
    write_status(path, state)
    return path


def test_an_outcome_note_escalates_when_the_state_write_was_refused(tmp_path):
    """The review's scenario: note lands, the blocked write is refused, the board stays in_flight."""
    path = _dispatched_board(tmp_path)
    note = ["note", "--status", str(path), "--task", "1", "--tier", "3"]
    assert wave_event.main([*note, "--summary", "wave outcome: escalation_required"]) == 0
    refused = ["task-state", "--status", str(path), "--task", "1", "--state", "blocked"]
    assert wave_event.main([*refused, "--verifier-verdict", "n/a"]) == 2
    state = read_status(path)
    assert state.tasks[1].state == "in_flight"
    assert reconcile_group_from_status_git(state, [1], set()) == {1: "escalate"}
    assert reported_wave_outcome(state, 1) == "escalation_required"


def test_an_outcome_note_from_before_the_current_dispatch_does_not_count():
    state = _state({1: "in_flight"})
    state.tasks[1].since = "2026-01-02T00:00:00+00:00"
    state.decisions.append(
        Decision(
            at="2026-01-01T12:00:00+00:00",
            task=1,
            tier=3,
            summary="wave outcome: escalation_required",
        )
    )
    assert reported_wave_outcome(state, 1) is None
    assert reconcile_group_from_status_git(state, [1], set()) == {1: "requeue"}


def test_without_since_the_latest_wave_dispatch_event_bounds_the_note():
    state = _state({1: "in_flight"})
    state.decisions += [
        Decision(
            at="2026-01-01T01:00:00Z", task=1, tier=3, summary="wave outcome: escalation_required"
        ),
        Decision(
            at="2026-01-01T02:00:00Z", task=0, tier=1, summary="session_wave_dispatched: Wave 2"
        ),
    ]
    assert reconcile_group_from_status_git(state, [1], set()) == {1: "requeue"}
    state.decisions.append(
        Decision(
            at="2026-01-01T03:00:00Z",
            task=1,
            tier=2,
            summary="wave outcome: test_failure_persistent",
        )
    )
    assert reconcile_group_from_status_git(state, [1], set()) == {1: "escalate"}
    assert reported_wave_outcome(state, 1) == "test_failure_persistent"


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("wave outcome: done with warnings", UNKNOWN_WAVE_OUTCOME),  # not cut down to "done"
        ("wave outcome: blocked but fine", UNKNOWN_WAVE_OUTCOME),
        ("wave outcome: garbage", UNKNOWN_WAVE_OUTCOME),
        ("wave outcome: ", UNKNOWN_WAVE_OUTCOME),
        ("wave outcome: unknown_outcome", UNKNOWN_WAVE_OUTCOME),
        ("wave outcome: blocked (inside review asked for revision)", "blocked"),
        (
            "wave outcome: verifier_blocker_persistent (best-of-3: all failed)",
            "verifier_blocker_persistent",
        ),
    ],
)
def test_reported_wave_outcome_parses_the_whole_token(summary, expected):
    state = _state({1: "in_flight"}, [(1, summary)])
    assert reported_wave_outcome(state, 1) == expected


def _js_list(src: str, name: str) -> set[str]:
    match = re.search(rf"^const {name} = \[([^\]]*)\]", src, re.MULTILINE)
    assert match, f"{name} not found in wave_group.js"
    return set(re.findall(r"'([^']*)'", match.group(1)))


def test_wave_group_vocabularies_match_the_python_side():
    """The workflow cannot import these, so each list is pinned to the one it mirrors."""
    wf = Path(__file__).resolve().parents[2] / "orchestrator" / "workflows" / "wave_group.js"
    src = wf.read_text(encoding="utf-8")
    assert _js_list(src, "NOTE_OUTCOMES") == WAVE_NOTE_OUTCOMES
    assert f"'{UNKNOWN_WAVE_OUTCOME}'" in src
    assert _js_list(src, "VERIFIER_VERDICTS") == wave_event.VERIFIER_VERDICTS
    assert _js_list(src, "FINDING_SEVERITIES") == wave_event.FINDING_SEVERITIES


# ---------------------------------------------------------------------------
# gh#737 — task body extraction: both heading levels, subsections kept, empty refused
# ---------------------------------------------------------------------------



def _plan_file(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "plan.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_task_body_h3_writing_plans_shape(tmp_path: Path) -> None:
    p = _plan_file(
        tmp_path,
        "# Plan\n\n### Task 2: Parser\nparse it\n**Files:** `a.py`\n\n### Task 3: Writer\nwrite it\n",
    )
    assert _task_body(p, 2) == "parse it\n**Files:** `a.py`"
    assert _task_body(p, 3) == "write it"


def test_task_body_h2_keeps_files_and_acceptance_subsections(tmp_path: Path) -> None:
    p = _plan_file(
        tmp_path,
        "## Task 2: Colon\nintro\n## Files:\n- `x.py`\n## Acceptance criteria\n- passes\n"
        "## Task 3: Next\nother\n",
    )
    assert _task_body(p, 2) == "intro\n## Files:\n- `x.py`\n## Acceptance criteria\n- passes"


def test_task_body_stops_at_shallower_heading(tmp_path: Path) -> None:
    p = _plan_file(tmp_path, "### Task 1: a\nbody\n## Phase 2\nnot the task\n")
    assert _task_body(p, 1) == "body"


def test_task_body_separator_variants_and_number_boundary(tmp_path: Path) -> None:
    p = _plan_file(
        tmp_path,
        "## Task 1. dot\none\n## Task 12 — dash\ntwelve\n## Task 2\ntwo\n",
    )
    assert _task_body(p, 1) == "one"
    assert _task_body(p, 12) == "twelve"
    assert _task_body(p, 2) == "two"


def test_build_wave_args_refuses_an_empty_task_body(tmp_path: Path) -> None:
    plan = PlanFrontmatter(
        epic="Test Epic",
        spec="spec.md",
        phases=["p1"],
        tasks={1: Task(id=1, phase="p1", deps=[])},
        test_commands={"regression": "pytest -q"},
        slug="test-epic",
    )
    plan_path = _plan_file(tmp_path, "## Task 1: heading only\n## Task 2: x\ny\n")
    with pytest.raises(ValueError, match=r"Task 1 has an empty body"):
        build_wave_args(
            plan=plan,
            plan_path=plan_path,
            group_task_ids=[1],
            status_path=tmp_path / "status.md",
            wave_n=1,
            base_branch="main",
            repo_path=tmp_path,
            ntfy_url=None,
            ntfy_topic=None,
        )


def test_task_body_keeps_hash_comments_inside_code_fences(tmp_path: Path) -> None:
    for level in ("##", "###"):
        p = _plan_file(
            tmp_path,
            f"{level} Task 1: a\nRun:\n```bash\n# install deps\npip install x\n```\nthen check\n"
            f"{level} Task 2: b\nnext\n",
        )
        assert _task_body(p, 1) == "Run:\n```bash\n# install deps\npip install x\n```\nthen check"


def test_task_heading_inside_a_fence_is_not_a_task(tmp_path: Path) -> None:
    p = _plan_file(
        tmp_path,
        "## Task 1: a\nexample:\n~~~~markdown\n## Task 2: example\n~~~\nstill in fence\n~~~~\nafter\n"
        "## Task 2: real\nreal body\n",
    )
    assert _task_body(p, 1) == (
        "example:\n~~~~markdown\n## Task 2: example\n~~~\nstill in fence\n~~~~\nafter"
    )
    assert _task_body(p, 2) == "real body"


def test_fence_line_with_info_string_does_not_close_the_open_fence(tmp_path: Path) -> None:
    p = _plan_file(
        tmp_path,
        "## Task 1: a\n````markdown\n```python\n# file: src/add.py\n## Task 9: quoted\n````\ntail\n"
        "## Task 2: b\nnext\n",
    )
    assert _task_body(p, 1) == (
        "````markdown\n```python\n# file: src/add.py\n## Task 9: quoted\n````\ntail"
    )
    assert _task_body(p, 2) == "next"


def test_unclosed_fence_does_not_swallow_later_tasks(tmp_path: Path) -> None:
    p = _plan_file(
        tmp_path,
        "## Task 1: a\n```bash\nrun it\n## Task 2: b\nsecond\n## Task 3: c\nthird\n",
    )
    assert _task_body(p, 1) == "```bash\nrun it"
    assert _task_body(p, 2) == "second"
    assert _task_body(p, 3) == "third"
