---
name: tdd-loop
description: Use when implementing a feature or bugfix from a spec/ticket and you want the full red-green-refactor cycle run autonomously with subagent-driven debug and critic review, surfacing only when green+critic-approved or genuinely blocked. Triggers on "tdd loop", "implement this spec via tdd", "run the autonomous tdd cycle", or any request to ship a feature without per-step intervention.
---

# TDD Loop

## Overview

Autonomous orchestrator for strict test-first development. Reads spec → writes failing tests → implements → debugs failures via subagent → runs critic review against the spec → refactors → reports. Hard file-gate prevents drift outside the planned file list. Surface to user only on green+critic-approved or genuine blocker.

**Core principle:** The user is the decision-maker, not the debugger. Failures get fixed by subagents working with the failure output. Drift gets caught by a critic, not by the user reading diffs.

**REQUIRED BACKGROUND:** `../_vendored/test-driven-development/SKILL.md` for the TDD discipline this skill enforces. `../_vendored/dispatching-parallel-agents/SKILL.md` for subagent contracts. `../_vendored/verification-before-completion/SKILL.md` for the green claim.

## When to Use

- Spec or ticket with clear acceptance criteria + a known test framework
- Feature small enough to hold the file list in your head (≤ 5 files)
- User explicitly wants autonomous execution

## When NOT to Use

- Spec is ambiguous → run `../refine/SKILL.md` first (it writes the spec where the epic survey reads it)
- Acceptance criteria undefined → write the spec first, don't guess
- No existing test infrastructure → set it up as a separate task
- Refactors with no behavior change → use a different workflow
- Cross-cutting changes touching > 5 files → decompose first

## The Loop

```dot
digraph tdd_loop {
  rankdir=TB;
  spec [label="1. Read spec\nLock file allowlist" shape=box];
  red [label="2. Write failing tests\nCommit (RED)" shape=box];
  run1 [label="3. Run tests" shape=box];
  red_ok [label="Tests fail as expected?" shape=diamond];
  impl [label="4. Implement minimum code\nVerify file allowlist" shape=box];
  run2 [label="5. Run tests" shape=box];
  green [label="Green?" shape=diamond];
  debug [label="6. Dispatch debug subagent\nwith failure output" shape=box];
  attempts [label="< 2 debug attempts?" shape=diamond];
  critic [label="7. Dispatch critic subagent\nspec compliance + quality" shape=box];
  approved [label="Critic approves?" shape=diamond];
  refactor [label="8. Refactor pass\ntests stay green" shape=box];
  done [label="9. Surface to user\ngreen + critic-approved" shape=doublecircle];
  blocked [label="Surface BLOCKED\nwith full context" shape=doublecircle];

  spec -> red -> run1 -> red_ok;
  red_ok -> impl [label="yes"];
  red_ok -> blocked [label="no — bad test"];
  impl -> run2 -> green;
  green -> critic [label="yes"];
  green -> debug [label="no"];
  debug -> attempts;
  attempts -> run2 [label="yes"];
  attempts -> blocked [label="no — escalate"];
  critic -> approved;
  approved -> refactor [label="needs work"];
  approved -> done [label="yes"];
  refactor -> run2;
}
```

## Step Detail

### 1. Read spec, lock file allowlist

- Read the spec/ticket fully. Extract acceptance criteria as a bullet list.
- Determine the file list — every path you may modify (existing or new), relative to the repo root. New files may be marked with a trailing ` (new)`; the gate strips it.
- From the repo root, hide the loop's state directory from git through `info/exclude`, never `.gitignore` (editing a tracked ignore file is itself an out-of-allowlist change):

  ```bash
  exclude="$(git rev-parse --git-path info/exclude)"
  mkdir -p .tdd-loop "$(dirname "$exclude")"
  grep -qxF '.tdd-loop/' "$exclude" 2>/dev/null || echo '.tdd-loop/' >> "$exclude"
  ```

- Write the allowlist to `.tdd-loop/allowlist.txt`. One path per line.
- Confirm test command for this codebase (e.g. `pytest`, `npm test`, `cargo test`).

### 2. Write failing tests first (RED)

- Tests express acceptance criteria, one test per criterion minimum.
- Tests reference the not-yet-existing function/module signatures from the spec.
- Cut a branch `tdd/<slug>` from `<base-ref>` and commit with message `red: failing tests for <feature>`.
- `<base-ref>` is the ref `tdd/<slug>` was cut from, and every later step uses the same one: `origin/<base-branch>` when the repo has that remote-tracking ref, else the local `<base-branch>`. `<base-branch>` is `canonical_repo.base_branch` off the resolved block on the configured project, else the branch you were on when you cut `tdd/<slug>`. Never assume `main`.

### 3. Run tests — must fail for the right reason

- New tests must fail with `NotImplementedError`, `ImportError`, `AssertionError` on missing behavior — NOT a syntax error or test framework misconfiguration.
- If tests don't fail, or fail for the wrong reason → STOP, surface BLOCKED.

### 4. Implement minimum code

- Smallest change that could plausibly turn tests green. No speculation, no premature abstraction.
- After editing, run the file gate from the repo root (see File Gate below) — it exits non-zero if any out-of-allowlist file changed on the branch:

  ```bash
  bash "<suite_root>/skills/tdd-loop/file-gate.sh" .tdd-loop/allowlist.txt <base-ref>
  ```

