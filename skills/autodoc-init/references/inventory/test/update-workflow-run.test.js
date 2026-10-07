// test/update-workflow-run.test.js — DYNAMIC execution of update-workflow.js
// with stubbed sandbox globals (same harness as init-workflow-run.test.js).
//
// Regression anchor (consumer break, 2026-08-07): the externalized refresh
// prompt demanded {{kind}} per checklist symbol, but real callers pass the
// coverage-slice shape {community,name,line,file} (SKILL.md Step 6.5) — the
// strict engine then killed every Refresh with "{{kind}} has no scalar value"
// (the old inline prompt silently rendered "[undefined]"). The refresh
// checklist identity is name + file:line; kind is forwarded when present and
// printed only under {{#if kind}} (gh#527), and these tests pin both halves.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { dumpPrompts } from '../../prompts/render-prompt.js';

const here = dirname(fileURLToPath(import.meta.url));
const updateWfPath = join(
  here, '..', '..', '..', '..', 'autodoc-update', 'references', 'update-workflow.js',
);

// E-0743: update-workflow.js refuses a POSIX runDir on win32 — the harness's file
// tools resolve `/run` under C:\, not where the runbook reads (see the guard's own
// comment). These tests are about prompt rendering, records paths and manifest
// joining, not path syntax, so the fixture DERIVES a runDir that is native on
// whichever platform the suite runs on: a drive-lettered path under Windows' temp
// dir, `/tmp/...` on linux/darwin. A literal of either kind would be wrong on the
// other CI leg. The guard itself is pinned separately, at the bottom of this file.
const RUN_DIR = join(tmpdir(), 'autodoc-update-test-run').replace(/\\/g, '/');
const recordsPath = (key, runDir = RUN_DIR) => `${runDir}/records/refresh-${key}.json`;
const CHECKLIST_PATH = `${RUN_DIR}/checklists/1.json`;
const reEscape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function loadWorkflow() {
  const src = readFileSync(updateWfPath, 'utf8').replace(/^export const meta/m, 'const meta');
  return new AsyncFunction('args', 'agent', 'parallel', 'phase', 'log', src);
}

function baseArgs(symbols, over = {}) {
  return {
    prompts: dumpPrompts(),
    runDir: RUN_DIR, // epic 706: refresh agents emit records under <runDir>/records/
    repoPath: '/repo', autodocPath: '/autodoc/toy', repoName: 'o/r',
    fromSha: 'a'.repeat(40), toSha: 'b'.repeat(40), date: '2026-08-07',
    taxonomyDocPath: '/tax.md',
    changedCommunities: [{
      key: '1', label: 'core',
      descriptor: { member_count: 2, top_symbols: ['handler'], top_prefixes: ['app'] },
      changedFiles: ['app/x.py'], affectedPages: ['services/Alpha.md'],
      symbols,
    }],
    deletedFiles: [],
    ...over,
  };
}

function cannedAgent(calls, runDir = RUN_DIR) {
  return async (prompt, opts) => {
    calls.push({ prompt, opts });
    const label = opts.label;
    if (label.startsWith('refresh:')) {
      return {
        community: label.split(':')[1],
        // epic 706: the return is a light manifest; `path` names the records
        // file on disk and must equal the recordsPath the prompt handed out.
        path: recordsPath(label.split(':')[1], runDir),
        updated: ['services/Alpha.md'], created: [], removed: [],
        covered: [{ community: '1', name: 'handler', line: 1 }], excluded: [],
      };
    }
    if (label === 'reconcile') return { summary: 'nav reconciled', path: recordsPath('reconcile', runDir) };
    if (label === 'audit') {
      // epic 706: the audit returns a records path like refresh does.
      return { audited: ['services/Alpha.md'], fixed: [], findings: [], path: recordsPath('audit', runDir) };
    }
    throw new Error(`unexpected agent label ${label}`);
  };
}

const parallelStub = async (fns) => Promise.all(fns.map((f) => f()));
const noop = () => {};

