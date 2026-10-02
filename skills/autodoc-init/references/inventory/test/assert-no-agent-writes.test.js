// test/assert-no-agent-writes.test.js — the provenance gate (gh#431 item 2).
// The assertion is the ONLY enforcement of the epic-704 "no agent may write
// page content" constraint, so the tests that matter most here are the ones
// that must stay RED: an extra file, a missing stamp, a malformed stamp and a
// vanished stamped file each have to fail even though the tolerance path
// exists one branch over. Every test below trips exactly one branch — neuter
// that branch and this file goes red.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import {
  existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';

import { findFiles, writeStamp, classify, STAMP_SCHEMA } from '../assert-no-agent-writes.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CLI = join(__dirname, '..', 'assert-no-agent-writes.js');

function runCLI(args) {
  return spawnSync(process.execPath, [CLI, ...args], { encoding: 'utf8' });
}

// A real temp tree, never a stubbed fs: the whole guard is a statement about
// what is on disk, and a stub would let the test agree with the code about a
// filesystem neither of them touched.
function scratch() {
  const root = mkdtempSync(join(tmpdir(), 'no-agent-writes-'));
  const dir = join(root, 'rendered');
  mkdirSync(dir);
  return { root, dir, stamp: join(root, 'rendered.render-stamp.json') };
}

function write(dir, rel, text = 'page body\n') {
  const abs = join(dir, rel);
  mkdirSync(dirname(abs), { recursive: true });
  writeFileSync(abs, text);
}

function renderedPages(dir) {
  write(dir, 'services/Alpha.md');
  write(dir, 'services/Beta.md');
  write(dir, 'flows/Login.md');
}

function cleanup(root) {
  rmSync(root, { recursive: true, force: true });
}

// ---------------------------------------------------------------- form (a)

test('form (a): an empty (or absent) target exits 0 with the OK line', () => {
  const { root, dir } = scratch();
  try {
    const ok = runCLI([dir]);
    assert.equal(ok.status, 0, ok.stderr);
    assert.match(ok.stdout, /no-agent-writes: OK/);

    // Absent is the same answer as empty — /autodoc-init runs before anything
    // has created the target.
    const absent = runCLI([join(root, 'never-created')]);
    assert.equal(absent.status, 0, absent.stderr);
  } finally {
    cleanup(root);
  }
});

test('form (a): any file present exits 1 with the existing accusatory message', () => {
  const { root, dir } = scratch();
  try {
    write(dir, 'services/Alpha.md');
    const res = runCLI([dir]);
    assert.equal(res.status, 1);
    // The message text is load-bearing: form (b) reuses it for a real agent
    // write, and the operator's only diagnosis is this line.
    assert.match(res.stderr, /ASSERT FAILED: render target .* is not empty/);
    assert.match(res.stderr, /an agent \(or something else\) wrote page content before the renderer ran/);
    assert.match(res.stderr, /services\/Alpha\.md/);
  } finally {
    cleanup(root);
  }
});

test('form (a) ignores an adjacent stamp entirely — no stamp flag, no tolerance', () => {
  const { root, dir, stamp } = scratch();
  try {
    renderedPages(dir);
    writeStamp(dir, stamp);
    // Same tree that form (b) would clear; without --stamp it must still fail.
    const res = runCLI([dir]);
    assert.equal(res.status, 1, 'form (a) grew a tolerance it must not have');
    assert.equal(findFiles(dir).length, 3, 'form (a) must never delete anything');
  } finally {
    cleanup(root);
  }
});

// ---------------------------------------------------------------- form (b)

test('stamp mode: an empty target exits 0 without needing a stamp to exist', () => {
  const { root, dir, stamp } = scratch();
  try {
    const res = runCLI([dir, '--stamp', stamp]);
    assert.equal(res.status, 0, res.stderr);
    assert.match(res.stdout, /no-agent-writes: OK/);
  } finally {
    cleanup(root);
  }
});

test('stamp mode: an exact match WITH --clear-prior-render exits 0 and empties the dir', () => {
  const { root, dir, stamp } = scratch();
  try {
    renderedPages(dir);
    writeStamp(dir, stamp);
    const res = runCLI([dir, '--stamp', stamp, '--clear-prior-render']);
    assert.equal(res.status, 0, res.stderr);
    assert.match(res.stdout, /prior render of this runbook/);
    assert.deepEqual(findFiles(dir), [], 'the prior render was not cleared');
    assert.ok(existsSync(dir), 'the target directory itself must survive');
    assert.ok(existsSync(stamp), 'the stamp lives outside the target and must not be deleted');
  } finally {
    cleanup(root);
  }
});

