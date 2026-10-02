// emit-graph.js — repo walk → entities + edges → deterministic JSONL (epic 702).
//
// The extraction half of the autodoc graph core. Composes extract-symbols
// (entities) with extract-edges (per-file raw facts), does the cross-file
// resolution that needs the whole file set (import paths, imported-name call
// targets), and renders deterministic JSONL for the Python `autodoc_graph`
// ingester. Storage-agnostic by construction: this module writes bytes to
// disk and NEVER touches a database.
//
// Identity contract (spec §Identity): sym:<corpus>:<normalized_path>:<kind>:<name>
// — path-and-name keyed, content-independent, so re-runs update rather than
// duplicate. Same-name/same-kind collisions in one file keep the FIRST
// occurrence (by line) and are counted, never silently dropped.
//
// Resolution honesty: a call/route/import that cannot be resolved by
// same-file or imported-name matching is SKIPPED AND COUNTED in
// counts.skipped — deterministic-and-honest beats complete. Additional
// honesty rules (adversarial-review finding 3): self.X() resolves only
// within the ENCLOSING class; callee names shadowed by parameters or local
// assignments are dropped at extraction (counts.skipped.calls_shadowed).
//
// KNOWN RESIDUAL FALSE-POSITIVE CLASSES for `calls` edges (documented, not
// detected — all rare, and all still evidence-carrying so they are auditable):
//   * module-LEVEL rebinding of a defined name after its definition
//     (`def f(): ...` then `f = wrap(f)` at module scope) — the call edge
//     still points at the original definition site;
//   * dynamic/monkey patching and `del name` at runtime;
//   * conditional shadowing that never executes on the taken path.
// Consumers needing certainty must treat `calls` as "statically apparent",
// which is exactly what confidence=1.0/origin=ast asserts.
//
// JSONL contract (consumed by autodoc_graph/ingest.py):
//   {"type":"entity", id, tier:"symbol", kind, name, qualified_name,
//    source_paths:[path], span:{file,start_line,end_line}, summary, origin:"ast"}
//   {"type":"edge", rel, src_id, dst_id, origin:"ast", confidence:1.0,
//    evidence:{file,line}}
//   {"type":"meta", corpus, repo, sha, counts:{...}}          (trailer, LAST line)
// No timestamps anywhere: byte-identical output for identical inputs is the
// double-run no-op guarantee's first half.
import { readFileSync, writeFileSync } from 'node:fs';
import { relative, extname, basename, dirname, posix } from 'node:path';
import { loadParsers, LANG_BY_EXT } from './grammars.js';
import { extractSymbols } from './extract-symbols.js';
import { extractFileEdges } from './extract-edges.js';
import { walk } from './extract-inventory.js';

const RELS = ['imports', 'calls', 'defines', 'routes_to'];

const stemOf = (rel) => basename(rel, extname(rel));
const entityId = (corpus, rel, kind, name) => `sym:${corpus}:${rel}:${kind}:${name}`;

function moduleEntity(corpus, rel, lineCount) {
  return {
    type: 'entity',
    id: entityId(corpus, rel, 'module', stemOf(rel)),
    tier: 'symbol',
    kind: 'module',
    name: stemOf(rel),
    qualified_name: rel,
    source_paths: [rel],
    span: { file: rel, start_line: 1, end_line: Math.max(1, lineCount) },
    summary: null,
    origin: 'ast',
  };
}

function symbolEntity(corpus, rel, sym, sourceLines) {
  return {
    type: 'entity',
    id: entityId(corpus, rel, sym.kind, sym.name),
    tier: 'symbol',
    kind: sym.kind,
    name: sym.name,
    qualified_name: `${rel}:${sym.name}`,
    source_paths: [rel],
    span: { file: rel, start_line: sym.line, end_line: sym.end_line ?? sym.line },
    summary: (sourceLines[sym.line - 1] || '').trim() || null,
    origin: 'ast',
  };
}

