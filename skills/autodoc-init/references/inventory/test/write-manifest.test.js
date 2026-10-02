// test/write-manifest.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync, writeFileSync, rmSync, mkdtempSync, existsSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { buildManifest, parseCliArgs } from '../write-manifest.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CLI = join(__dirname, '..', 'write-manifest.js');

function tmpAutodoc() {
  return mkdtempSync(join(tmpdir(), 'autodoc-manifest-'));
}

function pageMd({ type, title, sourcePaths = [], status = 'active' }) {
  const sp = sourcePaths.length
    ? `source_paths:\n${sourcePaths.map((s) => `  - ${s}`).join('\n')}\n`
    : `source_paths: []\n`;
  const typeLine = type != null ? `type: ${type}\n` : '';
  return (
    `---\n${typeLine}title: ${title}\ncreated: 2026-06-24\nupdated: 2026-06-24\n` +
    `source_repo: example-org/sample-repo\n${sp}status: ${status}\n---\n\n# ${title}\n`
  );
}

function runCLI(args) {
  return spawnSync(process.execPath, [CLI, ...args], { encoding: 'utf8' });
}

const ARGS = { sourceRepo: 'example-org/sample-repo', sourceBranch: 'main', sourcePath: '.source/sample-repo', lastSha: 'abc123def', project: 'sample', generatedAt: '2026-08-02' };
// The CLI's positional order is the one it has always had -- autodocDir, sourceRepo,
// sourceBranch, sourcePath, lastSha, [generatedAt] -- and `project` arrives as a named flag.
// Not cosmetic: a sixth positional would sit in generatedAt's slot, so an untouched legacy
// call would keep exiting 0 with its date in the project field. See the legacy-shape tests
// at the bottom of this file.
const CLI_ARGS = [ARGS.sourceRepo, ARGS.sourceBranch, ARGS.sourcePath, ARGS.lastSha, ARGS.generatedAt, '--project', ARGS.project];

test('buildManifest emits exactly seven top-level keys in the required order', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    const { manifest } = buildManifest({ autodocDir: dir, ...ARGS });
    assert.deepEqual(
      Object.keys(manifest),
      ['source_repo', 'source_branch', 'source_path', 'project', 'last_sha', 'generated_at', 'pages'],
    );
    assert.equal(manifest.source_repo, ARGS.sourceRepo);
    assert.equal(manifest.last_sha, ARGS.lastSha);
    // /autodoc-update reads this to pass `graph build --project`; it re-reads the
    // manifest, never the project profile, so an absent key leaves it with no source.
    assert.equal(manifest.project, ARGS.project);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('buildManifest sorts page keys and excludes nav pages (reuses listPages/parsePage)', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'zeta.md'), pageMd({ type: 'service', title: 'Zeta', sourcePaths: ['z.py'] }));
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    writeFileSync(join(dir, '_index.md'), pageMd({ type: 'index', title: 'Index', sourcePaths: [] }));
    const { manifest, errors } = buildManifest({ autodocDir: dir, ...ARGS });
    assert.deepEqual(errors, []);
    assert.deepEqual(Object.keys(manifest.pages), ['alpha.md', 'zeta.md']);
    assert.deepEqual(manifest.pages['alpha.md'], { type: 'service', source_paths: ['a.py'] });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI output has a trailing newline', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    const res = runCLI([dir, ...CLI_ARGS]);
    assert.equal(res.status, 0, `CLI should succeed (stderr: ${res.stderr})`);
    const text = readFileSync(join(dir, '.manifest.json'), 'utf8');
    assert.ok(text.endsWith('\n') && !text.endsWith('\n\n'), 'exactly one trailing newline');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI re-run with identical inputs produces a byte-identical manifest', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    writeFileSync(join(dir, 'beta.md'), pageMd({ type: 'endpoint', title: 'Beta', sourcePaths: ['b.py'] }));
    const args = [dir, ...CLI_ARGS];
    const run1 = runCLI(args);
    assert.equal(run1.status, 0, `first run should succeed (stderr: ${run1.stderr})`);
    const first = readFileSync(join(dir, '.manifest.json'), 'utf8');
    const run2 = runCLI(args);
    assert.equal(run2.status, 0, `second run should succeed (stderr: ${run2.stderr})`);
    const second = readFileSync(join(dir, '.manifest.json'), 'utf8');
    assert.equal(second, first, 'byte-identical re-run');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// gh#184 -- the backup name is CLAIMED by the caller, never composed here.
