// prompts/flows.js — cross-cutting flow narratives as records (epic 704).
// The flow-backlink EDIT dance is gone: backlinks are the inverse traversal
// of a flow's edges, rendered on the touched pages by the renderer.

import { renderTemplate } from './engine.js';
import { PAGE_RECORD_SCHEMA } from './record-schema.js';

export const id = 'flows';

// Records file shape: { pages: [PAGE_RECORD (type flow)] }; the return is a
// light manifest (adversarial finding 5 — bodies stay on disk).
export const schema = {
  type: 'object',
  required: ['path', 'flowsMeta'],
  properties: {
    path: { type: 'string', description: 'the records file written (must equal the recordsPath given in the prompt)' },
    flowsMeta: {
      type: 'array',
      items: {
        type: 'object',
        required: ['file', 'title', 'source_paths'],
        properties: {
          file: { type: 'string' }, title: { type: 'string' },
          source_paths: { type: 'array', items: { type: 'string' } },
        },
      },
      description: 'one light row per flow record in the file (no bodies)',
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
  repoName: 'string — owner/repo',
  repoPath: 'string — absolute path to the source clone',
  recordsPath: 'string — absolute path the agent writes its records JSON to (its ONLY allowed write)',
  catalog: 'string — the mapped entity catalog, one "type :: title :: source" line per page',
  notes: 'string — cross-community notes from the map agents (flow candidates live here)',
};

export const template = `You synthesize cross-cutting FLOW pages for the {{autodocName}} code wiki, as RECORDS.
You READ code and context; you do NOT write any page content — a deterministic renderer
produces the markdown from your records. The ONLY file you may write is your records JSON
at {{recordsPath}} (create parent directories as needed); any other write is a defect.

Read the taxonomy: {{taxonomyDocPath}} (especially the 'flow' type + body structure).

You are given the full entity catalog already documented (type :: title :: source):
{{catalog}}

Cross-community notes from the mappers (flow candidates live here):
{{notes}}

Identify the major end-to-end narratives that connect these entities — request paths,
pipelines, auth flows, webhook → job → process chains. You may Read the repo at
{{repoPath}}/ to get the path right — but be economical.

For each flow worth documenting (aim 4–8, only real ones), RETURN a page record:
  - file: "flows/<Title>.md", type: "flow", title per the naming convention. Page filenames
    are CASE-INSENSITIVELY unique across the whole autodoc — never differ from an existing
    page by case alone.
  - source_paths: the repo files the narrative's steps live in.
  - summary: one line; body: walks the steps in order, wikilinking each entity it touches
    (use exact catalog titles — a link to a page that doesn't exist is dropped at render).
  - frontmatter: flow_kind (request|data|auth|pipeline|event), entrypoint (the "[[...]]"
    entity the flow starts at, using a catalog title), touches (the "[[...]]" entities
    involved), status. NEVER set created/updated/source_repo/related/depends_on.
  - edges: one {rel: "related", dst_type, dst_title} per entity the flow touches, using
    exact catalog types + titles. These edges ARE the backlinks: the renderer walks them
    inverse and adds this flow to each touched page's 'related:' list — do not ask anyone
    to edit other pages.
  - NEVER return an "id"; never use a rel outside depends_on/part_of/related.

LIVENESS: a flow narrates a LIVE path. If the pipeline a candidate flow describes is retired
or legacy-mode-only (check the entities' status + mounting in code), either skip it or return
it with 'status: legacy' and the taxonomy's opening legacy callout in the body — never present
a retired pipeline in present tense.

WRITE one JSON object to {{recordsPath}}: { "pages": [ <every flow page record> ] }.
Then RETURN only the manifest (records stay on disk — never inline the bodies):
  { "path": "{{recordsPath}}", "flowsMeta": [ {file, title, source_paths} per flow ] }`;

export function render(context) {
  return renderTemplate(template, context);
}
