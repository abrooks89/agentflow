// prompts/tours.js — dependency-ordered onboarding tours as records (epic 704).

import { renderTemplate } from './engine.js';
import { PAGE_RECORD_SCHEMA } from './record-schema.js';

export const id = 'tours';

// Records file shape: { pages: [PAGE_RECORD (type tour)] }.
export const schema = {
  type: 'object',
  required: ['path', 'toursMeta'],
  properties: {
    path: { type: 'string', description: 'the records file written (must equal the recordsPath given in the prompt)' },
    toursMeta: {
      type: 'array',
      items: {
        type: 'object',
        required: ['file', 'title'],
        properties: { file: { type: 'string' }, title: { type: 'string' } },
      },
    },
  },
};

export const RECORDS_FILE_SCHEMA = {
  type: 'object',
  required: ['pages'],
  properties: { pages: { type: 'array', items: PAGE_RECORD_SCHEMA } },
};

export const contextSpec = {
  autodocName: 'string — display name of the code wiki',
  taxonomyDocPath: 'string — absolute path to references/taxonomy.md',
  recordsPath: 'string — absolute path the agent writes its records JSON to (its ONLY allowed write)',
  catalog: 'string — the mapped entity catalog, one "type :: title :: source" line per page',
  flowsList: 'string — the flow titles just synthesized, one "flow :: title" line each',
  tourOrderPath: 'string — path to references/inventory/tour-order.js (deterministic ordering reference)',
};

export const template = `You synthesize dependency-ordered TOUR pages for the {{autodocName}} code wiki, as
RECORDS. You do NOT write any page content — a deterministic renderer produces the markdown
from your records. The ONLY file you may write is your records JSON at {{recordsPath}}
(create parent directories as needed); any other write is a defect.

Read the taxonomy: {{taxonomyDocPath}} (especially the 'tour' type, its type-specific
frontmatter — tour_kind/steps/covers — and the "Flow vs. tour" distinction).

A tour is an ONBOARDING READING PATH: pick a real subsystem, gather its member pages,
and present them in a bottom-up dependency order (foundational pages first) so a new
reader learns it from the ground up. This is NOT a runtime narrative (that's a flow).

You are given the full entity catalog already documented (type :: title :: source):
{{catalog}}

Flows just synthesized (each is its own page; tours may reference flow pages as steps):
{{flowsList}}

STEP 1 — Pick 3–6 REAL subsystems worth an onboarding tour. Skip anything too thin to teach.

STEP 2 — For EACH chosen tour, determine its member pages from the catalog, then produce a
DEPENDENCY-ORDERED reading sequence using this rule: **if a page links/builds on another
page, the linked (foundational) page comes first.** The deterministic reference for this
ordering is {{tourOrderPath}} (buildTourGraph/orderTour) — you MAY shell out to node to call
orderTour for an exact order, OR order the steps yourself by the documented bottom-up rule.
Either way the steps MUST be dependencies-before-dependents and include every member page
exactly once.

STEP 3 — RETURN each tour as a page record:
  - file: "tours/<Title> Tour.md", type: "tour", title "<Subsystem> Tour".
  - source_paths: [] (a tour has no source of its own — it teaches existing pages).
  - summary: one line; body: walk the steps IN ORDER, one short section per step,
    wikilinking the step page (exact catalog titles) and giving a one-line "why this page
    matters here". Only claim a step "builds on" an earlier one when a real link exists.
    Follow the taxonomy's "Tour pedagogy rules": intro of at most 5 sentences including an
    audience line; pipeline tours ordered trigger-first (state the override explicitly);
    include the subsystem's hub pages; no changelog prose in step bodies.
  - frontmatter: tour_kind (subsystem|cross-cutting), steps (the ordered "[[Wikilinks]]",
    dependencies first), covers (the subsystem/communities taught, as "[[wikilinks]]"),
    status, tags. NEVER set created/updated/source_repo/related/depends_on.
  - edges: one {rel: "related", dst_type, dst_title} per step page, using exact catalog
    types + titles. NEVER return an "id".

WRITE one JSON object to {{recordsPath}}: { "pages": [ <every tour page record> ] }.
Then RETURN only: { "path": "{{recordsPath}}", "toursMeta": [ {file, title} per tour ] }`;

export function render(context) {
  return renderTemplate(template, context);
}
