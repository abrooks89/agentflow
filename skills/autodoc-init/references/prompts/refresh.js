// prompts/refresh.js — the incremental-refresh agent prompt.
//
// RECORD-CONVERTED (epic 706). This prompt used to tell the agent to EDIT the
// live markdown tree; it now tells the agent to emit PAGE RECORDS, exactly like
// the init-path prompts (map/flows/tours). The renderer owns the markdown on
// both paths, so an update can no longer be silently undone by the next
// `graph render` -- which is the whole defect 704 left behind and this closes.
//
// What did NOT change: every line of the VERIFICATION DISCIPLINE below. It is
// about how you establish a claim, not about where you write it, so it
// transfers to records untouched. Rewriting it here would have been the easiest
// way to lose it.
//
// Two things ride existing contracts rather than new store code (Q-10010):
//   * deprecation - `status:` is a taxonomy frontmatter value (taxonomy.md:82),
//     so a superseded page is a record carrying `frontmatter.status:
//     'deprecated'` plus the stale callout in its `body`. The store needs no
//     new concept.
//   * re-render - `graph render` has no partial mode, and does not need one:
//     rendering twice is byte-identical on an unchanged store, so a full render
//     after an update emits exactly the touched pages and nothing else.

import { renderTemplate } from './engine.js';
import { PAGE_RECORD_SCHEMA } from './record-schema.js';

export const id = 'refresh';

// The FULL records land on disk at {{recordsPath}}; this return is the light
// manifest the workflow needs to route the store write. Same split the init
// prompts use (map.js), and for the same reason: a workflow return carrying
// whole page bodies is a return nobody can read and a context nobody can spare.
export const schema = {
  type: 'object',
  required: ['community', 'path', 'updated', 'created', 'removed'],
  properties: {
    community: { type: 'string' },
    path: { type: 'string', description: 'the records file written (must equal the recordsPath given in the prompt)' },
    updated: { type: 'array', items: { type: 'string' }, description: 'autodoc-relative paths whose record this refresh re-emitted' },
    created: {
      type: 'array',
      items: {
        type: 'object',
        required: ['file', 'type', 'title', 'source_paths'],
        properties: {
          file: { type: 'string' }, type: { type: 'string' }, title: { type: 'string' },
          source_paths: { type: 'array', items: { type: 'string' } },
          links: { type: 'array', items: { type: 'string' } },
        },
      },
    },
    removed: { type: 'array', items: { type: 'string' }, description: 'autodoc-relative paths re-emitted as status: deprecated (the source was deleted)' },
    notes: { type: 'string' },
    covered: {
      type: 'array',
      items: { type: 'object', required: ['name', 'line'],
        properties: { community: { type: 'string' }, name: { type: 'string' }, line: { type: 'number' } } },
      description: 'changed-file inventory symbols this refresh documented (community,name,line from the checklist)',
    },
    excluded: {
      type: 'array',
      items: { type: 'object', required: ['name', 'line', 'reason'],
        properties: { community: { type: 'string' }, name: { type: 'string' }, line: { type: 'number' }, reason: { type: 'string' } } },
      description: 'changed-file inventory symbols intentionally NOT given/updated their own page, with a one-line reason',
    },
  },
};

