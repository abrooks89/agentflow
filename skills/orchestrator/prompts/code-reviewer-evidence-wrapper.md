# Code-Quality-Reviewer Evidence Wrapper

## Worktree — the one tree every command below runs against

**Worktree:** `{worktree}`

Every git call you make names it explicitly:
`git -C {worktree} diff <base>..<head>`, `git -C {worktree} show <sha>`, `git -C {worktree} log`.
Every **repository** path you read is under `{worktree}`. This does NOT cover the vendored
prompt in Step 1: `CODE_REVIEWER_PATH` is deliberately SKILL-relative (`../../_vendored/...`), it ships
with the suite rather than with the code under review, and it is read exactly as written.

**The upstream prompt at `CODE_REVIEWER_PATH` writes those commands bare** (`git diff
[BASE_SHA]..[HEAD_SHA]`, and similar). Substitute this path into each one before you run it — that
substitution is part of following the protocol, not an optional adaptation.

**If `{worktree}` is still an unsubstituted placeholder when you read this, STOP.** Do not run a
bare `git` to find "the" repo. Return the reviewer contract's own field — `"verdict": "blocker"` (Step 3's JSON shape),
with the reason `"worktree unresolved: dispatch supplied no path"`. **Not `outcome`** — that
belongs to the implementer wave contract, and `coerce_review_verdict()` reads only `verdict`,
so an `outcome` here coerces to the empty string: never the ✅ the dispatch loops on and never
the `inconclusive` that trips the two-attempt escalation. The loop would spin forever on a
reviewer that correctly refused to guess. A reviewer session is launched from a directory
that is deliberately not a repository, so a bare `git diff` there resolves against no repo — or
against whatever tree the process inherited — and **returns a clean verdict over a diff nobody
wrote.** This is the last gate before merge; a vacuous pass here ships unreviewed code.

## Step 1 — Locate the upstream code-quality-reviewer prompt

Use the vendored code-quality-reviewer prompt shipped alongside this suite as `CODE_REVIEWER_PATH`:

```
../../_vendored/review-prompts/code-quality-reviewer-prompt.md
```

This path is guaranteed present post-install (it is vendored into the suite), so there is no
lookup or fallback discovery step.

## Step 2 — Run the upstream code-quality-reviewer protocol

Execute the code-quality-reviewer protocol from `$CODE_REVIEWER_PATH` verbatim, applying it to
the current task's implementation.

> **Fallback (if the upstream prompt cannot be loaded at runtime):** Perform code quality
> review yourself using the task's diff. Check for clean responsibilities, testability,
> file structure adherence, and file growth as described in the upstream prompt's checklist.
> Then continue to Step 3 to produce the required JSON block.

## Step 3 — Required output extension

**On top of the upstream code-quality-reviewer's normal output**, return a final JSON code block
with the following shape:

```json
{
  "verdict": "clean | warnings | blocker",
  "finding_class": "code_quality",
  "evidence": "<EvidenceBlock — see agentflow.subagent_contract for field definitions>",
  "checklist": "<list[ChecklistItem] — see agentflow.subagent_contract for field definitions>"
}
```

Schema reference: `agentflow.subagent_contract` (classes `EvidenceBlock` and `ChecklistItem`).
Do **not** redefine the schema here — `subagent_contract.py` is the single source of truth.

**`finding_class` MUST be `"code_quality"`** — this distinguishes your findings from
spec-compliance findings (which carry `finding_class: "spec_compliance"` from the
spec-reviewer-evidence-wrapper). The orchestrator's `coerce_review_verdict()` uses this field
to route blocker classes correctly: spec-compliance blockers prevent merge approval;
code-quality blockers trigger a targeted refactor loop rather than a spec re-dispatch.

The verdict will be passed through `coerce_review_verdict()` from that module; untrustworthy
`clean` verdicts (missing evidence, empty `stdout_tail`, empty or unverified checklist) will
be automatically demoted. See `coerce_review_verdict`'s docstring for the exact demotion rules.
