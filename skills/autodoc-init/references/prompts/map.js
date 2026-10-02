// prompts/map.js — the map agent: one work unit of communities -> page records
// + community reports (epic 704). Agents READ code and RETURN records; they
// never write files and never mint ids.

import { renderTemplate } from './engine.js';
import { PAGE_RECORD_SCHEMA, REPORT_RECORD_SCHEMA } from './record-schema.js';

export const id = 'map';

// The FULL records land on disk at {{recordsPath}} (the workflow return must
// stay small — adversarial finding 5); the agent returns only this manifest.
// Records file shape: { pages: [PAGE_RECORD], reports: [REPORT_RECORD],
//   covered: [{community, name, line}], excluded: [{community, name, line, reason}] }
export const schema = {
  type: 'object',
  required: ['unit', 'path', 'pagesMeta', 'reportIds'],
  properties: {
    unit: { type: 'string' },
    path: { type: 'string', description: 'the records file written (must equal the recordsPath given in the prompt)' },
    pagesMeta: {
      type: 'array',
      items: {
        type: 'object',
        required: ['file', 'type', 'title', 'source_paths'],
        properties: {
          file: { type: 'string' }, type: { type: 'string' }, title: { type: 'string' },
          source_paths: { type: 'array', items: { type: 'string' } },
        },
      },
      description: 'one light row per page record in the file (no bodies)',
    },
    reportIds: { type: 'array', items: { type: 'number' }, description: 'community ids of the reports in the file' },
    notes: { type: 'string', description: 'anything cross-community worth flagging to synthesis (candidate flows, deps spanning communities)' },
    counts: {
      type: 'object',
      properties: {
        pages: { type: 'number' }, reports: { type: 'number' },
        covered: { type: 'number' }, excluded: { type: 'number' },
      },
    },
  },
};

// Kept for records-file documentation/consumers:
export const RECORDS_FILE_SCHEMA = {
  type: 'object',
  required: ['pages', 'reports', 'covered', 'excluded'],
  properties: {
    pages: { type: 'array', items: PAGE_RECORD_SCHEMA },
    reports: { type: 'array', items: REPORT_RECORD_SCHEMA },
    covered: {
      type: 'array',
      items: { type: 'object', required: ['name', 'line'],
        properties: { community: { type: 'string' }, name: { type: 'string' }, line: { type: 'number' } } },
    },
    excluded: {
      type: 'array',
      items: { type: 'object', required: ['name', 'line', 'reason'],
        properties: { community: { type: 'string' }, name: { type: 'string' }, line: { type: 'number' }, reason: { type: 'string' } } },
    },
  },
};

// Context contract (707's seam — a Python driver builds exactly this shape):
export const contextSpec = {
  autodocName: 'string — display name of the code wiki (autodoc root basename)',
  taxonomyDocPath: 'string — absolute path to references/taxonomy.md',
  repoName: 'string — owner/repo',
  repoPath: 'string — absolute path to the source clone',
  unitKey: 'string — the work-unit key, e.g. unit-03',
  recordsPath: 'string — absolute path the agent writes its records JSON to (its ONLY allowed write)',
  communitiesLine: 'string — id:"label" pairs, comma-joined',
  communities: 'array of {id (number), label, memberCount (number), topPrefixes (string, may be empty), topSymbols (string, may be empty), symbols: [{kind, name, file, line}]}',
};

