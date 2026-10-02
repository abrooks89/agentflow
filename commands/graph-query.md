---
description: Answer a question about a documented codebase from the autodoc graph store — one routed graph query (local traversal or global community-report search) with file:line citations on every claim.
---

Read the `graph-query` skill, then run it.

Usage:
- `/graph-query <question>` — auto-routed (entity questions go local, breadth questions global).
- `/graph-query local: <question>` — force local search (explicit override, never re-routed).
- `/graph-query global: <question>` — force global search; a corpus with zero community reports fails loudly.

Set `AUTODOC_PROJECT` to the agentflow project whose autodoc store you are querying (the skill's
Config preamble resolves the store path from it, and refuses when it is unset; `PROJECT` is never
read). Local mode needs `AUTODOC_OLLAMA_URL` reachable for
query embedding; global mode needs `AUTODOC_SCORER_MODEL` (and optionally `AUTODOC_SCORER_URL`)
for report scoring — no default model ships.

Answer from the JSON result only: cite every claim with the item's `citation` span, keep any
"via hypothesized link" markers verbatim, and name the gap instead of fabricating when the
graph doesn't cover the question.
