// test/graph-check.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync, writeFileSync, rmSync, cpSync, mkdtempSync, existsSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { parsePage, listPages, checkGraph, autoFix, classify } from '../graph-check.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PAGES = join(__dirname, 'fixtures', 'pages');
const REPO = join(__dirname, 'fixtures', 'repo');
const CLI = join(__dirname, '..', 'graph-check.js');

// Copy the canonical fixture pages into a throwaway temp dir so autoFix (which
// mutates page files on disk) never touches the committed fixtures.
function tmpPages() {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-graph-'));
  cpSync(PAGES, dir, { recursive: true });
  return dir;
}

const read = (rel) => readFileSync(join(PAGES, rel), 'utf8');

test('parsePage extracts wikilink target stems, stripping aliases', () => {
  const text = 'body [[Foo]] and [[Bar|b]] and again [[Foo]]';
  const { links } = parsePage(text);
  assert.deepEqual(links, ['Foo', 'Bar']);
});

test('parsePage reads source_paths and title from frontmatter', () => {
  const { frontmatter, source_paths, title } = parsePage(read('endpoints/create-project.md'));
  assert.equal(title, 'Create Project');
  assert.equal(frontmatter.type, 'endpoint');
  assert.equal(frontmatter.status, 'active');
  assert.deepEqual(source_paths, ['canonical/app.py', 'shared/util.go']);
});

test('parsePage returns empty links when there are none', () => {
  const { links } = parsePage('---\ntitle: x\n---\nplain body, no links');
  assert.deepEqual(links, []);
});

test('parsePage tolerates missing frontmatter', () => {
  const { frontmatter, source_paths, title, links } = parsePage('just [[Foo]] text');
  assert.deepEqual(frontmatter, {});
  assert.deepEqual(source_paths, []);
  assert.equal(title, undefined);
  assert.deepEqual(links, ['Foo']);
});

test('listPages finds *.md recursively and excludes nav pages by default', () => {
  const pages = listPages(PAGES);
  const files = pages.map((p) => p.file).sort();
  // valid content pages are present
  assert.ok(files.includes('endpoints/create-project.md'), 'has create-project');
  assert.ok(files.includes('endpoints/merge-util.md'), 'has merge-util (recursive)');
  // nav pages excluded
  assert.ok(!files.some((f) => f.endsWith('_index.md')), 'no _index.md');
  assert.ok(!files.some((f) => f.endsWith('index.md')), 'no index.md');
  // abspath resolves and is readable (normalize separators for comparison)
  for (const p of pages) {
    assert.ok(p.abspath.replace(/\\/g, '/').endsWith(p.file), 'abspath ends with normalized file');
    assert.doesNotThrow(() => readFileSync(p.abspath, 'utf8'), 'abspath is readable');
  }
});

test('listPages includes nav pages when excludeNav=false', () => {
  const all = listPages(PAGES, { excludeNav: false }).map((p) => p.file);
  assert.ok(all.some((f) => f.endsWith('_index.md')), 'nav included when not excluded');
});

const base = (p) => p.split('/').pop();

test('checkGraph resolves title-case links to kebab pages; only Nonexistent Target is broken', () => {
  const r = checkGraph({ pagesDir: PAGES, date: '2026-06-24' });
  // exactly one broken link, the genuinely-absent target, with zero candidates
  assert.equal(r.brokenLinks.length, 1, 'exactly one broken link');
  const bl = r.brokenLinks[0];
  assert.equal(base(bl.page), 'broken-link-page.md');
  assert.equal(bl.target, 'Nonexistent Target');
  assert.deepEqual(bl.candidates, [], 'genuinely-absent target has no candidates');
  assert.equal(bl.class, 'warning', 'plain broken link classed as advisory warning');
});

test('checkGraph finds exactly the orphan page', () => {
  const r = checkGraph({ pagesDir: PAGES, date: '2026-06-24' });
  const orphans = r.orphans.map(base).sort();
  assert.deepEqual(orphans, ['orphan-page.md']);
});

