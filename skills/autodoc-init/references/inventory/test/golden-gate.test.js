// test/golden-gate.test.js — the golden-output gate (epic 704 scope 6):
// structural equivalence between a rendered tree and the frozen baseline,
// with a declared page-set budget. Every assertion class gets a fixture that
// trips exactly it.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { runGoldenGate } from '../golden-gate.js';
import { findFiles } from '../assert-no-agent-writes.js';

function page({ type = 'service', title, body = '', extra = '' }) {
  return `---
type: "${type}"
title: "${title}"
created: "2026-08-01"
updated: "2026-08-07"
source_repo: "o/r"
source_paths:
  - "app/x.py"
status: "active"
${extra}---

${title} summary.
${body}
`;
}

const SERVICE_EXTRA = 'service_kind: "api"\npath: "app/"\nlanguage: "python"\nentrypoint: "app/x.py"\n';

function writeTree(dir, files) {
  for (const [rel, text] of Object.entries(files)) {
    const abs = join(dir, rel);
    mkdirSync(join(abs, '..'), { recursive: true });
    writeFileSync(abs, text);
  }
}

function baselineFiles() {
  return {
    'services/Alpha.md': page({
      title: 'Alpha', extra: SERVICE_EXTRA,
      body: 'Alpha links [[Beta]].',
    }),
    'services/Beta.md': page({ title: 'Beta', extra: SERVICE_EXTRA }),
  };
}

