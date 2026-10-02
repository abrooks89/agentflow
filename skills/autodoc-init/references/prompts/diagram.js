// prompts/diagram.js — BATCHED diagram authoring (epic 704 fan-out cap).
// One agent handles up to `diagramBatch` targets (default 8); every diagram
// still gets its own validate_mermaid.py gate. The validated mermaid comes
// back as a record; the renderer places the ## Diagram section.

import { renderTemplate } from './engine.js';

export const id = 'diagram';

// Records file shape below — mermaid can be sizable, so it stays on disk
// (adversarial finding 5); the agent returns only counts.
export const schema = {
  type: 'object',
  required: ['path', 'embedded', 'omitted'],
  properties: {
    path: { type: 'string', description: 'the records file written (must equal the recordsPath given in the prompt)' },
    embedded: { type: 'number', description: 'diagrams validated and included' },
    omitted: { type: 'number', description: 'diagrams omitted with a reason' },
  },
};

export const RECORDS_FILE_SCHEMA = {
  type: 'object',
  required: ['diagrams'],
  properties: {
    diagrams: {
      type: 'array',
      items: {
        type: 'object',
        required: ['type', 'title', 'embedded'],
        properties: {
          type: { type: 'string', enum: ['flow', 'data-model'] },
          title: { type: 'string' },
          embedded: { type: 'boolean' },
          mermaid: { type: 'string', description: 'the VALIDATED mermaid source (required when embedded)' },
          reason: { type: 'string', description: 'why the diagram was omitted (when not embedded)' },
          source_sha: { type: 'string' },
        },
      },
    },
  },
};

export const contextSpec = {
  repoPath: 'string — absolute path to the source clone',
  scratchPath: 'string — writable scratch directory for validation drafts (NOT the autodoc tree)',
  recordsPath: 'string — absolute path the agent writes its records JSON to',
  validateMermaidCmd: 'string — shell command validating a .mmd file (exit 0 = valid)',
  commitSha: 'string — the source sha diagrams are derived from',
  targets: 'array of {kind (flow|data-model), title, kindGuide (string), sourcePaths (string, comma-joined repo-relative paths)}',
};

export const template = `You author ONE validated Mermaid diagram per target below, as RECORDS. You never edit
autodoc pages — the renderer places each diagram from your records. The ONLY files you may
write are temporary .mmd drafts under {{scratchPath}}/ (create it if needed, delete your
drafts when done) and your records JSON at {{recordsPath}}.

For EACH target, in order:
{{#each targets}}  - page type "{{kind}}" | title "{{title}}" — diagram {{kindGuide}}; sources: {{sourcePaths}}
{{/each}}
The quoted page type ("flow" or "data-model") is EXACTLY what each record's "type" field
must carry — never the mermaid kind (sequenceDiagram / erDiagram / flowchart / C4Context),
which belongs only inside the mermaid source. "title" likewise copies verbatim.

Steps per target:
- Read its source files under {{repoPath}}/ FIRST.
- Context+Container abstraction ONLY: name services/models + their relationships, not every
  field. <= 20 elements; simplify if over.
- ONE connected story per diagram — never disconnected concept clusters; omit test-DI
  scaffolding entities. A diagram belongs to exactly ONE page — if two targets share the
  relationship, diagram it once and omit it (embedded: false, reason names the sibling).
- Every node naming a code entity gets a "%% source: <repo-relative-path>" comment. NEVER
  fabricate a node or a field — transcribe field names from the schema. Grounding paths are
  repo-relative EXACTLY as listed above (no repo-name prefix) and must exist: verify each
  with test -f under the clone. ALL %% comments go INSIDE the mermaid source.

GATE (mandatory, per diagram, deterministic — do NOT skip):
1. Write the diagram source to {{scratchPath}}/draft-<n>.mmd
2. Run: {{validateMermaidCmd}} {{scratchPath}}/draft-<n>.mmd
3. Exit 0 -> record {type, title, embedded: true, mermaid: <the validated source>, source_sha: "{{commitSha}}"}.
   Non-zero -> fix from the validator's own error output (MAX 2 attempts). Still failing ->
   record {type, title, embedded: false, reason: <why>} with NO mermaid.
4. Delete the draft file.

WRITE one JSON object to {{recordsPath}}: { "diagrams": [...] } covering EVERY target
exactly once (embedded true or false). Then RETURN only:
  { "path": "{{recordsPath}}", "embedded": <count>, "omitted": <count> }`;

export function render(context) {
  return renderTemplate(template, context);
}
