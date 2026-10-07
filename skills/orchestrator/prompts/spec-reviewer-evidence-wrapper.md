# Spec-Reviewer Evidence Wrapper

## Worktree — the one tree every command below runs against

**Worktree:** `{worktree}`

This is the tree that has the task's change checked out: the task's per-wave worktree when the
epic builds each task in its own tree, otherwise the epic worktree. On the per-wave path the
task's commit stays on its wave branch, in that tree, until merge-back after your approval; the
epic tree does not contain the change yet. So file reads and test runs go to this tree and no
other: the epic tree would show you pre-change code.

Every git call you make names it explicitly:
`git -C {worktree} diff <base>..<head>`, `git -C {worktree} show <sha>`, `git -C {worktree} log`.
Every **repository** path you read is under `{worktree}`. This does NOT cover the vendored
prompt in Step 1: `SPEC_REVIEWER_PATH` is deliberately SKILL-relative (`../../_vendored/...`), it ships
with the suite rather than with the code under review, and it is read exactly as written.

**The upstream prompt at `SPEC_REVIEWER_PATH` writes those commands bare** and mandates "verify by
reading code, not by trusting report" — so you must resolve a tree, and this is the tree.
Substitute this path into each command before running it.

**If `{worktree}` is still an unsubstituted placeholder when you read this, STOP.** Do not run a
bare `git` to find "the" repo. Return the reviewer contract's own field — `"verdict": "blocker"` (Step 3's JSON shape),
with the reason `"worktree unresolved: dispatch supplied no path"`. **Not `outcome`** — that
belongs to the implementer wave contract, and `coerce_review_verdict()` reads only `verdict`,
so an `outcome` here coerces to the empty string: never the ✅ the dispatch loops on and never
the `inconclusive` that trips the two-attempt escalation. The loop would spin forever on a
reviewer that correctly refused to guess. A reviewer session is launched from a directory
that is deliberately not a repository, so reading "the code" without a stated tree either fails or
silently reads a tree nobody chose — and a verdict from either is worthless while looking identical
to a real one.

## Step 1 — Locate the upstream spec-reviewer prompt

Use the vendored spec-reviewer prompt shipped alongside this suite as `SPEC_REVIEWER_PATH`:

```
../../_vendored/review-prompts/spec-reviewer-prompt.md
```

This path is guaranteed present post-install (it is vendored into the suite), so there is no
lookup or fallback discovery step.

## Step 2 — Run the upstream spec-reviewer protocol

Execute the spec-reviewer protocol from `$SPEC_REVIEWER_PATH` verbatim, applying it to the
current task's specification and implementation.

> **Fallback (if the upstream prompt cannot be loaded at runtime):** Perform spec compliance
> review yourself using the task's `verification.acceptance_criteria` as the spec. Check each
> criterion mechanically and record pass/fail. Then continue to Step 3 to produce the required
> JSON block.

## Step 3 — Required output extension

**On top of the upstream spec-reviewer's normal output**, return a final JSON code block
with the following shape:

```json
{
  "verdict": "clean | warnings | blocker",
  "evidence": "<EvidenceBlock — see agentflow.subagent_contract for field definitions>",
  "checklist": "<list[ChecklistItem] — see agentflow.subagent_contract for field definitions>"
}
```

Schema reference: `agentflow.subagent_contract` (classes `EvidenceBlock` and `ChecklistItem`).
Do **not** redefine the schema here — `subagent_contract.py` is the single source of truth.

The verdict will be passed through `coerce_review_verdict()` from that module; untrustworthy
`clean` verdicts (missing evidence, empty `stdout_tail`, empty or unverified checklist) will
be automatically demoted. See `coerce_review_verdict`'s docstring for the exact demotion rules.
