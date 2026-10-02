---
description: Thin wrapper that forwards $ARGUMENTS into the auditor skill. Exists so the self-paced ScheduleWakeup continuation string (`/auditor $ARGUMENTS`) stays a valid slash command — delegated bare skill invocation is unreliable, so each wake re-enters through this command. Both the optional `--project <name>` selector and the pass target (`--once`, `--re-audit <slug>`, or an explicit epic) ride through as `$ARGUMENTS`.
---

Invoke the `auditor` skill (the post-build intent-feedback loop), forwarding this wake's arguments verbatim.

Arguments for this wake (optional): `$ARGUMENTS`

`$ARGUMENTS` can carry two different things at once: an optional `--project <name>` selector
naming the project this pass audits (`/auditor --project agentflow`), and the pass target
(`--once`, `--re-audit <slug>`, or an explicit epic slug). Forward the WHOLE string — do not
strip, reorder, or interpret it here. The skill's step 0 pulls `--project <name>` out FIRST and
treats only the remainder as the target, so a bare `--project agentflow` means "audit that
project's normal window" and never becomes a phantom epic slug.

Run: `Skill(auditor, args="$ARGUMENTS")` — carry `$ARGUMENTS` through unchanged so the auditor loop's
self-paced wake continuation (`ScheduleWakeup(prompt="/auditor $ARGUMENTS")`) re-enters the same
entry with BOTH the project selector and the target intact. A continuation that drops
`--project <name>` silently reverts to the session's injected default on the very next wake —
the loop starts auditing a different project's epics. Then follow the skill's procedure to
completion, including its end-of-pass `ScheduleWakeup` continuation.
