---
description: Generate a full code-documentation wiki from a source repo using an ultracode workflow. Clones the repo, scaffolds the structure, fans out parallel agents to map every endpoint/page/component/service/process/vendor/runbook/data-model/ADR/flow/job, then builds the index and overview.
---

Read the `autodoc-init` skill, then run it.

Usage:
- `/autodoc-init` — document the project's repo (resolved from the project profile's `canonical_repo`) into that project's resolved `autodoc_root`.
- `AUTODOC_PROJECT=<name> /autodoc-init` — **required when more than one project is installed.**
  The skill refuses to start otherwise, rather than documenting whichever project the SESSION
  happens to default to. `/autodoc-update` takes the same variable under the same rule.
- `/autodoc-init --force` — re-initialize even if the autodoc root already exists (destructive of the nav layer).

The repo always comes from the project profile — there is no repo argument; to document another
repo, run it under that repo's project. This is an **ultracode / multi-agent workflow** — it
spawns up to 32 agents (the skill's `maxAgents` ceiling: one per community work unit, plus parent
roll-ups, flows/tours/index and diagram batches; the budget is checked before the first dispatch)
and can consume significant tokens. That is intended; the user opted in by invoking it.

Before launching:
- The skill's Config preamble resolves `autodoc_root` per project (`resolve_autodoc_root`: the project profile's override, then `environment.autodoc_root`) + `suite_root` + repo coordinates from agentflow config; it fails loudly if no root resolves.
- If the autodoc root already exists and is populated and `--force` was not given, stop and suggest `/autodoc-update`.
- Read `skills/autodoc-init/references/taxonomy.md` so you can verify agent output against the contract.

After the workflow completes (agents return RECORDS — no agent writes page content), the skill
writes the records to the graph store (`graph records`), renders the markdown tree
deterministically into a sibling directory (`graph render`), and runs the gates (graph-reviewer,
coverage, golden-output vs the frozen baseline). Replacing the live tree is an explicit
human-approved cutover; only then write `.manifest.json`, `log.md`, and `hot.md`, and report
pages-by-type, flows, community-report counts, symbol-join rates, and the agent count vs the
`maxAgents` ceiling.

**There is no commit step — it is RETIRED** (skill Step 7 says why). The corpus sits at
the root `/autodoc-init` resolves through `resolve_autodoc_root` (order in
`<suite_root>/docs/ARCHITECTURE.md#four-pillars-separated-by-authorship`), and in the
standard layout no git repository encloses it, so a "commit the autodoc subtree" step has nothing
to commit into. Version history comes from the vault MIRROR,
which is not wired yet — so report plainly that **the corpus is untracked**: regenerable from the
store, with nothing to restore from if the tree is deleted.