export const contextSpec = {
  autodocName: 'string — display name of the code wiki',
  recordsPath: 'string — absolute path the agent writes its records JSON to (its ONLY allowed write)',
  taxonomyDocPath: 'string — absolute path to references/taxonomy.md',
  repoName: 'string — owner/repo',
  repoPath: 'string — absolute path to the source clone',
  autodocPath: 'string — absolute path to the live autodoc tree',
  fromShort: 'string — 8-char from-sha',
  toShort: 'string — 8-char to-sha',
  communityKey: 'string — the community key being refreshed',
  label: 'string — community display label',
  topPrefixes: 'string — comma-joined main directories (may be empty)',
  topSymbols: 'string — comma-joined key symbols (may be empty)',
  changedFiles: 'array of strings — changed repo-relative files in this community',
  affectedPages: 'array of strings — autodoc-relative refresh candidates',
  contextPages: 'array of {page, owner} (optional) — pages that cite this community\'s changed files but are OWNED by another community\'s refresh (owner is its key). Read-only context: shown so the agent neither calls the code new nor re-emits a page another agent owns',
  symbols: 'array of {name, file, line, kind, coveredBy} — changed-file checklist rows (kind is optional and printed only when non-empty; coveredBy is optional and names a contextPages page that already cites the row\'s file; other extra caller keys are tolerated and ignored; may be empty). IGNORED when checklistPath is set: the checklist is then delivered BY PATH, because interpolating thousands of rows into the prompt text is what blows the workflow payload cap (E-0743)',
  checklistPath: 'string (optional) — absolute path to a JSON file holding the same {name, file, line, kind} checklist rows. When non-empty it REPLACES the inline listing and the agent is told to read the file; when empty/absent the rows render inline exactly as before',
  symbolCount: 'number (optional) — how many rows that file holds, so the agent knows the size of what it must read (only meaningful alongside checklistPath)',
  deletedFiles: 'array of strings — repo-relative files deleted in the range',
  date: 'string — run date YYYY-MM-DD',
  checkRecordsCmd: 'string (optional) — `graph check-records --db <store> --corpus <id>` as a shell command; the agent appends --records <its file> and must re-run it until it exits 0 (gh#465). Empty/absent omits the step',
};

