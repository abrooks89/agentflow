---
description: Incrementally refresh the autodoc code wiki from new commits. Pulls the source repo, diffs against the last-documented commit, maps changed files to the pages that document them, and runs an ultracode workflow to update affected pages, add new entities, and flag deletions.
---

Read the `autodoc-update` skill, then run it.

Usage:
- `/autodoc-update` — pull the source repo, diff against `last_sha` in the autodoc root's `.manifest.json`, and refresh only what changed.
- `AUTODOC_PROJECT=<name> /autodoc-update` — **required when more than one project is installed.**
  The skill refuses to start otherwise, rather than refreshing whichever project the SESSION
  happens to default to; that default is the session's project, not the corpus's. On a
  single-project box it is optional and the default is unambiguous. `/autodoc-init` takes the
  same variable under the same rule.

This is an **ultracode / multi-agent workflow** — it spawns one agent per changed area plus a
reconcile agent. Far cheaper than `/autodoc-init` (touches only changed entities), but still a
multi-agent run; that's intended.

Before launching:
- The skill's Config preamble resolves `autodoc_root` + `suite_root` from agentflow config; it fails loudly if `autodoc_root` is null.
- If the autodoc root or its `.manifest.json` is missing, stop and tell the user to run `/autodoc-init` first,
  after installing Node LTS (with npm) if the machine lacks it and re-running `python install/provision.py --autodoc`
  from the checkout (`--no-auto-allow` if they declined those hooks) — README's "To build a corpus".
- If the repo is already at `last_sha`, report "already current" and stop.
- Read `skills/autodoc-init/references/taxonomy.md` so you can verify agent output against the contract.

After the workflow completes, update `.manifest.json` (bump `last_sha`, merge new pages; `.bak` first),
prepend to `log.md`, refresh `hot.md`, verify, and report updated/created/deprecated counts.

**There is no commit step — it is RETIRED.** The corpus sits at the resolved `autodoc_root`
(resolution order: `<suite_root>/docs/ARCHITECTURE.md#four-pillars-separated-by-authorship`), which
in the standard layout no git repository encloses, so a "commit the autodoc subtree into the
enclosing repo" step has nothing to commit into (`git rev-parse --show-toplevel` returns
*"fatal: not a git repository"*). Version history for the corpus is meant
to come from the vault MIRROR, and that mirror is not wired yet — so say plainly in the run report
that **a refreshed corpus is untracked**: every page is regenerable from the store, but an
accidental deletion of the tree has nothing to restore from. Do not reach for a substitute; a
`git init` in the corpus creates a second history the mirror will have to reconcile with.
