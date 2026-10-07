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


def test_task_body_canonical_h3_keeps_h4_files_block(tmp_path: Path) -> None:
    p = _plan_file(
        tmp_path,
        "# Plan\n\n### Task 1: Canon\nbuild it\n#### Files:\n- `pkg/a.py`\n\n"
        "### Task 2: Next\nother\n",
    )
    assert _task_body(p, 1) == "build it\n#### Files:\n- `pkg/a.py`"
    assert _task_body(p, 2) == "other"


def test_task_body_h3_task_drops_h2_files_by_design(tmp_path: Path) -> None:
    # Regression lock: a `##` heading under a `### Task` is shallower, so it ends the
    # body and the Files block is NOT part of the task. This is why the solution SKILL
    # prescribes `#### Files:` under `### Task N:`. Do not "fix" the parser to swallow
    # `##` here: in a `###`-task plan, `##` is how phases and plan-level sections start.
    p = _plan_file(
        tmp_path,
        "### Task 1: Canon\nbuild it\n## Files:\n- `pkg/a.py`\n\n### Task 2: Next\nother\n",
    )
    assert _task_body(p, 1) == "build it"


def test_solution_skill_mentions_h2_task_format_only_as_legacy() -> None:
    # The plan format the solution SKILL prescribes is `### Task N:` + `#### Files:`;
    # an `## Task` / `## Files:` mention there is only acceptable as the legacy form.
    skill = Path(__file__).resolve().parents[2] / "solution/SKILL.md"
    lines = skill.read_text(encoding="utf-8").splitlines()
    pattern = re.compile(r"`##\s+(Task\b|Files:)|^\s*##\s+(Task|Files)\b")
    # "legacy" must come before the old form on the line, so a sentence that prescribes
    # `## Files:` and only later mentions legacy plans is still caught.
    offenders = [
        ln for ln in lines
        if (m := pattern.search(ln)) and "legacy" not in ln[: m.start()]
    ]
    assert offenders == []
    text = "\n".join(lines)
    assert "`### Task N:`" in text and "`#### Files:`" in text


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


def _orchestrator_step7() -> str:
    skill_md = Path(__file__).resolve().parents[2] / "orchestrator" / "SKILL.md"
    skill = skill_md.read_text(encoding="utf-8")
    start = skill.index("### Step 7: Dispatch ready tasks")
    return skill[start : skill.index("### Step 8:", start)]


def test_orchestrator_names_the_empty_body_refusal_and_its_handler() -> None:
    # The SKILL is build_wave_args' only real caller; without a handler the model meets a
    # bare traceback mid-dispatch.
    step7 = _orchestrator_step7()
    assert "Empty-body refusal" in step7
    para = step7[step7.index("Empty-body refusal") :].split("\n", 1)[0]
    assert "ValueError" in para and "build_wave_args" in para
    assert "tier-3 escalation" in para and "open_escalation(" in para
    assert "re-run /solution" in para
    assert "do not increment the wave counter" in para
    # The counter stays put, so the trees Step 7.0 made under that number must go, or the
    # re-dispatch after the answer collides on the same branch name.
    assert "Releasing a wave tree" in para
    # Only this refusal is handled; any other exception surfaces.
    assert "has an empty body" in para and "re-raise" in para


def test_orchestrator_builds_wave_args_before_incrementing_the_wave() -> None:
    # A refusal must leave state.total_waves untouched, so the args are built (with the
    # post-increment number) before increment_wave runs.
    step7 = _orchestrator_step7()
    build = step7.index("args = build_wave_args(")
    assert build < step7.index("increment_wave(state)")
    call = step7[build : step7.index("```", build)]
    assert "wave_n=state.total_waves + 1" in call


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


# --- best-of-N tree lifecycle, restart recovery, per-group wave numbers ------------------------
# These steps are prose the orchestrator session executes, so the sentences are the
# implementation; each pin below fails if its instruction is removed.

_ORCH_SKILL = Path(__file__).resolve().parents[2] / "orchestrator" / "SKILL.md"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _orch_section(start: str, end: str) -> str:
    text = _ORCH_SKILL.read_text(encoding="utf-8")
    i = text.index(start)
    return text[i : text.index(end, i)]


def _restart_log_command() -> str:
    step = _flat(_orch_section("**Step 1.2 —", "**Step 1.2.5"))
    cmds = re.findall(r"`(git -C <worktree> log [^`]*--branches=[^`]*)`", step)
    assert len(cmds) == 1, f"Step 1.2 names {len(cmds)} wave-branch restart logs: {cmds}"
    return cmds[0]


