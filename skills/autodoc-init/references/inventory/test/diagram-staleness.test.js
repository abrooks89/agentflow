// test/diagram-staleness.test.js
//
// gh#438: the detector compared an abbreviated page sha to the manifest's full
// 40-char last_sha by string equality, so a diagram generated at the current
// commit was reported stale and the budget line recommended regenerating it.
// The comparison is now a common-prefix match with a floor, so these tests exist
// mainly to give the report a reachable PASS state that stays reachable.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { execFileSync, spawnSync } from 'node:child_process';
import { computeStaleDiagrams, gitFreshness, shaMatches, MIN_SHA_PREFIX } from '../diagram-staleness.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CLI = join(__dirname, '..', 'diagram-staleness.js');

// Shaped like the real corpus: a full head sha, the abbreviation a page records
// for it, and an unrelated older abbreviation.
const HEAD = '964e5646300033a9f025a289f5795106996fc4c6';
const HEAD_SHORT = HEAD.slice(0, 8); // '964e5646'
const OLD_SHORT = '690ca5c';

function page({ sha, sourcePaths = ['src/a.py'], diagram = true, path: p = 'services/Alpha.md' }) {
  return { path: p, diagram, diagram_source_sha: sha, source_paths: sourcePaths };
}

const heads = { 'src/a.py': HEAD };

const verdict = (pages, headMap = heads) => {
  const r = computeStaleDiagrams({ pages, headSourceShas: headMap });
  return { stale: r.stale.map((p) => p.path), fresh: r.fresh.map((p) => p.path), budget: r.budget };
};

// --- the central case: without it the fix is unverifiable ------------------
// Every fixture that predates gh#438 is stale under both the old comparison and
// the new one, so only a page recording the SHORT form of the current head can
// tell the two apart.
test('a page whose diagram_source_sha is the short form of last_sha is FRESH', () => {
  const v = verdict([page({ sha: HEAD_SHORT })]);
  assert.deepEqual(v.fresh, ['services/Alpha.md']);
  assert.deepEqual(v.stale, []);
  assert.equal(v.budget.staleCount, 0);
  assert.equal(v.budget.estRegenTokens, 0, 'a fresh corpus recommends spending nothing');
});

test('a genuinely older short sha is still STALE', () => {
  const v = verdict([page({ sha: OLD_SHORT })]);
  assert.deepEqual(v.stale, ['services/Alpha.md']);
  assert.deepEqual(v.fresh, []);
  assert.equal(v.budget.estRegenTokens, 8000);
});

test('exact full-sha equality is still FRESH', () => {
  const v = verdict([page({ sha: HEAD })]);
  assert.deepEqual(v.fresh, ['services/Alpha.md']);
  assert.deepEqual(v.stale, []);
});

test('the corpus mix reports both verdicts, not a blanket all-stale', () => {
  const v = verdict([
    page({ sha: HEAD_SHORT, path: 'services/Fresh.md' }),
    page({ sha: OLD_SHORT, path: 'services/Stale.md' }),
  ]);
  assert.deepEqual(v.fresh, ['services/Fresh.md']);
  assert.deepEqual(v.stale, ['services/Stale.md']);
});

// --- degenerate values must match nothing ---------------------------------
// A prefix rule with no floor makes '' and '9' match every sha in the corpus,
// which would turn the report's failure mode from all-stale into all-fresh --
// the same unreadable output with the opposite sign.
test('degenerate diagram_source_sha values match nothing and read STALE', () => {
  for (const bad of ['', '   ', '9', '964e56', null, undefined]) {
    const v = verdict([page({ sha: bad })]);
    assert.deepEqual(v.stale, ['services/Alpha.md'], `${JSON.stringify(bad)} must not match`);
  }
});

