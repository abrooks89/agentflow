import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, rmSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, basename } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(HERE, '..', 'run-complete.js');
const SHA = 'f'.repeat(40);
const SHORT = SHA.slice(0, 8);

function scaffold() {
  const dir = mkdtempSync(join(tmpdir(), 'rc-'));
  mkdirSync(join(dir, 'services'));
  // The clone the manifest's source_path names: /autodoc-update fetches from it next run.
  mkdirSync(join(dir, '.source', 'sample-repo', '.git'), { recursive: true });
  writeFileSync(join(dir, 'services', 'Alpha.md'),
    '---\ntype: service\ntitle: "Alpha"\nsource_repo: "example-org/sample-repo"\nsource_paths:\n  - "src/a.py"\n---\n\n# Alpha\n');
  writeFileSync(join(dir, '.manifest.json'), JSON.stringify({
    source_repo: 'example-org/sample-repo', source_branch: 'master',
    source_path: '.source/sample-repo', last_sha: SHA, generated_at: '2026-08-02',
    pages: { 'services/Alpha.md': { type: 'service', source_paths: ['src/a.py'] } },
  }, null, 2));
  writeFileSync(join(dir, 'log.md'),
    `# log\n\n## [2026-08-02] autodoc-init | example-org/sample-repo @ ${SHORT}\n- Areas mapped: 1 | Pages created: 1\n`);
  writeFileSync(join(dir, 'hot.md'),
    `<!-- hot-cache contract: whole file <=400 words; Latest <=120 words -->\n\n## Latest\n\nOne service documented. last_sha ${SHORT}.\n`);
  return dir;
}

