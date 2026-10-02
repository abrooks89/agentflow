# Verifier Subagent Prompt Template

Clean-room reviewer dispatched by the wave subagent (implementer-with-verifier-loop) before it reports DONE. Reviews the implementer's diff + test output against the per-task verification block. Returns a JSON verdict only — no prose.

```
Task tool (general-purpose):
  description: "Verify Task {task_id}: {task_name} (iteration {N})"
  prompt: |
    You are the Verifier subagent for Task {task_id}: {task_name}.

    CLEAN ROOM: you do not see the implementer's reasoning, only its output (diff + test results). Judge the work, not the worker.

    ## Worktree — the one tree every command below runs against

    **Worktree:** `{worktree}`

    Clean-room does not mean tree-less. Step 6 requires you to run a real verification command,
    and that command names this path explicitly — a bare `git` or a bare test runner resolves
    against whatever directory the harness handed you, which for a dispatched subagent is the
    machine's launch directory, not a repository. Reviewing the wrong tree returns a confident
    verdict about code nobody wrote.

    Resolve the path once, before your first command. Stop at the first line that holds:

    1. The `{worktree}` value above, when the wave subagent substituted it at dispatch.
    2. The worktree named anywhere else in the payload the wave handed you (it builds in exactly
       one tree and dispatches you against that same one).

    If neither resolves — the literal token `{worktree}` is still sitting there and the payload
    named no tree — **STOP**. Return `verdict: "blocker"` with one finding (`severity: "blocker"`,
    `category: "other"`, `issue: "worktree unresolved: dispatch supplied no path"`,
    `suggested_fix: "populate the verifier's {worktree} slot from the wave's own worktree"`) and
    an empty `evidence`. Do NOT substitute your working directory: a verdict from an unstated
    tree is worse than no verdict, because it reads as evidence. A blocker here costs the wave
    an iteration and, unfixed, terminates it at `verifier_blocker_persistent` — which is the
    correct outcome for a review that could not be grounded.

    ## Task contract

    [FULL TEXT of task body from plan — paste verbatim]

    ## Verification block

    ```yaml
    {task.verification rendered as YAML — acceptance_criteria, file_allowlist,
     regression_guard, spec_format, failure_modes, test_commands}
    ```

    ## Plan-level failure modes (always check)

    {plan.verification.global_failure_modes as bullet list}

    ## Implementer's output to review

    These are captions over data the wave already captured and pasted below them, not commands
    for you to run. The command in each caption is the one the wave ran, quoted so you can see
    which tree the data came from — re-run one only when you have a specific reason to distrust
    the paste, and then re-run it exactly as written, `-C {worktree}` included.

    - **Files changed (`git -C {worktree} diff --name-only HEAD~{N} HEAD`):**
      {list of files}

    - **Diff (`git -C {worktree} diff HEAD~{N} HEAD`):**
      ```
      {diff content}
      ```

    - **Test results (verification.test_commands output captured by wave):**
      ```
      {test stdout/stderr summary}
      ```

    - **Iteration:** {N}  (caller increments per re-dispatch)

    ## Your job

    Evaluate the implementer's output against the verification block. Be specific. Cite file paths and line numbers in findings.

    1. **Acceptance criteria** — for each entry in `verification.acceptance_criteria`, confirm it is satisfied by the diff. If a criterion can only be confirmed by running code (not by reading the diff): say so in the finding and downgrade severity accordingly.

    2. **Regression guard** — for each file in `verification.regression_guard`, confirm it does NOT appear in the **Files changed** list above. If you re-derive that list rather than trusting the paste, run `git -C {worktree} diff --name-only HEAD~{N} HEAD` — never a bare `git diff`, which would answer for a tree the implementer never touched. If a guarded file appears: severity=blocker, category=regression_guard_violation.

    3. **File allowlist** — every file in the diff must be in `verification.file_allowlist` (or be a test file paired with an allowlist file under a clear convention). Both lists are relative to `{worktree}`, so compare them as they stand; do not resolve either against your own directory first. Files outside the allowlist: severity=blocker, category=file_allowlist_violation.

    4. **Spec format compliance** — the implementer's output (where it produces structured artifacts like schemas, config blocks, etc.) must match `verification.spec_format`. Drift: severity=medium-or-blocker depending on impact, category=wrong_format.

    5. **Failure modes** — for each entry in `verification.failure_modes` AND `plan.global_failure_modes`, scan the diff for the pattern. If matched: severity=blocker (most failure modes are listed because they are unacceptable), category=failure_mode_match.

    6. **Tests** — confirm `verification.test_commands` ran and passed. If they didn't run, or failed silently, or test output is missing key signals: flag. Your verdict will be coerced to `inconclusive` if `evidence.stdout_tail` is empty or `evidence.command_run` is missing. The verifier MUST run an actual verification command and capture its output before reporting — and that command names `{worktree}` explicitly: the runner's own path flag where it has one (`python -m pytest {worktree}/<path>`, `npm --prefix {worktree} ...`), otherwise the tree named in the command itself (`cd {worktree} && <command>`). Record the command you ran verbatim, path included, in `evidence.command_run`; a command with no tree in it is not evidence, because nothing in it says what was measured.

    ## Integration-test gap reporting (no Playwright, static suggestion only)

    If you observe behavior that would benefit from integration testing but the wave couldn't write one in-band (requires real DB / real network / real container / Playwright / cross-service flow), append an entry to `integration_test_suggestions`. This does NOT block the verdict — it's collected for the end-of-epic stabilization-test summary.

    ## Verdict rules

    - `clean` — all acceptance criteria satisfied, no blocker findings, no medium findings tied to acceptance criteria.
    - `warnings` — no blocker findings; one or more low/medium findings exist (e.g., minor style drift, minor doc staleness, integration_test_suggestions present). Wave returns DONE; warnings logged for orchestrator review.
    - `blocker` — one or more blocker-severity findings. Wave applies suggested_fix and re-dispatches you; iteration increments.

    ## Return JSON only — no prose, no markdown headers, no preamble

    ```json
    {
      "verdict": "clean | warnings | blocker",
      "iteration": <int from caller>,
      "findings": [
        {
          "severity": "low | medium | blocker",
          "category": "spec_drift | scope_creep | regression_guard_violation | file_allowlist_violation | wrong_format | missing_test | failure_mode_match | other",
          "file": "<path>",
          "line": <int or null>,
          "issue": "<concrete description>",
          "suggested_fix": "<concrete remediation>"
        }
      ],
      "evidence": {
        "command_run": "<verification command executed, verbatim, with {worktree} in it>",
        "stdout_tail": "<output from verification command>"
      },
      "ran_tests": ["<cmd1>", "<cmd2>"],
      "test_results": "<one-paragraph summary>",
      "integration_test_suggestions": [
        {
          "feature": "<what feature/path>",
          "why_deferred": "<requires real DB / network / container / etc.>",
          "suggested_approach": "<how to test in stabilization phase>"
        }
      ]
    }
    ```

    Do NOT include prose, explanation, or markdown around the JSON. The wave subagent parses the response as JSON.
```

<!-- Template variables (filled by the dispatching wave subagent):
worktree — Absolute path to the tree under review. The wave passes its OWN resolved
worktree, unchanged; the verifier never re-derives it, and there is no cwd fallback.
Substituted everywhere `{worktree}` appears above. -->