test('shaMatches rejects short, blank and non-string operands from either side', () => {
  assert.equal(MIN_SHA_PREFIX, 7, 'the floor is a git short-sha length');
  assert.equal(shaMatches(HEAD, HEAD_SHORT), true);
  assert.equal(shaMatches(HEAD_SHORT, HEAD), true, 'order does not matter');
  assert.equal(shaMatches(HEAD, HEAD), true);
  assert.equal(shaMatches(HEAD_SHORT, HEAD_SHORT), true);
  assert.equal(shaMatches(HEAD, OLD_SHORT), false);
  assert.equal(shaMatches(HEAD, ''), false);
  assert.equal(shaMatches('', HEAD), false);
  assert.equal(shaMatches('', ''), false);
  assert.equal(shaMatches(HEAD, HEAD.slice(0, MIN_SHA_PREFIX - 1)), false, 'one below the floor');
  assert.equal(shaMatches(HEAD, HEAD.slice(0, MIN_SHA_PREFIX)), true, 'exactly at the floor');
  assert.equal(shaMatches(HEAD, null), false);
  assert.equal(shaMatches(undefined, HEAD), false);
  assert.equal(shaMatches(HEAD, 964), false);
});

// --- pinned pre-existing behaviour ----------------------------------------
test('a page with no head info at all is treated as FRESH (never false-flag)', () => {
  // Line 38 of the detector promises this: with nothing to compare against,
  // staleness cannot be proven, so the page must not be billed for regeneration.
  const noHeads = {};
  assert.deepEqual(verdict([page({ sha: HEAD_SHORT })], noHeads).fresh, ['services/Alpha.md']);
  assert.deepEqual(verdict([page({ sha: OLD_SHORT })], noHeads).fresh, ['services/Alpha.md']);
  // including when the page records no sha either -- the degenerate-value rule
  // must not leak into the no-evidence path.
  assert.deepEqual(verdict([page({ sha: '' })], noHeads).fresh, ['services/Alpha.md']);
  assert.deepEqual(verdict([page({ sha: HEAD_SHORT, sourcePaths: [] })], noHeads).stale, []);
});

test('undiagrammed pages participate in neither list', () => {
  const r = computeStaleDiagrams({
    pages: [page({ sha: OLD_SHORT, diagram: false })],
    headSourceShas: heads,
  });
  assert.deepEqual(r.stale, []);
  assert.deepEqual(r.fresh, []);
});

// --- end to end through the CLI and the manifest fallback ------------------
function scaffold({ sha }) {
  const dir = mkdtempSync(join(tmpdir(), 'diagram-staleness-'));
  mkdirSync(join(dir, 'services'));
  writeFileSync(
    join(dir, 'services', 'Alpha.md'),
    `---\ntype: service\ntitle: "Alpha"\ndiagram: true\ndiagram_source_sha: "${sha}"\n` +
      `source_paths:\n  - "src/a.py"\n---\n\n# Alpha\n`,
  );
  // No source_path and no per-source source_sha: the CLI cannot consult git
  // history, so it takes the labelled last_sha fallback (gh#438, gh#533).
  writeFileSync(
    join(dir, 'manifest.json'),
    JSON.stringify({
      source_repo: 'example-org/sample-repo',
      last_sha: HEAD,
      pages: { 'services/Alpha.md': { type: 'service', source_paths: ['src/a.py'] } },
    }),
  );
  return dir;
}

function runCLI(dir) {
  const r = spawnSync(process.execPath, [CLI, dir, join(dir, 'manifest.json')], { encoding: 'utf8' });
  assert.equal(r.status, 0, `CLI exited ${r.status}: ${r.stderr}`);
  return r.stdout;
}

