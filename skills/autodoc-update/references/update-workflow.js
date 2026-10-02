export const meta = {
  name: 'autodoc-update',
  description: 'Incrementally refresh the autodoc code wiki from new commits: update affected pages, add new entities, flag deletions',
  phases: [
    { title: 'Refresh', detail: 'one agent per changed community: update affected pages, add new, flag removed' },
    { title: 'Audit', detail: 'one agent re-verifies a rolling sample of diff-untouched pages against source' },
    { title: 'Reconcile', detail: 'fix sub-indexes, master index, overview, touched flows, and touched tours' },
  ],
}

// ---- args (passed by the /autodoc-update skill) ---------------------------
// {
//   prompts:  REQUIRED — `node <autodoc-init>/references/prompts/render-prompt.js --dump`
//             (this workflow consumes the `refresh` entry: single-sourced prompt
//             body + return schema; the sandbox cannot import modules, so the
//             skill passes the dump and the engine is inlined below),
//   runDir:   REQUIRED (epic 706) — the writable run directory refresh agents write
//             their records files under, as <runDir>/records/refresh-<key>.json. Same
//             disk-based records contract the init path uses: the agent's return is a
//             light manifest and the records themselves never travel through it.
//   repoPath, autodocPath, repoName, fromSha, toSha, date, taxonomyDocPath,
//   changedCommunities: [ { key, label, descriptor: {member_count, top_symbols, top_prefixes},
//                     changedFiles: [repoRelPath], affectedPages: [autodocRelPath],
//                     symbols: [ {name,file,line,kind?} ] (changed-file symbols, from the
//                     communities artifact regenerated at TO — `kind` is forwarded when
//                     present (gh#527: a documents citation is keyed on file+kind+name, so
//                     the agent must copy it, not guess it); other extra keys such as the
//                     coverage slice's `community` are dropped; name + file:line is the
//                     required identity),
//                     checklistPath + symbolCount (optional, E-0743): the same checklist
//                     delivered BY PATH instead of inline, written by
//                     build-changed-communities.py --checklist-dir. Present => the refresh
//                     prompt tells the agent to READ that file and the rows are NOT
//                     interpolated into the prompt text, which is what keeps a large
//                     catch-up run under the workflow payload cap,
//                     contextPages (optional): [{page, owner}] pages that cite this
//                     unit's files but another unit owns, shown read-only; a symbols row
//                     may carry coveredBy, the one of those pages that cites its file } ],
//   deletedFiles: [repoRelPath],
//   auditPages: [ { page: autodocRelPath, source_paths: [repoRelPath], finding?: string } ]
//                (optional — skill Step 3.7 rolling sample of diff-untouched pages; entries drawn
//                from a Review Backlog file carry the parked finding text as `finding`;
//                omit/empty to skip the Audit phase),
//   backlogFile: autodocRelPath of the Review Backlog file the findings came from (optional),
//   ucAttachments: [ { flowPage: autodocRelPath, source_paths: [repoRelPath], ucs: ["UC-0147", ...] } ]
//                (optional — skill Step 3.8: flow pages that gained UC links from Use-Case: commit
//                trailers; the Reconcile agent writes use_cases:/## Use Cases and reconciles per-UC
//                Status against code via the liveness protocol; omit/empty to skip UC reconcile),
// }
const {
  runDir,
  repoPath, autodocPath, repoName, fromSha, toSha, date, taxonomyDocPath,
  changedCommunities, deletedFiles, auditPages, backlogFile, ucAttachments,
  prompts,
} = (typeof args === 'string' ? JSON.parse(args) : args)  // driver may deliver args stringified (live-smoke 2026-08-07)
// epic 706: refresh agents emit RECORDS to disk instead of editing the live
// tree, so a run without a records directory has nowhere to put its output.
// Fail here rather than letting every agent invent a path of its own.
if (!runDir) {
  throw new Error('args.runDir is required (epic 706) — the writable run directory refresh agents write their records files under')
}
const RUN_DIR = String(runDir).replace(/[\\/]+$/, '')
// THIS IS THE ONLY PLACE THE GUARD CAN LIVE, and that is a finding rather than a preference.
// On Windows the harness's file tools resolve a POSIX `/tmp/...` to `C:\tmp` while Git Bash
// resolves it to the user temp dir. `C:\tmp` exists, so a POSIX runDir sends every refresh agent
// to a directory the runbook never reads: they report success, `recordsPathFor` agrees with what
// they returned, and the step after the fan-out fails claiming a stale paste.
// The runbook cannot check this for itself. MEASURED while trying: MSYS rewrites such a path in
// argv AND in the environment, so every route bash has hands a native tool an ALREADY-NATIVE
// string — a shell-side check reports success whatever runDir holds. Only a value that never
// passed through the shell arrives raw, and the hand-baked Workflow args are exactly that, which
// is why the check belongs here, on the value as received.
//
// IT ASKS THE ARGS, NOT THE RUNTIME, AND THAT IS THE SECOND LESSON. The first version read
// `process.platform`. This workflow does not run under Node — the sandbox is Bun-based and has no
// `process` at all, so that line threw `ReferenceError: process is not defined` and killed the run
// before a single agent started: strictly worse than the defect it was added for. It passed its
// tests because the tests STUB `process.platform`, which only exists to be stubbed under Node -
// the guard was verified in an environment it never executes in.
// So judge COHERENCE between the paths instead. `autodocPath` comes from resolved config and is
// always in the platform's own shape, so a drive-lettered autodocPath beside a POSIX-absolute
// runDir means the two are in different path universes and the agents cannot write where the
// runbook reads. That needs no runtime introspection, works under any engine, and on a POSIX box
// both are `/`-rooted so it correctly stays silent.
const _driveRooted = (p) => /^[A-Za-z]:/.test(String(p || ''))
const _posixRooted = (p) => /^\//.test(String(p || ''))
if (_driveRooted(autodocPath) && _posixRooted(RUN_DIR)) {
  throw new Error(
    `args.runDir (${RUN_DIR}) is POSIX-rooted while autodocPath (${autodocPath}) is drive-rooted — ` +
    `refresh agents write runDir through native file tools, which resolve a leading "/" under ` +
    `C:\\tmp rather than the directory the runbook reads, so the records would land where nothing ` +
    `looks for them. Mint runDir from the native temp dir (autodoc-update/SKILL.md Step 4) and ` +
    `re-bake the args.`
  )
}
const recordsPathFor = (key) => `${RUN_DIR}/records/refresh-${key}.json`
// Review finding 4: a stale caller passing no changedCommunities must not
// produce a zero-agent Refresh that "succeeds" and advances last_sha. An
// EXPLICIT empty array stays legal (changes exist but map to no community).
if (changedCommunities === undefined || changedCommunities === null) {
  throw new Error('args.changedCommunities is required — build it from the communities artifact regenerated at TO (autodoc-update SKILL.md Step 3.5); pass [] explicitly only when the diff maps to no community')
}
// Folder name derived from the configured autodoc root — never hardcoded.
const autodocName = autodocPath.replace(/[\\/]+$/, '').split(/[\\/]/).pop()

