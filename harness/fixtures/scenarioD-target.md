# Scenario D — concrete target arg

Small explicit `<target>` handed to the `builder_entry` (`/lead-dev`) for the
Scenario D recording. It gives the recording a concrete target to fire the command with,
so it can then confirm the self-paced continuation re-invokes the SAME builder entry with
that target written out as literal text.

**target:** `toy-epic-d`

The command's continuation call emits `prompt="/lead-dev [--project <name>] toy-epic-d"`:
the resolved project (only when the launch passed `--project`) and this target, written
as literal text per the lead-dev continuation contract. It never carries the `$ARGUMENTS`
token, which a wake does not expand. That literal form is what D1 asserts (builder_entry
re-invoked, resolved target present, no `$ARGUMENTS`).