test('tour pages with zero inbound links are NOT flagged as orphans (entry-point exemption)', () => {
  const dir = tmpPages();
  try {
    const tourDir = join(dir, 'tours');
    if (!existsSync(tourDir)) mkdirSync(tourDir, { recursive: true });
    // A tour nobody links to — orphan-by-design, must be exempt.
    writeFileSync(
      join(tourDir, 'lonely-tour.md'),
      [
        '---',
        'type: tour',
        'title: "Lonely Tour"',
        'created: 2026-07-12',
        'updated: 2026-07-12',
        'source_repo: "example-org/sample-repo"',
        'status: active',
        '---',
        '# Lonely Tour',
        'A reading sequence linking [[Create Project]].',
        '',
      ].join('\n')
    );
    const r = checkGraph({ pagesDir: dir, date: '2026-07-12' });
    const orphans = r.orphans.map(base);
    assert.ok(!orphans.includes('lonely-tour.md'), 'tour must be exempt from orphan check');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('checkGraph finds exactly the page missing the status field', () => {
  const r = checkGraph({ pagesDir: PAGES, date: '2026-06-24' });
  assert.equal(r.missingFrontmatter.length, 1, 'exactly one page missing frontmatter');
  const mf = r.missingFrontmatter[0];
  assert.equal(base(mf.page), 'missing-field-page.md');
  assert.deepEqual(mf.missing, ['status']);
});

test('checkGraph flags only the non-existent source_path when repoPath given', () => {
  const r = checkGraph({ pagesDir: PAGES, repoPath: REPO, date: '2026-06-24' });
  assert.equal(r.missingSourcePaths.length, 1, 'exactly one missing source path');
  const ms = r.missingSourcePaths[0];
  assert.equal(base(ms.page), 'bad-source-path-page.md');
  assert.equal(ms.path, 'canonical/deleted.py');
});

// gh#695: an update deprecates a page whose source was deleted, and a record must
// still name what it documented, so the dead path is the explanation, not drift.
test('checkGraph exempts a deprecated page from source_path existence, and only that page', () => {
  const dir = tmpPages();
  try {
    const found = listPages(dir).find((p) => p.file.endsWith('bad-source-path-page.md'));
    assert.ok(found, 'fixture page moved');
    const src = readFileSync(found.abspath, 'utf8');
    assert.ok(/^status: active$/m.test(src), 'fixture no longer carries status: active');
    writeFileSync(found.abspath, src.replace(/^status: active$/m, 'status: "deprecated"'));
    const deprecated = checkGraph({ pagesDir: dir, repoPath: REPO, date: '2026-06-24' });
    assert.deepEqual(deprecated.missingSourcePaths, [], 'a deprecated page still failed source-paths');
    writeFileSync(found.abspath, src);
    const live = checkGraph({ pagesDir: dir, repoPath: REPO, date: '2026-06-24' });
    assert.deepEqual(live.missingSourcePaths.map((m) => m.path), ['canonical/deleted.py']);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('checkGraph skips source_path existence when repoPath falsy or "-"', () => {
  for (const repoPath of [undefined, '', '-']) {
    const r = checkGraph({ pagesDir: PAGES, repoPath, date: '2026-06-24' });
    assert.deepEqual(r.missingSourcePaths, [], `repoPath=${JSON.stringify(repoPath)} skips`);
  }
});

test('checkGraph coverage skipped when inventory absent or "-"', () => {
  for (const inventoryPath of [undefined, '-']) {
    const r = checkGraph({ pagesDir: PAGES, inventoryPath, date: '2026-06-24' });
    assert.deepEqual(r.coverage, { skipped: true }, 'coverage skipped without inventory');
  }
});

test('checkGraph delegates to coverage.js when inventory present', () => {
  // Write a tiny inventory whose one symbol lives in a file no page documents → fileGap.
  const invPath = join(__dirname, 'fixtures', 'tmp-inventory.json');
  writeFileSync(
    invPath,
    JSON.stringify({
      symbols: [
        { file: 'canonical/app.py', area: 'canonical', kind: 'endpoint', name: 'health', line: 4 },
        { file: 'app/frontend/src/List.tsx', area: 'frontend', kind: 'component', name: 'List', line: 1 },
      ],
    }),
  );
  try {
    const r = checkGraph({ pagesDir: PAGES, inventoryPath: invPath, date: '2026-06-24' });
    assert.equal(r.coverage.skipped, undefined, 'coverage not skipped');
    const gapFiles = r.coverage.fileGaps.map((s) => s.file);
    // canonical/app.py IS documented (by several pages); List.tsx is not → fileGap
    assert.ok(gapFiles.includes('app/frontend/src/List.tsx'), 'undocumented file flagged');
    assert.ok(!gapFiles.includes('canonical/app.py'), 'documented file not flagged');
  } finally {
    rmSync(invPath, { force: true });
  }
});

test('checkGraph returns the 6-field report shape with a summary string', () => {
  const r = checkGraph({ pagesDir: PAGES, repoPath: REPO, date: '2026-06-24' });
  for (const k of ['brokenLinks', 'orphans', 'missingFrontmatter', 'missingSourcePaths', 'coverage', 'summary']) {
    assert.ok(k in r, `report has ${k}`);
  }
  assert.equal(typeof r.summary, 'string');
});

// --- Task 3: safe auto-fix ---------------------------------------------------

const baseName = (p) => p.split('/').pop();

test('autoFix rewrites a single-candidate (fuzzy/typo) broken link in the page file', () => {
  const dir = tmpPages();
  try {
    // A typo link `[[Create Projct]]` — does NOT resolve via normKey (so it is
    // broken) but is edit-distance 1 from the existing page "Create Project",
    // the only fuzzy candidate. Place it on the orphan page (which already links
    // out) so we exercise fix (a) without disturbing other fixtures.
    const orphan = join(dir, 'endpoints', 'orphan-page.md');
    const txt = readFileSync(orphan, 'utf8').replace(
      'It links out to [[Create Project]]',
      'It links out to [[Create Projct]]',
    );
    writeFileSync(orphan, txt);

    const before = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    assert.ok(
      before.brokenLinks.some((b) => b.target === 'Create Projct'),
      'typo link is broken before fix',
    );

    const { fixed } = autoFix(before, { pagesDir: dir, date: '2026-06-24' });

    // file rewritten to the candidate, alias-free original preserved as plain link
    const after = readFileSync(orphan, 'utf8');
    assert.ok(after.includes('[[Create Project]]'), 'link rewritten to candidate');
    assert.ok(!after.includes('[[Create Projct]]'), 'typo no longer present');

    const linkFix = fixed.find((f) => f.kind === 'link' && baseName(f.page) === 'orphan-page.md');
    assert.ok(linkFix, 'fixed list records the link fix');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('autoFix preserves a |alias when rewriting a single-candidate broken link', () => {
  const dir = tmpPages();
  try {
    const orphan = join(dir, 'endpoints', 'orphan-page.md');
    const txt = readFileSync(orphan, 'utf8').replace(
      'It links out to [[Create Project]]',
      'It links out to [[Create Projct|the creator]]',
    );
    writeFileSync(orphan, txt);

    const before = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    autoFix(before, { pagesDir: dir, date: '2026-06-24' });

    const after = readFileSync(orphan, 'utf8');
    assert.ok(after.includes('[[Create Project|the creator]]'), 'alias preserved on rewrite');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('autoFix adds safe frontmatter defaults (status, updated) only', () => {
  const dir = tmpPages();
  try {
    const before = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    // missing-field-page.md is missing only `status`
    assert.ok(
      before.missingFrontmatter.some((m) => baseName(m.page) === 'missing-field-page.md'),
      'missing-field page flagged before fix',
    );

    const { fixed } = autoFix(before, { pagesDir: dir, date: '2026-06-24' });

    const page = join(dir, 'endpoints', 'missing-field-page.md');
    const parsed = parsePage(readFileSync(page, 'utf8'));
    assert.equal(parsed.frontmatter.status, 'active', 'status defaulted to active');

    const fmFix = fixed.find(
      (f) => f.kind === 'frontmatter' && baseName(f.page) === 'missing-field-page.md',
    );
    assert.ok(fmFix, 'fixed list records the frontmatter fix');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('autoFix never fabricates type/title/created/source_repo/source_paths', () => {
  const dir = tmpPages();
  try {
    // A page missing several non-safe universal fields — only status/updated may
    // be auto-added; the rest must remain flagged in residual.
    const page = join(dir, 'endpoints', 'bare-page.md');
    writeFileSync(
      page,
      '---\nupdated: 2026-06-24\n---\n\n# Bare\n\nLinks to [[Merge Util]] so not an orphan.\nproject: fixture\n',
    );

    const before = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    const { residual } = autoFix(before, { pagesDir: dir, date: '2026-06-24' });

    const parsed = parsePage(readFileSync(page, 'utf8'));
    assert.equal(parsed.frontmatter.status, 'active', 'status safely added');
    assert.equal(parsed.frontmatter.type, undefined, 'type NOT fabricated');
    assert.equal(parsed.frontmatter.title, undefined, 'title NOT fabricated');
    assert.equal(parsed.frontmatter.source_repo, undefined, 'source_repo NOT fabricated');

    const stillMissing = residual.missingFrontmatter.find((m) => baseName(m.page) === 'bare-page.md');
    assert.ok(stillMissing, 'bare-page still has residual frontmatter gaps');
    assert.ok(stillMissing.missing.includes('type'), 'type still flagged');
    assert.ok(stillMissing.missing.includes('title'), 'title still flagged');
    assert.ok(!stillMissing.missing.includes('status'), 'status resolved');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('autoFix does NOT touch ambiguous links (>1 fuzzy candidate)', () => {
  const dir = tmpPages();
  try {
    // Add two real pages whose stems are both edit-distance 1 from the typo
    // `[[Mxrge Util]]`: "Merge Util" already exists; add "Marge Util".
    const marge = join(dir, 'endpoints', 'marge-util.md');
    writeFileSync(
      marge,
      '---\ntype: endpoint\ntitle: Marge Util\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\nsource_paths:\n  - shared/util.go\nstatus: active\n---\n\n' +
        '# Marge Util\n\nLinked from [[Create Project]].\n',
    );
    const typoPage = join(dir, 'endpoints', 'ambiguous-page.md');
    writeFileSync(
      typoPage,
      '---\ntype: endpoint\ntitle: Ambiguous Page\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\nsource_paths:\n  - shared/util.go\nstatus: active\n---\n\n' +
        '# Ambiguous Page\n\nLinked from [[Create Project]]. Has a link [[Mxrge Util]] that is\n' +
        'edit-distance 1 from BOTH "Merge Util" and "Marge Util" — ambiguous, do not fix.\n',
    );
    // make both targets non-orphans / linked so they exist as pages
    const cp = join(dir, 'endpoints', 'create-project.md');
    writeFileSync(
      cp,
      readFileSync(cp, 'utf8').replace(
        'See also [[Missing Field Page]]',
        'See also [[Marge Util]] and [[Ambiguous Page]] and [[Missing Field Page]]',
      ),
    );

    const before = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    assert.ok(before.brokenLinks.some((b) => b.target === 'Mxrge Util'), 'ambiguous link broken before');

    const { fixed, residual } = autoFix(before, { pagesDir: dir, date: '2026-06-24' });

    const after = readFileSync(typoPage, 'utf8');
    assert.ok(after.includes('[[Mxrge Util]]'), 'ambiguous link left untouched in file');
    assert.ok(
      !fixed.some((f) => f.kind === 'link' && baseName(f.page) === 'ambiguous-page.md'),
      'ambiguous link not in fixed list',
    );
    assert.ok(
      residual.brokenLinks.some((b) => b.target === 'Mxrge Util'),
      'ambiguous link still broken in residual',
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('autoFix does NOT touch 0-candidate broken links, missing source_paths, or orphans', () => {
  const dir = tmpPages();
  try {
    const before = checkGraph({ pagesDir: dir, repoPath: REPO, date: '2026-06-24' });
    const { fixed, residual } = autoFix(before, { pagesDir: dir, repoPath: REPO, date: '2026-06-24' });

    // genuinely-absent target stays broken
    const blPage = join(dir, 'endpoints', 'broken-link-page.md');
    assert.ok(readFileSync(blPage, 'utf8').includes('[[Nonexistent Target]]'), 'unresolvable link untouched');
    assert.ok(
      residual.brokenLinks.some((b) => b.target === 'Nonexistent Target'),
      '0-candidate link still broken',
    );
    // orphans and missingSourcePaths are never fixed
    assert.ok(!fixed.some((f) => f.kind === 'orphan'), 'no orphan fixes');
    assert.ok(!fixed.some((f) => f.kind === 'sourcePath'), 'no source-path fixes');
    assert.deepEqual(
      residual.missingSourcePaths.map((m) => m.path),
      ['canonical/deleted.py'],
      'missing source path preserved in residual',
    );
    assert.deepEqual(residual.orphans.map(baseName).sort(), ['orphan-page.md'], 'orphan preserved');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('autoFix residual re-check shows fixed items resolved', () => {
  const dir = tmpPages();
  try {
    // seed a typo (fixable) link on the orphan page
    const orphan = join(dir, 'endpoints', 'orphan-page.md');
    writeFileSync(
      orphan,
      readFileSync(orphan, 'utf8').replace('[[Create Project]]', '[[Create Projct]]'),
    );

    const before = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    const { residual } = autoFix(before, { pagesDir: dir, date: '2026-06-24' });

    // residual must be a fresh checkGraph-shaped report
    for (const k of ['brokenLinks', 'orphans', 'missingFrontmatter', 'missingSourcePaths', 'coverage', 'summary']) {
      assert.ok(k in residual, `residual has ${k}`);
    }
    // fixed link no longer broken
    assert.ok(!residual.brokenLinks.some((b) => b.target === 'Create Projct'), 'typo link resolved in residual');
    // missing-field page no longer flagged for status
    const mf = residual.missingFrontmatter.find((m) => baseName(m.page) === 'missing-field-page.md');
    assert.ok(!mf, 'missing-field page frontmatter resolved in residual');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- Task 4: classify + CLI + exit codes -------------------------------------

test('classify buckets a seeded report: model link + missingSourcePaths -> blockers; plain broken link + orphan -> warnings', () => {
  const report = {
    brokenLinks: [
      { page: 'a.md', target: 'Nonexistent', candidates: [], class: 'warning' }, // warning (advisory gap-marker)
      { page: 'b.md', target: 'Typoish', candidates: ['Typo'] }, // warning (has candidate, unfixed)
      { page: 'e.md', target: 'Missing Model', candidates: [], class: 'blocker' }, // blocker (endpoint model link)
    ],
    orphans: ['orphan.md'], // warning
    missingFrontmatter: [{ page: 'c.md', missing: ['type'] }], // warning (unfixed)
    missingSourcePaths: [{ page: 'd.md', path: 'gone.py' }], // blocker
    coverage: { skipped: false, fileGaps: [{ file: 'x.go' }], uncovered: [] }, // blocker
    summary: 'whatever',
  };
  const { blockers, warnings } = classify(report);

  // every blocker/warning entry is a small {kind, detail} object
  for (const e of [...blockers, ...warnings]) {
    assert.ok(e && typeof e.kind === 'string', 'entry has a kind');
    assert.ok('detail' in e, 'entry has a detail');
  }

  const bKinds = blockers.map((b) => b.kind);
  assert.ok(bKinds.includes('brokenModelLink'), 'endpoint model link is a blocker');
  assert.ok(bKinds.includes('missingSourcePath'), 'missing source path is a blocker');
  assert.ok(bKinds.includes('coverageGap'), 'coverage fileGap is a blocker');
  assert.equal(blockers.length, 3, 'exactly the three blockers');

  const wKinds = warnings.map((w) => w.kind);
  assert.ok(wKinds.includes('orphan'), 'orphan is a warning');
  assert.ok(wKinds.includes('brokenLink'), 'plain 0-candidate broken link is a warning');
  assert.ok(wKinds.includes('brokenLinkCandidate'), 'broken link with candidates is a warning');
  assert.ok(wKinds.includes('missingFrontmatter'), 'unfixed frontmatter is a warning');
});

test('classify defaults class-less broken links (older reports) to warning', () => {
  const report = {
    brokenLinks: [{ page: 'a.md', target: 'Gone', candidates: [] }], // no class field
    orphans: [], missingFrontmatter: [], missingSourcePaths: [],
    coverage: { skipped: true }, summary: '',
  };
  const { blockers, warnings } = classify(report);
  assert.deepEqual(blockers, [], 'no blockers from a class-less broken link');
  assert.equal(warnings.length, 1);
  assert.equal(warnings[0].kind, 'brokenLink');
});

test('classify on a clean report yields no blockers and no warnings', () => {
  const clean = {
    brokenLinks: [],
    orphans: [],
    missingFrontmatter: [],
    missingSourcePaths: [],
    coverage: { skipped: true },
    summary: 'clean',
  };
  const { blockers, warnings } = classify(clean);
  assert.deepEqual(blockers, []);
  assert.deepEqual(warnings, []);
});

test('classify treats skipped coverage as no coverage blocker', () => {
  const r = {
    brokenLinks: [], orphans: [], missingFrontmatter: [], missingSourcePaths: [],
    coverage: { skipped: true }, summary: '',
  };
  assert.equal(classify(r).blockers.length, 0, 'skipped coverage contributes no blocker');
});

function runCLI(args) {
  return spawnSync(process.execPath, [CLI, ...args], { encoding: 'utf8' });
}

test('CLI exits 0 when the only issues are plain broken links (advisory) and writes the report JSON', () => {
  const dir = tmpPages();
  const out = join(mkdtempSync(join(tmpdir(), 'autodoc-out-')), 'report.json');
  try {
    // fixtures contain broken-link-page.md -> [[Nonexistent Target]] (0 candidates):
    // per taxonomy that is an advisory gap-marker (warning), not a blocker.
    const res = runCLI([dir, '-', '-', out]);
    assert.equal(res.status, 0, `exit 0: plain broken links are warnings (stderr: ${res.stderr})`);
    assert.ok(existsSync(out), 'report JSON written');
    const report = JSON.parse(readFileSync(out, 'utf8'));
    const bl = report.brokenLinks.find((b) => b.target === 'Nonexistent Target');
    assert.ok(bl, 'report still lists the broken link');
    assert.equal(bl.class, 'warning', 'broken link carries class: warning in the JSON report');
    assert.ok(res.stderr.length > 0, 'one-line summary printed to stderr');
    assert.match(res.stderr, /0 blocker\(s\)/, 'summary reports zero blockers');
  } finally {
    rmSync(dir, { recursive: true, force: true });
    rmSync(dirname(out), { recursive: true, force: true });
  }
});

test('CLI exits 1 on a broken endpoint request_model link (contract blocker)', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-model-'));
  const out = join(dir, 'report.json');
  try {
    writeFileSync(
      join(dir, 'post-projects.md'),
      '---\ntype: endpoint\ntitle: Create Project EP\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\nsource_paths:\n  - a.py\nstatus: active\n' +
        'request_model: "[[Missing Model]]"\nresponse_model: ""\n---\n\n# EP\n\nSee [[Alpha]].\n',
    );
    writeFileSync(
      join(dir, 'alpha.md'),
      '---\ntype: service\ntitle: Alpha\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\nsource_paths:\n  - a.py\nstatus: active\n---\n\n# Alpha\n\nSee [[Create Project EP]].\n',
    );
    const res = runCLI([dir, '-', '-', out]);
    assert.equal(res.status, 1, `exit 1 on dangling endpoint model link (stderr: ${res.stderr})`);
    const report = JSON.parse(readFileSync(out, 'utf8'));
    const bl = report.brokenLinks.find((b) => b.target === 'Missing Model');
    assert.ok(bl, 'model link reported broken');
    assert.equal(bl.class, 'blocker', 'model link classed as blocker');
    assert.match(res.stderr, /1 blocker\(s\)/, 'summary counts the model-link blocker');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI exits 0 on a clean fixture set', () => {
  // Build a clean page set: two pages that link to each other, full frontmatter, no broken links.
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-clean-'));
  const out = join(dir, 'report.json');
  const fm = (title, sp) =>
    `---\ntype: endpoint\ntitle: ${title}\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n` +
    `source_repo: example-org/sample-repo\nsource_paths:\n  - ${sp}\nstatus: active\n---\n\n`;
  try {
    writeFileSync(join(dir, 'alpha.md'), fm('Alpha', 'a.py') + '# Alpha\n\nLinks to [[Beta]].\n');
    writeFileSync(join(dir, 'beta.md'), fm('Beta', 'b.py') + '# Beta\n\nLinks to [[Alpha]].\n');
    const res = runCLI([dir, '-', '-', out]);
    assert.equal(res.status, 0, `exit 0 on clean set (stderr: ${res.stderr})`);
    const report = JSON.parse(readFileSync(out, 'utf8'));
    assert.deepEqual(report.brokenLinks, [], 'no broken links');
    assert.deepEqual(report.orphans, [], 'no orphans');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI "-" for inventory and repo skips coverage and source-path dimensions', () => {
  const dir = tmpPages();
  const out = join(mkdtempSync(join(tmpdir(), 'autodoc-skip-')), 'report.json');
  try {
    const res = runCLI([dir, '-', '-', out]);
    const report = JSON.parse(readFileSync(out, 'utf8'));
    assert.deepEqual(report.coverage, { skipped: true }, 'coverage skipped with "-" inventory');
    assert.deepEqual(report.missingSourcePaths, [], 'source-path dimension skipped with "-" repo');
    // Note: exit may be 1 due to the fixture broken link; we only assert the skip behavior here.
    assert.ok(res.status === 0 || res.status === 1, 'process completed');
  } finally {
    rmSync(dir, { recursive: true, force: true });
    rmSync(dirname(out), { recursive: true, force: true });
  }
});

test('CLI --fix resolves a single-candidate broken link (typo is a warning either way)', () => {
  // A clean set EXCEPT for one typo (single-candidate, fuzzy) broken link, which --fix repairs.
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-fix-'));
  const out = join(dir, 'report.json');
  const fm = (title, sp) =>
    `---\ntype: endpoint\ntitle: ${title}\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n` +
    `source_repo: example-org/sample-repo\nsource_paths:\n  - ${sp}\nstatus: active\n---\n\n`;
  try {
    writeFileSync(join(dir, 'alpha.md'), fm('Alpha', 'a.py') + '# Alpha\n\nLinks to [[Beta]].\n');
    // typo: [[Bета]] -> actually use a 1-edit typo "Beto" of "Beta"
    writeFileSync(join(dir, 'beta.md'), fm('Beta', 'b.py') + '# Beta\n\nLinks to [[Alphо]] hmm.\n');
    // Without --fix: the typo is an advisory warning (plain broken link) -> exit 0
    const before = runCLI([dir, '-', '-', out]);
    assert.equal(before.status, 0, `typo link warns, does not block (stderr: ${before.stderr})`);
    assert.match(before.stderr, /0 blocker\(s\)/, 'typo contributes no blocker before fix');
    assert.match(before.stderr, /1 broken \(0 blocker, 1 warning\)/, 'typo counted as a link warning');
    // With --fix: single fuzzy candidate "Alpha" gets rewritten -> residual clean -> exit 0
    const after = runCLI([dir, '-', '-', out, '--fix']);
    assert.equal(after.status, 0, `--fix run stays exit 0 (stderr: ${after.stderr})`);
    assert.match(after.stderr, /1 auto-fixed/, 'fix recorded in summary');
    assert.ok(readFileSync(join(dir, 'beta.md'), 'utf8').includes('[[Alpha]]'), 'typo rewritten in file');
    const report = JSON.parse(readFileSync(out, 'utf8'));
    assert.deepEqual(report.brokenLinks, [], 'residual report has no broken links after --fix');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI --fix repairs a typo endpoint model link, flipping exit 1 -> 0', () => {
  // The model-link blocker IS auto-fixable when the typo has exactly one fuzzy candidate.
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-modelfix-'));
  const out = join(dir, 'report.json');
  try {
    writeFileSync(
      join(dir, 'post-projects.md'),
      '---\ntype: endpoint\ntitle: Create Project EP\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\nsource_paths:\n  - a.py\nstatus: active\n' +
        'request_model: "[[Project Modl]]"\n---\n\n# EP\n\nSee [[Project Model]].\n',
    );
    writeFileSync(
      join(dir, 'project-model.md'),
      '---\ntype: data-model\ntitle: Project Model\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\nsource_paths:\n  - a.py\nstatus: active\n---\n\n# M\n\nUsed by [[Create Project EP]].\n',
    );
    const before = runCLI([dir, '-', '-', out]);
    assert.equal(before.status, 1, 'typo model link blocks before fix');
    const after = runCLI([dir, '-', '-', out, '--fix']);
    assert.equal(after.status, 0, `--fix repairs the model link and exits 0 (stderr: ${after.stderr})`);
    assert.ok(
      readFileSync(join(dir, 'post-projects.md'), 'utf8').includes('request_model: "[[Project Model]]"'),
      'model link rewritten in frontmatter',
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('checkGraph coverage is file-level only: uncovered suppressed, summary counts fileGaps', () => {
  // Inventory with a file-gap symbol AND a symbol in a documented file. The wiki
  // has no per-symbol covered record, so checkGraph runs coverage in
  // fileLevelOnly mode: `uncovered` must be empty (not "every symbol") and the
  // summary coverage number counts fileGaps only.
  const invPath = join(mkdtempSync(join(tmpdir(), 'autodoc-inv-')), 'inv.json');
  try {
    writeFileSync(
      invPath,
      JSON.stringify({
        symbols: [
          // documented file (create-project.md carries canonical/app.py) -> no gap
          { file: 'canonical/app.py', area: 'canonical', kind: 'endpoint', name: 'health', line: 4 },
          // undocumented file -> a fileGap
          { file: 'app/frontend/src/List.tsx', area: 'frontend', kind: 'component', name: 'List', line: 1 },
        ],
      }),
    );
    const r = checkGraph({ pagesDir: PAGES, inventoryPath: invPath, date: '2026-06-24' });
    assert.equal(r.coverage.fileGaps.length, 1, 'one file gap');
    assert.deepEqual(r.coverage.uncovered, [], 'symbol-level uncovered suppressed in graph-check mode');
    assert.match(r.summary, /coverage: 1 gap/, 'summary counts only fileGaps');
    // classify therefore sees exactly one coverage blocker, not one per symbol
    const covBlockers = classify(r).blockers.filter((b) => b.kind === 'coverageGap');
    assert.equal(covBlockers.length, 1, 'exactly one coverageGap blocker');
  } finally {
    rmSync(dirname(invPath), { recursive: true, force: true });
  }
});

// --- alias-aware link resolution ----------------------------------------------

const fmWith = (extra, title = 'T', sp = 'a.py') =>
  `---\ntype: endpoint\ntitle: ${title}\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n` +
  `source_repo: example-org/sample-repo\nsource_paths:\n  - ${sp}\nstatus: active\n${extra}---\n\n`;

test('parsePage exposes aliases from block-style and inline-array frontmatter', () => {
  const block = parsePage('---\ntitle: x\naliases:\n  - "POST /v1/sync/push"\n  - Other Name\n---\nbody');
  assert.deepEqual(block.aliases, ['POST /v1/sync/push', 'Other Name']);
  const inline = parsePage('---\ntitle: x\naliases: ["POST /v1/sync/push", "Second"]\n---\nbody');
  assert.deepEqual(inline.aliases, ['POST /v1/sync/push', 'Second']);
  const none = parsePage('---\ntitle: x\n---\nbody');
  assert.deepEqual(none.aliases, []);
});

test('checkGraph resolves links via frontmatter aliases (case-insensitive)', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-alias-'));
  try {
    writeFileSync(
      join(dir, 'post-sync-push.md'),
      fmWith('aliases:\n  - "POST /v1/sync/push"\n', 'Sync Push Endpoint') +
        '# Sync Push\n\nLinks to [[Caller Page]].\n',
    );
    writeFileSync(
      join(dir, 'caller-page.md'),
      fmWith('', 'Caller Page') +
        '# Caller\n\nCalls [[POST /v1/sync/push]] and again [[post /V1/Sync/Push]].\n',
    );
    const r = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    assert.deepEqual(r.brokenLinks, [], 'alias links resolve, nothing broken');
    // alias-resolved inbound links count: the aliased page is not an orphan
    assert.deepEqual(r.orphans, [], 'alias link counts as inbound');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('autoFix fuzzy candidates include aliases (typo of an alias rewrites to the page)', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-alias-fix-'));
  try {
    writeFileSync(
      join(dir, 'post-sync-push.md'),
      fmWith('aliases:\n  - "POST /v1/sync/push"\n', 'Sync Push Endpoint') +
        '# Sync Push\n\nLinks to [[Caller Page]].\n',
    );
    writeFileSync(
      join(dir, 'caller-page.md'),
      fmWith('', 'Caller Page') + '# Caller\n\nCalls [[POST /v1/sync/pus]].\n', // 1-edit typo of alias
    );
    const before = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    assert.ok(before.brokenLinks.some((b) => b.target === 'POST /v1/sync/pus'), 'typo broken before');
    const { fixed } = autoFix(before, { pagesDir: dir, date: '2026-06-24' });
    assert.ok(
      fixed.some((f) => f.kind === 'link' && baseName(f.page) === 'caller-page.md'),
      'alias typo fixed via fuzzy alias candidate',
    );
    const after = readFileSync(join(dir, 'caller-page.md'), 'utf8');
    assert.ok(!after.includes('[[POST /v1/sync/pus]]'), 'typo rewritten');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- tour pages exempt from source_paths ----------------------------------------

test('checkGraph does not flag missing source_paths on type: tour pages', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-tour-'));
  try {
    // tour page with NO source_paths — legitimate per taxonomy
    writeFileSync(
      join(dir, 'canonical-tour.md'),
      '---\ntype: tour\ntitle: Canonical Tour\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\nstatus: active\n---\n\n# Tour\n\nStep [[Alpha]].\n',
    );
    writeFileSync(join(dir, 'alpha.md'), fmWith('', 'Alpha') + '# Alpha\n\nSee [[Canonical Tour]].\n');
    const r = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    assert.deepEqual(r.missingFrontmatter, [], 'tour without source_paths not flagged');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('checkGraph still flags other universal fields missing on tour pages', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-tour2-'));
  try {
    // tour missing `status` — must still be flagged; source_paths must not be
    writeFileSync(
      join(dir, 'canonical-tour.md'),
      '---\ntype: tour\ntitle: Canonical Tour\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\n---\n\n# Tour\n\nStep [[Alpha]].\n',
    );
    writeFileSync(join(dir, 'alpha.md'), fmWith('', 'Alpha') + '# Alpha\n\nSee [[Canonical Tour]].\n');
    const r = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    assert.equal(r.missingFrontmatter.length, 1);
    assert.deepEqual(r.missingFrontmatter[0].missing, ['status'], 'status flagged, source_paths not');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- [[Page#heading]] anchor links ----------------------------------------------

test('checkGraph resolves [[Page#heading]] links against the page name (anchor stripped)', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-anchor-'));
  try {
    writeFileSync(
      join(dir, 'session-manager.md'),
      fmWith('', 'SessionManager') + '# SessionManager\n\nSee [[Caller Page]].\n',
    );
    writeFileSync(
      join(dir, 'caller-page.md'),
      fmWith('', 'Caller Page') +
        '# Caller\n\nBudget check lives in [[SessionManager#check_budget]] and a self-link [[#Local Heading]].\n',
    );
    const r = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    assert.deepEqual(r.brokenLinks, [], 'heading link resolves to the page; self-anchor never broken');
    // anchor link counts as inbound: session-manager is not an orphan
    assert.deepEqual(r.orphans, [], 'anchor link counts as an inbound link');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('checkGraph still flags a broken [[Missing Page#heading]] (broken by page name, not anchor)', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-anchor2-'));
  try {
    writeFileSync(
      join(dir, 'alpha.md'),
      fmWith('', 'Alpha') + '# Alpha\n\nSee [[Beta]] and [[Missing Page#some heading]].\n',
    );
    writeFileSync(join(dir, 'beta.md'), fmWith('', 'Beta') + '# Beta\n\nSee [[Alpha]].\n');
    const r = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    assert.equal(r.brokenLinks.length, 1, 'the anchored link to a missing page is still broken');
    assert.equal(r.brokenLinks[0].target, 'Missing Page#some heading', 'original target (with anchor) reported');
    assert.equal(r.brokenLinks[0].class, 'warning', 'plain broken anchored link is advisory');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('autoFix repairs a typo in an anchored link and preserves the #anchor', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-anchor-fix-'));
  try {
    writeFileSync(
      join(dir, 'session-manager.md'),
      fmWith('', 'SessionManager') + '# SessionManager\n\nSee [[Caller Page]].\n',
    );
    writeFileSync(
      join(dir, 'caller-page.md'),
      fmWith('', 'Caller Page') + '# Caller\n\nSee [[SessionManagr#check_budget]].\n', // 1-edit typo
    );
    const before = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    assert.ok(before.brokenLinks.some((b) => b.target === 'SessionManagr#check_budget'), 'typo broken before');
    const { fixed, residual } = autoFix(before, { pagesDir: dir, date: '2026-06-24' });
    assert.ok(fixed.some((f) => f.kind === 'link'), 'fix recorded');
    const after = readFileSync(join(dir, 'caller-page.md'), 'utf8');
    assert.ok(after.includes('[[SessionManager#check_budget]]'), 'rewritten with the anchor preserved');
    assert.deepEqual(residual.brokenLinks, [], 'residual clean');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- endpoint request_model / response_model contract links ---------------------

test('checkGraph classes a broken endpoint response_model link as blocker; other broken links on the same page stay warnings', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-model2-'));
  try {
    writeFileSync(
      join(dir, 'get-projects.md'),
      '---\ntype: endpoint\ntitle: Get Projects\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\nsource_paths:\n  - a.py\nstatus: active\n' +
        'request_model: ""\nresponse_model: "[[Project List]]"\n---\n\n' +
        '# EP\n\nSee [[Alpha]] and a plain gap [[Some Future Page]].\n',
    );
    writeFileSync(join(dir, 'alpha.md'), fmWith('', 'Alpha') + '# Alpha\n\nSee [[Get Projects]].\n');
    const r = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    const byTarget = Object.fromEntries(r.brokenLinks.map((b) => [b.target, b.class]));
    assert.equal(byTarget['Project List'], 'blocker', 'dangling response_model link is a blocker');
    assert.equal(byTarget['Some Future Page'], 'warning', 'ordinary broken link stays advisory');
    const { blockers, warnings } = classify(r);
    assert.deepEqual(blockers.map((b) => b.kind), ['brokenModelLink'], 'exactly the model-link blocker');
    assert.ok(warnings.some((w) => w.kind === 'brokenLink'), 'plain broken link warned');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('checkGraph: resolving model links produce no broken entries; model fields on NON-endpoint pages are not blockers', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-model3-'));
  try {
    // endpoint with a model link that RESOLVES -> nothing broken
    writeFileSync(
      join(dir, 'post-sync.md'),
      '---\ntype: endpoint\ntitle: Post Sync\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\nsource_paths:\n  - a.py\nstatus: active\n' +
        'request_model: "[[Sync Payload]]"\n---\n\n# EP\n\nSee [[Sync Payload]].\n',
    );
    writeFileSync(
      join(dir, 'sync-payload.md'),
      fmWith('', 'Sync Payload') + '# M\n\nUsed by [[Post Sync]].\n',
    );
    // a non-endpoint page carrying a (bogus) request_model field with a dangling link:
    // the taxonomy blocker applies to ENDPOINT pages only -> warning
    writeFileSync(
      join(dir, 'service-page.md'),
      '---\ntype: service\ntitle: Service Page\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\nsource_paths:\n  - a.py\nstatus: active\n' +
        'request_model: "[[Dangling Model]]"\n---\n\n# S\n\nSee [[Post Sync]].\n',
    );
    // link service-page so it is not an orphan
    writeFileSync(
      join(dir, 'sync-payload.md'),
      fmWith('', 'Sync Payload') + '# M\n\nUsed by [[Post Sync]] and [[Service Page]].\n',
    );
    const r = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    assert.ok(!r.brokenLinks.some((b) => b.target === 'Sync Payload'), 'resolving model link not broken');
    const dangling = r.brokenLinks.find((b) => b.target === 'Dangling Model');
    assert.ok(dangling, 'non-endpoint dangling link still reported');
    assert.equal(dangling.class, 'warning', 'model field on a non-endpoint page is not a contract blocker');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('non-tour page missing source_paths is still flagged', () => {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-notour-'));
  try {
    writeFileSync(
      join(dir, 'endpoint.md'),
      '---\ntype: endpoint\ntitle: Endpoint\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n' +
        'source_repo: example-org/sample-repo\nstatus: active\n---\n\n# E\n\nSee [[Alpha]].\n',
    );
    writeFileSync(join(dir, 'alpha.md'), fmWith('', 'Alpha') + '# Alpha\n\nSee [[Endpoint]].\n');
    const r = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    assert.equal(r.missingFrontmatter.length, 1);
    assert.ok(r.missingFrontmatter[0].missing.includes('source_paths'));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('a page missing ONLY project is flagged (E-0200 item 4b)', () => {
  // Without this, "the fixtures gained a project field" and "the gate requires a
  // project field" are indistinguishable - every existing test would pass either way.
  const dir = mkdtempSync(join(tmpdir(), 'gc-project-'));
  try {
    writeFileSync(
      join(dir, 'no-project.md'),
      '---' + '\n' + 'type: service' + '\n' + 'title: "No Project"' + '\n' + 'created: 2026-06-24' + '\n' +
      'updated: 2026-06-24' + '\n' + 'source_repo: example-org/sample-repo' + '\n' +
      'source_paths:' + '\n' + '  - a.py' + '\n' + 'status: active' + '\n' + '---' + '\n' + '\n' + '# No Project' + '\n',
    );
    const r = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    const mf = r.missingFrontmatter.find((m) => base(m.page) === 'no-project.md');
    assert.ok(mf, 'the page with no project is reported');
    assert.deepEqual(mf.missing, ['project'], 'project, and nothing else, is what it lacks');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('a literal [[ in prose does not swallow the lines after it (gh#432, second reader)', () => {
  // Measured on the first end-to-end /autodoc-update run. The renderer carried
  // this same too-permissive pattern and was MASKING it: it destroyed the prose
  // before this checker ever read it, so fixing the renderer is what made the
  // false positives appear. The two page bodies below are the real shapes - one
  // documenting the wikilink token, one quoting a regex that contains brackets.
  const doc = 'The page carries a `[[` wikilink token.\n'
    + '\n'
    + 'Prose between that must not be eaten.\n'
    + '\n'
    + 'Subject under test: both part of [[Agentflow Shared Library]].\n';
  assert.deepEqual(parsePage(doc).links, ['Agentflow Shared Library'],
    'the real link is found, and the stray [[ contributes no link of its own');

  const regexDoc = 'It matches `[[^\[\]]` against each line.\n\nAnd then [[Report Scorer]] scores it.\n';
  assert.deepEqual(parsePage(regexDoc).links, ['Report Scorer']);

  // A Lua long-string terminator: the closing ']]' has no '[' before it, so the
  // '[' exclusion alone cannot stop this one - only the newline exclusion can.
  const luaDoc = 'Match the literal `[[` token.\n\nProse that must survive.\n\nLua closes with ]] here.\n';
  assert.deepEqual(parsePage(luaDoc).links, [],
    'no link, and crucially no multi-line blob reported as one');
});

// --- gh#694: --fail-on-fixable is a read-only gate for a RENDERED tree -------
// /autodoc-update used to run `--fix` on the live tree after copy-back, writing
// repairs (e.g. a contract_with reciprocal) that no store record carried, so the
// next render dropped them and the next run's golden gate blamed an agent for a
// link-recall loss. The fixed runbook runs --fail-on-fixable on the render target
// instead: it must NAME the page, FAIL, and write NOTHING.

function oneWayContractTree() {
  const dir = mkdtempSync(join(tmpdir(), 'autodoc-gh694-'));
  const fm = (type, title, extra) =>
    `---\ntype: ${type}\ntitle: ${title}\ncreated: 2026-06-24\nupdated: 2026-06-24\nproject: fixture\n` +
    `source_repo: example-org/sample-repo\nsource_paths:\n  - a.py\nstatus: active\n${extra}---\n\n`;
  writeFileSync(join(dir, 'p-page.md'), fm('component', 'P Page', 'contract_with:\n  - "[[Q Page]]"\n') + '# P\n\nSee [[Q Page]].\n');
  writeFileSync(join(dir, 'q-page.md'), fm('endpoint', 'Q Page', '') + '# Q\n\nSee [[P Page]].\n');
  return dir;
}

const snapshot = (dir) => Object.fromEntries(
  ['p-page.md', 'q-page.md'].map((f) => [f, readFileSync(join(dir, f))]),
);

test('gh#694: --fail-on-fixable on a one-way contract_with names the page, exits 1, writes no page', () => {
  const dir = oneWayContractTree();
  const outDir = mkdtempSync(join(tmpdir(), 'autodoc-gh694-out-'));
  const out = join(outDir, 'report.json');
  try {
    const before = snapshot(dir);
    const res = runCLI([dir, '-', '-', out, '--fail-on-fixable']);
    assert.equal(res.status, 1, `fixable finding fails the gate (stderr: ${res.stderr})`);
    assert.match(res.stderr, /0 blocker\(s\)/, 'no blocker: the failure is the fixable finding alone');
    assert.match(res.stderr, /1 fixable on 1 page\(s\)/, 'summary counts fixable findings and pages');
    assert.match(res.stderr, /fixable \(not applied\): q-page\.md: contractWith: /, 'names the silent-side page');
    const after = snapshot(dir);
    for (const f of Object.keys(before)) {
      assert.ok(before[f].equals(after[f]), `${f} bytes unchanged by --fail-on-fixable`);
    }
    const report = JSON.parse(readFileSync(out, 'utf8'));
    assert.deepEqual(
      report.fixable.map((f) => [f.page, f.kind]),
      [['q-page.md', 'contractWith']],
      'report JSON carries the fixable list',
    );
    assert.equal(report.contractAsymmetry.length, 1, 'asymmetry still reported (nothing was repaired)');
  } finally {
    rmSync(dir, { recursive: true, force: true });
    rmSync(outDir, { recursive: true, force: true });
  }
});

test('gh#694: without the flag the same tree still exits 0 (existing callers unchanged)', () => {
  const dir = oneWayContractTree();
  const outDir = mkdtempSync(join(tmpdir(), 'autodoc-gh694-out-'));
  const out = join(outDir, 'report.json');
  try {
    const before = snapshot(dir);
    const res = runCLI([dir, '-', '-', out]);
    assert.equal(res.status, 0, `contract asymmetry alone is a warning (stderr: ${res.stderr})`);
    const report = JSON.parse(readFileSync(out, 'utf8'));
    assert.equal(report.fixable, undefined, 'no fixable key unless asked for');
    const after = snapshot(dir);
    for (const f of Object.keys(before)) assert.ok(before[f].equals(after[f]), `${f} unchanged`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
    rmSync(outDir, { recursive: true, force: true });
  }
});

test('gh#694: --fail-on-fixable exits 0 on a symmetric (clean) tree', () => {
  const dir = oneWayContractTree();
  const outDir = mkdtempSync(join(tmpdir(), 'autodoc-gh694-out-'));
  const out = join(outDir, 'report.json');
  try {
    const q = join(dir, 'q-page.md');
    writeFileSync(q, readFileSync(q, 'utf8').replace('status: active\n', 'status: active\ncontract_with:\n  - "[[P Page]]"\n'));
    const res = runCLI([dir, '-', '-', out, '--fail-on-fixable']);
    assert.equal(res.status, 0, `nothing fixable -> pass (stderr: ${res.stderr})`);
    assert.match(res.stderr, /0 fixable on 0 page\(s\)/);
    assert.deepEqual(JSON.parse(readFileSync(out, 'utf8')).fixable, []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
    rmSync(outDir, { recursive: true, force: true });
  }
});

test('gh#694: --fix and --fail-on-fixable together are refused (exit 2) and write nothing', () => {
  const dir = oneWayContractTree();
  const outDir = mkdtempSync(join(tmpdir(), 'autodoc-gh694-out-'));
  const out = join(outDir, 'report.json');
  try {
    const before = snapshot(dir);
    const res = runCLI([dir, '-', '-', out, '--fix', '--fail-on-fixable']);
    assert.equal(res.status, 2, `conflicting flags refused (stderr: ${res.stderr})`);
    assert.match(res.stderr, /mutually exclusive/);
    assert.ok(!existsSync(out), 'no report written');
    const after = snapshot(dir);
    for (const f of Object.keys(before)) assert.ok(before[f].equals(after[f]), `${f} unchanged`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
    rmSync(outDir, { recursive: true, force: true });
  }
});

test('gh#694: autoFix dryRun reports the same fixes a real run applies, without writing', () => {
  const dir = oneWayContractTree();
  try {
    const before = snapshot(dir);
    const report = checkGraph({ pagesDir: dir, date: '2026-06-24' });
    const dry = autoFix(report, { pagesDir: dir, date: '2026-06-24', dryRun: true });
    const after = snapshot(dir);
    for (const f of Object.keys(before)) assert.ok(before[f].equals(after[f]), `${f} unchanged by dryRun`);
    assert.equal(dry.residual, report, 'dryRun residual is the input report');
    const real = autoFix(report, { pagesDir: dir, date: '2026-06-24' });
    // On a rendered tree (every page has frontmatter) the dry run names the same fixes; a page with no
    // frontmatter can hide a later contractWith fix from the dry run, which reads each page as it was.
    assert.deepEqual(dry.fixed, real.fixed, 'dryRun predicts the real run on this frontmattered fixture');
    assert.ok(readFileSync(join(dir, 'q-page.md'), 'utf8').includes('"[[P Page]]"'), 'real run does write');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