// The refresh prompt body + return schema are single-sourced in the
// autodoc-init skill's prompt module (epic 704 scope 1 — 707's Python driver
// renders the same text via render-prompt.js, no fork).
const PROMPTS = prompts
if (!PROMPTS || !PROMPTS.refresh || !PROMPTS.refresh.template || !PROMPTS.refresh.schema) {
  throw new Error('args.prompts is required — pass the output of `node <autodoc-init>/references/prompts/render-prompt.js --dump` (this workflow consumes the refresh entry)')
}
const REFRESH_SCHEMA = PROMPTS.refresh.schema

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

function refreshPrompt(a) {
  const d = a.descriptor || {}
  return renderTemplate(PROMPTS.refresh.template, {
    autodocName, taxonomyDocPath, repoName, repoPath, autodocPath,
    recordsPath: recordsPathFor(a.key),
    fromShort: fromSha.slice(0, 8),
    toShort: toSha.slice(0, 8),
    communityKey: a.key,
    label: a.label || a.key,
    topPrefixes: (d.top_prefixes || []).join(', '),
    topSymbols: (d.top_symbols || []).join(', '),
    changedFiles: a.changedFiles || [],
    affectedPages: a.affectedPages || [],
    // Pages another unit owns that cite this unit's files; without them the
    // agent reads "no pages" as "all-new code" and duplicates what they document.
    contextPages: (a.contextPages || []).map((c) => ({ page: c.page, owner: c.owner })),
    // exactly the shape the refresh template consumes ({name,file,line,kind}) —
    // callers pass the communities-artifact shape (with `kind`) or the
    // coverage-slice shape (extra `community`, no `kind`); consumer break
    // 2026-08-07: forwarding items verbatim let the strict engine die on
    // {{kind}} for every caller that didn't happen to carry that key; the
    // template now prints it only under {{#if kind}}, and '' keeps the row shape
    // uniform when a caller (the coverage slice) carries no kind. It is forwarded at all because a documents citation
    // is keyed on file+kind+name and a guessed kind refuses the batch (gh#527).
    symbols: (a.symbols || []).map((s) => ({ name: s.name, file: s.file, line: s.line, kind: s.kind || '', coveredBy: s.coveredBy || '' })),
    // E-0743: the same checklist can arrive BY PATH instead of inline — a
    // catch-up run's 4636 rows are 94.9% of a 535KB payload against a 512KB
    // cap. Both are passed through; the template branches on checklistPath and
    // renders the inline rows only when it is empty, so a caller that sends
    // neither, only symbols, or both still gets exactly one checklist.
    checklistPath: a.checklistPath || '',
    symbolCount: a.symbolCount || 0,
    deletedFiles: deletedFiles || [],
    date,
  })
}

