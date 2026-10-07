# Recording harness calls by hand — ScheduleWakeup and Workflow

These notes describe how a live recording session logs what the CURRENT skills asked for.
**Nothing in this directory intercepts or replaces `ScheduleWakeup` or the `Workflow` wave
driver.** There is no stub code here, only this README and the log the operator writes.
When a skill calls either tool during a recording, the call is real.

This is behavior-frozen scaffolding: the log records *what the live skills asked for*.
It does not modify any live skill and it does not change the decision the skill made.

## Recording ScheduleWakeup

**Real behavior:** `ScheduleWakeup(prompt, future-time)` registers a future wake; at that
time the harness re-invokes `prompt`. In normal operation this is what makes the
orchestrator / builder loops self-pace across context resets — it is the loop.

**During a recording:** nothing intercepts the call, so the real wake is still registered
(see *Stopping a registered wake* below). The operator **appends one record by hand** to
`schedulewakeup_log.md` for each call:

```
<now-UTC> | scenario=<A|B|C|D> | wake_at=<future-UTC> | prompt=<verbatim prompt>
```

Consequences that make the harness deterministic:

- **No sleep.** Nobody waits for the registered wake, but it is on a real timer: end the
  session before it fires (see *Stopping a registered wake*).
- **No reliance on the real wake.** If a scenario needs a wake to actually happen, the
  operator performs exactly ONE inline wake by hand (see `../run-scenario.md`) and stops
  the registered one before it fires — the recurring loop is never entered on purpose.
- **Auditable.** The log row is the evidence `check.py` reads for B9 (a single future
  wake was scheduled) and D1-D3 (the continuation prompt re-targets the same builder
  entry with the resolved target written literally, plus the `--project harness-toy`
  every Scenario D launch passes; a literal `$ARGUMENTS` is a failure).

The operator records the wake by writing the line for the real call:
copy the `prompt` argument verbatim, set `wake_at` to any timestamp strictly after `now`,
tag the current `scenario`, and append. Verbatim matters — D-assertions diff the prompt
string, so do not normalize, trim, or re-quote it.

## Stopping a registered wake

Any recording session registers a real wake when a Scenario B or D run calls
`ScheduleWakeup`; one running with bypass permissions does so without prompting. Writing
the log row does not cancel it. A wake belongs to the session that scheduled it, so it
ends when that session ends (the same reason `../../docs/INSTALL.md`, "Stopping, and
undoing the install", gives for loops). Once the scenario's records are written, end the
session (`Ctrl`+`C` in its window, or close the window) before the real wake fires (the
call's `delaySeconds` after it was made, not the `wake_at` you logged). If the wake
fires first, the skill's prompt runs again in that session: that is the recurring loop, so
interrupt it and end the session. Nothing that firing does belongs in the recording.

## Recording a Workflow wave

**Real behavior:** the `Workflow` tool drives a wave of subagent tasks and, on a
self-paced run, chains the next wave via its own continuation.

**During a recording:** nothing intercepts `Workflow` either. The operator runs the wave
**inline, exactly once**. If the skill then calls `ScheduleWakeup` to chain the next wave,
that call is real: record it by hand as above and stop the registered wake as above. The
workflow's completion notification also wakes the session: treat that as the one inline
wake, and do not let a later notification or the fallback wake run. One wave runs; the
operator does not let the loop continue. Scenario B is the single scenario that drives a
*second* inline wake, and it does so manually to prove the re-prompt-guard marker (E2)
gates the buttoned prompt — it is still never the recurring loop.

## Invariants (do not violate when recording)

1. The log RECORDS. The operator never waits for a registered wake, never lets one fire
   into the recurring loop, and never re-enters that loop.
2. `wake_at` is always strictly in the future relative to the row's own timestamp.
3. The `prompt` field is captured verbatim from the live skill's call.
4. At most ONE inline wake cycle per scenario (Scenario B's scripted second wake is the
   sole, deliberate exception, used only to assert the re-prompt guard).
