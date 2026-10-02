# Auditor SHIP GATE (A7) — post-build intent-feedback-loop acceptance scenario

Self-contained, deterministic proof that the **auditor** loop's deterministic
machinery closes the build -> verify -> refine feedback loop. It is a **separate
driver** from `harness/check.py` (the harness's deterministic acceptance
checker), with its own fixtures under a throwaway scratch
tree — and it is now genuinely self-contained: it reads no gitignored config and
runs no subprocess. The relationship with `check.py` runs the other way round.
**`check.py` runs this driver**, on demand via `--auditor-scenario` and as step 6
of `--self-test`; this driver no longer runs `check.py`.

## Run

```bash
python harness/auditor-scenario/auditor_scenario.py   # exits 0 iff all assertions PASS
python harness/check.py --auditor-scenario            # same rows, prefixed `auditor.`
python harness/check.py --self-test                   # runs it as self-test step 6
```

Nothing needs configuring first: the only precondition is `pip install -e
skills/_shared`, which `check.py --self-test` already requires. Standalone runs
rebuild fixtures under `harness/auditor-scenario/_scratch/` (gitignored); when
`check.py` drives it, the fixtures go in a temp directory instead, so the gate
never clobbers a scratch tree you are looking at. Either way it is a temp
`data_root` with three toy "done" epics + a toy UC registry minted by the real
`agentflow.use_case_id.allocate`. **The live vault and real project data are
never touched.** Minted ids are deterministic (`UC-0001`, `UC-0002`; counter
starts fresh each run).

## Injected canned verdicts

The live audit-agent subagent (Playwright / probes) is non-deterministic, so the
scenario **injects canned subagent output** standing in for it:

| toy epic | canned output | outcome |
|---|---|---|
| `epic-satisfied` | UC-0001 `satisfied` | proven — ledger row, no gap |
| `epic-halfmet` | UC-0002 `partial` + `missed` | one drafted gap spec (UC reused) |
| `epic-noverify` | BLOCKER (no `verification.strategy`) | registry `## Blockers` row owned by dev-manager |

Given those, it drives the auditor's **deterministic** steps: discovery via the
real `agentflow.epic_survey`, the ledger dedupe filter, gap synthesis, the
ledger/report writes, and then **all fleet coordination through the shipped
`agentflow.registry` CLI** — the same argv, exit codes and `refused [key]`
stderr a live loop gets. Every outcome is judged by **real filesystem
artifacts** (no fabricated green).

## What it proves (spec "Ship gate (acceptance)" a-g)

- **(a)** satisfied epic -> a ledger row whose cells are the ones
  `skills/auditor/references/ledger-format.md` names, with `gap_epics = -`, and NO
  gap spec filed.
- **(ledger shape)** the header the driver writes is byte-equal to the one in
  `ledger-format.md`, and every data row carries that column count. The shape is
  *read out of* the format document rather than retyped here, so a drifted format
  is a red gate rather than a gate agreeing with its own copy of the old shape.
- **(b)** half-met epic -> exactly ONE drafted gap spec in `design/specs/` with
  `current_state: drafted` and a provenance block whose keys and order are parsed
  from `templates/gap-epic.md`, **reusing the original UC-ID** (UC continuity —
  the auditor never `allocate()`s).
- **(c)** every write confined to `design/specs/` + `status/` — no product-code /
  merge / deploy writes. The snapshot spans **both** the scratch `data_root` and the
  **suite root** (minus `.git`, `__pycache__`, `node_modules`, virtualenvs, tool caches
  and the scratch tree itself), because the writes this forbids land *outside* the
  fixture. Snapshotting only `data_root` could not see them: a driver that wrote
  `harness/LEAKED_PRODUCT_WRITE.txt` kept the gate 15/15 green. It also polices the
  registry writes: `registry.md` and `registry-log.md` live under `status/`, and the
  edit-lock dir sits outside `data_root` entirely.
- **(d)** no `approved-for-autonomous` in the artifacts the pass **wrote** — gap specs,
  ledger, report, registry, state — with a must-exist pre-check, so a missing artifact
  cannot pass by having nothing to scan. **Not covered:** the shipped
  `skills/auditor/SKILL.md`. The verdicts are canned, so that prose never executes and
  its text is never read; stamping `approved-for-autonomous` into the skill leaves this
  green. Policing the skill's own text is a separate assertion, not this one.
- **(e)** a SECOND pass audits nothing — 0 new ledger rows, 0 new gap specs, 0 new
  `B-` rows (ledger dedupe + reading the open blockers before raising; the CLI mints
  a fresh id on every `raise-blocker` and is deliberately not idempotent). The blocked
  epic is re-judged on that pass and still has exactly one open auditor row: the dedupe
  rule is the one `skills/auditor/SKILL.md` Step 6 states, parsed out of the skill and
  applied over a lock-free `registry read`, so a drift between the skill's match columns
  and the driver's matcher fails the gate.