phase('Refresh')
const refreshed = (await parallel(
  changedCommunities.map((a) => () =>
    agent(refreshPrompt(a), { label: `refresh:${a.key}`, phase: 'Refresh', schema: REFRESH_SCHEMA })
  )
)).filter(Boolean)

const created = refreshed.flatMap((r) => r.created || [])
const updated = refreshed.flatMap((r) => r.updated || [])
const removed = refreshed.flatMap((r) => r.removed || [])
const covered = refreshed.flatMap((r) => (r.covered || []).map((c) => ({ community: c.community, name: c.name, line: c.line })))
const excluded = refreshed.flatMap((r) => (r.excluded || []).map((c) => ({ community: c.community, name: c.name, line: c.line, reason: c.reason })))
const notes = refreshed.map((r) => `- [${r.community}] ${r.notes || ''}`).join('\n')
// epic 706: the records MANIFEST. `load_records_input --manifest` treats the
// workflow's file list as authoritative and makes any *.json on disk that is
// NOT in it a loud error -- an orphan from a prior attempt at the same runDir
// would otherwise be swept silently into the store. The list therefore comes
// from what the agents RETURNED, never from globbing the directory: globbing
// would re-admit exactly the orphan the manifest exists to catch.
const recordsFiles = refreshed.map((r) => r.path).filter(Boolean)
if (recordsFiles.length !== refreshed.length) {
  const missing = refreshed.filter((r) => !r.path).map((r) => r.community)
  throw new Error(`refresh agent(s) for community ${missing.join(', ')} returned no records path — a refresh that wrote no records file has documented nothing, and proceeding would advance last_sha over an unrecorded range`)
}
// Each agent was handed exactly one path; a return naming a different file means
// the agent wrote somewhere `graph records` will never read.
for (const r of refreshed) {
  const expected = recordsPathFor(r.community)
  if (r.path !== expected) {
    throw new Error(`refresh agent for community ${r.community} returned records path ${r.path}, expected ${expected}`)
  }
}
log(`Refresh: ${updated.length} updated, ${created.length} created, ${removed.length} deprecated, ${recordsFiles.length} records file(s)`)

// ---- Audit: rolling re-verify of diff-untouched pages ----------------------
// Runs AFTER Refresh (so it never races a community agent on a page) and BEFORE
// Reconcile (so audit fixes land in the reconciled nav layer).
// epic 706: the audit no longer EDITS pages. It re-verifies (read-only) and
// emits a record for anything it corrects, exactly like refresh. `path` names
// the records file; the bodies stay on disk and never ride the return.
const AUDIT_SCHEMA = {
  type: 'object',
  required: ['audited', 'fixed', 'findings', 'path'],
  properties: {
    audited: { type: 'array', items: { type: 'string' }, description: 'autodoc-relative paths re-verified (all of them)' },
    fixed: { type: 'array', items: { type: 'string' }, description: 'the subset re-emitted as corrected records' },
    findings: { type: 'array', items: { type: 'string' }, description: 'one line per defect found (page — what was wrong)' },
    path: { type: 'string', description: 'the records file written (must equal the recordsPath given in the prompt)' },
  },
}

