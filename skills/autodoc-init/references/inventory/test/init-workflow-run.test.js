// test/init-workflow-run.test.js — DYNAMIC execution of init-workflow.js with
// stubbed sandbox globals (epic 704 + adversarial remediation). The disk-based
// contract: stub agents WRITE real record files under a temp runDir and return
// only light manifests; the workflow return is a small manifest of paths.
// Also proves: the agent budget (incl. the diagram reserve) fails BEFORE the
// first dispatch; single-child parents collapse (no agent); diagram batches
// widen to fit the remaining budget; the return has a hard size guard.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { dumpPrompts } from '../../prompts/render-prompt.js';

const here = dirname(fileURLToPath(import.meta.url));
const initWfPath = join(here, '..', '..', 'init-workflow.js');

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function loadWorkflow() {
  const src = readFileSync(initWfPath, 'utf8').replace(/^export const meta/m, 'const meta');
  return new AsyncFunction('args', 'agent', 'parallel', 'phase', 'log', src);
}

const SYMBOLS = [
  { kind: 'function', name: 'handler', file: 'app/x.py', line: 1 },
  { kind: 'class', name: 'Widget', file: 'app/x.py', line: 10 },
];

function baseArgs(runDir, over = {}) {
  return {
    repoPath: '/repo', autodocName: 'toy-autodoc', repoName: 'o/r',
    commitSha: 'abc123def', date: '2026-08-07', taxonomyDocPath: '/tax.md',
    validateMermaidCmd: 'python validate_mermaid.py', scratchPath: '/scratch',
    tourOrderPath: '/inv/tour-order.js',
    runDir,
    prompts: dumpPrompts(),
    units: [{
      key: 'unit-01', label: 'core', symbol_count: 2,
      communities: [{ id: 1, label: 'core', descriptor: { member_count: 2, top_symbols: ['handler'], top_prefixes: ['app'] } }],
    }],
    communitySymbols: { 1: SYMBOLS },
    // parent 5 is single-child (collapses, zero cost); parent 9 is multi-child
    parents: [
      { id: 5, level: 1, label: 'solo', children: [1] },
      { id: 9, level: 2, label: 'root', children: [1, 5] },
    ],
    maxAgents: 12,
    ...over,
  };
}

const PAGE = {
  file: 'services/Alpha.md', type: 'service', title: 'Alpha',
  source_paths: ['app/x.py'], summary: 'The alpha service.',
  body: 'The alpha service.\n\n## Source\n\n`app/x.py`\n',
  frontmatter: { service_kind: 'api', path: 'app/', language: 'python', entrypoint: 'app/x.py' },
  documents: [{ file: 'app/x.py', kind: 'function', name: 'handler' }],
  edges: [],
};

const writeRecords = (path, obj) => {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(obj));
};

const pathFromPrompt = (prompt) => {
  const m = prompt.match(/records JSON\s+at\s+(\S+?\.json)/);
  assert.ok(m, 'prompt names no records path');
  return m[1];
};