export const template = `You incrementally refresh ONE derived code community of the "{{autodocName}}" code wiki after new commits.

You READ code and PRODUCE structured records. You do NOT write any page content anywhere — a
deterministic renderer produces the markdown later, from your records. The ONLY file you may
write is your records JSON at {{recordsPath}} (create parent directories as needed); any other
write is a defect. In particular {{autodocPath}}/ is READ-ONLY to you: it is the tree the
renderer owns, and an edit made there is silently undone by the next render.

STEP 1 — Read the taxonomy contract IN FULL: {{taxonomyDocPath}} (11 types, frontmatter, body, links).

STEP 2 — Context for this refresh:
  Repo: {{repoName}}  (root: {{repoPath}})   range: {{fromShort}}..{{toShort}}
  Community: {{communityKey}}
  Guidance: derived community "{{label}}"{{#if topPrefixes}} — main directories: {{topPrefixes}}{{/if}}{{#if topSymbols}} — key symbols: {{topSymbols}}{{/if}}

  Changed source files in this community (added/modified):
{{#each changedFiles}}    - {{.}}
{{/each}}{{^changedFiles}}    (none)
{{/^changedFiles}}
  Existing autodoc pages whose source_paths reference those files (refresh candidates):
{{#each affectedPages}}    - {{.}}
{{/each}}{{^affectedPages}}{{^contextPages}}    (none — likely all-new code)
{{/^contextPages}}{{#if contextPages}}    (none of your own — the read-only pages below document these files)
{{/if}}{{/^affectedPages}}{{#if contextPages}}  READ-ONLY context — pages that also cite those files but belong to ANOTHER community's refresh.
  Read them to see what is already documented; never emit a record for them, and never create a
  page for an entity they already cover:
{{#each contextPages}}    - {{page}}  (owned by {{owner}}; do not re-emit)
{{/each}}{{/if}}
  Entries marked "consumer of changed contract — re-verify claims about it" do NOT cite the
  changed files directly — they LINK to pages that do. For each one, re-verify every claim it
  makes about the changed contract (shapes, endpoints, behavior) against the current source;
  edit it if stale, leave it untouched (no 'updated' churn) if still accurate.

STEP 2.5 — COVERAGE CHECKLIST (deterministic tree-sitter inventory for the CHANGED files in this community).
Every symbol below MUST end up either (a) documented on a page that survives this refresh (its file in
that page's source_paths — whether you edited the page or it was already accurate), or (b) listed in
your "excluded" return with a one-line reason (e.g. trivial helper, private impl detail, re-export,
covered by an existing accurate page). Do not silently drop any changed-file symbol.
A row carrying "coveredBy" names a read-only page that cites this row's file. A cited file is not a
documented symbol, so read that page: if it already documents this symbol, list the row in "covered"
and emit nothing; otherwise treat it like any other row (a new page if it is a distinct entity,
else "excluded" with a reason naming that page and its owner).
{{#if checklistPath}}    The checklist is delivered BY PATH, not listed here: READ {{checklistPath}} IN FULL before
    you start. It is a JSON array of {{symbolCount}} row(s) of {name, file, line, kind} (plus
    "coveredBy" on some rows) — the same rows that would otherwise appear below, and every one of
    them is in scope for the rule above.
{{/if}}{{^checklistPath}}{{#each symbols}}    - {{name}}  ({{file}}:{{line}}){{#if kind}}  kind: {{kind}}{{/if}}{{#if coveredBy}}  coveredBy: {{coveredBy}}{{/if}}
{{/each}}{{^symbols}}    (no pre-extracted symbols in the changed files for this community — refresh by reading the diff)
{{/^symbols}}{{/^checklistPath}}
STEP 3 — Do the minimum correct update, as RECORDS:
  - Read the changed source files at {{repoPath}}/... and the affected pages at {{autodocPath}}/...
    You read the live tree to see what the page currently claims; you never write to it.
  - For each affected page whose behavior/contract/connections changed: EMIT A FULL RECORD for it,
    with the same 'file' path it already has. A record REPLACES that page, so carry forward
    everything still true — a record is not a patch, and anything you omit is dropped. List its
    path in 'updated'.
  - For genuinely NEW entities introduced by these commits (no existing page covers them): emit a
    record per the taxonomy and list it in 'created'.
  - If a changed file is trivial (formatting, comments, tests with no contract change) and no page's
    documented contract is affected, emit NOTHING for it. An unchanged page keeps its stored record
    and re-renders byte-identically, so silence is how you avoid churn — there is no 'updated:'
    date to bump, because the renderer owns that stamp.
  - Do NOT emit 'flow' records (Reconcile handles flows). Note new flow candidates in 'notes'.
  - Never mint ids and never write 'created'/'updated'/'source_repo'/'related'/'depends_on' into
    'frontmatter' — all five are renderer-owned and 'graph records' rejects a record carrying them.
    Relationships go in 'edges' (rel: depends_on | part_of | related), citations in 'documents'.
  - A 'documents' citation is resolved on (file, kind, name): copy each symbol's kind VERBATIM from
    its checklist row, never guess it — a Python method is stored as "function", and one citation
    that resolves to no symbol makes 'graph records' refuse your whole batch. A row with no kind
    gives you nothing to copy: leave that symbol out of 'documents' rather than guess one.

VERIFICATION DISCIPLINE (non-negotiable, every page you write or edit — the taxonomy's
"Evidence Protocols" and "Liveness protocol" sections are part of this contract, follow them all):
  - Endpoint pages: request/response fields must be TRANSCRIBED from the actual request/response
    models or return statements — never inferred from route or model names. Check the stated
    purpose against the module docstring before writing it; if they disagree, the docstring wins.
  - Route families: documenting any route from a module means enumerating EVERY route defined in
    that module — one alias per route; 'auth:' is what an anonymous caller experiences,
    strictest-member for a family.
  - LIVENESS: present-tense claims require the located activation chain (router mounted,
    component imported by live code, daemon wired with real deps, job registered). Legacy-mode-
    only or no-production-caller entities state that in their opening lines; retired subsystems
    are past tense everywhere.
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
  - Pages marked "contract pair" (or carrying 'contract_with:' frontmatter) MUST be verified
    against BOTH sides' source files — open both, compare actual shapes, document any mismatch
    explicitly on the page.
  - Every endpoint page includes the REQUIRED 'aliases' frontmatter (slash-form, one per route).
    Frontmatter relational links (calls_endpoints, used_by, renders, involves, …) must resolve
    per the taxonomy — endpoint links use the slash-form alias, never a dash-form stem.
  - Dates come only from source; undated doc => date: "" — never infer.
  - Never write tool-call/XML wrapper syntax (</content>, </invoke>, <parameter, etc.) into a page.

STEP 4 — Deletions: these source files were removed in this range:
{{#each deletedFiles}}    - {{.}}
{{/each}}{{^deletedFiles}}    (none)
{{/^deletedFiles}}  If any affected page documents ONLY deleted files (its subject no longer exists), do NOT drop it
  from your records — a page absent from the store is a page the renderer deletes, and a reader
  following an old link then gets nothing instead of an explanation. Emit its record with
  'frontmatter.status: "deprecated"' and a '> [!stale]' callout opening the body, noting the source
  was removed in {{toShort}}. List its path in 'removed'. An affected page that cites deleted
  files alongside live ones is not deprecated: re-emit it without the deleted paths in
  'source_paths' and without the claims only they supported, and list it in 'updated'.

CRITICAL: your ONLY write is the records JSON at {{recordsPath}}. It is a JSON OBJECT, never a
bare array — 'graph records' rejects anything else outright. Exactly this shape:

  { "pages": [ <page record>, ... ],
    "covered": [ {"community": "{{communityKey}}", "name": ..., "line": ...}, ... ],
    "excluded": [ {"community": "{{communityKey}}", "name": ..., "line": ..., "reason": ...}, ... ] }

Omit "reports" entirely — community reports belong to the init path, and a key you do not send is
simply skipped. Both {{repoPath}}/ and {{autodocPath}}/ are read-only to you. Do not emit records for
index.md / _index.md / overview.md (Reconcile's job; nav is rendered from meta records).
{{#if checkRecordsCmd}}
CHECK the file you just wrote, before returning:
    {{checkRecordsCmd}} --records "{{recordsPath}}"
Exit 0 means every page record is well-formed and every 'documents' citation resolves. A non-zero
exit prints one PROBLEM line per defect — a wrong kind (copy the kind the line names), an absent
symbol (drop the citation), or a kind that is never indexed at all (no such citation can ever
resolve: cite the enclosing symbol or leave the file to 'source_paths'). Fix every one, rewrite the
file, and re-run until it exits 0: this is where a bad citation costs one edit instead of a refused
'graph records' run after every agent has finished. A non-zero exit that prints NO PROBLEM line is
not a defect in your file (a missing store, a wrong corpus, a crash): do NOT re-run it. Stop
checking, return the manifest as below, and put "CHECK-RECORDS FAILED: <its error output,
verbatim>" first in notes.
{{/if}}
Return 'path' (equal to {{recordsPath}}) plus updated/created/removed (+ notes) as the light
manifest — NOT the record bodies, which stay on disk. Also return "covered" (the checklist symbols
now documented, as {community:"{{communityKey}}",name,line}) and "excluded"
({community,name,line,reason}) covering every remaining checklist symbol. "Documented" now means
"carried by a record you emitted, or already accurate in a page you deliberately left alone".`;

// Kept for records-file documentation/consumers, mirroring map.js. NOTE the
// difference from the map path: refresh emits no community 'reports' — those
// are init's, and `load_records_input` skips a key that is absent rather than
// faulting, so omitting it is the contract rather than a gap.
export const RECORDS_FILE_SCHEMA = {
  type: 'object',
  required: ['pages', 'covered', 'excluded'],
  properties: {
    pages: { type: 'array', items: PAGE_RECORD_SCHEMA },
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

export function render(context) {
  return renderTemplate(template, context);
}