def test_restart_recovery_logs_the_wave_branches_and_keeps_a_found_wave_commit() -> None:
    step = _flat(_orch_section("**Step 1.2 —", "**Step 1.2.5"))
    assert _restart_log_command() == (
        "git -C <worktree> log <epic_branch> --branches='<plan.slug>-w[0-9]*' --since=<previous last_wake>"
    )
    assert "When `plan.per_wave_worktrees` is False: `git -C <worktree> log --since=<previous last_wake>`" in step
    # Sub-step 3 searches that same log, not a fresh epic-only one.
    assert "Search the restart log from sub-step 2" in step
    assert "Search `git -C <worktree> log --since=<previous last_wake>`" not in step
    found = step[step.index("If found on the epic branch, or on the task's own recorded"):]
    found = found[: found.index("If found only on a best-of-N candidate branch")]
    assert 'set `state = "code_review"`' in found
    assert "Keep `wave_worktree_path` and `wave_branch` for a commit on the wave branch" in found
    cand = step[step.index("If found only on a best-of-N candidate branch"):]
    assert "Handle it as not found" in cand
    other = step[step.index("If found only on any other branch"):]
    other = other[: other.index("If not found:")]
    assert "Handle it as not found" in other, "a sibling epic's commit would complete this task"


def test_the_restart_log_command_finds_an_unmerged_wave_branch_commit(tmp_path: Path) -> None:
    """Runs the exact Step 1.2 command against a repo whose only task commit sits on a wave
    branch in a sibling tree, as it does after a restart before merge-back."""
    import shlex
    import subprocess

    from agentflow.wave_worktree import create_wave_worktree

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "master", str(repo)], check=True, capture_output=True)

    def git(cwd: Path, *a: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.email=t@e.st", "-c", "user.name=t", "-c", "commit.gpgsign=false", *a],
            cwd=cwd, check=True, capture_output=True, text=True,
        ).stdout

    git(repo, "commit", "--allow-empty", "-m", "base")
    tree = create_wave_worktree(repo, "ep", 2, 3, "master")
    git(tree, "commit", "--allow-empty", "-m", "feat(T3): wave work")
    cand = create_wave_worktree(repo, "ep", 2, "3-c1", "master")
    git(cand, "commit", "--allow-empty", "-m", "feat(T3): candidate work")
    # Another epic whose slug starts with `ep-w`: its wave branches must stay out of the log.
    sibling = create_wave_worktree(repo, "ep-webhooks", 1, 3, "master")
    git(sibling, "commit", "--allow-empty", "-m", "feat(T3): sibling epic work")

    cmd = (_restart_log_command()
           .replace("<worktree>", str(repo)).replace("<epic_branch>", "master")
           .replace("<plan.slug>", "ep").replace("<previous last_wake>", "2000-01-01"))
    argv = shlex.split(cmd)
    assert argv[:2] == ["git", "-C"]
    log = git(repo, *argv[3:], "--format=%s")
    assert "feat(T3): wave work" in log.splitlines()
    assert "feat(T3): candidate work" in log.splitlines(), "the pattern misses best-of-N candidate branches"
    assert "feat(T3): sibling epic work" not in log.splitlines(), "the pattern matches another epic's branches"
    # The epic-only log the recovery reads with the flag off cannot see it.
    assert "feat(T3): wave work" not in git(repo, "log", "--since=2000-01-01", "--format=%s")


def test_step_7_0_names_each_group_s_trees_with_that_group_s_wave_number() -> None:
    s70 = _orch_section("**Step 7.0", "1. Parse the Files section")
    flat = _flat(s70)
    assert "Step 7.0 runs once per parallel-safe group" in flat
    assert "never once for the whole wake" in flat
    assert "group_wave_n = state.total_waves + 1" in s70
    call = s70[s70.index("create_wave_worktree("):]
    call = call[: call.index("```")]
    assert "wave_n=group_wave_n" in call
    assert 'f"{plan.slug}-w{group_wave_n}-t{task.id}"' in s70
    assert "total_waves+1" not in s70
    # Item 4 runs Step 7.0 for the group immediately before that group's build_wave_args.
    step7 = _orchestrator_step7()
    first = step7.index("Run Step 7.0 for this group's tasks first")
    assert first < step7.index("args = build_wave_args(") < step7.index("increment_wave(state)")


