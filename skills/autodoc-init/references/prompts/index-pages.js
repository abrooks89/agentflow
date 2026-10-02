// prompts/index-pages.js — the index agent, shrunk to what needs an LLM
// (epic 704): overview.md, Start Here.md, Glossary.md as meta records.
// _index.md files and index.md are rendered deterministically from the store.

import { renderTemplate } from './engine.js';

export const id = 'index';

// Records file shape: { meta_pages: [{file, title, body}] }.
export const schema = {
  type: 'object',
  required: ['path', 'files'],
  properties: {
    path: { type: 'string', description: 'the records file written (must equal the recordsPath given in the prompt)' },
    files: {
      type: 'array',
      items: { type: 'string', enum: ['overview.md', 'Start Here.md', 'Glossary.md'] },
      description: 'the meta page files present in the records file',
    },
  },
};

export const RECORDS_FILE_SCHEMA = {
  type: 'object',
  required: ['meta_pages'],
  properties: {
    meta_pages: {
      type: 'array',
      items: {
        type: 'object',
        required: ['file', 'title', 'body'],
        properties: {
          file: { type: 'string', enum: ['overview.md', 'Start Here.md', 'Glossary.md'] },
          title: { type: 'string' },
          body: { type: 'string' },
        },
      },
    },
  },
};

export const contextSpec = {
  autodocName: 'string — display name of the code wiki',
  taxonomyDocPath: 'string — absolute path to references/taxonomy.md',
  repoName: 'string — owner/repo',
  recordsPath: 'string — absolute path the agent writes its records JSON to (its ONLY allowed write)',
  catalog: 'string — the full page set, one "file :: type :: title" line per page',
  typeSummary: 'string — counts by type, comma-joined',
};

export const template = `You write the three MAINTAINED meta pages of the {{autodocName}} code wiki, as RECORDS.
You do NOT write any page content — the deterministic renderer produces the markdown (and
builds every _index.md and the master index.md itself; those are no longer your job). The
ONLY file you may write is your records JSON at {{recordsPath}} (create parent directories
as needed); any other write is a defect.

Read the taxonomy: {{taxonomyDocPath}} (the "First-Class Meta Pages" section).

Full page set just created (file :: type :: title):
{{catalog}}

Counts by type: {{typeSummary}}

WRITE one JSON object to {{recordsPath}} — { "meta_pages": [ <exactly three records:
file, title, body — body is markdown without frontmatter> ] } — then RETURN only
{ "path": "{{recordsPath}}", "files": [the three file names] }. The three records:

1. overview.md — executive architecture summary (~300–500 words) for {{repoName}}:
   what the system is, the major services and how they fit, the central pipeline/flows, the
   external/vendored dependencies, and the key ADRs that shaped it. Wikilink the
   load-bearing pages using exact catalog titles (links to pages that don't exist are
   dropped at render). MUST contain exactly one mermaid architecture diagram (a small
   services-and-arrows graph of how the major pieces connect).

2. Start Here.md — plain-language product + state orientation for non-engineers: what the
   product does, for whom, how a request becomes shipped software in a few plain steps, and
   what state it's in (live vs. legacy vs. planned) — list EVERY legacy/retired subsystem
   the corpus marks, not just one. No unglossed coinages; minimal wikilinks.

3. Glossary.md — the invented domain vocabulary: one plain sentence per term, linking to the
   defining page. Include every coined term used load-bearingly in overview.md and the
   service pages (runtime primitives, persona names) — no coinage ships undefined.`;

export function render(context) {
  return renderTemplate(template, context);
}