test('CLI: the last_sha fallback no longer reports a current diagram stale', () => {
  const dir = scaffold({ sha: HEAD_SHORT });
  try {
    const out = runCLI(dir);
    assert.match(out, /0 stale of 1 diagrammed pages/);
    assert.match(out, /Mode: FALLBACK \(manifest has no source_path\)/, 'the fallback is never silent');
    assert.doesNotMatch(out, /STALE/);
    assert.match(out, /~0 tokens/, 'no spend recommended for a current diagram');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('CLI: a diagram from an older commit is still reported stale and priced', () => {
  const dir = scaffold({ sha: OLD_SHORT });
  try {
    const out = runCLI(dir);
    assert.match(out, /1 stale of 1 diagrammed pages/);
    assert.match(out, /Mode: FALLBACK \(manifest has no source_path\)/);
    assert.match(out, /STALE {2}services\/Alpha\.md/);
    assert.match(out, /~8000 tokens/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('a sha that is a strict SUPERSTRING of the head is STALE, not fresh', () => {
  // A floor without a ceiling is half a rule: 40 real hex characters followed by anything still
  // "starts with" the head, so the prefix test passed where the old equality correctly refused.
  // A sha is 7..40 hex characters and nothing else.
  assert.equal(shaMatches(HEAD, HEAD + 'deadbeef'), false, 'longer than a sha can be');
  assert.equal(shaMatches(HEAD, HEAD.slice(0, 39) + 'zz'), false, 'non-hex tail');
  assert.equal(shaMatches(HEAD_SHORT, HEAD_SHORT + 'zzzzzz'), false, 'non-hex, right length');
  assert.deepEqual(verdict([page({ sha: HEAD + 'deadbeef' })]).stale, ['services/Alpha.md']);
});

test('an UPPERCASE sha is the same commit, so it reads FRESH', () => {
  // The opposite error to the one above, and the one this issue is actually about: refusing a
  // value that names the current commit produces a false STALE, which is what prices a diagram
  // for regeneration it does not need.
  assert.equal(shaMatches(HEAD_SHORT.toUpperCase(), HEAD), true);
  assert.deepEqual(verdict([page({ sha: HEAD_SHORT.toUpperCase() })]).fresh, ['services/Alpha.md']);
});

// --- gh#533: staleness is decided by git history, not sha equality ----------
// diagram_source_sha is the commit a diagram was drawn at. Equality against last_sha flagged
// every diagram after every refresh, because any unrelated commit moves last_sha. The rule is
// now: stale iff some commit in (diagram_source_sha, last_sha] touched one of the source_paths.
const GIT_ID = ['-c', 'user.name=autodoc-test', '-c', 'user.email=autodoc-test@example.invalid',
  '-c', 'commit.gpgsign=false', '-c', 'init.defaultBranch=main'];

function git(cwd, ...args) {
  return execFileSync('git', [...GIT_ID, '-C', cwd, ...args], { encoding: 'utf8' }).trim();
}

function commitFile(repo, file, content) {
  writeFileSync(join(repo, file), content);
  git(repo, 'add', '--', file);
  git(repo, 'commit', '-q', '-m', `touch ${file}`);
  return git(repo, 'rev-parse', 'HEAD');
}

// A repo with a.py, b.py and an unrelated notes.txt, all committed at A.
function makeRepo(repo) {
  mkdirSync(repo, { recursive: true });
  git(repo, 'init', '-q');
  writeFileSync(join(repo, 'a.py'), 'a = 1\n');
  writeFileSync(join(repo, 'b.py'), 'b = 1\n');
  writeFileSync(join(repo, 'notes.txt'), 'n\n');
  git(repo, 'add', '--', 'a.py', 'b.py', 'notes.txt');
  git(repo, 'commit', '-q', '-m', 'A');
  return git(repo, 'rev-parse', 'HEAD');
}

function withRepo(fn) {
  const root = mkdtempSync(join(tmpdir(), 'diagram-staleness-git-'));
  try {
    const repo = join(root, 'repo');
    const A = makeRepo(repo);
    fn({ root, repo, A });
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

const gitVerdict = (repo, lastSha, pages) => {
  const r = computeStaleDiagrams({ pages, isFresh: gitFreshness({ clone: repo, lastSha }) });
  return {
    stale: r.stale.map((p) => p.path),
    fresh: r.fresh.map((p) => p.path),
    reasons: Object.fromEntries([...r.stale, ...r.fresh].map((p) => [p.path, p.reason])),
    r,
  };
};

test('gh#533 (a): drawn at A, only an unrelated file changed through B -> FRESH at B', () => {
  withRepo(({ repo, A }) => {
    const B = commitFile(repo, 'notes.txt', 'n2\n');
    const v = gitVerdict(repo, B, [page({ sha: A.slice(0, 8), sourcePaths: ['a.py'] })]);
    assert.deepEqual(v.fresh, ['services/Alpha.md']);
    assert.deepEqual(v.stale, []);
    assert.equal(v.r.budget.estRegenTokens, 0);
  });
});

test('gh#533 (b): a source touched at B -> STALE, with the touching commit named', () => {
  withRepo(({ repo, A }) => {
    const B = commitFile(repo, 'a.py', 'a = 2\n');
    const v = gitVerdict(repo, B, [page({ sha: A, sourcePaths: ['a.py'] })]);
    assert.deepEqual(v.stale, ['services/Alpha.md']);
    assert.match(v.reasons['services/Alpha.md'], new RegExp(`source changed at ${B.slice(0, 8)}`));
  });
});

test('gh#533 (c): multi-source page where only a NON-final path changed -> STALE', () => {
  withRepo(({ repo, A }) => {
    const B = commitFile(repo, 'a.py', 'a = 2\n');
    const v = gitVerdict(repo, B, [page({ sha: A.slice(0, 7), sourcePaths: ['a.py', 'b.py'] })]);
    assert.deepEqual(v.stale, ['services/Alpha.md']);
  });
});

test('gh#533 (c, legacy rule): every head is compared, not only the last one', () => {
  // The old detector read heads[heads.length - 1], so a moved first source slipped through.
  const v = verdict(
    [page({ sha: HEAD_SHORT, sourcePaths: ['src/a.py', 'src/b.py'] })],
    { 'src/a.py': OLD_SHORT, 'src/b.py': HEAD },
  );
  assert.deepEqual(v.stale, ['services/Alpha.md']);
});

test('gh#533: a diagram redrawn after the last source change is FRESH', () => {
  withRepo(({ repo }) => {
    commitFile(repo, 'a.py', 'a = 2\n');
    const drawn = commitFile(repo, 'notes.txt', 'redrawn\n');
    const C = commitFile(repo, 'notes.txt', 'later\n');
    const v = gitVerdict(repo, C, [page({ sha: drawn, sourcePaths: ['a.py', 'b.py'] })]);
    assert.deepEqual(v.fresh, ['services/Alpha.md']);
  });
});

test('gh#533 (d): an unknown or non-sha diagram_source_sha -> STALE with a labelled reason', () => {
  withRepo(({ repo, A }) => {
    const v = gitVerdict(repo, A, [
      page({ sha: 'deadbeefdeadbeef', path: 'flows/Unknown.md' }),
      page({ sha: 'not-a-sha', path: 'flows/Garbage.md' }),
      page({ sha: '', path: 'flows/Blank.md' }),
    ]);
    assert.deepEqual(v.stale, ['flows/Unknown.md', 'flows/Garbage.md', 'flows/Blank.md']);
    assert.match(v.reasons['flows/Unknown.md'], /does not resolve to a commit/);
    assert.match(v.reasons['flows/Garbage.md'], /is not a commit sha/);
    assert.match(v.reasons['flows/Blank.md'], /is not a commit sha/);
  });
});

test('gh#533: a page with no source_paths stays FRESH but is marked unproven', () => {
  withRepo(({ repo }) => {
    const r = computeStaleDiagrams({
      pages: [page({ sha: 'deadbeef', sourcePaths: [] })],
      isFresh: gitFreshness({ clone: repo, lastSha: 'HEAD' }),
    });
    assert.deepEqual(r.fresh.map((p) => p.path), ['services/Alpha.md']);
    assert.equal(r.fresh[0].unproven, true);
    assert.match(r.fresh[0].reason, /no source_paths/);
  });
});

test('gh#533: source_paths that exist nowhere in history are UNPROVEN, not a clean fresh', () => {
  // A typo or a renamed-away path makes git log come back empty, which proves nothing.
  withRepo(({ repo, A }) => {
    commitFile(repo, 'a.py', 'a = 2\n');
    const r = computeStaleDiagrams({
      pages: [page({ sha: A, sourcePaths: ['does/not/exist.py', 'src\\a.py'] })],
      isFresh: gitFreshness({ clone: repo, lastSha: 'HEAD' }),
    });
    assert.deepEqual(r.fresh.map((p) => p.path), ['services/Alpha.md']);
    assert.equal(r.fresh[0].unproven, true);
    assert.match(r.fresh[0].reason, /no listed source path exists/);
  });
});

test('gh#533: a last_sha the clone cannot resolve fails loudly instead of guessing', () => {
  withRepo(({ repo }) => {
    assert.throws(() => gitFreshness({ clone: repo, lastSha: 'deadbeefdeadbeef' }), /does not resolve/);
  });
});

// A corpus shaped like the real one: pages at the root, the clone under .source/<name>, and the
// manifest naming it by a path relative to the corpus root.
function gitScaffold(root, { pages, sourcePath = '.source/repo', lastSha }) {
  mkdirSync(join(root, 'flows'), { recursive: true });
  for (const [name, { sha, sourcePaths }] of Object.entries(pages)) {
    writeFileSync(
      join(root, 'flows', `${name}.md`),
      `---\ntype: flow\ndiagram: true\ndiagram_source_sha: "${sha}"\nsource_paths:\n` +
        sourcePaths.map((sp) => `  - "${sp}"\n`).join('') + `---\n\n# ${name}\n`,
    );
  }
  writeFileSync(
    join(root, 'manifest.json'),
    JSON.stringify({
      source_repo: 'example-org/sample-repo',
      source_path: sourcePath,
      last_sha: lastSha,
      pages: Object.fromEntries(
        Object.entries(pages).map(([name, { sourcePaths }]) => [`flows/${name}.md`, { source_paths: sourcePaths }]),
      ),
    }),
  );
}

test('gh#533 (e): CLI against a corpus with a real clone reports by git history', () => {
  const root = mkdtempSync(join(tmpdir(), 'diagram-staleness-cli-'));
  try {
    const repo = join(root, '.source', 'repo');
    const A = makeRepo(repo);
    commitFile(repo, 'notes.txt', 'n2\n');
    const B = commitFile(repo, 'b.py', 'b = 2\n');
    gitScaffold(root, {
      lastSha: B,
      pages: {
        Fresh: { sha: A.slice(0, 8), sourcePaths: ['a.py'] },
        Stale: { sha: A.slice(0, 8), sourcePaths: ['a.py', 'b.py'] },
      },
    });
    const out = runCLI(root);
    assert.match(out, /1 stale of 2 diagrammed pages/);
    assert.match(out, /Mode: git history of \.source\/repo/);
    assert.match(out, /STALE {2}flows\/Stale\.md/);
    assert.doesNotMatch(out, /flows\/Fresh\.md/);
    assert.doesNotMatch(out, /FALLBACK/);
    assert.match(out, /~8000 tokens/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('gh#533 (f): CLI with source_path naming a missing clone falls back and SAYS so', () => {
  const root = mkdtempSync(join(tmpdir(), 'diagram-staleness-cli-'));
  try {
    gitScaffold(root, {
      sourcePath: '.source/missing',
      lastSha: HEAD,
      pages: { Alpha: { sha: OLD_SHORT, sourcePaths: ['a.py'] } },
    });
    const out = runCLI(root);
    assert.match(out, /Mode: FALLBACK \(source clone \.source\/missing not found\)/);
    assert.match(out, /1 stale of 1 diagrammed pages/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('gh#533: CLI exits non-zero, not with a verdict, when last_sha is not in the clone', () => {
  const root = mkdtempSync(join(tmpdir(), 'diagram-staleness-cli-'));
  try {
    const A = makeRepo(join(root, '.source', 'repo'));
    gitScaffold(root, { lastSha: 'deadbeefdeadbeef', pages: { Alpha: { sha: A, sourcePaths: ['a.py'] } } });
    const r = spawnSync(process.execPath, [CLI, root, join(root, 'manifest.json')], { encoding: 'utf8' });
    assert.equal(r.status, 2);
    assert.match(r.stderr, /ERROR manifest last_sha deadbeefdeadbeef does not resolve/);
    assert.doesNotMatch(r.stdout, /stale of/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
