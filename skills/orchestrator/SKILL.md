---
name: orchestrator
description: Use when given a multi-day epic spec to execute autonomously across phases of tasks with parallel subagent dispatch, regression auto-fix, and status board persistence. Triggers on `/orchestrator <plan-or-spec-path>` or when user describes a multi-day epic and wants minimum approvals after plan approval.
---

# Orchestrator

Execute a multi-day epic by composing existing superpowers skills, paced by `ScheduleWakeup`, with `status.md` as the source of truth across context resets.

**Announce at start:** "I'm using the orchestrator skill to execute this epic."

## Setup (run once per machine)

Provision step 8 already ran the bootstrap script and installed PreToolUse hooks that auto-allow common subagent tool calls — **unless the operator installed with `--no-auto-allow`**. Check with this count-only probe (never print or Read the settings file itself; its env block holds tokens):

```
python -c "import json,os; p=os.environ.get('CLAUDE_SETTINGS_FILE') or os.path.expanduser('~/.claude/settings.local.json'); s=json.load(open(p,encoding='utf-8')); print(sum(1 for b in s.get('hooks',{}).get('PreToolUse',[]) for h in b.get('hooks',[]) if str(h.get('statusMessage','')).startswith('auto-allow: ')))"
```

If the count is 0 — no PreToolUse entry has a `statusMessage` beginning `auto-allow: ` — that is the operator's recorded choice: **do not run bootstrap.py**, which would silently reverse the opt-out. Run it by hand only on a machine that was never provisioned with `<suite_root>/install/provision.py` and whose operator has agreed to the hooks:

```bash
python <suite_root>/skills/orchestrator/bootstrap.py
```