function cannedAgent(calls, overrides = {}) {
  return async (prompt, opts) => {
    calls.push({ prompt, opts });
    const label = opts.label;
    const recordsPath = pathFromPrompt(prompt);
    if (label.startsWith('map:')) {
      if (overrides.map) return overrides.map(recordsPath);
      writeRecords(recordsPath, {
        pages: [PAGE],
        reports: [{ community_id: 1, summary: 'core does core things', findings: [{ text: 'handler is the hub', entities: ['handler'] }] }],
        covered: [{ community: '1', name: 'handler', line: 1 }],
        excluded: [{ community: '1', name: 'Widget', line: 10, reason: 'impl detail' }],
      });
      return {
        unit: 'unit-01', path: recordsPath,
        pagesMeta: [{ file: PAGE.file, type: PAGE.type, title: PAGE.title, source_paths: PAGE.source_paths }],
        reportIds: [1], notes: 'candidate flow: alpha path',
        counts: { pages: 1, reports: 1, covered: 1, excluded: 1 },
      };
    }
    if (label.startsWith('parent-report:')) {
      const id = Number(label.split(':')[1]);
      writeRecords(recordsPath, {
        reports: [{ community_id: id, summary: 'the whole system', findings: [{ text: 'cross-child insight', entities: [] }] }],
      });
      return { path: recordsPath, communityId: id };
    }
    if (label === 'flows') {
      writeRecords(recordsPath, {
        pages: [{
          file: 'flows/Alpha Flow.md', type: 'flow', title: 'Alpha Flow',
          source_paths: ['app/x.py'], summary: 'End to end.',
          body: 'End to end.\n\n## What it does\n\nSteps touching [[Alpha]].\n',
          frontmatter: { flow_kind: 'pipeline' },
          documents: [],
          edges: [{ rel: 'related', dst_type: 'service', dst_title: 'Alpha' }],
        }],
      });
      return { path: recordsPath, flowsMeta: [{ file: 'flows/Alpha Flow.md', title: 'Alpha Flow', source_paths: ['app/x.py'] }] };
    }
    if (label.startsWith('diagram:')) {
      // 0fe95e8 renamed the target rows from '- [kind]' to '- page type "kind"'
      const n = (prompt.match(/- page type "(flow|data-model)"/g) || []).length;
      writeRecords(recordsPath, {
        diagrams: [{ type: 'flow', title: 'Alpha Flow', embedded: true, mermaid: 'flowchart TD\n  A --> B', source_sha: 'abc123def' }],
      });
      return { path: recordsPath, embedded: n, omitted: 0 };
    }
    if (label === 'tours') {
      writeRecords(recordsPath, { pages: [] });
      return { path: recordsPath, toursMeta: [] };
    }
    if (label === 'index') {
      writeRecords(recordsPath, {
        meta_pages: [
          { file: 'overview.md', title: 'toy Overview', body: 'Overview.' },
          { file: 'Start Here.md', title: 'Start Here', body: 'Start.' },
          { file: 'Glossary.md', title: 'Glossary', body: 'Terms.' },
        ],
      });
      return { path: recordsPath, files: ['overview.md', 'Start Here.md', 'Glossary.md'] };
    }
    throw new Error(`unexpected agent label ${label}`);
  };
}

const parallelStub = async (fns) => Promise.all(fns.map((f) => f()));
const noop = () => {};

function tempRun() {
  return mkdtempSync(join(tmpdir(), 'wf-run-')).replace(/\\/g, '/');
}

test('happy path: records land on disk, return is a light manifest', async (t) => {
  const runDir = tempRun();
  t.after(() => rmSync(runDir, { recursive: true, force: true }));
  const calls = [];
  const run = loadWorkflow();
  const result = await run(baseArgs(runDir), cannedAgent(calls), parallelStub, noop, noop);

  // 1 map + 1 multi-child parent (single-child collapsed) + flows + 1 diagram batch + tours + index
  assert.equal(result.agentsDispatched, 6);
  assert.deepEqual(result.collapsedParents, [5]);
  assert.ok(!calls.some((c) => c.opts.label === 'parent-report:5'),
    'a single-child parent must never cost an agent');
  assert.ok(calls.some((c) => c.opts.label === 'parent-report:9'));

  // records live on disk; the return carries only paths + counts
  assert.equal(result.records.runDir, runDir);
  assert.equal(result.records.files.length, 6);
  for (const f of result.records.files) {
    assert.ok(existsSync(f), `manifest names a missing records file: ${f}`);
  }
  const mapFile = JSON.parse(readFileSync(result.records.files[0], 'utf8'));
  assert.equal(mapFile.pages[0].title, 'Alpha');
  assert.equal(mapFile.reports[0].community_id, 1);
  const returned = JSON.stringify(result);
  assert.ok(!returned.includes('## Source'),
    'page bodies must never travel through the workflow return');

  // the multi-child parent agent was pointed at its children's FILES —
  // including the collapsed parent 5 resolving through to unit-01's map file
  const parentCall = calls.find((c) => c.opts.label === 'parent-report:9');
  const refs = [...parentCall.prompt.matchAll(/report community_id (\d+) in (\S+?\.json)/g)]
    .map((m) => [Number(m[1]), m[2]]);
  assert.deepEqual(refs.map(([id]) => id), [1, 1],
    'child 1 directly and child 5 (collapsed) must both resolve to community 1');
  for (const [, p] of refs) assert.ok(p.endsWith('map-unit-01.json'));
  assert.match(parentCall.prompt, /UNTRUSTED/);
});

