export const meta = {
  name: 'autodoc-init',
  description: 'Map an entire repo into autodoc RECORDS: agents read code and write per-agent record files under a run directory; a deterministic renderer produces the markdown tree (epic 704)',
  phases: [
    { title: 'Map', detail: 'one agent per packed work unit: page records + community reports to disk (no page writes)' },
    { title: 'Parent Reports', detail: 'one agent per MULTI-child parent, reading only child report files; single-child chains collapse (inherit at records-write time)' },
    { title: 'Flows', detail: 'cross-cutting flow records; backlinks become edges, not page edits' },
    { title: 'Diagram', detail: 'BATCHED validated-mermaid records (validate_mermaid.py gate per diagram); batch size widens to fit the remaining agent budget' },
    { title: 'Tours', detail: 'dependency-ordered tour records' },
    { title: 'Index', detail: 'overview / Start Here / Glossary meta records (nav is rendered deterministically)' },
  ],
}

// ---- args (passed by the /autodoc-init skill) -----------------------------
// {
//   repoPath:       absolute path to the cloned source repo (agents READ here)
//   autodocName:    display name of the code wiki (autodoc root basename)
//   repoName:       e.g. "owner/repo"
//   commitSha:      HEAD sha of the clone (stamps diagram records)
//   date:           "YYYY-MM-DD" (scripts can't call Date)
//   taxonomyDocPath: absolute path to references/taxonomy.md
//   validateMermaidCmd: shell command that validates a .mmd file (exit 0 = valid)
//   scratchPath:    writable scratch dir for diagram drafts (NEVER the render target)
//   tourOrderPath:  path to references/inventory/tour-order.js
//   runDir:         REQUIRED — writable run directory; every agent writes its
//                   records JSON to <runDir>/records/<label>.json and returns
//                   only a light manifest (the workflow return must stay small)
//   prompts:        REQUIRED — `node references/prompts/render-prompt.js --dump`
//   units:          REQUIRED — packed work units from `graph communities --emit`
//   communitySymbols: REQUIRED — { "<community id>": [ {kind,name,file,line}, ... ] }
//   parents:        REQUIRED — the artifact's parents chain (children-first):
//                   [ {id, level, label, children: [ids]} ]. Pass [] explicitly
//                   for a smoke slice. SINGLE-child parents are collapsed here
//                   (no agent; `graph records --artifact` inherits the child's
//                   report verbatim) — only multi-child parents cost an agent.
//   maxAgents:      hard ceiling on total agents (default 32); the run fails
//                   BEFORE the first dispatch when map+parents+synthesis plus
//                   the diagram reserve exceed it
//   diagramReserve: diagram batches reserved in the PRE-DISPATCH budget check
//                   (default 2) — never burn the Map phase then die at Diagrams
//   diagramBatch:   preferred diagram targets per agent (default 8); widened
//                   up to maxDiagramBatch when the remaining budget is tight
//   maxDiagramBatch: hard per-agent diagram cap for the widen-to-fit step
//                   (default 12) — beyond this the run fails BEFORE the
//                   Diagram dispatch with the computed numbers. A widened
//                   batch that quietly omits most of its diagrams is caught
//                   separately: omitted/targets > 30% fails the run.
// }
const {
  repoPath, autodocName, repoName, commitSha, date, taxonomyDocPath,
  validateMermaidCmd, scratchPath, tourOrderPath,
} = (typeof args === 'string' ? JSON.parse(args) : args)  // driver may deliver args stringified (live-smoke 2026-08-07)

const PROMPTS = args.prompts
const REQUIRED_PROMPTS = ['map', 'parent-report', 'flows', 'diagram', 'tours', 'index']
if (!PROMPTS || REQUIRED_PROMPTS.some((id) => !PROMPTS[id] || !PROMPTS[id].template || !PROMPTS[id].schema)) {
  throw new Error(`args.prompts is required — pass the output of \`node references/prompts/render-prompt.js --dump\` (needs ${REQUIRED_PROMPTS.join(', ')})`)
}

