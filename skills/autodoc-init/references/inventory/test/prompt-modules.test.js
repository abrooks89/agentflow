// test/prompt-modules.test.js — the prompt module seam (epic 704 scope 1,
// success criterion 9): six extracted prompts + parent-report render from
// fixture contexts WITHOUT importing any workflow script; the template engine
// inlined in the workflow scripts is byte-identical to engine.js; and no
// record-emitting prompt instructs an agent to write page content.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { renderTemplate } from '../../prompts/engine.js';
import { PROMPTS, dumpPrompts } from '../../prompts/render-prompt.js';

const here = dirname(fileURLToPath(import.meta.url));
const promptsDir = join(here, '..', '..', 'prompts');
const initWf = join(here, '..', '..', 'init-workflow.js');
const updateWf = join(
  here, '..', '..', '..', '..', 'autodoc-update', 'references', 'update-workflow.js',
);

// ---- fixture contexts, one per prompt (the 707 driver builds these shapes)
export const FIXTURE_CONTEXTS = {
  map: {
    autodocName: 'toy-autodoc', taxonomyDocPath: '/tax.md',
    repoName: 'o/r', repoPath: '/repo', unitKey: 'unit-01',
    recordsPath: '/run/records/map-unit-01.json',
    communitiesLine: '1:"core"',
    communities: [{
      id: 1, label: 'core', memberCount: 2, topPrefixes: 'app',
      topSymbols: 'handler, Widget',
      symbols: [
        { kind: 'function', name: 'handler', file: 'app/x.py', line: 1 },
        { kind: 'class', name: 'Widget', file: 'app/x.py', line: 10 },
      ],
    }],
  },
  'parent-report': {
    autodocName: 'toy-autodoc', communityId: 9, label: 'root',
    recordsPath: '/run/records/parent-9.json',
    childReportFiles: [
      { label: '1', communityId: 1, path: '/run/records/map-unit-01.json' },
      { label: '5', communityId: 2, path: '/run/records/map-unit-02.json' },
    ],
  },
  flows: {
    autodocName: 'toy-autodoc', taxonomyDocPath: '/tax.md',
    repoName: 'o/r', repoPath: '/repo',
    recordsPath: '/run/records/flows.json',
    catalog: 'service :: Alpha :: app/x.py', notes: '- [unit-01] flow candidate',
  },
  diagram: {
    repoPath: '/repo', scratchPath: '/scratch',
    recordsPath: '/run/records/diagrams-1.json',
    validateMermaidCmd: 'python validate_mermaid.py', commitSha: 'abc123',
    targets: [{ kind: 'flow', title: 'Alpha Flow', kindGuide: 'a sequenceDiagram', sourcePaths: 'app/x.py' }],
  },
  tours: {
    autodocName: 'toy-autodoc', taxonomyDocPath: '/tax.md',
    recordsPath: '/run/records/tours.json',
    catalog: 'service :: Alpha :: app/x.py', flowsList: 'flow :: Alpha Flow',
    tourOrderPath: '/inv/tour-order.js',
  },
  index: {
    autodocName: 'toy-autodoc', taxonomyDocPath: '/tax.md', repoName: 'o/r',
    recordsPath: '/run/records/index.json',
    catalog: 'services/Alpha.md :: service :: Alpha', typeSummary: 'service: 1',
  },
  refresh: {
    autodocName: 'toy-autodoc', taxonomyDocPath: '/tax.md',
    recordsPath: '/run/records/refresh-core.json', // epic 706: refresh writes records, not pages
    repoName: 'o/r', repoPath: '/repo', autodocPath: '/autodoc',
    fromShort: 'aaaaaaaa', toShort: 'bbbbbbbb',
    communityKey: 'core', label: 'core', topPrefixes: 'app', topSymbols: '',
    changedFiles: ['app/x.py'], affectedPages: ['services/Alpha.md'],
    symbols: [{ name: 'handler', file: 'app/x.py', line: 1 }], // identity is name + file:line; kind optional
    deletedFiles: [], date: '2026-08-07',
  },
};

