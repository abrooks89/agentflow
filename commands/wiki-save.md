---
description: Thin wrapper that forwards $ARGUMENTS into the wiki-save skill — the `wiki/` pillar's writer. Exists so `/wiki-save [--project <name>] [what to save]` resolves as a real slash command in an interactive seat; delegated bare skill invocation is unreliable. Both the optional `--project <name>` selector and the subject of the save ride through as `$ARGUMENTS`.
---

Invoke the `wiki-save` skill (the writer for the project's `wiki/` pillar), forwarding this
invocation's arguments verbatim.

Arguments for this invocation (optional): `$ARGUMENTS`

`$ARGUMENTS` can carry two different things at once: an optional `--project <name>` selector
naming the project whose `wiki/` this page lands in (`/wiki-save --project agentflow`), and the
subject of the save (what was learned, or the epic/park/halt/block it came out of). Forward the
WHOLE string — do not strip, reorder, or interpret it here. The skill pulls `--project <name>`
out FIRST and treats only the remainder as the subject, so a bare `--project agentflow` means
"save into that project's wiki normally" and never becomes a phantom subject.

Run: `Skill(wiki-save, args="$ARGUMENTS")` — carry `$ARGUMENTS` through unchanged. A wrapper that
documents the selector in its prose and forwards nothing enters the skill with empty arguments:
the page then lands in whichever project the session's injected default happens to name, and
`pages/` is **create-only**, so a page written into the wrong project's store cannot be moved
back out.

**This is not a vault `/save`.** A vault `/save` skill, if you have one, files a conversation into
a human's Obsidian vault; this one writes one agentflow page into one project's `wiki/pages/`. A
seat that means the vault should follow the vault's own SKILL.md by path rather than by command
name.

**This wrapper is the HUMAN entry, and it is not how a lane saves.** The precondition on
unattended writing is discharged (the first generated pages were judged as page text), so
`lead-dev`, `jr-dev-1`, `jr-dev-2`, `jr-dev-3` and `dev-manager` now discharge the duty at their
own phase boundaries — by calling `agentflow.save_summariser.compose_and_save`, the one place the
bounded one-shot lives, never by re-entering through a slash command.
