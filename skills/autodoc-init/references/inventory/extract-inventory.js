// extract-inventory.js — walk a repo, extract symbols, write JSON.
// Grouping is by derived community (epic 703): the graph store owns the
// partition; this file no longer buckets by any hand-written area map.
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, relative, extname } from 'node:path';
import { loadParsers, LANG_BY_EXT } from './grammars.js';
import { extractSymbols } from './extract-symbols.js';

const SKIP_DIRS = new Set([
  'node_modules', '.git', '__pycache__', 'dist', 'build', '.next',
  '.source', 'coverage', '.venv', 'venv', 'target', '.pytest_cache',
]);

export function* walk(root, dir = root) {
  for (const ent of readdirSync(dir, { withFileTypes: true })) {
    if (ent.name.startsWith('.') && ent.name !== '.github') continue;
    const full = join(dir, ent.name);
    if (ent.isDirectory()) {
      if (SKIP_DIRS.has(ent.name)) continue;
      yield* walk(root, full);
    } else if (ent.isFile() && extname(ent.name) in LANG_BY_EXT) {
      yield full;
    }
  }
}

export async function buildInventory({ repoPath, repoName = '', sha = '' }) {
  const parsers = await loadParsers();
  const symbols = [];
  for (const full of walk(repoPath)) {
    const rel = relative(repoPath, full).replace(/\\/g, '/');
    const lang = LANG_BY_EXT[extname(full)];
    let source;
    try { source = readFileSync(full, 'utf8'); } catch { continue; }
    let syms = [];
    try { syms = extractSymbols({ lang, parsers, source, file: rel }); }
    catch { syms = []; } // a parse failure on one file must not abort the run
    for (const s of syms) symbols.push({ ...s });
  }
  const byKind = {};
  for (const s of symbols) {
    byKind[s.kind] = (byKind[s.kind] || 0) + 1;
  }
  return {
    repo: repoName, sha,
    generated: new Date().toISOString(),
    counts: { total: symbols.length, byKind },
    symbols,
  };
}

// CLI: node extract-inventory.js <repoPath> <outPath> [repoName] [sha]
const isMain = process.argv[1] && process.argv[1].endsWith('extract-inventory.js');
if (isMain) {
  const [, , repoPath, outPath, repoName = '', sha = ''] = process.argv;
  if (!repoPath || !outPath) {
    console.error('usage: node extract-inventory.js <repoPath> <outPath> [repoName] [sha]');
    process.exit(2);
  }
  buildInventory({ repoPath, repoName, sha })
    .then((inv) => {
      writeFileSync(outPath, JSON.stringify(inv, null, 2));
      console.error(`inventory: ${inv.counts.total} symbols across ${Object.keys(inv.counts.byKind).length} kinds → ${outPath}`);
    })
    .catch((e) => { console.error('extract-inventory failed:', e); process.exit(1); });
}