function gate(renderedFiles, budget = 0, over = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'golden-'));
  try {
    const renderedDir = join(dir, 'rendered');
    const baselineDir = join(dir, 'baseline');
    mkdirSync(renderedDir);
    mkdirSync(baselineDir);
    writeTree(baselineDir, over.baseline || baselineFiles());
    writeTree(renderedDir, renderedFiles);
    return runGoldenGate({
      renderedDir, baselineDir,
      config: { pageSetSymmetricDiffBudget: budget },
      repoPath: '-', inventoryPath: '-',
    });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

// Same fixtures, but with the config passed whole — the refresh mode declares
// `pageSetLostBudget` and may omit the symmetric budget entirely.
function gateConfig(renderedFiles, config, over = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'golden-'));
  try {
    const renderedDir = join(dir, 'rendered');
    const baselineDir = join(dir, 'baseline');
    mkdirSync(renderedDir);
    mkdirSync(baselineDir);
    writeTree(baselineDir, over.baseline || baselineFiles());
    writeTree(renderedDir, renderedFiles);
    return runGoldenGate({
      renderedDir, baselineDir, config, repoPath: '-', inventoryPath: '-',
    });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

// Standalone pages: no cross-links, so dropping one trips page-set and nothing
// else — the tolerance tests can assert on page-set without link noise.
function standaloneBaseline() {
  return {
    'services/One.md': page({ title: 'One', extra: SERVICE_EXTRA }),
    'services/Two.md': page({ title: 'Two', extra: SERVICE_EXTRA }),
    'services/Three.md': page({ title: 'Three', extra: SERVICE_EXTRA }),
  };
}

const resultOf = (run, id) => run.results.find((r) => r.id === id);

test('identical trees pass every assertion', () => {
  const run = gate(baselineFiles());
  assert.ok(run.ok, JSON.stringify(run.results, null, 2));
});

test('a missing page beyond the declared budget fails page-set', () => {
  const files = baselineFiles();
  delete files['services/Beta.md'];
  // Beta is also Alpha's link target: dropping it breaks links + recall too,
  // so assert the page-set failure specifically.
  const run = gate(files, 0);
  assert.ok(!resultOf(run, 'page-set').ok);
  const withBudget = gate(files, 1);
  assert.ok(resultOf(withBudget, 'page-set').ok, 'budget 1 must absorb one dropped page');
});

test('a type flip on a common page fails type-match and structural frontmatter', () => {
  const files = baselineFiles();
  files['services/Beta.md'] = page({
    type: 'process', title: 'Beta', extra: 'trigger: "manual"\n',
  });
  const run = gate(files);
  assert.ok(!resultOf(run, 'type-match').ok);
  assert.ok(!resultOf(run, 'frontmatter').ok); // structural field `type` moved
});

test('a missing required type field fails frontmatter', () => {
  const files = baselineFiles();
  files['services/Beta.md'] = page({ title: 'Beta', extra: 'service_kind: "api"\n' });
  const run = gate(files);
  const fm = resultOf(run, 'frontmatter');
  assert.ok(!fm.ok);
  assert.match(fm.detail, /Beta\.md/);
});

test('a broken wikilink in the rendered tree fails links-resolve', () => {
  const files = baselineFiles();
  files['services/Alpha.md'] = page({
    title: 'Alpha', extra: SERVICE_EXTRA, body: 'Alpha links [[Beta]] and [[Ghost]].',
  });
  const run = gate(files);
  const links = resultOf(run, 'links-resolve');
  assert.ok(!links.ok);
  assert.match(links.detail, /Ghost/);
});

test('losing a resolved outbound link fails link-recall', () => {
  const files = baselineFiles();
  files['services/Alpha.md'] = page({
    title: 'Alpha', extra: SERVICE_EXTRA, body: 'Alpha no longer links its sibling.',
  });
  const run = gate(files);
  const recall = resultOf(run, 'link-recall');
  assert.ok(!recall.ok);
  assert.match(recall.detail, /Alpha\.md: 1 -> 0/);
});

test('an undeclared budget is rejected loudly', () => {
  assert.throws(() => gate(baselineFiles(), null), /declared/);
});

test('baseline blockers do not fail the gate (parity, not perfection)', () => {
  // baseline carries a broken endpoint model link (a blocker); rendered is
  // clean — parity holds in the direction that matters.
  const baseline = baselineFiles();
  baseline['endpoints/GET x.md'] = page({
    type: 'endpoint', title: 'GET /x',
    extra: 'method: "GET"\nroute: "/x"\naliases:\n  - "GET /x"\nauth: "none"\nhandler: "app/x.py:h"\nrequest_model: "[[Missing Model]]"\n',
  });
  const rendered = {
    ...baselineFiles(),
    'endpoints/GET x.md': page({
      type: 'endpoint', title: 'GET /x',
      extra: 'method: "GET"\nroute: "/x"\naliases:\n  - "GET /x"\nauth: "none"\nhandler: "app/x.py:h"\n',
    }),
  };
  const run = gate(rendered, 0, { baseline });
  assert.ok(resultOf(run, 'blocker-parity').ok, JSON.stringify(run.results));
});

// --- pageSetLostBudget: the refresh mode (gh#431, E-0743 item 3) -----------
// The init path must not move, so the first test pins the default semantics:
// without the key, a CREATED page still counts against the symmetric budget.

test('without pageSetLostBudget, created pages still count symmetrically', () => {
  const files = { ...baselineFiles(), 'services/Gamma.md': page({ title: 'Gamma', extra: SERVICE_EXTRA }) };
  const run = gate(files, 0);
  const ps = resultOf(run, 'page-set');
  assert.ok(!ps.ok, 'the default path must keep failing on growth');
  assert.match(ps.detail, /symmetric difference 1 > budget 0/);
  assert.match(ps.detail, /Gamma\.md/);
});

test('pageSetLostBudget 0 passes a render that creates pages and loses none', () => {
  const files = {
    ...baselineFiles(),
    'services/Gamma.md': page({ title: 'Gamma', extra: SERVICE_EXTRA }),
    'services/Delta.md': page({ title: 'Delta', extra: SERVICE_EXTRA }),
  };
  // No symmetric budget at all: the refresh mode does not read one.
  const run = gateConfig(files, { pageSetLostBudget: 0 });
  const ps = resultOf(run, 'page-set');
  assert.ok(ps.ok, ps.detail);
  assert.match(ps.detail, /0 lost <= budget 0/);
  // Growth must stay visible on the PASS line, with the pages named.
  assert.match(ps.detail, /2 created/);
  assert.match(ps.detail, /Gamma\.md/);
  assert.match(ps.detail, /Delta\.md/);
  assert.ok(run.ok, JSON.stringify(run.results, null, 2));
});

test('pageSetLostBudget 0 fails a render that loses a page, naming it', () => {
  const files = standaloneBaseline();
  delete files['services/Two.md'];
  const run = gateConfig(files, { pageSetLostBudget: 0 }, { baseline: standaloneBaseline() });
  const ps = resultOf(run, 'page-set');
  assert.ok(!ps.ok);
  assert.match(ps.detail, /1 lost > budget 0/);
  assert.match(ps.detail, /only-baseline: .*Two\.md/);
  assert.ok(!run.ok);
});

test('pageSetLostBudget tolerates exactly its declared number of losses', () => {
  const one = standaloneBaseline();
  delete one['services/Two.md'];
  const two = standaloneBaseline();
  delete two['services/Two.md'];
  delete two['services/Three.md'];

  const atBudget = gateConfig(one, { pageSetLostBudget: 1 }, { baseline: standaloneBaseline() });
  assert.ok(resultOf(atBudget, 'page-set').ok, resultOf(atBudget, 'page-set').detail);
  assert.match(resultOf(atBudget, 'page-set').detail, /1 lost <= budget 1/);

  const overBudget = gateConfig(two, { pageSetLostBudget: 1 }, { baseline: standaloneBaseline() });
  const ps = resultOf(overBudget, 'page-set');
  assert.ok(!ps.ok, 'one loss past the declared tolerance must fail');
  assert.match(ps.detail, /2 lost > budget 1/);
  assert.match(ps.detail, /Three\.md/);
});

test('a lost-page budget that is not a declared non-negative integer is rejected', () => {
  for (const bad of [-1, 1.5, '0', true, NaN]) {
    assert.throws(
      () => gateConfig(baselineFiles(), { pageSetLostBudget: bad }),
      /pageSetLostBudget must be a declared non-negative integer/,
      `pageSetLostBudget: ${JSON.stringify(bad)} must throw`,
    );
  }
});

test('a malformed symmetric budget still throws even when the lost budget is given', () => {
  assert.throws(
    () => gateConfig(baselineFiles(), { pageSetLostBudget: 0, pageSetSymmetricDiffBudget: -1 }),
    /pageSetSymmetricDiffBudget must be a declared non-negative integer/,
  );
});

test('assert-no-agent-writes finds stray files recursively', () => {
  const dir = mkdtempSync(join(tmpdir(), 'no-writes-'));
  try {
    assert.deepEqual(findFiles(join(dir, 'missing')), []);
    mkdirSync(join(dir, 'sub'), { recursive: true });
    assert.deepEqual(findFiles(dir), []);
    writeFileSync(join(dir, 'sub', 'stray.md'), 'agent wrote here');
    assert.deepEqual(findFiles(dir), ['sub/stray.md']);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
