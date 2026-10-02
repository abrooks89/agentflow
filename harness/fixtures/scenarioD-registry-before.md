# Fleet Registry — toy-project (Scenario D consume-first seed)

<!-- Seed registry for the Scenario D consume-first recording (assertion D4). Shape follows
     templates/registry.md / docs/REGISTRY.md. The builder (builder_entry, lead-dev) wakes
     to find its own question ANSWERED and its work item PARKED on that answer, plus one
     fresh ready item in its lane. Firing-contract step 2 (CONSUME FIRST) requires it to
     apply the answer — resume E-0101, flip Q-0101 to applied — before claiming E-0102.
     The recording captures the post-wake registry to records/scenarioD/registry-after.md;
     check.py D4 reads ids from THIS file, never hardcoded. -->

## Agents

| agent | status | work | phase | heartbeat | eta | waiting_on |
|---|---|---|---|---|---|---|
| lead-dev | waiting | E-0101 | parked | 2026-08-05T12:00Z | - | Q-0101 |
| jr-dev-1 | idle | - | - | 2026-08-05T12:00Z | - | - |
| jr-dev-2 | idle | - | - | 2026-08-05T12:00Z | - | - |
| auditor | idle | - | - | 2026-08-05T12:00Z | - | - |
| dev-manager | idle | - | - | 2026-08-05T12:05Z | - | - |

## Queue

- **Deploy lock** — holder: none, taken-at: -
- **Notes** — (none)

| id | work item | lane | claimed_by | state | waiting_on | notes |
|---|---|---|---|---|---|---|
| E-0101 | toy-epic-d export naming | lead | lead-dev | parked | Q-0101 | parked on answer |
| E-0102 | toy-epic-d docs pass | lead | - | ready | - | |

## Questions

### Q-0101
- state: answered
- urgency: waiting
- raised_by: lead-dev
- work: E-0101
- raised_at: 2026-08-05T11:50Z
- question: Export file naming for toy-epic-d — `run-<id>.tar` or `<id>-run.tar`?
- default: run-<id>.tar (sorts by prefix, matches existing artifacts)
- assumption: -
- answer: run-<id>.tar
- answered_by: dev-manager
- answered_at: 2026-08-05T12:05Z

## Blockers

| id | raised_by | owner | work | state | description |
|---|---|---|---|---|---|

## Directives

<!-- Standing human instructions, transcribed by dev-manager only. -->