// ---------------------------------------------------------------- import path resolution
// Python absolute imports resolve against the repo root (the repo convention);
// relative imports resolve against the importing file's package dir.
function resolvePythonSpec(spec, level, fromFile, fileIndex) {
  let baseDir = '';
  if (level > 0) {
    baseDir = posix.dirname(fromFile);
    for (let i = 1; i < level; i++) baseDir = posix.dirname(baseDir);
    if (baseDir === '.') baseDir = '';
  }
  const specPath = spec ? spec.split('.').join('/') : '';
  const root = baseDir && specPath ? `${baseDir}/${specPath}` : baseDir || specPath;
  if (!root) return null;
  for (const cand of [`${root}.py`, `${root}/__init__.py`]) {
    if (fileIndex.has(cand)) return cand;
  }
  return null;
}

const TS_EXT_CANDIDATES = ['', '.ts', '.tsx', '.js', '.jsx',
  '/index.ts', '/index.tsx', '/index.js', '/index.jsx'];

function resolveTsSpec(spec, fromFile, fileIndex) {
  if (!spec.startsWith('./') && !spec.startsWith('../')) return null; // bare = external
  const base = posix.normalize(posix.join(posix.dirname(fromFile), spec));
  for (const ext of TS_EXT_CANDIDATES) {
    if (fileIndex.has(base + ext)) return base + ext;
  }
  return null;
}

