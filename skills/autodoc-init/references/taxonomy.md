# Autodoc Entity Taxonomy & Frontmatter Schema

This is the canonical contract for the **autodoc** code wiki. Both `/autodoc-init`
and `/autodoc-update` — and every workflow agent they dispatch — MUST follow it exactly.
It is the code-domain analogue of the personal wiki's `references/frontmatter.md`.

The autodoc mirrors the wiki's structure (folders-per-type, `_index.md` sub-indexes,
master `index.md`, `overview.md`, `log.md`, `hot.md`, wikilinks everywhere) but the
entities are **code artifacts**, not articles and people.

---

## The 12 Entity Types

| Type | Folder | What it is | Smell test |
|------|--------|------------|------------|
| `endpoint` | `endpoints/` | One HTTP/WS route | `@app.get`, `@router.post`, FastAPI/Flask route, an API service's HTTP handler, internal API |
| `page` | `pages/` | One frontend route/view | A routed React view (hash route), a full screen the user navigates to |
| `component` | `components/` | A reusable UI or functional unit | React component, hook, shared util/helper module, a class used across the codebase |
| `service` | `services/` | A deployable app/daemon/library | A top-level package that runs or is imported as a unit (an API backend, an orchestrator, a runtime, a retrieval layer, a sync daemon) |
| `process` | `processes/` | A named multi-step internal workflow | ticket→PR pipeline, validation loop, decomposition, promotion pipeline, polishing reset |
| `vendor` | `vendor/` | An external or vendored dependency | `vendor/` code, a pinned third-party dep, an external model or service the product calls |
| `runbook` | `runbooks/` | An operational procedure | deploy, restart, rotate-secret, recover, scan — the "how to operate it" docs |
| `data-model` | `data-models/` | A schema, type, or table | Pydantic model, TS interface/type, DB table, document schema, a database's table shape |
| `decision` | `decisions/` | An Architecture Decision Record | `docs/adr/ADR-NNN`, a locked design decision with rationale |
| `flow` | `flows/` | A cross-cutting narrative | request path, data flow, auth flow, event flow — synthesized, spans many services |
| `job` | `jobs/` | Scheduled/triggered background work | cron, webhook handler, watchdog, queue worker, Task Scheduler job |
| `tour` | `tours/` | A dependency-ordered learning sequence over existing pages (onboarding reading path) | "where do I start to understand subsystem X" |

**Flow vs. tour:** a `flow` is *what happens* (runtime narrative — the path a request/event takes); a `tour` is *how to learn it* (a dependency-ordered reading sequence over existing pages, foundational pages first).

**Routing rules when an artifact could be two things:**

