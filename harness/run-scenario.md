# Scenario runbook — recording A/B/D; C, F and G run from tracked fixtures

This is the operator runbook for producing the A, B and D recordings. A **live
Claude session** drives the CURRENT skills through each scenario against the fixtures in
`stubs/` and the toy repo, using the **record-stubs** (`stubs/README.md`) so no real wake
loop is ever entered. The deterministic checker `check.py` later reads the filesystem
artifacts these runs produce and checks properties of them - it never compares one
recording with another, so to show a change preserved behaviour, re-record A, B and D
after it and re-run `check.py <config>`.

Behavior-frozen rule: you are RECORDING how the current skills behave. Do not modify any
live skill, command, or the toy repo's seeded baseline mid-run except as the scenario
prescribes.

## Global rules (every scenario)

- **Config indirection.** All path/skill/command names come from
  `harness.config.json` (`solution_skill`, `orchestrator_skill`, `builder_entry`, dirs).
  Never hardcode them into a recording; read them from the config so every re-recording
  uses the same procedure.
- **Stubbed wakes only.** Any `ScheduleWakeup` the skill fires is captured by the stub as
  a row in `stubs/schedulewakeup_log.md`. It does not sleep and does not auto-wake.
- **One inline wake cycle MAX.** Where a scenario needs a wake to actually advance, the
  operator performs exactly ONE inline wake by hand. Never enter the recurring loop.
  (Scenario B is the single, deliberate exception: it drives a scripted SECOND inline wake
  purely to prove the re-prompt guard — still not the loop.)
- **Canned gate answers.** When a skill raises an approval / decision gate, answer with the
  scenario's canned response below and nothing else. The three canned answers used across
  the suite are: **None**, **Approve**, **Dispatch**.

## Scenario A — refine -> solution emits a parseable plan

- **Entry:** invoke the `solution_skill` (config: `solution`) on the canned refine
  input `refine-toy.md`.
- **Gate:** the deps/decomposition gate -> answer **None** (no extra dependencies).
- **Wake:** none. Scenario A produces a plan file only; it schedules no wake.
- **Records for the checker:** the emitted plan file (frontmatter contract, parse success,
  DAG membership, `test_commands`, slug, `per_wave_worktrees: true`, well-formed-or-null
  verification blocks) — assertions A2-A9.

## Scenario B — single-task plan -> one wave -> review -> commit

- **Entry:** invoke the `orchestrator_skill` (config: `orchestrator`) on
  `plan-single-task.md` against a freshly re-inited `toy-repo`.
- **Gate:** the orchestrator's approval gate (#2) -> answer **Approve**.
- **Wake:** run **exactly one** inline wake cycle. The wave runs inline once; the
  `ScheduleWakeup` the orchestrator fires is recorded to `stubs/schedulewakeup_log.md`
  (tag `scenario=B`, `wake_at` in the future, prompt verbatim) — this is assertion B9.
- **Second wake (B/E2 only):** after the first wake writes the
  `escalation_prompted` / `merge_decision_prompted` decision-log markers, drive ONE
  scripted second inline wake and confirm the buttoned prompt does NOT re-fire (the marker
  gates it). This is the re-prompt-guard evidence (E2). This is the ONLY scenario
  that drives two wakes; it is still not the recurring loop.
- **Records for the checker:** worktree created, >=1 new commit touching `calc.py`, commit
  diff within the file allowlist, `status.md` marks the task done,
  `test_status.result == pass`, review verdict recorded, independent pytest on the worktree
  HEAD green, one future `ScheduleWakeup` logged — assertions B2-B9 (+ E2).

## Scenario C — loud hard-fail on a malformed plan (and its complement)

- **Entry:** call the parser on `plan-bad-task-type.md` (a plan whose task 1 carries a
  `type:` outside `{standard, tdd}`).
- **Gate:** none reached — the parser must raise BEFORE any gate.
- **Wake:** none. Assert NO `status.md` is created and NO wake is scheduled (the log gains
  no `scenario=C` row).
