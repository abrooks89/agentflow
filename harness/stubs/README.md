# Record-stubs — how ScheduleWakeup / Workflow are recorded, not run

These stubs let a live recording session drive the CURRENT skills through a scenario
**without ever entering the real recurring wake loop**. They replace the two primitives
that would otherwise block or self-continue — `ScheduleWakeup` and the `Workflow` wave
driver — with **recording no-ops** that write an audit line and return.

This is behavior-frozen scaffolding: the stubs record *what the live skills asked for*.
They do not modify any live skill and they do not change the decision the skill made.

## The ScheduleWakeup stub

**Real behavior:** `ScheduleWakeup(prompt, future-time)` registers a future wake; at that
time the harness re-invokes `prompt`. In normal operation this is what makes the
orchestrator / builder loops self-pace across context resets — it is the loop.

**Stub behavior:** instead of registering a real wake, the stub **appends one record** to
`schedulewakeup_log.md` and returns immediately:

```
<now-UTC> | scenario=<A|B|C|D> | wake_at=<future-UTC> | prompt=<verbatim prompt>
```

Consequences that make the harness deterministic:

- **No sleep.** The stub never blocks the session. Wall-clock is irrelevant.
- **No auto-wake.** The stub never re-invokes the prompt. If a scenario needs a wake to
  actually happen, the operator performs exactly ONE inline wake by hand (see
  `../run-scenario.md`) — the recurring loop is never entered.
- **Auditable.** The log row is the evidence `check.py` reads for B9 (a single future
  wake was scheduled) and D1-D3 (the continuation prompt re-targets the same builder
  entry with the resolved target written literally, plus `--project <name>` when the
  launch passed it; a literal `$ARGUMENTS` is a failure).

The operator records the wake by writing the line the real call *would* have produced:
copy the `prompt` argument verbatim, set `wake_at` to any timestamp strictly after `now`,
tag the current `scenario`, and append. Verbatim matters — D-assertions diff the prompt
string, so do not normalize, trim, or re-quote it.

## The Workflow / wave stub

**Real behavior:** the `Workflow` tool drives a wave of subagent tasks and, on a
self-paced run, chains the next wave via its own continuation.

**Stub behavior:** the wave is run **inline, exactly once**, and its continuation is
routed through the ScheduleWakeup stub above (so any "schedule the next wave" intent lands
in the log as a record, not a real dispatch). One wave runs; the loop does not continue on
its own. Scenario B is the single scenario that drives a *second* inline wake, and it does
so manually to prove the re-prompt-guard marker (E2) gates the buttoned prompt — it is
still never the recurring loop.

## Invariants (do not violate when recording)

1. A stub RECORDS; it never sleeps, never auto-wakes, never re-enters the recurring loop.
2. `wake_at` is always strictly in the future relative to the row's own timestamp.
3. The `prompt` field is captured verbatim from the live skill's call.
4. At most ONE inline wake cycle per scenario (Scenario B's scripted second wake is the
   sole, deliberate exception, used only to assert the re-prompt guard).
