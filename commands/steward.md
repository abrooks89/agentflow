---
description: Thin wrapper that forwards $ARGUMENTS into the steward skill. Exists so the self-paced ScheduleWakeup continuation string (`/steward --project <name> <target>`, composed from the resolved values) stays a valid slash command — delegated bare skill invocation is unreliable, so the wake re-enters through this command. `$ARGUMENTS` carries both the optional `--project <name>` selector and this wake's sweep target.
---

Invoke the `steward` skill, forwarding this wake's arguments verbatim.

Arguments for this wake (optional): `$ARGUMENTS`

`$ARGUMENTS` can carry two different things at once: an optional `--project <name>` selector
naming the project this wake sweeps (`/steward --project agentflow`), and an explicit sweep
target (a repo area, a spec path, a named sweep). Forward the WHOLE string — do not strip,
reorder, or interpret it here. The skill's step 0 pulls `--project <name>` out FIRST and treats
only the remainder as the target, so a bare `--project agentflow` means "sweep that project
normally" and never becomes a phantom target.

Run: `Skill(steward, args="$ARGUMENTS")` — carry `$ARGUMENTS` through unchanged so the loop's
self-paced wake continuation (`ScheduleWakeup(prompt="/steward --project <name> <target>")`, written as literal text from
the values the skill's Step 0 resolved — never the `$ARGUMENTS` token, which does not expand on a self-wake) re-enters the same
entry with BOTH the project selector and the target intact. A continuation that drops
`--project <name>` silently reverts to the session's injected default on the very next wake —
the steward starts sweeping a different project's repo and specs. Then follow the skill's
procedure to completion, including its end-of-wake `ScheduleWakeup` continuation.
