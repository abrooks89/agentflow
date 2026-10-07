---
description: Thin wrapper that forwards $ARGUMENTS into the lead-dev skill. Exists so the self-paced ScheduleWakeup continuation string (`/lead-dev --project <name> <target>`, composed from the resolved values) stays a valid slash command — delegated bare skill invocation is unreliable, so the wake re-enters through this command. `$ARGUMENTS` carries both the optional `--project <name>` selector and this wake's work target.
---

Invoke the `lead-dev` skill, forwarding this wake's arguments verbatim.

Arguments for this wake (optional): `$ARGUMENTS`

`$ARGUMENTS` can carry two different things at once: an optional `--project <name>` selector
naming the active project for this lane (`/lead-dev --project agentflow`), and this wake's work
target (an issue ref like `app#123`, a spec path, a repo area). Forward the WHOLE string — do
not strip, reorder, or interpret it here. The skill's step 0 pulls `--project <name>` out FIRST
and treats only the remainder as the target, so a bare `--project agentflow` means "that
project, no explicit target" and never becomes a phantom build target.

Run: `Skill(lead-dev, args="$ARGUMENTS")` — carry `$ARGUMENTS` through unchanged so the
loop's self-paced wake continuation (`ScheduleWakeup(prompt="/lead-dev --project <name> <target>")`, written as literal text from
the values the skill's Step 0 resolved — never the `$ARGUMENTS` token, which does not expand on a self-wake)
re-enters the same builder entry with BOTH the project selector and the target intact. A
continuation that drops `--project <name>` silently reverts to the session's injected default on
the very next wake — the lane changes project mid-loop and nothing says so. Then follow the
skill's procedure to completion, including its end-of-firing `ScheduleWakeup` continuation.
