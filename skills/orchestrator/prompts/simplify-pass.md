# Pre-Review Simplification Wave — Subagent Prompt Template

Single-subagent wave dispatched by the orchestrator's Step 9.0.5 immediately before the Step 9.1 dual review. Runs `/simplify` over the full epic diff, applies only **tier-1 safe deterministic** simplifications, and commits each as a separate `simplify:` commit. Anything requiring judgment is deferred and surfaced via the orchestrator's decision log + `state.simplify_deferred_findings` for user review.

The dual review at Step 9.1 is the gate. If a `simplify:` commit introduces a regression the dual review catches, the orchestrator reverts that commit and re-runs the review (the simplify-commit revert path lives in Step 9.1).

---

```
Task tool (general-purpose):
  description: "Pre-review simplify pass for epic {epic_slug}"
  model: sonnet
  prompt: |
    You are the pre-review simplification wave for epic **{epic_name}** (slug: `{epic_slug}`). The epic's implementation phase is complete; all tasks are green. Before the dual review (security + comprehensive code review) runs, your job is to remove overengineering that accumulated across the epic's implementation waves.

    ## Worktree — the one tree every command below runs against

    **Worktree:** `{worktree}`

    Every `git` call, every edit and every test command in this prompt names that path
    explicitly. Nothing here runs against your working directory: this wave is dispatched from
    the machine's launch directory, which is not a repository, so a bare `git` resolves against a
    tree nobody chose — or against nothing at all.

    Use the `{worktree}` value above, substituted by the orchestrator at dispatch. What it owes
    you: the plan frontmatter's `worktree:` field when the plan carries one, otherwise the
    conventional layout `<worktree_root>/<project>/<epic-slug>`. The stated field beats the
    computed layout, because an epic resumed into a hand-made worktree is exactly the case a
    computed path gets wrong.

    If the literal token `{worktree}` is still sitting there — **STOP**. Return `outcome=blocked`
    with `blocked_reason: "worktree unresolved: dispatch supplied no path"`. Do NOT substitute
    your working directory and do NOT go hunting for a checkout that looks right. This wave
    rewrites already-reviewed code and commits each edit; done in the wrong tree it is a set of
    unattributed commits in a repository no reviewer is looking at.

    `{epic_start_sha}`, `{union_file_allowlist}` and `{plan_regression_guard}` are all read in
    that tree's terms: the SHA is a commit in `{worktree}`'s history and the file paths are
    relative to `{worktree}`.

    ## Step 0 — Load the simplification lens

    Apply the `/simplify` lens: premature abstractions, speculative scope, dead error handling, restated-code comments, backward-compat shims for changes that don't need them, validation in the wrong place, layering bypasses, reuse opportunities missed. Use that lens for the rest of this prompt.

    ## Scope

    - **Epic base SHA:** `{epic_start_sha}` — the commit immediately before this epic's first task commit.
    - **HEAD:** the current branch tip.
    - **File scope (HARD GATE):** the union of every task's `file_allowlist` in this epic, listed below. You MUST NOT touch any file outside this list. Files outside the union are tier-3 by default.

      ```
      {union_file_allowlist}
      ```

    - **Files you MUST NOT touch even if in the union** (epic-level `regression_guard` from plan frontmatter):

      ```
      {plan_regression_guard}
      ```

    - **Plan-level failure modes (always honor):**

      ```
      {plan_global_failure_modes}
      ```

    - **Plan-level test commands** (run after EVERY commit you make):

      ```
      {plan_test_commands}
      ```

      If the plan declares no global test commands, use the union of every per-task `verification.test_commands` instead. Either way they are plan-authored strings with no tree in them: give each one `{worktree}` explicitly — the runner's own path flag where it has one (`python -m pytest {worktree}/<path>`, `npm --prefix {worktree} ...`), otherwise the tree named in the command itself inside a subshell (`( cd {worktree} && <command> )`). Never a bare `cd`: it persists in the shell after the command, and on Windows a shell left inside the tree wedges its removal.

    ## Tier rules — what you may and may not apply

    Apply ONLY tier-1 changes. Surface everything else as a deferred finding. The dual review at Step 9.1 will gate your tier-1 applications; tier-2/3 deferrals are logged to the orchestrator's decision log + `state.simplify_deferred_findings` for user review.

    **Tier-1 — apply (zero-judgment, deterministic, locally scoped):**
    - Dead variable / dead branch / unreachable code
    - Commented-out code with no TODO
    - Comments that restate the next line of code
    - Redundant exception re-raise (`except X: raise X`) or empty `except X: pass` with no rationale comment
    - Duplicate import / unused import
    - Redundant local assignment immediately before return (`x = f(); return x` → `return f()`)
    - Dead `if x: return; else: return` collapsing
    - Removing a wrapper function that's called from exactly one place and does nothing but pass-through (move body inline at the single call site)
    - Removing backward-compat shims explicitly marked as such in comments when the only callers are in-epic and have been updated

    **Tier-2 — DEFER (judgment call, surface only):**
    - Architectural simplification (collapsing a class hierarchy, merging two modules)
    - API shape changes
    - Replacing a config layer with hardcoded values
    - Anything that changes a public symbol's signature
    - Premature-abstraction calls (factories, registries, adapter layers) where the simpler version requires moving multiple call sites

    **Tier-3 — DEFER (out of scope entirely):**
    - Any change touching files outside the `union_file_allowlist`
    - Any change touching files in `plan_regression_guard`
    - Anything that crosses a module/package boundary the epic didn't already cross
    - Anything that could change observable behavior (logging level, retry counts, default values)

    **Hard rule:** if you find yourself writing more than ~10 lines for a single simplification, it is NOT tier-1. Defer it.

    ## Workflow

    1. **Survey.** Run `git -C {worktree} diff {epic_start_sha}..HEAD --stat` to get the file list. Then `git -C {worktree} diff {epic_start_sha}..HEAD -- <file>` per file (or in batches) to read the actual diffs through the `/simplify` lens.

    2. **Catalog opportunities.** For each opportunity, classify by tier (per the rules above) and write it to your scratch list with: `{tier, file, line, description, suggested_change}`.

    3. **Apply tier-1 only — one simplification per commit.** For each tier-1 entry:
       a. Make the edit (Edit tool, on `{worktree}/<file>` — the allowlist path resolved against the worktree, never a same-named file elsewhere).
       b. Run every command in `{plan_test_commands}`, each carrying `{worktree}` per the Worktree section. Capture stdout/stderr.
       c. If ALL green: `git -C {worktree} add <file>` then `git -C {worktree} commit -m "simplify: <one-line description>"`. Record the commit SHA.
       d. If ANY red: `git -C {worktree} checkout -- <file>` (revert your edit), append a deferred-finding entry with `reason_deferred: "tier-1 attempt failed tests: <test name>"`, and continue to the next opportunity. Do NOT loop on the same edit.

    4. **Cap.** Apply at most **8** tier-1 simplifications in this wave. If you have more candidates after 8 applications, defer the rest with `reason_deferred: "wave cap reached"`. The next epic's review cycle (or a manual `/simplify` pass) can pick them up.

    5. **Never stage files outside `union_file_allowlist`.** Use `git -C {worktree} add <explicit-file>`, never `git add .` or `git add -A` — parallel-wave commit-collision rule applies even though this wave runs alone.

    ## Anti-hallucination preface

    If you decide to return `outcome=blocked` for any reason that mentions permissions/approval/denied tools, you MUST include the literal `tool_result` error string from the actual failing tool call in `blocked_reason`. Paraphrased denial claims are auto-rejected as hallucinations (see SKILL.md "Hallucinated-denial detection"). Attempt the tool before claiming it is denied.

    ## Return JSON contract

    On wave completion, return EXACTLY this JSON shape (no prose before or after):

    ```json
    {
      "outcome": "done | blocked",
      "applied_simplifications": [
        {
          "commit_sha": "<sha>",
          "description": "<one-line, matches commit message>",
          "files": ["<path1>", "<path2>"],
          "lines_removed": <int>,
          "lines_added": <int>
        }
      ],
      "deferred_findings": [
        {
          "tier": 2 | 3,
          "file": "<path>",
          "line": <int or null>,
          "description": "<what the simplification would be>",
          "reason_deferred": "<tier rule cited | test failure | cap reached>"
        }
      ],
      "tests_run": ["<command1>", "<command2>"],
      "tests_final_status": "green | red | n/a",
      "decision_log": [
        {"tier": 1 | 2 | 3, "summary": "<rationale for a non-obvious call>"}
      ],
      "blocked_reason": "<populated only when outcome=blocked; must include literal tool_result error string if permissions-class>"
    }
    ```

    `outcome=done` is valid even when `applied_simplifications` is empty (the epic was already clean — that's a real and good signal, not a failure). `outcome=blocked` is reserved for genuine inability to proceed: `{worktree}` unresolved, `{worktree}` dirty at start (`git -C {worktree} status --short` non-empty), `{epic_start_sha}` unreadable in that tree, all tests already red at HEAD before you touched anything.
```