test('stamp mode: an exact match WITHOUT the flag exits 1 and leaves the dir alone', () => {
  const { root, dir, stamp } = scratch();
  try {
    renderedPages(dir);
    writeStamp(dir, stamp);
    const res = runCLI([dir, '--stamp', stamp]);
    assert.equal(res.status, 1);
    // It must NOT accuse an agent — that false diagnosis is the defect.
    assert.match(res.stderr, /it IS a prior render of this runbook/);
    assert.match(res.stderr, /--clear-prior-render/);
    assert.equal(findFiles(dir).length, 3, 'nothing may be deleted without the flag');
  } finally {
    cleanup(root);
  }
});

test('stamp mode: one extra file the stamp does not list is an agent write — named, and nothing deleted', () => {
  const { root, dir, stamp } = scratch();
  try {
    renderedPages(dir);
    writeStamp(dir, stamp);
    write(dir, 'services/Smuggled.md');   // the agent write this gate exists for
    const { verdict, unaccounted } = classify(dir, stamp);
    assert.equal(verdict, 'agent-write');
    assert.deepEqual(unaccounted, ['services/Smuggled.md']);

    const res = runCLI([dir, '--stamp', stamp, '--clear-prior-render']);
    assert.equal(res.status, 1, 'an agent write passed the gate');
    assert.match(res.stderr, /an agent \(or something else\) wrote page content/);
    assert.match(res.stderr, /services\/Smuggled\.md/);
    assert.equal(findFiles(dir).length, 4, 'the evidence of an agent write must not be deleted');
  } finally {
    cleanup(root);
  }
});

test('stamp mode: a missing stamp file is an agent write (fail closed)', () => {
  const { root, dir, stamp } = scratch();
  try {
    renderedPages(dir);   // deliberately no writeStamp
    assert.equal(classify(dir, stamp).verdict, 'agent-write');
    const res = runCLI([dir, '--stamp', stamp, '--clear-prior-render']);
    assert.equal(res.status, 1);
    assert.match(res.stderr, /no readable render stamp/);
    assert.equal(findFiles(dir).length, 3);
  } finally {
    cleanup(root);
  }
});

test('stamp mode: malformed JSON in the stamp is an agent write (fail closed)', () => {
  const { root, dir, stamp } = scratch();
  try {
    renderedPages(dir);
    writeFileSync(stamp, '{ "schema": "autodoc/no-agent');   // truncated write
    assert.equal(classify(dir, stamp).verdict, 'agent-write');
    const res = runCLI([dir, '--stamp', stamp, '--clear-prior-render']);
    assert.equal(res.status, 1, 'an unparseable stamp must not authorise a delete');
    assert.match(res.stderr, /not valid JSON/);
    assert.equal(findFiles(dir).length, 3);
  } finally {
    cleanup(root);
  }
});

test('stamp mode: a well-formed stamp of the WRONG shape is an agent write', () => {
  const { root, dir, stamp } = scratch();
  try {
    renderedPages(dir);
    // Parses fine, lists every file, and is still not a stamp: no schema key.
    writeFileSync(stamp, JSON.stringify({ files: findFiles(dir) }));
    const res = runCLI([dir, '--stamp', stamp, '--clear-prior-render']);
    assert.equal(res.status, 1);
    assert.match(res.stderr, new RegExp(`not a ${STAMP_SCHEMA.replace('/', '\\/')} stamp`));
    assert.equal(findFiles(dir).length, 3);
  } finally {
    cleanup(root);
  }
});

test('stamp mode: a stamped file that no longer exists fails closed and says so', () => {
  const { root, dir, stamp } = scratch();
  try {
    renderedPages(dir);
    writeStamp(dir, stamp);
    rmSync(join(dir, 'flows', 'Login.md'));   // something edited the tree post-render
    const result = classify(dir, stamp);
    assert.equal(result.verdict, 'agent-write');
    assert.deepEqual(result.unaccounted, [], 'nothing extra is present in this case');
    assert.deepEqual(result.missing, ['flows/Login.md']);

    const res = runCLI([dir, '--stamp', stamp, '--clear-prior-render']);
    assert.equal(res.status, 1, 'a tree that drifted from the stamp must not be cleared');
    // An empty `unaccounted` must not read as "clean" — the message has to
    // name the missing file, or this failure is undiagnosable.
    assert.match(res.stderr, /MISSING \(stamped, now gone\): flows\/Login\.md/);
    assert.equal(findFiles(dir).length, 2, 'the drifted tree must survive for inspection');
  } finally {
    cleanup(root);
  }
});

// ---------------------------------------------------------------- form (c)

