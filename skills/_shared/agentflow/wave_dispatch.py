"""Dispatch seam between the orchestrator (Step 7) and the wave-group Workflow.

Pure, testable helpers: build the workflow `args` from a ready group, parse the
workflow return (applying coerce_wave_outcome), reconcile a group from
status.md + git, and compute the ScheduleWakeup fallback delay. The actual
Workflow tool call is made by the orchestrator (model) per SKILL.md Step 7.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from agentflow.plan_parser import PlanFrontmatter, Task, parse_plan  # noqa: F401 (parse_plan re-export)
from agentflow.status_writer import StatusState, coerce_wave_outcome

_LOG = logging.getLogger(__name__)

_MODEL_BY_COMPLEXITY = {"low": "haiku", "medium": "sonnet", "high": "opus"}


def _mode(task: Task) -> str:
    if task.best_of_n > 1:
        return "best_of_n"
    if task.adversarial:
        return "adversarial"
    return "single"


# `## Task N:` (solution SKILL) and `### Task N:` (vendored writing-plans) are both
# in use, so the level is captured rather than assumed.
_TASK_HEADING_RE = re.compile(r"^(#{2,3})\s+Task\s+(\d+)\b")
_ANY_HEADING_RE = re.compile(r"^(#{1,6})\s")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _scan_task_body(lines: list[str], tid: int, fences: bool) -> tuple[str, bool]:
    """One pass of `_task_body`; also reports whether a fence was still open at EOF."""
    out: list[str] = []
    level = 0
    fence = ""
    for line in lines:
        f = _FENCE_RE.match(line) if fences else None
        if fence:
            # As in CommonMark, only a bare fence of the opener's character, at least as long,
            # closes it; a ```python line inside the block is content.
            if (
                f
                and f.group(1)[0] == fence[0]
                and len(f.group(1)) >= len(fence)
                and not line[f.end():].strip()
            ):
                fence = ""
            if level:
                out.append(line)
            continue
        if f:
            fence = f.group(1)
            if level:
                out.append(line)
            continue
        m = _TASK_HEADING_RE.match(line)
        if m:
            if level:
                return "\n".join(out).strip(), False
            if int(m.group(2)) == tid:
                level = len(m.group(1))
            continue
        if level:
            h = _ANY_HEADING_RE.match(line)
            if h and len(h.group(1)) < level:
                return "\n".join(out).strip(), False
            out.append(line)
    return "\n".join(out).strip(), bool(fence)


def _task_body(plan_path: Path, tid: int) -> str:
    """Extract the markdown body under the `## Task <tid>` or `### Task <tid>` heading.

    The body runs to the next Task heading, or to a heading shallower than the task's
    own. A same-level heading that is not a Task heading is part of the body, because
    the solution SKILL puts `## Files:` inside each `## Task N:` body. Lines inside a
    fenced code block are never headings: task bodies paste shell and Python whose
    `# comment` lines would otherwise end the body. A fence left open at end of file
    would swallow every later task into this one, so an unbalanced plan is re-read
    with fences ignored.
    """
    lines = Path(plan_path).read_text(encoding="utf-8").splitlines()
    body, unbalanced = _scan_task_body(lines, tid, fences=True)
    if unbalanced:
        _LOG.warning("unclosed code fence in %s; reading Task %s with fences ignored", plan_path, tid)
        body, _ = _scan_task_body(lines, tid, fences=False)
    return body


def _contract_path(skill_dir: Path | None, task_type: str) -> str | None:
    """Resolve the wave-contract prompt the implementer must read and follow.

    `type: tdd` tasks get the TDD variant; everything else gets the
    verifier-loop contract. Returns None when skill_dir is not provided
    (legacy caller) — the workflow prompt then omits the contract line.
    """
    if skill_dir is None:
        return None
    name = (
        "implementer-with-tdd-loop.md"
        if task_type == "tdd"
        else "implementer-with-verifier-loop.md"
    )
    return (Path(skill_dir) / "prompts" / name).as_posix()


def build_wave_args(
    *,
    plan: PlanFrontmatter,
    plan_path: Path,
    group_task_ids: list[int],
    status_path: Path,
    wave_n: int,
    base_branch: str,
    repo_path: Path,
    ntfy_url: str | None,
    ntfy_topic: str | None,
    skill_dir: Path | None = None,
) -> dict:
    """Build the JSON `args` object passed to the wave-group Workflow.

    `skill_dir` (the orchestrator skill root) resolves each task's
    `contract_path` — the implementer prompt template the wave subagent
    reads and follows as its governing contract.
    """
    gv = plan.verification
    max_fix = gv.max_fix_iterations if gv is not None else 4
    tasks_out: list[dict] = []
    for tid in group_task_ids:
        t = plan.tasks[tid]
        v = t.verification
        body = _task_body(plan_path, tid)
        if not body:
            # The implementer sees only this body; an empty one is a silent no-op task.
            raise ValueError(
                f"Task {tid} has an empty body in {Path(plan_path).as_posix()}: "
                f"expected a '## Task {tid}:' or '### Task {tid}:' heading followed by its contract"
            )
        tasks_out.append({
            "id": tid,
            "mode": _mode(t),
            "best_of_n": t.best_of_n,
            "adversarial": t.adversarial,
            "model": _MODEL_BY_COMPLEXITY.get(t.complexity, "sonnet"),
            "task_type": t.task_type,
            "contract_path": _contract_path(skill_dir, t.task_type),
            "body": body,
            "file_allowlist": list(v.file_allowlist) if v else [],
            "regression_guard": list(v.regression_guard) if v else [],
            "acceptance_criteria": list(v.acceptance_criteria) if v else [],
            "test_commands": list(v.test_commands) if v else [],
            "wave_deadline_minutes": t.wave_deadline_minutes,
        })
    return {
        "wave_n": wave_n,
        "epic_slug": plan.slug,
        "status_path": Path(status_path).as_posix(),
        "repo_path": Path(repo_path).as_posix(),
        "base_branch": base_branch,
        "max_fix_iterations": max_fix,
        "ntfy": ({"url": ntfy_url, "topic": ntfy_topic} if ntfy_url else None),
        "reporter": ["python", "-m", "agentflow.wave_event"],
        "worktree_cli": ["python", "-m", "agentflow.wave_worktree"],
        "tasks": tasks_out,
    }


_DEFAULT_WAVE_DEADLINE_MIN = 30
_FALLBACK_SLACK_SEC = 300  # extra 5 min so the fallback only fires on genuine overrun


def fallback_delay_seconds(plan: PlanFrontmatter, group_task_ids: list[int]) -> int:
    """ScheduleWakeup fallback for a fired wave: max task wave_deadline + slack.

    This is a wedge-catcher, not a poll — the completion notification is the
    primary wake. Sized so it only fires if the workflow overran/wedged.
    """
    gv = plan.verification
    plan_default = (
        gv.wave_deadline_minutes if gv is not None and gv.wave_deadline_minutes else None
    ) or _DEFAULT_WAVE_DEADLINE_MIN
    deadlines = [
        (plan.tasks[tid].wave_deadline_minutes or plan_default) for tid in group_task_ids
    ]
    return max(deadlines) * 60 + _FALLBACK_SLACK_SEC


def reconcile_group_from_status_git(
    state: StatusState, group_task_ids: list[int], committed_task_ids: set[int]
) -> dict[int, str]:
    """Decide each group task's reconciled state from durable signals only.

    `committed_task_ids` = tasks the caller matched to a landing commit via
    `git log` (orchestrator computes this). status.md (reporter writes) is the
    primary signal; git commit presence corroborates. Returns {tid: action} where
    action ∈ {"done", "needs_review", "escalate", "requeue"}.

    `escalate` means the wave REPORTED the task blocked: it reached a terminal outcome that
    needs the governor (escalation_required, a persistent verifier or test failure, a refute).
    It is never requeued, because re-dispatching it as ready would bypass the escalation the
    wave asked for. `reported_wave_outcome` recovers which outcome it was. `blocked` is checked
    before commit presence: a task that committed and then failed its verifier is still blocked.

    A task with no terminal state and no commit ALSO escalates when the wave recorded a
    `wave outcome:` note for it since its dispatch: the note lands before the state write, so a
    refused or lost state write leaves exactly this shape, and requeueing it would re-dispatch
    an escalation. `requeue` is kept for the genuine wedge only - no terminal state, no commit,
    no outcome note.
    """
    actions: dict[int, str] = {}
    for tid in group_task_ids:
        ts = state.tasks.get(tid)
        st = ts.state if ts else "pending"
        if st == "done":
            actions[tid] = "done"
        elif st == "blocked":
            actions[tid] = "escalate"
        elif st in ("spec_review", "code_review") or tid in committed_task_ids:
            # reporter marked it implemented/reviewed, or a commit exists →
            # orchestrator runs its review gates (single-shot) or finalizes.
            actions[tid] = "needs_review"
        elif reported_wave_outcome(state, tid) is not None:
            actions[tid] = "escalate"  # outcome noted, state write refused or lost
        else:
            actions[tid] = "requeue"  # wedged before reporting terminal state
    return actions


# The decision-log prefix wave_group.js writes (through the wave-event reporter) when a task
# ends in anything but `done`. TaskState has no outcome field, so the note is where the outcome
# survives a lost workflow return. The JS spells this literal too; a test pins the two together.
WAVE_OUTCOME_NOTE_PREFIX = "wave outcome: "

# The outcomes a note may carry: every non-done outcome of the implementer contract, plus the
# token both sides use for anything else. wave_group.js holds the same list (pinned by a test)
# and maps an unlisted outcome to UNKNOWN_WAVE_OUTCOME before writing, so free agent text never
# reaches the shell; this side re-checks, so a garbled note still cannot pass as a known outcome.
UNKNOWN_WAVE_OUTCOME = "unknown_outcome"
WAVE_NOTE_OUTCOMES = frozenset(
    {"blocked", "escalation_required", "test_failure_persistent", "verifier_blocker_persistent"}
)

# The session event the governor appends at every wave dispatch (SKILL Step 7).
_WAVE_DISPATCHED_EVENT = "session_wave_dispatched:"


def _as_utc(value: object) -> datetime | None:
    """An ISO timestamp (string, or the datetime YAML may already have made of it) in UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            _LOG.warning("unparseable timestamp %r; not using it as a dispatch bound", value)
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dispatch_bound(state: StatusState, tid: int) -> datetime | None:
    """When *tid*'s current dispatch began: its `since`, else the latest wave-dispatch event."""
    ts = state.tasks.get(tid)
    bound = _as_utc(ts.since) if ts else None
    if bound is not None:
        return bound
    for d in reversed(state.decisions):
        if d.task == 0 and d.summary.startswith(_WAVE_DISPATCHED_EVENT):
            return _as_utc(d.at)
    return None


