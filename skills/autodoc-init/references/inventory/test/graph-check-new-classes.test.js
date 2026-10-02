// test/graph-check-new-classes.test.js — A1–A8 check classes of graph-check.js.
// Every fixture set is built in a mkdtempSync temp dir — autoFix/--fix never
// runs against committed fixtures.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync, writeFileSync, rmSync, mkdtempSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { checkGraph, autoFix, classify } from '../graph-check.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CLI = join(__dirname, '..', 'graph-check.js');
const DATE = '2026-07-17';

const runCLI = (args) => spawnSync(process.execPath, [CLI, ...args], { encoding: 'utf8' });
const tmp = (prefix) => mkdtempSync(join(tmpdir(), prefix));
const base = (p) => p.split('/').pop();

// Full-frontmatter page builder (all universal fields present so the only
// findings in a test set are the ones the test seeds deliberately).
function page(type, title, extra = '', body = '') {
  return (
    `---\ntype: ${type}\ntitle: "${title}"\ncreated: ${DATE}\nupdated: ${DATE}\n` +
    `source_repo: "example-org/sample-repo"\nsource_paths:\n  - "a.py"\nstatus: active\n${extra}---\n\n` +
    `# ${title}\n\n${body}\n`
  );
}

// --- A1: contract_with symmetry ----------------------------------------------