- **Records for the checker:** the raised error names the offending `type` key; no status
  file; no wake — assertions C1-C4. C5-C8 assert the resolved `agentflow` parser and the
  ported skill trees (C7 only when `preport_ip_literals` is set in your local config).
- **The complement, C14:** `plan-missing-epic.md` served the entry above until gh#74, which
  made `epic` optional because `solution/` holds human-authored operator documents as well as
  orchestrator plans, and requiring orchestrator frontmatter of both dropped fifty files from
  every survey, silently. That fixture is KEPT with its assertion INVERTED — C14 proves it
  now parses — rather than deleted, so the harness still records that the old strictness
  was retired on purpose and a later reader cannot quietly restore it.
- **The steward's delegated sweep, C15 (gh#224):** nothing to record. Like C11/C12 it drives
  the real registry CLI against a throwaway registry seeded with four open questions, one of
  them the newest and lowest-class yet waited on by a parked Queue row. It runs the steward's
  domain-P procedure in order — read, lift the blocking question over the read's own order,
  answer the delegated ones `--by steward`, decline the two human-only ones with their reasons in one routed
  note —
  and asserts every question ends with a recorded outcome and `--by human` is refused. The
  delegated-vs-human-only call is supplied as data, as scenario F supplies bug-vs-epic, and the
  sweep order is restated in the check from the skill's prose rather than read from it; C15
  covers the registry behaviour around that judgement, not the judgement.

## Scenario D — builder command self-paced wake-continuation

- **Entry:** fire the current `builder_entry` (config: `/lead-dev`) with a concrete
  `<target>`.
- **Gate:** answer the dispatch gate **Dispatch** if raised.
- **Wake:** capture the `ScheduleWakeup` continuation prompt VERBATIM into
  `stubs/schedulewakeup_log.md` (tag `scenario=D`). Perform at most one inline wake to
  confirm the continuation re-invokes the SAME `builder_entry` with the resolved target
  written literally, plus `--project <name>` when the launch passed it (the lead-dev
  continuation contract). A literal `$ARGUMENTS` in the recorded prompt is a failure.
  Confirm no second wake fires on its own.
- **Records for the checker:** the recorded continuation prompt re-targets the config's
  `builder_entry` with arguments intact; exactly one wake row for `scenario=D`; no
  spontaneous second wake — assertions D1-D3. D1-D3 read the expected continuation command
  from `harness.config.json` `builder_entry`, never hardcoded, so the same procedure
  validates whatever builder entry the config names (`/lead-dev` as shipped).

### Scenario D consume-first (assertion D4)

Records that a builder wake which finds an ANSWERED `## Questions` row applies it before
claiming new work (`docs/REGISTRY.md` firing contract, step 2 CONSUME FIRST). Requires a
live builder firing — this record cannot be canned.

1. **Seed.** Copy `fixtures/scenarioD-registry-before.md` to the toy project's registry
   path (`<data_root>/projects/<toy>/status/registry.md`). It seeds: `Q-0101` in state
   `answered` (raised by the builder, answering its parked work), Queue row `E-0101`
   `parked` on `Q-0101`, and a fresh `ready` row `E-0102` in the same lane. Do not edit the
   seed — D4 reads its ids from the fixture, never hardcoded.
2. **Fire.** Fire the `builder_entry` exactly as in the base Scenario D procedure (record
   stubs active, one inline wake maximum, canned answer **Dispatch** if a gate is raised).
3. **Capture.** After the single wake's WRITE phase completes, copy the toy project's
   registry verbatim to `records/scenarioD/registry-after.md`. Do not hand-edit it.
- **Records for the checker:** D4 asserts the captured registry shows `Q-0101` flipped to
  `applied`, `E-0101` no longer `parked` (resumed), and `E-0102` still unclaimed — i.e. the
  wake consumed the answer instead of grabbing fresh work first. A wake that claims
  `E-0102` while `Q-0101` sits answered-but-unapplied is a real consume-first violation to
  report, not to paper over.

## Scenarios F and G — issue intake, reconstructed (assertions F1-F4, G1)

