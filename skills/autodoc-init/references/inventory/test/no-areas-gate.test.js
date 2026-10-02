// test/no-areas-gate.test.js — deletion completeness + portability grep gate
// (epic 703). The hand-written AREAS layer is deleted; nothing in the shipped
// package may reference it, and nothing outside test fixtures may name a
// repo-specific directory — that is the portability thesis, enforced here.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const inventoryDir = join(here, '..');
const referencesDir = join(inventoryDir, '..');

const shippedJs = () => readdirSync(inventoryDir)
  .filter((f) => f.endsWith('.js'))
  .map((f) => join(inventoryDir, f))
  .concat([join(referencesDir, 'init-workflow.js')]);

test('areas.js is deleted outright', () => {
  assert.equal(existsSync(join(inventoryDir, 'areas.js')), false,
    'inventory/areas.js must not exist — the area layer is derived now');
});

test('areaForFile has zero references in shipped code', () => {
  for (const file of shippedJs()) {
    const src = readFileSync(file, 'utf8');
    assert.ok(!src.includes('areaForFile'), `${file} references areaForFile`);
  }
});

test('init-workflow.js carries no AREAS literal and no args.areas override', () => {
  const src = readFileSync(join(referencesDir, 'init-workflow.js'), 'utf8');
  assert.ok(!/\bAREAS\b/.test(src), 'init-workflow.js still contains an AREAS literal');
  assert.ok(!src.includes('args.areas'), 'init-workflow.js still honors args.areas');
  assert.ok(!src.includes('areaSymbols'), 'init-workflow.js still reads areaSymbols');
});

test('extract-inventory.js no longer emits byArea', () => {
  const src = readFileSync(join(inventoryDir, 'extract-inventory.js'), 'utf8');
  assert.ok(!src.includes('byArea'), 'extract-inventory.js still emits byArea');
});

test('no repo-shaped directory names in shipped code (portability thesis)', () => {
  // The old AREAS map named these; the shipped package must not. Test
  // fixtures are exempt (they exercise multi-language parsing, not areas).
  // The old map's area directories were one repo's <name>-frontend / -backend /
  // -runtime, so the needle is that SHAPE rather than any one repo's prefix: it
  // catches the old names and any other repo's, and names nobody.
  const legacyAreas = ['frontend', 'backend', 'runtime'].map((s) => `\\b[A-Za-z0-9_]+-${s}\\b`);
  const banned = new RegExp(legacyAreas.join('|') + `|sync_daemon|['"\`](canonical|orchestrator|dowse|retrieval|packs)['"\`]`);
  for (const file of shippedJs()) {
    const src = readFileSync(file, 'utf8');
    const m = src.match(banned);
    assert.equal(m, null, `${file} names a repo-specific directory: ${m && m[0]}`);
  }
});
