# Vendored third-party skills — provenance

The skills in this directory are vendored from **superpowers** by
**Jesse Vincent** (https://github.com/obra/superpowers), MIT License —
see [`LICENSE`](LICENSE) in this directory. Vendoring gives agentflow a
zero-runtime-dependency copy of the process skills the suite's pipeline
invokes by relative path; upstream is not tracked automatically.

## What is vendored

Thirteen skills, imported during the agentflow port (July 2026) from two
upstream snapshots (the closure spanned superpowers 6.1.1 for the
execution-time skills and 5.0.5 for the remainder):

`brainstorming` · `dispatching-parallel-agents` · `executing-plans` ·
`finishing-a-development-branch` · `receiving-code-review` ·
`requesting-code-review` · `review-prompts` · `subagent-driven-development` ·
`systematic-debugging` · `test-driven-development` · `using-git-worktrees` ·
`verification-before-completion` · `writing-plans`

## Local modifications

These copies are **not** byte-identical to upstream: they have been adapted
to resolve paths and configuration through agentflow's config layer where
needed, and `writing-plans` returns to its caller without offering execution
when `solution` invokes it. Treat this directory as a maintained fork, not a
mirror.

## Derived first-party skills

- `skills/refine/` is a diverged **fork** of `_vendored/brainstorming`
  (substantially rewritten for agentflow's refine gate; it also carries
  copies of the brainstorming visual-companion scripts). Its lineage is
  noted here so the attribution travels with the repo.

All other first-party skills under `skills/` are original to agentflow.