// THE consumer break: a caller passing the documented coverage-slice symbol
// shape (no `kind`) must still get a working Refresh phase.
test('refresh renders for coverage-slice symbols (no kind) — the live consumer shape', async () => {
  const calls = [];
  const run = loadWorkflow();
  const result = await run(
    baseArgs([{ community: '1', name: 'handler', line: 1, file: 'app/x.py' }]),
    cannedAgent(calls), parallelStub, noop, noop,
  );
  assert.equal(result.communitiesRefreshed, 1,
    'a symbols entry without `kind` must not kill the Refresh phase');
  const refreshCall = calls.find((c) => c.opts.label === 'refresh:1');
  assert.ok(refreshCall, 'the refresh agent must be dispatched');
  assert.match(refreshCall.prompt, /^ {4}- handler {2}\(app\/x\.py:1\)$/m,
    'checklist row must name the symbol and its file:line');
});

test('refresh renders for artifact-shape symbols (extra keys tolerated, not required)', async () => {
  const calls = [];
  const run = loadWorkflow();
  const result = await run(
    baseArgs([{ kind: 'function', name: 'handler', file: 'app/x.py', line: 1 }]),
    cannedAgent(calls), parallelStub, noop, noop,
  );
  assert.equal(result.communitiesRefreshed, 1);
  const refreshCall = calls.find((c) => c.opts.label === 'refresh:1');
  // gh#527: a documents citation is keyed on file+kind+name, so the row must
  // hand the agent the store's kind to copy rather than leave it to guess.
  assert.match(refreshCall.prompt, /^ {4}- handler {2}\(app\/x\.py:1\) {2}kind: function$/m,
    'the artifact kind was dropped from the checklist row');
  assert.ok(!refreshCall.prompt.includes('undefined'),
    'no checklist row may render an undefined field');
});

test('empty and absent symbols fall back to the read-the-diff line', async () => {
  for (const symbols of [[], undefined]) {
    const calls = [];
    const run = loadWorkflow();
    const result = await run(baseArgs(symbols), cannedAgent(calls), parallelStub, noop, noop);
    assert.equal(result.communitiesRefreshed, 1);
    const refreshCall = calls.find((c) => c.opts.label === 'refresh:1');
    assert.match(refreshCall.prompt, /no pre-extracted symbols/);
  }
});

test('a symbols entry missing its identity (name/file/line) still fails loudly', async () => {
  // name+file:line IS the checklist contract — silently dropping identity
  // would un-anchor the coverage gate, so the engine's loud error must hold.
  const run = loadWorkflow();
  await assert.rejects(
    () => run(
      baseArgs([{ community: '1', line: 1 }]),
      cannedAgent([]), parallelStub, noop, noop,
    ),
    /\{\{name\}\} has no scalar value/,
  );
});


// ---------------------------------------------------------------------------
// E-0743: the coverage checklist can arrive BY PATH. It has to: the workflow
// interpolates every symbol into the PROMPT TEXT, and the pending catch-up run
// is 4636 symbols across 24 units — 535KB of args, 94.9% of it symbols, against
// the 512KB cap on a baked variant script. These pin both halves of the branch,
// because a pass-through that silently kept rendering the rows would cut
// nothing while looking like it worked.
// ---------------------------------------------------------------------------
function withChecklist(over = {}) {
  const args = baseArgs([{ name: 'handler', file: 'app/x.py', line: 1 }]);
  args.changedCommunities[0] = {
    ...args.changedCommunities[0],
    checklistPath: CHECKLIST_PATH,
    symbolCount: 4636,
    ...over,
  };
  return args;
}

test('a checklistPath entry sends the PATH and stops interpolating the rows', async () => {
  const calls = [];
  const run = loadWorkflow();
  const result = await run(withChecklist(), cannedAgent(calls), parallelStub, noop, noop);
  assert.equal(result.communitiesRefreshed, 1);
  const prompt = calls.find((c) => c.opts.label === 'refresh:1').prompt;
  assert.match(prompt, new RegExp(`READ ${reEscape(CHECKLIST_PATH)} IN FULL`),
    'the agent was never told where its checklist is');
  assert.match(prompt, /4636 row\(s\) of \{name, file, line, kind\}/,
    'the agent was not told how big the file it must read is');
  assert.ok(!/^ {4}- handler {2}\(app\/x\.py:1\)$/m.test(prompt),
    'the symbol rows are STILL in the prompt text — the payload was not cut');
  assert.ok(!/no pre-extracted symbols/.test(prompt),
    'the no-symbols fallback fired despite a checklist being delivered');
});