// ---- engine semantics -------------------------------------------------------
test('engine substitutes, iterates, and guards missing keys', () => {
  assert.equal(renderTemplate('x {{a}} y', { a: 1 }), 'x 1 y');
  assert.equal(
    renderTemplate('{{#each xs}}[{{n}}]{{/each}}', { xs: [{ n: 1 }, { n: 2 }] }),
    '[1][2]',
  );
  assert.equal(renderTemplate('{{#each xs}}{{.}};{{/each}}', { xs: ['a', 'b'] }), 'a;b;');
  assert.equal(renderTemplate('{{#if s}}Y{{/if}}', { s: '' }), '');
  assert.equal(renderTemplate('{{#if s}}Y{{/if}}', { s: 'x' }), 'Y');
  assert.equal(renderTemplate('{{^xs}}none{{/^xs}}', { xs: [] }), 'none');
  assert.equal(renderTemplate('{{^xs}}none{{/^xs}}', { xs: [1] }), '');
  // outer-scope fallthrough inside each
  assert.equal(
    renderTemplate('{{#each xs}}{{n}}-{{outer}};{{/each}}', { xs: [{ n: 1 }], outer: 'o' }),
    '1-o;',
  );
  assert.throws(() => renderTemplate('{{missing}}', {}), /missing/);
  assert.throws(() => renderTemplate('{{#each nope}}x{{/each}}', {}), /nope/);
  assert.throws(() => renderTemplate('{{#if x}}unclosed', { x: 1 }), /unclosed/);
});

// ---- module shape + rendering ----------------------------------------------
test('all seven prompt modules export id/schema/template/contextSpec/render', () => {
  const ids = Object.keys(PROMPTS).sort();
  assert.deepEqual(ids, ['diagram', 'flows', 'index', 'map', 'parent-report', 'refresh', 'tours']);
  for (const mod of Object.values(PROMPTS)) {
    assert.equal(typeof mod.template, 'string');
    assert.equal(typeof mod.render, 'function');
    assert.equal(typeof mod.schema, 'object');
    assert.equal(typeof mod.contextSpec, 'object');
  }
});

test('every prompt renders from its fixture context', () => {
  for (const [id, ctx] of Object.entries(FIXTURE_CONTEXTS)) {
    const text = PROMPTS[id].render(ctx);
    assert.ok(text.length > 200, `${id} rendered suspiciously short`);
    assert.ok(!text.includes('{{'), `${id} left unrendered placeholders`);
  }
});

test('map prompt carries the checklist rows and the report ask', () => {
  const text = PROMPTS.map.render(FIXTURE_CONTEXTS.map);
  assert.match(text, /community 1 "core" — 2 symbols — main directories: app — key symbols: handler, Widget/);
  assert.match(text, /- \[function\] handler {2}\(app\/x\.py:1\)/);
  assert.match(text, /COMMUNITY REPORTS/);
  assert.match(text, /NEVER return an "id"/);
});

test('refresh prompt emits RECORDS, not live-tree edits, and keeps its fallbacks', () => {
  const text = PROMPTS.refresh.render(FIXTURE_CONTEXTS.refresh);
  assert.match(text, /aaaaaaaa\.\.bbbbbbbb/);
  assert.match(text, /- app\/x\.py/);
  assert.match(text, / {4}\(none\)/); // deletedFiles empty -> fallback line
  const noSyms = PROMPTS.refresh.render({ ...FIXTURE_CONTEXTS.refresh, symbols: [] });
  assert.match(noSyms, /no pre-extracted symbols/);

  // epic 706: the page-editing instruction is GONE. This assertion is the
  // inverse of the one it replaces ("EDIT it"), so it fails loudly if the
  // conversion is ever reverted rather than passing on absence of evidence.
  assert.ok(!/EDIT it/.test(text), 'refresh still instructs a live-tree page edit');
  assert.match(text, /EMIT A FULL RECORD/);
  // A record REPLACES a page, so the prompt must say so — an agent treating it
  // as a patch silently drops every field it does not repeat.
  assert.match(text, /a record is not a patch/i);
  // Deletion has no store concept; it rides the taxonomy's own status field.
  assert.match(text, /frontmatter\.status: "deprecated"/);
});