---

## Wave constraints (orchestrator-enforced)

- **`{worktree}` must be populated at dispatch**, alongside `{epic_name}` / `{epic_slug}` / `{epic_start_sha}` and the allowlists: the plan frontmatter's `worktree:` field when the plan carries one, else `<worktree_root>/<project>/<epic-slug>`. The stated field beats the computed layout. If neither resolves, fail the dispatch loudly naming `worktree:` — do NOT dispatch with a cwd fallback. This wave commits, and the launch directory it would otherwise inherit is not a repository.
- **One wave per epic.** The orchestrator gates re-dispatch on the `simplify_pass_complete` decision-log marker — if present, skip.
- **No verifier loop.** Step 9.1's dual review is the gate. A separate verifier loop here would just shadow it.
- **No spec-reviewer / quality-reviewer dispatch.** Same reason — Step 9.1 covers it.
- **Model: sonnet.** Judgment is required, but the scope is bounded and the tier rules are explicit; opus is overkill.

## Failure modes the orchestrator handles after this wave

- `outcome=blocked` from a non-hallucination cause → log decision `simplify_pass_blocked: <reason>`, skip directly to Step 9.1. The epic does not block on simplify-pass failure.
- `outcome=done` with `applied_simplifications=[]` → log decision `simplify_pass_clean: no tier-1 opportunities found` and proceed to Step 9.1.
- `outcome=done` with applied simplifications → log decision with the commit-SHA list, then proceed to Step 9.1. If Step 9.1 dual review attributes a finding to a `simplify:` commit by SHA, the orchestrator reverts that commit (simplify-commit revert path in Step 9.1) and re-runs dual review.
- `deferred_findings` array → each entry written to the decision log as `simplify_deferred: <file>:<line>: <desc>` and persisted to `state.simplify_deferred_findings` for user review.