test('without checklistPath the rows still render inline, unchanged', async () => {
  const calls = [];
  const run = loadWorkflow();
  await run(baseArgs([{ name: 'handler', file: 'app/x.py', line: 1 }]),
    cannedAgent(calls), parallelStub, noop, noop);
  const prompt = calls.find((c) => c.opts.label === 'refresh:1').prompt;
  assert.match(prompt, /^ {4}- handler {2}\(app\/x\.py:1\)$/m);
  assert.ok(!/delivered BY PATH/.test(prompt),
    'the by-path branch fired for a caller that passed no checklistPath');
});

// The empty-checklist community keeps the inline shape on purpose (the builder
// writes it no path), so the read-the-diff fallback must still be reachable
// while the feature is on for its siblings.
test('an empty checklistPath falls through to the ordinary symbols branch', async () => {
  const calls = [];
  const run = loadWorkflow();
  await run(withChecklist({ checklistPath: '', symbolCount: 0, symbols: [] }),
    cannedAgent(calls), parallelStub, noop, noop);
  const prompt = calls.find((c) => c.opts.label === 'refresh:1').prompt;
  assert.match(prompt, /no pre-extracted symbols/);
  assert.ok(!/delivered BY PATH/.test(prompt));
});

// gh#696: a unit whose file is documented by a page ANOTHER unit owns used to
// get affectedPages [] and the "likely all-new code" line, which points its
// agent at duplicate records for symbols that page already documents.
function withContextPage(symbols) {
  const args = baseArgs(symbols);
  args.changedCommunities[0] = {
    ...args.changedCommunities[0],
    affectedPages: [],
    contextPages: [{ page: 'services/App Service.md', owner: 'unit-01' }],
  };
  return args;
}

test('a page owned by another unit is shown read-only, not as all-new code', async () => {
  const calls = [];
  const run = loadWorkflow();
  await run(withContextPage([{ name: 'handler', file: 'app/x.py', line: 1 }]),
    cannedAgent(calls), parallelStub, noop, noop);
  const prompt = calls.find((c) => c.opts.label === 'refresh:1').prompt;
  assert.ok(!/likely all-new code/.test(prompt),
    'the agent was told its documented file is all-new code');
  assert.match(prompt, /^ {4}- services\/App Service\.md {2}\(owned by unit-01; do not re-emit\)$/m,
    'the page another unit owns is not shown with its owner');
  assert.match(prompt, /READ-ONLY context/);
});

test('without contextPages the all-new-code line is unchanged', async () => {
  const calls = [];
  const run = loadWorkflow();
  const args = baseArgs([{ name: 'handler', file: 'app/x.py', line: 1 }]);
  args.changedCommunities[0].affectedPages = [];
  await run(args, cannedAgent(calls), parallelStub, noop, noop);
  const prompt = calls.find((c) => c.opts.label === 'refresh:1').prompt;
  assert.match(prompt, /\(none — likely all-new code\)/);
  assert.ok(!/READ-ONLY context/.test(prompt));
});

test('an inline checklist row carries its coveredBy page', async () => {
  const calls = [];
  const run = loadWorkflow();
  await run(withContextPage([
    { name: 'handler', file: 'app/x.py', line: 1, kind: 'function', coveredBy: 'services/App Service.md' },
    { name: 'other', file: 'app/x.py', line: 2 },
  ]), cannedAgent(calls), parallelStub, noop, noop);
  const prompt = calls.find((c) => c.opts.label === 'refresh:1').prompt;
  assert.match(prompt,
    /^ {4}- handler {2}\(app\/x\.py:1\) {2}kind: function {2}coveredBy: services\/App Service\.md$/m,
    'the coveredBy marker was dropped between the builder and the prompt');
  assert.match(prompt, /^ {4}- other {2}\(app\/x\.py:2\)$/m);
});

// A page citing a row's FILE says nothing about whether it documents the row's
// SYMBOL, so coveredBy must send the agent to read the page, not straight to
// "covered".
test('coveredBy asks the agent to check the page before counting the row covered', async () => {
  const calls = [];
  const run = loadWorkflow();
  await run(withContextPage([{ name: 'handler', file: 'app/x.py', line: 1, coveredBy: 'services/App Service.md' }]),
    cannedAgent(calls), parallelStub, noop, noop);
  const prompt = calls.find((c) => c.opts.label === 'refresh:1').prompt.replace(/\s+/g, ' ');
  assert.match(prompt, /A cited file is not a documented symbol, so read that page: if it already documents this symbol, list the row in "covered" and emit nothing;/);
  assert.match(prompt, /otherwise treat it like any other row \(a new page if it is a distinct entity, else "excluded" with a reason naming that page and its owner\)/);
  assert.ok(!/so it meets \(a\)/.test(prompt), 'coveredBy is still treated as proof of coverage');
});