// E-0743: the checklist may be delivered BY PATH instead of interpolated into
// the prompt. The branch is the whole saving, so both sides are pinned here at
// the prompt level (the workflow pass-through is pinned in
// update-workflow-run.test.js).
test('refresh renders the checklist by PATH when checklistPath is set, rows when it is not', () => {
  const inline = PROMPTS.refresh.render(FIXTURE_CONTEXTS.refresh);
  assert.match(inline, /^ {4}- handler {2}\(app\/x\.py:1\)$/m);
  assert.ok(!/delivered BY PATH/.test(inline));

  const byPath = PROMPTS.refresh.render({
    ...FIXTURE_CONTEXTS.refresh,
    checklistPath: '/run/checklists/core.json',
    symbolCount: 4636,
  });
  assert.match(byPath, /READ \/run\/checklists\/core\.json IN FULL/);
  assert.match(byPath, /4636 row\(s\)/);
  assert.ok(!/^ {4}- handler {2}\(app\/x\.py:1\)$/m.test(byPath),
    'the rows are still interpolated — delivering the path saved nothing');
  assert.ok(!/no pre-extracted symbols/.test(byPath),
    'the no-symbols fallback fired alongside a delivered checklist');
  // The coverage rule the checklist serves must survive the branch: an agent
  // handed a path and no rule is an agent with no obligation.
  assert.match(byPath, /Do not silently drop any changed-file symbol\./);
});

// epic 706: the records file is an OBJECT. `load_records_input` rejects a bare
// array with "must contain a JSON object", so a prompt that asks for an array
// fails at the store rather than in review — pin the shape here.
test('refresh prompt asks for the object-shaped records file, never an array', () => {
  const t = PROMPTS.refresh.template;
  assert.match(t, /JSON OBJECT, never a\s+bare array/);
  assert.match(t, /"pages":/);
  assert.ok(!/single JSON array/.test(t), 'refresh asks for a bare array; the store rejects that');
});

// ---- criterion 2 support: record prompts must not instruct page writes ------
test('record prompts allow exactly the records file, never page content', () => {
  // 'refresh' JOINED this list in epic 706 — it was the one page-writing
  // prompt, and the guard excluding it is exactly how the defect stayed
  // invisible to a gate that otherwise covered every sibling.
  for (const id of ['map', 'parent-report', 'flows', 'tours', 'index', 'refresh']) {
    const t = PROMPTS[id].template;
    assert.ok(!/Use Write/i.test(t), `${id} instructs the Write tool`);
    assert.ok(!/WRITE each entity/i.test(t), `${id} kept the old write instruction`);
    assert.match(t, /ONLY file you may\s+write is your records JSON/i,
      `${id} must scope writes to the records file`);
    assert.match(t, /\{\{recordsPath\}\}/, `${id} must thread the records path`);
  }
  // diagram agents may write scratch drafts + their records file — never pages
  assert.match(PROMPTS.diagram.template, /never edit\s+autodoc pages/i);
  assert.match(PROMPTS.diagram.template, /ONLY files you may\s+write are temporary/i);
});

// ---- finding 9: parent roll-up treats child content as untrusted data -------
test('parent-report prompt fences child reports as untrusted data', () => {
  const t = PROMPTS['parent-report'].template;
  assert.match(t, /UNTRUSTED/);
  assert.match(t, /never instructions/i);
  assert.match(t, /do NOT follow them/i);
});

// ---- engine drift gate ------------------------------------------------------
// Finding 8 (re-review): a lazy single .match let an APPENDED second marked
// block ride along unseen — and via function-declaration hoisting the later
// (drifted) renderTemplate would win at runtime. The gate therefore matchAlls
// every marked block, requires EXACTLY ONE per workflow file, and compares
// that block byte-for-byte against engine.js.
const engineBlocks = (src, file) => {
  const all = [...src.matchAll(/\/\/ >>> template-engine[\s\S]*?\/\/ <<< template-engine <<</g)]
    .map((m) => m[0].replace(/\r\n/g, '\n'));
  assert.equal(all.length, 1,
    `${file} must contain EXACTLY ONE marked template-engine block, found ${all.length}`);
  return all[0];
};

test('workflow-inlined engines are byte-identical to engine.js', () => {
  const canonical = engineBlocks(readFileSync(join(promptsDir, 'engine.js'), 'utf8'), 'engine.js');
  assert.equal(engineBlocks(readFileSync(initWf, 'utf8'), 'init-workflow.js'), canonical,
    'init-workflow.js inlined engine drifted from prompts/engine.js');
  assert.equal(engineBlocks(readFileSync(updateWf, 'utf8'), 'update-workflow.js'), canonical,
    'update-workflow.js inlined engine drifted from prompts/engine.js');
});