const RUN_DIR = args.runDir
if (!RUN_DIR || typeof RUN_DIR !== 'string') {
  throw new Error('args.runDir is required — the writable run directory agents write their records files under (the disk-based records contract)')
}
const recordsPathFor = (label) => `${RUN_DIR.replace(/[\\/]+$/, '')}/records/${label}.json`

const UNITS = args.units
if (!Array.isArray(UNITS) || UNITS.length === 0) {
  throw new Error('args.units is required and non-empty — run `graph communities --emit` and pass its units (no hardcoded fallback exists)')
}
const communitySymbols = args.communitySymbols
if (!communitySymbols || typeof communitySymbols !== 'object') {
  throw new Error('args.communitySymbols is required — the per-community coverage checklists from the communities artifact, keyed by community id')
}
for (const u of UNITS) {
  if (!u.key || !Array.isArray(u.communities) || u.communities.length === 0) {
    throw new Error(`unit ${u.key || '<unkeyed>'} has no communities — the artifact and units are out of sync`)
  }
  for (const c of u.communities) {
    const syms = communitySymbols[String(c.id)]
    if (!Array.isArray(syms) || syms.length === 0) {
      throw new Error(`communitySymbols['${c.id}'] is empty — every packed community has members by construction; a missing checklist means the artifact and units are out of sync`)
    }
  }
}
const PARENTS = args.parents
if (!Array.isArray(PARENTS)) {
  throw new Error('args.parents is required — the artifact\'s parents chain (pass [] explicitly for a smoke slice without parent roll-up)')
}
for (const p of PARENTS) {
  if (p.id == null || !Array.isArray(p.children) || p.children.length === 0) {
    throw new Error(`parent ${p.id ?? '<no id>'} has no children — the artifact and parents are out of sync`)
  }
}
// Single-child parent chains COLLAPSE (adversarial finding 1a): they inherit
// their child's report verbatim at records-write time; only multi-child
// parents cost an agent.
const ROLLUP_PARENTS = PARENTS.filter((p) => p.children.length > 1)
const COLLAPSED_PARENTS = PARENTS.filter((p) => p.children.length === 1)


