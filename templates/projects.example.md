# projects/<name>.md — per-product config (TEMPLATE)

This is a **template** for one product. The installer copies it to
`~/.agentflow/config/projects/<name>.md` and fills the values from an interview. One file per
product; the **filename stem == `name` == the routing key** (the backlog/project skills
enumerate `projects/*.md` at runtime, so no project list is hardcoded anywhere).

The typed schema and every validation rule are frozen in `skills/_shared/SCHEMA.md` §2. The
values below are illustrative placeholders for a deployed web product; strip the blocks that
don't apply (a CLI or library project omits `staging` and sets `verification.strategy` to
`test-suite`/`smoke-cli`).

```yaml
schema_version: 1
name: "example"               # R — MUST equal the filename stem (routing integrity)

canonical_repo:
  host_ref: "acme/app"        # R — "owner/repo" on environment.git_host
  local_checkout: "~/CODE/app"  # R — local git worktree; origin must resolve
  base_branch: "main"         # O (default "main") — the branch a merge deploys from
  python: "~/CODE/app/.venv/bin/python"  # O — the PRODUCT's interpreter. The ambient `python` in a fleet
                              #     session is the agentflow suite venv (PyYAML only); a product suite run with it
                              #     imports nothing and can go green over a broken app. Set this for any Python product.

deprecated_mirror: null       # O — legacy mirror ref to flag + re-resolve away from
deprecated_mirror_checkout: null  # O — legacy clone path to re-ground away from

# publish_repo:               # O — the PUBLIC repo this project is published to. LEAVE THIS OUT unless a
#   host_ref: "acme/app"      #     public repo actually exists: absent means nothing is published, which is
#   cadence: "manual, en masse"  #  the normal case. READ-ONLY to every builder — the rule is stated in
                              #     SCHEMA.md §2 and nowhere else, so it cannot drift. Not deprecated_mirror,
                              #     which means the opposite ("frozen legacy, ignore it").

staging:                      # O — omit for non-deployed (CLI/library) projects
  url: "https://staging.example.com"  # R* iff verification.strategy == web-journey
  deploy_host_ssh: "staging-box"      # O — ssh alias of the box a merge rebuilds
  checkout_path: "~/app-staging"      # O — staging-host checkout for the drift guard
  deploy_recipe: |            # O — Configured procedure (verbatim) OR a Delegated SKILL.md path
    Merging to base_branch fires the deploy CI workflow, which rebuilds staging and wipes any
    uncommitted host patches. Before merging, check the staging checkout for live drift
    (ssh deploy_host_ssh, `git -C <checkout_path> status --porcelain`) and never merge over it.

verification:
  strategy: web-journey       # R — web-journey | test-suite | smoke-cli | none | <SKILL.md path>
  url: "https://staging.example.com"  # binds to staging.url under web-journey
  roles:                      # O — personas for role-based web journeys (web-journey only)
    - "new signup"
    - "returning user"
  test_login: "Sign in with the seeded non-SSO test account before any journey."  # O

domains:                      # O — build-order bands
  - core-engine
  - enhance-engine
  - modules
  - prod-deploy
  - infra-meta
  - bugfix

aliases:                      # O — routing cue words that map free text to this project
  - "example-app"
  - "the app"

harness_notes: null           # O — project-specific grounding kept verbatim; null for most projects

excluded_domains: []          # O — epic domain/project values the fleet never self-selects (e.g. a sibling
                              #     program you build by hand). One epic can opt back in with frontmatter
                              #     `build_approved_by: "<name> <YYYY-MM-DD>"` — a per-epic grant, not a policy change.

work_source:
  kind: github-issues         # O (default filesystem) — github-issues | gitea | filesystem | none
  bug_triage: true            # O (default false) — is open-bug work first-class here
  # open_issue_page: 200      # O (default 200) — open issues one intake read answers for; raise when more are open

generation_skills:            # O — Delegated skills that CREATE work (lead-dev only)
  polishing: null             # O — a bug-finding/polishing skill path
  provision: null             # O — a tenant/seed provisioning skill path

audit:                        # O — the auditor loop's per-project dial; omit entirely to accept all defaults
  window: 7                   # O (default 7) — last-N done epics considered per audit pass (5-10; below 1 hard-fails)
  roles: []                   # O (default []) — Playwright personas for web-journey checks; [] falls back to verification.roles
  idle_wake_minutes: 60       # O (default 60) — long-wake backoff when there is nothing new to audit
  notify: true                # O (default true) — send a one-line ASCII pass summary via the notify endpoint
```

## Notes (human-only, ignored by the resolver)

- **`name` must equal the filename stem** — the resolver hard-fails otherwise. This equality
  is the routing key.
- **This project's work files live under one derived subtree:**
  `<data_root>/projects/<name>/{design/{specs,plans,use-cases,suggestions},status,wiki/pages{,/done,/archived}}/` (refine docs, solution
  plans, status boards + the Fleet Registry + per-agent state files, and use-case docs
  respectively; the `queue/` directory is retired). The path is
  derived from `environment.data_root` + this `name`; the installer scaffolds it. See
  `docs/DATA-LAYOUT.md` and `skills/_shared/SCHEMA.md` §1.
- **`staging.url` is required iff `verification.strategy == web-journey`.** For CLI/library
  products set `strategy` to `test-suite` or `smoke-cli` and omit `staging` entirely —
  Playwright is never forced onto non-web projects.
- **`publish_repo` is commented out above because absent is the right answer for almost every
  project.** Set it only once a separate PUBLIC repo exists; `canonical_repo` stays the repo the
  fleet works in either way. It is read-only to every builder (SCHEMA.md §2 states the rule; this
  is a pointer, not a second copy), and publishing is done by a human following
  `docs/runbooks/publish.md`. If the public
  repo is to take the name the working repo currently holds, read that runbook's step (2) first:
  the working repo has to be renamed before the name is free.
- **`harness_notes`** carries verbatim project grounding (e.g. "outputs are written by X and
  Y, NOT Z"). Keep it empty unless a project has a real footgun to record.
- **Delegated paths** (`staging.deploy_recipe` when a path, `generation_skills.*`,
  `verification.strategy` when a path) are presence-checked at resolve
  time; a missing optional one degrades its step to the Forced "skip silently" behavior.
- **`audit`** is the auditor loop's per-project dial and is entirely optional — omit the block
  to accept every default (`window=7`, `roles=[]`, `idle_wake_minutes=60`, `notify=true`).
  Empty `roles` means the auditor reuses `verification.roles` for its web-journey checks. The
  block is purely additive: projects written before the auditor existed need no change.