phase('Audit')
let audit = { audited: [], fixed: [], findings: [], path: null }
// One audit agent, so one records file. Same runDir/records convention as the
// per-community refresh files, so `graph records` ingests the whole directory
// in one pass and the manifest covers both phases.
const auditRecordsPath = recordsPathFor('audit')
if ((auditPages || []).length) {
  const recordsPath = auditRecordsPath
  const auditResult = await agent(
    `You AUDIT pages of the "${autodocName}" code wiki that were NOT touched by this run's diff —
a rolling re-verify that catches stale or hallucinated claims the incremental refresh never revisits.

STEP 1 — Read the taxonomy contract IN FULL: ${taxonomyDocPath}.

STEP 2 — Audit each of these pages (autodoc-relative path + the source files it documents):
${auditPages.map((p) => `  - ${p.page}  ←  ${(p.source_paths || []).join(', ') || '(no source_paths in manifest — use the page frontmatter)'}${p.finding ? `\n      PARKED FINDING to verify FIRST: ${p.finding}` : ''}`).join('\n')}

Follow the taxonomy's "Evidence Protocols" and "Liveness protocol" sections on every page.
A page with a "PARKED FINDING" line came from the review backlog: verify THAT claim against
source first (fix if confirmed; leave the page alone if refuted), then continue the normal audit.

For EACH page, in order:
  1. Read the page at ${autodocPath}/<page>.
  2. Open its source files under ${repoPath}/ (the page's source_paths).
  3. Re-verify EVERY contract claim — routes, request/response fields, auth, invariants,
     side effects — with the same verification discipline as a refresh:
     - transcribe fields from the actual models/return statements, never from names;
     - never characterize a cited test — QUOTE its docstring;
     - cross-service claims and any page carrying 'contract_with:' frontmatter: open BOTH
       sides' source and confirm the shapes actually match; document any mismatch explicitly
       on the page instead of papering over it.
  4. If anything is wrong or stale: EMIT A FULL RECORD for that page, carrying the correction.
     A record REPLACES the page, so carry forward everything still true — anything you omit is
     dropped. Keep source_paths accurate. Do NOT bump 'updated' yourself: the renderer owns it.
  5. Either way, the page needs 'audited: ${date}'. For a page you corrected, put it in that
     record's 'frontmatter'. For a page you found ACCURATE, emit a record that is otherwise
     identical to what the page already says, carrying only the new 'audited' stamp — the store
     has no partial update, so a stamp with no record is a stamp that never lands.

CRITICAL: your ONLY write is the records JSON at ${recordsPath}, a JSON OBJECT of the same shape
refresh emits: { "pages": [ <page record>, ... ] }. Both ${repoPath}/ and ${autodocPath}/ are
READ-ONLY to you — the renderer owns that tree, and an edit made there is undone by the next
render. Do not emit records for index.md / _index.md / overview.md (Reconcile's job; and
'graph records' rejects a record claiming a reserved basename outright). Never write
tool-call/XML wrapper syntax (</content>, </invoke>, <parameter, etc.) into a body.

${backlogFile ? `BACKLOG BOOKKEEPING: the parked findings above live in ${autodocPath}/${backlogFile}.
That file is NOT a rendered page and holds no record — it is one of the human-owned files the
renderer never produces. Report each one you resolved (fixed OR refuted) in 'findings' as
"BACKLOG <bullet text> — [fixed|refuted]"; the skill edits ${backlogFile} after the render, in
the one place that still owns it. Do not edit it here: this phase writes records only, and a
write into the tree mid-run is the exact hazard the conversion removes.` : ''}

Return: 'path' (equal to ${recordsPath}), audited (every page you stamped), fixed (the subset you
re-emitted as corrected records), findings (one line per defect: "page — what was wrong").`,
    { label: 'audit', phase: 'Audit', schema: AUDIT_SCHEMA }
  )
  if (auditResult) {
    audit = {
      audited: auditResult.audited || [],
      fixed: auditResult.fixed || [],
      findings: auditResult.findings || [],
      path: auditResult.path || null,
    }
    // Same contract as refresh: a returned path that is not the one handed out
    // means the agent wrote where `graph records` will never read, which would
    // look like a clean audit while losing every correction it made.
    if (audit.path !== auditRecordsPath) {
      throw new Error(`audit agent returned records path ${audit.path}, expected ${auditRecordsPath}`)
    }
    recordsFiles.push(audit.path)
  }
  log(`Audit: ${audit.audited.length} audited, ${audit.fixed.length} re-emitted as corrected records`)
}

