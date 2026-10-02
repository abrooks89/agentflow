# Roadmap

Honest and bounded. This lists what is genuinely next, what is further out, and — just as importantly — what agentflow is deliberately **not** going to do. The design bias is practical: build to the current need, leave clean seams, and don't gold-plate.

## Near-term

Things with a clear shape, mostly hardening what already works on the machine it was built on.

- **Fixing what the clean-machine run found.** This item used to read "the outstanding work is an actual clean-machine run". That run exists now: `harness/fresh-clone-install-drill/` clones the repo into a throwaway tree and provisions it against a throwaway config home, data root and Claude root, and `docs/INSTALL.md` records the last verified run and what it was run against. What it found was `gh#186`: on an interpreter without PyYAML, which is what a genuinely fresh machine has, `provision.py` died at step 5 and had to be re-run with the venv it had just built. **That half is fixed** — step 4 now hands the rest of the run over to the venv when the parent interpreter cannot import the dependencies — and the drill's second limb was inverted rather than deleted, so it now requires the clean run and fails loudly if the old failure comes back. The issue's **second** finding, split out as `gh#322`, is fixed for the case it recorded: step 2 no longer pre-creates a scraped `data_root` or `worktree_root` that is relative, or whose only existing ancestor is the drive root, and leaves the loud failure to the step-10 resolver. Which platform the last clean-machine run was on is stated in one place, the Last-verified line in `docs/INSTALL.md`.
- **`lead-dev` as a foreman, and N junior lanes.** The lane set is a hardcoded enum today (`AGENTS`/`BUILDERS`/`LANES` in `agentflow/registry.py`), and `jr-dev-3` is a hand-copied skill added as a stopgap to reach three juniors. The `lead-dev-as-foreman-and-n-junior-lanes` epic replaces the enum with a count-driven set, retires the copied lane, and moves `lead-dev` further toward publishing and assigning rather than building one itself. Anyone reading `skills/jr-dev-3/SKILL.md` and wondering why it looks like a duplicate is reading it correctly: it says so in its own prose, and this is the item that removes it.
- **Readers for the `wiki/` pillar.** The pillar has writers — `skills/wiki-save/`, the projection in `agentflow.wiki`, the steward's digest — and the backfill gave it a corpus. What it does not yet have is a routine reader: no loop's wake procedure reads `hot.md` or its own `hot-<lane>.md` today. A pillar nothing reads is a write-only log with good intentions.
- **Registry write-CLI hardening under real fleet load.** The full-mediation write path now covers the *whole* write surface — owner rows, coordination Notes (`note`), the steward's cross-row liveness sweep (`reap`), malformed-row repair (`repair`), and a fail-closed `validate` backstop run at wake and in CI. What remains is edge-hardening under real concurrent-writer and stale-lock load, and (deliberately **not** planned) a filesystem-level write-block: a mandated CLI plus fail-closed validation is the enforcement ceiling that stays compatible with a file-native, git-diffable, hand-inspectable store.
- **`SEED_FLOOR` recalibration on real embeddings.** The query router's default-path fallback threshold (the score below which a `local` question re-routes to `global`) is currently set against test-scale data; it needs recalibration against real Ollama embeddings over a real corpus.
- **A full production-scale corpus run.** Exercise the autodoc graph — extraction, communities, render, query — end-to-end against a large real repository, not just the parse fixtures and the toy repo, and record where it strains.
- **`autodoc-update` record conversion.** Finish moving the incremental update path onto the same store-primary records → render pipeline the init path already uses, so update and init share one contract.

## Mid-term

Bigger pieces that the seams are already cut for.

- **A pgvector backend drop-in.** The `GraphAdapter` seam exists precisely so the SQLite + `sqlite-vec` backend can be swapped for a Postgres/pgvector implementation with nothing above the interface changing. This is the first real test of the fork thesis.
- **An updater skill for deployed suites.** The deployment manifest (`.agentflow-deploy.json`, source → target + hash per entry) is the seam a future updater reads to refresh a deployed suite in place when the checkout moves ahead.
- **Community reports at production scale.** Global search synthesizes over community reports; generating and validating those reports over a large corpus is the mid-term consumer of the corpus run above.

## Explicitly not doing

Cuts made on purpose. Listing them is part of the honesty.

- **Multi-repo autodoc "areas."** Superseded and gone — the hand-written area map was replaced by **derived Louvain communities**. We are not resurrecting a manually-curated cross-repo area layer; the map is computed from the code or it does not exist.
- **A web dashboard.** Deliberately cut. Work discovery is the filesystem epic-survey, not an HTTP API; there is no server to host and no dashboard to keep alive. The file-native posture is a feature, not a gap to be filled with a UI. The suite's one daemon (single-daemon rule, 2026-09-22) is a scheduled report-only watchdog that pushes to ntfy — it is not a service, and it is not the seam a dashboard would be built on.
- **Cloud / SaaS anything.** agentflow is a suite you install on your own machine and run as Claude Code skills. There is no hosted service, no multi-tenant control plane, no account system — and none is planned.
