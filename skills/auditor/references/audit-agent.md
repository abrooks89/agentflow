# Audit-Subagent Prompt — one epic, intent verification

Contract for ONE auditor subagent dispatched by the auditor loop's fan-out step (one
subagent per completed epic, parallel). This subagent reconstructs a single epic's
**intent** — its use cases, NOT its task list — verifies whether that intent is actually
live, and returns **structured per-UC verdicts** to the main auditor loop — or, when the
epic cannot be judged at all, a **structured blocker** (Part 3).

This is a **read-only judge**. It never edits product code, never writes specs, never
mints UC ids, never sets any `approved-*` state, never merges or deploys. Its only output
is the verdict or blocker JSON returned to the caller. Gap-epic synthesis and all writes happen in the
main loop, not here.

The caller fills every `{placeholder}` before dispatch. The subagent that receives this
prompt sees only what is pasted — it does not read the caller's reasoning.

---

## Dispatch shape

```
Task tool (general-purpose):
  description: "Audit epic {slug}: intent verification"
  prompt: |
    <this contract, placeholders filled>
```

Caller-supplied context (paste literal, no summaries):

- `{slug}` — the completed epic under audit.
- `{refine_doc_path}` + FULL TEXT of the epic's refine design doc.
- `{solution_doc_path}` + FULL TEXT of the solution doc-of-record (at minimum its
  `## Use Cases` section, every referenced `## Task N` verification block, and the plan
  frontmatter's per-task `verification.test_commands` and top-level `test_commands`, which
  are where the probe commands live).
- `{autodoc_root}` — path to the managed code-wiki root, RESOLVED for this project
  (`resolve_autodoc_root`: the project's own override outranks the machine-level
  `environment.autodoc_root`), or `null` if the project has none. The two differ on any box
  running more than one project, and the zero-hits signal below is only meaningful against
  the corpus that belongs to the epic under audit.
- `{verification}` — the resolved `verification` config block: `strategy`, `url`, `roles`,
  `test_login`.
- `{staging_url}` — resolved `staging.url` (web-journey only), or `null`.
- `{repo_root}` — product repo checkout, for doc/code-level inspection.
- `{product_python}` — the product's interpreter (`canonical_repo.python`, the resolved
  config's `[product python]` line), or `null` when the project sets none. Bare `python` in a
  fleet session is the agentflow suite venv, not the product's, so a product probe run with it
  can import the wrong dependencies; Part 2 says what to do when this is `null`.

---

## Part 1 — INTENT RECONSTRUCTION (do this first, always)

**Intent = the epic's use cases.** A green `status.md` proves tasks finished; it does NOT
prove a use case is met. Reconstruct intent from the UC spine, never from the task list.

Assemble, per UC the epic claims to deliver:

1. **Functional story (Hop 1)** — from the refine doc's `### UC-NNNN` blocks:
   `- **Functional story:** As a <user>, I can <capability>, so that <value>.` plus its
   `Domain:`. This is what the user was promised.
2. **Technical story + acceptance (Hop 2)** — from the solution doc's `## Use Cases`
   section: the `### UC-NNNN` block's `Functional summary`, `Technical story`, `Domain`,
   `Owning tasks`. For each owning task, pull that `## Task N` block's `acceptance_criteria`
   — these are the discrete, testable checks the capability must satisfy.
3. **Current spine status (Hop 3+)** — run `grep -rn "UC-NNNN" {autodoc_root}` per UC id.
   Record where each id lands (flow pages) and the per-UC `Status:` written there
   (`completed` / `in-progress` / `deprecated` / absent). A UC id that greps to **nothing**
   under a non-null `autodoc_root` is itself a signal (intent never reached the code-wiki).
   If `autodoc_root` is `null`, note "no code-wiki" and rely on doc + live evidence only.

Produce, internally, the **check set**: every `{uc_id}` the epic claims, each with its
functional story, its acceptance criteria, and its current spine status. This set — not the
task list — is what you verify and return a verdict for.

---

## Part 2 — LIVE VERIFICATION (gated by `verification.strategy`)

Verify each UC's check set against the running system. **The strategy dictates the method** —
do not improvise a different one:

### `web-journey`
- Drive Playwright **as each configured role** in `verification.roles` (fall back to
  `audit.roles`; if both empty, the default single anonymous journey).
- Authenticate per `verification.test_login`; target `{staging_url}`.
- **Reach the feature VIA REAL UI.** Navigate as a real user would — log in, click through
  nav, land on the surface. **NEVER seed entry state**, never deep-link past the flow,
  never inject fixtures or localStorage to fake arrival. A UC that only "passes" from a
  seeded state is a MISS, because a real user cannot reach it that way. (gate-must-reach-
  via-real-UI discipline: if you cannot get there through the UI, that is the finding.)
- Evidence = screenshot path(s) at the moment the acceptance criterion is observed
  satisfied or failed.

### `test-suite` / `smoke-cli`
- **The probe command comes from the solution doc**, not from project config (no config key
  holds one): each owning task's `verification.test_commands` in the plan frontmatter (the
  per-task verification block), falling back to the plan's top-level `test_commands`
  (`regression`, then `fast`: an audit wants the full probe, not the inner-loop one). Do not
  invent a command the solution doc does not name; when it names none, the probe cannot run
  (Part 3 blocker).
- **Run every Python probe through `{product_python}`.** The one allowed rewrite is the
  interpreter: a command starting with `pytest` runs as `{product_python} -m pytest ...`, and
  one starting with `python` runs as `{product_python} ...`, arguments unchanged. That swaps the
  interpreter, it does not invent a command.