def test_every_exit_after_step_7_0_releases_the_group_s_trees() -> None:
    s70 = _flat(_orch_section("**Step 7.0", "1. Parse the Files section"))
    rule = s70[s70.index("**Every exit between Step 7.0 and the workflow launch releases this group's trees.**"):]
    for phrase in ("any other exception `build_wave_args` raises", "`bake_wave_script` raising",
                   "Workflow call that errored, so no run launched", "**Releasing a wave tree**", "back to `ready`"):
        assert phrase in rule, phrase
    assert "returns no `runId`" not in rule, "a run that may exist must not lose its trees"
    assert ("Release only when the launch definitely did not happen: a Workflow call that returned without "
            "error but with no readable `runId` may have started a run that builds in these trees, so keep "
            "the trees and fields and open a tier-3 escalation") in rule
    assert 'append a tier-2 decision `"wave {n} not sent: {reason}"`' in rule
    para = _orchestrator_step7()
    para = para[para.index("Empty-body refusal"):].split("\n", 1)[0]
    other = para[para.index("any other exception is not this refusal"):]
    other = other[: other.index("re-raise it")]
    assert "**Releasing a wave tree**" in other, "a re-raised error leaves this group's trees behind"
    launch = _flat(_orchestrator_step7())
    assert "release them first (**Every exit between Step 7.0 and the workflow launch" in launch
    assert "or the Workflow call errors, no workflow holds" in launch
    assert "no readable `runId` may have launched a run: escalate, never release" in launch


def test_best_of_n_exits_remove_every_candidate_tree() -> None:
    step7 = _flat(_orchestrator_step7())
    for phrase in ("the workflow retries the land once",
                   "it removes every candidate tree, the winner's included",
                   "keeps the winner's branch (`cleanup --preserve-branch`)",
                   "All candidates failed tests: no candidate is eligible, so the workflow removes every candidate's tree and branch"):
        assert phrase in step7, phrase
    blocked = step7[step7.index("**Unlanded best-of-N winner**"):]
    blocked = blocked[: blocked.index("First time on this task")]
    assert "**Releasing a wave tree**" in blocked
    assert "re-dispatch through Step 7 (Step 7.0, then item 4)" in blocked
    release = _flat(_orch_section("**Releasing a wave tree (every return to `ready`).**", "### Step 2:"))
    assert "a best-of-N task the BLOCKED handler re-dispatches after an unlanded winner" in release


def test_the_worktree_context_block_is_filled_after_the_group_s_step_7_0() -> None:
    """Item 3 comes before item 4 in reading order, but Step 7.0 runs inside item 4 sub-step 1:
    a block filled at item 3 names a tree that does not exist yet."""
    flat = _flat(_orchestrator_step7())
    item3 = flat[flat.index("**Worktree context (ALWAYS"):flat.index("## Worktree context (read FIRST")]
    assert "**Fill `{wave_tree}` and `{wave_ref}` at item 4 sub-step 1, not here.**" in item3
    assert ("Read them after that group's Step 7.0, immediately before the \"Prepend the "
            "worktree-context block\" step there.") in item3
    s71 = flat[flat.index("Run Step 7.0 for this group's tasks first"):]
    prepend = s71.index("**Prepend the worktree-context block (item 3 above)")
    fill = s71.index("Fill its `{wave_tree}` and `{wave_ref}` now, from the `wave_worktree_path` and "
                     "`wave_branch` this group's Step 7.0 just recorded.")
    assert prepend < fill < s71.index("increment_wave(state)")


def _unlanded_bullet() -> str:
    step7 = _flat(_orchestrator_step7())
    b = step7[step7.index("**Unlanded best-of-N winner**"):]
    return b[: b.index("First time on this task")]


def test_an_unlanded_winner_branch_gets_one_merge_back_before_a_re_dispatch() -> None:
    b = _unlanded_bullet()
    assert "inspect with git -C <worktree> log {branch}" in b
    assert "{worktree}" not in b.split("Then try ONE merge-back")[0], "the winner tree is gone"
    assert "merge_back(wave_worktree=worktree, main_worktree=worktree, branch=<winner-branch>)" in b
    ok = b[b.index("On `result.success`:"):b.index("On `result.success is False`:")]
    for phrase in ("`git -C <worktree> branch -d <winner-branch>`", "**Releasing a wave tree**",
                   "set the task's commit to `winner_commit` and `state = \"code_review\"`",
                   "run the orchestrator review gates", "Skip the merge-back on approval there"):
        assert phrase in ok, phrase
    fail = b[b.index("On `result.success is False`:"):]
    assert "delete {branch} by hand once inspected" in fail
    assert fail.index("**Releasing a wave tree**") < fail.index("re-dispatch through Step 7 (Step 7.0, then item 4)")
    assert b.index("Then try ONE merge-back") < b.index("re-dispatch through Step 7")