// ---- Reconcile the navigation layer + touched flows + touched tours -------
// UC ingestion + status reconcile (skill Step 3.8): flow pages that gained UC links from
// Use-Case: commit trailers. The Reconcile agent writes them into use_cases:/## Use Cases and
// reconciles per-UC Status against code (completed if the activation chain is live, deprecated if
// gone) — never copying plan/registry status. Empty list => task 9 is a no-op.
const ucList = ucAttachments || []
const ucBlock = ucList.length
  ? ucList.map((a) => `  - ${a.flowPage}  (source_paths: ${(a.source_paths || []).join(', ') || 'see page frontmatter'})\n      UCs: ${a.ucs.join(', ')}`).join('\n')
  : '  (none — no Use-Case: trailers touched a flow page this run)'

phase('Reconcile')
// epic 706: Reconcile emits records too — flow/tour page records plus meta
// records for overview/Glossary/Start Here. The nav layer it used to hand-edit
// is now rendered deterministically from the store, so most of this phase's
// old work simply has no agent left to do it.
const reconcileRecordsPath = recordsPathFor('reconcile')
const recordsPath = reconcileRecordsPath
const reconcileSummary = await agent(
  `You reconcile the ${autodocName} navigation layer after an incremental refresh.

Read the taxonomy: ${taxonomyDocPath}.

This refresh produced:
  Created pages: ${created.map((p) => `${p.file} (${p.type}: ${p.title})`).join('; ') || '(none)'}
  Updated pages: ${updated.concat(audit.fixed).join('; ') || '(none)'}
  Deprecated pages: ${removed.join('; ') || '(none)'}
  Mapper notes (flow candidates, cross-community deps):
${notes}

Use-Case attachments (Step 3.8 — flow pages that gained UC links from Use-Case: commit trailers):
${ucBlock}

YOUR ONLY WRITE IS THE RECORDS JSON AT ${recordsPath} — a JSON OBJECT of the shape
{ "pages": [ <page record>, ... ], "meta_pages": [ {file, title, body}, ... ] }. ${autodocPath}/ is
READ-ONLY to you; the renderer owns that tree.

THE NAV LAYER IS NO LONGER YOURS, AND THAT IS MOST OF WHAT THIS PHASE USED TO DO. The renderer
generates every <folder>/_index.md and index.md deterministically from the stored pages — links,
one-clause summaries and counts included — so the old tasks 1, 2, 6 and 8 (folder indexes, master
catalog, nav-count audit, nav freshness stamps) are GONE. Do not attempt them: those basenames are
reserved and 'graph records' rejects a record claiming one. overview.md, Start Here.md and
Glossary.md are META records, below.

Tasks:
1. META PAGES — emit a meta record ONLY for one that actually needs to change:
   - overview.md, if a service/endpoint/process changed materially or a new one appeared and the
     architecture summary no longer reads true (light touch — only if the big picture moved).
   - Glossary.md, if a newly-created page introduces NEW invented domain vocabulary (a project
     coinage, not a standard industry term).
   - Start Here.md, if any page's 'status:' changed this run, or the product's user-visible
     capabilities materially changed — its live/legacy state list must name every retired/legacy
     subsystem, in past tense.
   A meta record REPLACES that page in full, so carry forward everything still true. Emit nothing
   for a meta page that does not need to change; its stored record re-renders byte-identically.
2. For each FLOW page in ${autodocPath}/flows/ that touches a changed/created/deprecated entity,
   re-read it and update the steps/links. Create a new flow page only if these commits introduced a
   genuinely new end-to-end narrative (per notes). Emit each as a full page record.
   FLOW BACKLINKS (taxonomy rule): for any flow you create or whose touched set changed, ensure
   every entrypoint/hub page the flow touches carries "[[<Flow Title>]]" in its 'related:' list
   (append-only edits to those pages).
3. For each TOUR page in ${autodocPath}/tours/ whose member/step pages were changed, created, or
   deprecated in this refresh, re-read it and regenerate it (mirror how flows are handled). A tour is a
   dependency-ordered onboarding reading sequence (foundational pages first), NOT a runtime narrative.
   Re-determine its member pages, then RE-ORDER the steps dependencies-first using this rule: **if a page
   links/builds on another page, the linked (foundational) page comes first.** The deterministic ordering
   reference lives in the autodoc-init skill at references/inventory/tour-order.js (buildTourGraph/orderTour);
   you MAY shell out to node to call orderTour for an exact order, OR order by that documented bottom-up
   wikilink rule. Update the 'steps:' frontmatter ("[[Wikilinks]]", dependencies first) and the body step
   sections/links to match; drop deprecated step pages, add newly-relevant ones. Create a NEW tour only if
   a genuinely new subsystem emerged (rare; per notes). Emit each as a full page record.
4. USE-CASE ingestion + status reconcile (flow pages only — the list above). For each attached
   flow page:
   a. INGEST — ensure every listed UC id appears in the page's 'use_cases:' frontmatter as an
      OPAQUE SCALAR ('UC-0147', never a "[[wikilink]]") and as a row in the '## Use Cases' table
      (columns: UC | Domain | Status | Summary), per the taxonomy. If the section is missing,
      create it immediately after the "What it does" section. Never emit a UC as a wikilink and
      never touch the page's relational-link frontmatter with it.
   b. RECONCILE Status by READING CODE via the taxonomy's liveness protocol — NOT by copying any
      plan or registry value. Open the flow's source_paths and locate the UC's activation chain
      (router mounted, component imported by live code, handler wired with real deps). Chain still
      present => Status 'completed'. Chain gone or unmounted => Status 'deprecated', and add a
      '> [!stale]' banner to the page naming the UC and the ${toSha.slice(0, 8)} range. This
      self-heals every run: returned code flips a UC back to 'completed', removed code flips it to
      'deprecated'.
   c. Domain + Summary columns: PRESERVE any value already on an existing row (it is
      human-gated / solution-sourced — do not overwrite). For a newly-attached UC, Summary =
      one plain sentence of what the UC does, derived from THIS flow's narrative for that
      activation chain; if not confidently derivable, leave '—'. Domain is not derivable from a
      commit trailer — leave '—' (Phase-5 enrichment fills it from the registry). Keep the
      'use_cases:' frontmatter list and the table rows in sync, inside that page's record.

A record REPLACES its page, so carry forward everything still true — a record is not a patch.
Never write 'created'/'updated'/'source_repo'/'related'/'depends_on' into 'frontmatter': all five
are renderer-owned and 'graph records' rejects a record carrying them. There is no 'updated:' to
bump — the renderer stamps it.

Return 'path' (equal to ${recordsPath}) and a one-line summary.`,
  {
    label: 'reconcile',
    phase: 'Reconcile',
    schema: {
      type: 'object',
      required: ['path'],
      properties: {
        summary: { type: 'string' },
        path: { type: 'string', description: 'the records file written (must equal the recordsPath given in the prompt)' },
      },
    },
  }
)

// Same contract as refresh and audit: a returned path that is not the one
// handed out means the records went where `graph records` will never read.
if (reconcileSummary) {
  if (reconcileSummary.path !== reconcileRecordsPath) {
    throw new Error(`reconcile agent returned records path ${reconcileSummary.path}, expected ${reconcileRecordsPath}`)
  }
  recordsFiles.push(reconcileSummary.path)
}

return {
  repoName,
  fromSha,
  toSha,
  date,
  communitiesRefreshed: refreshed.length,
  updated,
  created: created.map((p) => ({ file: p.file, type: p.type, title: p.title, source_paths: p.source_paths || [] })),
  removed,
  audit,
  reconcile: (reconcileSummary && reconcileSummary.summary) || '',
  recordsFiles,
  coverage: {
    pages: created.map((p) => ({ source_paths: p.source_paths || [] })),
    covered,
    excluded,
  },
}
