# Phase-4 SHIP GATE — use-case traceability acceptance scenario (T11)

Self-contained, deterministic proof that the epic → use-case → flow-page → code
**traceability spine** works end to end. Standalone from `harness/check.py` (the
Phase-0/1 golden gate) so it can never perturb the 31-PASS baseline.

## Run

```bash
python harness/uc-scenario/uc_scenario.py        # exits 0 iff all 13 assertions PASS
```

Every run rebuilds throwaway fixtures under `harness/uc-scenario/_scratch/`
(gitignored) — a toy git code repo plus refine / solution / flow docs. **The live
vault and real project data are never touched.** The minted id is deterministic
(`UC-0001`, counter starts fresh each run).

## What it proves

1. **Mint** — an opaque `UC-NNNN` via `agentflow.use_case_id.allocate` with a
   birth-cert registry row (UC1).
2. **Spine** — one id placed as a refine functional-story block, a solution
   `## Use Cases` doc-of-record + task-frontmatter `use_cases`, a `Use-Case:` git
   trailer, an activation-chain anchor in tracked source, and a flow-page
   `use_cases:` frontmatter + `## Use Cases` row.
3. **Grep chain** — `grep -r UC-0001` (design/specs, design/plans, code, flow) + `git log
   --grep` (commit) returns **all five hops** (UC2–UC7); `agentflow.use_case_impact`
   locates the same floor across all stages (UC8).
4. **Gate-safety** — `plan_parser` threads the additive optional `use_cases` field
   (UC9); the flow-page `use_cases` list is an **opaque scalar** (no `[[wikilink]]`),
   inert to `graph-check` (UC10).
5. **Reconcile (liveness protocol)** — with code present the flow-page per-UC
   Status reconciles to `completed` (UC11); a simulated `git rm` of the activation
   chain + re-reconcile flips it to `deprecated` with a `> [!stale]` banner (UC12),
   **without editing the plan / solution / refine docs** — reconcile reads code,
   never copies plan status (UC13).

Output is `PASS/FAIL <id>: <detail>` lines + a summary; exit code 0 iff every
assertion passes.