**Why:** the orchestrator dispatches general-purpose subagents (wave subagents, reviewers, fix subagents) which run their own Bash/Edit/Write calls, and an unattended run stops at every permission prompt one of those calls raises. Per the Claude Code hooks reference (https://code.claude.com/docs/en/hooks), hooks from settings files also run inside subagents, so the allow hooks cover subagent calls too. Idempotent; safe to re-run after settings changes.

**Reviewer/verifier subagents: FOREGROUND suite runs + same-turn verdict.** Every reviewer/verifier prompt the orchestrator dispatches MUST include: *"Run all test suites in the FOREGROUND (plain `npx vitest run` / `pytest` — never `run_in_background`, never piped through `tail` or a watcher) and emit your final verdict in the SAME turn the suite finishes; never end your turn 'waiting' on a suite."* Why foreground: a reviewer that backgrounds its suite stops without a verdict — the detached run leaves it wedged in a wait-loop that survives nudges, and several concurrent suites thrash one worktree. If a reviewer returns verdict-less twice: stop nudging (two-attempt rule), run the suite ONCE yourself, and close the review inline from the reviewer's completed analysis + your run.

**Wave execution layer:** wave execution runs through the **Workflow tool** (`workflows/wave_group.js`) — general-purpose subagents cannot dispatch sub-subagents, and the workflow's `agent()` calls are the sub-dispatch layer that lets wave fan-out go deeper than that. Implementers, in-wave verifiers, best-of-N candidates/judges, and adversarial refuters all fan out inside the workflow; the orchestrator dispatches reviewers itself only on the single-shot reconcile path (Step 7).

## When to use

- User invokes `/orchestrator <plan-or-spec-path>` — a direct skill-name invocation; the orchestrator
  ships as a skill only, with no `commands/` wrapper (the `commands/` wrappers cover the seven
  loops plus a few user-invoked skills such as `/report` and `/wiki-save`; the orchestrator is
  not one of them)
- User describes a multi-day epic and wants minimum approvals after plan approval
- A scheduled wake fires for an in-flight epic (status.md exists, status=running or status=escalation)
- A fleet loop dispatches an approved epic (see **Fleet invocation context** below)

## When NOT to use

- Single-task work — use the Task tool directly
- Tightly coupled tasks that can't be subagent-isolated — use `../_vendored/executing-plans/SKILL.md` interactively
- Cross-machine background execution — out of scope (use your CI/remote runner)
- Plans without YAML frontmatter — invoke `solution` first to add the frontmatter

## Fleet invocation context (who is the human surface for this run?)

The orchestrator runs in one of two contexts, and every human-facing gate in this skill
branches on which one it is:

- **Standalone interactive** — a human at the keyboard invoked `/orchestrator` or `/project`.
  The human IS the surface: every `AskUserQuestion` gate below behaves exactly as written.
- **Fleet-launched** — a fleet loop dispatched this run. Per `<suite_root>/docs/REGISTRY.md`, no loop but
  dev-manager asks the human, so **a fleet-launched run never calls `AskUserQuestion`**. Almost
  every site that would have asked instead writes a registry `## Questions` row (answered by
  dev-manager) and polls the registry for the answer. "Never asks a human" is the invariant;
  "always raises a Question row" is not, and the table below says which is which per site.

**The mechanism — an explicit marker, never inference.** The launching loop MUST include the
literal line `fleet-context: <agent>` (e.g. `fleet-context: lead-dev`) in the dispatch prompt or
arguments. At bootstrap, if the invocation carries that marker, record it: write
`fleet_context: <agent>` into `status.md` frontmatter and append a tier-1 decision
(`"fleet-launched by <agent>; human surface = registry Question rows"`). Later wakes are
self-scheduled (`ScheduleWakeup`), not fleet-launched, so they **inherit the mode from
`status.md`** rather than re-detecting it. No marker and no `fleet_context` field = standalone
interactive. Never infer fleet mode from anything else (cwd, epic state, time of day).

**How a fleet-context question works** (applies at every branched site below):

1. On the transition wake — the same wake where the standalone branch would have fired its
   buttons — raise the row through the registry CLI, never by hand-editing `registry.md`:
   `python -m agentflow.registry ask --actor <fleet_context> --work <E-NNNN> --urgency waiting
   --question "orchestrator (<epic-slug>): <question>" --default "<default>"`. The CLI takes the
   edit-lock, allocates the id and derives `raised_by` from `--actor`, which must be the bare
   lane name — a composite such as `<fleet_context> (orchestrator)` is rejected — so name the
   orchestrator and the epic in the question TEXT. `--work` is the Queue-row id the dispatching
   loop is building, or `-` if the invocation did not carry one; a slug is rejected
   (`id_format`) and no row is written. `--default` is mandatory — use the escalation's
   recommendation, else the first option; urgency per the REGISTRY classification rule,
   normally `waiting`. Record the Q-id the CLI prints in the same decision-log marker the
   standalone branch uses to suppress re-prompting (`*_prompted: Q-NNNN at <utc-now>`).

   **GROUND IT FIRST - the same discipline the build side already demands.** Grounding is
   mandatory before a loop spends its OWN hour (lead-dev SKILL.md section 2.5, "approval is never
   a grounding waiver") and just as mandatory before it spends the HUMAN'S attention, which is the
   scarcer of the two and the one you cannot get back.

   **In the question body, state the measurement behind each load-bearing clause and when you
   took it.** Not "task 4 is blocked" but "task 4 blocked as of 05:24Z (`status.md` tasks[4])".
   A clause with no measurement beside it is visible as unmeasured, which is the whole point:
   the rule is not "use invariants", it is "show the reading", because a false invariant looks
   exactly like a true one until someone checks.

   **Two failure shapes are why this exists.** A premise can be true when written and
   false by the time it is read: even a peer's correction can expire before anyone reads it. And a
   premise can be false when it was written, however durable the invariant it rests on looks.
   In both cases the reading that would have caught it takes minutes, far less than the human
   attention the question spends.

   **A question that should not have been asked comes back off the board:**
   `python -m agentflow.registry retract --actor <fleet_context> --id Q-NNNN --reason
   "<what collapsed>"`. Raiser only, open only, terminal, and NOT `answered` - nothing was
   decided. Use it the moment you notice the premise died. `retract` and `apply` both refuse
   with `waited_on_live` while a Queue row is still parked on the id: before `apply`, the
   claimant resumes it (`release --state building`); before `retract`, it resumes it or
   re-parks it on the new id.
2. Keep the existing `ScheduleWakeup` poll cadence. On each poll wake, lock-free re-read the
   registry instead of waiting for a button: `state: answered` → map the `answer` text through
   the SAME canonical-reply mapping as a free-text chat reply, take the matching branch, and
   flip the row `answered -> applied` with `python -m agentflow.registry apply --actor
   <fleet_context> --id Q-NNNN` (you are the raiser; `apply` refuses any other actor). Still `open` →
   persist, reschedule, exit — exactly like an unanswered button.
3. Ntfy stays notification-only in both contexts — it may announce that a question exists,
   never carry one.

Fleet-launched epics are `approved-for-autonomous` by construction (only promoted epics are
dispatchable), so the approval gates and the merge gate auto-skip on state and most runs raise
no questions at all. The branch matters for the exception paths. **This table enumerates every
`AskUserQuestion` site in this skill as of the commit that wrote it. Read what the CI gate
actually enforces before trusting it (below the table) — it is weaker than this table, and the
difference is where an unbranched site can hide:**

| Site | Fleet-launched behaviour |
|---|---|
| Bootstrap step 3 dispatch gates | Question row (epic somehow NOT `approved-for-autonomous`) |
| Step 3 blocker-surfacing wakes | Question row |
| Step 3 / Step 7 wave `escalation_requests` | Question row |
| **Step 8 escalation-rate guard** | **Question row, `urgency: waiting`, `default: "Pause for spec review"`** |
| Step 9.1.5 frontend regression note | Not a site: asks nothing in either context; a fleet run records `polishing_skipped: fleet_context` and prints no note |
| Step 9.2 merge gate | Auto-skips on `current_state == approved-for-autonomous`, which is every fleet epic **by construction** — covered by state, not by the marker. Not a proof of unreachability: the same "somehow NOT approved" epic that row 1 exists for would reach these buttons too, so if that path is ever real, this row needs the marker as well |
| `## Escape hatches for the user` | Not a site — a list of ways a human answers a question raised elsewhere; parked in `KNOWN_UNBRANCHED` with that reason |

Step 9.1.5 asks nothing in either context, so it is not a site; the gap it leaves is stated at
the site.

**What the CI gate actually enforces — narrower than the table above.**
`<suite_root>/skills/_shared/tests/test_human_surface_gates.py` checks that each markdown *section* containing
`AskUserQuestion` also contains the literal `fleet-context` token, unless the heading is in
`KNOWN_UNBRANCHED`. For `project` and `solution` it is per-gate (proximity-checked); **this skill is
gated at SECTION level**, so once a section carries the marker, a *newly added* `AskUserQuestion`
inside that same section passes green. The gate is a ratchet against whole sections regressing, not
a proof that every individual call site is branched. **So adding a gate inside Step 3, Step 8 or
Step 9.1.5 is on you, not on CI:** branch it and add a row here. Do not read the table as something
CI maintains for you.

The frontmatter-edit fallbacks documented at each site remain valid in both contexts.

## Event-emission contract

Every session-level transition emits an event via `agentflow.status_writer.append_session_event(state, kind, detail)`. The full contract:

| Event prefix | Trigger location | Summary template |
|---|---|---|
| `session_started:` | Bootstrap, after status.md initialized | `<epic>; plan=<path>` |
| `session_resumed:` | Step 1, when last_wake older than 1h and a wake fires | `after <human-age> idle` |
| `session_lock_held:` | Step 1, when bailing because of lock contention | `held by <other session id>` |
| `session_paused:` | Step 3, when state.paused becomes True | `by user` |
| `session_unpaused:` | Step 3, when state.paused becomes False | `by user` |
| `escalation_opened:` | inside open_escalation() | `<id> task <N>: <q>` |
| `escalation_answered:` | inside `answer_escalation()` (called by Step 3 or by orchestrator on chat-reply) | `<id> answered` |
| `wave_dispatched:` | Step 7 item 4 sub-step 2 (the decision note is separate: `wave <n> dispatched: tasks [...] run <runId>`, sub-step 3) | `Wave <N>` |
| `wave_completed:` | Step 7 (existing — keep "Wave N complete:") | unchanged |
| `test_run:` | Step 5, after every regression run | `green/red, regressions=<N>` (also calls record_test_run) |
| `session_ended:` | Step 9, status→done | `<N> tasks, <N> waves, total <T>` |
| `session_abandoned:` | user-flipped status to "abandoned" | `<reason>` |

**Ntfy dedup:** Every ntfy send in this skill goes through `agentflow.ntfy.notify_once(state, key=..., trigger_type=..., epic=..., detail=..., ...)`, never raw `agentflow.ntfy.notify(...)`. `notify_once` wraps `notify` with `state.notifications_sent` dedup (default 1-hour window), so a wake that re-runs a step does not re-push. It does NOT persist `state` — the dedup record only survives the wake if the caller's next `write_status` lands it. Each site below names its `key`; one key per event, since a shared key suppresses the other event.
```python
agentflow.ntfy.notify_once(state, key="<unique-key>", trigger_type=TriggerType.<TRIGGER>, epic=..., detail=...)
```

## Bootstrap (first invocation only)

If `status.md` does NOT exist for this epic, you are bootstrapping.

1. Read the file at the provided path and check its frontmatter first. **A plan-shaped file**
   (top-level `epic:` + `phases:` + `tasks:` — the same test `/project` Step 0 uses) is already
   the plan: `/project` hands one in after gate #1 approved it. Do NOT invoke `solution` on it —
   `solution` refuses existing plans, and only after its writing-plans pass has run. Skip step 2
   and go straight to the summary and gate #2 (step 3) with that file as the plan. Only a spec
   goes on to step 2.
2. Invoke `solution` skill to produce the implementation plan with YAML frontmatter. **In a
   fleet-launched run, include the literal line `fleet-context: <agent>` in that invocation** (the
   agent recorded in `status.md`, per **Fleet invocation context** above). `solution` has no
   `status.md` of its own to inherit the mode from — the caller's marker is its only signal — and
   its Dependencies gate fires on every plan it authors, so dropping the marker here would fire
   `AskUserQuestion` into an unattended session. This is the second of the two paths that reach
   `solution`; `/project` Step 3 propagates it on the other.
3. Show the user a one-screen summary (rendered as plain prose, NOT as part of the question — the
   summary is the context, the buttons are the answer mechanism):
   - Epic name
   - Total task count
   - Phases and their task counts
   - Estimated wall-time (use MEASURED bands, not tasks × 30 min — actuals run 5–15 min/task
     sonnet, 10–25 min/task opus TDD, so a flat ×30 per task overshoots several-fold. Estimate = task count ×
     10 min (sonnet-heavy plan) or × 18 min (opus-heavy plan), then state the band, not a point.)
   - Path to plan file

   **Auto-skip for `approved-for-autonomous` epics (gateless autonomous path).** Before calling
   `AskUserQuestion`, read `current_state` from the plan's frontmatter. If
   `current_state == approved-for-autonomous`, the epic has already been human-greenlit for gateless
   execution (the fleet autonomy envelope — promotion to this state WAS the approval).
   **Skip the AskUserQuestion**, append a tier-1 decision
   `"gate #2 auto-skipped: current_state=approved-for-autonomous"`, treat the result as canonical
   reply `approve`, and proceed to step 5. The summary above MUST still render for the record. This
   auto-skip fires ONLY for that exact state — every other state still gates normally. The frontmatter
   state is the only trigger (no env var / arg bypass).

   **Fleet-context branch (dispatch gate).** In a fleet-launched run (see Fleet invocation
   context), a non-`approved-for-autonomous` state reaching this gate is itself an anomaly —
   only promoted epics are dispatchable. Do NOT call `AskUserQuestion`. Write a registry
   `## Questions` row per the fleet-context procedure (question: "fleet-dispatched epic
   <slug> is `<state>`, not approved-for-autonomous — approve scope or decline?";
   `default: decline (un-promoted epic should not run unattended)`; urgency `waiting`), set
   `state.status = "escalation"`, and poll. `answer` mapping: `approve`-class → canonical
   reply `approve`; `decline`-class → `decline`, exit cleanly. The `approved-for-autonomous`
   auto-skip above already covers the normal fleet path — this branch is the exception rail.

   Otherwise (any other state, standalone interactive), call the `AskUserQuestion` tool with:

   ```yaml
   header: "Plan scope"
   question: "Approve plan scope?"
   options:
     - label: "Approve"
       description: "Write status.md, send ntfy, fire first wave inline"
     - label: "Decline"
       description: "Exit cleanly; plan stays on disk"
   ```

   This is approval gate #2 — the orchestrator's contract with the user, separate from `/project`'s
   gate #1 (which approves the brainstorm/plan content). Do NOT collapse these two gates: the
   /project gate covers "is this the right plan?" and this gate covers "should the orchestrator
   start dispatching waves now?". The summary preceding the buttons MUST still render so the user
   has the context to answer informedly.

   **Free-text fallback:** the orchestrator MUST accept typed free-text answers as well, mapped to
   the canonical replies the rest of the bootstrap procedure expects:
   - Button **Approve** OR free-text containing `approve` / `y` / `yes` / `go` → canonical reply `approve`
   - Button **Decline** OR free-text containing `decline` / `n` / `no` / `cancel` → canonical reply `decline`
   - AskUserQuestion's auto-`Other` slot routes typed answers through this same mapping so
     backward-compat with existing user habits (typed `y`) is preserved.
4. **WAIT for explicit user approval.** This is the only mid-flow approval gate. Treat silence as a
   continued wait — do NOT proceed without a button click or typed canonical reply.
5. On approval:
   - Take `<slug>` from `plan.slug`, the parser's value (the plan's `slug:`, or the slug derived
     from its filename when it has none), never from the epic name. The survey keys a board by
     its filename stem and `mark-done` writes `<slug>.status.md`, so a board named from the
     title is a second record no join finds: the epic stays selectable while it runs and reads
     as running forever after it ships.
   - Decide `status.md` location: default `<data_root>/projects/<name>/status/<slug>.status.md`
     (the consolidated status board dir the epic-survey Pass-3 globs and the developer/dev-manager
     skills read; `<name>` is the plan's `project:`). Ask the user only if the project is ambiguous.
   - Initialize `status.md` from `templates/status-template.md`
   - Populate `state.tasks` from plan: for each task in `plan.tasks`, create a `TaskState(state=<initial>, phase=<plan.tasks[tid].phase>)` so the rendered body groups by phase correctly
   - Mark all tasks with no deps as `state: ready`, all others as `state: pending`
   - Set `status: running`, `current_phase: <first phase>`, `started: <now>`
   - Set `state.project = plan.project` if the plan declares a `project:` field (used by the wiki mirror to route status artifacts to `wiki/epics/<project>/<slug>/status.md` instead of `_unsorted/`)
   - **Inside this "status.md does NOT exist" branch only:** call `append_session_event(state, "started", f"<epic-name>; plan=<plan-path>")` — this event fires exactly once at bootstrap. Do NOT call it on subsequent wakes.
   - Then call `agentflow.status_writer.write_status` to persist (event is already in state.decisions at this point)
6. Send ntfy via `agentflow.ntfy.notify_once(state, key="epic_started", trigger_type=TriggerType.PHASE_COMPLETE, epic=..., detail="started")`
7. **Proceed directly into the wake-cycle algorithm below — run the first wake-cycle inline in this same invocation.** The first wave fires NOW, not on a future schedule. Do NOT call `ScheduleWakeup` to start the work; bootstrap and the first wake-cycle execution happen in one turn. Step 10 (at the end of this first wake-cycle) schedules the *next* wake.

If `status.md` DOES exist, skip bootstrap — go straight to wake-cycle.

## Wake-cycle algorithm

Every invocation (manual or scheduled) runs these steps in order. Use TodoWrite to create one todo per major step (1-10) and mark them as you progress.

### Step 1: Acquire lock

**Step 1.0 — Liveness pulse and lock (first action, before anything else).**

On entering Step 1, BEFORE any other action:

1. Acquire lock at `<status_path>.lock` via `agentflow.lock_file.acquire(path)`.
   - If `LockHeldError` raised (lock held by an alive PID): exit silently with no other action; another session is running this epic.
   - If the lock acquire silently steals a stale lock (PID was dead): log a tier-1 decision `"claimed orphan lock from PID <X>"` after status.md is read in subsequent steps.

2. Pulse `last_wake`: call `agentflow.status_writer.pulse(status_path)`. This atomically rewrites status.md with `last_wake = utc_now()`. Always do this, even on no-op wakes (paused, halted, idle).

3. Continue with the rest of Step 1.

The lock handle is held for the duration of the turn. Release on normal exit; rely on crash + orphan-detection to recover from abnormal exit.

**At the very start of every wake (before the lock check):** call `increment_wake(state)` so the wake counter is accurate regardless of what happens next. This requires loading minimal state first — if state doesn't exist yet (bootstrap path), skip this call.

Check for `<lock_dir>/orchestrator-<slug>.lock`, where `<lock_dir>` is the config-resolved lock
directory (`agentflow.config.load_environment().lock_dir` — the same home as the registry
edit-lock `<lock_dir>/<project.name>-registry.lock`). If it exists and was modified within the last 30 minutes:
- Call `append_session_event(state, "lock_held", f"held by <other session id from lock file>")` — emit once per contention round (do not call in a loop; one event per bail is enough to avoid spam in the decision log).
- Log "another wake in progress, exiting" and return — do NOT schedule another wake (the in-flight wake will reschedule).

Otherwise, write the lock file with current timestamp + this session's identifier.

**Step 1.1 — Resolve the target tree, then pre-flight it.**

**Why every git call in this skill names its tree, and why that is not optional.** The lane that
dispatches into this skill is launched from the machine's single launch directory — the folder
whose `CLAUDE.md` chain and session history the fleet runs on — and that directory is not a
repository. A bare `git` therefore has nothing correct to resolve against: it walks up until it
finds *some* tree, or none, and either way the tree it lands on is one nobody chose. Nothing
puts a dispatched subagent in the right tree for it, so nothing compensates for a bare
command — an unqualified
`git log` here reads whatever the harness handed the process, and an unqualified `git commit`
writes to it. Every epic-level call below carries `-C <worktree>`, every repo-level call carries
`-C <canonical_repo.local_checkout>`, and `<worktree>` is passed explicitly into every subagent
prompt, test command and review: an agent told the path edits the right tree, one told nothing
edits whatever it inherited. The git placeholders in this section come off the resolved block; for an epic whose
`project:` is not the block's `[project]`, follow `/project` Step 0.5, item 1 "Confirm the canonical repo" (`config show --project`).

**Resolve `<worktree>` once per wake, before any git call:**

1. Read the plan's frontmatter. **If it carries a `worktree:` field, that value IS `<worktree>`** —
   authoritative, even where it disagrees with the computed layout in (2). An epic resumed from a
   differently-named worktree, or one a human created by hand, is a *stated* fact; computing a
   path first would override an explicit human statement, which is the same ambient-beats-stated
   failure this rule exists to remove.
2. No `worktree:` field → fall back to the hand-run layout
   `<worktree_root>/<project>/<epic-slug>`: `worktree_root` from
   `agentflow.config.load_environment()`, `<project>` from the plan's `project:`, `<epic-slug>`
   from the epic slug computed at bootstrap. A fleet plan always declares the field, because its
   tree is named after the claim branch, which this computation cannot know.
3. **Neither resolves** (no `worktree:` field AND no `project:` on the plan, or `worktree_root`
   unresolvable) → **fail loudly, do not proceed**:

   ```
   ESCALATION: Target tree unresolvable.
   This epic has no worktree to build in and one cannot be computed. Add a
   `worktree:` field to the plan frontmatter naming the epic's git worktree
   (conventionally <worktree_root>/<project>/<epic-branch>), or add the plan's
   `project:` field so the fallback layout resolves. Then restart the orchestrator.
   ```

   Open a real escalation record — `open_escalation(state, task=0, question=<the ESCALATION block
   text above>)`, which sets `state.status = "escalation"` itself — then persist `status.md` and
   exit. A bare status assignment with no record halts the epic while rendering no question, so
   nobody can see what is being asked or answer it. **There is no fallback to
   the working directory.** The working directory is the launch directory; building there is the
   precise failure this step exists to prevent, and a silent default hides it until commits land
   in the wrong tree.

Bind the result for the rest of the wake — `<worktree>` in the prose below, `worktree` in the
Python blocks.

**Pre-flight `<worktree>` — existence and identity, not a cwd comparison:**

1. `git -C <worktree> rev-parse --show-toplevel` must succeed AND name the **same directory** as
   `<worktree>`. **Compare as paths, never as strings** —
   `Path(out).resolve() == Path(worktree).resolve()` (or `Path(out).samefile(worktree)`).
   git prints POSIX separators (`C:/Users/...`) on Windows however `<worktree>` was spelled, while
   the computed fallback in (2) yields a backslash form, so a string compare **false-negatives on
   every correct worktree on that platform** and blocks the run it was meant to protect. A
   difference only in separators, in drive-letter case, or via a symlink is a MATCH.
   Non-zero exit = the path is not a git worktree (never created, or removed out from under a
   resumed epic). Output naming a genuinely *different* directory = `<worktree>` is a subdirectory
   of some other checkout rather than the epic's own tree. Do not adopt whatever it printed;
   escalate.
2. `git -C <worktree> remote get-url origin` must resolve to `<canonical_repo.host_ref>`. This is
   the identity half: it catches a `worktree:` field pointing at a real git tree belonging to a
   different project, which (1) alone cannot see.

On either failure, escalate:

   ```
   ESCALATION: Worktree mandate.
   This epic must run in <worktree>, and that path is not a usable worktree of
   <canonical_repo.host_ref>. Cross-session collisions on shared checkouts cause
   commit drift, which this rule exists to prevent.

   Recreate it — the two cases differ. Using form (a) when the branch already
   exists fails loudly ("fatal: a branch named <epic-branch> already exists")
   and creates nothing, so the risk is a confusing dead end rather than lost
   work; the epic's commits are safe on the branch either way. Pick by whether
   the branch exists. Both forms and every placeholder (<epic-branch>,
   <base-branch>, ...) are defined once in /project Step 5:

   (a) <worktree> was never created (no branch for this epic yet):
         git -C <canonical_repo.local_checkout> fetch
         git -C <canonical_repo.local_checkout> worktree add <worktree> -b <epic-branch> --no-track origin/<base-branch>

   (b) the directory was removed out from under a resumed epic (<epic-branch>
       already exists, so its commits are safe on the branch and only the
       directory is gone):
         git -C <canonical_repo.local_checkout> worktree prune
         git -C <canonical_repo.local_checkout> worktree add <worktree> <epic-branch>

   Check with: git -C <canonical_repo.local_checkout> rev-parse --verify <epic-branch>
   then restart the orchestrator.
   ```

   Then **open a real escalation record, not just a status flag**:

   ```python
   open_escalation(state, task=0, question=<the ESCALATION block text above>)
   ```

   `open_escalation` sets `state.status = "escalation"` itself, so a separate status assignment is
   redundant once it is called. Persist `status.md` and exit. Do not proceed. **Setting the status
   without opening the record is what makes an escalation invisible** — `status.md` renders the
   question from the record, so a bare status flag halts the epic while showing nobody what is
   being asked, and the unblock path has nothing to answer.

**There is no skip.** A check that ran only when the plan carried a `worktree:` field would
skip silently otherwise, and that silence lets a plan without one run against the working
directory. A plan with no `worktree:` now resolves through the
fallback or fails at (3); it never proceeds unchecked.

**Step 1.2 — Restart-aware startup.**

After acquiring the lock and pulsing, inspect `status.restart_history`:

- If `restart_history` is empty OR the most-recent entry is older than 5 min: skip this section, proceed to Step 1 main logic.

- Else (recent entry, likely a watchdog auto-restart):

  1. Append a tier-1 decision: `"session resumed via watchdog auto-restart; reason=<reason from latest restart_history[-1]>"`.

  2. Run the restart log over every ref that can hold a wave's commit. When `plan.per_wave_worktrees` is True: `git -C <worktree> log <epic_branch> --branches='<plan.slug>-w[0-9]*' --since=<previous last_wake>`. The digit after `-w` keeps out another epic whose slug starts with `<plan.slug>-w` (slug `auth` and an epic `auth-webhooks`). A wave commits on its own wave branch, in a sibling tree, and reaches the epic branch only at merge-back, so a commit made before the restart can sit on a wave branch alone; a log of the epic branch does not show it. When `plan.per_wave_worktrees` is False: `git -C <worktree> log --since=<previous last_wake>`. Log any commits as orphan-recovery candidates in a tier-2 decision, naming the branch that holds each (`git -C <worktree> branch --contains <sha>`).

  3. If `status.tasks[*].state` shows a task in `in_flight` AND no completion was recorded:
     - Search the restart log from sub-step 2 (the wave branches too, on the per-wave path) for a commit whose message matches the wave's expected task signature (e.g. `feat(T<n>):` or `Task <n>`).
     - If found on the epic branch, or on the task's own recorded `state.tasks[tid].wave_branch`: record the wave as COMPLETED for that task, set `state = "code_review"`, advance to the reviewer step normally. Keep `wave_worktree_path` and `wave_branch` for a commit on the wave branch: the Step 7 review gates read that tree, and the merge-back on approval lands the commit.
     - If found only on a best-of-N candidate branch (`<plan.slug>-w<n>-t<id>-c<i>`): no judge picked it and nothing landed it, so the task did not complete. Handle it as not found (below). The release frees the task's tree, so the Step 1.2.5 prune in this same wake finds the candidate trees orphaned and keeps each candidate branch that holds unmerged commits, with a tier-2 decision naming it.
     - If found only on any other branch (another task's wave branch, or a branch of another epic the pattern still matches): it is not this task's commit. Handle it as not found (below).
     - If not found: mark the wave ABANDONED (`state = "ready"`), schedule re-dispatch as the next action, log tier-2 decision "wave abandoned at restart; will redispatch". When the task records a `wave_worktree_path`, release that tree and clear its fields first (**Releasing a wave tree**, Step 1.2.5).

**Step 1.2.5 — Orphan wave worktree prune (conditional).**

If `plan.per_wave_worktrees` is True:

```python
from agentflow.wave_worktree import prune_orphan_worktrees
live_paths = {t.wave_worktree_path for t in state.tasks.values() if t.wave_worktree_path}
summary = prune_orphan_worktrees(
    repo_path=worktree,   # resolved in Step 1.1 — never Path.cwd()
    epic_slug=plan.slug,
    live_paths=live_paths,
)
```

**Pass `live_paths` on every call.** This step runs on every wake, including the reconcile wake
and a fallback wake that fires while a wave workflow is still running. A tree a task still
records in `wave_worktree_path` belongs to that task: an in-flight agent may be building in it,
and a single-shot task's commit sits in it until merge-back on approval. A prune without the
live set force-removes that tree, and the merge-back that follows fails because its source
directory is gone. Kept trees come back in `summary.branches_kept` and need no decision.

A best-of-N candidate tree (`<slug>-w<n>-t<id>-c<i>`, created by the workflow, one per
candidate) is never recorded in state, yet its agent may still be building in it. The prune keeps
it, tree and branch, whenever its task's own `<slug>-w<n>-t<id>` tree is in `live_paths`, and
lists it in `summary.branches_kept` with the rest. Nothing else is needed for that: Step 7.0
records the task's tree before the workflow fires, and the path is cleared only once the workflow
has finished or is treated as dead (every release below). The match includes the wave number, so
it holds because Step 7.0 names the task's tree with the same wave number the workflow later
gives its candidates. A candidate whose task tree is not live is an orphan like any other.
The workflow removes every candidate tree on each of its exits; an unlanded winner keeps only its
branch, which holds the commit. A best-of-N task that ends `blocked` keeps its tree recorded, so
its candidate trees stay too when any is left, which is only a tree whose cleanup failed (listed in
the return's `cleanup_failures`). It stays until the release that clears the task's path, and the
first prune after that release removes it.

The live set stays honest only because every path that sends a task back to `ready` first
releases its tree (see **Releasing a wave tree** below): a cleared path is the only thing that
lets a dead tree become an orphan again.

For each branch in `summary.branches_preserved`: append tier-2 decision
`"preserved unmerged wave branch {branch} after crash — inspect via `git -C {worktree} log {branch}`"`.
For each branch in `summary.branches_deleted`: append tier-1 decision
`"pruned merged-back wave branch {branch}"`.
For each entry in `summary.failed`: append tier-2 decision
`"could not prune wave worktree {entry.path} (branch {entry.branch}): {entry.stderr}"`, and when
`entry.has_unmerged_commits` is True, append
`" — branch {entry.branch} holds unmerged commits; inspect via `git -C {worktree} log {entry.branch}` before deleting it"`.
A failed entry does not stop the wake: the prune has already handled every other orphan. Its
directory or branch can outlive the failure even after git stops listing the tree, so a later
prune may not see it; the decision is the record to clean it up from. On Windows the usual cause
is a process whose cwd is still inside that tree.

If `summary.worktrees_removed > 0` or `summary.failed` is non-empty: persist `status.md`.

If `plan.per_wave_worktrees` is False: skip silently.

**Releasing a wave tree (every return to `ready`).** A task re-dispatched from `ready` gets a new
tree under the next wave number, so the tree it had is never used again. Whenever a step sends a
task with a non-null `state.tasks[tid].wave_worktree_path` back to `ready` (the restart abandon in
Step 1.2, the Step 4.0 timeout `requeue`, the Step 7 reconcile `requeue`, and a human reset of a
`blocked` task), release that tree first, then clear the fields. A best-of-N task marked `done`
at reconcile releases its tree the same way, since nothing built in it, and so does a best-of-N task the BLOCKED handler re-dispatches after an unlanded winner, or merges back itself, any task the `verifier_blocker_persistent` handler re-dispatches, any task the hallucinated-denial branch re-dispatches, and any task the first-BLOCKED handler re-dispatches (Step 7). An adversarial task whose
workflow proved its land (**Adversarial land proof**, Step 4 reconcile) only has its fields
cleared, whatever its outcome: the workflow already removed that tree and branch, and a release
would fail on a branch that is gone. A `done` single-shot
task keeps its path only when its merge-back failed, so the user can inspect that tree:

```python
from agentflow.wave_worktree import release_wave_worktree
released = release_wave_worktree(
    path=Path(state.tasks[tid].wave_worktree_path),
    branch=state.tasks[tid].wave_branch,
    main_worktree=worktree,   # resolved in Step 1.1 — never Path.cwd()
)
state.tasks[tid].wave_worktree_path = None
state.tasks[tid].wave_branch = None
```

Log `released.branches_preserved` and `released.failed` with the same decisions the prune above
uses: a requeued task may have committed before it stalled, and its branch is then the only copy
of that work. Clear the fields even when the release failed: the tree is then an orphan, and the
next wake's prune retries it. Leaving the path set would keep a dead tree live on every later
wake. With `plan.per_wave_worktrees` False the fields are never set and there is nothing to
release. A requeue, or a human reset of a task the timeout ladder blocked, releases a tree only
after **Stopping a wave run before a requeue** (below) has stopped the run or found it already
over; a removal that still fails (on Windows, a held cwd refuses it) lands in `released.failed`
for the next prune to retry.

**Stopping a wave run before a requeue (the Step 4.0 timeout `requeue` and `block` rungs and the
Step 7 reconcile `requeue`).** A wave run belongs to the whole wave group, not to one task:
stopping it stops every task it builds, including a sibling partway through its merge-back into
the shared epic tree. Read the run id from the task's latest dispatch decision note
(`wave <n> dispatched: tasks [...] run <runId>`, Step 7 item 4 sub-step 3): it is the token after
the final ` run ` in the summary (`run (\S+)$`), and the bracketed list names the group. With no
readable run id, go straight to the last case below. Throughout, "requeued" includes a Step 4.0
`block` in the same pass, whose task a stopped or finished run leaves `blocked` with its tree.
- If any task the note lists is still `in_flight` and is not being requeued in this same pass, do
  not stop the run: defer. Keep the task's tree and its fields and leave the task as it is, not
  `ready`. A later wake or the group's reconcile applies this rule again; once the run has ended
  on its own, the stop below answers "No task found" and the release goes ahead. The exception is
  a blocking sibling with no readable dispatch time (its `since` missing or unparseable): it never
  times out, so the defer would never end. Do not defer then; take the last case below and open
  its tier-3 escalation.
- When every task the note lists that is still `in_flight` is being requeued in this same pass,
  call `TaskStop` on the run id. If the stop succeeds, or fails with "No task found" (the run
  already finished), release the task's wave tree and clear its fields (**Releasing a wave
  tree**, above), then reset the task to `ready`.
- If no run id is readable, or the stop fails with any other error, the run may still be building
  in that tree: keep the tree and its fields, do not reset the task to `ready`, and open a tier-3
  escalation naming the task, its tree and the stop error (the rule a launch with no readable
  `runId` follows, Step 7.0). The open escalation holds the task until it is answered: Step 3
  exits before Step 4 while it is unanswered, so a later Step 4.0 pass does not re-fire the same
  timeout into a second escalation. Unlike a defer, this case keeps any increment Step 4.0 made
  this pass: a human has looked at the task by the time the escalation is answered, so its next
  timeout after that blocks it rather than requeuing it again.

### Step 2: Dispatch orc-bootstrap subagent (load state + pre-flight audit + ready set)

**Step 2.0 — Reconcile the board with the plan (every wake, before orc-bootstrap).**

A plan may gain tasks mid-epic; the board follows it on the next wake. Removing a task from the
plan also requires removing it from `status.md`, or the epic halts on it. The ready set is
computed from `plan.tasks` while every state write goes through `state.tasks`, which bootstrap
fills once, so the two sets are diffed here through the one helper the inline fallback also
calls. orc-bootstrap stays read-only: the board it reads is already reconciled and persisted.

`state` here is the in-memory board Step 1 already holds, never a fresh `read_status`: Step 1's
wake counter, events and decisions are not persisted yet, and a re-read would drop them.

```python
from agentflow.plan_parser import parse_plan, reconcile_board
from agentflow.status_writer import sync_orphan_escalations, write_status

plan = parse_plan(plan_path)
board = reconcile_board(plan, state)
for tid in board.orphaned_done:
    # Logged, not recorded as a decision: the set is stable, so a decision would repeat every wake.
    print(f"task {tid} is done on the board and no longer in the plan; left alone")
escalations_changed = sync_orphan_escalations(state, board)
if board.orphaned_open:
    persist_and_exit(state, status_path)  # no dispatch: not a dependency deadlock
    return
if board.added or escalations_changed:
    # Persisted BEFORE orc-bootstrap reads the board, so no ready-task write hits a missing key.
    write_status(status_path, state)
```

`reconcile_board` adds each plan-only task as `pending` with its plan phase and appends a tier-1
decision naming it. A board-only task that is not `done` halts the epic with exactly the text
`orphan_escalation_text` returns, never the deadlock escalation; a board-only `done` task is
logged and left alone. `sync_orphan_escalations` keeps exactly one open escalation per orphaned
task across wakes, keeps a `blocked` or held status instead of overwriting it, and answers an
orphan escalation once the operator has fixed the board or the plan, which returns the epic to
`running` when nothing else is open.

Dispatch the `orc-bootstrap` named subagent. It loads state, runs the pre-flight dependency
audit and computes the ready set in its own isolated context, returning a typed JSON struct the orchestrator parses with one call.

```python
from agentflow.bootstrap_dispatch import parse_bootstrap_return, BootstrapMalformed

# Dispatch the orc-bootstrap named subagent
result_json = dispatch_named_subagent(
    agent="orc-bootstrap",
    prompt=f"Bootstrap epic `{plan_path}` in worktree `{worktree}`. Return dispatch instructions.",
    # `worktree_path` is REQUIRED, not decorative: orc-bootstrap runs the repo-identity check
    # (`remote get-url origin`) and the orphan-commit scan, and it is dispatched from the
    # launch directory like everything else — with no path it resolves against nothing.
    # The key string is `worktree_path`, matching the name `agents/orc-bootstrap.md` declares
    # in its Inputs section alongside `plan_path` / `status_path`. A key the agent does not
    # recognise reads to it as an absent path, and its absent-path rule is a hard stop —
    # `worktree_unresolved` with `deadlock: true` — so a rename on either side halts every wake.
    inputs={"plan_path": str(plan_path), "status_path": str(status_path),
            "worktree_path": str(worktree)},
)
try:
    bootstrap = parse_bootstrap_return(result_json)
except BootstrapMalformed as exc:
    # See BootstrapMalformed handling below
    _handle_bootstrap_malformed(exc, state, retry_count=0, worktree=worktree)
    return
```

On success: `bootstrap.ready_tasks` carries the task dispatch list, `bootstrap.in_flight_reconcile` replaces Step 4's crash-recovery logic,
`bootstrap.escalations` are passed directly to `open_escalation()`, and
`bootstrap.preflight_audit_status` is written to `state.preflight_audit_status`.

**Apply bootstrap results:**

```python
# 1. Apply preflight audit status
state.preflight_audit_status = bootstrap.preflight_audit_status

# 2. Surface any escalations
for esc in bootstrap.escalations:
    open_escalation(state, task=esc.task, question=esc.question, eid=esc.id)
if bootstrap.escalations:
    state.status = "preflight_audit_blocked" if bootstrap.preflight_audit_status == "findings" \
        else "escalation"
    persist_and_exit(state, status_path)
    return

# 3. Apply in-flight reconcile actions
for action in bootstrap.in_flight_reconcile:
    _apply_reconcile_action(state, action)

# 4. Deadlock check (replaces Step 6 deadlock branch)
if bootstrap.deadlock:
    # No status assignment here: open_escalation sets "escalation" itself, and a write
    # before the call is discarded. A deadlocked epic surfaces as an escalation.
    open_escalation(state, task=0, question="Deadlock: no ready tasks, no in-flight tasks, "
                                            "not all done (bootstrap.deadlock=true).")
    agentflow.ntfy.notify_once(state, key="deadlock", trigger_type=TriggerType.HARD_FAILURE,
                               epic=state.epic, detail="deadlock detected")
    persist_and_exit(state, status_path)
    return

# 5. Mark ready tasks (replaces Step 6 ready-set write)
for task_instr in bootstrap.ready_tasks:
    state.tasks[task_instr.id].state = "ready"
```

**Proceed to Step 3** (early-exit checks) with `bootstrap.ready_tasks` available for Step 7.

## Fallback: inline state-load

If the orc-bootstrap subagent is unavailable (agent file missing, dispatch error, or
`BootstrapMalformed` persists after one retry), fall back to inline reads. This preserves emergency manual-recovery paths and lets existing epics
continue without re-bootstrap.

```python
def _handle_bootstrap_malformed(exc: BootstrapMalformed, state, retry_count: int, worktree):
    append_decision(state, task=0, tier=2,
        summary=f"BootstrapMalformed on {'retry' if retry_count else 'first'} call: "
                f"{exc.field_path}: {exc.message}. "
                f"{'Falling back to inline state-load.' if retry_count else 'Retrying once.'}")
    if retry_count == 0:
        # One retry — re-dispatch with a corrective hint
        # (Caller handles retry via _dispatch_bootstrap_with_retry)
        raise  # signal caller to retry
    # retry_count == 1: second failure → fall back to inline
    _fallback_inline_state_load(state, plan_path, status_path, worktree)


def _fallback_inline_state_load(state, plan_path, status_path, worktree):
    # Inline reads — the state load, pre-flight audit and ready-set compute orc-bootstrap does
    from agentflow.plan_parser import compute_ready_set, parse_plan, reconcile_board
    from agentflow.status_writer import read_status, sync_orphan_escalations, write_status
    import subprocess

    plan = parse_plan(plan_path)
    state = read_status(status_path)
    # Same reconcile as Step 2.0, before any ready-set compute: the emergency path writes ready
    # tasks through the same board.
    board = reconcile_board(plan, state)
    escalations_changed = sync_orphan_escalations(state, board)
    if board.orphaned_open:
        persist_and_exit(state, status_path)
        return
    if board.added or escalations_changed:
        write_status(status_path, state)
    # `-C worktree` is load-bearing on the emergency path too: this process's cwd is the
    # launch directory, so a bare `git log` scans a tree this epic never touched.
    subprocess.run(["git", "-C", str(worktree), "log", "--oneline",
                    f"--since={state.last_wake}"], check=False)
    # (Pre-flight audit and ready-set compute follow inline)
    # NOTE: This path intentionally omits the named-subagent forensic tracing.
    # It is the emergency path only.
```

**Fallback trigger conditions:**
1. `agents/orc-bootstrap.md` missing from skill directory (file not found error on dispatch)
2. Dispatch returns non-JSON or raises an exception (network, subprocess, or harness error)
3. `BootstrapMalformed` raised on first call → one retry with corrective hint
4. `BootstrapMalformed` raised on retry → fall back immediately (no third attempt)

### Step 3: Early-exit checks

**Fleet-context branch (all blocker-surfacing wakes in this step).** In a fleet-launched run
(see Fleet invocation context), every "buttoned prompt on the transition wake" below — the
blocked branch, `spec_quality_review`, `preflight_audit_blocked`, `awaiting_external_resolution`,
and the escalation branch — replaces its `AskUserQuestion` call with a registry `## Questions`
row per the fleet-context procedure: raise the row on the transition wake (same question text,
options folded into the question body, `default:` = the option the standalone prose recommends
or the no-state-change option), record the Q-id in the site's `*_prompted` decision-log marker,
then on each poll wake re-read the registry and — when `answered` — map the answer through the
site's canonical-reply mapping and execute that button's branch verbatim, flipping the row to
`applied`. The frontmatter-edit fallbacks stay live in both contexts (dev-manager's answer may
equally arrive as a direct `status.md` edit). Standalone interactive runs use the buttons below
unchanged.

- If `state.paused` is True:

  **Paused tick handling (in Step 1 main logic):**
  
  After Step 1.0-1.2 prologue, if `status.paused == True`:
  
  1. Check `status.halt_reason`. If set: release lock and exit WITHOUT calling
     ScheduleWakeup. The epic is intentionally terminated.
  
  2. Otherwise: ScheduleWakeup(delaySeconds=300, reason="paused; checking for unpause",
     prompt="/orchestrator <status_path>"). Release lock, exit.
  
  The pulse (Step 1.0) already wrote `last_wake = utc_now()`; the 5-min wake
  keeps `last_wake` fresh through indefinite pauses so the watchdog never
  falsely alarms on a paused epic.
- If `state.paused` was previously True and is now False (user flipped it back):
  - Call `append_session_event(state, "unpaused", "by user")` before continuing.
- If `state.status == "blocked"`: same as paused — surface to user, do not auto-resume, exit.

  **Buttoned prompt on the transition wake only.** If this is the wake that first entered
  `blocked` (i.e. no `blocked_prompted: <utc-now>` decision-log marker exists for this blocked
  session yet — fresh blocks always lack the marker), render the blocked reason as a context line
  + call `AskUserQuestion`. Subsequent surfacing wakes detect the marker and do NOT re-prompt;
  they just surface the existing state and exit. This avoids button-spam during multi-day blocks.

  This is the **generic** blocked branch — Step 9.2 substep 7d's `abandon` path (the merge-gate
  abandon branch) is a separate, terminal-stage decision and is NOT affected by this prompt. The
  Step 9.2 abandon path is reached only via `state.merge_decision = "abandon"`, never through
  this Step 3 blocked branch.

  Context line (prose, rendered immediately before the buttons — pulls from the most recent
  blocking decision-log entry or escalation summary so the user knows why they're being asked):

  > "Epic blocked. Reason: **{last decision-log entry summary, or open escalation summary if no
  > recent decision}**. Reopen as running, or abandon the epic?"

  Then call `AskUserQuestion`:

  ```yaml
  header: "Blocked epic"
  question: "Epic is blocked — reopen as running, or abandon?"
  options:
    - label: "Reopen as running"
      description: "Reset state.status to running; orchestrator resumes next wake"
    - label: "Abandon"
      description: "Mark state.status as abandoned; epic terminates"
  ```

  Button-click branches:
  - **Reopen as running** → write `state.status = "running"` directly (no frontmatter edit
    needed). Append a tier-2 decision: `blocked_reopen: user coarse-override; underlying tasks
    may still be marked blocked`. This is a coarse override — individual `state.tasks[tid]` that
    were marked blocked are NOT reset by this button. The user can either (a) manually flip
    those tasks back to `ready` via frontmatter edit (zeroing each one's `timeout_count`, and confirming its run is over per **Stopping a wave run before a requeue** before releasing its wave tree; see Step 4.0 `block`), or (b) leave them `blocked`: they stay out
    of the ready set, so they are not re-dispatched, and when nothing else can run the deadlock
    check (bootstrap, or the Step 6 fallback) stops the epic again. Fall through to
    Step 4 in the same wake.
  - **Abandon** → write `state.status = "abandoned"` directly. Call
    `append_session_event(state, "abandoned", "user-abandoned via blocked-branch button")`.
    Persist `status.md`, release lock, **do not schedule next wake**, exit (terminal).

  After AskUserQuestion (whichever branch is taken or no answer yet): write the
  `blocked_prompted: <utc-now>` decision-log marker so subsequent wakes skip the prompt. Both
  tool calls (AskUserQuestion + ScheduleWakeup at exit, EXCEPT on the Abandon branch which
  is terminal) must occur in the autonomous resume pattern.

  **Frontmatter-edit fallback preserved:** the user can still bypass the buttons by editing
  `status.md` directly to flip `status: running` (reopen) or `status: abandoned` (terminate).
  The Step 3 logic above honors either path on every wake.

- If `state.status == "spec_quality_review"`: same as blocked — Step 8's escalation-rate guard set this; user must review the spec and reset the status to `running` (or take another corrective action) before the orchestrator resumes. Surface to user, do not auto-resume, exit. **Re-prompt protection:** the buttoned `AskUserQuestion` for Override/Pause fires only on the transition wake (inside Step 8 when the threshold first trips). Subsequent wakes that land in this Step 3 branch detect the prior prompt via the `spec_quality_prompted` decision-log marker and do NOT call AskUserQuestion again — just persist, ScheduleWakeup 4 min, exit. Frontmatter-edit fallback (user edits `status: running` directly) remains active on every wake.
- If `state.status == "preflight_audit_blocked"`: same as blocked — user must review findings in status.md, set `state.preflight_audit_status = "acknowledged"` (or fix the underlying gaps the auditors flagged), then reset `state.status` back to `running`. Surface to user, do not auto-resume, exit.

  **Buttoned prompt on the transition wake only.** If this is the wake that first detected
  `preflight_audit_blocked` (i.e. the wake that just wrote the `preflight_audit_status =
  "findings_open"` decision-log entry — check by looking for the absence of a
  `preflight_audit_prompted` decision-log entry on this epic), render a short context line + call
  `AskUserQuestion`. On subsequent wakes while still in `preflight_audit_blocked`, do NOT
  re-prompt; just persist and exit (the frontmatter-edit fallback below is always available).

  Context line (prose, immediately before the buttons):

  > "Pre-flight audit found **<N findings>** across <M dimensions>: <dimension1>: <count>,
  > <dimension2>: <count>, ... Review status.md decision log for the per-finding detail."

  Then call `AskUserQuestion`:

  ```yaml
  header: "Pre-flight audit"
  question: "Pre-flight audit flagged <N> findings — how to proceed?"
  options:
    - label: "Acknowledge & resume"
      description: "Set preflight_audit_status: acknowledged and proceed despite N findings"
    - label: "Pause for manual fix"
      description: "Leave findings_open; you will fix the underlying gaps before resetting status"
  ```

  Button-click branches:
  - **Acknowledge & resume** → in a SINGLE write to `status.md`: set
    `state.preflight_audit_status = "acknowledged"` AND `state.status = "running"`. Append a
    decision-log entry `preflight_audit_acknowledged: user accepted N findings`. Then fall through
    to Step 4 in the same wake (do NOT exit early — the audit is unblocked and the next wave should
    dispatch).
  - **Pause for manual fix** → no state change. Append a one-line
    decision-log marker `preflight_audit_pause_acknowledged: user chose manual fix` so the
    prompt does not re-fire on subsequent wakes. Persist `status.md`, release lock,
    ScheduleWakeup 4 min, exit.

  Write `preflight_audit_prompted: <utc-now>` to the decision log immediately AFTER the
  AskUserQuestion call so subsequent wakes know not to re-fire. Both tool calls
  (AskUserQuestion + ScheduleWakeup at exit) must occur within the autonomous resume pattern;
  ScheduleWakeup MUST always be called before exit.

  **Frontmatter-edit fallback preserved:** the user can still bypass the buttons by editing
  `status.md` directly (set `preflight_audit_status: acknowledged` and `status: running`
  manually). The Step 3 logic above honors either path.
- If `state.status == "awaiting_merge_decision"`: route to Step 9.2 substep 7d — read `state.merge_decision` and act per the merge/hold/abandon/null branches. Do NOT proceed to Step 4 or subsequent wave-cycle steps; this is a terminal-stage wait.

  **Dual-input path:** `state.merge_decision` is set either by a button click (the `AskUserQuestion` fired in substep 7c on the transition wake) or by a frontmatter edit to `status.md`. Both paths write the same canonical string (`"merge"` / `"hold"` / `"abandon"`) to `state.merge_decision`, so substep 7d branches identically regardless of input source. The Step 3 branch detects `merge_decision_prompted` in the decision log to distinguish a returning poll wake (already prompted) from a fresh button click arriving on this wake.
- If `state.status == "awaiting_external_resolution"`:
  **Fleet-context branch (this external-wait branch).** Restating the Step 3 contract at the point
  of use, because this branch and the ones just above it sit well below the section's opening
  statement of it. In a fleet-launched run the `AskUserQuestion` below is not made; it follows the
  fleet-context procedure this section opens with: raise a registry `## Questions` row with the
  option labels folded into the question body and `default:` = **Keep waiting**, the no-state-change
  option. Record the Q-id in
  the `external_wait_*` decision-log marker so the prompt does not re-fire. On each poll wake
  re-read the registry and, when the row is `answered`, map the answer through the same
  button-branch mapping below and execute that branch verbatim, then flip the row to `applied`. The
  frontmatter-edit fallback stays live in both contexts, and standalone interactive runs use the
  buttons unchanged.

  - Read `state.blocker_watcher` (a dict field on StatusState).
  - **If `state.blocker_watcher` is `None` or missing `next_wake`**: corrupted state (status says we're waiting on an external condition but no condition is configured). Open an escalation with detail `f"status=awaiting_external_resolution but blocker_watcher={state.blocker_watcher!r} — unrecoverable; manual intervention needed"`, set `state.status = "blocked"`, persist + release lock + exit. Do NOT proceed.

  - **Buttoned prompt on the transition wake only.** If this is the wake that first entered
    `awaiting_external_resolution` (i.e. there is no `external_wait_prompted: <utc-now>` decision-log
    marker for this blocker_watcher session yet), render a context line + call `AskUserQuestion`.
    Subsequent poll wakes detect the marker and do NOT re-fire — they proceed straight to the
    next-wake / poll handling below. This avoids button-spam during multi-day external waits.

    Context line (rendered as prose immediately before the question):

    > "External wait active: kind=**{blocker_watcher.kind}**, condition=**{blocker_watcher.condition}**.
    > Reason: {blocker_watcher.reason}. Polls so far: **{blocker_watcher.poll_count}**;
    > max_wait elapsed: **{elapsed_human}** / {max_wait_human}."

    Then call `AskUserQuestion`:

    ```yaml
    header: "External wait"
    question: "Blocker watcher is polling an external condition — how to proceed?"
    options:
      - label: "Keep waiting"
        description: "Continue polling per blocker_watcher schedule (poll_count rendered in question)"
      - label: "Force re-check now"
        description: "Skip to next poll immediately (sets next_wake to now)"
      - label: "Mark resolved"
        description: "Override the watcher; flip status back to running"
      - label: "Abandon wait"
        description: "Set status to blocked; manual intervention required"
    ```

    Button-click branches:
    - **Keep waiting** → no state change. Append a one-line decision-log marker
      `external_wait_keep: user chose continue` (this is the one-time acknowledgment so the
      prompt does not re-fire on subsequent polls). Continue to the next-wake / poll handling
      below (the normal poll cadence resumes).
    - **Force re-check now** → mutate `state.blocker_watcher["next_wake"] = utc_now()` AND
      call `ScheduleWakeup(delaySeconds=60, reason="user-requested re-check")` so the
      blocker-watcher hand-off fires on the next tick. Append decision
      `external_wait_force_recheck: user requested immediate poll`. Persist `status.md`,
      release lock, exit.
    - **Mark resolved** → set `state.status = "running"`, clear `state.blocker_watcher = None`
      (override the watcher entirely). Append decision
      `external_wait_marked_resolved: user override; watcher cleared`. Fall through to Step 4
      in the same wake (the epic resumes immediately).
    - **Abandon wait** → set `state.status = "blocked"`, leave `state.blocker_watcher` for
      forensic reference but mark it `abandoned: true`. Append decision
      `external_wait_abandoned: user gave up on external resolution; manual intervention needed`.
      Persist `status.md`, release lock, exit.

    After AskUserQuestion (whichever branch is taken or no answer yet): write the
    `external_wait_prompted: <utc-now>` decision-log marker so subsequent wakes skip the
    prompt. Both tool calls (AskUserQuestion + ScheduleWakeup at exit) must occur in the
    autonomous resume pattern — never gate the next wake on the question alone.

    **Frontmatter-edit fallback preserved:** the user can still bypass the buttons by editing
    `status.md` directly (e.g. flip `status: running` and clear `blocker_watcher`, or flip to
    `blocked`). The existing poll-handling logic below honors either path on every wake.

  - If `blocker_watcher["next_wake"]` is in the past OR `None`: hand off via Skill tool to `blocker-watcher` (when installed). That skill polls the configured probe, updates `poll_count` + `last_polled_at` + `next_wake`, and either flips `state.status` back to `running` (probe passed) or flips to `blocked` (max_wait exceeded). Return from this Step 3 branch after the hand-off.
  - If `blocker_watcher["next_wake"]` is in the future: do not poll yet — persist status.md, ScheduleWakeup at `next_wake` (or 4 min, whichever is sooner), release lock, exit.
- If `state.status == "escalation"`:
  **Fleet-context branch (this escalation branch).** Restating the Step 3 contract at the point of
  use, because this branch sits ~190 lines below the section's opening statement of it. In a
  fleet-launched run the discrete-choice escalation prompt below is not
  buttoned; it follows the fleet-context procedure this section opens with: raise a registry
  `## Questions` row carrying the escalation question and its `options` array, with `default:` =
  the recommended option, or the no-state-change option when the
  prose recommends none. Record the Q-id in the escalation's decision-log marker. On each poll wake
  re-read the registry and, when the row is `answered`, pass the answer text to
  `answer_escalation(state, eid=<id>, answer=<answer>)` exactly as a chat reply would, then flip the
  row to `applied`. The chat-reply and frontmatter-edit paths stay live in both contexts, and
  standalone interactive runs use the buttons unchanged.

  - **If the user replied in chat this wake** (orchestrator is foreground, has a fresh user message answering an open escalation): call `answer_escalation(state, eid=<id>, answer=<user reply text>)` for each escalation the chat reply addresses. The helper writes the answer to `escalations[<id>].answer`, emits `session_escalation_answered: <id> answered`, and — if all escalations are now answered — flips `state.status` from "escalation" to "running" automatically. Do NOT mutate `state.status` or `escalations[].answer` directly; always go through the helper so the event lands and the status transition is atomic.
  - **Buttoned prompt for discrete-choice escalations (transition-wake only).** Before falling back to the chat-reply / frontmatter-edit path, scan the open (unanswered) escalations for a *discrete-choice signal*:
    - The escalation's source wave entry (in `state.tasks[tid].escalation_requests`, populated at dispatch time) includes a non-empty `options` array of `{label, description}` objects, OR
    - The escalation question is a yes/no shape (e.g. matches a heuristic like ending in "?" with a binary recommendation).

    If at least one open escalation has a discrete-choice signal AND no `escalation_prompted: <eid>` decision-log marker exists for that escalation yet (i.e. this is the wake that first detects an unanswered discrete-choice escalation, not a subsequent polling wake), call `AskUserQuestion`. Buttons fire ONLY on this transition wake; subsequent polling wakes detect the marker and skip the call. Multiple open discrete-choice escalations are handled one-at-a-time — call `AskUserQuestion` for the **first** unanswered discrete-choice escalation only; the next wake will pick up the next one.

    Question structure (one question = one escalation):

    ```yaml
    header: "Escalation"
    question: "<escalation.question text>"
    options:
      # Derived verbatim from escalation.options entries (max 4 labels rendered;
      # if the wave emitted more than 4, the orchestrator picks the first 4 and
      # appends a "see chat reply" Other fallback). Labels MUST be validated
      # against reserved canonical replies (y/n/merge/hold/abandon/s/p/t) — on
      # collision, fall through to free-text path instead.
      - label: "<entry.label>"
        description: "<entry.description>"
      # ... up to 4
    ```

    Button click → call `answer_escalation(state, eid, <clicked-label>)` — the SAME helper as the chat-reply path. The label string is recorded verbatim as the escalation's `answer` field. The helper emits `session_escalation_answered` and flips status atomically. After AskUserQuestion: write a `escalation_prompted: <eid> at <utc-now>` decision-log marker so subsequent wakes do not re-fire the buttons, then persist + ScheduleWakeup 4 min + exit (the user's button click arrives on the next wake; the autonomous resume pattern requires both AskUserQuestion AND ScheduleWakeup in the same wake).

    If no discrete-choice signal on any open escalation, fall through to the existing chat-reply / frontmatter-edit path verbatim. Existing escalations without an `options` array continue to work unchanged.
  - Then check the answer state of every escalation:
    - If all answered (whether via chat reply this wake or via prior frontmatter edit): the helper has already cleared `state.status` to "running" and emitted the events. Continue to Step 4 (no-op when bootstrap ran; apply reconcile inline only in fallback mode).
    - If any unanswered: log + release lock + ScheduleWakeup in 4 minutes (re-check for user answer) + exit.
  - If you find escalations whose `answer` field is filled in but `state.status` is still "escalation" (state written without the helper, or a hand-edited frontmatter), call `answer_escalation(state, eid, esc.answer)` once per such escalation with the existing answer text — this re-emits the event and triggers the status transition. The helper is safe to call with the prior answer text; it just records that the answer was honored on this wake.

### Step 4: Reconcile in-flight tasks (handled by orc-bootstrap)

**Sub-step 4.0 — Wave-timeout check (runs BEFORE the commit-on-branch / orc-bootstrap reconcile below).**

Call `agentflow.lifecycle.check_wave_timeouts(state, plan, now)` **once per wake**, with `now = datetime.now(timezone.utc)` (timezone-aware UTC; a naive `now` raises TypeError). One call covers every task, so calling it per task would return each event N times and increment N times. The helper
inspects each `state.tasks[tid]` with `state == "in_flight"`, compares `now - state.tasks[tid].since` to
the task-level `wave_deadline_minutes` on the plan's task (falling back to the plan-level
`verification.wave_deadline_minutes`, default 30), and returns a list of `TimeoutEvent` objects
for any task whose wave has exceeded its deadline. The helper is pure: it reads
`timeout_count` to pick `event.suggested_action` but never writes it, so **this step owns the increment**.

For each `TimeoutEvent` (with `tid = event.task_id`):
- Append a tier-2 decision (`append_decision(state, task=event.task_id, tier=2, summary=event.summary, evidence_path=f"status.md tasks.{event.task_id}.since={event.dispatched_at}")`). There is no per-task dispatch record to link (the wave variant script is per wave and step 5 deletes it), so the evidence is the dispatch timestamp Step 7 wrote, which names the dispatch this timeout measured from. A pass whose `requeue` or `block` defers (below) skips this entry.
- Increment `state.tasks[tid].timeout_count += 1` BEFORE applying the action, then apply `event.suggested_action`:
  - `requeue` (first timeout for this task — `state.tasks[tid].timeout_count == 1` after this step's increment): the run may still be building, so apply **Stopping a wave run before a requeue** (Step 1.2.5) before any release. When it releases the tree (**Releasing a wave tree**) and resets the task to `state = "ready"`, also clear `state.tasks[tid].since` and let Step 7 re-dispatch on the next wave cycle. When it defers, take back this pass's increment (`state.tasks[tid].timeout_count -= 1`): nothing was requeued, so the ladder has not moved. A deferring pass also skips the per-pass tier-2 decision above (the defer is decidable first: the rule reads only the dispatch note and the board) and logs at most one tier-1 `deferred: sibling <ids> still in flight` note per dispatch, so a defer that spans many wakes does not add an entry on each.
  - `block` (second timeout for this task — `state.tasks[tid].timeout_count >= 2` after this step's increment): the run may still be building, so apply **Stopping a wave run before a requeue** (Step 1.2.5) before marking the task blocked. When it defers, handle the pass as the `requeue` rung does: take back this pass's increment (`state.tasks[tid].timeout_count -= 1`) and log at most one tier-1 `deferred: sibling <ids> still in flight` note per dispatch; the task stays `in_flight` with its tree, and a later pass blocks it. Otherwise mark `state.tasks[tid].state = "blocked"`, open an escalation explaining the repeated timeout and whether the run was stopped (in the rule's last case, put this in its tier-3 escalation rather than opening a second), and let Step 3's blocked-branch surface the escalation. A human who resets this task to `ready` also sets `state.tasks[tid].timeout_count = 0`: the reset starts a fresh ladder, and a count left at 2 would block the task on its very next timeout. No step notices a reset, so whoever makes it first confirms the run is over, as **Stopping a wave run before a requeue** would (that escalation says whether it was stopped), then releases the task's wave tree and clears its fields (**Releasing a wave tree**, Step 1.2.5); a blocked task keeps its tree until then, since a human may inspect it.
- Persist status.md (`write_status`) once all events are applied. An unpersisted increment is lost at the next wake, and the ladder never reaches `block`.

This sub-step MUST run **before** the commit-on-branch reconcile (Step 2 bootstrap result OR the fallback inline block) so a wave that timed out but happens to share a commit-message signature with an unrelated landing commit is still detected as a timeout, not a false-positive completion.

In-flight task reconciliation is performed by the orc-bootstrap subagent (Step 2).
The orchestrator applies the returned `bootstrap.in_flight_reconcile` actions in Step 2's
result-handling block. This step is a no-op when the bootstrap path runs successfully.

**Fallback:** When running in inline state-load mode (see Step 2 Fallback subsection),
apply reconcile logic inline: for each task with `state == "in_flight"`, check
`git -C <worktree> log` for a matching commit (within `verification.file_allowlist`). If found: promote to review.
If `since` > 2 hours with no commit: mark `blocked`, open escalation.

### Step 5: Regression check

If any tasks completed since `state.last_green_commit`:
1. Run `plan.test_commands["regression"]` against `<worktree>` — a plan-authored command string
   carries no tree, so give it one the same way a wave does: the runner's own path flag where it
   has one, otherwise a subshell, `( cd <worktree> && <command> )`. Never a bare `cd`: it
   persists in the shell and wedges worktree removal on Windows. Run from the launch directory
   it would test whatever happens to be there, and a regression check that passed on the wrong
   tree is worse than one that did not run.
2. Failures present → this is a true regression (implementer's new tests are guaranteed green at task-complete time)
3. Dispatch `prompts/regression_debugger.md` subagent (synchronous):
   - Populate `{{WORKTREE}}` with `<worktree>` (Step 1.1). **Required** — the prompt bisects,
     edits and commits, and its own rule is to return BLOCKED rather than guess a tree, so a
     dispatch that omits it turns every regression into a blocked wave.
   - Provide: failing tests, last green commit, list of intervening commits
   - Subagent runs systematic-debugging, commits fix
4. Re-run tests:
   - Green → update `state.last_green_commit = HEAD`
   - Still failing:
     - **External-resolution exception (same pattern as Step 7):** if the regression debug agent's report includes `blocker_kind: external` (e.g., the regression is caused by a service being down, an env var being unset, a deploy in flight), do NOT set `state.status = 'blocked'`. Instead, populate `state.blocker_watcher` from the debug agent's descriptor (kind/condition/reason/interval/max_wait, `poll_count: 0`, `last_polled_at: null`, `next_wake: now + interval`), set `state.status = 'awaiting_external_resolution'`, append decision (`f"regression blocked on external condition ({kind}); handing off to blocker-watcher (interval={interval}s, max_wait={max_wait}s)"`), persist status.md, ScheduleWakeup at `next_wake`, release lock, exit. The blocker-watcher skill takes over polling.
     - Otherwise: `state.status = "blocked"`, ntfy HARD_FAILURE via `agentflow.ntfy.notify_once(state, key="hard_failure_<timestamp>", trigger_type=TriggerType.HARD_FAILURE, ...)`, exit
5. All green from start → `state.last_green_commit = HEAD`

**After every regression run (green OR red), regardless of branch taken:**
- Call `record_test_run(state, result=<"green"|"red">, regression_count=<count-of-failing-tests>)` — updates `state.test_status` and appends to `state.test_status_history`.
- Call `append_session_event(state, "test_run", f"<result>, regressions=<count>")` — emits an event in the decision log visible in the dashboard Wave Log.
- Do NOT call `record_test_run` twice for the same run (green initial run and green re-run after fix each count as one call).

### Step 6: Compute ready set (handled by orc-bootstrap)

Ready-set computation is performed by the orc-bootstrap subagent (Step 2).
The orchestrator applies the returned `bootstrap.ready_tasks` list directly in Step 7.
This step is a no-op when the bootstrap path runs successfully.

**Fallback:** When running in inline state-load mode (see Step 2 Fallback subsection),
compute the ready set inline. `_fallback_inline_state_load` has already run `reconcile_board`, so
every task in `ready_ids` has a board entry and a board-only task that is not `done` has already
halted the wake:

```python
done = {tid for tid, ts in state.tasks.items() if ts.state == "done"}
blocked = {tid for tid, ts in state.tasks.items() if ts.state == "blocked"}
ready_ids = compute_ready_set(plan, done, blocked=blocked)
```

For each `tid` in `ready_ids` not currently `in_flight`: mark `state.tasks[tid].state = "ready"`.
A `blocked` task is never in `ready_ids` and is never marked `ready` here; only a human or a
handler resets it.

Deadlock check: if no ready tasks AND no in-flight tasks AND not all tasks are done:
set `state.status = "blocked"`, open escalation, ntfy HARD_FAILURE, exit. The escalation names
every `blocked` task and says that answering it does not reset them; the reset step is to set
`state.tasks[<tid>].state` to `ready` (and its `timeout_count` to `0`, so a task blocked by Step 4.0's timeout ladder starts a fresh one) in the status.md frontmatter for each task to run again, confirming its run is over per **Stopping a wave run before a requeue** (Step 4.0 `block`) before releasing its wave tree and clearing its fields (**Releasing a wave tree**, Step 1.2.5).

### Step 7: Dispatch ready tasks

**Step 7.0 — Per-wave worktree setup (conditional).**

Step 7.0 runs once per parallel-safe group, at the start of that group's item 4 sub-step 1,
just before its `build_wave_args` call, for each task in that group. Each group is its own wave
with its own number: item 4 sub-step 2 calls `increment_wave` once per group, so a wake that
dispatches two groups gives them two consecutive numbers. Read `state.total_waves + 1` at that
group's own Step 7.0, after the previous group's increment, never once for the whole wake: a
number read before the first group's increment names the second group's trees after the first
group's wave, while its workflow names its candidates after its own.

- If `plan.per_wave_worktrees` is `True`:
  ```python
  from agentflow.wave_worktree import create_wave_worktree
  group_wave_n = state.total_waves + 1   # this group's number; its increment_wave runs after build_wave_args
  wave_path = create_wave_worktree(
      repo_path=worktree,   # resolved in Step 1.1 — never Path.cwd()
      epic_slug=plan.slug,
      wave_n=group_wave_n,
      task_id=task.id,
      base_branch=epic_branch,
  )
  ```
  Record `state.tasks[task.id].wave_worktree_path = str(wave_path)` and
  `state.tasks[task.id].wave_branch = f"{plan.slug}-w{group_wave_n}-t{task.id}"`.
  Persist `status.md`.

  **Every exit between Step 7.0 and the workflow launch releases this group's trees.** Until
  item 4 sub-step 3 launches the workflow, no agent builds in a tree Step 7.0 made, and nothing
  else will release it. So any exit before that launch (the empty-body refusal, any other
  exception `build_wave_args` raises, `wave_script_dir` or `bake_wave_script` raising, or a
  Workflow call that errored, so no run launched) releases every tree Step 7.0 made for this
  group and clears its fields (**Releasing a wave tree**, Step 1.2.5), resets any group task
  already marked `in_flight` back to `ready`, persists `status.md`, and only then raises,
  escalates or exits. Item 4 sub-step 1 spells out the empty-body case. Release only when the
  launch definitely did not happen: a Workflow call that returned without error but with no
  readable `runId` may have started a run that builds in these trees, so keep the trees and
  fields and open a tier-3 escalation naming the group's tasks and trees instead. An exit after
  item 4 sub-step 2 comes after `increment_wave`: the counter has moved and the `wave_dispatched`
  session event stands, so also append a tier-2 decision `"wave {n} not sent: {reason}"`; the
  next dispatch takes the next number.
- If `plan.per_wave_worktrees` is `False` (legacy): create no per-wave worktree — every task in
  the wave builds directly in `<worktree>`. **The subagents must still be TOLD that path** (see
  the worktree-context block below, which is emitted on this path too, naming `<worktree>` where
  the per-wave path would otherwise appear). Nothing puts the subagents in the right tree for them,
  so silence on this path means the wave builds wherever the harness dropped it.

1. Parse the Files section (`#### Files:`, or the legacy `## Files:` / `**Files:**`) of each ready task body in the plan markdown (not frontmatter — the body)
2. Group ready tasks into "parallel-safe" groups: tasks whose Files: lists are disjoint can run together
3. **Assemble operational-context block** (per-dispatch; read fresh — no caching):
   - If `plan.extra_context_files` is non-empty, for each `ContextFile` in order:
     - Read the file from disk. If `path` is absolute, use as-is; otherwise resolve relative to repo root.
     - On read failure (missing / permission / IO error): append a WARN entry to the status decision log (`extra_context_file_unreadable: <path>: <error>`); skip this file; continue with others. Do NOT block dispatch.
   - Render the block as:
     ```
     ## Operational context (read first, treat as binding)
     These runbooks describe operational reality you must honor. Stop and surface
     any conflict between your task and these runbooks rather than diverging.

     ### {file.path}
     _Why: {file.why}_

     {file contents}

     ---

     ### {next_file.path}
     ...
     ```
   - This block is prepended to **every** implementer subagent's prompt for this dispatch wave (BEFORE the task body).
   - **Worktree context (ALWAYS — this block is never omitted):** prepend a `## Worktree context`
     block to the wave subagent prompt. Let `{wave_tree}` be
     `state.tasks[task.id].wave_worktree_path` when `plan.per_wave_worktrees` is True, and
     `<worktree>` when it is False; let `{wave_ref}` be `state.tasks[task.id].wave_branch` or the
     epic branch, correspondingly. **Fill `{wave_tree}` and `{wave_ref}` at item 4 sub-step 1,
     not here.** On the per-wave path those fields do not exist yet: Step 7.0 writes them at the
     start of item 4 sub-step 1, once per group. Read them after that group's Step 7.0, immediately
     before the "Prepend the worktree-context block" step there. A value read at this point is
     null, or a previous wave's path, and the whole wave then builds in the wrong tree. Item 3
     fixes only the block's text and its place in the prompt.

     ```
     ## Worktree context (read FIRST, before any other action)

     Build in `{wave_tree}`, a git worktree on branch `{wave_ref}`. Name that tree in
     every command:

     - Every git call carries `-C {wave_tree}`: `git -C {wave_tree} add`,
       `git -C {wave_tree} commit`, and the rest.
     - Every file edit and test run uses an absolute path under `{wave_tree}`. Where a
       runner takes a path flag, use it (`npm --prefix {wave_tree} test`); where it has
       none, wrap it in a subshell: `( cd {wave_tree} && <command> )`.
     - Never a bare `cd`. The shell's working directory persists across calls, and on
       Windows a directory that is a live process's working directory cannot be removed,
       so the tree's cleanup fails. This session was launched from a directory that is
       not a repository, so a command that names no tree resolves against nothing.

     Do NOT work in another tree. Do NOT `git checkout` other branches.

     Your branch is merged back into the epic branch for you. You do not need to push
     or PR — just commit.
     ```

     Place this block ABOVE the operational-context block. On the legacy (`False`) path the
     merge-back sentence does not apply — the wave commits straight onto the epic branch — so
     drop that final paragraph there and keep the rest verbatim.
   - If `plan.extra_context_files` is empty: no *operational-context* block is injected; existing prompt assembly proceeds unchanged. The worktree-context block above is unaffected — it is keyed on nothing and always emitted.
   - **Use-Case trailer injection (per ready task, Hop 3 of the use-case traceability spine):** if `plan.tasks[tid].use_cases` is non-empty, prepend a single `Use-Case: <comma-joined UC-IDs>` line to that task's brief body (above any operational-context block). The implementer prompt turns each ID into a `Use-Case: <id>` git trailer on every commit, so the flow-page ingestion (autodoc-update) can attach the UC to its flow page. Tasks with no `use_cases` get no line — behavior unchanged.
4. For each parallel-safe group (each Wave), dispatch the group as ONE Workflow instead of N Task subagents — **fire-and-reconcile**:

   1. Run Step 7.0 for this group's tasks first, then build args **before** touching the wave counter — `wave_n` is the number this wave is about to get (`state.total_waves + 1`, the same `group_wave_n` this group's Step 7.0 just named its trees with), so a refusal leaves `state.total_waves` unchanged:
      ```python
      from agentflow.wave_dispatch import build_wave_args, fallback_delay_seconds
      args = build_wave_args(
          plan=plan, plan_path=plan_path, group_task_ids=group_ids,
          status_path=status_path, wave_n=state.total_waves + 1,  # incremented in sub-step 2
          base_branch=epic_branch, repo_path=worktree,  # Step 1.1 — never Path.cwd()
          ntfy_url=os.environ.get("NTFY_URL"), ntfy_topic=os.environ.get("NTFY_TOPIC"),
          skill_dir=skill_dir)  # the orchestrator skill root — resolves each task's contract_path
      ```
      **Empty-body refusal.** `build_wave_args` raises `ValueError` when a group task's body in the plan is empty (`Task <tid> has an empty body in <plan path>: ...`). The implementer sees only that body, so the task cannot run. Do not dispatch the wave and do not increment the wave counter. Handle only a message matching `^Task (\d+) has an empty body`; any other exception is not this refusal, so release this group's Step 7.0 trees and clear their fields (**Releasing a wave tree**, Step 1.2.5), then re-raise it. On the refusal itself, release every wave tree Step 7.0 created for this group and clear its fields (**Releasing a wave tree**, Step 1.2.5): the counter stays put, so the re-dispatch after the answer reuses the same wave number, and a tree left behind holds that branch name. Then raise a tier-3 escalation: take the task id from that match and call `open_escalation(state, task=<tid>, question=f"Task {tid} has an empty body in {plan_path}; re-run /solution to restore the body, then answer to resume.")` (it sets `status = "escalation"` itself), then `agentflow.ntfy.notify_once(state, key=f"empty_task_body_{tid}", trigger_type=TriggerType.HARD_FAILURE, epic=state.epic, detail=<the same question>)`, persist `status.md`, and exit.
      `skill_dir` selects each task's wave-contract prompt (`prompts/implementer-with-verifier-loop.md`, or `prompts/implementer-with-tdd-loop.md` for `type: tdd` tasks) as `tasks[*].contract_path`; the workflow's implementer prompt instructs the wave subagent to Read and follow it.
      **`repo_path` is the only tree any wave subagent is ever told** — it surfaces as `A.repo_path`
      in `workflows/wave_group.js` and lands in the implementer prompt as "in repo …". A wrong or
      unresolved value here does not fail; it silently builds the whole wave somewhere else. That is
      why Step 1.1 escalates rather than defaulting.
      **Prepend the worktree-context block (item 3 above) to every task's `body` in
      `args["tasks"]` before firing — unconditionally, for every task in the wave.** Fill its
      `{wave_tree}` and `{wave_ref}` now, from the `wave_worktree_path` and `wave_branch` this
      group's Step 7.0 just recorded. `body` is
      the only free-text channel that reaches the implementer's prompt: `wave_group.js` embeds it
      verbatim, and the other `tasks[*]` fields it reads are structured inputs, not prose. A block that is declared "always emitted" but only written
      into `body` alongside some other block is not emitted at all on the path where that other
      block is empty, which is the common case. Then, if the operational-context block is
      non-empty, prepend its rendered text as well (below the worktree block, per item 3's
      ordering).
      **When `plan.per_wave_worktrees` is True, `build_wave_args` sets `wave_worktree` and
      `wave_branch` on every task in `args["tasks"]`**, derived from `repo_path`, the plan slug,
      `wave_n` and the task id exactly as Step 7.0 named the tree, and raises `ValueError` naming
      the path when that tree does not exist; when False it sets neither. The
      workflow reads them for adversarial tasks only: an adversarial task that carries both is
      landed by the workflow (see the merge-back notes under the review gates below), one that
      carries neither is treated as having built in the epic tree and is not merged by the
      workflow, and one that carries only one of the two is reported `blocked` without running.
      Leaving both off on the per-wave path is caught at reconcile (**Adversarial land proof**),
      which merges the tree back itself.
   2. Only after `build_wave_args` returns: `increment_wave(state)` and `append_session_event(state, "wave_dispatched", f"Wave {state.total_waves}")`. The wave's only decision-log entry is the dispatch decision note sub-step 3 appends once the run id is known; there is no other dispatch entry format. `state.total_waves` now equals `args["wave_n"]`.
   3. Mark each group task `state.tasks[tid].state = "in_flight"`, set `state.tasks[tid].since = now` as a UTC ISO 8601 timestamp (the dispatch time: reconcile only counts an outcome note written after it), and record the run by launching the wave Workflow. **DISPATCH MECHANISM (load-bearing): bake the args in.** The wave runs from a baked variant script, never from `Workflow(scriptPath=wave_group.js, args=<dict>)`. The Workflow tool does not bind the `args` global for `wave_group.js` in this harness (`const A = args` resolves to undefined and the wave fails at `A.tasks.map`), and workflow scripts cannot read files or env, so the inputs must be inlined as a literal. Call `agentflow.wave_dispatch.bake_wave_script(args, skill_dir, tag=f"{plan.slug}-w{state.total_waves}", out_dir=wave_script_dir(load_environment().data_root, plan.project))` (`from agentflow.config import load_environment`; `from agentflow.wave_dispatch import bake_wave_script, wave_script_dir`; `plan.project` is the same `<name>` step 5 of the bootstrap used for the status.md path — if the plan has no `project:`, `wave_script_dir` raises before touching the filesystem: add the field to the plan frontmatter (the bootstrap asked for it) rather than guessing a name) to write the variant to **`<data_root>/projects/<name>/status/workflows/wave_group_<tag>.js`** with the args object inlined as a literal, then call the **Workflow tool** with `scriptPath` = that variant path and **NO `args` param**. Record the returned `runId` in the dispatch decision note: for each group task, `append_decision(state, task=tid, tier=1, summary=f"wave {n} dispatched: tasks [{ids}] run {runId}")`, with `n = state.total_waves` and `ids` the group's task ids, comma-separated. That note is the only record of the run id: a requeue (Step 4.0, and the reconcile below) reads it back to stop the run, and step 5 reads `n` from it. Persist status.md. **The variant is project runtime state, not suite code** — it carries this project's absolute paths, so it lives under the project's `status/`, never beside the template in the orchestrator skill directory (`bake_wave_script` refuses that directory; the template `workflows/wave_group.js` stays read-only). The variant path is deterministic from (`data_root`, `plan.project`, `plan.slug`, wave number) — nothing needs to be persisted to find it again; step 5 deletes it. If `wave_script_dir` or `bake_wave_script` raises, or the Workflow call errors, no workflow holds this group's trees: release them first (**Every exit between Step 7.0 and the workflow launch releases this group's trees**, Step 7.0). A call that returned without error but with no readable `runId` may have launched a run: escalate, never release.
   4. **Fire-and-reconcile.** ScheduleWakeup with `delaySeconds = fallback_delay_seconds(plan, group_ids)` (the wedge-catcher fallback ≈ wave_deadline + slack) and exit the turn. The workflow's completion notification is the primary, fast wake; the fallback only fires if it wedged.
   5. **On the reconcile wake** (notification or fallback): the wave-event reporter has already written each task's terminal state to status.md. For each group task:
      - Compute `committed_task_ids` per group task with a commit-signature match over the log of the refs that can hold the task's commit. When `plan.per_wave_worktrees` is True: `git -C <worktree> log <state.tasks[tid].wave_branch> <epic_branch> --since=<state.tasks[tid].since>`. A single-shot task's commit sits on its wave branch, in the sibling tree, until merge-back on approval, so a log of the epic branch alone does not contain it; a task that merges back inside the workflow lands on the epic branch. When `plan.per_wave_worktrees` is False: `git -C <worktree> log --since=<state.tasks[tid].since>`.
      - `actions = reconcile_group_from_status_git(state, group_ids, committed_task_ids)`.
      - Delete the wave's baked variant — it is runtime state that has served its purpose: `out_dir = wave_script_dir(load_environment().data_root, plan.project); remove_wave_script(out_dir / f"wave_group_{plan.slug}-w{wave_n}.js", out_dir=out_dir)` where `wave_n` is the `state.total_waves` value step 3 baked with — read it back from the dispatch decision note step 3 wrote (`wave <n> dispatched: tasks [...] run <runId>`) rather than re-deriving it from a counter that may have moved (idempotent; returns False if already gone). A variant that outlives its wave means this step never ran.
      - If the aggregate workflow return is available, run `parse_wave_return(...)` and cross-check against status.md; on disagreement, trust status.md + git and log a tier-2 decision. The exception is a task whose return carries a non-empty `report_failures` list: a durable write for it was refused, dropped or never confirmed, so its status.md entry may be stale. For that task, trust the workflow return, re-issue that task's state write, rebuilt from the return's outcome, since a `report_failures` entry names what failed but not the fields (`python -m agentflow.wave_event task-state ...` with only the fields the reporter accepts) before reconciling. The return's `commit` goes through the same rule `wave_group.js` applies: trimmed first, `none`, `null`, `n/a` (any case) and empty mean no commit and add no `--commit`, and only 7 to 64 hex characters reach `--commit`; anything else is left off and never passed raw. Log the failures as a tier-2 decision.
      - Apply `coerce_wave_outcome` to every task result (defense-in-depth — see the coercion block below).
      - For `actions[tid] == "needs_review"` AND the task ran single-shot (`needs_orchestrator_review == True`): run the orchestrator review gates below (unchanged spec-reviewer then code-quality-reviewer loop). For best-of-N/adversarial tasks (`needs_orchestrator_review == False`): reviews already ran inside the workflow — mark done on approved, escalate otherwise. When a best-of-N task is marked done, release the per-task tree Step 7.0 recorded for it and clear its fields (**Releasing a wave tree**, Step 1.2.5): its candidates built in their own trees, so that tree was never used, and only the single-shot merge-back below clears a path otherwise.
      - **Adversarial land proof** (per-wave path, adversarial task with a non-null `state.tasks[tid].wave_worktree_path`). The workflow's word that it landed the task is its return: `merge_exit_code == 0` and `branch == state.tasks[tid].wave_branch`. On a fallback wake with no return, the proof is that `git -C <worktree> rev-parse --verify --quiet refs/heads/<wave_branch>` finds no branch and the task's status.md commit is an ancestor of the epic HEAD (`git -C <worktree> merge-base --is-ancestor <commit> HEAD`). Never infer a land from a `done` state alone: a task dispatched without its wave tree in args reports `done` with its commit still on the wave branch.
        - Proven, whatever the outcome (`done`, or blocked by the refuter panel or an inside review after the land): clear `wave_worktree_path` and `wave_branch` without a release, since the workflow removed that tree and branch and a release would fail on the missing branch. A tree the workflow failed to remove is listed in the return's `cleanup_failures`; once its path is cleared the next prune removes it as an orphan.
        - Not proven and the task is `done`: run the single-shot `merge_back` on its recorded tree (below) and follow that section's landed / not-landed handling, so a commit left on the wave branch reaches the epic branch or escalates.
        - Not proven and the task is not `done` (an unlanded or self-blocked task): keep the fields. The workflow kept that tree because it holds the only copy of the commit, and the release on its return to `ready` checks the branch for unmerged commits before removing it.
      - For `actions[tid] == "escalate"`: the wave reported the task `blocked`, or recorded a non-done outcome for it during this dispatch. Never reset it to `ready`. Take its outcome from the parsed return's `effective_outcome` when the return is available, otherwise from `reported_wave_outcome(state, tid)` (`from agentflow.wave_dispatch import reported_wave_outcome`), which reads the reporter's `wave outcome:` note, and route it to the matching handler below. On a fallback wake with no outcome recorded, open an Escalation for the task naming it as blocked with the outcome unknown, and exit rather than re-dispatching: a blocked task may be a tier-3 question, and re-dispatching it answers that question without the human. The same holds for any outcome no handler below names, including `unknown_outcome` and any garbled value: route it to the tier-3 escalation handler, never to re-dispatch. The ready set excludes `blocked` tasks (Step 6), so a task marked `blocked` stays out of dispatch until a human or a handler resets it.
      - For `actions[tid] == "requeue"`: the task never reported a terminal state, has no commit and recorded no outcome, so it wedged, and the group's run may still be building. Apply **Stopping a wave run before a requeue** (Step 1.2.5), which releases the tree (**Releasing a wave tree**) and resets the task to `ready` only once the run is stopped or already over; Step-4 wave-timeout handling governs repeat offenders.
      - For `effective_outcome == "escalation_required"`: existing escalation handler (below).
      - For `effective_outcome == "verifier_blocker_persistent"` / `test_failure_persistent`: hard subagent failure — the reporter has already written `blocked`, and `blocked` never re-enters the ready set, so set `state.tasks[tid].state = "in_flight"` (and `since = now`) explicitly, then re-dispatch once with more context. The re-dispatch goes back through Step 7.0, which makes a new tree under the next wave number, so before it, when the task records a `wave_worktree_path`, release that tree and clear its fields (**Releasing a wave tree**, Step 1.2.5); a tree left recorded stays live and is never pruned; if still blocked, mark task `blocked`, set `status=blocked`, ntfy HARD_FAILURE, exit. Record `verifier_iterations` and findings history to the decision log.
      - For `effective_outcome == "blocked"`: run hallucinated-denial detection first, then the BLOCKED handler (both below).
   6. Stabilization-test suggestions, merge-gate, and all Step 8/9 logic are unchanged.

   **Note on `task.verification is None`:** the workflow's implementer prompt degrades gracefully (empty allowlist/criteria lists). Append a WARN to `state.decisions`: *"task {tid} has no verification block — running without in-wave verifier scope; consider re-running solution to add verification frontmatter."*

   **Orchestrator review gates (single-shot reconcile path only):**
   - Dispatch the **spec-reviewer-evidence-wrapper** (`prompts/spec-reviewer-evidence-wrapper.md`), **substituting `{worktree}` with `state.tasks[tid].wave_worktree_path` when `plan.per_wave_worktrees` is True, and with `<worktree>` when it is False**. On the per-wave path the task's commit sits on its wave branch in that tree until the merge-back below runs on approval; the epic tree does not contain the change yet, so a reviewer pointed there reads and tests pre-change code. Apply `agentflow.subagent_contract.coerce_review_verdict()` immediately on return. Loop until ✅. **Always runs** for single-shot tasks — the spec-reviewer sees the work in a different context (whole-task review, not just-completed-diff review) and catches different classes of drift.
   - On spec-OK: dispatch the **code-quality-reviewer-evidence-wrapper** (`prompts/code-reviewer-evidence-wrapper.md`), **substituting `{worktree}` with `state.tasks[tid].wave_worktree_path` when `plan.per_wave_worktrees` is True, and with `<worktree>` when it is False**, for the same reason. Apply `coerce_review_verdict()` immediately on return. Loop until ✅. Always runs.
   - **On `inconclusive` coercion (either reviewer):** re-dispatch the implementer once with `agentflow.subagent_contract.build_enriched_redispatch_prompt(original, findings)`. Second `inconclusive` on same role → escalate per existing two-attempt rule.
   - On approval: mark `state.tasks[tid].state = "done"`, capture commit SHA. Record `verifier_iterations`, `verifier_verdict`, `verifier_findings_count` to `state.tasks[tid]`; append `integration_test_suggestions` (if any) for the end-of-epic stabilization-test summary.
   - **On approval, if `plan.per_wave_worktrees` is True:** call `agentflow.wave_worktree.merge_back`:
     ```python
     from agentflow.wave_worktree import merge_back, cleanup_wave_worktree
     result = merge_back(
         wave_worktree=Path(state.tasks[tid].wave_worktree_path),
         main_worktree=worktree,   # resolved in Step 1.1 — never Path.cwd()
         branch=state.tasks[tid].wave_branch,
     )
     ```
     `merge_back` fast-forwards when the epic branch has not moved since the wave tree was created, and otherwise lands the wave branch through a merge commit. Siblings in one parallel-safe group all start from the same epic HEAD, so every sibling after the first lands that way. The task commit is never rebased: its sha stays the one the wave tree committed, and the `committed_task_ids` log above (which does not follow first parents only) still finds it behind the merge commit. A conflict aborts the merge and leaves the epic branch and tree exactly as they were, and `merge_back` returns `success: False` with git's conflict output in `stderr`. A merge commit also runs the merge hooks and needs a committer identity, which a fast-forward never did, so a failing hook, a missing identity, a busy index or a dirty tree each refuse the land the same way. The fetch goes to a ref private to the wave branch, never to the shared `FETCH_HEAD`, so concurrent merge-backs into one epic tree cannot merge each other's tip.
     - On `result.success`: `cleanup_wave_worktree(path, branch, main_worktree, preserve_branch=False)`; clear `state.tasks[tid].wave_worktree_path` and `.wave_branch`; append tier-1 decision `"wave merged back: {branch} → epic"`.
     - On `result.success is False`: tier-3 escalation — append decision `"merge-back failed for {branch}: {result.stderr}"`, set `state.status = "escalation"`, ntfy `MERGE_BACK_FAILED`, persist `status.md`, exit dispatch loop. Do NOT delete the wave worktree (user inspects manually).
     This orchestrator-side merge-back applies to the single-shot per-wave-worktree path. **Best-of-N and adversarial tasks land inside the workflow instead.** `wave_group.js` runs the worktree CLI's `merge-back` for the judged winner and reads its result `{merge_exit_code, success, stderr}`; it counts the winner as landed only on exit 0 with `success: true`. Group tasks run concurrently, so the epic branch moving under that merge is an ordinary outcome, not an edge case: it lands through a merge commit. Concurrent lands are not serialised: a conflict, or any other refused merge (a failing hook, a missing committer identity, an index another land holds), leaves the winner unlanded.
     - Landed (on the first attempt or the retry below): the workflow removes the winner's worktree and branch and every losing candidate's, then runs the inside reviews (and the refuter panel when the task is adversarial).
     - Not landed (any other result, including a land agent that returned nothing): the workflow retries the land once, because a concurrent land in the same group (an index it holds, an epic branch it moved) is the usual refusal, and keeps the winner's tree only through that retry. Refused again, it removes every candidate tree, the winner's included, and deletes the losers' branches, but keeps the winner's branch (`cleanup --preserve-branch`), which holds the only copy of the commit. The tree adds nothing to the branch (merge-back fetches a branch from any tree of the repository), and a kept tree leaks one tree per re-dispatch. It skips the inside reviews and reports the task `blocked` with a `wave outcome: blocked (...)` note that carries merge-back's stderr, and returns `outcome: "blocked"` with `merge_exit_code`, `merge_stderr`, the kept `branch` and the unlanded `winner_commit` (never `commit`). Reconcile routes it to the BLOCKED handler.
     - A candidate cleanup that does not exit 0 is listed in the return's `cleanup_failures` and in a task note; it does not change the outcome. Remove each listed tree with the worktree CLI's `cleanup` command.
     - All candidates failed tests: no candidate is eligible, so the workflow removes every candidate's tree and branch, reports the task `blocked` with a `wave outcome: verifier_blocker_persistent (...)` note, and returns that outcome.
     - Judge votes: each judge returns a 0-based index into the eligible candidates. A vote that is missing, not an integer, or out of range is dropped, and only the remaining votes are tallied. When no valid vote remains, the workflow reports the task `blocked` with the note `wave outcome: blocked (best-of-N: no valid judge vote)`, removes every candidate's tree and branch (with no winner, no candidate holds a commit that will land), and returns `outcome: "blocked"` with empty `judge_votes`. It does not throw, so the task stays in the wave's return and reaches the BLOCKED handler like any other blocked task.
     - **Adversarial tasks** land through the same merge-back and the same landed / not-landed handling, with the task's own wave tree and branch (`wave_worktree`, `wave_branch`, set on its args at Build args above) in place of a winner. The workflow runs `merge-back` once the implementer reports `done` and before the refuter panel and the inside reviews, because those read the epic tree. Landed: it removes the wave tree and branch, then runs the refuter panel and the inside reviews. Not landed: it keeps the tree, skips the reviews, and returns the same blocked shape as an unlanded best-of-N winner, with the implementer's commit as `winner_commit`. An implementer that reports `done` while counting one or more blockers in `verifier_findings_count` is not landed either: that outcome is `verifier_blocker_persistent`, which is re-dispatched, and the retry must not build on a blocked commit. The workflow keeps the tree, skips the reviews, writes `blocked` with the verifier fields and a `wave outcome: verifier_blocker_persistent` note, and returns that outcome with the commit as `winner_commit`. An adversarial task whose args carry no wave tree built in the epic tree, and nothing is merged; one whose args carry only one of `wave_worktree` and `wave_branch` is reported `blocked` before its implementer runs. The implementer's `verifier_verdict` and `verifier_findings_count` ride the task's state write and its return, so `coerce_wave_outcome` (below) sees a self-reported blocker. A refuter-panel or review block after a landing leaves the commit on the epic branch, as it does for a best-of-N winner.
   - **Stabilization-test suggestions:** if a task result includes `integration_test_suggestions` (any outcome — done, warnings, or even verifier_blocker_persistent), call `agentflow.stabilization_tests.append_suggestions(epic_slug, epic_name, suggestions)` for each one. Creates `stabilization-tests/<epic-slug>.md` on first write. Step 9 surfaces the count at epic completion.

**Wave auto-mode contract:**

Wave subagents run in auto-mode by default (`task.auto_mode: true` — the plan_parser default). The wave prompt instructs autonomous execution within the verification scope. Tier-3 ambiguity escalates via JSON (`outcome=escalation_required`, `escalation_requests` field); tier-1/2 decisions are self-made and recorded in `decision_log`. The wave MUST NEVER inline-ask the parent — there is no inline question channel.

**Do not author bespoke git rules into wave prompts.** The wave templates' "Git hygiene" section already covers scoped staging and the stash-pull-push pattern. Layering additional rules like "do not stash" contradicts documented patterns, and a wave cannot follow both.

Tier mapping (ambiguity tiers, by cost to undo):

| Tier | Scope | Wave behavior | JSON field |
|---|---|---|---|
| **Tier-1** (low-stakes) | Naming, formatting, local file placement WITHIN file_allowlist | Auto-decide, state assumption, proceed | `decision_log` entry with `tier: 1` |
| **Tier-2** (medium-stakes) | Architectural choices, scope edges within plan intent | Recommend-and-proceed, record rationale | `decision_log` entry with `tier: 2` |
| **Tier-3** (high-stakes) | Security model, data-model, irreversible writes, anything outside file_allowlist or plan acceptance scope | STOP — return `outcome=escalation_required`, populate `escalation_requests` | `escalation_requests` entry + `outcome=escalation_required` |

**When in doubt between tier-2 and tier-3: prefer recommend-and-proceed.** Tier-3 is for changes that cannot be reversed without human review — security model changes, schema migrations, irreversible writes, files outside the allowlist.

`file_allowlist` and `regression_guard` are HARD GATES even in auto-mode. Any file outside the allowlist is automatically tier-3 regardless of how innocuous the change looks.

**Escalation-required path (Step 7 handler):**
1. For each `escalation_requests` entry: call `open_escalation(state, task=tid, question=<the
   escalation question from that entry>)` — `tid` is the task whose wave returned
   `escalation_required`, the same key Step 7 uses for `state.tasks[tid].escalation_requests`.
   `task` is required; there is no `detail` parameter, and `asked` is stamped by
   `open_escalation` itself.
   **Fleet-context branch:** in a fleet-launched run, ALSO write one registry `## Questions`
   row per entry in the same wake (per the fleet-context procedure: question = the escalation
   question; `default:` = the entry's recommendation if present, else its first `options`
   label; urgency `waiting`; raised with `registry ask --actor <fleet_context>`, never
   hand-written); record each Q-id
   against its escalation id in the decision log. Step 3's escalation branch then never fires
   `AskUserQuestion` for these — on poll wakes it reads the registry rows and, when answered,
   feeds each answer through `answer_escalation(state, eid, <answer text>)` (the same helper
   as a chat reply) and flips the row `applied` (`registry apply --actor <fleet_context>`).
2. Mark task `state=in_flight`.
3. Set `state.status = "escalation"`.
4. Persist `status.md` + ScheduleWakeup 4 min + exit dispatch loop.
5. On next wake: Step 3 detects answered escalation → `state.status = "running"` → re-dispatch wave with `## User answers` context block (each `escalation_requests` entry + its answer).

**Opt-out:** If `task.auto_mode: false` in plan frontmatter, the wave runs in interactive mode (rare; for tasks requiring per-decision human guidance). The tier mapping still applies, but tier-2 surfaces to the user before proceeding instead of auto-deciding.

**Wave-outcome coercion (deterministic guard against blocker-leak):**

```python
from agentflow.status_writer import coerce_wave_outcome, append_decision

# After parsing wave JSON:
new_outcome, new_verdict, audit_reason = coerce_wave_outcome(
    outcome=raw["outcome"],
    verdict=raw.get("verifier_verdict"),
    findings_count=raw.get("verifier_findings_count", {}),
)
if audit_reason:  # non-None means a rewrite happened
    append_decision(state, task=tid, tier=2, summary=audit_reason)
# Use new_outcome / new_verdict from here forward, NOT raw["outcome"] / raw["verifier_verdict"].
```

A wave that returns `outcome=done` with `verifier_findings_count.blocker > 0` is **always** rewritten to `outcome=verifier_blocker_persistent` and `verifier_verdict=blocker_persistent`. The audit-decision-log entry surfaces the rewrite in the dashboard. This is the choke point — a wave cannot ship a task with unresolved blockers regardless of how it self-labels.
5. Tier-1/2 decisions made by you or implementer (file placement, naming, etc.) → call `append_decision(state, ...)`. Match the tier per the tier mapping table above.
6. Tier-3 ambiguity hit → call `open_escalation(state, task=tid, question=<the tier-3 question>)` (which sets `status = "escalation"` itself), **break out of dispatch** (do not continue to next task), proceed to Step 9 to ntfy and persist.

**Hallucinated-denial detection (run BEFORE the BLOCKED handler below):**

When a wave returns `outcome=blocked`, check the hallucination signature:

```python
import re
is_hallucinated_denial = (
    raw["outcome"] == "blocked"
    and not raw.get("files_modified") and not raw.get("files_changed")
    and not raw.get("commits") and not raw.get("commit_sha")
    and re.search(r"\b(denied|permission|approve|approval)\b",
                  raw.get("blocked_reason") or "", re.IGNORECASE)
)
```

If `is_hallucinated_denial`:
- Append a tier-2 decision: `f"Wave T{tid} returned hallucinated-denial signature (outcome=blocked, no files, no commits, blocked_reason mentions permission/approve). Re-dispatching with EMPIRICISM PREFACE."`
- Before the re-dispatch, set `state.tasks[tid].state = "in_flight"` (and `since = now`): the reporter has already written `blocked`, which never re-enters the ready set. The re-dispatch goes back through Step 7.0, which makes a new tree under the next wave number, so before it, when the task records a `wave_worktree_path`, release that tree and clear its fields (**Releasing a wave tree**, Step 1.2.5); a tree left recorded stays live and is never pruned.
- Re-dispatch the wave (this counts as the first BLOCKED for two-attempt-rule purposes), but **prepend** the following EMPIRICISM PREFACE to the wave prompt verbatim, BEFORE any other content:

  ```
  ## EMPIRICISM PREFACE — prior dispatch hallucinated denial

  A previous dispatch of this exact task returned outcome=blocked with files_modified=[],
  commits=[], and a blocked_reason claiming all file-creation tools were denied — WITHOUT
  ever calling a single Write/Edit/Bash file-creation tool. This was a hallucination, not
  a real harness response.

  The harness allows Write, Edit, mcp__filesystem__write_file, and Bash file-creation
  calls (mkdir, echo to file, npm init, npm install) in general-purpose subagent
  context. The PreToolUse hooks bootstrap.py installs also run inside subagents
  (Claude Code hooks reference, https://code.claude.com/docs/en/hooks) — unless this machine
  was installed with `--no-auto-allow`, in which case those calls may prompt instead and a
  literal harness error is real evidence.

  You MUST attempt at least one Write tool call before reporting any permission-class
  blocked outcome. If you report outcome=blocked with a permission/approval blocked_reason
  WITHOUT including the literal tool_result error string in that reason, your return will
  be auto-rejected as another hallucination and the task will be hard-blocked.

  Proceed to implementation. The wave instructions follow:
  ```

- Do NOT count this as a real BLOCKED for the two-attempt rule on the second dispatch — if the second dispatch ALSO returns hallucinated-denial signature, THEN escalate as normal blocked (mark task `blocked`, `status=blocked`, ntfy HARD_FAILURE).

**On any subagent BLOCKED return that is NOT a hallucination signature:**

- **External resolution path:** if the BLOCKED return's `blocked_reason` includes the marker `blocker_kind: external` OR the `escalation_requests` entries include `escalation_kind: external_resolution`, the failure is classified as externally-resolvable. Instead of the normal blocked handling:
  1. Parse the wave's blocker descriptor from the JSON (`blocker_kind`, `condition` dict — kind/condition/reason/interval/max_wait per the blocker-watcher descriptor schema).
  2. Populate `state.blocker_watcher` with the parsed descriptor (set `poll_count: 0`, `last_polled_at: null`, `next_wake: now + interval`).
  3. Set `state.status = "awaiting_external_resolution"`.
  4. Append a decision: `f"task {tid} blocked on external condition ({kind}); handing off to blocker-watcher (interval={interval}s, max_wait={max_wait}s)"`.
  5. Persist status.md, ScheduleWakeup at `next_wake`, release lock, exit.

  This branch fires INSTEAD OF the existing first-blocked / second-blocked handling — externally-resolvable blockers do not count toward the two-attempt rule because the failure is environmental, not subagent quality. The two-attempt rule still applies for non-external blockers.

- **Unlanded best-of-N winner** (a `best_of_n` return carrying `winner_commit` and `branch`): the workflow already retried the land and removed every candidate tree, and the winner's branch holds the commit. Append a tier-2 decision `"best-of-N winner {branch} not landed ({winner_commit}); branch kept, inspect with git -C <worktree> log {branch}"`, where `<worktree>` is the Step 1.1 epic tree (the winner's own tree is gone). Then try ONE merge-back of the kept branch (`<winner-branch>`, the return's `branch`) from the epic tree. Branches are shared by every tree of the repository, so the epic tree serves as the tree merge-back fetches from:
  ```python
  from agentflow.wave_worktree import merge_back
  result = merge_back(wave_worktree=worktree, main_worktree=worktree, branch=<winner-branch>)  # Step 1.1 tree for both
  ```
  - On `result.success`: the winner has landed, as it would have inside the workflow. Delete the kept branch (`git -C <worktree> branch -d <winner-branch>`), release the task's Step 7.0 tree and clear its fields (**Releasing a wave tree**, Step 1.2.5), append tier-1 decision `"best-of-N winner {branch} merged back by the orchestrator"`, set the task's commit to `winner_commit` and `state = "code_review"`, and run the orchestrator review gates (Step 7 reconcile) with `{worktree}` substituted by `<worktree>`: a landed winner gets the inside reviews, and the workflow skipped them for this one. Skip the merge-back on approval there, since the commit is already on the epic branch. This is not a BLOCKED return, so nothing counts toward the two-attempt rule.
  - On `result.success is False`: append tier-2 decision `"merge-back of kept branch {branch} failed: {result.stderr}; delete {branch} by hand once inspected"`. Before the re-dispatch, set `state.tasks[tid].state = "in_flight"` (and `since = now`) and release the task's Step 7.0 tree and clear its fields (**Releasing a wave tree**, Step 1.2.5), then re-dispatch through Step 7 (Step 7.0, then item 4), so the new task tree carries the new wave number its candidates get. A task tree left recorded stays live on every later wake and is never pruned. Name every kept branch still undeleted in any escalation this task later raises. Nothing deletes the kept branch automatically: it may hold a judged winner's only copy, so the epic's final report lists every one that survives (**Kept winner branches**, Step 9.2 item 7b).
  - An unlanded winner counts toward the two-attempt rule like any BLOCKED return: the re-dispatch above is the first-time handling, and a second unlanded return on the same task (or any second BLOCKED) follows Second BLOCKED below. The one merge-back try still runs first on that second return; only when it fails does Second BLOCKED replace the re-dispatch.
- First time on this task: set `state.tasks[tid].state = "in_flight"` (and `since = now`), then re-dispatch with more context (per `subagent-driven-development` rules). The re-dispatch goes back through Step 7.0, which makes a new tree under the next wave number, so before it, when the task still records a `wave_worktree_path`, release that tree and clear its fields (**Releasing a wave tree**, Step 1.2.5). A tree left recorded stays live and is never pruned. The release keeps a branch that holds commits the epic does not (an unlanded adversarial commit, for one) and lists it in `released.branches_preserved`; log it as that section says.
- Second BLOCKED return on same task (any role, within wake or across wakes): mark task `blocked`, set `status = "blocked"`, ntfy HARD_FAILURE via `agentflow.ntfy.notify_once(state, key=f"hard_failure_task_{tid}", trigger_type=TriggerType.HARD_FAILURE, ...)`, exit. The HARD_FAILURE detail names every surviving kept branch of the epic, computed as the **Kept winner branches** list (Step 9.2 item 7), since a halted epic never reaches the final report that would list them.

### Step 8: Phase transition

If all tasks in `state.current_phase` are now `done`:

1. **Escalation-rate guard.** Compute `compute_phase_escalation_rate(state, completed_phase)` from `agentflow.status_writer`. Compare against the threshold:
   - Threshold = `plan.verification.escalation_threshold_pct` if `plan.verification` is set, else default **15**.
   - If rate ≥ threshold:
     - Set `state.status = "spec_quality_review"`. (Use `transition_status` with reason: *"phase {X} verifier escalation rate {N}% ≥ threshold {T}%"*.)
     - Open an escalation: *"Phase {X} verifier escalation rate {N}% (threshold: {T}%). Spec may be producing tasks the verifier can't satisfy. Review spec at {plan.spec} before continuing."*
     - Send ntfy `SPEC_QUALITY_REVIEW` with the same detail: `agentflow.ntfy.notify_once(state, key="spec_quality_review_<phase>", trigger_type=TriggerType.SPEC_QUALITY_REVIEW, ...)`.
     - **BRANCH FIRST — which surface is this run's?** Read this before executing anything
       below it. A run carrying the `fleet-context: <agent>` marker (recorded as `fleet_context`
       in `status.md` at bootstrap; see **Fleet invocation context**) takes the FLEET path and
       **never calls `AskUserQuestion` anywhere in this step**. Everything from here to the end
       of the button-click branches is the STANDALONE path unless a line says otherwise.

     - **Buttoned prompt on the transition wake only — STANDALONE ONLY.** This is the wake that
       tripped the
       threshold (we just wrote `state.status = "spec_quality_review"` above), so call
       `AskUserQuestion` to surface the override/pause decision in-line rather than relying on
       a status-md frontmatter edit. On subsequent wakes already in `spec_quality_review`,
       Step 3's early-exit branch fires WITHOUT re-prompting (write a
       `spec_quality_prompted: <utc-now>` marker to the decision log so Step 3 can detect the
       prior prompt and skip re-firing).

       Question text (rendered as the question string, so the user has the rate + phase to
       decide on):

       > "Phase **{X}** tripped the escalation-rate guard at **{N}%** (threshold {T}%).
       > The spec may be producing tasks the verifier can't satisfy. Override and resume, or
       > pause for spec review?"

       **THE FLEET PATH, IN FULL** (the branch declared above; do none of the button machinery):
       on this same transition wake, raise a `## Questions` row per the procedure in **Fleet
       invocation context** — same wake, same `spec_quality_prompted: Q-NNNN at <utc-now>`
       marker so Step 3's early-exit fires on later wakes without re-raising — with:

       - **Urgency by the `<suite_root>/docs/REGISTRY.md` rule, not hardcoded.** Normally `waiting`: the epic
         cannot safely advance without the answer, but the launching loop has other claimable
         work. Use `halted` **only** if the raising loop's lane has nothing else claimable —
         both conditions are required, and it is the launching loop that knows. Never
         `advisory`: proceeding on an assumption is the one thing this guard exists to prevent.
       - **`waiting` means PARK, not merely ask.** The classification is half a rule; the other
         half is the action. The launching loop releases its Queue row to `parked` with
         `--waiting-on Q-NNNN` so the lane is free and the epic is visibly blocked rather than
         silently stalled. A question raised without parking leaves a row that still reads
         `building` while nothing builds.
       - `default: "Pause for spec review"` — **deliberately NOT the first option.** Defaulting
         to "Override & resume" would make every unattended run acknowledge its own escalation
         rate and advance, so the guard would fire, answer itself, and pass — a gate that
         defeats itself is worse than no gate, because it also reports success. Pausing simply
         leaves `status = spec_quality_review`, which is exactly the state Step 3 already
         handles, and it is fully reversible by the human's answer.
       - **Then END THE WAKE.** Persist `status.md` (status stays `spec_quality_review`; do
         **NOT** advance `state.current_phase` — that advancement belongs only to the
         "Override & resume" answer), keep the existing `ScheduleWakeup` poll cadence, release
         the lock, exit. The fleet path takes NEITHER button-click branch below; it takes the
         one the answer names, on the wake that reads the answer.

       Then, **standalone interactive only**, call `AskUserQuestion`:

       ```yaml
       header: "Spec quality"
       question: "Phase {X} escalation rate {N}% ≥ threshold {T}% — override or pause?"
       options:
         - label: "Override & resume"
           description: "Acknowledge the high escalation rate; reset status to running and advance to next phase"
         - label: "Pause for spec review"
           description: "Leave status as spec_quality_review; you will review the spec before resetting"
       ```

       Button-click branches (executed in this SAME wake — the question fires before exit, and
       the orchestrator records both pre-emptive states so the next wake honors the answer):
       - **Override & resume** → in a SINGLE write to `status.md`: set `state.status = "running"`
         AND advance `state.current_phase` to the next phase in `plan.phases` (the same
         advancement step 2 below would have done). ALSO reset the per-phase
         escalation-rate tracker for the new phase so it does NOT carry the prior phase's
         escalation count forward and immediately re-trip on the next phase check. Append
         decision: `spec_quality_override: user accepted N% escalation rate; advancing to phase
         <next>`. Then proceed to ntfy PHASE_COMPLETE below (step 3) and continue normally.
       - **Pause for spec review** → no state change (status stays
         `spec_quality_review`, phase NOT advanced). Append a decision-log marker
         `spec_quality_pause_acknowledged: user chose spec-review pause` so the prompt does not
         re-fire on subsequent wakes. Persist `status.md`, release lock, ScheduleWakeup 4 min,
         exit.

       **Frontmatter-edit fallback preserved:** the user can still bypass the buttons by
       editing `status.md` directly to flip `status: running` and advance `current_phase`.
       Step 3 + Step 8 honor either path.

       After AskUserQuestion: if the user clicks Pause (or no answer yet), persist
       `status.md`, release lock, ScheduleWakeup 4 min, **do not advance to next phase**, exit.
   - If rate < threshold: append a decision-log entry with the rate (INFO-level), continue to step 2.
2. Update `state.current_phase` to the next phase in `plan.phases` (or sentinel if last)
3. Send ntfy PHASE_COMPLETE. Pass `epic=<human-readable project name>` (never blank),
   `task=<epic-wide tasks done>`, `task_total=<epic-wide total tasks>`, and `detail="<phase> complete"`.
   The lib renders the title as **`Task X of Y - <project>`** (progress + project lead the banner;
   the body keeps `detail`). X/Y are epic-wide completed/total task counts, not per-phase.
   - Call: `agentflow.ntfy.notify_once(state, key="phase_complete_<phase>", trigger_type=TriggerType.PHASE_COMPLETE, epic=..., detail="<phase> complete", task=<tasks_done>, task_total=<total_tasks>)`

### Step 9: Epic completion

If ALL tasks have `state == "done"`:

#### Step 9.0.5: Pre-review simplification pass

**Rationale:** Across an epic's implementation waves, small overengineering accumulates — premature abstractions, dead error handling, restated-code comments, leftover scaffolding from earlier iterations. Per-task verifiers catch task-local cases; the dual review at 9.1 catches correctness; neither systematically passes a `/simplify` lens over the full epic diff. This step does that, applying only tier-1 deterministic fixes so the dual review evaluates the simplified surface.

**Idempotency gate:** if `state.decisions` contains any entry with summary matching `/^simplify_pass_(complete|clean|blocked|skipped):/`, skip this step entirely and proceed to Step 9.1. The marker survives wake cycles — the wave runs at most once per epic.

**Dispatch:** single subagent via the Task tool, `subagent_type="general-purpose"`, model=sonnet, using the prompt template at `<suite_root>/skills/orchestrator/prompts/simplify-pass.md`. Populate the template with:

- `{worktree}` — the tree resolved in Step 1.1. **Required.** This wave writes commits and reverts
  files; dispatched from the launch directory with no path it would do both somewhere else.
- `{epic_name}`, `{epic_slug}` — from `state`.
- `{epic_start_sha}` — the commit on the base branch immediately before this epic's first task commit. Read from `state.epic_start_sha` if recorded by Step 7, else compute as `git -C <worktree> rev-parse <epic-branch>~N` where N = total task commit count.
- `{union_file_allowlist}` — Python: `sorted(set(itertools.chain.from_iterable(t.verification.file_allowlist for t in plan.tasks if t.verification)))`. One file per line.
- `{plan_regression_guard}` — from `plan.verification.regression_guard` (the plan-level prohibitive list). One file per line. Empty string if absent.
- `{plan_global_failure_modes}` — from `plan.verification.global_failure_modes`. One bullet per line.
- `{plan_test_commands}` — from `plan.verification.global_test_commands` if present, else the deduplicated union of every `task.verification.test_commands`. One command per line.

The wave runs to completion (no verifier loop, no spec-reviewer, no quality-reviewer — the dual review at 9.1 is the gate). Apply `agentflow.subagent_contract.coerce_wave_outcome()` on return. Also apply the hallucinated-denial detection from Step 7 (same signature, same EMPIRICISM PREFACE re-dispatch path).

**Handle the wave return:**

1. **Parse JSON.** Validate the shape per `prompts/simplify-pass.md`'s "Return JSON contract" section. If parse fails, treat as `outcome=blocked` with `blocked_reason: "simplify wave returned malformed JSON: <excerpt>"`.

2. **Branch on outcome:**

   - `outcome=done`, `applied_simplifications=[]`, `deferred_findings=[]` → append decision: `simplify_pass_clean: no tier-1 opportunities and no deferrals` (tier-1). Proceed to Step 9.1.

   - `outcome=done`, `applied_simplifications` non-empty → for each entry:
     - Append decision: `simplify_applied: <sha[:7]> <description>` (tier-2).
     - Record the SHA into `state.simplify_commits` (a list, init `[]` if absent — this list is what 9.1's revert path uses to identify simplify-attributable findings).

     Then append summary decision: `simplify_pass_complete: <N> applied, <M> deferred` (tier-2). Proceed to Step 9.1.

   - `outcome=done`, `applied_simplifications=[]`, `deferred_findings` non-empty → append decision: `simplify_pass_complete: 0 applied, <M> deferred (all candidates were tier-2/3 or failed tests)` (tier-1). Proceed to Step 9.1.

   - `outcome=blocked` (genuine, not hallucinated) → append decision: `simplify_pass_blocked: <blocked_reason>` (tier-2). The epic does NOT block on simplify-pass failure — proceed to Step 9.1.

   - `outcome=blocked` matching the hallucinated-denial signature → re-dispatch ONCE with the EMPIRICISM PREFACE (per Step 7). Second hallucinated return → append decision: `simplify_pass_skipped: hallucinated denial after retry` (tier-2) and proceed to Step 9.1.

3. **Surface deferred findings.** For each entry in `deferred_findings`, append a decision: `simplify_deferred: <file>:<line>: <description> (<reason_deferred>)` (tier-1 if reason is `cap reached` or `test failure`, tier-2 if reason cites a tier-2/3 rule — these are judgment calls worth surfacing in the dashboard). Also persist the full list to `state.simplify_deferred_findings` for downstream consumers (a future concern-triage step, or manual review at epic-done).

4. **Persist `status.md`** before proceeding to Step 9.1.

**What this step does NOT do:**

- Does NOT block the epic. Any failure path is logged and the orchestrator continues to dual review.
- Does NOT re-run if the marker is already in the decision log.
- Does NOT touch files outside the union allowlist (the wave prompt enforces this; the dual review at 9.1 verifies it).
- Does NOT apply tier-2/3 changes. Those are surfaced via decision log + `state.simplify_deferred_findings` for the user (or a future triage step) to ratify.

#### Step 9.1: Dual final review (security + code)

**Rationale:** A single code review catches spec-drift but misses security-class concerns (auth gaps, secret handling, unsafe deserialization). Running both reviews in parallel costs no additional wall-clock time and matches the project's deterministic-verification pattern.

Dispatch **both of the following in a single message** (two parallel Agent tool calls — per the `../_vendored/dispatching-parallel-agents/SKILL.md` pattern):

1. **Security review:** A `/security-review`-equivalent subagent over the full epic diff in `<worktree>` (all commits from epic start to HEAD; the subagent reads it with `git -C <worktree> diff <epic-start-sha>..HEAD` — **state the worktree path in its prompt**, it has no other way to know which tree). Scope: auth, secret handling, injection vectors, unsafe deserialization, network exposure, privilege escalation. Apply `agentflow.subagent_contract.coerce_review_verdict()` immediately on return.
2. **Comprehensive code reviewer:** Dispatch the **code-quality-reviewer-evidence-wrapper** (`prompts/code-reviewer-evidence-wrapper.md`) across the same diff, **substituting `{worktree}` with `<worktree>` in the prompt**. Scope: spec compliance, correctness, architecture drift, dead code, missing error handling. Apply `agentflow.subagent_contract.coerce_review_verdict()` immediately on return.

**On `inconclusive` coercion (either reviewer):** re-dispatch the implementer once with `agentflow.subagent_contract.build_enriched_redispatch_prompt(original, findings)`. This is a fresh re-dispatch (prior-session resume is not an available path; see *Re-dispatch as the supported fix-iteration path*). Second `inconclusive` on same role → escalate per existing two-attempt rule.

**BOTH must return ✅ before the epic is declared done.** If either flags issues:

**Simplify-commit revert path (check FIRST, before re-dispatching any implementer):** for each ✗ finding from either reviewer, check whether the reviewer attributed it to a commit in `state.simplify_commits` (by SHA reference, by the `simplify:` commit-message prefix, or by the finding's file path matching a file whose only post-task changes are in a simplify commit). If yes:
- `git -C <worktree> revert --no-edit <simplify_sha>` for the implicated commit.
- Append decision: `simplify_pass_reverted: <sha[:7]>: <reviewer summary>` (tier-2).
- Remove the SHA from `state.simplify_commits`.
- Re-run **BOTH** reviews in parallel over the new HEAD. Do NOT count this revert against the two-attempt-rule budget for the affected task — the simplification was orchestrator-introduced, not task-introduced.

If a reviewer ✗ is NOT attributable to a simplify commit:
- Re-dispatch the relevant implementer subagent for any task with issues identified by that reviewer.
- Once fixes are committed, re-run **BOTH** reviews again in parallel (do not short-circuit to just one).
- Loop until BOTH reviewers return ✅. If either reviewer blocks more than twice on the same issue, treat as a hard blocker: mark `state.status = "blocked"`, open an escalation, ntfy HARD_FAILURE, exit.

#### Step 9.1.5: Frontend regression note

**Rationale:** Even backend-only epics frequently break a UI surface that was already built. After
the dual review passes ✅ but before declaring the epic done, check whether the repo has a testable
frontend, and if it does, tell the human that no per-epic UI regression check ran. This step
invokes no skill and asks no question, in either context: no shipped skill runs a per-epic UI
regression check, and polish-mode generates backlog work and rules out mid-epic runs in its own
"When NOT to use".

**Detect frontend testability** (no user prompt):
- Is there a `package.json` + a frontend dir (`*/frontend/`, `client/`, `web/`, `ui/`) in the repo?
- Is the dev server runnable (`npm run dev`, `yarn dev`, or a documented equivalent in CLAUDE.md / README)?
- Is there a known live-app URL (`project.staging.url`, CLAUDE.md, or equivalent)?

**If no testability signals:** record a one-liner in the decision log (`polishing_skipped: no_frontend_detected`) and proceed straight to Step 9.2. Print nothing — there's nothing to check.

**FLEET-LAUNCHED RUNS PRINT NO NOTE.** A run carrying the `fleet-context: <agent>` marker
(recorded as `fleet_context` in `status.md` at bootstrap; see **Fleet invocation context**) records
`polishing_skipped: fleet_context` in the decision log (the same key every other skip in this step
uses — `no_frontend_detected`, `no_pass_available` — so any later reader sees one consistent field)
and continues to Step 9.2. It prints no note, because the note is a chat line for a human at the
keyboard, and a fleet run has none.

**Standalone interactive, with at least one signal true:** record
`polishing_skipped: no_pass_available` in the decision log, print one prose line in the chat
output, then continue to Step 9.2. The line names the detected frontend dir and the live URL if known, and says no per-epic
UI regression check ran, so the human may want to drive the golden path before merging. When the
epic itself touched frontend files, the line says so and recommends the check. For example:

> "Detected a testable frontend at `<frontend-dir>` (live URL: `<url>` if known). No per-epic UI regression check ran; you may want to drive the golden path before merging. This epic touched frontend files, so that check is recommended."

**This is a known gap, stated rather than papered over.** Nothing runs a per-epic UI regression
check after a backend change, which is exactly the case the step's rationale was written for.
lead-dev's polish mode is a **backlog-refill pass gated on an empty queue**, and polish-mode
generates work rather than checking an epic, so neither covers this gap and neither may be cited as
if it did. Until a per-epic check that runs without a human exists, a standalone run hands the check
to the human in its note, a fleet run trades it away, and the decision-log entry makes both visible.

#### Step 9.2: Post-review steps

2. **Optional knowledge capture (config-gated).** This applies only when the resolved block prints a `vault_save_skill` line (the project's `vault.save_skill`, else the environment's; presence-checked at resolve time); when it does, follow that `vault_save_skill` path directly as a SKILL.md, not by command name, to file a knowledge-base note for the epic (goal → build → review verdicts → PRs → load-bearing lessons). Best-effort — record `knowledge_capture_pending` decision if it fails; with no `vault_save_skill` line, skip silently (never fatal).
4. **Mark ended and set status:**
   - Call `mark_ended(state)` — sets `state.ended` to the current UTC time (required for total-time computation; omitting this breaks session-time strip on the dashboard).
   - Call `append_session_event(state, "ended", f"<N-tasks> tasks, <N-waves> waves, total <human-duration>")` — do this BEFORE the final `write_status` so the event lands in the persisted state.
   - Set `state.status = "done"`.
5. **Stabilization-test summary:** call `agentflow.stabilization_tests.render_summary(epic_slug)`. If non-None: append the line to the EPIC_DONE ntfy detail and surface in chat output as *"Wave subagents flagged {N} integration-test gaps during this epic. See {path} for the post-epic stabilization queue."* If None (no suggestions collected): omit the line entirely — do not mention an empty queue.
6. Send ntfy EPIC_DONE (detail includes the stabilization summary line when present) via `agentflow.ntfy.notify_once(state, key="epic_done", trigger_type=TriggerType.EPIC_DONE, ...)`
7. **Merge gate (substeps 7a-7d):** open the PR, enter `awaiting_merge_decision`, and on next wake act on the user's `state.merge_decision` value.

   **Kept winner branches list (computed once, before 7a).** Every branch named in a `merge-back of kept branch {branch} failed` decision (Step 7, unlanded best-of-N winner) that still exists (`git -C <worktree> rev-parse --verify --quiet refs/heads/{branch}` finds it), each with its tip (`git -C <worktree> rev-parse {branch}`). A branch deleted by hand since is left out. The PR body (7b) and the merge-decision summary (7c) both render this list, so it does not depend on a PR opening.

   **7a — Final integration commit (if any uncommitted scope changes)**
   - Run `git -C <worktree> status --short`.
   - If any tracked-but-unstaged changes scoped to this epic's files exist, **scope-stage** them (`git -C <worktree> add <explicit-paths>` — never `git add -A` / `git add .`) and commit with `git -C <worktree> commit -m "chore({epic-slug}): final integration cleanup"`.
   - Skip the commit if the tree is clean.

   **7b — Push + open or update PR**
   - **Precondition checks** (run BEFORE any push or `gh` call; document each failure in decision log; degrade gracefully):
     - `git -C <worktree> remote get-url origin 2>/dev/null` — no origin → local-only repo. Skip push + PR machinery; merge gate degrades to state-only (user merges manually via local `git merge` later).
     - `command -v gh` — missing → skip PR machinery (push may still occur if origin exists).
     - `gh auth status` — missing/expired → skip PR machinery.
     - Origin URL inspection: read it with `git -C <worktree> remote get-url origin`. If the URL is not GitHub-shaped (e.g., Gitea, GitLab, file://), skip all `gh` calls. Document `non_github_remote: <url>` in decision log. Push-only degradation.
     - **Bind `<gh-repo>` = `<owner>/<repo>` parsed from that same URL, and pass `-R <gh-repo>` on every `gh` call below.** `gh` resolves its repo from the working directory exactly the way a bare `git` does, so in a lane launched from the launch directory it has no repo to find — and if it finds one, it is whatever tree the process happened to inherit. This is the same defect as a bare `git` call, one tool over. Worse, these are the merge-gate calls, so a wrong resolution opens, merges or closes a PR **on another repository**.
   - `git -C <worktree> push -u origin <epic-branch>` (or `git -C <worktree> push` if origin tracked). On push failure (auth, network), document + skip to 7c with `pr_url=None`.
   - Look up any existing PR via `gh pr list -R <gh-repo> --head <epic-branch> --json number,url --jq '.[0]'`.
   - Assemble the PR body as a single string with these sections:
     - **Title:** `Epic: <epic-name>`
     - **Body:**
       - `Agentflow [orchestrator]` as the FIRST line. The prose convention for this body, and for
         every comment this skill posts, is `<suite_root>/skills/_shared/PR-PROSE.md` — attribution, the prose
         rules, the prose cap and its exemptions, and the session-URL prohibition.
         **Cite it, do not restate it here**: three copies of a rule drift.
       - Epic summary (one-liner from the plan / spec).
       - A provenance line for the `pr-provenance-gate`: `Source: gh#N` when the spec or plan
         frontmatter's `provenance.source` (or the dispatch) names issue `gh#N`; otherwise
         `Epic: <epic-slug>`.
       - Task table with header `| Task | Phase | Commit | Verifier verdict | Findings | Spec ✓ | Quality ✓ |` — one row per task, populate `verifier_verdict` and `verifier_findings_count` from `state.tasks[tid]`, and the Spec/Quality columns from the relevant entries in `state.decisions` (spec-review and quality-review outcomes per task).
       - Stabilization-test queue summary from `agentflow.stabilization_tests.render_summary(epic_slug)`. If the helper returns `None`, **omit this section entirely** — do not write an empty placeholder.
       - Dual-review status: security ✅ + code ✅ from Step 9.1 (verbatim verdicts + reviewer commit SHAs).
       - Decision-log highlights: filter `state.decisions` to entries with `tier == 2` and render as a bulleted list.
       - **Kept winner branches:** the **Kept winner branches list** computed before 7a, one bullet per branch with its tip, saying it is kept on purpose and is deleted by hand once inspected. Omit the section when the list is empty.
       - A `Review:` line, per `<suite_root>/skills/_shared/PR-PROSE.md` *Review pointer*, so the body is final at create:
         - **Fleet-context run with a source issue** (the `gh#N` on the `Source:` line above): before `gh pr create`, post the review comment on that issue with `gh issue comment -R <gh-repo> <N> --body-file <file>`. Its content is the one `skills/lead-dev/SKILL.md` §4 describes, built from the Step 9.1 verdicts. Write the URL the command prints as `Review: <url>`.
         - **Otherwise:** write `Review: none`. The dual-review section above already carries the verdicts in the body.
   - If no PR exists: `gh pr create -R <gh-repo> --title "<title>" --body "<assembled-body>" --base <base-branch> --head <epic-branch>`.
   - If a PR exists: when its body has a `Review:` line, carry that line into the assembled body as it stands and post no new pointer comment; when it has none, use the freshly assembled one. Read its current title and body with `gh pr view -R <gh-repo> <pr-number> --json title,body`. When both equal the assembled title and body after normalising CRLF to LF and stripping trailing whitespace, skip the edit (every `gh pr edit` fires `edited` and re-runs every CI job) and log `append_decision(state, task=0, tier=1, summary="pr edit skipped: title and body unchanged")`. Otherwise run once:
     `gh pr edit -R <gh-repo> <pr-number> --title "<title>" --body "<assembled-body>"`.
   - Capture the resulting PR URL and PR number for status.md and downstream substeps.

   **7c — Enter awaiting-merge state**

   **Auto-merge for `approved-for-autonomous` epics (gateless — no merge gate).** Before entering the
   awaiting-merge state, read `current_state` from the plan's frontmatter. If
   `current_state == approved-for-autonomous`, the epic was human-greenlit for **fully gateless**
   execution — the promotion covers the ENTIRE lifecycle, merge included (same rationale as gate #1 /
   gate #2 auto-skip). So **skip the merge AskUserQuestion entirely**: set `state.merge_decision = "merge"`,
   append a tier-1 decision `"merge gate auto-skipped: current_state=approved-for-autonomous; auto-merging"`,
   and proceed DIRECTLY to substep 7d's `merge` branch (squash-merge the PR, mark done, autodoc refresh).
   Do NOT render the summary-prompt or call AskUserQuestion. This fires ONLY for that exact state — every
   other state (drafted, etc.) still hits the normal merge gate below. (Merging the PR to
   the repo's main is in-scope for gateless, and that merge MAY fire the project's staging deploy
   per `staging.deploy_recipe` with no human; a production deploy is never auto-done here and is
   always a human step per lead-dev §8's prod-gate rule.)

   Otherwise (any other state), run the normal merge gate:
   - Set `state.status = "awaiting_merge_decision"`.
   - Set `state.merge_decision = None` (the user will edit frontmatter to `merge` / `hold` / `abandon`).
   - Call `append_decision(state, task=0, tier=1, summary=f"PR opened/updated at {pr_url}; awaiting merge decision")`.
   - Send the awaiting-merge ntfy via `agentflow.ntfy.notify_once(state, key=f"epic_awaiting_merge_{epic_slug}", trigger_type=TriggerType.ESCALATION, ...)` with a title saying the PR awaits a merge decision. `ESCALATION`, not a new member: a human decision is needed, and `wake_watch.py` sets the same precedent.
   - Persist `status.md` so the user can see the PR URL + the merge-decision prompt.
   - **Render a bootstrap-style summary** (prose, immediately before the AskUserQuestion call — user sees this in the same chat turn as the buttons):
     > "Epic **{epic_name}** is ready to merge. PR: {pr_url}
     > Dual-review: security ✅ · code ✅ (from Step 9.1 verdicts)
     > Kept winner branches: {the Kept winner branches list computed before 7a; omit this line when it is empty}
     > Choose how to proceed:"
   - **Call `AskUserQuestion`** (fires on the transition wake only — guard with `merge_decision_prompted` below):

     ```yaml
     header: "Merge decision"
     question: "How should the PR for {epic_name} be handled?"
     options:
       - label: "Merge PR"
         description: "gh pr merge -R <gh-repo> <pr-number> --squash --delete-branch; mark epic done"
       - label: "Hold"
         description: "Leave PR open; mark status done_on_hold"
       - label: "Abandon"
         description: "gh pr close -R <gh-repo>; mark status abandoned"
     ```

     Button-click branches (execute in the SAME wake as the click, before ScheduleWakeup):
     - **Merge PR** → `state.merge_decision = "merge"`. Proceed immediately to substep 7d merge branch (do not wait for the next wake).
     - **Hold** → `state.merge_decision = "hold"`. Proceed immediately to substep 7d hold branch.
     - **Abandon** → `state.merge_decision = "abandon"`. Proceed immediately to substep 7d abandon branch.

   - Write `merge_decision_prompted: <utc-now>` to the decision log **immediately after** the AskUserQuestion call (before the ScheduleWakeup exit) so subsequent poll wakes know not to re-fire the prompt.

   - **Frontmatter-edit fallback preserved:** if the user edits `status.md` directly and sets `state.merge_decision = "merge"` / `"hold"` / `"abandon"` before clicking a button, substep 7d on the next poll wake will read it and branch identically. The Step 3 `awaiting_merge_decision` branch detects the `merge_decision_prompted` marker to skip re-prompting on poll wakes.

   - ScheduleWakeup in 30 minutes (fallback poll — fires if user does not click a button on this wake; on the next wake Step 3 routes to 7d which reads frontmatter).
   - Release the lock and exit. **Do NOT proceed to step 8 on this wake unless a button was clicked — step 8 runs only after the merge gate resolves.**

   **7d — On next wake or immediately after button click (state.status == "awaiting_merge_decision")**
   Read `state.merge_decision` (set either by button click in 7c or by user frontmatter edit) and dispatch on its value:
   - **`merge`:**
     - **Re-bind `<gh-repo>` here; do not assume step 7b already ran.** `<gh-repo>` is bound from
       the origin URL in 7b's preconditions, but this step is reachable on a **resume wake** that
       never executed 7b, and these are the highest-stakes `gh` calls in the skill — a merge and a
       close. Read it again from the tree that is about to be merged:
       `git -C <worktree> remote get-url origin`, parse `<owner>/<repo>`, and if it is not
       GitHub-shaped, skip the `gh` calls exactly as 7b would. An unbound `<gh-repo>` silently
       degrades `-R` back to cwd resolution, which is the whole defect `-R` exists to prevent.
     - **Pre-merge deploy guard (deploy-on-merge projects — runs on BOTH the gateless 7c auto-merge path and this
       gated path).** When the project declares a `staging.deploy_recipe`, merging to `<canonical_repo.host_ref>`
       main fires the deploy, which rebuilds the staging stack from plain main and WIPES any
       uncommitted hand-patches on the staging host. Before merging, check staging
       drift: `ssh <staging.deploy_host_ssh> "git -C <staging.checkout_path> status --porcelain"`. If dirty, port the
       hand-patches into the repo (commit to the branch, re-run reviews if code changed) or escalate —
       never merge over live drift. If the staging host is unreachable, note it as a tier-2 decision
       and proceed (unreachable ≠ dirty).
     - **Deploy-lock preflight (fleet-context runs — `fleet-context: <loop>` in the dispatch prompt).**
       The dispatching loop holds the fleet's single deploy lock across this merge, and the merge is
       the irreversible act it guards. Two guards, and they chain differently:
       - `deploy-lock take` refuses with **exit 4** while any active `## Directives` are
         unacknowledged. Pass `--ack-directive "<substr>"` once per active Directive, quoting the
         **human's words** and never the bracketed ISO-8601 prefix the CLI minted — a timestamp
         needle matches and passes while proving nothing was read. Read the refusal KEY rather
         than the bare code: exit 2 is `deploy_held` (another lane holds it — wait), or
         `deploy_stale_unbroken` (below); every other exit 2 — `deploy_builder`, `agent_known`,
         `file_missing`, `file_unreadable`, `file_damaged` among them — is a malformed call that
         waiting never fixes; exit 3 is the registry edit-lock and should simply be retried.
         `deploy_stale_unbroken` is unlike both: the hold is stale and stealable,
         and nothing breaks it automatically, so waiting never ends.
         The full liveness probe on the named holder is a precondition for passing
         `--break-stale`: re-run with it only if the probe finds the holder dead.
       - Put the merge on the right-hand side of **`deploy-lock assert`**, never of the take:

         ```
         python -m agentflow.registry deploy-lock assert --actor <loop> --project <name> \
           && gh pr merge -R <gh-repo> <pr-number> --squash --delete-branch
         ```

         `assert` is read-only and lock-free (exit `0` iff the holder is `<loop>` and the hold is
         not stale, exit `5` otherwise). Take and merge are two commands in two tools, so a refused
         take stops only itself: a take correctly refused leaves a `gh pr merge` run after it free
         to land on master.

       The **judgement** half — re-reading the Directives and the dispatching loop's answered
       questions and deciding whether any of them contradicts this merge — belongs to that loop and
       must NOT be chained here: `preflight` never returns nonzero because of what it FOUND, so a
       contradicting Directive prints and still exits 0 and `&&` would carry you straight past it.
       (It does exit 2 for a registry it could not read, so a caller still reads the status.)
       Full contract: `skills/lead-dev/SKILL.md` §4.
     - **A red, missing or pending required check** follows `skills/lead-dev/SKILL.md` §4 *Re-running CI*: re-run with
       `gh run rerun <run-id> --failed -R <gh-repo>` only; a text-gate failure is fixed in the text
       and never re-run; a job that failed with 0 steps is a billing block, reported once (to the
       user when standalone) and never re-run to probe.
     - **A red `PR provenance gate` counts as a red required check here**, although it is not a
       required one: the PR is not merge-ready, and the text is fixed per `skills/lead-dev/SKILL.md`
       §4, never re-run.
     - **The merge itself.** On a **fleet-context run this bullet is discharged by the chained
       command above** — do not also run a bare merge, or the PR is merged twice. Standalone:
       `gh pr merge -R <gh-repo> <pr-number> --squash --delete-branch` (default merge mode;
       substitute `--merge` or `--rebase` if the repo convention dictates, but always pass an
       explicit merge-mode flag, and always `--delete-branch`: the remote head is removed at the
       source, and `agentflow.refs` removes the local branch it leaves).
     - Verify the merge landed: `gh pr view -R <gh-repo> <pr-number> --json mergedAt` — `mergedAt` must be non-null.
       This check also settles a non-zero exit from any of the merge commands above: `gh pr merge`
       can fail after the merge lands (the remote branch delete failing, say), so a non-null
       `mergedAt` means merged, and the merge is never re-run.
     - **Auto-doc refresh (best-effort; autodoc-enabled project only).** Now that the epic's code is on
       `<canonical_repo.host_ref>` `main`, refresh the managed code wiki (`autodoc_root`) from the just-merged commits.
       Gate it: run ONLY if this epic's source repo is the autodoc-enabled project's repo (`<canonical_repo.host_ref>` — i.e. the
       plan `worktree`/git remote resolves to the canonical repo). **Skip** for any project other than
       the autodoc-enabled one (the project whose `autodoc_root` is configured) — autodoc
       only documents that project, so an update elsewhere is a no-op or wrong. When it applies, run
       `/autodoc-update` (the vault's autodoc-update skill; from outside the vault, follow
       `<vault.autodoc_update_skill>`) with the project exported: `export AUTODOC_PROJECT=<name>`
       as the first line of the same shell command that runs its Step 1 block, where `<name>` is the
       plan's `project:`. An export in a separate call does not survive, and that Step 1 refuses to
       start without it whenever more than one profile is installed. It `git diff`s the project repo since
       `.manifest.json`'s `last_sha`, maps changed files → affected pages via the manifest reverse-index,
       and runs its refresh workflow. **Best-effort:** on any failure (or if the vault skill is absent),
       append an `autodoc_update_pending: <reason>` tier-2 decision and continue — NEVER block epic
       completion on the doc refresh.
       - _Execution note (load-bearing):_ `autodoc-update` runs `update-workflow.js` via the Workflow
         tool, and its recipe **bakes the args into a variant script** (`const args = {...}` injected
         immediately AFTER `export const meta`, which must stay the first statement; then call the
         Workflow tool with `scriptPath` only, no `args` param). Follow it as written: the autodoc
         skill owns its delivery recipe.
     - Call `mark_ended(state)`.
     - Set `state.status = "done"`.
     - Send ntfy `EPIC_DONE` with detail `"merged PR #<n>"` via `notify_once` with `key=f"epic_merged_{epic_slug}"` (not `epic_done`, which Step 9.6 already spent inside the dedup window).
     - Exit the merge-gate branch and continue to step 8 (final persist + release + exit).
   - **`hold`:**
     - Leave the PR open (no `gh` action required).
     - Call `mark_ended(state)`.
     - Set `state.status = "done_on_hold"` (annotate the hold decision in the decision log via `append_decision(state, task=0, tier=2, summary="merge_decision=hold; PR #<n> left open")`).
     - Send ntfy `EPIC_DONE` with detail `"PR left open per hold decision"` via `notify_once` with `key=f"epic_on_hold_{epic_slug}"`.
     - Exit and continue to step 8.
   - **`abandon`:**
     - `gh pr close -R <gh-repo> <pr-number>`.
     - Call `mark_ended(state)`.
     - Set `state.status = "abandoned"`.
     - Send the abandoned ntfy with detail `"PR closed per abandon decision"` via `notify_once` with `key=f"epic_abandoned_{epic_slug}"` and `trigger_type=TriggerType.EPIC_DONE` (the epic has ended; the detail says how).
     - Exit and continue to step 8.
   - **`null`** (user hasn't decided yet):
     - Do not change `state.status` — it remains `awaiting_merge_decision`.
     - Persist `status.md` (no state change otherwise).
     - ScheduleWakeup in 30 minutes (re-check on next wake).
     - Release the lock and exit. **Do NOT proceed to step 8 yet** — step 8 only runs after the user provides a decision.

8. Persist `status.md` (includes the `ended` timestamp + `session_ended:` event from step 4), release lock, **do not schedule next wake**, exit.

#### Step 9.X — Always schedule a defensive wake before exit

Before exiting any turn, ALWAYS call `ScheduleWakeup`. Cadence:

| Situation | delaySeconds | reason hint |
|---|---|---|
| Wave in flight | 240 | "polling wave Tn" |
| Reviewer in flight | 240 | "polling reviewer for Tn" |
| Paused (no halt_reason) | 300 | "paused; checking for unpause" |
| Idle (no wave, no review, not paused, no halt_reason) | 900 | "idle tick" |
| `awaiting_external_resolution` | use `blocker_watcher["next_wake"]` | "blocker_watcher poll" |
| `halt_reason` set | DO NOT schedule — release lock, exit | — |

NEVER exit without a scheduled wake unless `halt_reason` is set. A session that
edits files, finishes its turn and schedules no wake stalls silently: nothing
wakes it again.

### Step 10: Persist + schedule

1. Update `state.last_wake = <now>`
2. If still scheduling: `state.next_wake = <now + 3-4 min>`. Otherwise leave null.
3. `write_status(path, state)` — atomic write
4. Schedule next wake via the ScheduleWakeup tool. **Cap at 4 minutes — never longer.**
   - `status == "running"`: 3-4 minutes (default 240s)
   - `status == "escalation"`: 4 minutes (re-check for user answer; same cap)
   - else: do not schedule
5. Release lock (delete lock file)
6. Output a terse summary to the user. Two lines max plus a rough-progress prefix:
   - **Landed:** `<n>/<total>` (rough %) — which tasks completed this wake + commit SHAs
   - **Readying:** which tasks are dispatching now (or queued for next wake), naked task IDs only
   Do NOT announce next-wake timing (it's frequent enough). Do NOT preview what the next wake will dispatch in detail. Do NOT explain task semantics — the user knows what their tasks do. Do NOT use headers, bold-section banners, or multi-paragraph reports. The user reads the diff and `status.md` if they want details; chat output is just a heartbeat with progress.

## Subagent contract

Every wave subagent (implementer, verifier, reviewer) operates under a binding contract enforced at two levels: the orchestrator's `coerce_wave_outcome()` guard (`agentflow.status_writer`) rewrites a self-reported outcome that contradicts its own findings, and the **scoped-staging gate** — §(d) below, mandated verbatim by the implementer prompts — keeps each commit inside `verification.file_allowlist`. This section describes what every subagent **must** do and what the orchestrator **guarantees** in return.

> **The staging gate is a procedure, not a hook.** Nothing in this repo blocks a commit mechanically: there is no pre-commit hook under any packaging, and the suite's hook manifest (in the source repo, not shipped with the skills) registers exactly one hook, the config injector that runs on `SessionStart`. A subagent that skips §(d) commits successfully and is caught later, or not at all. Saying so is the point: a reader who believes a hook is watching has no reason to run the check by hand.

> Implementation detail lives in `prompts/`, `agents/`, `workflows/` and `templates/` beside this file, with the shared guards in the `agentflow` package. This section documents the contract — the stable interface — not the mechanics that may evolve.

### (a) EvidenceBlock JSON — mandatory return
**Evidence states.** Empty, unavailable, truncated and stale are different answers: `<suite_root>/skills/_shared/EVIDENCE.md`.

Every subagent return **must** include the full EvidenceBlock JSON in its response. The EvidenceBlock is the structured JSON block parsed by the orchestrator (see the `Return format` section in each prompt template). A return with no parseable JSON block is treated as a hard failure — equivalent to `outcome=blocked` with `blocked_reason: "missing_evidence_block"`.

Required fields in every return:
- `outcome` — terminal state (one of the valid outcome values; see `Return format`)
- `verifier_iterations` — how many verifier sub-dispatches this wave ran
- `verifier_verdict` — the final verdict from the last verifier iteration
- `verifier_findings_count` — per-severity tally: `{"low": N, "medium": N, "blocker": N}`
- `verifier_findings` — flat list of finding objects at final iteration
- `verifier_history` — chronological per-iteration snapshot list
- `commit_sha` — the git SHA of the task commit if `outcome=done`, else `null`
- `files_changed` — list of file paths modified

The orchestrator writes these fields to `TaskState` (via `agentflow.status_writer`) and into the status board. Callers that do not emit this block cannot be tracked and will be re-dispatched on the next wake as orphaned in-flight tasks.

### (b) DoD checklist injection — implementer waves

Every implementer wave prompt is pre-populated with a Definition-of-Done (DoD) checklist derived from the task's `verification.acceptance_criteria`. The checklist is injected into the prompt by the orchestrator at dispatch time (Step 7 operational-context block). The implementer **must** self-certify each checklist item before reporting `outcome=done`. Unchecked items found by the verifier are automatically classified as blocker findings.

The DoD injection format appears in the prompt as:

```
## Definition of Done (self-certify before returning outcome=done)
- [ ] <acceptance_criterion_1>
- [ ] <acceptance_criterion_2>
...
```

Implementers that skip the DoD self-check — or report `done` without satisfying all criteria — will be caught by the verifier and recycled (up to `max_fix_iterations`).

### (c) coerce_review_verdict — all reviewer roles

`coerce_review_verdict` applies to **every** reviewer role: spec-reviewer, code-quality-reviewer, and the in-wave verifier. It is called by the orchestrator immediately on parsing any reviewer's JSON verdict. It enforces the INVARIANT:

- If `findings_count.blocker > 0` and the verdict is `clean` or `warnings` → rewritten to `blocker_persistent`; `outcome` rewritten to `verifier_blocker_persistent`.
- If `findings_count.blocker == 0` and the verdict is `blocker_persistent` → treated as a data error; escalated.

A reviewer cannot ship a blocker finding by mislabeling it as a warning. The guard is deterministic and runs regardless of what the reviewer self-reports. Tier-2 audit decisions log every rewrite so the coercion is visible in the dashboard.

### (d) Scoped-staging gate — required before each git commit

Before every `git commit` in a wave, the implementer **must** run the scoped-staging gate (the same procedure the implementer prompts mandate):

Throughout this gate `<worktree>` is the tree the wave's own `## Worktree context` block named it
(its per-wave worktree, or the epic worktree on the legacy path) — not a tree it inferred.

1. Stage ONLY explicit paths from `verification.file_allowlist` (`git -C <worktree> add <explicit paths>` — never `git add .`, `git add -A`, or `git commit -am`).
2. Verify with `git -C <worktree> diff --cached --name-only` that every staged path is inside the allowlist and that no `verification.regression_guard` path appears in the staged set.
3. On any violation: unstage the offender, abort the commit, and surface the violating paths in `blocked_reason`.

A wave that commits without this gate risks carrying sibling-wave diffs into its commit (parallel-wave collision) or touching protected files. The `verification.file_allowlist` hard gate (tier-3 in auto-mode) applies at the wave level; the scoped-staging gate is the enforcement mechanism at the commit level.

### (e) evidence_path convention — tier-2+ decisions

For any tier-2 (recommend-and-proceed) or tier-3 (escalation-required) decision, the wave **must** record an `evidence_path` — a pointer to the artifact or observation that drove the decision. This is the `decision_log` entry's optional `evidence_path` field:

```json
{
  "tier": 2,
  "summary": "Extended auth_middleware.py rather than creating new file — both are in allowlist; existing module already imported by callers",
  "evidence_path": "src/api/auth_middleware.py:import_block"
}
```

`evidence_path` is free-form: a file path, a `file:line` reference, a git SHA, a URL, or a short phrase pointing to the observable. It is NOT required for tier-1 (low-stakes naming/formatting) decisions. The orchestrator persists `evidence_path` to `TaskState.decisions` and surfaces it in the dashboard decision log. Tier-3 decisions additionally require `evidence_path` in their `escalation_requests` entry.

### Re-dispatch as the supported fix-iteration path

**Fresh re-dispatch is the supported fix-iteration path.** When a wave fails its verifier loop (blocker findings remain after `max_fix_iterations`), the orchestrator discards the wave's partial work and re-dispatches a fresh wave with more context (the blocker findings from the previous run) appended to the prompt.

> **Note:** SendMessage-based subagent resume is **deferred** — it is NOT an available fix path. The `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` agent-teams API that would enable mid-wave resume is an experimental dependency deliberately excluded from this skill (see spec Out of scope for the rationale). Do not attempt to wire a SendMessage resume path — the orchestrator has no channel to inject into a running wave's context.

The re-dispatch cadence is:
1. First failure → re-dispatch with blocker findings injected as additional context (counts as iteration 1 of the per-wave fix budget).
2. Still failing after `max_fix_iterations` (default 4) → `outcome=verifier_blocker_persistent`, task marked `blocked`, ntfy HARD_FAILURE, exit.

This means every wave operates within a fixed context window (its own session) and the orchestrator manages continuity across failed attempts. Waves should be written to be re-entrant: they must not assume prior partial state from a previous dispatch.

## Failure modes

| Failure | Action |
|---|---|
| Plan frontmatter malformed | `status=blocked`, escalate with parse error |
| `status.md` missing/corrupted | `status.md` lives at `<data_root>/projects/<name>/status/<slug>.status.md` — in NEITHER git tree, and `<data_root>` is not necessarily a repo at all. Recover from history only if it is one: `git -C <data_root> log -- <status-path>`. Otherwise escalate. Never run this without `-C`: an unqualified `git log -- status.md` from the launch directory reports on a file that is not there and returns empty, which reads as "no history" rather than "wrong tree". |
| Subagent BLOCKED twice on same task | Mark task `blocked`, status=blocked, ntfy, exit |
| Test command not found | Escalate, do not proceed |
| `<worktree>` dirty, or on a branch other than the epic's | Escalate, do not dispatch. The tree is `<worktree>` from Step 1.1, never "the repo" — check with `git -C <worktree> status --porcelain` and `git -C <worktree> branch --show-current`. A bare `git status` names no tree; from the launch directory it reports on nothing and the check passes vacuously. |
| Disk full / I/O error | Surface error, preserve last good status.md |
| Regression debug agent gives up | status=blocked, ntfy HARD_FAILURE, exit |
| Deadlock (no ready, no in-flight, not all done) | status=blocked, escalate (plan has unsatisfiable deps) |
| Task on the board but no longer in the plan, not `done` | Step 2.0 escalates with `orphan_escalation_text(tid)` and dispatches nothing; never reported as a deadlock |
| Lock held by another wake | Log + bail, no double-fire |
| Wave returns `outcome=done` with `verifier_findings_count.blocker > 0` | `coerce_wave_outcome()` rewrites to `verifier_blocker_persistent`; existing escalation path (re-dispatch once with more context; if still blocked, mark task `blocked`, ntfy HARD_FAILURE, exit) takes over. Tier-2 audit decision logged. |
| Wave subagent asks an inline clarifying question instead of using `escalation_requests` | Re-dispatch with a stronger AUTO-MODE preface (prepend the AUTO-MODE EXECUTION section verbatim to the prompt). If the wave asks inline again on the second dispatch, mark task `blocked` with `blocked_reason: "auto_mode_violation"`. Do NOT interpret inline questions as escalation requests — the format contract is strict. |
| Wave subagent makes a tier-3 decision unilaterally instead of escalating (e.g., modifies a file outside file_allowlist, makes a schema change, performs an irreversible write without returning `escalation_required`) | Treat as a contract violation. Revert the commit (`git -C <worktree> revert <sha>`), mark task `blocked` with `blocked_reason: "auto_mode_tier3_violation"`, open an Escalation surfacing what was done and why it was reversed, ntfy HARD_FAILURE. Tier-3 decisions are irreversible by definition — a wave that auto-decides one has bypassed the only safety gate available in unattended mode. |

## Escape hatches for the user

- Click button in chat when AskUserQuestion fires → orchestrator dispatches on the selected label (lowest latency; recommended)
- Reply in chat with free-text or canonical abbreviation (e.g., 'y', 'N', 'merge', 'hold') → orchestrator maps the reply to the structured option and continues (fallback; supports legacy typed-answer workflows)
- Edit `status.md` frontmatter `escalations[].answer: "..."` → next wake reads the answer, calls `answer_escalation()` to emit the event + flip `status` back to "running", then unblocks (fallback when chat is not reachable)
- Edit `status.md` frontmatter `paused: true` → next wake exits without doing anything

## Integration

**Composed skills:**
- `../_vendored/writing-plans/SKILL.md` (via `solution` wrapper) — produces the plan with YAML frontmatter
- `../_vendored/subagent-driven-development/SKILL.md` — per-task implementer + spec-reviewer + code-quality-reviewer prompts
- `../_vendored/dispatching-parallel-agents/SKILL.md` — pattern for parallel-safe groups
- `../_vendored/systematic-debugging/SKILL.md` (via `regression_debugger.md` wrapper) — debug agent for regressions
- `../_vendored/finishing-a-development-branch/SKILL.md` — invoked at epic completion
- `/autodoc-update` (the vault's autodoc-update skill) — best-effort code-wiki refresh at epic completion, **autodoc-enabled project only**, after the PR merges (Step 9.2 substep 7d `merge` branch). Skipped for other projects; never blocks epic-done.

**Skill-internal prompts:**
- `prompts/regression_debugger.md` — wraps systematic-debugging for cross-task regressions
- `prompts/simplify-pass.md` — single-subagent wave for Step 9.0.5 pre-review simplification
