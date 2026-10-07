#!/usr/bin/env node
// run-complete.js — the run-completion gate. Run LAST by /autodoc-init AND
// /autodoc-update. Verifies the run's bookkeeping actually happened; exits
// non-zero naming the FIRST failed check. No --fix, no --force, no partial credit.
//
// Checks:
//   (a) .manifest.json: pages non-empty; every key resolves to a file on disk;
//       key count == on-disk content-page count (listPages excludeNav).
//   (b) source_path names a clone inside autodocDir, and that clone has a .git:
//       the next /autodoc-update fetches from it, so a cutover that lost it is not done.
//   (c) log.md contains an entry carrying the manifest's generated_at date AND
//       short last_sha.
//   (d) hot.md states the manifest's short last_sha and satisfies the declared
//       hot-cache contract: whole file <=400 words, "## Latest" section <=120 words.
//
// Usage: node run-complete.js <autodocDir>
import { readFileSync, existsSync } from 'node:fs';
import { join, resolve, relative, isAbsolute, sep } from 'node:path';
import { listPages } from './graph-check.js';

const [autodocDir] = process.argv.slice(2);
if (!autodocDir) { console.error('usage: node run-complete.js <autodocDir>'); process.exit(1); }

function fail(msg) { console.error(`run-complete: FAIL — ${msg}`); process.exit(1); }
const words = (s) => (s.trim().match(/\S+/g) || []).length;

// (a) manifest
const manifestPath = join(autodocDir, '.manifest.json');
if (!existsSync(manifestPath)) fail(`.manifest.json missing at ${manifestPath}`);
let manifest;
try { manifest = JSON.parse(readFileSync(manifestPath, 'utf8')); }
catch (err) { fail(`.manifest.json unparseable: ${err.message}`); }
const pageKeys = Object.keys(manifest.pages || {});
if (pageKeys.length === 0) fail('.manifest.json `pages` is empty — the manifest write step did not run');
for (const key of pageKeys) {
  // Keys must stay inside autodocDir: a tampered manifest must not probe the wider filesystem.
  if (key.includes('..') || key.startsWith('/') || /^[A-Za-z]:/.test(key)) {
    fail(`manifest page key escapes the autodoc dir (absolute or '..'): ${key}`);
  }
  if (!existsSync(join(autodocDir, key))) fail(`manifest page does not resolve on disk: ${key}`);
}
const onDisk = listPages(autodocDir, { excludeNav: true });
if (pageKeys.length !== onDisk.length) {
  const diskSet = new Set(onDisk.map((p) => p.file));
  const missing = onDisk.filter((p) => !(p.file in manifest.pages)).map((p) => p.file);
  const extra = pageKeys.filter((k) => !diskSet.has(k));
  fail(`manifest page count ${pageKeys.length} != on-disk content-page count ${onDisk.length}` +
    (missing.length ? `; on disk but not in manifest: ${missing.slice(0, 5).join(', ')}` : '') +
    (extra.length ? `; in manifest but not on disk: ${extra.slice(0, 5).join(', ')}` : ''));
}
const shortSha = String(manifest.last_sha || '').slice(0, 8);
if (shortSha.length < 7) fail(`manifest last_sha missing/short: "${manifest.last_sha}"`);

// (b) source clone
const sourcePath = manifest.source_path;
if (typeof sourcePath !== 'string' || !sourcePath.trim()) {
  fail('manifest source_path is missing - the next /autodoc-update has no clone to fetch from');
}
// Resolved, not pattern-matched: a stem like `a..b` is inside, and an absolute, drive or UNC
// path is judged by where it lands.
const sourceRel = relative(resolve(autodocDir), resolve(autodocDir, sourcePath));
if (!sourceRel || isAbsolute(sourceRel) || sourceRel === '..' || sourceRel.startsWith(`..${sep}`)) {
  fail(`manifest source_path escapes the autodoc dir (it must name a directory inside it): ${sourcePath}`);
}
if (!existsSync(join(autodocDir, sourcePath, '.git'))) {
  fail(`manifest source_path ${sourcePath} has no .git - the clone did not reach the live tree`);
}

// (c) log.md
const logPath = join(autodocDir, 'log.md');
if (!existsSync(logPath)) fail('log.md missing');
const log = readFileSync(logPath, 'utf8');
// generated_at is interpolated into a RegExp below — enforce its shape first.
if (!/^\d{4}-\d{2}-\d{2}$/.test(String(manifest.generated_at || ''))) {
  fail(`manifest generated_at is not a YYYY-MM-DD date: "${manifest.generated_at}"`);
}
const dateRe = new RegExp(`^## \\[${manifest.generated_at}\\]`, 'm');
if (!dateRe.test(log)) fail(`log.md has no entry for this run's date ${manifest.generated_at}`);
if (!log.includes(shortSha)) fail(`log.md never mentions this run's short sha ${shortSha}`);

// (d) hot.md
const hotPath = join(autodocDir, 'hot.md');
if (!existsSync(hotPath)) fail('hot.md missing');
const hot = readFileSync(hotPath, 'utf8');
if (!hot.includes(shortSha)) fail(`hot.md last_sha is stale — expected ${shortSha}`);
if (words(hot) > 400) fail(`hot.md violates the hot-cache contract: ${words(hot)} words > 400`);
const latestMatch = hot.match(/^## Latest\s*$([\s\S]*?)(?=^## |\n*$(?![\s\S]))/m);
if (!latestMatch) fail('hot.md has no "## Latest" section');
if (words(latestMatch[1]) > 120) fail(`hot.md "## Latest" violates the contract: ${words(latestMatch[1])} words > 120`);

console.log(`run-complete: PASS — ${pageKeys.length} pages, log + hot current @ ${shortSha}`);