export const template = `You are mapping ONE work unit (a group of derived code communities) of a codebase into
the "{{autodocName}}" code wiki. You READ code and PRODUCE structured records. You do NOT
write any page content anywhere — a deterministic renderer produces the markdown later,
from your records. The ONLY file you may write is your records JSON at
{{recordsPath}} (create parent directories as needed); any other write is a defect.

STEP 1 — Read the taxonomy contract IN FULL: {{taxonomyDocPath}}
It defines the entity types, the exact frontmatter schema per type, naming, page body
structure, and cross-linking rules. Follow it precisely; it is non-negotiable.

STEP 2 — Map this work unit:
  Repo:        {{repoName}}
  Repo root:   {{repoPath}}
  Unit:        {{unitKey}}
  Communities: {{communitiesLine}}

Your unit is a set of graph-derived symbol communities and may span directories. Its exact
membership is the coverage checklist below — the files named there (paths are repo-relative
under the repo root) are yours to document. Read those files (skip node_modules, .git, build
artifacts, lockfiles, __pycache__, binary assets; a vendored subtree — vendor/, vendored/,
third_party/ — gets at most ONE vendor page per dependency, never deep documentation).
Identify every entity that belongs to one of the taxonomy's types. Resolve type ambiguity
using the taxonomy's routing rules. Do not invent entities that aren't in the code. Do NOT
create 'flow' or 'tour' pages — those are synthesized in later phases; note flow candidates
in your "notes". Other agents may READ files you share imports with, but only YOU return
records for your checklist symbols — page ownership stays disjoint.

STEP 2.5 — COVERAGE CHECKLIST (deterministic tree-sitter inventory, one section per
community in this unit). Every symbol below MUST end up either (a) documented on a page (its
file in that page's source_paths AND, wherever the page's claims rest on that exact symbol,
cited in that page's "documents" list), or (b) listed in your "excluded" return with a
one-line reason (e.g. trivial helper, private impl detail, re-export). Do not silently drop
any.
{{#each communities}}community {{id}} "{{label}}" — {{memberCount}} symbols{{#if topPrefixes}} — main directories: {{topPrefixes}}{{/if}}{{#if topSymbols}} — key symbols: {{topSymbols}}{{/if}}
{{#each symbols}}  - [{{kind}}] {{name}}  ({{file}}:{{line}})
{{/each}}
{{/each}}
STEP 3 — RETURN each entity as a page RECORD (no file writes — records only):
  - file: the autodoc-relative path the renderer will write, e.g. "services/Canonical.md" —
    folder per the taxonomy table, filename per the naming convention. Page filenames are
    CASE-INSENSITIVELY unique across the whole autodoc (the tree may land on a
    case-insensitive filesystem): "services/Foo.md" and "services/foo.md" collide.
  - type + title per the taxonomy. NEVER return an "id" — identity is derived from
    type + title by the writer; a record carrying its own id fails the run.
  - source_paths: every repo-relative file the page derives a load-bearing claim from
    (load-bearing — be complete).
  - summary: the one-line plain-English summary.
  - body: the full markdown body WITHOUT frontmatter, per the taxonomy's "Page Body
    Structure": the one-line summary, ## Source, ## What it does, ## Key behavior / contract,
    ## Connections, ## Gotchas. 60–250 lines. Document intent and contract, NOT line-by-line
    code. Never paste large code blocks. Cross-link generously with [[Wikilinks]] in prose —
    a link whose target page never materializes is dropped at render time, so link real
    entities. Do NOT author a ## Diagram section (the diagram phase owns those).
  - frontmatter: ONLY the type-specific fields (plus status and tags). NEVER set created,
    updated, source_repo, source_paths, related, or depends_on in frontmatter — the renderer
    stamps the universal fields and renders related/depends_on from your edges.
  - documents: the tier join. For every checklist symbol whose behavior this page documents,
    copy its {file, kind, name} EXACTLY from the checklist into this page's "documents" list.
    A citation that does not match a checklist symbol fails the run. This is what makes
    "who calls this?" and "what explains this?" answerable in one query — cite at symbol
    granularity wherever you can justify it; where you can only justify a file, leave it to
    source_paths.
  - edges: concept-to-concept relations among the pages YOU are returning (or their
    obviously-shared service), as {rel, dst_type, dst_title}. The vocabulary is CLOSED:
    depends_on (asserted architectural dependency), part_of (composition), related (weak
    association). Any other rel fails the run naming the record. Assert only relations you
    located in code, never name-similarity guesses.

VERIFICATION DISCIPLINE (non-negotiable, every page record — the taxonomy's "Evidence
Protocols" and "Liveness protocol" sections are part of this contract, follow them all):
  - Endpoint pages: request/response fields must be TRANSCRIBED from the actual request/response
    models or return statements — never inferred from route or model names. Check the stated
    purpose against the module docstring before writing it; if they disagree, the docstring wins.
  - Route families: documenting any route from a module means enumerating EVERY route defined in
    that module (your checklist lists them) — one alias per route; 'auth:' is what an anonymous
    caller experiences, strictest-member for a family.
  - LIVENESS: present-tense "it does X" claims require the located activation chain (router
    mounted, component imported by live code, daemon wired with real deps, job registered).
    Legacy-mode-only or no-production-caller entities state that in their opening lines.
  - Consumer claims (used_by/consumed_by/Connections): each entry is written from a LOCATED
    import or call site — never from name similarity. Write-path claims name the exact exported
    function and file; a pure-read module never gains a write capability or mutation warning.
  - Multi-file pages: attribute each claim to its file; before finishing, re-open every
    source_paths entry and confirm a distinguishing symbol from your text lives in that file.
  - Never characterize a cited test — QUOTE its docstring. A test citation that contradicts the
    sentence citing it is a critical defect.
  - Cross-service contracts: when documenting one side (a caller's payload, a consumer's expected
    shape), OPEN the other side's source and confirm the shapes actually match. Document any
    mismatch explicitly instead of asserting an integration you haven't confirmed.
  - When a page documents ONE SIDE of a cross-service/cross-process wire contract, emit the
    optional 'contract_with:' frontmatter listing the other side's page(s) per the taxonomy —
    each side lists the other.
  - Every endpoint page includes the REQUIRED 'aliases' frontmatter (slash-form, one per route).
    Frontmatter relational links (calls_endpoints, used_by, renders, involves, …) must resolve
    per the taxonomy — endpoint links use the slash-form alias, never a dash-form stem.
  - Dates come only from source (an ADR's own date line); undated doc => date: "" — never infer.
  - Never put tool-call/XML wrapper syntax (</content>, </invoke>, <parameter, etc.) into a record.

STEP 4 — COMMUNITY REPORTS (same call, zero extra agents). For EACH community in this unit,
return one report — the unit of global search over this corpus:
  - community_id: the NUMBER from the checklist section header (never the label).
  - summary: one tight paragraph — what this community is, what it does, how it fits the
    system.
  - findings: ranked, most load-bearing first, each EXACTLY this shape:
    {"text": "<one self-contained insight — an invariant, a hub symbol, a risky coupling,
    a contract>", "entities": ["<page titles / symbol names it cites>"]}. The insight prose
    goes in "text" (required, non-empty); 3–8 findings; only real ones.

STEP 5 — WRITE the records file, then return a light manifest. Write ONE JSON object to
{{recordsPath}} with exactly these keys:
  - "pages": every page record from STEP 3
  - "reports": one report per community from STEP 4
  - "covered": the checklist symbols you documented, as
    {community:"<its community id>",name,line}
  - "excluded": {community,name,line,reason} covering every remaining checklist symbol
Then RETURN only the manifest (the records stay on disk — never inline the bodies):
  - "unit": "{{unitKey}}"
  - "path": the records file you wrote ({{recordsPath}})
  - "pagesMeta": one {file, type, title, source_paths} row per page record (no bodies)
  - "reportIds": the community ids you summarized
  - "notes": cross-community flow candidates
  - "counts": {pages, reports, covered, excluded}`;

export function render(context) {
  return renderTemplate(template, context);
}