test('A1: one-way contract_with is reported as contractAsymmetry on the silent side', () => {
  const dir = tmp('gc-a1-pos-');
  try {
    writeFileSync(join(dir, 'p-page.md'), page('component', 'P Page', 'contract_with:\n  - "[[Q Page]]"\n', 'See [[Q Page]].'));
    writeFileSync(join(dir, 'q-page.md'), page('endpoint', 'Q Page', '', 'See [[P Page]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.equal(r.contractAsymmetry.length, 1, 'exactly one asymmetry');
    assert.deepEqual(r.contractAsymmetry[0], { page: 'q-page.md', missingTarget: 'P Page', linkedFrom: 'p-page.md' });
    const { blockers, warnings } = classify(r);
    assert.ok(warnings.some((w) => w.kind === 'contractAsymmetry'), 'classified as warning');
    assert.ok(!blockers.some((b) => b.kind === 'contractAsymmetry'), 'never a blocker');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A1: symmetric contract_with pair is clean', () => {
  const dir = tmp('gc-a1-clean-');
  try {
    writeFileSync(join(dir, 'p-page.md'), page('component', 'P Page', 'contract_with:\n  - "[[Q Page]]"\n', 'See [[Q Page]].'));
    writeFileSync(join(dir, 'q-page.md'), page('endpoint', 'Q Page', 'contract_with:\n  - "[[P Page]]"\n', 'See [[P Page]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.contractAsymmetry, [], 'no asymmetry when both sides list each other');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A1 autofix: creates the contract_with list on the silent side (corpus block-list style)', () => {
  const dir = tmp('gc-a1-fix-');
  try {
    writeFileSync(join(dir, 'p-page.md'), page('component', 'P Page', 'contract_with:\n  - "[[Q Page]]"\n', 'See [[Q Page]].'));
    writeFileSync(join(dir, 'q-page.md'), page('endpoint', 'Q Page', '', 'See [[P Page]].'));
    const before = checkGraph({ pagesDir: dir, date: DATE });
    const { fixed, residual } = autoFix(before, { pagesDir: dir, date: DATE });
    const after = readFileSync(join(dir, 'q-page.md'), 'utf8');
    assert.ok(after.includes('contract_with:\n  - "[[P Page]]"'), 'reciprocal entry appended as a quoted block-list item');
    assert.ok(fixed.some((f) => f.kind === 'contractWith' && f.page === 'q-page.md'), 'fix recorded');
    assert.deepEqual(residual.contractAsymmetry, [], 'residual symmetric');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A1 autofix: appends to an EXISTING contract_with list without disturbing prior entries', () => {
  const dir = tmp('gc-a1-fix2-');
  try {
    writeFileSync(join(dir, 'p-page.md'), page('component', 'P Page', 'contract_with:\n  - "[[Q Page]]"\n', 'See [[Q Page]] and [[R Page]].'));
    writeFileSync(join(dir, 'q-page.md'), page('endpoint', 'Q Page', 'contract_with:\n  - "[[R Page]]"\n', 'See [[P Page]] and [[R Page]].'));
    writeFileSync(join(dir, 'r-page.md'), page('data-model', 'R Page', 'contract_with:\n  - "[[Q Page]]"\n', 'See [[Q Page]].'));
    const before = checkGraph({ pagesDir: dir, date: DATE });
    autoFix(before, { pagesDir: dir, date: DATE });
    const after = readFileSync(join(dir, 'q-page.md'), 'utf8');
    assert.ok(
      after.includes('contract_with:\n  - "[[R Page]]"\n  - "[[P Page]]"'),
      'new entry appended after the existing item',
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- A2: frontmatter relational-link resolution --------------------------------

test('A2: dangling relational frontmatter link is a frontmatterLinkUnresolved BLOCKER (CLI exits 1)', () => {
  const dir = tmp('gc-a2-pos-');
  const out = join(dir, 'report.json');
  try {
    writeFileSync(join(dir, 'alpha.md'), page('service', 'Alpha', 'related:\n  - "[[Ghost Page]]"\n', 'See [[Beta]].'));
    writeFileSync(join(dir, 'beta.md'), page('component', 'Beta', '', 'See [[Alpha]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.equal(r.frontmatterLinkUnresolved.length, 1);
    assert.deepEqual(r.frontmatterLinkUnresolved[0], { page: 'alpha.md', field: 'related', target: 'Ghost Page', class: 'blocker' });
    assert.ok(classify(r).blockers.some((b) => b.kind === 'frontmatterLinkUnresolved'), 'classified blocker');
    const res = runCLI([dir, '-', '-', out]);
    assert.equal(res.status, 1, `CLI exits 1 on unresolved frontmatter link (stderr: ${res.stderr})`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A2: resolving relational links (incl. nav-page targets) are clean; flow entrypoint wikilink is checked', () => {
  const dir = tmp('gc-a2-clean-');
  try {
    writeFileSync(
      join(dir, 'alpha.md'),
      page('service', 'Alpha', 'related:\n  - "[[Beta]]"\n  - "[[Glossary]]"\n', 'See [[Beta]].'),
    );
    writeFileSync(join(dir, 'beta.md'), page('component', 'Beta', '', 'See [[Alpha]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.frontmatterLinkUnresolved, [], 'resolving + nav-page links not flagged');

    // flow entrypoint holds a wikilink and MUST resolve
    writeFileSync(join(dir, 'flow.md'), page('flow', 'Some Flow', 'entrypoint: "[[Ghost Job]]"\n', 'See [[Alpha]].'));
    const r2 = checkGraph({ pagesDir: dir, date: DATE });
    assert.ok(
      r2.frontmatterLinkUnresolved.some((e) => e.field === 'entrypoint' && e.target === 'Ghost Job'),
      'flow entrypoint link checked',
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A2: endpoint request_model stays on the brokenModelLink path (no double count)', () => {
  const dir = tmp('gc-a2-model-');
  try {
    writeFileSync(
      join(dir, 'post-x.md'),
      page('endpoint', 'Post X', 'request_model: "[[Missing Model]]"\n', 'See [[Alpha]].'),
    );
    writeFileSync(join(dir, 'alpha.md'), page('service', 'Alpha', '', 'See [[Post X]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    const bl = r.brokenLinks.find((b) => b.target === 'Missing Model');
    assert.equal(bl.class, 'blocker', 'existing brokenModelLink blocker preserved');
    assert.ok(
      !r.frontmatterLinkUnresolved.some((e) => e.target === 'Missing Model'),
      'endpoint model field not double-counted in frontmatterLinkUnresolved',
    );
    assert.equal(classify(r).blockers.length, 1, 'exactly one blocker for the dangling model link');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A2 autofix: METHOD-path dangling link normalizes to the unique endpoint alias; exit flips 1 -> 0', () => {
  const dir = tmp('gc-a2-fix-');
  const out = join(dir, 'report.json');
  try {
    writeFileSync(
      join(dir, 'get-beads-stream.md'),
      page(
        'endpoint',
        'GET /v1/beads/stream',
        'aliases:\n  - "GET /v1/beads/stream"\n',
        'See [[Caller Page]].',
      ),
    );
    // dangling: normKey differs by 2+ edits so the fuzzy fixer skips it; the
    // A2 endpoint normalization (strip non-alphanumerics) matches exactly one.
    writeFileSync(
      join(dir, 'caller-page.md'),
      page('page', 'Caller Page', 'calls_endpoints:\n  - "[[GET v1beads.stream]]"\n', 'See [[GET /v1/beads/stream]].'),
    );
    const before = runCLI([dir, '-', '-', out]);
    assert.equal(before.status, 1, `unresolved frontmatter link blocks before fix (stderr: ${before.stderr})`);
    const after = runCLI([dir, '-', '-', out, '--fix']);
    assert.equal(after.status, 0, `--fix rewrites to the slash-form alias and exits 0 (stderr: ${after.stderr})`);
    const text = readFileSync(join(dir, 'caller-page.md'), 'utf8');
    assert.ok(text.includes('- "[[GET /v1/beads/stream]]"'), 'link rewritten to the slash-form alias, quoting preserved');
    assert.ok(!text.includes('[[GET v1beads.stream]]'), 'dash-form gone');
    const report = JSON.parse(readFileSync(out, 'utf8'));
    assert.deepEqual(report.frontmatterLinkUnresolved, [], 'residual clean');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A2 autofix: ambiguous normalization (>1 endpoint match) is reported, never guessed', () => {
  const dir = tmp('gc-a2-ambig-');
  const out = join(dir, 'report.json');
  try {
    writeFileSync(
      join(dir, 'ep-one.md'),
      page('endpoint', 'GET /v1/beads/stream', 'aliases:\n  - "GET /v1/beads/stream"\n', 'See [[Caller Page]].'),
    );
    writeFileSync(
      join(dir, 'ep-two.md'),
      page('endpoint', 'GET /v1/be/adsstream', 'aliases:\n  - "GET /v1/be/adsstream"\n', 'See [[Caller Page]].'),
    );
    writeFileSync(
      join(dir, 'caller-page.md'),
      page(
        'page',
        'Caller Page',
        'calls_endpoints:\n  - "[[GET v1beads.stream]]"\n',
        'See [[GET /v1/beads/stream]] and [[GET /v1/be/adsstream]].',
      ),
    );
    const res = runCLI([dir, '-', '-', out, '--fix']);
    assert.equal(res.status, 1, `ambiguous match still blocks after --fix (stderr: ${res.stderr})`);
    const text = readFileSync(join(dir, 'caller-page.md'), 'utf8');
    assert.ok(text.includes('[[GET v1beads.stream]]'), 'dangling link left untouched (no guessing)');
    const report = JSON.parse(readFileSync(out, 'utf8'));
    assert.equal(report.frontmatterLinkUnresolved.length, 1, 'still reported in residual');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- A3: routes/aliases parity -------------------------------------------------

test('A3: route-family entry without a matching alias is reported; parity is clean', () => {
  const dir = tmp('gc-a3-');
  try {
    writeFileSync(
      join(dir, 'get-things.md'),
      page(
        'endpoint',
        'GET /v1/things',
        'method: multiple\nroutes:\n  - "GET /v1/things"\n  - "GET /v1/things/{id}"\naliases:\n  - "GET /v1/things"\n',
        'See [[Alpha]].',
      ),
    );
    writeFileSync(join(dir, 'alpha.md'), page('service', 'Alpha', '', 'See [[GET /v1/things]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.aliasParity, [{ page: 'get-things.md', route: 'GET /v1/things/{id}' }]);
    assert.ok(classify(r).warnings.some((w) => w.kind === 'aliasParity'), 'warning class');

    // parity -> clean
    writeFileSync(
      join(dir, 'get-things.md'),
      page(
        'endpoint',
        'GET /v1/things',
        'method: multiple\nroutes:\n  - "GET /v1/things"\n  - "GET /v1/things/{id}"\n' +
          'aliases:\n  - "GET /v1/things"\n  - "GET /v1/things/{id}"\n',
        'See [[Alpha]].',
      ),
    );
    const r2 = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r2.aliasParity, [], 'full parity clean');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A3 autofix: appends the missing slash-form alias verbatim from the routes entry', () => {
  const dir = tmp('gc-a3-fix-');
  try {
    writeFileSync(
      join(dir, 'get-things.md'),
      page(
        'endpoint',
        'GET /v1/things',
        'method: multiple\nroutes:\n  - "GET /v1/things"\n  - "GET /v1/things/{id}"\naliases:\n  - "GET /v1/things"\n',
        'See [[Alpha]].',
      ),
    );
    writeFileSync(join(dir, 'alpha.md'), page('service', 'Alpha', '', 'See [[GET /v1/things]].'));
    const before = checkGraph({ pagesDir: dir, date: DATE });
    const { fixed, residual } = autoFix(before, { pagesDir: dir, date: DATE });
    const after = readFileSync(join(dir, 'get-things.md'), 'utf8');
    assert.ok(
      after.includes('aliases:\n  - "GET /v1/things"\n  - "GET /v1/things/{id}"'),
      'alias appended verbatim as a quoted block-list item',
    );
    assert.ok(fixed.some((f) => f.kind === 'aliasParity'), 'fix recorded');
    assert.deepEqual(residual.aliasParity, [], 'residual parity clean');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- A4: self-link --------------------------------------------------------------

test('A4: frontmatter or body wikilink resolving to its own page warns as selfLink; no autofix', () => {
  const dir = tmp('gc-a4-');
  try {
    writeFileSync(
      join(dir, 'self-page.md'),
      page('component', 'Self Page', 'related:\n  - "[[Self Page]]"\n', 'Also links [[Self Page]] in prose and [[Other Page]].'),
    );
    writeFileSync(join(dir, 'other-page.md'), page('component', 'Other Page', '', 'See [[Self Page]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.selfLink, [{ page: 'self-page.md', target: 'Self Page' }], 'self link reported once (deduped targets)');
    const { blockers, warnings } = classify(r);
    assert.ok(warnings.some((w) => w.kind === 'selfLink'), 'warning class');
    assert.ok(!blockers.length, 'not a blocker');

    // no autofix: file untouched, residual still warns
    const beforeText = readFileSync(join(dir, 'self-page.md'), 'utf8');
    const { residual } = autoFix(r, { pagesDir: dir, date: DATE });
    assert.equal(readFileSync(join(dir, 'self-page.md'), 'utf8'), beforeText, 'file unchanged by --fix');
    assert.equal(residual.selfLink.length, 1, 'selfLink persists in residual');

    // clean pair without self references
    writeFileSync(join(dir, 'self-page.md'), page('component', 'Self Page', '', 'Links [[Other Page]].'));
    const r2 = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r2.selfLink, [], 'clean set has no selfLink entries');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- A5: sanitized-title alias ---------------------------------------------------

test('A5: title with filename-invalid chars and no full-title alias warns as titleAliasMissing', () => {
  const dir = tmp('gc-a5-pos-');
  try {
    writeFileSync(join(dir, 'citizen-build-plan.md'), page('flow', 'Citizen Build: Plan', 'entrypoint: "[[Other Page]]"\n', 'See [[Other Page]].'));
    writeFileSync(join(dir, 'other-page.md'), page('component', 'Other Page', '', 'See [[Citizen Build Plan]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.titleAliasMissing, [{ page: 'citizen-build-plan.md', title: 'Citizen Build: Plan' }]);
    assert.ok(classify(r).warnings.some((w) => w.kind === 'titleAliasMissing'), 'warning class');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A9: the same alias string on two pages warns as aliasCollision; disambiguated (service) suffixes are clean', () => {
  const dir = tmp('gc-a9-pos-');
  try {
    // Two endpoints in different services both aliasing bare "GET /metrics".
    writeFileSync(join(dir, 'get-healthz.md'), page('endpoint', 'GET /healthz', 'aliases:\n  - "GET /healthz"\n  - "GET /metrics"\n', 'x'));
    writeFileSync(join(dir, 'system-api.md'), page('endpoint', 'System API', 'aliases:\n  - "GET /api/system/health"\n  - "GET /metrics"\n', 'x'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.equal(r.aliasCollision.length, 1, 'one colliding alias');
    assert.equal(r.aliasCollision[0].alias, 'GET /metrics');
    assert.deepEqual(r.aliasCollision[0].pages, ['get-healthz.md', 'system-api.md']);
    assert.ok(classify(r).warnings.some((w) => w.kind === 'aliasCollision'), 'warning class, not blocker');
    assert.equal(classify(r).blockers.filter((b) => b.kind === 'aliasCollision').length, 0, 'never a blocker');

    // Disambiguated per the taxonomy rule: clean.
    writeFileSync(join(dir, 'get-healthz.md'), page('endpoint', 'GET /healthz', 'aliases:\n  - "GET /healthz"\n  - "GET /metrics (canonical)"\n', 'x'));
    writeFileSync(join(dir, 'system-api.md'), page('endpoint', 'System API', 'aliases:\n  - "GET /api/system/health"\n  - "GET /metrics (orchestrator)"\n', 'x'));
    const r2 = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r2.aliasCollision, [], 'service-disambiguated aliases are unique');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A5: clean when the full title is aliased, or when the title has no invalid chars', () => {
  const dir = tmp('gc-a5-clean-');
  try {
    writeFileSync(
      join(dir, 'citizen-build-plan.md'),
      page('flow', 'Citizen Build: Plan', 'entrypoint: "[[Other Page]]"\naliases:\n  - "Citizen Build: Plan"\n', 'See [[Other Page]].'),
    );
    writeFileSync(join(dir, 'other-page.md'), page('component', 'Other Page', '', 'See [[Citizen Build: Plan]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.titleAliasMissing, [], 'aliased sanitized title clean; plain title clean');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A5 autofix: adds aliases list with the full title (and appends to an existing list)', () => {
  const dir = tmp('gc-a5-fix-');
  try {
    writeFileSync(join(dir, 'citizen-build-plan.md'), page('flow', 'Citizen Build: Plan', 'entrypoint: "[[Other Page]]"\n', 'See [[Other Page]].'));
    writeFileSync(
      join(dir, 'wsx.md'),
      page('endpoint', 'WS /x', 'aliases:\n  - "Old Alias"\n', 'See [[Other Page]].'),
    );
    writeFileSync(join(dir, 'other-page.md'), page('component', 'Other Page', '', 'See [[Citizen Build Plan]] and [[WS x]].'));
    const before = checkGraph({ pagesDir: dir, date: DATE });
    const { fixed, residual } = autoFix(before, { pagesDir: dir, date: DATE });
    const created = readFileSync(join(dir, 'citizen-build-plan.md'), 'utf8');
    assert.ok(created.includes('aliases:\n  - "Citizen Build: Plan"'), 'aliases list created with the full title');
    const appended = readFileSync(join(dir, 'wsx.md'), 'utf8');
    assert.ok(appended.includes('aliases:\n  - "Old Alias"\n  - "WS /x"'), 'full title appended to the existing list');
    assert.ok(fixed.filter((f) => f.kind === 'titleAlias').length >= 2, 'both fixes recorded');
    assert.deepEqual(residual.titleAliasMissing, [], 'residual clean');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- A6: diagram grounding lint --------------------------------------------------

const MERMAID_OK =
  '```mermaid\nsequenceDiagram\n    participant A as Alpha\n    %% source: a.py\n    participant B as Beta\n    A->>B: hi\n```';

test('A6a: nonexistent %% source: path inside a fence is a diagramSourceMissing BLOCKER; existing path clean; "-" repo skips', () => {
  const dir = tmp('gc-a6a-');
  const repo = tmp('gc-a6a-repo-');
  const out = join(dir, 'report.json');
  try {
    writeFileSync(join(repo, 'a.py'), 'x = 1\n');
    writeFileSync(
      join(dir, 'flow.md'),
      page(
        'flow',
        'Some Flow',
        '',
        '## Diagram\n\n```mermaid\nsequenceDiagram\n    participant A as Alpha\n    %% source: a.py\n    participant B as Beta\n    %% source: gone/nowhere.py\n    A->>B: hi\n```\n\nSee [[Other Page]].',
      ),
    );
    writeFileSync(join(dir, 'other-page.md'), page('component', 'Other Page', '', 'See [[Some Flow]].'));
    const r = checkGraph({ pagesDir: dir, repoPath: repo, date: DATE });
    assert.equal(r.diagramSourceMissing.length, 1, 'only the nonexistent path flagged');
    assert.deepEqual(r.diagramSourceMissing[0], { page: 'flow.md', path: 'gone/nowhere.py', class: 'blocker' });
    assert.ok(classify(r).blockers.some((b) => b.kind === 'diagramSourceMissing'), 'blocker class');
    const res = runCLI([dir, '-', repo, out]);
    assert.equal(res.status, 1, `CLI exits 1 on missing diagram source (stderr: ${res.stderr})`);
    // no autofix for diagram classes: --fix leaves it blocking
    const fixRes = runCLI([dir, '-', repo, out, '--fix']);
    assert.equal(fixRes.status, 1, '--fix does not repair diagram grounding');
    // "-" repo skips the check entirely
    const skipped = checkGraph({ pagesDir: dir, repoPath: '-', date: DATE });
    assert.deepEqual(skipped.diagramSourceMissing, [], 'skipped without a repo');
  } finally {
    rmSync(dir, { recursive: true, force: true });
    rmSync(repo, { recursive: true, force: true });
  }
});

test('A6b: %% comment within 3 lines after a closing fence warns; farther away is clean', () => {
  const dir = tmp('gc-a6b-');
  try {
    writeFileSync(
      join(dir, 'flow.md'),
      page('flow', 'Some Flow', '', `## Diagram\n\n${MERMAID_OK}\n\n%% source: a.py\n\nSee [[Other Page]].`),
    );
    writeFileSync(
      join(dir, 'flow-two.md'),
      page('flow', 'Flow Two', '', `## Diagram\n\n\`\`\`mermaid\nflowchart TD\n  A[One] --> B[Two]\n\`\`\`\n\ntext\ntext\ntext\n%% far away comment\n\nSee [[Other Page]].`),
    );
    writeFileSync(join(dir, 'other-page.md'), page('component', 'Other Page', '', 'See [[Some Flow]] and [[Flow Two]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.equal(r.diagramCommentOutsideFence.length, 1, 'only the near comment flagged');
    assert.equal(r.diagramCommentOutsideFence[0].page, 'flow.md');
    assert.ok(classify(r).warnings.some((w) => w.kind === 'diagramCommentOutsideFence'), 'warning class');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A6c: byte-identical mermaid fences on two pages warn as diagramDuplicate; distinct fences clean', () => {
  const dir = tmp('gc-a6c-');
  try {
    writeFileSync(join(dir, 'flow-a.md'), page('flow', 'Flow A', '', `## Diagram\n\n${MERMAID_OK}\n\nSee [[Flow B]].`));
    writeFileSync(join(dir, 'flow-b.md'), page('flow', 'Flow B', '', `## Diagram\n\n${MERMAID_OK}\n\nSee [[Flow A]].`));
    writeFileSync(
      join(dir, 'flow-c.md'),
      page('flow', 'Flow C', '', '## Diagram\n\n```mermaid\nflowchart TD\n  A[One] --> B[Two]\n```\n\nSee [[Flow A]].'),
    );
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.equal(r.diagramDuplicate.length, 1, 'one duplicate group');
    assert.deepEqual(r.diagramDuplicate[0].pages.sort(), ['flow-a.md', 'flow-b.md']);
    assert.ok(classify(r).warnings.some((w) => w.kind === 'diagramDuplicate'), 'warning class');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A6d: >20 nodes/entities in one fence warns as diagramOverBudget (erDiagram + flowchart heuristics)', () => {
  const dir = tmp('gc-a6d-');
  try {
    const erEntities = (n) =>
      Array.from({ length: n }, (_, i) => `  E${i} {\n    string id\n  }`).join('\n');
    writeFileSync(
      join(dir, 'model-big.md'),
      page('data-model', 'Model Big', '', `## Diagram\n\n\`\`\`mermaid\nerDiagram\n${erEntities(21)}\n\`\`\`\n\nSee [[Model Ok]].`),
    );
    writeFileSync(
      join(dir, 'model-ok.md'),
      page('data-model', 'Model Ok', '', `## Diagram\n\n\`\`\`mermaid\nerDiagram\n${erEntities(20)}\n\`\`\`\n\nSee [[Model Big]].`),
    );
    const flowNodes = Array.from({ length: 21 }, (_, i) => `  N${i}[Node ${i}]`).join('\n');
    writeFileSync(
      join(dir, 'flow-big.md'),
      page('flow', 'Flow Big', '', `## Diagram\n\n\`\`\`mermaid\nflowchart TD\n${flowNodes}\n\`\`\`\n\nSee [[Model Big]].`),
    );
    const r = checkGraph({ pagesDir: dir, date: DATE });
    const byPage = Object.fromEntries(r.diagramOverBudget.map((e) => [e.page, e.count]));
    assert.equal(byPage['model-big.md'], 21, '21-entity erDiagram over budget');
    assert.equal(byPage['flow-big.md'], 21, '21-node flowchart over budget');
    assert.ok(!('model-ok.md' in byPage), '20-entity diagram within budget');
    assert.ok(classify(r).warnings.some((w) => w.kind === 'diagramOverBudget'), 'warning class');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- A7: field-format validation --------------------------------------------------

test('A7: prose handler / non-ISO decision date / spaced service entrypoint / long vendor version warn as fieldFormat', () => {
  const dir = tmp('gc-a7-');
  try {
    writeFileSync(join(dir, 'post-x.md'), page('endpoint', 'Post X', 'handler: "handles the projects"\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'job-x.md'), page('job', 'Job X', 'handler: "orchestrator/webhook.py:handle"\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'adr-1.md'), page('decision', 'ADR-001 Thing', 'date: July 2026\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'svc.md'), page('service', 'Svc', 'entrypoint: "run canonical/app.py manually"\n', 'See [[Hub]].'));
    writeFileSync(
      join(dir, 'vendor-x.md'),
      page('vendor', 'Vendor X', `version: "${'pinned at a very long descriptive sentence '.repeat(3)}"\n`, 'See [[Hub]].'),
    );
    writeFileSync(
      join(dir, 'hub.md'),
      page('component', 'Hub', '', 'See [[Post X]], [[Job X]], [[ADR-001 Thing]], [[Svc]], [[Vendor X]].'),
    );
    const r = checkGraph({ pagesDir: dir, date: DATE });
    const hits = r.fieldFormat.map((e) => `${base(e.page)}:${e.field}`).sort();
    assert.deepEqual(hits, ['adr-1.md:date', 'post-x.md:handler', 'svc.md:entrypoint', 'vendor-x.md:version']);
    assert.ok(classify(r).warnings.filter((w) => w.kind === 'fieldFormat').length === 4, 'all four warned');
    assert.ok(!classify(r).blockers.length, 'fieldFormat never blocks');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A7: well-formed handler/date/entrypoint/version (and empty values) are clean', () => {
  const dir = tmp('gc-a7-clean-');
  try {
    writeFileSync(join(dir, 'post-x.md'), page('endpoint', 'Post X', 'handler: "canonical/app.py:create"\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'adr-1.md'), page('decision', 'ADR-001 Thing', 'date: 2026-07-17\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'adr-2.md'), page('decision', 'ADR-002 Undated', 'date: ""\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'svc.md'), page('service', 'Svc', 'entrypoint: "canonical/app.py"\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'vendor-x.md'), page('vendor', 'Vendor X', 'version: "v1.0.0 @ 67c821c"\n', 'See [[Hub]].'));
    writeFileSync(
      join(dir, 'hub.md'),
      page('component', 'Hub', '', 'See [[Post X]], [[ADR-001 Thing]], [[ADR-002 Undated]], [[Svc]], [[Vendor X]].'),
    );
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.fieldFormat, [], 'well-formed fields clean');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A7: module:router families, qualified/Go-receiver handlers, and CI workflow handlers are clean', () => {
  const dir = tmp('gc-a7-router-');
  try {
    writeFileSync(join(dir, 'sessions-api.md'), page('endpoint', 'Sessions API', 'handler: "orchestrator/routes/sessions.py:router"\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'links.md'), page('endpoint', 'Bead Link Verbs', 'handler: "canonical/routes/links.py:links_router"\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'reaper.md'), page('job', 'Reaper', 'handler: "orchestrator/main.py:lifespan._reaper_loop"\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'branchgc.md'), page('job', 'Branch GC', 'handler: "app-runtime/branchgc.go:(*BranchGC).sweep"\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'ci.md'), page('job', 'CI Build', 'handler: ".github/workflows/build-sandbox-image.yaml"\n', 'See [[Hub]].'));
    writeFileSync(join(dir, 'hub.md'), page('component', 'Hub', '', 'See [[Sessions API]], [[Bead Link Verbs]], [[Reaper]], [[Branch GC]], [[CI Build]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.fieldFormat, [], 'router/qualified/receiver/workflow handlers all valid');
    // A comma-list of per-route functions is still rejected (families must use :router).
    writeFileSync(join(dir, 'links.md'), page('endpoint', 'Bead Link Verbs', 'handler: "canonical/routes/links.py:get_blocks_graph, delete_link"\n', 'See [[Hub]].'));
    const r2 = checkGraph({ pagesDir: dir, date: DATE });
    assert.ok(r2.fieldFormat.some((e) => base(e.page) === 'links.md'), 'comma-list handler still warns');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A3/A5: a (service)-suffixed alias covers the bare route and bare title (no parity/title warning)', () => {
  const dir = tmp('gc-suffix-');
  try {
    // Family page: routes stay bare, aliases are (service)-suffixed → parity clean.
    writeFileSync(
      join(dir, 'get-healthz.md'),
      page('endpoint', 'GET /healthz', 'method: multiple\nroutes:\n  - "GET /healthz"\n  - "GET /readyz"\n' +
        'aliases:\n  - "GET /healthz (canonical)"\n  - "GET /readyz (canonical)"\n', 'x'),
    );
    // Single-route page whose sanitized title collides cross-service: title covered by suffixed alias.
    writeFileSync(
      join(dir, 'get-health.md'),
      page('endpoint', 'GET /health', 'aliases:\n  - "GET /health (retrieval)"\n', 'x'),
    );
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.aliasParity, [], 'suffixed aliases cover bare routes');
    assert.deepEqual(r.titleAliasMissing, [], 'suffixed alias covers sanitized bare title');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('missingFrontmatter: type:meta pages are exempt from source_repo/source_paths', () => {
  const dir = tmp('gc-meta-fm-');
  try {
    // A meta doc with the universal fields minus source_repo/source_paths is clean.
    writeFileSync(
      join(dir, 'Review Backlog.md'),
      '---\ntype: meta\ntitle: "Review Backlog"\ncreated: ' + DATE + '\nupdated: ' + DATE + '\nproject: fixture\nstatus: current\n---\n\n# Review Backlog\n\nBody.\n',
    );
    // A non-meta page still requires them.
    writeFileSync(
      join(dir, 'svc.md'),
      '---\ntype: service\ntitle: "Svc"\ncreated: ' + DATE + '\nupdated: ' + DATE + '\nproject: fixture\nstatus: active\n---\n\n# Svc\n\nBody.\n',
    );
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.ok(!r.missingFrontmatter.some((m) => base(m.page) === 'Review Backlog.md'), 'meta doc exempt');
    assert.ok(r.missingFrontmatter.some((m) => base(m.page) === 'svc.md'), 'non-meta still required');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- A8: exposes_endpoints reciprocity ---------------------------------------------

test('A8: endpoint whose service does not list it in exposes_endpoints warns as exposesAsymmetry; no autofix', () => {
  const dir = tmp('gc-a8-pos-');
  try {
    writeFileSync(
      join(dir, 'post-x.md'),
      page('endpoint', 'POST /x', 'aliases:\n  - "POST /x"\nservice: "[[Svc]]"\n', 'See [[Svc]].'),
    );
    writeFileSync(join(dir, 'svc.md'), page('service', 'Svc', '', 'See [[POST /x]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.exposesAsymmetry, [{ page: 'post-x.md', service: 'svc.md' }]);
    const { blockers, warnings } = classify(r);
    assert.ok(warnings.some((w) => w.kind === 'exposesAsymmetry'), 'warning class');
    assert.ok(!blockers.length, 'not a blocker');
    // editorial: --fix must not touch either page
    const svcBefore = readFileSync(join(dir, 'svc.md'), 'utf8');
    const { residual } = autoFix(r, { pagesDir: dir, date: DATE });
    assert.equal(readFileSync(join(dir, 'svc.md'), 'utf8'), svcBefore, 'service page untouched by --fix');
    assert.equal(residual.exposesAsymmetry.length, 1, 'asymmetry persists in residual');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('A8: clean when the service lists the endpoint (resolved by slash-form alias)', () => {
  const dir = tmp('gc-a8-clean-');
  try {
    writeFileSync(
      join(dir, 'post-x.md'),
      page('endpoint', 'POST /x', 'aliases:\n  - "POST /x"\nservice: "[[Svc]]"\n', 'See [[Svc]].'),
    );
    writeFileSync(
      join(dir, 'svc.md'),
      page('service', 'Svc', 'exposes_endpoints:\n  - "[[POST /x]]"\n', 'See [[POST /x]].'),
    );
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.exposesAsymmetry, [], 'reciprocal listing clean');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- report shape, exit semantics, CRLF tolerance ----------------------------------

test('report shape is additive: original six keys plus all A1-A8 arrays', () => {
  const dir = tmp('gc-shape-');
  try {
    writeFileSync(join(dir, 'alpha.md'), page('service', 'Alpha', '', 'See [[Beta]].'));
    writeFileSync(join(dir, 'beta.md'), page('component', 'Beta', '', 'See [[Alpha]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    for (const k of ['brokenLinks', 'orphans', 'missingFrontmatter', 'missingSourcePaths', 'coverage', 'summary']) {
      assert.ok(k in r, `original key ${k} preserved`);
    }
    for (const k of [
      'frontmatterLinkUnresolved', 'contractAsymmetry', 'aliasParity', 'selfLink', 'titleAliasMissing',
      'diagramSourceMissing', 'diagramCommentOutsideFence', 'diagramDuplicate', 'diagramOverBudget',
      'fieldFormat', 'exposesAsymmetry', 'aliasCollision',
    ]) {
      assert.ok(Array.isArray(r[k]), `new key ${k} is an array`);
      assert.deepEqual(r[k], [], `clean set: ${k} empty`);
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('exit semantics: warning-only new classes exit 0; classify tolerates reports without the new keys', () => {
  const dir = tmp('gc-warn-exit-');
  const out = join(dir, 'report.json');
  try {
    // seed contractAsymmetry + aliasParity + selfLink + titleAliasMissing +
    // fieldFormat + exposesAsymmetry — all warnings, none block.
    writeFileSync(
      join(dir, 'p-page.md'),
      page('component', 'P Page', 'contract_with:\n  - "[[Q Page]]"\nrelated:\n  - "[[P Page]]"\n', 'See [[Q Page]].'),
    );
    writeFileSync(
      join(dir, 'q-page.md'),
      page(
        'endpoint',
        'GET /v1/q',
        'aliases:\n  - "Q Page"\nmethod: multiple\nroutes:\n  - "GET /v1/q"\n  - "GET /v1/q/{id}"\n' +
          'service: "[[Svc]]"\nhandler: "prose description here"\n',
        'See [[P Page]].',
      ),
    );
    writeFileSync(join(dir, 'svc.md'), page('service', 'Svc', '', 'See [[P Page]] and [[GET /v1/q]].'));
    const res = runCLI([dir, '-', '-', out]);
    assert.equal(res.status, 0, `warning-only classes exit 0 (stderr: ${res.stderr})`);
    const report = JSON.parse(readFileSync(out, 'utf8'));
    assert.ok(report.contractAsymmetry.length >= 1, 'contractAsymmetry present');
    assert.ok(report.aliasParity.length >= 1, 'aliasParity present');
    assert.ok(report.selfLink.length >= 1, 'selfLink present');
    assert.ok(report.titleAliasMissing.length >= 1, 'titleAliasMissing present');
    assert.ok(report.fieldFormat.length >= 1, 'fieldFormat present');
    assert.ok(report.exposesAsymmetry.length >= 1, 'exposesAsymmetry present');

    // older report objects (no new keys) still classify cleanly
    const legacy = {
      brokenLinks: [], orphans: [], missingFrontmatter: [], missingSourcePaths: [],
      coverage: { skipped: true }, summary: '',
    };
    const { blockers, warnings } = classify(legacy);
    assert.deepEqual(blockers, []);
    assert.deepEqual(warnings, []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CRLF tolerance: A3 autofix appends an alias to a CRLF page preserving CRLF line endings', () => {
  const dir = tmp('gc-crlf-');
  try {
    const crlfPage = page(
      'endpoint',
      'GET /v1/things',
      'method: multiple\nroutes:\n  - "GET /v1/things"\n  - "GET /v1/things/{id}"\naliases:\n  - "GET /v1/things"\n',
      'See [[Alpha]].',
    ).replace(/\n/g, '\r\n');
    writeFileSync(join(dir, 'get-things.md'), crlfPage);
    writeFileSync(join(dir, 'alpha.md'), page('service', 'Alpha', '', 'See [[GET /v1/things]].'));
    const before = checkGraph({ pagesDir: dir, date: DATE });
    assert.equal(before.aliasParity.length, 1, 'CRLF page parsed; parity gap detected');
    const { residual } = autoFix(before, { pagesDir: dir, date: DATE });
    const after = readFileSync(join(dir, 'get-things.md'), 'utf8');
    assert.ok(after.includes('aliases:\r\n  - "GET /v1/things"\r\n  - "GET /v1/things/{id}"'), 'alias appended with CRLF endings');
    assert.deepEqual(residual.aliasParity, [], 'residual clean');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// nested nav dirs still exempt (regression guard for the new text-retaining page map)
test('nav pages in subfolders remain exempt from all new checks', () => {
  const dir = tmp('gc-nav-');
  try {
    mkdirSync(join(dir, 'flows'));
    writeFileSync(join(dir, 'flows', '_index.md'), '# Flows index\n\n[[Flow A]] links [[Ghost]]\n');
    writeFileSync(join(dir, 'flows', 'flow-a.md'), page('flow', 'Flow A', 'entrypoint: "[[Other Page]]"\n', 'See [[Other Page]].'));
    writeFileSync(join(dir, 'other-page.md'), page('component', 'Other Page', '', 'See [[Flow A]].'));
    const r = checkGraph({ pagesDir: dir, date: DATE });
    assert.deepEqual(r.frontmatterLinkUnresolved, [], 'nav page content never checked');
    assert.ok(!r.brokenLinks.some((b) => b.page.endsWith('_index.md')), 'nav pages excluded as before');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