def test_an_unlanded_winner_counts_toward_the_two_attempt_rule() -> None:
    b = _unlanded_bullet()
    assert ("An unlanded winner counts toward the two-attempt rule like any BLOCKED return: the re-dispatch "
            "above is the first-time handling, and a second unlanded return on the same task (or any second "
            "BLOCKED) follows Second BLOCKED below.") in b


def test_the_kept_branch_merge_back_works_from_the_epic_tree(tmp_path: Path) -> None:
    """The BLOCKED handler merges a kept branch whose tree is gone by passing the epic tree as
    the tree to fetch from; branches are shared, so that must land it."""
    import subprocess

    from agentflow.wave_worktree import cleanup_wave_worktree, create_wave_worktree, merge_back

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "master", str(repo)], check=True, capture_output=True)
    for k, v in (("user.email", "t@e.st"), ("user.name", "t"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True, capture_output=True)

    def git(cwd: Path, *a: str) -> str:
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout

    git(repo, "commit", "--allow-empty", "-m", "base")
    tree = create_wave_worktree(repo, "ep", 1, "3-c0", "master")
    git(tree, "commit", "--allow-empty", "-m", "feat(T3): winner")
    git(repo, "commit", "--allow-empty", "-m", "epic moved on")
    cleanup_wave_worktree(tree, "ep-w1-t3-c0", repo, preserve_branch=True)
    assert not tree.exists()
    result = merge_back(wave_worktree=repo, main_worktree=repo, branch="ep-w1-t3-c0")
    assert result.success, result.stderr
    assert "feat(T3): winner" in git(repo, "log", "--format=%s").splitlines()
    git(repo, "branch", "-d", "ep-w1-t3-c0")


def test_the_verifier_blocker_re_dispatch_releases_the_task_tree() -> None:
    step7 = _flat(_orchestrator_step7())
    h = step7[step7.index('For `effective_outcome == "verifier_blocker_persistent"`'):]
    h = h[: h.index('For `effective_outcome == "blocked"`')]
    assert ("before it, when the task records a `wave_worktree_path`, release that tree and clear its "
            "fields (**Releasing a wave tree**, Step 1.2.5)") in h
    assert h.index("**Releasing a wave tree**") < h.index("if still blocked")
    release = _flat(_orch_section("**Releasing a wave tree (every return to `ready`).**", "### Step 2:"))
    assert "any task the `verifier_blocker_persistent` handler re-dispatches" in release


def test_the_hallucinated_denial_re_dispatch_releases_the_task_tree() -> None:
    # This branch runs instead of the first-BLOCKED handler, so it never reaches that release.
    step7 = _flat(_orchestrator_step7())
    h = step7[step7.index("If `is_hallucinated_denial`:"):]
    h = h[: h.index("**On any subagent BLOCKED return that is NOT a hallucination signature:**")]
    assert ("before it, when the task records a `wave_worktree_path`, release that tree and clear its "
            "fields (**Releasing a wave tree**, Step 1.2.5)") in h
    assert h.index("**Releasing a wave tree**") < h.index("Re-dispatch the wave")
    release = _flat(_orch_section("**Releasing a wave tree (every return to `ready`).**", "### Step 2:"))
    assert "any task the hallucinated-denial branch re-dispatches" in release


_IN_FLIGHT = '`state.tasks[tid].state = "in_flight"` (and `since = now`)'


def _re_dispatch_paths() -> dict[str, tuple[str, str]]:
    """Each Step 7 path that re-dispatches a task the reporter wrote `blocked`, with the phrase
    that is its re-dispatch instruction."""
    step7 = _flat(_orchestrator_step7())

    def cut(start: str, end: str) -> str:
        s = step7.index(start)
        return step7[s:step7.index(end, s)]

    return {
        "verifier blocker": (
            cut('For `effective_outcome == "verifier_blocker_persistent"`',
                'For `effective_outcome == "blocked"`'),
            "re-dispatch once with more context",
        ),
        "hallucinated denial": (
            cut("If `is_hallucinated_denial`:",
                "**On any subagent BLOCKED return that is NOT a hallucination signature:**"),
            "Re-dispatch the wave",
        ),
        "failed merge-back": (
            cut("On `result.success is False`: append tier-2 decision `\"merge-back of kept branch",
                "An unlanded winner counts toward the two-attempt rule"),
            "re-dispatch through Step 7",
        ),
        "first blocked": (
            cut("- First time on this task:", "- Second BLOCKED return on same task"),
            "re-dispatch with more context",
        ),
    }


def _flips_in_flight_before_release_and_re_dispatch(path: str, re_dispatch: str) -> bool:
    # Mirrors the verifier path: flip the state, release the old tree, then re-dispatch.
    flip, release, again = (path.find(p) for p in (_IN_FLIGHT, "**Releasing a wave tree**", re_dispatch))
    return -1 < flip < release and flip < again


@pytest.mark.parametrize("where", ["verifier blocker", "hallucinated denial", "failed merge-back", "first blocked"])
def test_every_re_dispatch_of_a_blocked_task_sets_it_in_flight_first(where: str) -> None:
    assert _flips_in_flight_before_release_and_re_dispatch(*_re_dispatch_paths()[where])


@pytest.mark.parametrize("where", ["verifier blocker", "hallucinated denial", "failed merge-back", "first blocked"])
def test_the_in_flight_check_rejects_a_flip_after_the_re_dispatch(where: str) -> None:
    path, re_dispatch = _re_dispatch_paths()[where]
    broken = path.replace(_IN_FLIGHT, "it") + f" Then set {_IN_FLIGHT}."
    assert not _flips_in_flight_before_release_and_re_dispatch(broken, re_dispatch)


# ---------------------------------------------------------------------------
# gh#1049: build_wave_args carries each task's wave tree on the per-wave path
# ---------------------------------------------------------------------------

def _seed_repo(tmp_path: Path) -> Path:
    import subprocess

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "master", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.st", "-c", "user.name=t", "-c", "commit.gpgsign=false",
         "commit", "--allow-empty", "-m", "base"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


def _per_wave_args(tmp_path: Path, repo: Path, *, per_wave: bool, wave_n: int = 3) -> dict:
    plan = PlanFrontmatter(
        epic="Test Epic",
        spec="spec.md",
        phases=["p1"],
        tasks={1: Task(id=1, phase="p1", deps=[]), 2: Task(id=2, phase="p1", deps=[])},
        test_commands={},
        slug="otherproj",
        per_wave_worktrees=per_wave,
    )
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("### Task 1: one\nbody one\n### Task 2: two\nbody two\n", encoding="utf-8")
    return build_wave_args(
        plan=plan, plan_path=plan_path, group_task_ids=[1, 2],
        status_path=tmp_path / "status.md", wave_n=wave_n, base_branch="master",
        repo_path=repo, ntfy_url=None, ntfy_topic=None,
    )


def test_per_wave_args_name_the_tree_and_branch_create_wave_worktree_made(tmp_path):
    import subprocess

    from agentflow.wave_worktree import create_wave_worktree

    repo = _seed_repo(tmp_path)
    made = {tid: create_wave_worktree(repo, "otherproj", 3, tid, "master") for tid in (1, 2)}

    tasks = {t["id"]: t for t in _per_wave_args(tmp_path, repo, per_wave=True)["tasks"]}

    for tid, tree in made.items():
        checked_out = subprocess.run(
            ["git", "branch", "--show-current"], cwd=tree, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert tasks[tid]["wave_worktree"] == tree.as_posix()
        assert tasks[tid]["wave_branch"] == checked_out


def test_per_wave_args_refuse_a_wave_tree_that_does_not_exist(tmp_path):
    from agentflow.wave_worktree import create_wave_worktree

    repo = _seed_repo(tmp_path)
    create_wave_worktree(repo, "otherproj", 3, 1, "master")  # task 2's tree was never made
    missing = (tmp_path / "repo-otherproj-w3-t2").as_posix()

    with pytest.raises(ValueError, match=re.escape(missing)):
        _per_wave_args(tmp_path, repo, per_wave=True)


def test_without_per_wave_worktrees_args_carry_neither_key(tmp_path):
    from agentflow.wave_worktree import create_wave_worktree

    repo = _seed_repo(tmp_path)
    for tid in (1, 2):  # trees at the derived paths must not leak into legacy args
        create_wave_worktree(repo, "otherproj", 3, tid, "master")

    for task in _per_wave_args(tmp_path, repo, per_wave=False)["tasks"]:
        assert "wave_worktree" not in task
        assert "wave_branch" not in task
