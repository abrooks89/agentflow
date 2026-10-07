// record-schema.js — the shared page-record return shape (epic 704).
//
// One JSON Schema fragment for "a page as a record", reused by the map, flows,
// and tours prompts. This is the SAME contract `autodoc_graph.records`
// enforces on write: extend both together or not at all.

export const PAGE_RECORD_SCHEMA = {
  type: 'object',
  required: ['file', 'type', 'title', 'source_paths', 'summary', 'body', 'frontmatter'],
  properties: {
    file: { type: 'string', description: 'autodoc-relative path, e.g. services/Canonical.md — folder must match the type' },
    type: { type: 'string', enum: ['endpoint', 'page', 'component', 'service', 'process', 'vendor', 'runbook', 'data-model', 'decision', 'flow', 'job', 'tour'] },
    title: { type: 'string' },
    source_paths: { type: 'array', items: { type: 'string' }, description: 'every repo-relative file the page derives a claim from' },
    summary: { type: 'string', description: 'the one-line plain-English summary (also the body opening line)' },
    body: { type: 'string', description: 'the full markdown body WITHOUT frontmatter and WITHOUT a Diagram section' },
    frontmatter: { type: 'object', description: 'type-specific frontmatter fields only — never created/updated/source_repo/related/depends_on (renderer-owned)' },
    documents: {
      type: 'array',
      items: {
        type: 'object',
        required: ['file', 'kind', 'name'],
        properties: {
          file: { type: 'string' }, kind: { type: 'string' }, name: { type: 'string' },
        },
      },
      description: 'symbol-granularity citations, copied EXACTLY from the coverage checklist rows this page documents; `graph check-records` resolves each against the symbol index of the store the moment the records file is written',
    },
    edges: {
      type: 'array',
      items: {
        type: 'object',
        required: ['rel', 'dst_type', 'dst_title'],
        properties: {
          rel: { type: 'string', enum: ['depends_on', 'part_of', 'related'] },
          dst_type: { type: 'string' },
          dst_title: { type: 'string' },
        },
      },
      description: 'concept-to-concept relations; targets are named by type+title, never by id',
    },
  },
};

export const REPORT_RECORD_SCHEMA = {
  type: 'object',
  required: ['community_id', 'summary', 'findings'],
  properties: {
    community_id: { type: 'number', description: 'the community id from the checklist section header — NEVER the label' },
    summary: { type: 'string', description: 'what this community is and does, one tight paragraph' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['text'],
        properties: {
          text: { type: 'string', description: 'one ranked, self-contained insight' },
          entities: { type: 'array', items: { type: 'string' }, description: 'entity titles / symbol names this finding cites' },
        },
      },
      description: 'ranked findings, most load-bearing first (MS GraphRAG report shape)',
    },
  },
};