### 5. Run tests

- All previously-green tests must still pass.
- All RED tests must turn green.
- If green → step 7. If failing → step 6.

### 6. Debug via subagent (max 2 attempts)

- REQUIRED: use `../_vendored/dispatching-parallel-agents/SKILL.md` to dispatch a debug subagent.
- Hand the subagent: the failing test output (verbatim), the relevant source files, the allowlist, the spec.
- Subagent's instructions: diagnose root cause, propose minimal fix, modify only allowlist files, return the diff.
- After subagent returns: re-run tests + file-gate. If still failing, dispatch ONE more debug attempt with both failure outputs in context.
- After 2 failed attempts: STOP, surface BLOCKED with full debug history.

### 7. Critic subagent

- REQUIRED: use the prompt at `<suite_root>/skills/tdd-loop/critic-prompt.md`.
- Hand the critic: the spec, the full diff (`git diff <base-ref>...HEAD`, with the `<base-ref>` from Step 2), the test output, the allowlist.
- Critic returns: APPROVED, or NEEDS WORK with specific issues (drift from spec, missing edge cases, quality problems).

### 8. Refactor (only if critic flagged issues)

- Address each critic issue. Re-run tests after every change — they must stay green.
- Re-run file-gate.
- Send refactored diff back to the critic. Repeat until APPROVED or 2 critic rounds (then surface BLOCKED).

### 9. Surface to user

Report includes:
- Branch name and commit list
- Test output (all green)
- Critic's APPROVED report
- Full diff
- Shipped checklist: pushed? deployed? verified live? docs updated?

## File Gate

Run after every implementation or refactor edit:

```bash
bash "<suite_root>/skills/tdd-loop/file-gate.sh" .tdd-loop/allowlist.txt <base-ref>
```

The script checks the branch's whole change set against the allowlist: the diff from the merge-base of `<base-ref>` and `HEAD` to the working tree and to the index (so committed, staged and unstaged edits all count), plus untracked files that are not ignored. It diffs with `--no-renames`, so a rename counts as both its deleted and its added path. Every path is compared repo-root-relative, so it gives the same verdict from any subdirectory. Anything under `.tdd-loop/` never counts, nor does the allowlist file itself while it is untracked; a committed allowlist is a branch change like any other. Allowlist lines may carry whole-line `#` comments or inline ` # ...` comments. Exit 0 if every changed file is in the allowlist; exit 1 with the offending paths otherwise.

How it learns the base: the second argument, which is the `<base-ref>` from Step 2. Omitted, it falls back to `origin/HEAD`. If that is unset, the ref does not resolve, or it names the current branch or that branch's own remote-tracking copy (`<remote>/tdd/<slug>`), the gate exits 2 and says why, because an empty branch scope would pass every committed edit. The branch's upstream is a valid base: cutting `tdd/<slug>` from `origin/<base-branch>` makes that its upstream. The guard compares ref names, not commits, since at the start of a loop the base and `HEAD` are legitimately the same commit; a raw SHA or another spelling of the branch's own tip is not caught and passes vacuously, so always pass the `<base-ref>` from Step 2.

If the gate fails, the loop STOPS — do not commit, do not proceed. Either:
- Revert the out-of-scope edit (preferred)
- Surface to user that the spec requires a wider allowlist (acceptable)

Never silently expand the allowlist mid-loop.

## Surface BLOCKED When

- RED test fails for the wrong reason (framework, import, syntax)
- 2 debug subagent attempts failed
- 2 critic rounds didn't reach APPROVED
- File gate would require expanding allowlist beyond the spec
- Acceptance criteria are ambiguous in a way that makes "done" undefined
- Any infrastructure failure (test runner won't start, deps missing)

When BLOCKED: report what was tried, what failed, and the recommended next decision the user needs to make.

## Stop Conditions (do not surface as success)

- Tests green but file gate failed → not done
- Tests green but critic flagged drift → not done
- Tests green but you skipped a debug or critic step → not done

Green + critic-approved + file-gate-clean is the only "done."

## Common Failures

| Failure | Cause | Mitigation |
|---------|-------|------------|
| Tests pass on first run | Wrote weak tests, or implementation snuck in | Tests MUST fail first — verify RED commit |
| Subagent expands allowlist | Didn't enforce file-gate in subagent contract | Make file-gate verification a required step in subagent prompt |
| Critic rubber-stamps | Critic prompt too vague | Use the structured critic prompt; don't paraphrase it |
| Loop runs forever | No attempt cap | Hard cap: 2 debug attempts, 2 critic rounds, then surface |
| Refactor breaks tests | Refactor without re-running tests | Re-run tests after every refactor edit, before next change |

## Project Defaults

When running on the configured project:
- Test command: the project's suite (e.g. `pytest` for a Python backend, `npm test` for a frontend) — from `test_commands` / project config
- Branch convention: `tdd/<slug>` off `<base-ref>` (Step 2), whose branch is `canonical_repo.base_branch`
- Push: REQUIRED at end — verify with `git log origin/<branch>`
- Deploy: only after PR merge — TDD loop produces the PR, doesn't deploy
- Lint: the repo's linter (e.g. `ruff check`) on touched files before commit
