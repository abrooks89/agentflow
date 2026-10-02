import assert from 'node:assert';
import { computeStaleDiagrams, parsePageFrontmatter } from '../diagram-staleness.js';

// parsePageFrontmatter
{
  const text = `---
type: flow
diagram: true
diagram_source_sha: "abc123"
source_paths:
  - "orchestrator/runner.py"
  - "shared/gitea.py"
---
# Body`;
  const fm = parsePageFrontmatter(text);
  assert.strictEqual(fm.diagram, true, 'diagram true parsed');
  assert.strictEqual(fm.diagram_source_sha, 'abc123', 'sha parsed');
  assert.deepStrictEqual(fm.source_paths, ['orchestrator/runner.py', 'shared/gitea.py'], 'source_paths parsed');
}
{
  const fm = parsePageFrontmatter('---\ntype: endpoint\n---\nbody');
  assert.strictEqual(fm.diagram, false, 'missing diagram defaults false');
  assert.strictEqual(fm.diagram_source_sha, '', 'missing sha defaults empty');
}

// computeStaleDiagrams — a diagrammed page whose source moved is stale.
// Shas must look like shas (7..40 hex chars): since gh#438 the comparison is a
// common-prefix match with a floor, so placeholder strings like 'cur' read as
// degenerate and STALE by design. B records the abbreviated form of the head,
// which is what real pages do and the case gh#438 fixed.
{
  const CUR = '964e5646300033a9f025a289f5795106996fc4c6';
  const CUR_SHORT = CUR.slice(0, 8);
  const OLD = '690ca5c';
  const pages = [
    { path: 'flows/A.md', diagram: true, diagram_source_sha: OLD, source_paths: ['a.py'] },
    { path: 'flows/B.md', diagram: true, diagram_source_sha: CUR_SHORT, source_paths: ['b.py'] },
    { path: 'endpoints/C.md', diagram: false, diagram_source_sha: '', source_paths: ['c.py'] },
  ];
  const headSourceShas = { 'a.py': CUR, 'b.py': CUR, 'c.py': CUR };
  const r = computeStaleDiagrams({ pages, headSourceShas });
  assert.deepStrictEqual(r.stale.map(p => p.path), ['flows/A.md'], 'only A stale (sha moved)');
  assert.deepStrictEqual(r.fresh.map(p => p.path), ['flows/B.md'], 'B fresh');
  assert.strictEqual(r.budget.staleCount, 1, 'budget staleCount');
  assert.ok(r.budget.estRegenTokens > 0, 'budget has token estimate');
}

// pages with no diagram are ignored entirely
{
  const pages = [{ path: 'x.md', diagram: false, diagram_source_sha: '', source_paths: ['x.py'] }];
  const r = computeStaleDiagrams({ pages, headSourceShas: { 'x.py': 'z' } });
  assert.strictEqual(r.stale.length, 0, 'no-diagram page never stale');
  assert.strictEqual(r.fresh.length, 0, 'no-diagram page not counted fresh');
}

// a source path missing from headSourceShas does NOT crash and does NOT falsely flag
{
  const pages = [{ path: 'flows/D.md', diagram: true, diagram_source_sha: 'x', source_paths: ['gone.py'] }];
  const r = computeStaleDiagrams({ pages, headSourceShas: {} });
  assert.strictEqual(r.stale.length + r.fresh.length, 1, 'D classified, not crashed');
}

console.log('diagram-staleness: all tests passed');
