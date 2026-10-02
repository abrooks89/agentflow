---
description: Thin wrapper that forwards $ARGUMENTS into the jr-dev-3 skill (the QUATERNARY (interim) parallel builder). Exists so the self-paced ScheduleWakeup continuation string (`/jr-dev-3 $ARGUMENTS`) stays a valid slash command — delegated bare skill invocation is unreliable, so each wake re-enters through this command. jr-dev-3 takes no work target; the only argument it accepts is the optional `--project <name>` selector, because its work comes solely from the registry `## Queue` rows lead-dev publishes to the `jr-dev-3` lane.
---

Invoke the `jr-dev-3` skill (the quaternary parallel builder), forwarding this wake's arguments verbatim.

Arguments for this wake (optional): `$ARGUMENTS`

`$ARGUMENTS` carries ONE thing here: the optional `--project <name>` selector naming the project
whose lane this builder claims from (`/jr-dev-3 --project agentflow`). Forward the WHOLE string —
do not strip or interpret it here. The skill's step 0 pulls `--project <name>` out FIRST; unlike
lead-dev, jr-dev-3 expects the remainder to be EMPTY and must not read it as a work target. It
never selects its own work — it claims only from the registry `## Queue` rows lead-dev publishes
to its lane (`lane: jr-dev-3`), and those rows must be read from the project the selector names.

Run: `Skill(jr-dev-3, args="$ARGUMENTS")` — carry `$ARGUMENTS` through unchanged so the loop's
self-paced wake continuation (`ScheduleWakeup(prompt="/jr-dev-3 $ARGUMENTS")`) re-enters the same
builder with the project selector intact. A continuation that drops `--project <name>` silently
reverts to the session's injected default on the very next wake — the builder starts claiming
lane rows out of a different project's registry. Then follow the skill's procedure to completion,
including its opt-in `parallel_builders` module gate and its end-of-firing `ScheduleWakeup`
continuation.
