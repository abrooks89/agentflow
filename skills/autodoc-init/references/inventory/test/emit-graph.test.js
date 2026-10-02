// test/emit-graph.test.js — cross-file resolution + deterministic JSONL (epic 702).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { buildGraph, renderJSONL } from '../emit-graph.js';

const here = dirname(fileURLToPath(import.meta.url));
// fixture repo lives OUTSIDE test/ — node --test executes .js/.ts files under test/ dirs
const repo = join(here, '..', 'fixtures', 'edges-repo');

const CORPUS = 'fix';
let _graph = null;
async function graph() {
  if (!_graph) _graph = await buildGraph({ repoPath: repo, corpus: CORPUS, sha: 'abc123' });
  return _graph;
}

test('emits a module entity per source file', async () => {
  const g = await graph();
  const mods = g.entities.filter((e) => e.kind === 'module');
  assert.ok(mods.some((e) => e.id === `sym:${CORPUS}:app/main.py:module:main`));
  assert.ok(mods.some((e) => e.id === `sym:${CORPUS}:web/util.ts:module:util`));
});

test('symbol entity ids follow sym:<corpus>:<path>:<kind>:<name>', async () => {
  const g = await graph();
  assert.ok(g.entities.some((e) => e.id === `sym:${CORPUS}:app/service.py:class:Service`));
  assert.ok(g.entities.some((e) => e.id === `sym:${CORPUS}:app/service.py:function:compute`));
});

test('all four deterministic rels are non-empty on the fixture repo', async () => {
  const g = await graph();
  for (const rel of ['imports', 'calls', 'defines', 'routes_to']) {
    assert.ok(g.edges.some((e) => e.rel === rel), `no ${rel} edges`);
  }
});

test('imports: python module-to-module via file-path resolution', async () => {
  const g = await graph();
  assert.ok(g.edges.some((e) =>
    e.rel === 'imports'
    && e.src_id === `sym:${CORPUS}:app/main.py:module:main`
    && e.dst_id === `sym:${CORPUS}:app/service.py:module:service`));
});

test('imports: ts relative specifier resolves across extensions', async () => {
  const g = await graph();
  assert.ok(g.edges.some((e) =>
    e.rel === 'imports'
    && e.src_id === `sym:${CORPUS}:web/index.ts:module:index`
    && e.dst_id === `sym:${CORPUS}:web/util.ts:module:util`));
});

test('calls: same-file resolution lands on the exact entity', async () => {
  const g = await graph();
  assert.ok(g.edges.some((e) =>
    e.rel === 'calls'
    && e.src_id === `sym:${CORPUS}:app/main.py:function:local_helper`
    && e.dst_id === `sym:${CORPUS}:app/main.py:function:top`));
});

test('calls: imported-name resolution crosses files', async () => {
  const g = await graph();
  // list_items (endpoint) calls compute imported from app/service.py
  assert.ok(g.edges.some((e) =>
    e.rel === 'calls'
    && e.src_id === `sym:${CORPUS}:app/main.py:endpoint:list_items`
    && e.dst_id === `sym:${CORPUS}:app/service.py:function:compute`));
});

test('calls: module-alias attribute call resolves to the aliased module symbol', async () => {
  const g = await graph();
  assert.ok(g.edges.some((e) =>
    e.rel === 'calls'
    && e.src_id === `sym:${CORPUS}:app/main.py:function:top`
    && e.dst_id === `sym:${CORPUS}:app/util.py:function:util_fn`));
});

test('calls: self.method() resolves to the class member', async () => {
  const g = await graph();
  assert.ok(g.edges.some((e) =>
    e.rel === 'calls'
    && e.src_id === `sym:${CORPUS}:app/service.py:function:run`
    && e.dst_id === `sym:${CORPUS}:app/service.py:function:helper`));
});

test('calls: unresolvable call sites are skipped and counted, never guessed', async () => {
  const g = await graph();
  assert.ok(g.counts.skipped.calls_unresolved >= 1);
  assert.ok(!g.edges.some((e) => e.dst_id.includes('unknown_call')));
});