function run(dir) {
  return execFileSync(process.execPath, [SCRIPT, dir], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}

function expectFail(dir, needle) {
  assert.throws(() => run(dir), (err) => {
    assert.notEqual(err.status, 0);
    assert.ok(String(err.stderr).toLowerCase().includes(needle), `stderr names "${needle}"; got: ${err.stderr}`);
    return true;
  });
}

test('a complete run exits zero', () => {
  const dir = scaffold();
  try { assert.ok(run(dir).includes('run-complete: PASS')); }
  finally { rmSync(dir, { recursive: true, force: true }); }
});

test('injection (a): blanked manifest pages -> non-zero naming the manifest', () => {
  const dir = scaffold();
  try {
    const m = JSON.parse(readFileSync(join(dir, '.manifest.json'), 'utf8'));
    m.pages = {};
    writeFileSync(join(dir, '.manifest.json'), JSON.stringify(m, null, 2));
    expectFail(dir, 'pages');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('injection (b): scaffold log.md -> non-zero naming the log', () => {
  const dir = scaffold();
  try {
    writeFileSync(join(dir, 'log.md'), '# log\n\nThe autodoc-init mapping Workflow appends its run entry here.\n');
    expectFail(dir, 'log.md');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('injection (c): stale hot.md last_sha -> non-zero naming hot', () => {
  const dir = scaffold();
  try {
    writeFileSync(join(dir, 'hot.md'), '<!-- contract -->\n\n## Latest\n\nStale. last_sha 00000000.\n');
    expectFail(dir, 'hot.md');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('manifest key not on disk -> non-zero naming the missing file', () => {
  const dir = scaffold();
  try {
    rmSync(join(dir, 'services', 'Alpha.md'));
    expectFail(dir, 'alpha.md');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('page on disk missing from manifest (count mismatch) -> non-zero', () => {
  const dir = scaffold();
  try {
    writeFileSync(join(dir, 'services', 'Beta.md'),
      '---\ntype: service\ntitle: "Beta"\nsource_repo: "example-org/sample-repo"\nsource_paths:\n  - "src/b.py"\n---\n\n# Beta\n');
    expectFail(dir, 'count');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('hot.md over 400 words -> non-zero naming the contract', () => {
  const dir = scaffold();
  try {
    const blob = Array.from({ length: 401 }, (_, i) => `w${i}`).join(' ');
    writeFileSync(join(dir, 'hot.md'), `<!-- c -->\n\n## Latest\n\nlast_sha ${SHORT}. ok\n\n## Rest\n\n${blob}\n`);
    expectFail(dir, '400');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('log.md has the run date but a stale sha -> non-zero via the never-mentions path', () => {
  const dir = scaffold();
  try {
    writeFileSync(join(dir, 'log.md'),
      '# log\n\n## [2026-08-02] autodoc-init | example-org/sample-repo @ 00000000\n- Areas mapped: 1 | Pages created: 1\n');
    expectFail(dir, 'never mentions');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('hot.md "## Latest" over 120 words while whole file under 400 -> non-zero naming 120', () => {
  const dir = scaffold();
  try {
    const blob = Array.from({ length: 130 }, (_, i) => `w${i}`).join(' ');
    writeFileSync(join(dir, 'hot.md'), `<!-- c -->\n\n## Latest\n\nlast_sha ${SHORT}. ${blob}\n`);
    expectFail(dir, '120');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('hot.md has the sha but no "## Latest" section -> non-zero naming Latest', () => {
  const dir = scaffold();
  try {
    writeFileSync(join(dir, 'hot.md'), `<!-- c -->\n\n## Summary\n\nCurrent. last_sha ${SHORT}.\n`);
    expectFail(dir, 'latest');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('manifest last_sha shorter than 7 chars -> non-zero naming last_sha', () => {
  const dir = scaffold();
  try {
    const m = JSON.parse(readFileSync(join(dir, '.manifest.json'), 'utf8'));
    m.last_sha = 'abc';
    writeFileSync(join(dir, '.manifest.json'), JSON.stringify(m, null, 2));
    expectFail(dir, 'last_sha');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('manifest page key with .. escapes the dir -> non-zero naming the key check', () => {
  const dir = scaffold();
  try {
    const m = JSON.parse(readFileSync(join(dir, '.manifest.json'), 'utf8'));
    m.pages['../outside.md'] = { type: 'service', source_paths: [] };
    writeFileSync(join(dir, '.manifest.json'), JSON.stringify(m, null, 2));
    expectFail(dir, 'escapes');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('manifest generated_at not YYYY-MM-DD -> non-zero naming generated_at', () => {
  const dir = scaffold();
  try {
    const m = JSON.parse(readFileSync(join(dir, '.manifest.json'), 'utf8'));
    // '.*' is the gate-bypass repro: pre-fix code interpolates it into the date
    // regex, which then matches ANY log line and the gate wrongly exits 0.
    m.generated_at = '.*';
    writeFileSync(join(dir, '.manifest.json'), JSON.stringify(m, null, 2));
    expectFail(dir, 'yyyy-mm-dd');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('manifest with no source_path -> non-zero naming source_path', () => {
  const dir = scaffold();
  try {
    const m = JSON.parse(readFileSync(join(dir, '.manifest.json'), 'utf8'));
    delete m.source_path;
    writeFileSync(join(dir, '.manifest.json'), JSON.stringify(m, null, 2));
    expectFail(dir, 'source_path');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('source_path with no .git (the clone did not travel) -> non-zero naming .git', () => {
  const dir = scaffold();
  try {
    rmSync(join(dir, '.source', 'sample-repo', '.git'), { recursive: true, force: true });
    expectFail(dir, '.git');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('source_path that escapes the autodoc dir -> non-zero naming the escape', () => {
  const dir = scaffold();
  // The escaped target HAS a .git, so only the escape guard can reject it, not the .git check.
  const elsewhere = `${dir}-elsewhere`;
  try {
    mkdirSync(join(elsewhere, '.git'), { recursive: true });
    const m = JSON.parse(readFileSync(join(dir, '.manifest.json'), 'utf8'));
    m.source_path = `../${basename(elsewhere)}`;
    writeFileSync(join(dir, '.manifest.json'), JSON.stringify(m, null, 2));
    expectFail(dir, 'escapes');
  } finally {
    rmSync(dir, { recursive: true, force: true });
    rmSync(elsewhere, { recursive: true, force: true });
  }
});

test('source_path whose stem merely contains .. stays inside -> PASS', () => {
  const dir = scaffold();
  try {
    mkdirSync(join(dir, '.source', 'a..b', '.git'), { recursive: true });
    const m = JSON.parse(readFileSync(join(dir, '.manifest.json'), 'utf8'));
    m.source_path = '.source/a..b';
    writeFileSync(join(dir, '.manifest.json'), JSON.stringify(m, null, 2));
    assert.match(run(dir), /PASS/);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