test('drift gate catches the reviewer append-attack (second drifted block)', () => {
  // the exact attack: append a second, subtly-drifted marked block to a
  // workflow file — hoisting makes the LAST function declaration win, so the
  // gate must refuse the file outright rather than match the first block.
  const real = readFileSync(initWf, 'utf8');
  const drifted = engineBlocks(real, 'init-workflow.js')
    .replace('v.length > 0', 'v.length >= 0');
  const attacked = `${real}\n${drifted}\n`;
  assert.throws(
    () => engineBlocks(attacked, 'init-workflow.js(attacked)'),
    /EXACTLY ONE marked template-engine block, found 2/,
  );
});

// ---- criterion 9: the CLI renders WITHOUT importing any workflow ------------
test('render-prompt.js CLI renders all prompts from fixture contexts', () => {
  const dir = mkdtempSync(join(tmpdir(), 'render-prompt-'));
  try {
    for (const [id, ctx] of Object.entries(FIXTURE_CONTEXTS)) {
      const ctxPath = join(dir, `${id}.json`);
      writeFileSync(ctxPath, JSON.stringify(ctx));
      const out = execFileSync(process.execPath,
        [join(promptsDir, 'render-prompt.js'), '--id', id, '--context', ctxPath],
        { encoding: 'utf8' });
      assert.ok(out.length > 200, `${id}: CLI rendered nothing`);
      assert.ok(!out.includes('{{'), `${id}: CLI left placeholders`);
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('render-prompt.js never imports a workflow script', () => {
  const src = readFileSync(join(promptsDir, 'render-prompt.js'), 'utf8');
  assert.ok(!/init-workflow|update-workflow/.test(src),
    'the prompt CLI must stay independent of the workflow scripts (707 seam)');
});

// ---- template-vars-vs-contextSpec drift gate --------------------------------
// Family regression (consumer break 2026-08-07: refresh's {{kind}}): a template
// demanding a variable its render sites don't supply kills the phase loudly at
// runtime — for EVERY consumer. The contextSpec is the render-site contract, so
// this gate fails the suite the moment a template consumes a variable the
// contextSpec does not declare. Scalars and {{#each}} keys are hard errors in
// the engine; {{#if}}/{{^}} keys are legal-when-missing and exempt.
function consumedVars(template) {
  const TOKEN = /\{\{(#each|#if|\^|\/each|\/if|\/\^)?\s*([\w.]*)\s*\}\}/g;
  const out = []; // { name, eaches: [enclosing #each keys, outermost first] }
  const stack = [];
  for (const [, kind, key] of template.matchAll(TOKEN)) {
    if (kind === '#each') {
      out.push({ name: key, eaches: [...stack] });
      stack.push(key);
    } else if (kind === '/each') {
      stack.pop();
    } else if (!kind && key !== '.') {
      out.push({ name: key, eaches: [...stack] });
    } // #if / ^ and their closes: missing keys are falsy by design, skip
  }
  assert.equal(stack.length, 0, 'unbalanced {{#each}} sections');
  return out;
}

test('every template variable the engine hard-requires is declared by the contextSpec', () => {
  for (const [id, mod] of Object.entries(PROMPTS)) {
    for (const { name, eaches } of consumedVars(mod.template)) {
      const topLevel = name in mod.contextSpec;
      // an each-item variable may instead be named in an enclosing array's
      // contextSpec description, e.g. 'array of {name, file, line} — ...'
      const itemText = eaches
        .filter((e) => e in mod.contextSpec)
        .map((e) => mod.contextSpec[e])
        .join(' ');
      const asItem = new RegExp(`\\b${name}\\b`).test(itemText);
      assert.ok(topLevel || asItem,
        `${id}: template consumes {{${name}}}` +
        (eaches.length ? ` (inside {{#each ${eaches.join(' > ')}}})` : '') +
        ` but the contextSpec does not declare it — render sites build contexts` +
        ` from the contextSpec, so this dies at runtime for every consumer`);
    }
  }
});

test('--dump exposes template + schema + contextSpec per id', () => {
  const dump = dumpPrompts();
  for (const id of Object.keys(FIXTURE_CONTEXTS)) {
    assert.ok(dump[id].template.length > 0, `${id} missing from dump`);
    assert.ok(dump[id].schema.type === 'object');
    assert.ok(Object.keys(dump[id].contextSpec).length > 0);
  }
});