def reported_wave_outcome(state: StatusState, tid: int) -> str | None:
    """The outcome the wave noted for *tid* during its current dispatch, or None.

    Only notes at or after the dispatch bound count (`_dispatch_bound`), so an outcome from an
    earlier run of the task cannot escalate this one. With no readable bound, every note counts:
    a false escalation reaches a human, a false requeue bypasses one. The whole token is parsed -
    it must be a listed outcome, alone or followed by a parenthesised detail - and anything else
    reads as UNKNOWN_WAVE_OUTCOME rather than being cut down to a word that looks valid.
    """
    bound = _dispatch_bound(state, tid)
    for d in reversed(state.decisions):
        if d.task != tid or not d.summary.startswith(WAVE_OUTCOME_NOTE_PREFIX):
            continue
        if bound is not None:
            at = _as_utc(d.at)
            if at is None or at < bound:
                continue
        token, _, detail = d.summary[len(WAVE_OUTCOME_NOTE_PREFIX) :].strip().partition(" ")
        if token in WAVE_NOTE_OUTCOMES and (not detail or detail.startswith("(")):
            return token
        return UNKNOWN_WAVE_OUTCOME
    return None


def parse_wave_return(return_items: list[dict]) -> list[dict]:
    """Normalize the workflow's per-task return, applying coerce_wave_outcome to
    every item (defense-in-depth against a hallucinated done-with-blockers).

    Returns a list of dicts with effective_outcome / effective_verdict /
    coercion_reason added, preserving the original fields.
    """
    out: list[dict] = []
    for item in return_items:
        outcome = item.get("outcome", "blocked")
        findings = item.get("verifier_findings_count") or {}
        verdict = item.get("verifier_verdict")
        eff_outcome, eff_verdict, reason = coerce_wave_outcome(outcome, findings, verdict)
        merged = dict(item)
        merged["effective_outcome"] = eff_outcome
        merged["effective_verdict"] = eff_verdict
        merged["coercion_reason"] = reason
        merged["needs_orchestrator_review"] = bool(item.get("needs_orchestrator_review", True))
        out.append(merged)
    return out