// ---------------------------------------------------------------- build
export async function buildGraph({ repoPath, corpus, sha = '', repoName = '', parsers = null }) {
  if (!repoPath || !corpus) throw new Error('emit-graph: repoPath and corpus are required');
  const ps = parsers ?? await loadParsers();

  // phase 1: per-file extraction
  const files = []; // { rel, lang, symbols, facts, lines }
  const skipped = {
    imports_unresolved: 0, calls_unresolved: 0, calls_ambiguous: 0,
    calls_shadowed: 0, routes_unresolved: 0, defines_unresolved: 0,
    id_collisions: 0, parse_failures: 0,
  };
  const walked = [...walk(repoPath)].sort(); // deterministic file order
  for (const full of walked) {
    const rel = relative(repoPath, full).replace(/\\/g, '/');
    const lang = LANG_BY_EXT[extname(full)];
    if (!ps[lang]) {
      throw new Error(
        `emit-graph: no parser loaded for language '${lang}' (file: ${rel}) — `
        + 'a missing grammar is an error, not a silent skip',
      );
    }
    const source = readFileSync(full, 'utf8');
    let symbols; let facts;
    try {
      symbols = extractSymbols({ lang, parsers: ps, source, file: rel });
      facts = extractFileEdges({ lang, parsers: ps, source, file: rel });
    } catch (err) {
      // one malformed file must not abort indexing a whole repo — but it is
      // counted and reported, never silently ignored.
      skipped.parse_failures += 1;
      console.error(`emit-graph: parse failure in ${rel}: ${err.message}`);
      continue;
    }
    files.push({ rel, lang, symbols, facts, lines: source.split(/\r?\n/) });
  }

  // phase 2: entities + per-file lookup tables
  const entities = new Map(); // id -> entity
  const fileIndex = new Set(files.map((f) => f.rel));
  const perFile = new Map(); // rel -> { moduleId, byNameLine, byName, parentOf }
  for (const f of files) {
    const mod = moduleEntity(corpus, f.rel, f.lines.length);
    entities.set(mod.id, mod);
    const byNameLine = new Map(); // `${name}:${line}` -> id
    const byName = new Map(); // name -> Set<id>
    const spanEntries = []; // { id, kind, line, end_line } for scope containment
    const addLookup = (name, line, id) => {
      byNameLine.set(`${name}:${line}`, id);
      if (!byName.has(name)) byName.set(name, new Set());
      byName.get(name).add(id);
    };
    const sortedSyms = [...f.symbols].sort((a, b) => a.line - b.line || a.name.localeCompare(b.name));
    for (const sym of sortedSyms) {
      const ent = symbolEntity(corpus, f.rel, sym, f.lines);
      if (entities.has(ent.id)) {
        skipped.id_collisions += 1; // first occurrence (lowest line) wins
      } else {
        entities.set(ent.id, ent);
        spanEntries.push({ id: ent.id, kind: ent.kind,
          line: ent.span.start_line, end_line: ent.span.end_line });
      }
      addLookup(sym.name, sym.line, ent.id);
    }
    // supplemental member entities (e.g. TS class methods extract-symbols skips)
    for (const d of f.facts.defines) {
      const key = `${d.member.name}:${d.member.line}`;
      if (byNameLine.has(key)) continue;
      const ent = symbolEntity(corpus, f.rel, {
        kind: d.member.kind, name: d.member.name,
        line: d.member.line, end_line: d.member.end_line,
      }, f.lines);
      if (entities.has(ent.id)) {
        skipped.id_collisions += 1;
      } else {
        entities.set(ent.id, ent);
        spanEntries.push({ id: ent.id, kind: ent.kind,
          line: ent.span.start_line, end_line: ent.span.end_line });
      }
      addLookup(d.member.name, d.member.line, ent.id);
    }
    // Innermost enclosing symbol per entity (span containment) — drives the
    // bare-call visibility rule (finding R2-3): a nested def is callable by
    // bare name only from inside its enclosing function.
    const parentOf = new Map(); // id -> spanEntry of innermost container | undefined
    for (const e of spanEntries) {
      let best = null;
      for (const p of spanEntries) {
        if (p.id === e.id) continue;
        if (p.line < e.line && p.end_line >= e.end_line) {
          if (!best || p.line > best.line) best = p; // latest start = innermost
        }
      }
      if (best) parentOf.set(e.id, best);
    }
    perFile.set(f.rel, { moduleId: mod.id, byNameLine, byName, parentOf });
  }

  // phase 3: import resolution + imported-name maps
  const edges = new Map(); // `${src}|${dst}|${rel}` -> edge (first evidence wins)
  const addEdge = (rel, srcId, dstId, file, line) => {
    if (!entities.has(srcId) || !entities.has(dstId)) return false;
    const key = `${srcId}|${dstId}|${rel}`;
    if (!edges.has(key)) {
      edges.set(key, {
        type: 'edge', rel, src_id: srcId, dst_id: dstId,
        origin: 'ast', confidence: 1.0, evidence: { file, line },
      });
    }
    return true;
  };

  const importedNames = new Map(); // rel -> Map(local -> {file, name|null})
  for (const f of files) {
    const local = new Map();
    importedNames.set(f.rel, local);
    for (const d of f.facts.importDecls) {
      let target = null;
      if (f.lang === 'python') {
        target = resolvePythonSpec(d.spec, d.level, f.rel, fileIndex);
        // `from pkg import name` where name is itself a module: pkg/name.py
        for (const n of d.names) {
          const sub = resolvePythonSpec(
            d.spec ? `${d.spec}.${n.imported}` : n.imported, d.level, f.rel, fileIndex,
          );
          if (sub) {
            local.set(n.local, { file: sub, name: null }); // module import
            addEdge('imports', perFile.get(f.rel).moduleId, perFile.get(sub).moduleId, f.rel, d.line);
          } else if (target) {
            local.set(n.local, { file: target, name: n.imported });
          }
        }
      } else {
        target = resolveTsSpec(d.spec, f.rel, fileIndex);
        if (target) {
          for (const n of d.names) local.set(n.local, { file: target, name: n.imported });
        }
      }
      if (target) {
        addEdge('imports', perFile.get(f.rel).moduleId, perFile.get(target).moduleId, f.rel, d.line);
        if (d.moduleAlias) local.set(d.moduleAlias, { file: target, name: null });
      } else if (d.names.every((n) => !local.has(n.local))) {
        skipped.imports_unresolved += 1; // external / stdlib / not-found: honest count
      }
    }
    skipped.imports_unresolved += f.facts.skipped.imports_unresolved;
    skipped.routes_unresolved += f.facts.skipped.routes_unresolved;
    skipped.calls_shadowed += f.facts.skipped.calls_shadowed;
  }

  // Visibility rule for bare-name references (finding R2-3): a top-level
  // symbol is visible everywhere in its file; a def nested inside a function
  // is visible ONLY at lines within that function's span; a class member is
  // never bare-visible (self/method resolution handles the real path).
  const visibleAt = (rel, id, ln) => {
    const parent = perFile.get(rel).parentOf.get(id);
    if (!parent) return true;
    if (parent.kind === 'class' || parent.kind === 'type' || parent.kind === 'component') {
      return false;
    }
    return ln >= parent.line && ln <= parent.end_line;
  };

  // unique VISIBLE id for `name` at line `ln` in `rel`, or null / 'AMBIGUOUS'
  const resolveVisible = (rel, name, ln) => {
    const t = perFile.get(rel);
    const set = t ? t.byName.get(name) : null;
    if (!set || set.size === 0) return null;
    const candidates = [...set].filter((id) => visibleAt(rel, id, ln));
    if (candidates.length === 0) return null;
    if (candidates.length > 1) return 'AMBIGUOUS';
    return candidates[0];
  };

  // unique TOP-LEVEL id for `name` in `rel` — cross-file references (imports,
  // route handlers by import) can only see a file's module-level names
  const resolveTopLevel = (rel, name) => {
    const t = perFile.get(rel);
    const set = t ? t.byName.get(name) : null;
    if (!set || set.size === 0) return null;
    const candidates = [...set].filter((id) => !t.parentOf.get(id));
    if (candidates.length === 0) return null;
    if (candidates.length > 1) return 'AMBIGUOUS';
    return candidates[0];
  };

  // phase 4: calls, defines, routes
  for (const f of files) {
    const table = perFile.get(f.rel);
    const local = importedNames.get(f.rel);
    const callerIdOf = (caller) => (caller
      ? table.byNameLine.get(`${caller.name}:${caller.line}`) ?? table.moduleId
      : table.moduleId);

    // defines
    for (const d of f.facts.defines) {
      let containerId = null;
      if (d.container.line != null) {
        containerId = table.byNameLine.get(`${d.container.name}:${d.container.line}`);
      } else {
        const u = resolveTopLevel(f.rel, d.container.name);
        containerId = u === 'AMBIGUOUS' ? null : u;
      }
      const memberId = table.byNameLine.get(`${d.member.name}:${d.member.line}`);
      if (!containerId || !memberId || !addEdge('defines', containerId, memberId, f.rel, d.line)) {
        skipped.defines_unresolved += 1;
      }
    }

    // calls — same-file, then imported-name; anything else is skipped + counted
    for (const c of f.facts.calls) {
      const srcId = callerIdOf(c.caller);
      let dstId = null;
      if (c.calleeObject === null) {
        const u = resolveVisible(f.rel, c.calleeName, c.line);
        if (u === 'AMBIGUOUS') { skipped.calls_ambiguous += 1; continue; }
        dstId = u;
        if (!dstId && local.has(c.calleeName)) {
          const imp = local.get(c.calleeName);
          const ru = imp.name === null
            ? null // calling a module object directly — not a symbol call
            : resolveTopLevel(imp.file, imp.name);
          if (ru === 'AMBIGUOUS') { skipped.calls_ambiguous += 1; continue; }
          dstId = ru;
        }
      } else if (c.calleeObject === 'self' && f.lang === 'python') {
        // finding 3a: resolve ONLY within the enclosing class — a same-named
        // method on a different class in the file must never be guessed
        if (c.enclosingClass) {
          const members = f.facts.defines.filter((d) => d.container.name === c.enclosingClass.name
            && d.container.line === c.enclosingClass.line
            && d.member.name === c.calleeName);
          if (members.length === 1) {
            dstId = table.byNameLine.get(`${members[0].member.name}:${members[0].member.line}`);
          } else if (members.length > 1) {
            skipped.calls_ambiguous += 1; continue;
          }
        }
      } else if (local.has(c.calleeObject)) {
        const imp = local.get(c.calleeObject);
        if (imp.name === null) { // module alias: u.util_fn() / m.f()
          const ru = resolveTopLevel(imp.file, c.calleeName);
          if (ru === 'AMBIGUOUS') { skipped.calls_ambiguous += 1; continue; }
          dstId = ru;
        }
      }
      if (!dstId || !addEdge('calls', srcId, dstId, f.rel, c.line)) {
        skipped.calls_unresolved += 1;
      }
    }

    // routes — src is the module (the route declaration's home), dst the handler
    for (const r of f.facts.routes) {
      let dstId = null;
      if (r.handlerLine != null) {
        dstId = table.byNameLine.get(`${r.handlerName}:${r.handlerLine}`);
      } else if (r.handlerName) {
        const u = resolveVisible(f.rel, r.handlerName, r.line);
        if (u && u !== 'AMBIGUOUS') {
          dstId = u;
        } else if (u !== 'AMBIGUOUS' && local.has(r.handlerName)) {
          const imp = local.get(r.handlerName);
          const ru = imp.name === null ? null : resolveTopLevel(imp.file, imp.name);
          dstId = ru === 'AMBIGUOUS' ? null : ru;
        }
      }
      if (!dstId || !addEdge('routes_to', table.moduleId, dstId, f.rel, r.line)) {
        skipped.routes_unresolved += 1;
      }
    }
  }

  // phase 5: deterministic ordering + counts
  const entityList = [...entities.values()].sort((a, b) => a.id.localeCompare(b.id));
  const edgeList = [...edges.values()].sort((a, b) => a.rel.localeCompare(b.rel)
    || a.src_id.localeCompare(b.src_id) || a.dst_id.localeCompare(b.dst_id));
  const edgeCounts = Object.fromEntries(RELS.map((r) => [r, 0]));
  for (const e of edgeList) edgeCounts[e.rel] += 1;
  const moduleCount = entityList.filter((e) => e.kind === 'module').length;
  return {
    corpus,
    repo: repoName,
    sha,
    entities: entityList,
    edges: edgeList,
    counts: {
      entities: entityList.length,
      modules: moduleCount,
      symbols: entityList.length - moduleCount,
      edges: edgeCounts,
      skipped,
    },
  };
}

