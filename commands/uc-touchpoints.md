---
description: Print the advisory touchpoint floor for one opaque use-case id (UC-NNNN), grouped by SDLC stage — refine, solution, commits, code, flow. Read-only, never edits, always exits 0.
---

Run the use-case impact tool for the given id, forwarding this invocation's arguments verbatim.

Arguments for this invocation: `$ARGUMENTS`

Usage:
- `/uc-touchpoints UC-NNNN` — print every located reference to that id, grouped by stage.
- `/uc-touchpoints UC-NNNN <project>` — disambiguate when more than one project profile exists (otherwise the sole project is used).

What it does:
- Reads the registry birth-cert row (`<data_root>/projects/<name>/design/use-cases/registry.jsonl`) for the id's title + owning epic.
- Grep-scans the refine and solution docs under `<data_root>/projects/<name>/` for the id.
- `git log --grep 'Use-Case: <id>'` over the project code repo (`canonical_repo.local_checkout`) for commit trailers.
- `git grep` over the code repo for source references.
- Grep-scans `<autodoc_root>/flows/` for the flow pages that host the id.

Contract (do not change): this is **advisory only** — it never edits a file, never gates or blocks, and **always exits 0**, even when config is unresolvable or `git` is absent. The output is a *floor*, not a proof of completeness: a missing commit trailer just means that hop won't appear, not that the work is absent. The opaque id is matched literally, so nothing here touches graph-check or the relational-link set.

Invocation:

```bash
python -m agentflow.use_case_impact $ARGUMENTS
```

The arguments stay unquoted so the id and the optional `<project>` arrive as two arguments. No `cd` is needed: the `agentflow` package is pip-installed into the suite venv (provision step 5), and install Step 7 puts that venv on PATH, so the module resolves from any working directory. With no arguments the tool prints its usage line to stderr and still exits 0, per the advisory contract.

(More than one project profile requires the trailing `<project>` arg; a single project profile is auto-selected.)
