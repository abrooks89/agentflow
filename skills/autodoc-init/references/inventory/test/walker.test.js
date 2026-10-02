// test/walker.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { buildInventory } from '../extract-inventory.js';

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, 'fixtures', 'repo');

test('buildInventory walks the tree and extracts symbols (no area layer)', async () => {
  const inv = await buildInventory({ repoPath: repo });
  assert.ok(inv.counts.total >= 3);
  assert.ok(inv.symbols.some((s) => s.kind === 'endpoint'));
  assert.ok(inv.symbols.some((s) => s.kind === 'component'));
  assert.ok(Object.keys(inv.counts.byKind).length >= 2);
  // epic 703: the hardcoded area layer is deleted — symbols carry no area,
  // and there is no byArea bucketing (grouping is by derived community,
  // read from the graph store).
  assert.ok(inv.symbols.every((s) => !('area' in s)));
  assert.equal('byArea' in inv, false);
  assert.equal('byArea' in inv.counts, false);
});
