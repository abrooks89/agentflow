// test/tour-order.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync } from 'node:fs';
import { parsePage, listPages } from '../graph-check.js';
import { buildTourGraph, orderTour } from '../tour-order.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const TOUR_PAGES = join(__dirname, 'fixtures', 'tour-pages');

// Build the [{file, title, links}] page set the tour helpers consume, reusing
// graph-check's parser/lister so normalization stays identical.
function loadPages() {
  return listPages(TOUR_PAGES).map((p) => {
    const parsed = parsePage(readFileSync(p.abspath, 'utf8'));
    return { file: p.file, title: parsed.title, links: parsed.links };
  });
}

const titles = (ordered) => ordered.map((s) => s.title);

test('buildTourGraph emits a node per page and a dependency edge A->B when A links B', () => {
  const pages = loadPages();
  const { nodes, edges } = buildTourGraph(pages);
  assert.equal(nodes.length, pages.length, 'one node per input page');
  // A links B  => A depends on B  => edge A->B
  const hasEdge = (from, to) =>
    edges.some((e) => e.from === from && e.to === to);
  assert.ok(hasEdge('Page A', 'Page B'), 'A depends on B');
  assert.ok(hasEdge('Page B', 'Page C'), 'B depends on C');
  // cycle: D<->E
  assert.ok(hasEdge('Page D', 'Page E'), 'D depends on E');
  assert.ok(hasEdge('Page E', 'Page D'), 'E depends on D');
});

test('orderTour places dependencies before dependents (C before B before A)', () => {
  const pages = loadPages();
  const ordered = orderTour(pages, { seedTitles: ['Page A'] });
  const t = titles(ordered);
  assert.deepEqual(t, ['Page C', 'Page B', 'Page A'], 'bottom-up chain order');
  // reason placeholders are deterministic
  assert.equal(ordered[0].reason, 'foundational');
  assert.equal(ordered[1].reason, 'builds on Page C');
  assert.equal(ordered[2].reason, 'builds on Page B');
});

test('orderTour emits every input page exactly once (no drops, no dups)', () => {
  const pages = loadPages();
  const ordered = orderTour(pages);
  assert.equal(ordered.length, pages.length, 'count preserved');
  const seen = new Set(ordered.map((s) => s.title));
  assert.equal(seen.size, pages.length, 'no duplicates');
  for (const p of pages) {
    assert.ok(seen.has(p.title), `${p.title} present`);
  }
  // each entry carries file + reason
  for (const s of ordered) {
    assert.ok(s.file, 'has file');
    assert.ok(typeof s.reason === 'string' && s.reason.length, 'has reason');
  }
});

test('orderTour breaks a cycle deterministically, emitting both nodes once', () => {
  const pages = loadPages();
  const ordered = orderTour(pages, { seedTitles: ['Page D', 'Page E'] });
  const t = titles(ordered);
  assert.equal(t.length, 2, 'closure is exactly the two cycle nodes');
  assert.deepEqual([...t].sort(), ['Page D', 'Page E']);
  // deterministic tie-break: stable alphabetical by title on the unresolved cycle
  assert.deepEqual(t, ['Page D', 'Page E'], 'cycle broken alphabetically');
  // stable across repeated runs
  const again = titles(orderTour(loadPages(), { seedTitles: ['Page D', 'Page E'] }));
  assert.deepEqual(again, t, 'deterministic across runs');
});

test('orderTour seedTitles restricts to the dependency closure', () => {
  const pages = loadPages();
  // Seed on B: closure is B + its dependency C (NOT A, which depends on B).
  const ordered = orderTour(pages, { seedTitles: ['Page B'] });
  const t = titles(ordered);
  assert.deepEqual(t, ['Page C', 'Page B'], 'closure = B and its dep C, deps first');
  assert.ok(!t.includes('Page A'), 'dependent A excluded from closure');
  assert.ok(!t.includes('Page D'), 'unrelated cycle excluded');
});