test('pre-dispatch budget includes the diagram reserve and collapse math', async () => {
  const runDir = tempRun();
  try {
    const calls = [];
    const run = loadWorkflow();
    // base = 1 map + 1 multi-child parent + 3 = 5; +2 reserve = 7 > 6
    await assert.rejects(
      () => run(baseArgs(runDir, { maxAgents: 6 }), cannedAgent(calls), parallelStub, noop, noop),
      /1 map \+ 1 multi-child parent \(1 single-child collapsed, zero cost\) \+ 3 synthesis \+ 2 reserved diagram batch\(es\) = 7 agents > --max-agents 6/,
    );
    assert.equal(calls.length, 0, 'no agent may be dispatched on an over-budget run');
    // exactly at the ceiling: passes pre-dispatch and completes
    const result = await run(baseArgs(runDir, { maxAgents: 7 }), cannedAgent([]), parallelStub, noop, noop);
    assert.ok(result.agentsDispatched <= 7);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test('diagram batches widen to fit the remaining budget', async (t) => {
  const runDir = tempRun();
  t.after(() => rmSync(runDir, { recursive: true, force: true }));
  const calls = [];
  // 20 data-model pages + 1 flow = 21 targets; maxAgents 7 leaves
  // 7 - (1 map + 1 parent + 1 flows) - 2 = 2 batches: ceil(21/8)=3 > 2, so the
  // batch widens to ceil(21/2)=11 (<= cap 16) and the run completes at 7
  const pagesMeta = [{ file: PAGE.file, type: PAGE.type, title: PAGE.title, source_paths: PAGE.source_paths }];
  for (let i = 0; i < 20; i++) {
    pagesMeta.push({ file: `data-models/DM ${i}.md`, type: 'data-model', title: `DM ${i}`, source_paths: ['app/x.py'] });
  }
  const base = cannedAgent(calls);
  const agentStub = async (prompt, opts) => {
    if (opts.label.startsWith('map:')) {
      calls.push({ prompt, opts });
      const recordsPath = pathFromPrompt(prompt);
      writeRecords(recordsPath, { pages: [PAGE], reports: [{ community_id: 1, summary: 's', findings: [{ text: 'f' }] }], covered: [], excluded: [] });
      return { unit: 'unit-01', path: recordsPath, pagesMeta, reportIds: [1], notes: '' };
    }
    return base(prompt, opts);
  };
  const run = loadWorkflow();
  const result = await run(baseArgs(runDir, { maxAgents: 7 }), agentStub, parallelStub, noop, noop);
  assert.equal(result.diagrams.targets, 21);
  assert.equal(result.diagrams.batchSize, 11); // widened from 8 to fit 2 slots (<= cap 12)
  const diagramCalls = calls.filter((c) => c.opts.label.startsWith('diagram:'));
  assert.equal(diagramCalls.length, 2);
  assert.equal(result.agentsDispatched, 7); // exactly at the ceiling, no mid-flight death
});

test('widen-to-fit refuses to blow the per-agent diagram cap', async (t) => {
  const runDir = tempRun();
  t.after(() => rmSync(runDir, { recursive: true, force: true }));
  const calls = [];
  const pagesMeta = [{ file: PAGE.file, type: PAGE.type, title: PAGE.title, source_paths: PAGE.source_paths }];
  for (let i = 0; i < 40; i++) {
    pagesMeta.push({ file: `data-models/DM ${i}.md`, type: 'data-model', title: `DM ${i}`, source_paths: ['app/x.py'] });
  }
  const base = cannedAgent(calls);
  const agentStub = async (prompt, opts) => {
    if (opts.label.startsWith('map:')) {
      calls.push({ prompt, opts });
      const recordsPath = pathFromPrompt(prompt);
      writeRecords(recordsPath, { pages: [PAGE], reports: [{ community_id: 1, summary: 's', findings: [{ text: 'f' }] }], covered: [], excluded: [] });
      return { unit: 'unit-01', path: recordsPath, pagesMeta, reportIds: [1], notes: '' };
    }
    return base(prompt, opts);
  };
  const run = loadWorkflow();
  // 41 targets, budget leaves 2 batches -> ceil(41/2)=21 > cap 12: fail BEFORE diagram dispatch
  await assert.rejects(
    () => run(baseArgs(runDir, { maxAgents: 7 }), agentStub, parallelStub, noop, noop),
    /over the --max-diagram-batch cap 12/,
  );
  assert.equal(calls.filter((c) => c.opts.label.startsWith('diagram:')).length, 0,
    'no diagram agent may be dispatched once the widened batch cannot fit');
});

test('diagram omission rate over 30% fails the run loudly', async (t) => {
  // Re-review item 2: a widened batch that quietly omits most of its
  // diagrams must not "succeed" — quality collapse is loud.
  const runDir = tempRun();
  t.after(() => rmSync(runDir, { recursive: true, force: true }));
  const calls = [];
  const pagesMeta = [{ file: PAGE.file, type: PAGE.type, title: PAGE.title, source_paths: PAGE.source_paths }];
  for (let i = 0; i < 9; i++) {
    pagesMeta.push({ file: `data-models/DM ${i}.md`, type: 'data-model', title: `DM ${i}`, source_paths: ['app/x.py'] });
  }
  const base = cannedAgent(calls);
  const agentStub = async (prompt, opts) => {
    if (opts.label.startsWith('map:')) {
      calls.push({ prompt, opts });
      const recordsPath = pathFromPrompt(prompt);
      writeRecords(recordsPath, { pages: [PAGE], reports: [{ community_id: 1, summary: 's', findings: [{ text: 'f' }] }], covered: [], excluded: [] });
      return { unit: 'unit-01', path: recordsPath, pagesMeta, reportIds: [1], notes: '' };
    }
    if (opts.label.startsWith('diagram:')) {
      calls.push({ prompt, opts });
      const recordsPath = pathFromPrompt(prompt);
      const n = (prompt.match(/- page type "(flow|data-model)"/g) || []).length;
      writeRecords(recordsPath, { diagrams: [] });
      // validate gate kept failing: only 1 of n embedded per batch
      return { path: recordsPath, embedded: 1, omitted: n - 1 };
    }
    return base(prompt, opts);
  };
  const run = loadWorkflow();
  // 10 targets, 8/10 omitted -> 80% > 30%: throw naming the rate
  await assert.rejects(
    () => run(baseArgs(runDir), agentStub, parallelStub, noop, noop),
    /omission rate 8\/10 = 80% exceeds the 30% limit/,
  );
});

test('a community ending Map without a report fails the run naming it', async (t) => {
  const runDir = tempRun();
  t.after(() => rmSync(runDir, { recursive: true, force: true }));
  const calls = [];
  const agentStub = cannedAgent(calls, {
    map: (recordsPath) => {
      writeRecords(recordsPath, { pages: [PAGE], reports: [], covered: [], excluded: [] });
      return { unit: 'unit-01', path: recordsPath, pagesMeta: [], reportIds: [], notes: '' };
    },
  });
  const run = loadWorkflow();
  await assert.rejects(
    () => run(baseArgs(runDir), agentStub, parallelStub, noop, noop),
    /community 1 .*NO report/,
  );
});

test('missing runDir / prompts / parents args fail loudly', async () => {
  const run = loadWorkflow();
  await assert.rejects(
    () => run(baseArgs(undefined), cannedAgent([]), parallelStub, noop, noop),
    /args\.runDir is required/,
  );
  await assert.rejects(
    () => run(baseArgs('/tmp/x', { prompts: undefined }), cannedAgent([]), parallelStub, noop, noop),
    /args\.prompts is required/,
  );
  await assert.rejects(
    () => run(baseArgs('/tmp/x', { parents: undefined }), cannedAgent([]), parallelStub, noop, noop),
    /args\.parents is required/,
  );
});

test('an oversized return payload trips the size guard', async (t) => {
  const runDir = tempRun();
  t.after(() => rmSync(runDir, { recursive: true, force: true }));
  const calls = [];
  const base = cannedAgent(calls);
  const agentStub = async (prompt, opts) => {
    if (opts.label === 'tours') {
      calls.push({ prompt, opts });
      const recordsPath = pathFromPrompt(prompt);
      writeRecords(recordsPath, { pages: [] });
      // a stub gone wrong: inlining ~0.5MB through the return channel
      return { path: recordsPath, toursMeta: [{ file: 'tours/X Tour.md', title: 'x'.repeat(500000) }] };
    }
    return base(prompt, opts);
  };
  const run = loadWorkflow();
  await assert.rejects(
    () => run(baseArgs(runDir), agentStub, parallelStub, noop, noop),
    /records must stay on disk/,
  );
});

test('index agent producing fewer than three meta pages fails the run', async (t) => {
  const runDir = tempRun();
  t.after(() => rmSync(runDir, { recursive: true, force: true }));
  const calls = [];
  const base = cannedAgent(calls);
  const agentStub = async (prompt, opts) => {
    if (opts.label === 'index') {
      calls.push({ prompt, opts });
      const recordsPath = pathFromPrompt(prompt);
      writeRecords(recordsPath, { meta_pages: [] });
      return { path: recordsPath, files: [] };
    }
    return base(prompt, opts);
  };
  const run = loadWorkflow();
  await assert.rejects(
    () => run(baseArgs(runDir), agentStub, parallelStub, noop, noop),
    /meta page/,
  );
});
