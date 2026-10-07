"""Pins which tree the orchestrator's reconcile step reads for a task's change.

With `per_wave_worktrees: true`, a single-shot task commits on its wave branch in a sibling
worktree, and that commit reaches the epic branch only through the merge-back that runs after the
reviewers approve. So the reviewers must be pointed at the wave tree, and the commit lookup must
read the wave branch; the epic tree does not hold the change yet. With the flag off, the task
builds in the epic tree and both still read it.

Best-of-N tasks land inside the workflow instead; the SKILL must say that the landing result is
read, that a failed merge-back blocks the task, and that the winner's tree is removed only after a
successful one.

These are prose rules a governor follows by reading them, so this module asserts the sentences
are present. It cannot prove a governor follows them.
"""

from __future__ import annotations

import re
from pathlib import Path

ORCH = Path(__file__).resolve().parents[2] / "orchestrator"
SKILL = ORCH / "SKILL.md"
WRAPPERS = (
    ORCH / "prompts" / "spec-reviewer-evidence-wrapper.md",
    ORCH / "prompts" / "code-reviewer-evidence-wrapper.md",
)


def _flat(text: str) -> str:
    """Whitespace-collapsed, so a re-wrap of a paragraph does not fail the pin."""
    return re.sub(r"\s+", " ", text)


def _step7() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("### Step 7: Dispatch ready tasks")
    end = text.index("**Wave auto-mode contract:**", start)
    return _flat(text[start:end])


def _review_gates() -> str:
    s = _step7()
    start = s.index("**Orchestrator review gates (single-shot reconcile path only):**")
    return s[start:]


def test_reviewers_read_the_wave_tree_when_per_wave_worktrees_is_on():
    gates = _review_gates()
    for role in ("spec-reviewer-evidence-wrapper", "code-quality-reviewer-evidence-wrapper"):
        i = gates.index(role)
        clause = gates[i : i + 600]
        assert (
            "substituting `{worktree}` with `state.tasks[tid].wave_worktree_path` when "
            "`plan.per_wave_worktrees` is True" in clause
        ), f"{role}: the per-wave substitution rule is not stated"
        assert "and with `<worktree>` when it is False" in clause, (
            f"{role}: the non-per-wave path must still substitute the epic worktree"
        )


def test_the_review_gates_say_why_the_epic_tree_is_wrong_for_a_per_wave_task():
    assert (
        "the task's commit sits on its wave branch in that tree until the merge-back below runs "
        "on approval" in _review_gates()
    )


def test_committed_task_ids_read_the_wave_branch_when_per_wave_worktrees_is_on():
    s = _step7()
    assert (
        "`git -C <worktree> log <state.tasks[tid].wave_branch> <epic_branch> --since=<state.tasks[tid].since>`"
        in s
    )
    assert (
        "When `plan.per_wave_worktrees` is False: `git -C <worktree> log --since=<state.tasks[tid].since>`"
        in s
    )
    # The old unconditional rule read only the epic tree's branch.
    assert (
        "Compute `committed_task_ids` from `git -C <worktree> log --since=<state.tasks[tid].since>` (commit-signature match), as today."
        not in s
    )


def test_each_reviewer_wrapper_names_the_per_wave_tree():
    for path in WRAPPERS:
        text = _flat(path.read_text(encoding="utf-8"))
        assert (
            "the task's per-wave worktree when the epic builds each task in its own tree" in text
        ), path.name
        assert "otherwise the epic worktree" in text, path.name


def test_best_of_n_landing_is_read_and_a_failed_merge_back_blocks():
    s = _step7()
    for phrase in (
        "reads its result `{merge_exit_code, success, stderr}`",
        "only on exit 0 with `success: true`",
        "removes the winner's worktree and branch",
        "keeps the winner's tree",
        "reports the task `blocked` with a `wave outcome: blocked (...)` note that carries merge-back's stderr",
        "`cleanup_failures`",
    ):
        assert phrase in s, f"best-of-N landing rule missing: {phrase!r}"
