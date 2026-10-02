---
description: Thin wrapper that forwards $ARGUMENTS into the dev-manager skill (the human's seat — question hub and refinement loop, open only while a human has it). Exists so `/dev-manager` resolves as a slash command like the other loops. An optional `--project <name>` selector and an optional focus (an epic slug, or "review the feature suggestions") both ride through as `$ARGUMENTS`.
---

Invoke the `dev-manager` skill (the fleet's question hub and interactive prep loop — it pumps registry Question rows and drives epics from `drafted` to `approved-for-autonomous`), forwarding this session's arguments verbatim.

Arguments for this session (optional): `$ARGUMENTS`

`$ARGUMENTS` can carry two things at once: an optional `--project <name>` selector naming the
project this seat manages (`/dev-manager --project agentflow`), and an optional focus (an epic
slug, or "review the feature suggestions"). Forward the WHOLE string — do not strip, reorder, or
interpret it here. The skill's step 0 pulls `--project <name>` out FIRST and reads only the
remainder as the focus, so a bare `--project agentflow` means "manage that project, no explicit
focus" and never becomes a phantom epic slug.

Run: `Skill(dev-manager, args="$ARGUMENTS")` — carry `$ARGUMENTS` through unchanged so the
selector and the focus both reach the skill intact; empty means the skill surveys the queue
itself. Then follow the skill's procedure to completion.

The seat has one mode and it is interactive: it runs while a human has the session open and
schedules nothing when they close it (skill §10). There is no unattended mode and no self-wake
continuation to carry; the unattended pump between visits belongs to the steward. A stale launch
line that still carries `--unattended` is handled by the skill (it ignores the flag and runs the
normal pass), so forward it like any other argument rather than acting on it here.