// gh#695: a range of deletions alone reaches the agent as an entry with pages
// and no changed files; its prompt must say so without the all-new-code line.
test('a deletion-only entry renders its page, no changed files, and the deprecation step', async () => {
  const calls = [];
  const run = loadWorkflow();
  const args = baseArgs([], { deletedFiles: ['app/newmod.py'] });
  args.changedCommunities[0] = {
    ...args.changedCommunities[0],
    changedFiles: [], affectedPages: ['components/Newmod.md'], symbols: [],
  };
  await run(args, cannedAgent(calls), parallelStub, noop, noop);
  const prompt = calls.find((c) => c.opts.label === 'refresh:1').prompt;
  assert.match(prompt, /in this community \(added\/modified\):\n {4}\(none\)\n/);
  assert.match(prompt, /^ {4}- components\/Newmod\.md$/m);
  assert.ok(!/likely all-new code/.test(prompt));
  assert.match(prompt, /removed in this range:\n {4}- app\/newmod\.py\n/);
  assert.match(prompt, /'frontmatter\.status: "deprecated"'/);
});

test('STEP 4 tells the agent to trim, not deprecate, a page that still cites live files', async () => {
  const calls = [];
  const run = loadWorkflow();
  await run(baseArgs([], { deletedFiles: ['app/old.py'] }), cannedAgent(calls), parallelStub, noop, noop);
  const prompt = calls.find((c) => c.opts.label === 'refresh:1').prompt.replace(/\s+/g, ' ');
  assert.match(prompt, /An affected page that cites deleted files alongside live ones is not deprecated: re-emit it without the deleted paths in 'source_paths' and without the claims only they supported, and list it in 'updated'\./);
});

