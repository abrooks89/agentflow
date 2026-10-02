# Autodoc Scaffold

The structure `/autodoc-init` creates under the configured `autodoc_root`
(`<autodoc-dir>/`, where `<autodoc-dir>` is the basename of the root `resolve_autodoc_root`
returns for the project).
This is a **code documentation wiki** — structured pages, one per code entity. When
`autodoc_root` sits inside an Obsidian vault its pages join that graph and can wikilink to
neighbouring notes, but the vault is not required.

Substitute `<autodoc-dir>` (the `autodoc_root` basename) and `<repo>` (the project's
`canonical_repo.host_ref`) below; when they match a pre-existing code wiki's folder and repo, the
generated output is unchanged.

```
<autodoc-dir>/
├── CLAUDE.md            # autodoc instructions (template below)
├── index.md            # master catalog of all pages  (built by workflow Index phase)
├── overview.md         # executive architecture summary, MUST contain one mermaid architecture diagram (built by workflow Index phase)
├── Start Here.md       # plain-language product + state orientation for non-engineers (built by workflow Index phase, maintained by update)
├── Glossary.md         # the invented domain vocabulary, one plain sentence per term (built by workflow Index phase, maintained by update)
├── log.md              # chronological record of init + every update
├── hot.md              # recent-context cache — hard cap 400 words total, Latest paragraph ≤120 words
├── .manifest.json      # delta tracking: source repo, last_sha, source_paths → pages reverse-index
├── .gitignore          # ignores .source/
├── .source/            # gitignored shallow clone of the source repo (NOT committed)
├── endpoints/          # HTTP/WS routes            + _index.md
├── pages/              # frontend routes/views     + _index.md
├── components/         # reusable UI/functional units + _index.md
├── services/           # deployable apps/daemons   + _index.md
├── processes/          # named multi-step workflows + _index.md
├── vendor/             # external/vendored deps    + _index.md
├── runbooks/           # operational procedures    + _index.md
├── data-models/        # schemas, types, tables    + _index.md
├── decisions/          # ADRs                      + _index.md
├── flows/              # cross-cutting narratives  + _index.md
├── jobs/               # crons, webhooks, workers  + _index.md
└── tours/              # dependency-ordered reading sequences + _index.md
```

Create the folders empty (Bash `mkdir -p`). The workflow's Map agents write entity pages into
them; the Index phase writes each `_index.md`, `index.md`, `overview.md` (with its mermaid
architecture diagram), `Start Here.md`, and `Glossary.md` (the two maintained meta pages — see
the taxonomy's "First-Class Meta Pages" section). The skill writes `CLAUDE.md`, `log.md`,
`hot.md`, `.manifest.json`, `.gitignore`.

`.gitignore` contents:
```
.source/
```

---

## CLAUDE.md template (write to `<autodoc-dir>/CLAUDE.md`)

```markdown
# <autodoc-dir> — Code Documentation Wiki

Auto-generated, cross-linked documentation of the **<repo>** codebase.
A code documentation wiki: structured pages, one per code entity, that compound
and stay current via `/autodoc-update`.

## What this is

A read-first map of the codebase. Every endpoint, page, component, service, process, vendor,
runbook, data-model, ADR, flow, and job is one page with frontmatter + intent/contract docs +
wikilinks. Start at [[overview]], then [[index]]. Non-engineers start at [[Start Here]];
[[Glossary]] defines the invented domain vocabulary.

## Source of truth

The CODE is authoritative; these pages document intent and contract. Source repo:
<repo> (the project's `canonical_repo`, on its `base_branch`). Each page's `source_paths`
frontmatter points back to the files it documents. `.manifest.json` tracks the last-documented
commit (`last_sha`).

## Structure

- 12 type folders (endpoints, pages, components, services, processes, vendor, runbooks,
  data-models, decisions, flows, jobs, tours), each with an `_index.md`.
- `index.md` master catalog, `overview.md` architecture summary, `log.md`, `hot.md`.

## Maintenance

- Regenerate from scratch: `/autodoc-init` (destructive of nav layer).
- Incremental refresh from new commits: `/autodoc-update` (the normal path).
- Conventions live in `skills/autodoc-init/references/taxonomy.md`.

## Reading this from another project

1. Read `<autodoc-dir>/hot.md` (recent state + last_sha).
2. Then `<autodoc-dir>/overview.md` and `<autodoc-dir>/index.md`.
3. Then drill into specific entity pages.
Do not read it for questions answered by the wiki or the project's own files.
```
