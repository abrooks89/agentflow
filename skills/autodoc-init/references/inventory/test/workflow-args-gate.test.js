// test/workflow-args-gate.test.js — static gates for the workflow scripts'
// required-arg contracts (epic 703 review finding 4). Workflow scripts run
// only inside the ultracode sandbox (args/agent/parallel globals), so these
// assertions are source-level: the loud-failure paths must exist and the
// silent-tolerance patterns must not.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const initWf = join(here, '..', '..', 'init-workflow.js');
const updateWf = join(
  here, '..', '..', '..', '..', 'autodoc-update', 'references', 'update-workflow.js',
);

test('init-workflow requires units and per-community-id checklists', () => {
  const src = readFileSync(initWf, 'utf8');
  assert.ok(src.includes('args.units is required'),
    'init-workflow must hard-fail a missing units arg');
  assert.ok(src.includes('args.communitySymbols is required'),
    'init-workflow must hard-fail missing checklists');
  assert.ok(src.includes('communitySymbols[String(c.id)]'),
    'checklists must be keyed by community ID, not label');
});

test('init-workflow consumers agree with the map schema field rename', () => {
  // 17283a9 renamed the agent-return field 'community' -> 'unit'; any
  // consumer still reading m.community renders '- [undefined] ...' silently.
  // The schema itself now lives in the prompts module (epic 704).
  const src = readFileSync(initWf, 'utf8');
  assert.ok(!/\bm\.community\b/.test(src),
    'a mapped-result consumer still reads m.community (schema field is unit)');
  assert.ok(src.includes('m.unit'),
    'the unit field must actually be consumed');
});

test('init-workflow requires the single-sourced prompts and parents args', () => {
  const src = readFileSync(initWf, 'utf8');
  assert.ok(src.includes('args.prompts is required'),
    'init-workflow must hard-fail a missing prompts dump');
  assert.ok(src.includes('args.parents is required'),
    'init-workflow must hard-fail a missing parents chain');
  assert.ok(src.includes('agent budget exceeded BEFORE dispatch'),
    'the pre-dispatch agent-budget ceiling must exist');
});

test('update-workflow requires the prompts dump for the refresh entry', () => {
  const src = readFileSync(updateWf, 'utf8');
  assert.ok(src.includes('args.prompts is required'),
    'update-workflow must hard-fail a missing prompts dump');
  assert.ok(src.includes('PROMPTS.refresh.schema'),
    'REFRESH_SCHEMA must come from the prompts module, not an inline copy');
});

test('update-workflow threads the by-path checklist AND keeps the inline one', () => {
  // E-0743: dropping the `symbols` pass-through would break every caller that
  // does not pass --checklist-dir; dropping either new key would leave the
  // template's by-path branch unreachable from the live render site.
  const src = readFileSync(updateWf, 'utf8');
  assert.ok(/checklistPath: a\.checklistPath/.test(src),
    'refreshPrompt must pass checklistPath into the template context');
  assert.ok(/symbolCount: a\.symbolCount/.test(src),
    'refreshPrompt must pass symbolCount into the template context');
  assert.ok(/symbols: \(a\.symbols \|\| \[\]\)/.test(src),
    'the inline symbols pass-through must survive — most callers still use it');
});

test('update-workflow hard-fails a missing changedCommunities arg', () => {
  const src = readFileSync(updateWf, 'utf8');
  assert.ok(src.includes('changedCommunities === undefined'),
    'update-workflow must hard-fail an absent changedCommunities');
  assert.ok(!src.includes('changedCommunities || []'),
    'the silent zero-agent tolerance pattern must not return');
});
