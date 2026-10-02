// test/grammars.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { loadParsers, LANG_BY_EXT } from '../grammars.js';

const here = dirname(fileURLToPath(import.meta.url));

test('loadParsers returns a working python parser', async () => {
  const parsers = await loadParsers();
  const src = readFileSync(join(here, 'fixtures/tiny.py'), 'utf8');
  const tree = parsers.python.parse(src);
  assert.equal(tree.rootNode.type, 'module');
  assert.ok(tree.rootNode.descendantsOfType('function_definition').length >= 1);
});

test('LANG_BY_EXT maps the four languages', () => {
  assert.equal(LANG_BY_EXT['.py'], 'python');
  assert.equal(LANG_BY_EXT['.go'], 'go');
  assert.equal(LANG_BY_EXT['.tsx'], 'tsx');
  assert.equal(LANG_BY_EXT['.ts'], 'typescript');
});