test('writeStamp -> classify round-trips to prior-render', () => {
  const { root, dir, stamp } = scratch();
  try {
    renderedPages(dir);
    const written = writeStamp(dir, stamp);
    assert.equal(written.schema, STAMP_SCHEMA);
    assert.deepEqual(written.files, ['flows/Login.md', 'services/Alpha.md', 'services/Beta.md']);
    const result = classify(dir, stamp);
    assert.equal(result.verdict, 'prior-render');
    assert.deepEqual(result.files, written.files);
  } finally {
    cleanup(root);
  }
});

test('form (c): --write-stamp writes the stamp and exits 0', () => {
  const { root, dir, stamp } = scratch();
  try {
    renderedPages(dir);
    const res = runCLI(['--write-stamp', dir, '--stamp', stamp]);
    assert.equal(res.status, 0, res.stderr);
    const parsed = JSON.parse(readFileSync(stamp, 'utf8'));
    assert.equal(parsed.schema, STAMP_SCHEMA);
    assert.deepEqual([...parsed.files].sort(), findFiles(dir));
    // And the pair composes: the stamp this form writes is the one the clear
    // form accepts. That composition IS the fix; testing the halves separately
    // would pass with two incompatible schemas.
    const cleared = runCLI([dir, '--stamp', stamp, '--clear-prior-render']);
    assert.equal(cleared.status, 0, cleared.stderr);
    assert.deepEqual(findFiles(dir), []);
  } finally {
    cleanup(root);
  }
});

// ------------------------------------------------ /autodoc-init retry (gh#594)

// The init runbook's exact sequence: assert (--stamp --clear-prior-render) ->
// render -> --write-stamp -> a red Step 5 gate stops the run -> the operator
// re-runs Step 4 unchanged. The retry must clear the prior render and pass,
// and a foreign file dropped in between must still stop it with nothing lost.
test('init retry: a stamped prior render clears and passes; an unstamped write still fails', () => {
  const { root, dir, stamp } = scratch();
  const initGate = () => runCLI([dir, '--stamp', stamp, '--clear-prior-render']);
  try {
    // First attempt: target absent-or-empty, no stamp exists yet.
    rmSync(dir, { recursive: true, force: true });
    const first = initGate();
    assert.equal(first.status, 0, first.stderr);
    mkdirSync(dir);
    renderedPages(dir);                                   // `cli render`
    const stamped = runCLI(['--write-stamp', dir, '--stamp', stamp]);
    assert.equal(stamped.status, 0, stamped.stderr);
    // ...Step 5 goes red here, so the target is never moved away.

    const retry = initGate();
    assert.equal(retry.status, 0, `the retry died on its own previous render: ${retry.stderr}`);
    assert.doesNotMatch(retry.stderr, /an agent \(or something else\) wrote page content/);
    assert.deepEqual(findFiles(dir), []);

    // Second render, stamped, then a write no render produced.
    renderedPages(dir);
    assert.equal(runCLI(['--write-stamp', dir, '--stamp', stamp]).status, 0);
    write(dir, 'services/Smuggled.md');
    const tampered = initGate();
    assert.equal(tampered.status, 1, 'an unstamped write passed the init gate');
    assert.match(tampered.stderr, /an agent \(or something else\) wrote page content/);
    assert.match(tampered.stderr, /services\/Smuggled\.md/);
    assert.equal(findFiles(dir).length, 4, 'the evidence must not be deleted');
  } finally {
    cleanup(root);
  }
});

// ------------------------------------------------ the stamp names ONE directory

test('stamp mode: a stamp taken of ANOTHER directory is an agent write, and clears nothing', () => {
  const { root: rootA, dir: dirA, stamp: stampA } = scratch();
  const { root: rootB, dir: dirB } = scratch();
  try {
    // Identical listings in both trees, so the file names cannot be what decides this: the only
    // difference between A and B is which one the stamp says it describes.
    renderedPages(dirA);
    renderedPages(dirB);
    writeStamp(dirA, stampA);

    const result = classify(dirB, stampA);
    assert.equal(result.verdict, 'agent-write', "a stamp of another directory authorised B's delete");
    assert.match(result.reason, /describes/);

    const res = runCLI([dirB, '--stamp', stampA, '--clear-prior-render']);
    assert.equal(res.status, 1, 'exit 0 here means A\'s stamp authorised deleting B');
    // The exit code alone would pass even if the clear had already happened.
    assert.equal(findFiles(dirB).length, 3, 'B was cleared on the authority of a stamp of A');
    assert.equal(findFiles(dirA).length, 3, 'A must not be touched either');
  } finally {
    cleanup(rootA);
    cleanup(rootB);
  }
});