test('defines: python class->method, go type->method, ts class->supplemental method', async () => {
  const g = await graph();
  assert.ok(g.edges.some((e) =>
    e.rel === 'defines'
    && e.src_id === `sym:${CORPUS}:app/service.py:class:Service`
    && e.dst_id === `sym:${CORPUS}:app/service.py:function:run`));
  assert.ok(g.edges.some((e) =>
    e.rel === 'defines'
    && e.src_id === `sym:${CORPUS}:pkg/pkg.go:type:Store`
    && e.dst_id === `sym:${CORPUS}:pkg/pkg.go:method:Get`));
  assert.ok(g.edges.some((e) =>
    e.rel === 'defines'
    && e.src_id === `sym:${CORPUS}:web/index.ts:class:View`
    && e.dst_id === `sym:${CORPUS}:web/index.ts:method:draw`));
});

test('ts supplemental method entity is minted (not in extract-symbols output)', async () => {
  const g = await graph();
  const m = g.entities.find((e) => e.id === `sym:${CORPUS}:web/index.ts:method:draw`);
  assert.ok(m, 'supplemental method entity missing');
  assert.equal(m.tier, 'symbol');
  assert.equal(m.origin, 'ast');
});

test('routes_to: python decorator route = module -> endpoint', async () => {
  const g = await graph();
  assert.ok(g.edges.some((e) =>
    e.rel === 'routes_to'
    && e.src_id === `sym:${CORPUS}:app/main.py:module:main`
    && e.dst_id === `sym:${CORPUS}:app/main.py:endpoint:list_items`));
});

test('routes_to: express identifier handler resolves via imported name', async () => {
  const g = await graph();
  assert.ok(g.edges.some((e) =>
    e.rel === 'routes_to'
    && e.src_id === `sym:${CORPUS}:srv/server.js:module:server`
    && e.dst_id === `sym:${CORPUS}:srv/handlers.js:function:handler`));
});

test('referential integrity: every edge endpoint is an emitted entity', async () => {
  const g = await graph();
  const ids = new Set(g.entities.map((e) => e.id));
  for (const e of g.edges) {
    assert.ok(ids.has(e.src_id), `dangling src ${e.src_id}`);
    assert.ok(ids.has(e.dst_id), `dangling dst ${e.dst_id}`);
  }
});

test('every ast edge carries origin=ast confidence=1.0 and file/line evidence', async () => {
  const g = await graph();
  for (const e of g.edges) {
    assert.equal(e.origin, 'ast');
    assert.equal(e.confidence, 1.0);
    assert.ok(e.evidence && typeof e.evidence.file === 'string' && e.evidence.line >= 1);
  }
});

test('entities carry span with 1-based start/end lines', async () => {
  const g = await graph();
  const f = g.entities.find((e) => e.id === `sym:${CORPUS}:app/service.py:class:Service`);
  assert.equal(f.span.file, 'app/service.py');
  assert.ok(f.span.start_line >= 1 && f.span.end_line >= f.span.start_line);
});

test('JSONL render is byte-deterministic across runs and carries a meta trailer', async () => {
  const g1 = await buildGraph({ repoPath: repo, corpus: CORPUS, sha: 'abc123' });
  const g2 = await buildGraph({ repoPath: repo, corpus: CORPUS, sha: 'abc123' });
  const t1 = renderJSONL(g1);
  const t2 = renderJSONL(g2);
  assert.equal(t1, t2, 'JSONL must be byte-identical for identical inputs');
  const lines = t1.trim().split('\n').map((l) => JSON.parse(l));
  const meta = lines[lines.length - 1];
  assert.equal(meta.type, 'meta');
  assert.equal(meta.corpus, CORPUS);
  assert.equal(meta.sha, 'abc123');
  assert.ok(meta.counts.entities > 0);
  assert.ok(meta.counts.edges.imports > 0);
  // no timestamps anywhere: determinism is the contract
  assert.ok(!t1.includes('generated'), 'JSONL must not embed timestamps');
});

