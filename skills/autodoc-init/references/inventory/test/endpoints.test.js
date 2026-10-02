// test/endpoints.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { loadParsers } from '../grammars.js';
import { extractSymbols } from '../extract-symbols.js';

const here = dirname(fileURLToPath(import.meta.url));

test('python: decorated route → endpoint, not function', async () => {
  const parsers = await loadParsers();
  const src = readFileSync(join(here, 'fixtures/routes.py'), 'utf8');
  const syms = extractSymbols({ lang: 'python', parsers, source: src, file: 'canonical/app.py' });
  const ep = syms.find((s) => s.kind === 'endpoint' && s.name === 'create_project');
  assert.ok(ep, 'expected endpoint create_project');
  assert.match(ep.route_decorator, /post/i);
  // plain_helper stays a function; create_project is NOT double-counted as a function
  assert.ok(syms.some((s) => s.kind === 'function' && s.name === 'plain_helper'));
  assert.ok(!syms.some((s) => s.kind === 'function' && s.name === 'create_project'));
});