// ---------------------------------------------------------------------------
// epic 706: the Audit phase emits RECORDS. Before this epic the audit branch had
// NO test at all -- `auditPages` was never passed by any case -- so the whole
// phase, and now its records contract, would have shipped unexercised.
// ---------------------------------------------------------------------------
test('audit phase emits records and joins the manifest', async () => {
  const calls = [];
  const run = loadWorkflow();
  const args = baseArgs([{ name: 'handler', file: 'app/x.py', line: 1 }], {
    auditPages: [{ page: 'services/Alpha.md', source_paths: ['app/x.py'] }],
  });
  await run(args, cannedAgent(calls), parallelStub, noop, noop);

  const auditCall = calls.find((c) => c.opts.label === 'audit');
  assert.ok(auditCall, 'audit agent was never dispatched');
  // The prompt must hand the agent its records path and forbid the live tree.
  assert.match(auditCall.prompt,
    new RegExp(`ONLY write is the records JSON at ${reEscape(recordsPath('audit'))}`));
  assert.match(auditCall.prompt, /READ-ONLY to you/);
  // The old edit-the-page instruction is gone; its inverse is asserted so a
  // revert fails loudly rather than passing on absence of evidence.
  assert.ok(!/FIX it \(edit the body/.test(auditCall.prompt), 'audit still instructs a live-tree page edit');
  assert.match(auditCall.prompt, /EMIT A FULL RECORD/);
});

test('audit returning a records path it was not given is a loud failure', async () => {
  const run = loadWorkflow();
  const args = baseArgs([{ name: 'handler', file: 'app/x.py', line: 1 }], {
    auditPages: [{ page: 'services/Alpha.md', source_paths: ['app/x.py'] }],
  });
  const wrongPath = async (prompt, opts) => {
    if (opts.label.startsWith('refresh:')) {
      const c = opts.label.split(':')[1];
      return { community: c, path: recordsPath(c), updated: [], created: [], removed: [], covered: [], excluded: [] };
    }
    if (opts.label === 'audit') return { audited: [], fixed: [], findings: [], path: '/somewhere/else.json' };
    if (opts.label === 'reconcile') return { summary: 'nav reconciled', path: recordsPath('reconcile') };
    throw new Error(`unexpected label ${opts.label}`);
  };
  await assert.rejects(
    () => run(args, wrongPath, parallelStub, noop, noop),
    /audit agent returned records path \/somewhere\/else\.json, expected/,
  );
});

// ---------------------------------------------------------------------------
// epic 706: Reconcile emits records too, and the nav layer it used to hand-edit
// is now the renderer's. These pin BOTH halves -- the records contract, and the
// deletion of the tasks that would silently fight the renderer if reinstated.
// ---------------------------------------------------------------------------
test('reconcile emits records and no longer hand-edits the nav layer', async () => {
  const calls = [];
  const run = loadWorkflow();
  const result = await run(baseArgs([{ name: 'handler', file: 'app/x.py', line: 1 }]),
    cannedAgent(calls), parallelStub, noop, noop);

  const rec = calls.find((c) => c.opts.label === 'reconcile');
  assert.ok(rec, 'reconcile agent was never dispatched');
  assert.match(rec.prompt, /ONLY WRITE IS THE RECORDS JSON/);
  assert.match(rec.prompt, /THE NAV LAYER IS NO LONGER YOURS/);
  // The renderer owns these basenames; a record claiming one is rejected by the
  // store, so the prompt must not ask for them. Asserting the INVERSE of the
  // deleted tasks, so reinstating them fails here rather than at ingest.
  assert.ok(!/update its <folder>\/_index\.md/.test(rec.prompt), 'reconcile still hand-edits _index.md');
  assert.ok(!/Update index\.md \(master catalog\)/.test(rec.prompt), 'reconcile still hand-edits index.md');
  assert.ok(!/Nav-count audit/.test(rec.prompt), 'reconcile still runs the nav-count audit the renderer owns');
  // Every records file the run produced is exposed for the store step.
  assert.deepEqual(result.recordsFiles, [recordsPath('1'), recordsPath('reconcile')]);
});

test('reconcile returning a records path it was not given is a loud failure', async () => {
  const run = loadWorkflow();
  const wrong = async (prompt, opts) => {
    if (opts.label.startsWith('refresh:')) {
      const c = opts.label.split(':')[1];
      return { community: c, path: recordsPath(c), updated: [], created: [], removed: [], covered: [], excluded: [] };
    }
    if (opts.label === 'reconcile') return { summary: 'x', path: '/elsewhere.json' };
    throw new Error(`unexpected label ${opts.label}`);
  };
  await assert.rejects(
    () => run(baseArgs([{ name: 'handler', file: 'app/x.py', line: 1 }]), wrong, parallelStub, noop, noop),
    /reconcile agent returned records path \/elsewhere\.json, expected/,
  );
});

// ---------------------------------------------------------------------------
// E-0743: the runDir COHERENCE guard. On Windows the harness's file tools resolve
// a POSIX `/tmp/...` to `C:	mp` while Git Bash resolves it to the user temp dir,
// and `C:	mp` EXISTS — so a POSIX runDir sends every refresh agent to a directory
// the runbook never reads, they all report success, and the step after the fan-out
// fails blaming a stale paste. The check cannot live in the shell (MSYS rewrites
// such a path in argv AND in the environment, so bash hands every native tool an
// already-native string); the hand-baked Workflow args are the one value that
// never passes through a shell, so the workflow sees it raw and checks it here.
//
// THESE TESTS PASS NO PLATFORM AND STUB NOTHING, and that is the point. The first
// version of the guard read `process.platform`, and these tests stubbed it. Both
// were wrong in the same way: this workflow does not run under Node. The sandbox is
// Bun-based and has no `process`, so the guard threw `ReferenceError: process is
// not defined` and killed the real run before any agent started — while the suite
// stayed green, because a stub only exists where the thing being stubbed does.
// A guard verified in an environment it never executes in is not verified.
// The guard now compares the ARGS to each other, so the test drives it the same way
// the sandbox will: with data, under whatever engine happens to be running.
// ---------------------------------------------------------------------------
const oneSymbol = [{ name: 'handler', file: 'app/x.py', line: 1 }];

test('the guard never touches the runtime — no `process` reference survives in the source', () => {
  // The regression that mattered: the module is executed by a Bun-based sandbox with
  // no `process` binding at all, so ANY reference is a launch-time ReferenceError
  // rather than a failed check. Asserted against the source because no amount of
  // in-process testing under Node can observe its absence.
  const src = readFileSync(updateWfPath, 'utf8');
  const hits = src.split('\n')
    .map((line, i) => [i + 1, line])
    .filter(([, line]) => !/^\s*(\/\/|\*)/.test(line))   // a comment ABOUT process is not a reference
    .filter(([, line]) => /(^|[^.\w])process\s*\./.test(line));
  assert.deepEqual(hits, [],
    `update-workflow.js must not reference \`process\` — the Workflow sandbox has none, and a ` +
    `reference throws before any agent runs:\n` + hits.map(([n, l]) => `  :${n} ${l.trim()}`).join('\n'));
});

test('a drive-rooted autodocPath beside a POSIX runDir is refused, naming both and the fix', async () => {
  const run = loadWorkflow();
  await assert.rejects(
    () => run(
      baseArgs(oneSymbol, { runDir: '/run', autodocPath: 'C:/Users/x/autodoc' }),
      cannedAgent([], '/run'), parallelStub, noop, noop,
    ),
    (err) => {
      // Substance, not merely "it threw": the message has to identify BOTH offending
      // values, say where the native tools will actually land, and point at the step
      // that mints runDir — a reader who only learns something was rejected cannot act.
      assert.match(err.message, /args\.runDir \(\/run\) is POSIX-rooted/,
        'the error does not name the rejected runDir');
      assert.match(err.message, /autodocPath \(C:\/Users\/x\/autodoc\) is drive-rooted/,
        'the error does not name the autodocPath it disagrees with');
      assert.match(err.message, /C:\\tmp/,
        'the error does not say where the native file tools will actually resolve it');
      assert.match(err.message, /native temp dir/,
        'the error does not say what to mint runDir from');
      assert.match(err.message, /Step 4/,
        'the error does not point at the skill step that mints runDir');
      return true;
    },
  );
});

test('a drive-rooted runDir beside a drive-rooted autodocPath runs — not a blanket refusal', async () => {
  const calls = [];
  const run = loadWorkflow();
  const nativeDir = 'C:/Temp/autodoc-update-guard';
  const result = await run(
    baseArgs(oneSymbol, { runDir: nativeDir, autodocPath: 'C:/Users/x/autodoc' }),
    cannedAgent(calls, nativeDir), parallelStub, noop, noop,
  );
  assert.equal(result.communitiesRefreshed, 1);
  assert.deepEqual(result.recordsFiles, [
    `${nativeDir}/records/refresh-1.json`,
    `${nativeDir}/records/refresh-reconcile.json`,
  ], 'records paths must be derived from the native runDir as given');
});

test('both POSIX-rooted is accepted — a POSIX box is coherent, not broken', async () => {
  const calls = [];
  const run = loadWorkflow();
  const result = await run(
    baseArgs(oneSymbol, { runDir: '/run', autodocPath: '/home/x/autodoc' }),
    cannedAgent(calls, '/run'), parallelStub, noop, noop,
  );
  assert.equal(result.communitiesRefreshed, 1);
  assert.deepEqual(result.recordsFiles, ['/run/records/refresh-1.json', '/run/records/refresh-reconcile.json']);
});

test('checkRecordsCmd reaches refresh AND audit, each with the stop on a no-PROBLEM fault', async () => {
  const cmd = 'python -m autodoc_graph check-records --db /s/graph.db --corpus toy';
  const calls = [];
  const run = loadWorkflow();
  await run(baseArgs([{ name: 'handler', file: 'app/x.py', line: 1 }], {
    checkRecordsCmd: cmd,
    auditPages: [{ page: 'services/Alpha.md', source_paths: ['app/x.py'] }],
  }),
    cannedAgent(calls), parallelStub, noop, noop);
  for (const label of ['refresh:1', 'audit']) {
    const call = calls.find((c) => c.opts.label === label);
    assert.ok(call, `${label} agent not dispatched`);
    assert.ok(call.prompt.includes(`${cmd} --records "`), `${label}: command not rendered`);
    assert.match(call.prompt, /non-zero exit that prints NO\s+PROBLEM line/, label);
    assert.match(call.prompt, /do NOT re-run\s+it/, label);
  }
  const audit = calls.find((c) => c.opts.label === 'audit').prompt;
  assert.match(audit, /first in 'findings'/);
});