_WAVE_TAG_RE = re.compile(r"[A-Za-z0-9._-]+")
_WAVE_SCRIPT_PREFIX = "wave_group_"


def wave_script_dir(data_root, project_name: str) -> Path:
    """Home of the generated per-wave workflow variants for one project:
    `<data_root>/projects/<name>/status/workflows/`. Runtime state carrying that
    project's absolute paths lives with the project's other status files, never in
    the suite tree (docs/DATA-LAYOUT.md)."""
    if not isinstance(project_name, str) or not _WAVE_TAG_RE.fullmatch(project_name):
        # None means the plan has no `project:` and the bootstrap never resolved one;
        # anything with separators would relocate the variant outside the project.
        raise ValueError(
            "wave_script_dir: project_name must be the plan's `project:` slug "
            f"(matching {_WAVE_TAG_RE.pattern!r}), got {project_name!r}"
        )
    return Path(data_root) / "projects" / project_name / "status" / "workflows"


def bake_wave_script(args: dict, skill_dir, tag: str, *, out_dir) -> Path:
    """Workaround for a harness limitation: Workflow(scriptPath=..., args=...) does
    NOT bind the `args` global inside the launched script in this Claude Code build
    (verified 2026-06-24: wave_group.js `const A = args` -> A undefined -> instant
    `A.tasks.map` failure). Workflow scripts also can't read files/env, so args can't
    be side-loaded. Fix: bake the args object in as a literal and launch the variant
    by scriptPath with NO args param (scriptPath itself works; only `args` is unbound).

    The template is read from `<skill_dir>/workflows/wave_group.js` and never
    modified. The variant is written to `<out_dir>/wave_group_<tag>.js` — callers
    pass `wave_script_dir(env.data_root, project.name)`. Writing into the template
    directory (the suite checkout on an installed machine) is refused: the baked
    literal carries a project's absolute paths, which must not land in a tree that
    is meant to be byte-identical across installs.
    """
    import json
    if not _WAVE_TAG_RE.fullmatch(tag):
        # The tag becomes a filename component; a slug carrying separators or '..'
        # would walk out of out_dir and defeat the suite-tree guard below.
        raise ValueError(f"bake_wave_script: tag must match {_WAVE_TAG_RE.pattern!r}, got {tag!r}")
    skill_dir = Path(skill_dir)
    template_dir = skill_dir / "workflows"
    out = Path(out_dir)
    # Refuse the whole installed `skills/` tree (skill_dir's parent), not just this
    # skill: a sibling skill directory is equally the suite tree provision copies.
    if out.resolve().is_relative_to(skill_dir.resolve().parent):
        raise ValueError(
            "bake_wave_script: out_dir is inside the installed skills directory (suite tree); "
            "generated variants belong under the project data_root "
            f"(wave_script_dir(...)), got {out}"
        )
    src = (template_dir / "wave_group.js").read_text(encoding="utf-8")
    needle = "const A = args"
    if src.count(needle) != 1:
        raise RuntimeError(f"bake_wave_script: expected exactly one '{needle}' in wave_group.js")
    # Replace the whole line (keep it robust to the trailing comment).
    lines = src.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.startswith(needle):
            lines[i] = "const A = " + json.dumps(args, ensure_ascii=True) + "  // baked-in args (scriptPath args-global unbound; see bake_wave_script)\n"
            break
    out.mkdir(parents=True, exist_ok=True)
    dst = out / f"{_WAVE_SCRIPT_PREFIX}{tag}.js"
    dst.write_text("".join(lines), encoding="utf-8")
    return dst


def remove_wave_script(path, *, out_dir) -> bool:
    """Delete a baked variant once its wave reached terminal state. Returns True if
    a file was removed, False if it was already gone (idempotent — the reconcile
    wake may run more than once for the same wave).

    `out_dir` is the same `wave_script_dir(...)` the bake used: only a file shaped
    like a variant AND sitting directly in that directory is ever unlinked, so a
    path recomputed on a later wake can never reach outside the project's
    `status/workflows/`."""
    p = Path(path)
    if not (p.name.startswith(_WAVE_SCRIPT_PREFIX) and p.suffix == ".js"):
        raise ValueError(f"remove_wave_script: not a baked wave variant: {p}")
    if p.resolve().parent != Path(out_dir).resolve():
        raise ValueError(f"remove_wave_script: {p} is not directly under {out_dir}")
    if not p.is_file():
        return False
    p.unlink()
    return True