//
// This CLI used to copy the old manifest to a hand-built `.manifest.json` + marker name that
// `agentflow.backups`' reaper does not recognise as canonical, so the manifest's backups grew
// without bound. The format lives once, in `skills/_shared/agentflow/backups.py`; the caller
// claims a name there (`python -m agentflow.backups new`, which creates the file EMPTY) and
// passes it as `--backup-to`. The claim below is a stand-in empty file: these tests pin what the
// CLI does with a claim, and `skills/_shared/tests/test_autodoc_update_manifest_backup.py` pins
// that the runbook's claim really is a canonical name.
// ---------------------------------------------------------------------------

// Every sibling of the manifest that is not the manifest itself, the pages, or the claim --
// i.e. any backup the CLI invented on its own.
function inventedSiblings(dir, claimName) {
  return readdirSync(dir).filter(
    (f) => f !== '.manifest.json' && f !== claimName && !f.endsWith('.md'),
  );
}

const CLAIM = 'claimed-backup';

test('CLI copies the prior manifest byte-for-byte into the --backup-to claim', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    // Bytes a text round-trip would alter (CRLF, no trailing newline, non-ASCII) so "preserved"
    // means preserved, not merely re-serialised.
    const prior = Buffer.from('{"source_repo":"old/repo",\r\n "note":"café"}', 'utf8');
    writeFileSync(join(dir, '.manifest.json'), prior);
    writeFileSync(join(dir, CLAIM), '');
    const res = runCLI([dir, ...CLI_ARGS, '--backup-to', join(dir, CLAIM)]);
    assert.equal(res.status, 0, `CLI should succeed (stderr: ${res.stderr})`);
    assert.deepEqual(readFileSync(join(dir, CLAIM)), prior, 'the claim holds the prior manifest exactly');
    const fresh = JSON.parse(readFileSync(join(dir, '.manifest.json'), 'utf8'));
    assert.equal(fresh.source_repo, ARGS.sourceRepo, 'live manifest was overwritten with new content');
    assert.deepEqual(inventedSiblings(dir, CLAIM), [], 'no second, self-named backup beside the claim');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI never invents a backup name: without --backup-to it writes none and says so', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    writeFileSync(join(dir, '.manifest.json'), '{"source_repo":"old/repo"}\n');
    const res = runCLI([dir, ...CLI_ARGS]);
    assert.equal(res.status, 0, `CLI should succeed (stderr: ${res.stderr})`);
    assert.deepEqual(inventedSiblings(dir, null), [], 'no backup file of any name was composed here');
    assert.match(res.stderr, /WITHOUT a backup/, 'the unbacked overwrite is announced, not silent');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI leaves the claim empty when there is no prior manifest to back up', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    writeFileSync(join(dir, CLAIM), '');
    const res = runCLI([dir, ...CLI_ARGS, '--backup-to', join(dir, CLAIM)]);
    assert.equal(res.status, 0, `CLI should succeed (stderr: ${res.stderr})`);
    assert.equal(readFileSync(join(dir, CLAIM)).length, 0, 'nothing to copy, so nothing copied');
    assert.ok(existsSync(join(dir, '.manifest.json')), 'manifest written');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('--backup-to must be an existing EMPTY file: a missing or non-empty path exits 2 with no write', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    const prior = '{"source_repo":"old/repo"}\n';
    writeFileSync(join(dir, '.manifest.json'), prior);

    // Missing: a name typed on the spot rather than claimed.
    const missing = runCLI([dir, ...CLI_ARGS, '--backup-to', join(dir, 'invented.bak')]);
    assert.equal(missing.status, 2, 'an unclaimed path is a usage error');
    assert.match(missing.stderr, /agentflow\.backups new/, 'stderr says how to claim one');
    assert.ok(!existsSync(join(dir, 'invented.bak')), 'the CLI did not create it either');

    // Non-empty: an older backup this run must not clobber.
    const older = 'an earlier backup\n';
    writeFileSync(join(dir, CLAIM), older);
    const full = runCLI([dir, ...CLI_ARGS, '--backup-to', join(dir, CLAIM)]);
    assert.equal(full.status, 2, 'a non-empty path is a usage error');
    assert.equal(readFileSync(join(dir, CLAIM), 'utf8'), older, 'the earlier backup is untouched');

    assert.equal(readFileSync(join(dir, '.manifest.json'), 'utf8'), prior, 'manifest untouched by both');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('parseCliArgs carries --backup-to (both spellings) and refuses it without a value', () => {
  const base = ['dir', 'repo', 'branch', 'path', 'abc123', '--project', 'demoproj'];
  assert.equal(parseCliArgs(base).backupTo, undefined, 'absent flag -> undefined, nothing invented');
  assert.equal(parseCliArgs([...base, '--backup-to', 'x/claim']).backupTo, 'x/claim');
  assert.equal(parseCliArgs(['--backup-to=x/claim', ...base]).backupTo, 'x/claim');
  assert.match(parseCliArgs([...base, '--backup-to']).error, /--backup-to requires a value/);
  assert.match(parseCliArgs([...base, '--backup-to', '--project', 'p']).error, /--backup-to requires a value/);
  assert.match(parseCliArgs([...base, '--backup-to=']).error, /--backup-to requires a value/);
});

test('CLI exits 1 naming page+field on missing type frontmatter and leaves an existing manifest untouched', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'notype.md'), pageMd({ type: null, title: 'No Type', sourcePaths: ['a.py'] }));
    const priorManifest = '{"source_repo":"old/repo"}\n';
    writeFileSync(join(dir, '.manifest.json'), priorManifest);
    writeFileSync(join(dir, CLAIM), '');
    const res = runCLI([dir, ...CLI_ARGS, '--backup-to', join(dir, CLAIM)]);
    assert.equal(res.status, 1, 'exit 1 on missing type frontmatter');
    assert.match(res.stderr, /notype\.md/, 'stderr names the offending page');
    assert.match(res.stderr, /type/, "stderr names the missing field 'type'");
    assert.equal(readFileSync(join(dir, '.manifest.json'), 'utf8'), priorManifest, 'existing manifest left untouched');
    assert.equal(readFileSync(join(dir, CLAIM)).length, 0, 'no backup written into the claim — the write never happened');
    assert.deepEqual(inventedSiblings(dir, CLAIM), [], 'and no self-named backup either');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI exits 1 on missing type frontmatter and creates no manifest when none existed', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'notype.md'), pageMd({ type: null, title: 'No Type', sourcePaths: ['a.py'] }));
    const res = runCLI([dir, ...CLI_ARGS]);
    assert.equal(res.status, 1, 'exit 1 on missing type frontmatter');
    assert.ok(!existsSync(join(dir, '.manifest.json')), 'no partial manifest left on disk');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('a page with empty source_paths gets [] on its entry and a stderr warning', () => {
  const dir = tmpAutodoc();
  const origError = console.error;
  const logged = [];
  console.error = (...args) => logged.push(args.join(' '));
  try {
    writeFileSync(join(dir, 'bare.md'), pageMd({ type: 'service', title: 'Bare', sourcePaths: [] }));
    const { manifest, errors } = buildManifest({ autodocDir: dir, ...ARGS });
    assert.deepEqual(errors, []);
    assert.deepEqual(manifest.pages['bare.md'].source_paths, []);
    assert.ok(logged.some((l) => l.includes('bare.md') && l.includes('source_paths')), 'warning printed');
  } finally {
    console.error = origError;
    rmSync(dir, { recursive: true, force: true });
  }
});

test('status: deprecated frontmatter propagates onto the page entry', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(
      join(dir, 'old.md'),
      pageMd({ type: 'service', title: 'Old', sourcePaths: ['old.py'], status: 'deprecated' }),
    );
    writeFileSync(join(dir, 'live.md'), pageMd({ type: 'service', title: 'Live', sourcePaths: ['live.py'] }));
    const { manifest, errors } = buildManifest({ autodocDir: dir, ...ARGS });
    assert.deepEqual(errors, []);
    assert.equal(manifest.pages['old.md'].status, 'deprecated');
    assert.equal(manifest.pages['live.md'].status, undefined, 'active pages carry no status field');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI exits 2 when project is omitted, rather than writing a manifest without it', () => {
  // gh#152: /autodoc-update reads `project` from here to pass `graph build --project`.
  // A manifest written without it hands the update path a chain that cannot render, so
  // the omission has to fail at the command that could supply it.
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    const res = runCLI([dir, ARGS.sourceRepo, ARGS.sourceBranch, ARGS.sourcePath, ARGS.lastSha]);
    assert.equal(res.status, 2, 'exit 2 on usage error');
    assert.match(res.stderr, /--project/, 'usage message names the missing flag');
    assert.ok(!existsSync(join(dir, '.manifest.json')), 'no manifest written on a usage error');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI exits 2 with a usage message when a required arg is missing', () => {
  const dir = tmpAutodoc();
  try {
    const res = runCLI([dir, ARGS.sourceRepo, ARGS.sourceBranch, ARGS.sourcePath, '--project', ARGS.project]); // lastSha omitted
    assert.equal(res.status, 2, 'exit 2 on usage error');
    assert.match(res.stderr, /usage:/, 'usage message printed');
    assert.ok(!existsSync(join(dir, '.manifest.json')), 'no manifest written on a usage error');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// gh#152 -- the positional-collision class.
//
// `project` was first added as a SIXTH POSITIONAL, which is the slot the optional
// `generatedAt` already occupied. An untouched call site written against the older CLI
// therefore kept exiting 0 while its DATE landed in `project`, and `generatedAt` defaulted
// to today -- the same value, so nothing looked wrong on inspection. `graph build --project`
// then filed every page into a vault pillar named after a date. write-manifest.js's own note
// calls that outcome worse than the refusal it replaces, so these tests pin the refusal.
// ---------------------------------------------------------------------------

test('a legacy six-positional call refuses instead of writing the date into project', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    // Exactly the shape of a call written against the CLI before `project` existed.
    const res = runCLI([dir, ARGS.sourceRepo, ARGS.sourceBranch, ARGS.sourcePath, ARGS.lastSha, ARGS.generatedAt]);
    assert.equal(res.status, 2, 'a legacy six-positional call is a usage error, not a silent success');
    assert.match(res.stderr, /--project/, 'stderr names the flag that would fix it');
    assert.ok(
      !existsSync(join(dir, '.manifest.json')),
      'no manifest at all -- succeeding with project set to the date is the defect',
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('appending project as a seventh positional refuses rather than shifting generatedAt', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    // The collision from the other side: a caller "upgrading" by wedging the project in
    // positionally. Left unchecked its date would silently become an ignored extra arg.
    const res = runCLI([dir, ARGS.sourceRepo, ARGS.sourceBranch, ARGS.sourcePath, ARGS.lastSha, ARGS.project, ARGS.generatedAt]);
    assert.equal(res.status, 2, 'exit 2 on too many positionals');
    assert.match(res.stderr, /positional/, 'stderr says the extra argument is the problem');
    assert.match(res.stderr, /--project/, 'stderr names the flag to use instead');
    assert.ok(!existsSync(join(dir, '.manifest.json')), 'no manifest written on a usage error');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('--project fills only project; the sixth positional is still generatedAt', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    const res = runCLI([dir, ARGS.sourceRepo, ARGS.sourceBranch, ARGS.sourcePath, ARGS.lastSha, ARGS.generatedAt, '--project', ARGS.project]);
    assert.equal(res.status, 0, `CLI should succeed (stderr: ${res.stderr})`);
    const m = JSON.parse(readFileSync(join(dir, '.manifest.json'), 'utf8'));
    assert.equal(m.project, ARGS.project, 'project comes from the flag');
    assert.equal(m.generated_at, ARGS.generatedAt, 'the sixth positional still means generatedAt');
    assert.equal(m.last_sha, ARGS.lastSha, 'the fifth positional still means lastSha');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('--project=<name> is accepted and the flag may precede the positionals', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    const res = runCLI([`--project=${ARGS.project}`, dir, ARGS.sourceRepo, ARGS.sourceBranch, ARGS.sourcePath, ARGS.lastSha]);
    assert.equal(res.status, 0, `CLI should succeed (stderr: ${res.stderr})`);
    const m = JSON.parse(readFileSync(join(dir, '.manifest.json'), 'utf8'));
    assert.equal(m.project, ARGS.project);
    assert.equal(m.generated_at, new Date().toISOString().slice(0, 10), 'generatedAt defaults to today when the slot is empty');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('--project with no value, or an unrecognised flag, exits 2 rather than guessing', () => {
  const dir = tmpAutodoc();
  try {
    writeFileSync(join(dir, 'alpha.md'), pageMd({ type: 'service', title: 'Alpha', sourcePaths: ['a.py'] }));
    const base = [dir, ARGS.sourceRepo, ARGS.sourceBranch, ARGS.sourcePath, ARGS.lastSha];
    const noValue = runCLI([...base, '--project']);
    assert.equal(noValue.status, 2, '--project as the last argument is a usage error');
    const swallowed = runCLI([...base, '--project', '--projekt', ARGS.project]);
    assert.equal(swallowed.status, 2, '--project must not swallow the next flag as its value');
    const unknown = runCLI([...base, '--projekt', ARGS.project]);
    assert.equal(unknown.status, 2, 'a misspelled flag is rejected, not parsed as a positional');
    assert.match(unknown.stderr, /--projekt/, 'stderr names the option it did not recognise');
    assert.ok(!existsSync(join(dir, '.manifest.json')), 'no manifest written on any of them');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('parseCliArgs reports the legacy six-positional shape as a missing --project', () => {
  // The unit view of the decisive case: six positionals parse cleanly, and the ONLY thing
  // standing between them and a manifest is the absent flag.
  const legacy = parseCliArgs(['dir', 'repo', 'branch', 'path', 'abc123', '2026-09-04']);
  assert.equal(legacy.project, undefined, 'nothing was invented for project');
  assert.match(legacy.error, /--project/, 'the error names the flag');
  const ok = parseCliArgs(['dir', 'repo', 'branch', 'path', 'abc123', '2026-09-04', '--project', 'demoproj']);
  assert.equal(ok.project, 'demoproj');
  assert.deepEqual(ok.positionals, ['dir', 'repo', 'branch', 'path', 'abc123', '2026-09-04']);
});