- **(f)** the shipped registry, not a local reimplementation of one:
  - the fleet is `agentflow.registry.AGENTS` — **imported, never listed here, and
    never counted here**. Every loop that tuple exports is seeded from the shipped
    template, and each one's `status` write changes **exactly its own** `## Agents`
    row. Naming or counting the loops in this file is precisely the drift this epic
    set out to kill: the last enumeration said "six" and was false the day the fleet
    grew its next builder. Read `agentflow.registry.AGENTS` for the roster.
  - the auditor's blocker routes to an open `## Blockers` row **owned by dev-manager**,
    and the loop still scheduled a wake (not a human-halt).
  - **ownership is proved by refusals, not by reading back a column the driver just
    wrote**: `--actor human` is refused `agent_known`; a blocker's raiser resolving its
    own row is refused `blocker_owner_resolves`; `cull --actor dev-manager` is refused
    `cull_steward` (E-0220 moved culling to steward).
  - the **steward** cull rolls the resolved row verbatim to `registry-log.md` under a
    `<!-- culled ...Z -->` marker, leaves no `epic-checkout` row behind, and leaves
    **one `## Agents` row per member of `agentflow.registry.AGENTS`** intact — the
    assertion compares against that tuple, so it stays true as the fleet grows.
  - after every mutation the shipped `validate` returns 0 and prints `registry valid`.
  - **(f2b)** the EMPTY pass — the one the scenario's own fixtures never produce —
    schedules a wake and does not halt. It reaches an assertion only by asking
    `decide_continuation([], [])` directly. **What that covers:** this driver's
    reproduction of the continuation rule. **What it does not:** the prose in
    `skills/auditor/SKILL.md`, which no assertion reads — reintroducing the halt rule
    there leaves this green. The auditor SKILL has no importable decision function, so
    a canned stand-in is the only option; overstating its reach is not.
- **(g)** rename: zero surviving `developer-1|developer-2|developer-3` skill-name
  refs in `skills/` + `commands/`; `builder_entry` resolves to `/lead-dev`, read from
  the **tracked** `harness/harness.config.example.json` — the machine-local
  `harness.config.json` is gitignored, and reading it unguarded is what used to abort
  this driver before it printed a single line.

`check.py` owns its own registry size and reports **44** assertions wired
(`python harness/check.py --self-test`); this driver's 15 checks are separate and
are not counted there.

Output is `PASS/FAIL <id>: <detail>` lines + a summary; exit code 0 iff every
assertion passes.

## How the gate runs it, and why in its own process

`check.py --auditor-scenario` (and self-test step 6) runs this driver as a
**subprocess**, with the driver's own `skills/_shared` ahead of `PYTHONPATH`, and
parses the `--emit-rows` lines back out of stdout into `PASS auditor.<id>: ...`.

It used to import the driver in-process, and that made the gate **validate the wrong
checkout**. `check.py` imports `agentflow.plan_parser` at self-test step 2, well before
it reaches the driver; `agentflow` was therefore already bound in `sys.modules`, the
driver's `sys.path.insert` was inert, and every registry assertion graded the
*pip-installed* tree instead of the branch under review. A branch that broke
`registry.py` shipped green through the gate this epic added — the same false-green
class the epic exists to remove, reintroduced by the wiring.

A fresh interpreter has no prior binding. The driver does not take that on trust: on
startup it compares `agentflow.registry.__file__` against the copy beside itself and,
if they differ, **refuses to run** (exit 2, naming both paths). Not a skip — a gate
that silently grades a foreign checkout is worse than one that declines.

## Note on the machinery

What is still reproduced here, and why: the auditor SKILL is a prose procedure with
no importable writer, so the discovery filter, the ledger/report writers and the gap
synthesis are reproductions of the documented steps. Their *shapes* are parsed out of
the documents that define them (`references/ledger-format.md`,
`references/report-format.md`, `templates/gap-epic.md`) rather than retyped, so the
driver cannot quietly agree with itself about a format that has moved.

What is no longer reproduced: fleet coordination. Agents rows, blockers, ownership
refusals and the cull are all `agentflow.registry`'s, driven through its own CLI
in-process. The earlier version carried a private four-section fleet board — its own
skeleton, its own lock, its own ownership rules and its own cull — and asserted
against that. That board was superseded by `docs/REGISTRY.md`, and a gate can prove
nothing about a surface it implements itself.

Where real code exists it is used directly: `agentflow.epic_survey.survey_epics`
(discovery), `agentflow.use_case_id.allocate` (the toy UC registry),
`agentflow.config.Audit` (the real `window=7` default), and `agentflow.registry`
(the whole fleet surface). The point is to prove the observable feedback-loop
outcomes deterministically, judged by real filesystem artifacts.