test('stamp mode: a stamp with no dir, or a non-string one, is an agent write (fail closed)', () => {
  const { root, dir, stamp } = scratch();
  try {
    renderedPages(dir);
    const files = findFiles(dir);
    for (const bad of [
      { schema: STAMP_SCHEMA, files },                  // dir omitted entirely
      { schema: STAMP_SCHEMA, dir: 17, files },         // present, wrong type
      { schema: STAMP_SCHEMA, dir: '', files },         // present, empty — resolve('') is cwd
    ]) {
      writeFileSync(stamp, JSON.stringify(bad));
      const result = classify(dir, stamp);
      assert.equal(result.verdict, 'agent-write', `${JSON.stringify(bad.dir)} was forgiven`);
      // The SHAPE check has to catch these, not the path comparison downstream of it. `dir: 17`
      // reaches `resolve()` as a crash otherwise, and `dir: ''` resolves to the cwd and gets
      // diagnosed as "describes some other directory" — true by accident, and the wrong story.
      assert.match(result.reason, new RegExp(`is not a ${STAMP_SCHEMA.replace('/', '\\/')} stamp`));
      const res = runCLI([dir, '--stamp', stamp, '--clear-prior-render']);
      assert.equal(res.status, 1);
      assert.equal(findFiles(dir).length, 3);
    }
  } finally {
    cleanup(root);
  }
});

test('writeStamp refuses a stamp path inside the directory it stamps', () => {
  const { root, dir } = scratch();
  try {
    assert.throws(
      () => writeStamp(dir, join(dir, 'stamp.json')),
      /must live outside the render target/,
    );
    assert.deepEqual(readdirSync(dir), [], 'the refusal must not leave the stamp behind');
  } finally {
    cleanup(root);
  }
});

// ---------------------------------------------------------------- usage

test('usage errors exit 2 — never 0, never 1', () => {
  const { root, dir, stamp } = scratch();
  try {
    const cases = [
      [[], 'no directory at all'],
      [[dir, '--stamp'], '--stamp with no value'],
      [[dir, '--stamp', '--clear-prior-render'], '--stamp swallowing the next flag as its value'],
      [[dir, '--clear-prior-render'], '--clear-prior-render without --stamp'],
      [['--write-stamp', dir], '--write-stamp without --stamp'],
      [['--write-stamp'], '--write-stamp with no value'],
      [[dir, '--nonsense'], 'an unknown option'],
    ];
    for (const [args, why] of cases) {
      const res = runCLI(args);
      assert.equal(res.status, 2, `${why}: expected exit 2, got ${res.status}`);
      assert.match(res.stderr, /usage: node assert-no-agent-writes\.js/);
    }
    // A usage error must never be mistaken for a pass on a populated tree.
    write(dir, 'services/Alpha.md');
    assert.equal(runCLI([dir, '--clear-prior-render']).status, 2);
    assert.equal(findFiles(dir).length, 1);
    assert.equal(existsSync(stamp), false);
  } finally {
    cleanup(root);
  }
});

// AN EMPTY STRING IS AN ARGUMENT THE SHELL PASSED, NOT AN ARGUMENT NOBODY GAVE. `$RENDER_TARGET`
// unbound expands to `""`, which arrives here as a real argv entry; `findFiles('')` then
// short-circuits on `existsSync('') === false` and reports an empty tree. Tested through the CLI
// and not parseCliArgs alone, because the whole defect is what the PROCESS exits with.
test('an empty-string directory is a usage error, not an empty render target', () => {
  const { root, stamp } = scratch();
  try {
    const bare = runCLI(['']);
    assert.equal(bare.status, 2, `"" was accepted as a directory: ${bare.stdout}${bare.stderr}`);
    assert.match(bare.stderr, /usage: node assert-no-agent-writes\.js/);
    assert.doesNotMatch(bare.stdout, /no-agent-writes: OK/);

    const withStamp = runCLI(['', '--stamp', stamp, '--clear-prior-render']);
    assert.equal(withStamp.status, 2, '"" was accepted as a directory in stamp mode');

    // And the same one character on the --write-stamp branch, where accepting "" wrote a stamp
    // whose `dir` was the CURRENT WORKING DIRECTORY — a record that lies about what it describes.
    const ws = runCLI(['--write-stamp', '', '--stamp', stamp]);
    assert.equal(ws.status, 2, `--write-stamp "" was accepted: ${ws.stdout}${ws.stderr}`);
    assert.match(ws.stderr, /usage: node assert-no-agent-writes\.js/);
    assert.equal(existsSync(stamp), false, 'a usage error must not leave a stamp behind');
  } finally {
    cleanup(root);
  }
});