- A FastAPI route handler is an `endpoint`. The function it lives in is NOT also a component.
- A React file that is a routed screen → `page`. A React file imported by pages → `component`.
- A top-level runnable package → `service`. A class/module inside it → `component` or `data-model`, not another service.
- A webhook *handler* is a `job` (it's triggered work); the *route* it's mounted at is also an `endpoint` — create both and cross-link them.
- Vendored code: create ONE `vendor` page per dependency. Do NOT deep-document a vendor's internals as components/services.
- A `flow` is never a single file — it's a synthesized story connecting endpoints, services, processes, and jobs. Only the synthesis stage creates flows.

When genuinely ambiguous, pick the type that best answers "what would a new engineer search for?" and cross-link to the alternative.

---

## Naming Convention (page titles = filenames)

Filenames are unique across the whole autodoc (Obsidian wikilinks resolve by stem). Use:

- **endpoint**: `METHOD route` → `POST api-projects.md`, title `POST /api/projects`, and REQUIRED `aliases: ["POST /api/projects"]` so slash-form wikilinks resolve (see endpoint frontmatter)
- **page**: route or screen name → `Projects Page.md`
- **component**: component/module name → `ApprovalRelay.md`, `useStreaming.md`
- **service**: human name → `API Backend.md`, `Orchestrator.md`, `App Runtime.md`
- **process**: the workflow name → `Export Pipeline.md`
- **vendor**: the dependency name → `claude-agent-sdk.md`, `redis-py.md`
- **runbook**: the operation → `Deploy API.md`
- **data-model**: the schema name → `Note.md`
- **decision**: the ADR id + slug → `ADR-044 Engine.md`
- **flow**: the narrative → `Nightly Export to Archive.md`
- **job**: the trigger → `Nightly Export.md`
- **tour**: the subsystem (title-case) → `tours/<Subsystem> Tour.md`, e.g. `Orchestrator Tour.md`

If two real entities would collide, disambiguate with the owning service: `Config (shared).md`.

**Sanitized titles require an alias:** filenames cannot carry `: / \ # | ^ [ ]` — when a title
contains one (e.g. `Sharing: Note to Link`), the filename drops it and the page MUST add
`aliases: ["<full title>"]` so prose links using the real title still resolve. A stripped
character with no alias is a guaranteed dead link.

---

## Universal Frontmatter (every autodoc page)

```yaml
---
type: <endpoint|page|component|service|process|vendor|runbook|data-model|decision|flow|job|tour>
title: "Human-Readable Title"
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_repo: "owner/repo"
project: <name>           # RENDERER-OWNED. The project this corpus documents, taken
                          # from the corpus row ("the room points at a project
                          # codebase"). Never write it into a record's `frontmatter`:
                          # the renderer stamps it, and a record that carries it is
                          # ignored. Every page needs one because every page is filed
                          # into that project's vault pillar.
source_paths:
  - "relative/path/to/file.py"
status: active            # active | deprecated | experimental | planned | legacy
tags:
  - <type>
  - <community-tag>       # the derived community label (e.g. api, ingest, frontend).
                          # Labels are charset-constrained to [A-Za-z0-9 _.-] at the
                          # source (deterministic, pin, and LLM naming paths all
                          # sanitize) — they flow into agent prompts, so a hostile
                          # repo must not smuggle markup/control chars through them.
related:
  - "[[Other Page]]"
diagram: false            # true when a validated Mermaid diagram is embedded (flow/data-model only)
diagram_source_sha: ""    # source sha the embedded diagram was derived from (staleness tracking)
---
```

Rules (same as wiki frontmatter): flat YAML only, no nested objects; dates as `YYYY-MM-DD`;
lists as `- item`; wikilinks in YAML quoted (`"[[X]]"`); bump `updated` on every edit;
`source_paths` is the load-bearing field for `/autodoc-update` — it maps changed code files
back to the pages that document them, so keep it accurate and complete. `diagram`/`diagram_source_sha` are OPTIONAL and only set on flow/data-model pages that embed a diagram — `diagram_source_sha` is the load-bearing field for diagram staleness detection (compared against the current source sha by `/autodoc-update`).

`status: legacy` marks a **mothballed-but-present** subsystem: the code is still in the repo but
the path is not live (entrypoint unmounted, client stubbed, superseded by a newer pipeline).
Any page or tour documenting a legacy subsystem MUST open with a status callout saying so, e.g.
`> [!warning] Legacy subsystem — code present in the repo, but this path is not currently live.`
A reader must never spend twenty pages learning a dormant system before finding that out.

**Liveness protocol (how `status:` is determined — never assumed):** a page describes an entity
in the present tense only after its activation chain is located in source: a router is mounted
(`include_router`/`app.mount` found), a component has a live importer, a daemon's entrypoint is
wired with its real dependencies, a job's scheduler/trigger registration exists. What the
verification finds goes on the page: an entity mounted **only in a legacy/compat mode** says so
in its opening lines; an entity with **no production caller** (barrel-exported dead code, an
unmounted route family, a stub-wired path) states that explicitly rather than describing
intended behavior as running behavior. Retired subsystems are described in past tense everywhere
they appear — including `_index.md` entries, `overview.md`, `Start Here.md`, and tours.

### `contract_with:` — explicit contract pairs (OPTIONAL)

A page documenting **one side** of a cross-service / cross-process contract (producer ↔ consumer,
client ↔ server) MAY carry an optional `contract_with:` list of quoted `"[[wikilinks]]"` naming
the page(s) that document the **other side**. The pairing is symmetric — each side lists the other.

```yaml
contract_with:
  - "[[Push Delta]]"
  - "[[Sync Daemon]]"
```

Semantics: when **either** side's source changes, **both** pages must be re-verified together
against both codebases' actual shapes — `/autodoc-update` fans affected pages out across
`contract_with` in both directions. A mismatch between the two sides is documented explicitly
on BOTH pages, never papered over by describing the intended integration as if it worked.

Example — the client-to-API sync: `endpoints/POST sync-push.md` and
`data-models/Push Payload.md` (the API/server side) each carry
`contract_with: ["[[Push Delta]]", "[[Sync Daemon]]"]`, while `data-models/Push Delta.md` and
`services/Sync Daemon.md` (the daemon/client side) each carry
`contract_with: ["[[POST sync-push]]", "[[Push Payload]]"]`. A change to `sync_daemon/pusher.py`
re-verifies all four pages, and the shape drift (`delta` missing, `note_id`/`edited_at` vs
`id`/`updated_at`) stays documented on both sides until one of them changes.

`audited: YYYY-MM-DD` is a second optional universal field, stamped by `/autodoc-update`'s
rolling audit pass on every page it re-verifies (whether or not anything changed). Unlike
`updated`, it does NOT mean the page's content moved — it means the page's claims were checked
against source on that date. Don't set it by hand.

---

## Evidence Protocols (the claim types most often gotten wrong)

Every writing/refreshing/auditing agent follows these. Each states what a finished claim IS —
a claim without its evidence step isn't done yet.

- **Consumer claims** (`used_by:`, `consumed_by:`, "X is used by Y" in Connections): each entry
  is backed by a **located import or call site** in the consumer's source. The relation is
  written from that hit, never from name similarity or architectural plausibility. Direction
  matters: `used_by` lists things that import/call THIS page's subject.
- **Write-path claims**: a module documented as writing/mutating names the **exact exported
  function and its file**, transcribed from the module's real export list. A pure-read module is
  documented as read-only — it never gains a write capability, a seeding behavior, or a
  mutation warning it doesn't have.
- **Per-file anchoring (multi-file pages)**: each claim is attributed to the file it comes
  from; before finishing a page, re-open every `source_paths` entry and confirm one
  distinguishing symbol from the page's text actually lives in that file. This is the check
  that catches two sibling files documented with each other's contents.
- **Route-family completeness**: documenting any route from a module means enumerating **every
  route defined in that module** (the coverage checklist lists them) — siblings are either
  documented in the same family or given their own page, never silently dropped.
- **Dates**: only dates that appear in source (a doc's own date line, a commit the page cites)
  are recorded. An undated ADR gets `date: ""` and "undated" in the body — never a date
  inferred from run date or repo activity.
- **source_paths completeness**: every file a page derives a load-bearing claim from appears in
  `source_paths`. It is the update reverse-index — a claim sourced from an unlisted file will
  never be re-verified when that file changes.

---

## Type-Specific Frontmatter (append after universal fields)

### endpoint
```yaml
method: POST              # GET|POST|PUT|PATCH|DELETE|WS — or `multiple` for a route family
route: "/api/projects"
aliases:                  # REQUIRED on every endpoint page
  - "POST /api/projects"  # the slash-form title, so [[POST /api/projects]] resolves
service: "[[API]]"
auth: required           # required | optional | none | internal-secret
handler: "module.path:function"
request_model: "[[Project Create Request]]"   # link to a data-model, or ""
response_model: "[[Project]]"
```

`aliases` is REQUIRED: endpoint filenames are dash-style (`POST api-projects.md`) but prose
links naturally use the slash-form title, and Obsidian only resolves those through the alias.
Every endpoint page carries `aliases: ["<METHOD> <route>"]` — **one alias per route**: on a
route family, every entry in `routes:` has its slash-form alias; a routes/aliases count
mismatch is a defect.

**Aliases are the wiki's global resolution keys, so they MUST be globally unique.** When two
endpoints in **different services** legitimately expose the same `METHOD /route` (e.g. the API
and the orchestrator both serve `GET /metrics`), a bare `[[GET /metrics]]` link is ambiguous —
so **both** pages carry the service-disambiguated alias `"<METHOD> <route> (<service>)"`
(`"GET /metrics (api)"` and `"GET /metrics (orchestrator)"`) and **neither** keeps the
bare form. The disambiguation is symmetric — there is no "primary" page that keeps the bare
alias. Prose then links the disambiguated form. This is the alias-level generalization of the
filename-collision rule above ("disambiguate with the owning service"). The `routes:` entry
itself stays the bare real path (`"GET /metrics"`) — it is the actual route, not a link key;
only the **alias** carries the `(service)` suffix. `graph-check`'s `aliasCollision` check warns
on any duplicate alias string across pages.

`auth:` describes what an **anonymous caller experiences** on the route — a handler that
requires and verifies its own bearer is `required`, regardless of middleware wiring. For a
route family, `auth:` is the **strictest member's** value, with per-route exceptions (e.g.
unauthenticated health probes) listed in the body contract.

**Route families:** when several routes form ONE contract family (same resource, same auth, one
handler module — e.g. login + callback + logout + me), a single endpoint page MAY cover them:
set `method: multiple`, replace `route:` with a `routes:` list of `"METHOD /path"` entries, and
put every slash form in `aliases`. Never pack sibling routes as free text into a single `route:`
value — machine consumers of `method`/`route` must never get garbage.

### page
```yaml
route: "#/projects"
framework: react
renders:
  - "[[ProjectList]]"
calls_endpoints:
  - "[[GET /api/projects]]"
```

### component
```yaml
component_kind: ui       # ui | functional | hook | util | class
path: "frontend/src/components/ProjectList.tsx"
language: tsx
used_by:
  - "[[Projects Page]]"
```

### service
```yaml
service_kind: api        # api | daemon | worker | cli | library
path: "api/"
language: python
entrypoint: "api/app.py"
port: "8000"             # "" if not a network service
exposes_endpoints:
  - "[[POST /api/projects]]"
depends_on:
  - "[[Redis]]"
  - "[[Claude Agent SDK]]"
```

### process
```yaml
trigger: "The Nightly Export job fires"
involves:
  - "[[Export Worker]]"
  - "[[Storage Backend]]"
produces: "A dated archive of every workspace's notes"
```

### vendor
```yaml
vendor_kind: vendored    # vendored | package | external-service | model
version: "v1.0.0 @ 67c821c"
upstream: "https://github.com/..."
used_by:
  - "[[App Runtime]]"
risk: "Pinned; weekly Monday upstream sync"
```

### runbook
```yaml
operation: deploy        # deploy | restart | rotate | recover | scan | migrate
applies_to:
  - "[[API]]"
trigger: "After merging to release branch"
```

### data-model
```yaml
model_kind: schema       # schema | type | table | document | enum
path: "api/models/note.py"
language: python
owned_by: "[[API]]"
consumed_by:
  - "[[Sync Daemon]]"
```

### decision
```yaml
adr_id: "ADR-044"
decision_status: accepted   # proposed | accepted | superseded | deprecated
date: YYYY-MM-DD            # ONLY if the source doc records it; undated doc => "" (never infer)
supersedes: ""              # "[[ADR-040 ...]]" or ""
affects:
  - "[[App Engine]]"
```

### flow
```yaml
flow_kind: pipeline      # request | data | auth | pipeline | event
entrypoint: "[[Nightly Export]]"
touches:
  - "[[Export Worker]]"
  - "[[Storage Backend]]"
  - "[[API]]"
use_cases:               # OPTIONAL — flat list of OPAQUE UC-ID scalars, NOT wikilinks
  - "UC-0147"            # opaque id; carries no domain meaning; never "[[ ]]"
  - "UC-0203"
```

> [!note] `use_cases` is an **opaque-scalar list, gate-inert.** Entries are bare `UC-NNNN`
> strings (never `"[[wikilinks]]"`), so `graph-check.js` — which only validates the enumerated
> relational-link fields — never sees them and never resolves them. **Do NOT add `use_cases` to
> the graph-check relational-link set** (see Cross-Linking Discipline). Registry-existence
> checking (does `UC-0147` exist in the project's `design/use-cases/registry.jsonl`?) is deliberately
> deferred to **Phase-5 enrichment** — for now the field is inert content hosted on the flow
> page, populated by `/autodoc-update` from the `Use-Case:` commit trailers it ingests. The
> per-UC `Status` in the `## Use Cases` body table (`backlog|in-progress|completed|deprecated`)
> is **distinct from** the page-level `status:` frontmatter and tracks the use case's own
> lifecycle, not the page's.

### job
```yaml
job_kind: cron           # cron | webhook | worker | scheduled-task
schedule: "0 2 * * *"    # cron expr OR trigger event
runs: "[[Export Pipeline]]"
handler: "worker/export.py:run"
```

### tour
```yaml
tour_kind: subsystem     # subsystem | cross-cutting
steps:                   # ordered reading sequence, dependencies first
  - "[[Redis]]"
  - "[[Nightly Export]]"
  - "[[Export Pipeline]]"
  - "[[Export Worker]]"
covers:                  # the community(ies)/subsystem the tour teaches
  - "[[export]]"
```

**Tour pedagogy rules:**

- The intro is **at most 5 sentences** and MUST include an audience line ("who this tour is
  for"). No epic-enumerating run-ons — epic/ticket IDs belong on the entity pages.
- Step bodies teach why the page matters and where it sits in the subsystem — **never
  update-changelog prose** ("since epic N it also…" currency notes go on the entity pages, not
  in tour steps).
- **Pipeline tours order steps trigger-first** (narrative order: entry trigger → … → end
  artifact), overriding the pure topo sort. State the override explicitly in the tour intro so
  the ordering is honest, not a fabricated "builds on" chain.
- Every tour includes its subsystem's **hub pages** (the covering process/flow/page docs its
  steps keep pointing at), not just leaf components — a reader needs the orienting pages, early.
- A tour over a `status: legacy` subsystem MUST open with the legacy status callout (see the
  universal frontmatter rules).

---

## First-Class Meta Pages (maintained, not generated-once)

Beyond the 12 entity types, the autodoc root carries two **maintained** meta pages. Both are
first-class deliverables: init creates them, every update keeps them current.

- **`Start Here.md`** — plain-language product + state orientation for non-engineers: what the
  product does, for whom, how a request becomes shipped software in a few plain steps, and what
  state it's in (live vs. legacy vs. planned). No unglossed coinages; minimal wikilinks.
- **`Glossary.md`** — the invented domain vocabulary (workspace, note, share link, …): one plain
  sentence per term, linking to the defining page. **Any NEW invented term introduced by an
  update MUST land here in the same run** — no coinage ships undefined. Coined terms used
  load-bearingly in `overview.md` or service pages (runtime primitives, persona names) count.

**Nav freshness:** whenever a run (re)generates the nav layer, it stamps `updated:` with the
actual run date on `index.md`, `overview.md`, `Start Here.md`, and `Glossary.md` — nav pages
carrying a date older than the corpus they index misreport the regen. `Start Here.md`'s
live/legacy state list is refreshed in any run that changed a page's `status:`.

---

## Page Body Structure (all types)

Keep pages 60–250 lines. Body sections, in order:

1. **One-line summary** (what it is, in plain English).
2. **Source** — backtick-quoted `source_paths`, clickable.
3. **What it does** — 2–6 sentences. The *why*, not a line-by-line restatement of code.
4. **Key behavior / contract** — inputs, outputs, side effects, auth, error handling. Type-appropriate.
5. **Connections** — wikilinks to what it calls, what calls it, schemas it uses, ADRs that govern it.
6. **Gotchas** — non-obvious constraints, known quirks, race conditions (only if real).

Do NOT paste large code blocks. Link to `source_paths` instead. Document intent and contract;
the code is the source of truth for implementation detail.

### Diagram section (flow and data-model pages only)

`flow` and `data-model` pages carry ONE embedded, validated Mermaid diagram as a
`## Diagram` section placed immediately after the one-line summary (before "Source").

- **flow** → `sequenceDiagram` (service/actor interactions in narrative order) or
  `flowchart` (branching pipelines). One diagram, the flow's single narrative.
- **data-model** → `erDiagram` (the schema and its relationships to sibling models).

Rules:
- Stay at Context+Container abstraction: name services/models and their relationships,
  not internal fields beyond the load-bearing ones. ≤ 20 elements; split or simplify if over.
- **One diagram = one connected story.** Never pack disconnected concept clusters into a
  single diagram; if the page's models don't relate, diagram the load-bearing cluster only.
  Test-DI scaffolding entities (empty-attribute interface types) are noise — omit them.
- Every node naming a code entity carries a `%% source: <repo-relative-path>` comment
  (grounding contract — never a fabricated node). Grounding rules:
  - the path is **repo-relative exactly as in `source_paths`** (no repo-name
    prefix) and must exist in the clone — verify with `test -f` before embedding;
  - every `%%` comment sits **inside** the mermaid fence — a comment after the closing
    ` ``` ` renders as junk text on the page;
  - entity fields are transcribed from the schema's real field names — a fabricated field on a
    diagram node is the same defect as a fabricated claim in prose.
- A diagram belongs to exactly ONE page. Sibling models sharing a relationship get one page
  with the diagram and a wikilink from the other — never the same diagram pasted twice.
- The diagram MUST pass `validate_mermaid.py` before embedding (see the Diagram Gate below).
- Set `diagram: true` and `diagram_source_sha: "<current source sha>"` in frontmatter when
  a diagram is embedded. Pages without a diagram omit both fields (or `diagram: false`).

Other page types (endpoint, service, component, …) do NOT get diagrams in this system.

### Use Cases section (flow pages only)

`flow` pages that host use cases carry a `## Use Cases` body section placed **immediately after
"What it does"** (section 3). It is a table with exactly these columns:

```markdown
## Use Cases

| UC | Domain | Status | Summary |
|----|--------|--------|---------|
| UC-0147 | auth/login | completed | A user exchanges GitHub OAuth for a session token. |
| UC-0203 | auth/login | in-progress | Session refresh on token expiry. |
```

- **UC** — the opaque `UC-NNNN` id, matching an entry in the page's `use_cases:` frontmatter
  scalar list. Bare text, never a wikilink.
- **Domain** — the human-gated domain path from `design/use-cases/domains.md` (e.g. `auth/login`).
- **Status** — the per-UC lifecycle enum `backlog | in-progress | completed | deprecated`,
  **distinct from** the page-level `status:` field. Pre-ship values (`backlog`/`in-progress`) are
  plan-derived; post-ship values (`completed`/`deprecated`) are reconciled by `/autodoc-update`
  against code via the liveness protocol. A UC whose activation chain is gone → `deprecated` (with
  a stale banner), never silently dropped.
- **Summary** — one plain-English sentence of what the use case does.

The table rows and the `use_cases:` frontmatter list stay in sync (every scalar id has a row,
every row's id appears in frontmatter). This section is content, not a relational-link field —
see the `use_cases` note under the flow frontmatter above.

---

## Cross-Linking Discipline

The value of the autodoc is the graph. Every page should link to at least 2 others. Specifically:
- endpoints ↔ the service that exposes them, the data-models they use, the flows they start
- components ↔ the pages/components that use them
- services ↔ their endpoints, dependencies (services + vendors), data-models, governing ADRs
- processes/flows ↔ every service, endpoint, and job they touch
- decisions ↔ the services/processes they govern

Unresolved `[[Wikilinks]]` in **body prose** are advisory — they mark a gap to fill, exactly
like in the wiki, and `/autodoc-update` and `/wiki-lint`-style passes resolve them later.

**Frontmatter relational links are NOT advisory.** `calls_endpoints`, `renders`, `used_by`,
`consumed_by`, `involves`, `exposes_endpoints`, `runs`, `request_model`, `response_model`,
`steps`, `covers`, `contract_with`, and `related` are machine-consumed (update fan-out, tours,
graph checks) — every entry must resolve to an existing page by stem or alias. Endpoint links in
frontmatter use the **slash-form alias** (`"[[GET /v1/notes]]"`), never an invented dash-form
stem (`[[GET v1-notes]]` resolves to nothing). A page never wikilinks itself in its own
relational fields.

**Flow backlinks:** when a flow page is created or its touched set changes, every entrypoint
and hub page the flow touches gets the flow added to its `related:` list — a reader landing on
`POST sync-push` must be able to find the flow that owns it. Hub→flow backlinks are part of
writing the flow, not an optional courtesy.

---

## Coverage (tree-sitter pre-pass)

Before any map agent runs, a deterministic tree-sitter pre-pass enumerates every
endpoint/function/class/method/type/component in the source as ground truth. The CLI
(`references/inventory/extract-inventory.js`) parses Python/Go/TS/TSX and writes a
gitignored `.source/.inventory.json` (`{repo, sha, counts, symbols}`), where each
`Symbol = {file, lang, kind, name, line}`. Grouping into fan-out units is by **derived
community**: the graph chain (`emit-graph.js` -> `graph build` ->
`graph communities`) partitions the symbol graph and emits `.source/.communities.json`
with per-community symbol checklists.

The inventory makes completeness provable, two ways:

- **File-level:** every inventoried symbol's `file` must appear in some page's
  `source_paths` frontmatter — keep `source_paths` accurate, it is now cross-checked.
- **Symbol-level:** each map agent receives its community's symbols as a checklist and MUST
  return every one in either `covered` (documented on a page, as `{community, name, line}`) or
  `excluded` (intentionally skipped, as `{community, name, line, reason}` — e.g. trivial helper,
  private impl detail, re-export). Nothing may be silently dropped.

The coverage gate (`references/inventory/coverage.js`) fails the run on any unexplained
symbol. A FastAPI/Flask decorated route is inventoried as `kind: endpoint` (not also a
`function`), matching the endpoint-supersedes-function routing rule above.

---

## Graph integrity (graph-reviewer)

After the pages exist, a deterministic graph-reviewer
(`references/inventory/graph-check.js`) validates the generated wiki against ground truth.
It runs in the **skill main-loop** (via Bash), not inside a Workflow — the Workflow sandbox
has no filesystem/Node access. It reports five dimensions:

- **broken wikilinks** — a `[[Target]]` whose stem, title or alias matches no page. No
  candidates are recorded at check time: a case-insensitive match resolves, so it is never broken.
  `--fix` finds a single near-match (edit distance 1 on the normalized name) itself;
- **orphans** — non-nav pages with zero inbound wikilinks;
- **missing frontmatter** — pages missing any universal required field;
- **non-existent `source_paths`** — a `source_paths` entry not found under the repo clone;
- **symbol coverage** — file-level gaps against the tree-sitter `.inventory.json`.

**Safe auto-fix scope** (`--fix`): six repairs, each made only when exactly one answer exists:

- a broken wikilink, in the body or the frontmatter, with exactly ONE near-match candidate
  (stem, title or alias) is rewritten to that page (alias preserved);
- missing `status`/`updated` frontmatter defaults are filled;
- an unresolved frontmatter endpoint link written in another separator form
  (`[[GET v1-notes]]`) that matches exactly ONE endpoint page is rewritten to that page's alias
  (its title when no alias matches);
- a one-way `contract_with` pair gets the reciprocal entry on the side missing it;
- on a route-family endpoint page (`method: multiple`), a `routes:` entry with no matching alias
  gets its slash-form alias added;
- a sanitized title with no alias gets the full title added as an alias.

It NEVER auto-generates pages, NEVER guesses ambiguous (>1 candidate) links, and never
touches source-of-truth code.

**Blocker gate:** graph-check's `classify()` sorts every finding into a blocker or a warning.
It emits five blocker kinds:

- **unexplained symbol-coverage gaps** (`coverageGap`);
- **non-existent `source_paths`** (`missingSourcePath`): a page citing a file that isn't in the
  repo;
- **an endpoint page's unresolved `request_model`/`response_model` link** (`brokenModelLink`);
- **an unresolved frontmatter relational link** (`frontmatterLinkUnresolved`);
- **a diagram `%% source:` path that does not exist in the clone** (`diagramSourceMissing`).

Neither gate runs `--fix` first, and they differ on fixable findings:

- `/autodoc-init` Step 5a runs graph-check over the rendered tree with no flag. A blocker exits 1;
  fixable findings are not computed, and the ones that are not blockers stay warnings. The block
  saves the exit status in `GRAPH_RC` and does not exit itself: a non-zero `GRAPH_RC` means fix
  the records and re-render.
- `/autodoc-update` Step 4.5 runs it with `--fail-on-fixable`, which exits 1 on any blocker or
  any fixable finding, so both are fatal there.

Other broken wikilinks are **ADVISORY** gap-markers: never a blocker. On Step 5a they are
reported as warnings. On Step 4.5 one that `--fix` could rewrite is a fixable finding, and so
fatal. The model-link blocker means the endpoint's contract is undocumented: if the data-model
page doesn't exist, create it or inline the shape on the endpoint page. Orphans, and any other
warning that `--fix` cannot repair, are reported and not fatal on either gate.

**The update path is stricter.** `/autodoc-update` Step 4.5 runs graph-check over
`$RENDER_TARGET` with `--fail-on-fixable` and no `--fix` (the two are mutually exclusive). That
mode computes what `--fix` would repair, writes nothing, records each finding in the report's
`fixable` list, names its page on stderr, and exits 1: there, a fixable finding is **fatal**, the
same as a blocker. The reason is store-primacy: a repair written into a rendered page is dropped
by the next render, and the next run's golden gate then blames an agent for the lost link. So a
fixable finding on the update path is repaired in the page's record and re-rendered, never
applied to the page.

---

## Diagram Gate (flow + data-model diagrams)

Every embedded Mermaid diagram is deterministically validated before it lands, mirroring
the coverage/graph gates:

- **Syntax gate:** the diagram source MUST pass
  `python <suite_root>/scripts/validate_mermaid.py <draft.mmd>` (exit 0), where `<suite_root>`
  is `environment.suite_root` — the same `validateMermaidCmd` the init workflow threads in. On failure,
  the generating agent fixes it from the validator's own error output (max 2 attempts),
  then omits the diagram with a `> [!warning] diagram omitted: <reason>` callout rather than
  embedding broken source.
- **Staleness (currency):** `/autodoc-update` walks the source clone's git history for each
  diagrammed page: the diagram is stale when some commit after its `diagram_source_sha`, up to
  the manifest's `last_sha`, touched any of its `source_paths` (the newest commit touching them is
  not already an ancestor of the commit the diagram was drawn at). An unrelated commit leaves it
  fresh. A `diagram_source_sha` that is not a sha, or resolves to no commit in the clone, reads
  stale; a page with no `source_paths`, or whose paths exist nowhere in that history, is
  reported UNPROVEN and counted fresh. Only when the manifest has no `source_path` or
  `last_sha`, or the clone is missing, does it take a labelled FALLBACK that compares the sha
  against per-page `source_sha` heads or `last_sha` directly. A stale verdict FLAGS the diagram
  in the staleness report — it does NOT silently regenerate. Default is
  flag-don't-regenerate; regeneration is an explicit, budgeted follow-up action. The report
  (`references/inventory/diagram-staleness.js`) carries two classes: **STALE**, a diagrammed page
  whose sources changed after its `diagram_source_sha`, and **MISSING**, a `type: flow` or
  `type: data-model` page whose `diagram` frontmatter is not `true`, so it never had a diagram to
  go stale. Its budget estimate is `(stale + missing) * 8000` tokens, one agent pass per diagram
  drawn or redrawn. Both classes are advisory: the script exits 0 on them, and exits non-zero
  only on an error (a failed git call, a `last_sha` that resolves to no commit), because an error
  is not a verdict.