test('edges are deduped on (src, dst, rel)', async () => {
  const g = await graph();
  const seen = new Set();
  for (const e of g.edges) {
    const k = `${e.src_id}|${e.dst_id}|${e.rel}`;
    assert.ok(!seen.has(k), `duplicate edge ${k}`);
    seen.add(k);
  }
});

test('a walked extension with no loaded parser fails loudly naming the language', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'emit-graph-'));
  try {
    writeFileSync(join(dir, 'a.py'), 'def f():\n    return 1\n');
    await assert.rejects(
      () => buildGraph({ repoPath: dir, corpus: 'x', sha: '', parsers: {} }),
      /python/,
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('id collisions (same path/kind/name at two lines) keep first and are counted', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'emit-graph-'));
  try {
    mkdirSync(join(dir, 'p'));
    writeFileSync(join(dir, 'p', 'dup.py'), 'def f():\n    return 1\n\n\ndef f():\n    return 2\n');
    const g = await buildGraph({ repoPath: dir, corpus: 'x', sha: '' });
    const fs_ = g.entities.filter((e) => e.id === 'sym:x:p/dup.py:function:f');
    assert.equal(fs_.length, 1);
    assert.equal(fs_[0].span.start_line, 1);
    assert.equal(g.counts.skipped.id_collisions, 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ------------------------------------------- adversarial-review remediation
test('self-call across classes must NOT mint an edge (finding 3a)', async () => {
  const g = await graph();
  // Beta has no helper; Alpha.helper must not be guessed
  assert.ok(!g.edges.some((e) =>
    e.rel === 'calls'
    && e.src_id === `sym:${CORPUS}:app/cross_class.py:function:run`
    && e.dst_id === `sym:${CORPUS}:app/cross_class.py:function:helper`));
});

test('same-class self-call still resolves (finding 3a positive control)', async () => {
  const g = await graph();
  assert.ok(g.edges.some((e) =>
    e.rel === 'calls'
    && e.src_id === `sym:${CORPUS}:app/service.py:function:run`
    && e.dst_id === `sym:${CORPUS}:app/service.py:function:helper`));
});

test('shadowed names never become call edges (finding 3b)', async () => {
  const g = await graph();
  assert.ok(!g.edges.some((e) =>
    e.rel === 'calls' && e.dst_id === `sym:${CORPUS}:app/shadow.py:function:shadowed_fn`));
  assert.ok(!g.edges.some((e) =>
    e.rel === 'calls' && e.dst_id === `sym:${CORPUS}:app/local_shadow.py:function:rebound_fn`));
});

test('shadowed and unresolved-self skips are surfaced in counts (finding 3)', async () => {
  const g = await graph();
  assert.ok(g.counts.skipped.calls_shadowed >= 2,
    `calls_shadowed=${g.counts.skipped.calls_shadowed}`);
  // the Beta self.helper() site lands in calls_unresolved, not in the graph
  assert.ok(g.counts.skipped.calls_unresolved >= 2);
});

// -------------------------------------- round-2 remediation (finding R2-3)
test('nested def is NOT bare-callable from another scope (no false edge)', async () => {
  const g = await graph();
  const helper = `sym:${CORPUS}:app/nested_scope.py:function:epsilon_helper`;
  const badSrcs = g.edges.filter((e) => e.rel === 'calls' && e.dst_id === helper
    && e.src_id !== `sym:${CORPUS}:app/nested_scope.py:function:epsilon_fn`);
  assert.deepEqual(badSrcs, [], `false nested-def edges: ${JSON.stringify(badSrcs)}`);
});

test('nested def call from its visible scope keeps its edge (positive control)', async () => {
  const g = await graph();
  assert.ok(g.edges.some((e) =>
    e.rel === 'calls'
    && e.src_id === `sym:${CORPUS}:app/nested_scope.py:function:epsilon_fn`
    && e.dst_id === `sym:${CORPUS}:app/nested_scope.py:function:epsilon_helper`));
});