- **When `{product_python}` is `null`**, run the command as written under the session's default
  `python` and put `product interpreter unset; ran under default python` in the evidence. An
  unset interpreter is a normal project state, not a blocker, and blocking on it would leave
  every epic of such a project unjudged. If the probe then fails because the interpreter or a
  third-party dependency is missing, that is the "probe cannot run" blocker (Part 3).
- Do not hand-wave — execute the actual command and capture its real stdout/stderr.
- Evidence = the literal probe command run + its captured output tail.

### `none`
- Doc/code-level check only against `{repo_root}`: confirm the code paths, interfaces, and
  data flow the technical story names actually exist and are wired (not stubbed, not
  TODO-gated, not dead). No live drive.
- Evidence = grep/read citations (file:line) proving presence or absence.

### Playwright is "as necessary", not universal
Drive Playwright **only when the epic's UCs have a user-facing surface** AND
`strategy == web-journey`. A UC with no UI surface (library, schema, internal API) is
verified by suite/cli/doc even in a web-journey project — don't fabricate a browser journey
for something a user never sees.

---

## Part 3 — VERDICT CONTRACT

Return **one verdict per UC / acceptance criterion**. Each verdict:

- `uc_id` — the opaque `UC-NNNN`.
- `criterion` — the specific acceptance check (or the UC as a whole when it has no
  decomposed criteria).
- `verdict` — exactly one of:
  - `satisfied` — the criterion is demonstrably met live; evidence proves it.
  - `partial` — some criteria met, others not; the capability is reachable but incomplete.
  - `missed` — the criterion is not met, or the feature is unreachable via the real path.
  - `regressed` — a criterion that was previously `satisfied`/`completed` (per spine status
    or a prior ledger row) is now broken. Highest-severity class.
- `evidence` — **LITERAL artifact only**, never a claim: a screenshot path, the exact probe
  command + captured output, or grep evidence (`file:line` + matched text). Every non-`missed`
  verdict MUST carry evidence that actually demonstrates the state.

### Hallucination discipline (non-negotiable)
- **Never fabricate a green.** A `satisfied` with empty or invented evidence is a contract
  violation — downgrade it to `missed` (or `partial`) rather than assert an unproven pass.
- Paste **real** output — do not describe a result you did not observe. Judge by artifacts,
  not by expectation. A grep that returned nothing, or a screenshot that shows the feature
  absent, is evidence and the verdict reflects it. A probe that could not run, or a page the
  environment would not serve, is not evidence about the product: that is the blocker below,
  never a verdict.
- Never guess `satisfied`, and never return `missed` for something you could not check. A
  verdict judges the product; when the environment keeps you from judging it, return the
  blocker below instead.

### Blocker — "I could not verify", never a verdict

A blocker and a shortfall are different claims. `missed` says the intent fell short, and the
loop turns it into a gap epic plus a ledger row, and the ledger row means the epic is never
audited again. A blocker says the epic could not be judged this pass, so the loop raises a
registry row, writes no ledger row and no gap epic, and judges the epic again on a later pass.
Reporting an outage as `missed` therefore files a false gap and retires the epic for good.

Return the **blocker shape** (below) instead of any verdicts when one of these holds:

- **No `verification.strategy`** in `{verification}`, so no method is defined.
- **Staging unreachable** — `{staging_url}` does not answer, or a configured role cannot log in
  because the environment is down.
- **Spec or plan doc missing** — `{refine_doc_path}` or `{solution_doc_path}` is absent or
  empty, so the intent cannot be reconstructed.
- **A probe cannot run** — the interpreter, a third-party dependency, a binary or the checkout
  the probe needs is missing, or the solution doc names no probe command. The product's OWN
  modules failing to import or collect is not this case: the product broke, so it is a
  verdict (`regressed` or `missed`), with the import error as evidence.

A blocker covers the whole epic: when any of these stops you from judging part of it, return
the blocker, not a mix of verdicts and a blocker. What stays a verdict is a feature the running
system does not deliver — a UI path that does not lead to it, a probe that runs and fails.
Those are `missed`, and the `unreachable` list names the ones the running system does not
let a user reach. A probe that could not run never goes in `unreachable`; it is the blocker.

---

## Return JSON only — no prose, no preamble

The main auditor loop parses this as JSON.

```json
{
  "epic": "{slug}",
  "strategy_used": "web-journey | test-suite | smoke-cli | none",
  "intent_reconstructed": {
    "uc_count": <int>,
    "uc_ids": ["UC-NNNN"],
    "spine_status": { "UC-NNNN": "completed | in-progress | deprecated | absent | no-code-wiki" }
  },
  "verdicts": [
    {
      "uc_id": "UC-NNNN",
      "criterion": "<the acceptance check, or 'whole-uc'>",
      "verdict": "satisfied | partial | missed | regressed",
      "evidence": "<screenshot path | probe cmd + output tail | file:line grep evidence>",
      "note": "<one line: what was observed; empty if self-evident>"
    }
  ],
  "unreachable": ["<uc_id or criterion the running system does not deliver via the real UI/probe, with why (never a probe that could not run: that is the blocker)>"],
  "roles_driven": ["<role names actually driven, web-journey only>"]
}
```

When a blocker holds (Part 3), return this shape instead, with no `verdicts` key. The main loop
routes on the presence of `blocker`:

```json
{
  "epic": "{slug}",
  "strategy_used": null,
  "blocker": {
    "blocked_on": "<which case: no verification.strategy | staging unreachable | spec/plan doc missing | probe cannot run, plus the specific detail>",
    "tried": "<what you attempted and what it returned, literally>"
  }
}
```

`strategy_used` is the strategy you were working under when the blocker hit, or `null` when
there is none.

**Return the JSON and nothing else.** No markdown headers, no explanation around it. The
verdicts flow back to the main auditor loop, which clusters shortfalls into drafted gap
epics — that synthesis, and every write, belongs to the loop, not to you.
