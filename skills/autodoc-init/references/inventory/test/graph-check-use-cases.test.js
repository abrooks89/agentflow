// test/graph-check-use-cases.test.js
// Phase-4 Part B, T9: regression proof that graph-check.js treats a flow page's
// `use_cases:` frontmatter (+ `## Use Cases` body table) as an INERT opaque
// scalar list — never a wikilink / relational field. UC-IDs must be invisible to
// the graph checker: no broken-link, no unresolved-relational-link, no blocker.
// Guards the taxonomy contract (Part B §B3): `use_cases` must NOT be added to the
// relational-link set; registry-existence checking is Phase-5 enrichment.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { writeFileSync, rmSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { checkGraph, classify } from '../graph-check.js';

const __dirname = dirname(fileURLToPath(import.meta.url));

// A complete `type: flow` page carrying use_cases frontmatter + a `## Use Cases`
// table (schema: UC | Domain | Status | Summary). `body` lets a caller inject an
// extra frontmatter field or body links for the contrast case.
function flowPage({ extraFm = '', extraBody = '' } = {}) {
  return (
    '---\n' +
    'type: flow\n' +
    'title: Create Project Flow\n' +
    'created: 2026-07-18\n' +
    'updated: 2026-07-18\nproject: fixture\n' +
    'source_repo: example-org/sample-repo\n' +
    'source_paths:\n' +
    '  - canonical/app.py\n' +
    'status: active\n' +
    'use_cases: ["UC-9999"]\n' +
    extraFm +
    '---\n\n' +
    '# Create Project Flow\n\n' +
    'Links to [[Other Page]] so this is not an orphan.\n\n' +
    '## Use Cases\n\n' +
    '| UC | Domain | Status | Summary |\n' +
    '| --- | --- | --- | --- |\n' +
    '| UC-9999 | auth/login | backlog | Create a project from the dashboard |\n' +
    extraBody
  );
}

// A neighbour page that links back to the flow page (keeps both off the orphan list).
function otherPage() {
  return (
    '---\n' +
    'type: service\n' +
    'title: Other Page\n' +
    'created: 2026-07-18\n' +
    'updated: 2026-07-18\nproject: fixture\n' +
    'source_repo: example-org/sample-repo\n' +
    'source_paths:\n' +
    '  - canonical/app.py\n' +
    'status: active\n' +
    '---\n\n' +
    '# Other Page\n\nSee [[Create Project Flow]].\n'
  );
}

const mentionsUC = (s) => /UC-9999/.test(String(s));

test('graph-check ignores use_cases: a flow page with UC ids in frontmatter + table is completely clean', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-usecases-'));
  try {
    writeFileSync(join(dir, 'create-project-flow.md'), flowPage());
    writeFileSync(join(dir, 'other-page.md'), otherPage());

    const r = checkGraph({ pagesDir: dir, date: '2026-07-18' });

    // No UC-9999 anywhere the checker records links/relations.
    assert.ok(
      !r.brokenLinks.some((b) => mentionsUC(b.target)),
      'UC id must never surface as a broken wikilink',
    );
    assert.ok(
      !r.frontmatterLinkUnresolved.some((e) => e.field === 'use_cases' || mentionsUC(e.target)),
      'use_cases must never be treated as an unresolved relational link',
    );
    // The opaque scalar introduces no defects at all.
    assert.deepEqual(r.brokenLinks, [], 'no broken links from the use_cases-bearing flow page');
    assert.deepEqual(r.frontmatterLinkUnresolved, [], 'no unresolved relational links');

    // ...and therefore no blocker mentions the UC id.
    const { blockers } = classify(r);
    assert.ok(!blockers.some((b) => mentionsUC(b.detail)), 'no blocker references a UC id');
    assert.deepEqual(blockers, [], 'a clean flow page yields zero blockers');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('contrast: the relational-link checker IS live on the same flow page, yet stays blind to use_cases', () => {
  // Add a genuinely dangling *relational* field (`related`) alongside the UC ids.
  // This proves the checker runs on this page and flags real relational gaps —
  // while UC-9999 (opaque scalar) remains invisible to it.
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-usecases-contrast-'));
  try {
    writeFileSync(
      join(dir, 'create-project-flow.md'),
      flowPage({ extraFm: 'related: "[[Ghost Page]]"\n' }),
    );
    writeFileSync(join(dir, 'other-page.md'), otherPage());

    const r = checkGraph({ pagesDir: dir, date: '2026-07-18' });

    // The relational checker DID fire: the dangling `related` link is unresolved.
    const ghost = r.frontmatterLinkUnresolved.find((e) => e.target === 'Ghost Page');
    assert.ok(ghost, 'a real dangling relational link IS caught (checker is live on flow pages)');
    assert.equal(ghost.field, 'related', 'the caught relation is `related`, not `use_cases`');

    // ...but nothing about use_cases / the UC id was ever considered relational.
    assert.ok(
      !r.frontmatterLinkUnresolved.some((e) => e.field === 'use_cases' || mentionsUC(e.target)),
      'use_cases stays inert even when a sibling relational field is broken',
    );
    assert.ok(!r.brokenLinks.some((b) => mentionsUC(b.target)), 'UC id still not a broken link');

    // The only blocker is the ghost relation — never the UC id.
    const { blockers } = classify(r);
    assert.ok(blockers.some((b) => /Ghost Page/.test(b.detail)), 'ghost relation is the blocker');
    assert.ok(!blockers.some((b) => mentionsUC(b.detail)), 'UC id never becomes a blocker');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