// Deterministic JSONL text: entity lines, edge lines, one meta trailer. No timestamps.
export function renderJSONL(graph) {
  const lines = [];
  for (const e of graph.entities) lines.push(JSON.stringify(e));
  for (const e of graph.edges) lines.push(JSON.stringify(e));
  lines.push(JSON.stringify({
    type: 'meta', corpus: graph.corpus, repo: graph.repo, sha: graph.sha, counts: graph.counts,
  }));
  return `${lines.join('\n')}\n`;
}

// CLI: node emit-graph.js <repoPath> <outPath> <corpus> [sha] [repoName]
const isMain = process.argv[1] && process.argv[1].endsWith('emit-graph.js');
if (isMain) {
  const [, , repoPath, outPath, corpus, sha = '', repoName = ''] = process.argv;
  if (!repoPath || !outPath || !corpus) {
    console.error('usage: node emit-graph.js <repoPath> <outPath> <corpus> [sha] [repoName]');
    process.exit(2);
  }
  buildGraph({ repoPath, corpus, sha, repoName })
    .then((graph) => {
      writeFileSync(outPath, renderJSONL(graph));
      const { counts } = graph;
      console.error(
        `graph: ${counts.entities} entities (${counts.modules} modules), edges `
        + `imports=${counts.edges.imports} calls=${counts.edges.calls} `
        + `defines=${counts.edges.defines} routes_to=${counts.edges.routes_to} → ${outPath}`,
      );
    })
    .catch((e) => { console.error('emit-graph failed:', e); process.exit(1); });
}
