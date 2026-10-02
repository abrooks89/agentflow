---
description: Thin wrapper that forwards $ARGUMENTS into the report skill. Exists so `/report --project <name>` resolves as a real slash command — dev-manager fires it at session start and on the 24h ceiling, and delegated bare skill invocation is unreliable, so the trigger re-enters through this command. The `--project <name>` selector rides through as `$ARGUMENTS`.
---

Invoke the `report` skill (the fleet status report), forwarding this wake's arguments verbatim.

Arguments for this wake (optional): `$ARGUMENTS`

`$ARGUMENTS` carries the optional `--project <name>` selector naming the fleet this report reads
(`/report --project agentflow`). Forward the WHOLE string — do not strip, reorder, or interpret it
here. The skill resolves the project itself, and a report rendered against the wrong project's
registry is worse than no report, because it looks authoritative.

Run: `Skill(report, args="$ARGUMENTS")` — carry `$ARGUMENTS` through unchanged so dev-manager's
session-start trigger and its 24h ceiling both reach the same entry with the selector intact.

**The report never acts.** It is a pure read: no labelling, no promoting, no reaping, no notes.
Anything that both reports and mutates is eventually run for its side effect, so this entry must
stay read-only no matter what the caller passes.
