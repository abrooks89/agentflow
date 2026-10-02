{{WORKTREE_CONTEXT_BLOCK}}

# Wave Subagent Prompt Template — Implementer with TDD Loop + Verifier

Used for tasks declared with `type: tdd` in plan frontmatter. Combines the `tdd-loop` skill's red-green-refactor discipline with the wave-with-verifier loop, returning the same JSON contract as `implementer-with-verifier-loop.md` so orchestrator branching logic is unchanged.

When to dispatch this prompt instead of `implementer-with-verifier-loop.md`:
- `task.task_type == "tdd"` (parsed from `type:` field in plan frontmatter)
- Acceptance criteria are crisp enough to write tests against
- A test framework is already wired up

```
Task tool (general-purpose):
  description: "TDD-implement+verify Task {task_id}: {task_name}"
  prompt: |
    You are the TDD wave subagent for Task {task_id}: {task_name}. You implement the task strictly test-first, then run the in-wave verifier loop, then return the same JSON contract as a standard wave.

    > **DoD gate:** Any `verified=false` in the Definition of Done checklist will rewrite your outcome to `needs_more_work` regardless of your declared label. Do not mark items verified until you have concrete evidence.

    > **Exhaust will degrade your senses, use subagents and workflows to keep yourself focused.**
    > You already dispatch a verifier for exactly this reason - a long solo pass makes a fresh
    > subagent a better judge of your diff than you are. Send the wide reads out the same way
    > rather than absorbing them between red and green.

    ## Worktree — the one tree every command below runs against

    **Worktree:** `{worktree}`

    Every `git` call, every edit and every test command in this prompt names that path
    explicitly. Nothing here runs against your working directory: a wave is dispatched from the
    machine's launch directory, which is not a repository, so a bare `git` resolves against a
    tree nobody chose — or against nothing at all.

    Resolve the path once, before your first action. Stop at the first line that holds:

    0. A worktree your own dispatch message told YOU to create — a best-of-N candidate is told
       "the path that command prints is YOUR worktree". It is yours alone, narrower than any
       per-wave tree, so it outranks everything below including slot 1. A `## Worktree context`
       block is prepended unconditionally and was written before candidates existed: it names the
       wave's single SHARED tree and does not know about you. If you were told to create your own,
       that block does NOT win — ignore it.
    1. A `## Worktree context` block accompanying this prompt and naming a per-wave worktree —
       a narrower tree cut for your task alone, so it wins.
    2. The `{worktree}` value above, when the orchestrator substituted it at dispatch.
    3. The repo path your dispatch message named (`Implement orchestrator task <id> in repo
       <path>`) — on the workflow dispatch path that sentence is the only place it appears.

    What the orchestrator owes you in slot 2: the plan frontmatter's `worktree:` field when the
    plan carries one, otherwise the conventional layout `<worktree_root>/<project>/<epic-slug>`.
    The stated field beats the computed layout, because an epic resumed into a hand-made
    worktree is exactly the case a computed path gets wrong.

    If none of the four resolves — the literal token `{worktree}` is still sitting there and no
    dispatch message named a repo — **STOP**. Return `outcome=blocked` with
    `blocked_reason: "worktree unresolved: dispatch supplied no path"`. Do NOT substitute your
    working directory and do NOT go hunting for a checkout that looks right. An unsubstituted
    placeholder is not a path, and a wave that guesses one commits into a tree nobody is
    reviewing.

    **Test commands carry the path too.** `verification.test_commands` are plan-authored strings
    with no tree in them. Give each one `{worktree}` explicitly: the runner's own path flag where
    it has one (`python -m pytest {worktree}/<path>`, `npm --prefix {worktree} ...`), and where
    it has none, name the tree in the command itself (`cd {worktree} && <command>`) so the tree
    is stated rather than inherited from whatever cwd the harness handed you.

    A TDD wave writes two commits into that tree — RED then GREEN — so a mis-resolved path here
    does not merely lose work, it leaves a failing-test commit stranded in a tree the
    orchestrator will never reconcile.

    {operational_context_block}     # rendered from plan.extra_context_files; see SKILL.md Step 7

    ## Task contract

    [FULL TEXT of task body from plan — paste verbatim]

    ## Verification block

    ```yaml
    {task.verification rendered as YAML — acceptance_criteria, file_allowlist,
     regression_guard, spec_format, failure_modes, test_commands}
    ```

    ## Plan-level failure modes (always honor)

    {plan.verification.global_failure_modes as bullet list}

    ## Verifier loop parameters

    - max_fix_iterations: {plan.verification.max_fix_iterations}   # default 4
    - verifier prompt template: <suite_root>/skills/orchestrator/prompts/verifier.md

    ## Your responsibilities

    0. **Consider the simplest approach.** Same Step 0 as the standard wave: think `/simplify`-style about the planned implementation before writing any code. Reuse existing helpers/patterns inside `verification.file_allowlist` where possible.

    1. **Lock the file allowlist.** Treat `verification.file_allowlist` as a hard limit on every git diff for the rest of this task. Its paths are relative to `{worktree}` — write `{worktree}/<allowlist path>`, never a same-named file in another checkout. New files only when the allowlist explicitly permits.

    ## Definition of Done (you MUST verify each item)

    Your DoD checklist is `verification.acceptance_criteria`, one item per criterion in
    order, numbered from 1. If the task carries no acceptance criteria, the checklist is
    empty (return an empty `definition_of_done_checklist`).

    Each item in the DoD checklist maps to a `definition_of_done_checklist` entry in your JSON return (see below). For every item:
    - Set `verified: true` only when you have concrete, observable evidence (test output, file content, grep match, command exit code).
    - Set `how` to a brief description of the evidence (e.g. "grep -q returned 0", "test output shows PASS", "git diff shows function added").
    - If any item is `verified: false`, your outcome is forced to `needs_more_work` by the orchestrator's `coerce_wave_outcome()` guard regardless of verifier verdict.

    2. **Write failing tests (RED) — one test per acceptance_criteria entry.**
       - Tests live in the test files declared in `verification.file_allowlist` (or alongside the implementation file with the standard test convention for this stack).
       - Each test references signatures/types from the spec. Tests must fail with `NotImplementedError`, `ImportError`, or `AssertionError on missing behavior` — NOT a syntax error or framework misconfiguration.
       - Commit on the working branch in `{worktree}`, scoped-staged per the Git hygiene section:
         `git -C {worktree} add <test paths from allowlist>` then
         `git -C {worktree} commit -m "red: failing tests for <task slug>"`.

    3. **Run the test commands. Verify RED for the right reason.**
       - Run every command in `verification.test_commands`, each carrying `{worktree}` per the Worktree section. New tests must fail.
       - If tests pass on first run → you wrote weak tests, OR the implementation already exists. STOP, return `outcome=blocked` with `blocked_reason: "tests passed on RED commit — review whether the task is actually unimplemented"`.
       - If tests fail with framework / import / syntax errors → STOP, return `outcome=blocked` with the exact error.

    4. **Implement the minimum code that turns RED tests GREEN.**
       - Smallest plausible change. No speculation, no abstractions you don't need yet.
       - Honor `verification.regression_guard` (do not touch those files at all).
       - After each edit, eyeball your `git -C {worktree} diff --name-only HEAD` against the allowlist. Anything outside is automatic tier-3.

    5. **Run tests until green** (same commands, same `{worktree}`, every iteration).
       - All previously-green tests must still pass.
       - All RED tests must turn green.
       - If a test fails for a reason your implementation can address: keep iterating, max 3 in-loop attempts. If still red after 3 attempts, return `outcome=test_failure_persistent` with the full failure history.

    6. **Refactor pass.** Once green, look for cleanup that doesn't change behavior:
       - Inline single-use helpers, remove dead branches, tighten names.
       - Re-run tests after each refactor edit. They MUST stay green.
       - Stop refactoring when there's nothing obvious left — don't refactor for sport.

    7. **Self-review with the `/simplify` lens.** Same as standard wave Step 3: read your own diff as `/simplify` would. Catch over-engineering, premature abstraction, and reuse misses before the verifier sees the diff.

    8. **Dispatch the Verifier subagent.** Same as standard wave Step 4. Use the Task tool with subagent_type="general-purpose" and `<suite_root>/skills/orchestrator/prompts/verifier.md`. Populate its `{worktree}` slot with `{worktree}` — it runs its own verification command and has no other way to learn the tree. Include in the verifier payload that this was a TDD wave (so the verifier knows tests were written first and can sanity-check that the test commits exist in that tree).

    9. **Read the Verifier verdict and branch** — same logic as `implementer-with-verifier-loop.md` Step 6. Re-fix and re-dispatch up to `max_fix_iterations` if the verifier returns `blocker`. Tests MUST stay green across every fix iteration.

    ## TDD-specific stop conditions

    Beyond the standard wave's stop conditions, also stop and return `outcome=blocked` if:
    - RED commit's failing tests pass without any implementation change (weak tests).
    - You cannot write a test for an acceptance criterion (the criterion is too vague — that's a tier-3 escalation, return `outcome=escalation_required`).
    - You needed to expand the allowlist to add tests in a new test file (acceptable only if `verification.file_allowlist` already includes that test path; otherwise tier-3).

    ### Git hygiene — staging, pulling, pushing

    Two failure modes exist on a shared branch. Use the right tool for each.
    Every command below carries `-C {worktree}`; a bare one is a different tree.

    **Collision within your wave's commit (sibling waves staging your files):**
    Stage ONLY paths from `verification.file_allowlist`. Never `git add .`,
    `git add -A`, or `git commit -am`. Example:
    ```
    git -C {worktree} add path/from/allowlist.py tests/test_file.py
    git -C {worktree} commit -m "..."
    ```
    Hard rule. Unscoped staging in a parallel wave produces a single commit
    carrying two waves' diffs but only one task's message, breaking the
    status board's per-task `commit_sha` mapping.

    **Another Claude Code session blocking your `git -C {worktree} pull --rebase`:**
    The other session's unstaged changes are not yours to commit and not yours
    to discard. Use the stash-pull-push pattern, every step carrying `-C {worktree}`:
    stash ONLY the sibling's paths (never your own), pull --rebase, do your work,
    stash sibling paths again before pushing, pull --rebase, push. Do NOT pop —
    let the other session restore its own WIP on its next operation.

    These two rules are complementary, not alternatives. Scoped staging prevents
    collision *inside* your commit; the stash dance unblocks rebase *between*
    sessions. Do not interpret "scoped staging" as "never stash" — stashing a
    sibling session's paths is fine and often necessary.

    After pushing: verify with `git -C {worktree} log origin/<branch> --oneline -3`
    (never claim a push you did not verify).

    **Before EVERY `git commit`:** stage ONLY files inside `verification.file_allowlist` (`git -C {worktree} add <explicit paths>` — never `git add .` or `git add -A`), then verify with `git -C {worktree} diff --cached --name-only` that every staged path is inside the allowlist. If any staged path falls outside it, unstage it, abort the commit, and surface the violators in your `blocked_reason`. Do not skip this check — envelope violations committed to the branch require a force-revert and re-dispatch.

    ## AUTO-MODE EXECUTION

    [Same AUTO-MODE EXECUTION block as `implementer-with-verifier-loop.md` — copy verbatim.]

    ## INVARIANT (MUST hold in every JSON output you return)

    [Same INVARIANT block as `implementer-with-verifier-loop.md` — copy verbatim.]

    ## Return format

    Return the same JSON shape as `implementer-with-verifier-loop.md`. The orchestrator's Step 7 branching logic is identical for both wave types. The only signal that this was a TDD wave is the dispatch prompt name (logged separately in the decision log).

    **escalation_requests options shape:** identical to `implementer-with-verifier-loop.md` — preferred shape is `[{"label": "...", "description": "..."}, ...]` (max 4) for discrete-choice escalations, rendered as `AskUserQuestion` buttons in standalone interactive runs and as registry `## Questions` rows in fleet-launched runs; legacy shape is `["...", "..."]` or omitted, which falls through to free-text chat reply. See the verifier-loop prompt for the full label-collision rule and routing semantics.

    Additional fields (TDD-specific, optional):
    - `tdd_red_commit_sha` — SHA of the RED commit (failing-tests commit) you made in `{worktree}` before implementation. Helpful for spec-reviewer to confirm tests were written first — it looks the SHA up in that tree, so a commit made anywhere else reads as fabricated.
    - `tdd_refactor_iterations` — number of refactor passes you made after green.
    - `definition_of_done_checklist` — one entry per DoD item rendered at dispatch time. Each entry: `{"n": <int>, "item": "<text>", "verified": true|false, "how": "<evidence description or null if false>"}`. The orchestrator's `coerce_wave_outcome()` sets outcome=`needs_more_work` if any entry has `verified=false`.
    - `evidence` — structured proof block for the orchestrator audit trail. Fields: `{"test_commands_output": "<captured stdout/stderr>", "grep_checks": ["<command>: exit <N>", ...], "files_verified": ["<path>", ...], "commit_sha_verified": "<SHA or null>"}`. Omit fields that do not apply; do not fabricate output.

    ```json
    {
      "outcome": "done | verifier_blocker_persistent | test_failure_persistent | blocked | escalation_required | needs_more_work",
      "verifier_iterations": 1,
      "verifier_verdict": "clean | warnings | blocker_persistent",
      "verifier_findings_count": {"low": 0, "medium": 0, "blocker": 0},
      "verifier_findings": [],
      "verifier_history": [],
      "verifier_findings_history": [],
      "decision_log": [],
      "escalation_requests": [],
      "integration_test_suggestions": [],
      "definition_of_done_checklist": [
        {"n": 1, "item": "<DoD item text>", "verified": true, "how": "<evidence>"}
      ],
      "evidence": {
        "test_commands_output": "<captured stdout/stderr>",
        "grep_checks": ["<command>: exit 0"],
        "files_verified": ["<path>"],
        "commit_sha_verified": "<SHA or null>"
      },
      "tdd_red_commit_sha": "<SHA of RED commit, or null>",
      "tdd_refactor_iterations": 0,
      "commit_sha": "<SHA if outcome=done, else null>",
      "files_changed": ["<path>", "..."],
      "blocked_reason": null
    }
    ```


<!-- Template variables (filled by orchestrator at dispatch time):
worktree — Absolute path to the tree this wave builds in: the plan frontmatter's
`worktree:` field when the plan carries one, else `<worktree_root>/<project>/<epic-slug>`.
The stated field beats the computed layout. There is no cwd fallback: a dispatcher
that cannot resolve it must fail loudly naming `worktree:` rather than let the wave
inherit the launch directory. Substituted everywhere `{worktree}` appears above.
WORKTREE_CONTEXT_BLOCK — Either an empty string (legacy shared-worktree mode)
or the full "## Worktree context" block defined in SKILL.md Step 7. Always
expanded literally; never escape its leading `##`. When it names a per-wave
worktree it overrides `{worktree}` for this dispatch (Worktree section, slot 1).
An unsubstituted literal token is not a block and not a path — the wave ignores
it and falls through to the next slot. -->

    The orchestrator records `tdd_red_commit_sha` and `tdd_refactor_iterations` to the status board's `TaskState` for audit / dashboard surfacing. Spec-reviewer and code-quality-reviewer dispatches still run after a successful TDD wave.
```
