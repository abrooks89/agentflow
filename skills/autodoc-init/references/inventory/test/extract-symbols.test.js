// test/extract-symbols.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { loadParsers } from '../grammars.js';
import { extractSymbols } from '../extract-symbols.js';

const here = dirname(fileURLToPath(import.meta.url));
const read = (f) => readFileSync(join(here, 'fixtures', f), 'utf8');

test('python: functions and classes', async () => {
  const parsers = await loadParsers();
  const syms = extractSymbols({ lang: 'python', parsers, source: read('tiny.py'), file: 'a/tiny.py' });
  assert.ok(syms.some((s) => s.kind === 'function' && s.name === 'hello'));
});

test('go: funcs, methods, types', async () => {
  const parsers = await loadParsers();
  const syms = extractSymbols({ lang: 'go', parsers, source: read('sample.go'), file: 'b/sample.go' });
  assert.ok(syms.some((s) => s.kind === 'function' && s.name === 'Run'));
  assert.ok(syms.some((s) => s.kind === 'type' && s.name === 'Provider'));
});

test('typescript: exported function + class', async () => {
  const parsers = await loadParsers();
  const syms = extractSymbols({ lang: 'typescript', parsers, source: read('sample.ts'), file: 'c/sample.ts' });
  assert.ok(syms.some((s) => s.kind === 'function' && s.name === 'parsePlan'));
  assert.ok(syms.some((s) => s.kind === 'class' && s.name === 'Client'));
});

test('tsx: react component', async () => {
  const parsers = await loadParsers();
  const syms = extractSymbols({ lang: 'tsx', parsers, source: read('sample.tsx'), file: 'd/sample.tsx' });
  assert.ok(syms.some((s) => s.kind === 'component' && s.name === 'ProjectList'));
});

test('tsx: lower-case names are NOT components', async () => {
  const parsers = await loadParsers();
  const syms = extractSymbols({ lang: 'tsx', parsers, source: read('sample.tsx'), file: 'd/sample.tsx' });
  assert.ok(!syms.some((s) => s.kind === 'component' && /^[a-z]/.test(s.name)));
});