**There is nothing to record.** F and G used to demand a live dev-manager pass against a
tracker the harness controlled, and their records lived under `records/`, which is gitignored —
so both were permanently red on every fresh clone and in CI. E-0360 replaced the recording with
a reconstruction, following the scenario-D precedent: **never commit a record.** A record is
canned for every clone after the first; a fixture is an input the assertion reasons about, which
is a different thing.

They now run anywhere, with no account and no network:

```
python harness/check.py harness/harness.config.example.json --only C,F,G
```

CI runs exactly that line. If you are here because F or G went red, it is a real regression in
`agentflow.issue_intake` — not a missing recording.

### What actually runs

`_reconstruct_scenario_f()` replays a triage pass over
`fixtures/scenarioF-issues-before.json`, using the product's own functions through the
injectable `Runner` seam (`issue_intake.py:57`, `:168-204`):

1. **`untriaged()` draws the visit set.** The already-`triaged` seeds are excluded by the
   product, not skipped by the harness — that is the whole of F1 and F4.
2. **`label_issue()` builds the argv**, captured by a fake runner that returns `(0, "", "")`.
3. **The after-state is parsed back OUT of that argv**, never read from a hand-written
   after-fixture. Build the wrong `--add-label` and F2/F3 go red.
4. **`builder_selectable()`** over the same fixture is the whole of G1.

### The four seeds, and why there are four

`fixtures/scenarioF-issues-before.json` is classified by LABEL, never by number
(`_scenario_f_seed`, `_seeded_triaged_epic`), so a tracker handing out different numbers does
not silently invalidate anything:

| role | seeded labels | why it is there |
|---|---|---|
| bounded bug | `bug` | F2: ends `triaged`+`bug`, no spec carved |
| epic-sized | *(none)* | F3: ends `triaged`+`epic` with a carve at `drafted` |
| already triaged | `triaged`, `bug` | F4: `untriaged()` never offers it to the pass |
| triaged **epic** | `triaged`, `epic` | G1: makes the `epic` exclusion discriminating |

The fourth seed earns its place: without it every seed is decided by the `triaged` clause
alone, and an implementation of `builder_selectable` that dropped the `epic` exclusion entirely
still returned the right answer over the fixture. That version of G1 passed. It was found by
mutation, and the seed is what kills it.

### What these assertions do NOT cover, stated so a green run is not read as more

**The bug-vs-epic judgement.** The contract table in `skills/dev-manager/SKILL.md` §3.4 names
its own decider — *"dev-manager (delegated)"*, *"bug-vs-epic is spec-interpretation"* — and
there is no triage or carve function anywhere in `issue_intake.py`. So no CI run reconstructs
that call; it is supplied as data by `fixtures/scenarioF-dispositions.json`, and F1/F2/F3 each
say so in their own PASS text. What they cover is everything downstream of the judgement.
**G1 carries no such caveat**, deliberately: it calls the function that decides, so a green G1
*is* the rule holding.

**`untriaged()`'s spec cross-link filter.** The reconstruction passes `linked=set()` — the
state a first triage pass runs in — so the `not in linked` clause is a no-op here and a
mutation deleting it survives F. That clause is covered by
`skills/_shared/tests/test_issue_intake.py::test_untriaged_drops_triaged_and_linked` instead.

### Changing the fixtures

`fixtures/scenarioF-issues-before.json`, `scenarioF-dispositions.json` and
`scenarioF-carved/` are tracked. F3 matches a carve to its issue on `gh#<number>`, so if the
numbers ever move, the fixture and the carve's `source:` move **together, in one deliberate
commit** — editing one and not the other turns a real assertion into a confusing red.


## After recording

Leave the produced artifacts in place (`toy-repo` state, `status.md`,
`stubs/schedulewakeup_log.md`, decision log). These are gitignored (they embed personal
paths / server IPs) but stay on disk for `check.py` to read. Do not hand-edit them to make
an assertion pass — a red assertion is a real finding to report, not to paper over.
