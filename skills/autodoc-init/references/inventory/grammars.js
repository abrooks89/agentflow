// grammars.js — load web-tree-sitter parsers from prebuilt tree-sitter-wasms.
import Parser from 'web-tree-sitter';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';

const require = createRequire(import.meta.url);
// tree-sitter-wasms ships compiled grammars under out/tree-sitter-<lang>.wasm
const WASM_DIR = join(dirname(require.resolve('tree-sitter-wasms/package.json')), 'out');

export const LANG_BY_EXT = {
  '.py': 'python',
  '.go': 'go',
  '.ts': 'typescript',
  '.tsx': 'tsx',
  '.jsx': 'tsx',
  '.js': 'typescript',
};

const GRAMMAR_FILES = {
  python: 'tree-sitter-python.wasm',
  go: 'tree-sitter-go.wasm',
  typescript: 'tree-sitter-typescript.wasm',
  tsx: 'tree-sitter-tsx.wasm',
};

let _cache = null;

// Returns { python, go, typescript, tsx } configured Parser instances. Cached.
export async function loadParsers() {
  if (_cache) return _cache;
  await Parser.init();
  const out = {};
  for (const [lang, file] of Object.entries(GRAMMAR_FILES)) {
    const language = await Parser.Language.load(join(WASM_DIR, file));
    const p = new Parser();
    p.setLanguage(language);
    out[lang] = p;
    out[`_lang_${lang}`] = language; // language handle for building queries later
  }
  _cache = out;
  return out;
}