// >>> template-engine (single-sourced: references/prompts/engine.js) >>>
function renderTemplate(template, context) {
  const OPEN = /\{\{(#each|#if|\^)?\s*([\w.]+)\s*\}\}/;
  const lookup = (scopes, key) => {
    for (let i = scopes.length - 1; i >= 0; i--) {
      const s = scopes[i];
      if (s !== null && typeof s === 'object' && key in s) return s[key];
      if (key === '.' && (typeof s !== 'object' || s === null)) return s;
    }
    return undefined;
  };
  const truthy = (v) => Array.isArray(v) ? v.length > 0
    : typeof v === 'string' ? v.length > 0
    : typeof v === 'number' ? v !== 0
    : Boolean(v);
  const render = (src, scopes) => {
    let out = '';
    let rest = src;
    for (;;) {
      const m = rest.match(OPEN);
      if (!m) return out + rest;
      out += rest.slice(0, m.index);
      rest = rest.slice(m.index + m[0].length);
      const [, kind, key] = m;
      if (!kind) {
        const v = lookup(scopes, key);
        if (v === undefined || v === null || (typeof v === 'object')) {
          throw new Error(`template placeholder {{${key}}} has no scalar value`);
        }
        out += String(v);
        continue;
      }
      const closeTag = kind === '^' ? `{{/^${key}}}` : `{{/${kind.slice(1)}}}`;
      // find the matching close, honoring nesting of the same kind
      const openRe = kind === '^'
        ? new RegExp(`\\{\\{\\^\\s*[\\w.]+\\s*\\}\\}`, 'g')
        : new RegExp(`\\{\\{${kind}\\s*[\\w.]+\\s*\\}\\}`, 'g');
      const closeRe = kind === '^' ? /\{\{\/\^[\w.]+\}\}/g : new RegExp(`\\{\\{/${kind.slice(1)}\\}\\}`, 'g');
      let depth = 1;
      let idx = 0;
      for (;;) {
        openRe.lastIndex = idx;
        closeRe.lastIndex = idx;
        const nextOpen = openRe.exec(rest);
        const nextClose = closeRe.exec(rest);
        if (!nextClose) throw new Error(`unclosed section for ${key} (expected ${closeTag})`);
        if (nextOpen && nextOpen.index < nextClose.index) {
          depth += 1;
          idx = nextOpen.index + nextOpen[0].length;
        } else {
          depth -= 1;
          idx = nextClose.index + nextClose[0].length;
          if (depth === 0) {
            var inner = rest.slice(0, nextClose.index);
            rest = rest.slice(idx);
            break;
          }
        }
      }
      const value = lookup(scopes, key);
      if (kind === '#each') {
        if (value === undefined) throw new Error(`template section {{#each ${key}}} has no value`);
        if (!Array.isArray(value)) throw new Error(`{{#each ${key}}} needs an array`);
        for (const item of value) out += render(inner, [...scopes, item]);
      } else if (kind === '#if') {
        if (truthy(value)) out += render(inner, scopes);
      } else {
        if (!truthy(value)) out += render(inner, scopes);
      }
    }
  };
  return render(template, [context]);
}
// <<< template-engine <<<

const renderPrompt = (id, context) => renderTemplate(PROMPTS[id].template, context)

// ---- agent budget: fail BEFORE the first dispatch (spec criterion 7 +
// adversarial finding 1b). Diagram target counts are only known after Map +
// Flows, so the pre-dispatch check reserves `diagramReserve` batches; at
// Diagram time the batch size WIDENS (up to maxDiagramBatch) to fit whatever
// budget remains, so a run that passed pre-dispatch cannot die mid-flight at
// the Diagram phase unless even the widened batches cannot fit.
const MAX_AGENTS = args.maxAgents == null ? 32 : args.maxAgents
const DIAGRAM_BATCH = args.diagramBatch == null ? 8 : args.diagramBatch
const DIAGRAM_RESERVE = args.diagramReserve == null ? 2 : args.diagramReserve
const MAX_DIAGRAM_BATCH = args.maxDiagramBatch == null ? 12 : args.maxDiagramBatch
for (const [name, v, min] of [['maxAgents', MAX_AGENTS, 1], ['diagramBatch', DIAGRAM_BATCH, 1],
  ['diagramReserve', DIAGRAM_RESERVE, 0], ['maxDiagramBatch', MAX_DIAGRAM_BATCH, 1]]) {
  if (!Number.isInteger(v) || v < min) {
    throw new Error(`args.${name} must be an integer >= ${min}, got ${v}`)
  }
}
if (MAX_DIAGRAM_BATCH < DIAGRAM_BATCH) {
  throw new Error(`args.maxDiagramBatch (${MAX_DIAGRAM_BATCH}) must be >= diagramBatch (${DIAGRAM_BATCH})`)
}
const BASE_AGENTS = UNITS.length + ROLLUP_PARENTS.length + 3 // map + multi-child parents + flows/tours/index
if (BASE_AGENTS + DIAGRAM_RESERVE > MAX_AGENTS) {
  throw new Error(`agent budget exceeded BEFORE dispatch: ${UNITS.length} map + ${ROLLUP_PARENTS.length} multi-child parent (${COLLAPSED_PARENTS.length} single-child collapsed, zero cost) + 3 synthesis + ${DIAGRAM_RESERVE} reserved diagram batch(es) = ${BASE_AGENTS + DIAGRAM_RESERVE} agents > --max-agents ${MAX_AGENTS}. Narrow the communities --band or raise the ceiling deliberately.`)
}
let agentsDispatched = 0
const recordFiles = []

// ---- Map: one agent per unit → records FILE + light manifest --------------
phase('Map')
const mapped = (await parallel(
  UNITS.map((u) => () => {
    const recordsPath = recordsPathFor(`map-${u.key}`)
    const context = {
      autodocName, taxonomyDocPath, repoName, repoPath,
      unitKey: u.key,
      recordsPath,
      communitiesLine: u.communities.map((c) => `${c.id}:"${c.label}"`).join(', '),
      communities: u.communities.map((c) => {
        const d = c.descriptor || {}
        const syms = communitySymbols[String(c.id)]
        return {
          id: c.id,
          label: c.label,
          memberCount: d.member_count != null ? d.member_count : syms.length,
          topPrefixes: (d.top_prefixes || []).join(', '),
          topSymbols: (d.top_symbols || []).join(', '),
          symbols: syms,
        }
      }),
    }
    agentsDispatched += 1
    return agent(renderPrompt('map', context), { label: `map:${u.key}`, phase: 'Map', schema: PROMPTS.map.schema })
  })
)).filter(Boolean)

const pagesMeta = mapped.flatMap((m) => m.pagesMeta || [])
const communityNotes = mapped.map((m) => `- [${m.unit}] ${m.notes || ''}`).join('\n')
for (const m of mapped) {
  if (!m.path) throw new Error(`map result for ${m.unit} carries no records path`)
  recordFiles.push(m.path)
}
log(`Mapped ${mapped.length}/${UNITS.length} work units → ${pagesMeta.length} page records on disk`)

// Fail early on a community that ended Map without a report (criterion 4 —
// better here than at the records CLI, while the failing unit is known).
const reportIds = new Set(mapped.flatMap((m) => m.reportIds || []))
for (const u of UNITS) {
  for (const c of u.communities) {
    if (!reportIds.has(c.id)) {
      throw new Error(`community ${c.id} ("${c.label}", unit ${u.key}) ended the Map phase with NO report — every fan-out community must be summarized in the same call`)
    }
  }
}

// ---- Parent Reports: multi-child parents only, children-first -------------
// Each parent agent READS its children's record files (agents have file
// tools; the workflow sandbox does not). A collapsed single-child parent has
// no file of its own — its effective report is its (transitive) child's, so
// the file reference resolves through the chain.
phase('Parent Reports')
const unitOfCommunity = new Map()
for (const u of UNITS) for (const c of u.communities) unitOfCommunity.set(c.id, u.key)
const parentById = new Map(PARENTS.map((p) => [p.id, p]))
const summarizedParents = new Set()
function reportFileRef(cid) {
  if (unitOfCommunity.has(cid)) {
    return { communityId: cid, path: recordsPathFor(`map-${unitOfCommunity.get(cid)}`) }
  }
  const p = parentById.get(cid)
  if (!p) throw new Error(`community ${cid} is neither a unit community nor a known parent — the parents chain is out of sync`)
  if (p.children.length === 1) return reportFileRef(p.children[0]) // collapsed: inherits the child's report
  if (!summarizedParents.has(cid)) {
    throw new Error(`parent community ${cid} ("${p.label}") is needed as a child before it was summarized — parents must be listed children-first`)
  }
  return { communityId: cid, path: recordsPathFor(`parent-${cid}`) }
}
for (const p of ROLLUP_PARENTS) {
  const recordsPath = recordsPathFor(`parent-${p.id}`)
  const childReportFiles = p.children.map((cid) => {
    const ref = reportFileRef(cid)
    return { label: String(cid), communityId: ref.communityId, path: ref.path }
  })
  agentsDispatched += 1
  const result = await agent(
    renderPrompt('parent-report', {
      autodocName, communityId: p.id, label: p.label, recordsPath, childReportFiles,
    }),
    { label: `parent-report:${p.id}`, phase: 'Parent Reports', schema: PROMPTS['parent-report'].schema }
  )
  if (!result || !result.path || result.communityId !== p.id) {
    throw new Error(`parent community ${p.id} ("${p.label}") returned no usable report manifest`)
  }
  recordFiles.push(result.path)
  summarizedParents.add(p.id)
}
log(`Parent reports: ${ROLLUP_PARENTS.length} summarized by agents, ${COLLAPSED_PARENTS.length} single-child parent(s) collapse (inherit at records-write time)`)

// ---- Flows ----------------------------------------------------------------
phase('Flows')
const catalog = pagesMeta
  .map((p) => `${p.type} :: ${p.title} :: ${(p.source_paths || [])[0] || ''}`)
  .join('\n')
agentsDispatched += 1
const flowsRecordsPath = recordsPathFor('flows')
const flowResult = await agent(
  renderPrompt('flows', {
    autodocName, taxonomyDocPath, repoName, repoPath,
    recordsPath: flowsRecordsPath,
    catalog, notes: communityNotes,
  }),
  { label: 'flows', phase: 'Flows', schema: PROMPTS.flows.schema }
)
const flowsMeta = (flowResult && flowResult.flowsMeta) || []
if (flowResult && flowResult.path) recordFiles.push(flowResult.path)
log(`Synthesized ${flowsMeta.length} flow records`)

// ---- Diagram: batched targets; batch size widens to fit the budget --------
phase('Diagram')
const dataModelPages = pagesMeta.filter((p) => p.type === 'data-model')
const diagramTargets = flowsMeta.map((f) => ({
  kind: 'flow', title: f.title,
  kindGuide: 'a sequenceDiagram (service/actor interactions in narrative order) — or a flowchart for a branching pipeline',
  sourcePaths: (f.source_paths || []).join(', '),
})).concat(dataModelPages.map((d) => ({
  kind: 'data-model', title: d.title,
  kindGuide: 'an erDiagram (the schema + its relationships to sibling models)',
  sourcePaths: (d.source_paths || []).join(', '),
})))
// remaining budget: total ceiling minus what ran, minus tours + index to come
const budgetLeft = MAX_AGENTS - agentsDispatched - 2
let batchSize = DIAGRAM_BATCH
if (diagramTargets.length > 0) {
  const needed = Math.ceil(diagramTargets.length / batchSize)
  if (needed > budgetLeft) {
    if (budgetLeft < 1) {
      throw new Error(`agent budget exhausted before the Diagram dispatch: ${agentsDispatched} dispatched + 2 (tours/index) leaves no room for ${diagramTargets.length} diagram target(s) under --max-agents ${MAX_AGENTS}`)
    }
    batchSize = Math.ceil(diagramTargets.length / budgetLeft) // widen to fit
    if (batchSize > MAX_DIAGRAM_BATCH) {
      throw new Error(`agent budget exceeded before the Diagram dispatch: ${diagramTargets.length} target(s) need batches of ${batchSize} to fit the remaining ${budgetLeft} agent slot(s), over the --max-diagram-batch cap ${MAX_DIAGRAM_BATCH}. Raise --max-agents or the cap deliberately.`)
    }
    log(`Diagram batch widened ${DIAGRAM_BATCH} → ${batchSize} to fit the remaining budget (${budgetLeft} slot(s))`)
  }
}
const batches = []
for (let i = 0; i < diagramTargets.length; i += batchSize) {
  batches.push(diagramTargets.slice(i, i + batchSize))
}
log(`Diagram phase: ${diagramTargets.length} targets in ${batches.length} batch(es) of ≤${batchSize} (${flowsMeta.length} flows + ${dataModelPages.length} data-models)`)

const diagramResults = batches.length
  ? await parallel(batches.map((batch, i) => () => {
      agentsDispatched += 1
      return agent(
        renderPrompt('diagram', {
          repoPath, scratchPath, validateMermaidCmd, commitSha,
          recordsPath: recordsPathFor(`diagrams-${i + 1}`),
          targets: batch,
        }),
        { label: `diagram:batch-${i + 1}`, phase: 'Diagram', schema: PROMPTS.diagram.schema }
      )
    }))
  : []
let diagramsEmbedded = 0
let diagramsOmitted = 0
for (const r of diagramResults.filter(Boolean)) {
  if (r.path) recordFiles.push(r.path)
  diagramsEmbedded += r.embedded || 0
  diagramsOmitted += r.omitted || 0
}
log(`Diagram phase: ${diagramsEmbedded}/${diagramTargets.length} diagrams validated (${diagramsOmitted} omitted with reasons)`)
// Quality wall (re-review item 2): omission is the documented per-diagram
// escape hatch, but a phase quietly dropping most of its diagrams — the
// widened-batch failure mode — must be loud, not a footnote in a log line.
const OMISSION_RATE_LIMIT = 0.30
if (diagramTargets.length > 0 && diagramsOmitted / diagramTargets.length > OMISSION_RATE_LIMIT) {
  throw new Error(`diagram omission rate ${diagramsOmitted}/${diagramTargets.length} = ${Math.round((diagramsOmitted / diagramTargets.length) * 100)}% exceeds the ${OMISSION_RATE_LIMIT * 100}% limit across ${batches.length} batch(es) of ≤${batchSize} — the phase is quietly failing; inspect the omission reasons in the diagram record files and re-run with more agent budget (larger --max-agents or smaller batches) instead of shipping a mostly-diagramless tree`)
}

// ---- Tours ----------------------------------------------------------------
phase('Tours')
agentsDispatched += 1
const toursRecordsPath = recordsPathFor('tours')
const tourResult = await agent(
  renderPrompt('tours', {
    autodocName, taxonomyDocPath, catalog,
    recordsPath: toursRecordsPath,
    flowsList: flowsMeta.map((f) => `flow :: ${f.title}`).join('\n'),
    tourOrderPath,
  }),
  { label: 'tours', phase: 'Tours', schema: PROMPTS.tours.schema }
)
const toursMeta = (tourResult && tourResult.toursMeta) || []
if (tourResult && tourResult.path) recordFiles.push(tourResult.path)
log(`Synthesized ${toursMeta.length} tour records`)

// ---- Index: the three maintained meta pages (nav renders deterministically)
phase('Index')
const allMeta = pagesMeta
  .concat(flowsMeta.map((f) => ({ file: f.file, type: 'flow', title: f.title })))
  .concat(toursMeta.map((t) => ({ file: t.file, type: 'tour', title: t.title })))
const byType = {}
for (const p of allMeta) (byType[p.type] = byType[p.type] || []).push(p)
const typeSummary = Object.keys(byType)
  .map((t) => `${t}: ${byType[t].length}`)
  .join(', ')
agentsDispatched += 1
const indexResult = await agent(
  renderPrompt('index', {
    autodocName, taxonomyDocPath, repoName,
    recordsPath: recordsPathFor('index'),
    catalog: allMeta.map((p) => `${p.file} :: ${p.type} :: ${p.title}`).join('\n'),
    typeSummary,
  }),
  { label: 'index', phase: 'Index', schema: PROMPTS.index.schema }
)
const indexFiles = (indexResult && indexResult.files) || []
if (indexResult && indexResult.path) recordFiles.push(indexResult.path)
if (indexFiles.length !== 3) {
  throw new Error(`index agent produced ${indexFiles.length} meta page(s) — overview.md, Start Here.md, and Glossary.md are all required`)
}

// ---- return: a LIGHT manifest; the records live under runDir --------------
const result = {
  repoName,
  commitSha,
  date,
  agentsDispatched,
  maxAgents: MAX_AGENTS,
  collapsedParents: COLLAPSED_PARENTS.map((p) => p.id),
  unitsMapped: mapped.length,
  unitsTotal: UNITS.length,
  pagesByType: Object.keys(byType).reduce((o, k) => ((o[k] = byType[k].length), o), {}),
  totalPages: allMeta.length,
  flows: flowsMeta.map((f) => f.title),
  tours: toursMeta.map((t) => t.title),
  diagrams: { targets: diagramTargets.length, embedded: diagramsEmbedded, omitted: diagramsOmitted, batchSize },
  records: { runDir: RUN_DIR, files: recordFiles },
}
// Defensive size guard (adversarial finding 5): the return is a manifest; if
// bulk data ever leaks back inline, fail loudly instead of shipping megabytes
// through the workflow return channel.
const RETURN_BUDGET_BYTES = 262144
const payloadSize = JSON.stringify(result).length
if (payloadSize > RETURN_BUDGET_BYTES) {
  throw new Error(`workflow return payload is ${payloadSize} bytes > ${RETURN_BUDGET_BYTES} — records must stay on disk under runDir; something inlined bulk data`)
}
return result
