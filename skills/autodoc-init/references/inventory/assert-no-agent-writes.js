// assert-no-agent-writes.js — the hard "no agent may write page content"
// gate (epic 704 constraint). Run in the skill main-loop AFTER the agent
// phases and BEFORE the renderer: the render target must be empty (or
// absent). Any file present is a hard failure naming the path — the
// agent(prompt) driver exposes no tool allowlist, so this post-hoc
// assertion is the real enforcement.
//
//   node assert-no-agent-writes.js <renderTargetDir>
//   node assert-no-agent-writes.js <renderTargetDir> --stamp <path> [--clear-prior-render]
//   node assert-no-agent-writes.js --write-stamp <renderTargetDir> --stamp <path>
//
// THE STAMP MODE DOES NOT WEAKEN THE GATE, and that is the whole design.
// Both runbooks render into a FIXED path ($AUTODOC.rendered): /autodoc-update
// removes it only at the copy-back, /autodoc-init only at the human-approved
// cutover `mv`. So a red Step 5 / golden gate leaves the target populated and
// the next attempt died here accusing an agent of a write whose real author
// was the previous render. Both runbooks now use form (b) + (c). The fix is provenance, not tolerance: the runbook
// runs --write-stamp immediately after a successful `cli render`, and only a
// tree matching that stamp's listing EXACTLY is forgiven. One extra file, one
// missing file, a missing stamp, an unreadable or malformed one, a stamp
// describing a different directory — any of those is still 'agent-write' and
// still exits 1. A guard that cannot fail would be worse than the bug it
// hides, so every doubt fails closed.
//
// WHAT THE STAMP ACTUALLY RECORDS: whatever is present in the target at the
// moment --write-stamp runs. Not "what the renderer produced" — this file
// cannot know that. It is a name-only listing, unauthenticated, at a
// predictable path. The honest security property is better than that
// overclaim anyway: anything able to counterfeit the stamp could equally have
// written the pages themselves, and a counterfeit stamp buys the counterfeiter ERASURE OF
// THEIR OWN FILES, never injection of content into the corpus —
// --clear-prior-render deletes what the stamp lists, and `cli render` then
// rewrites the tree from the store. The window between `cli render` and
// --write-stamp is identical to the pre-stamp behaviour, so nothing here is a
// weakening; the guarantee the prose used to claim was never implemented.
import {
  existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync,
} from 'node:fs';
import { dirname, join, resolve, sep } from 'node:path';

// Bumped if the stamp's shape changes. classify() fails closed on anything
// else, so an old stamp read by a new binary is an 'agent-write', never a
// silent pass — the operator clears the target by hand once and moves on.
export const STAMP_SCHEMA = 'autodoc/no-agent-writes-stamp@1';

export function findFiles(dir) {
  if (!existsSync(dir)) return [];
  const out = [];
  const walk = (current, rel) => {
    for (const ent of readdirSync(current, { withFileTypes: true })) {
      const relPath = rel ? `${rel}/${ent.name}` : ent.name;
      if (ent.isDirectory()) walk(join(current, ent.name), relPath);
      else out.push(relPath);
    }
  };
  walk(dir, '');
  return out.sort();
}

function isInside(child, parent) {
  return child === parent || child.startsWith(parent + sep);
}

// Record what is in `dir` RIGHT NOW — the listing is a snapshot, not a report
// from the renderer. Call it immediately after a successful `cli render` and
// nowhere else: that placement is the only thing that makes the snapshot and
// the render's output the same set of files. A stamp written early would
// account for files the renderer had not yet written.
export function writeStamp(dir, stampPath) {
  if (!stampPath) throw new Error('writeStamp: a stamp path is required');
  const resolvedDir = resolve(dir);
  const resolvedStamp = resolve(stampPath);
  // OUTSIDE the render target, or the stamp is part of what it stamps: it
  // would have to list itself, and --clear-prior-render would delete the only
  // record of what it was allowed to delete.
  if (isInside(resolvedStamp, resolvedDir)) {
    throw new Error(
      `writeStamp: stamp path ${stampPath} is inside ${dir} — the stamp must live outside the render target`,
    );
  }
  const files = findFiles(dir);
  const stamp = {
    schema: STAMP_SCHEMA,
    dir: resolvedDir,
    wrote_at: new Date().toISOString(),
    files,
  };
  mkdirSync(dirname(resolvedStamp), { recursive: true });
  writeFileSync(resolvedStamp, `${JSON.stringify(stamp, null, 2)}\n`);
  return stamp;
}

function agentWrite(unaccounted, missing, reason) {
  return { verdict: 'agent-write', unaccounted, missing, reason };
}

// Exactly one of:
//   { verdict: 'empty' }                      — absent, or no files
//   { verdict: 'prior-render', files }        — stamp readable AND listing matches exactly
//   { verdict: 'agent-write', unaccounted, missing, reason }  — everything else
//
// `unaccounted` is the files PRESENT that the stamp does not list. It is empty
// when the only mismatch is a file the stamp lists and the tree no longer has;
// `reason` says which case this is so the CLI never prints an empty list as if
// it meant "clean".
export function classify(dir, stampPath) {
  const present = findFiles(dir);
  if (present.length === 0) return { verdict: 'empty' };

  if (!stampPath) {
    return agentWrite(present, [], 'no render stamp path was given, so nothing accounts for these files');
  }

  let raw;
  try {
    raw = readFileSync(stampPath, 'utf8');
  } catch {
    return agentWrite(present, [], `no readable render stamp at ${stampPath}`);
  }

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return agentWrite(present, [], `the render stamp at ${stampPath} is not valid JSON`);
  }

  const wellFormed = parsed
    && typeof parsed === 'object'
    && !Array.isArray(parsed)
    && parsed.schema === STAMP_SCHEMA
    && typeof parsed.dir === 'string'
    && parsed.dir !== ''
    && Array.isArray(parsed.files)
    && parsed.files.every((f) => typeof f === 'string');
  if (!wellFormed) {
    return agentWrite(
      present, [],
      `the render stamp at ${stampPath} is not a ${STAMP_SCHEMA} stamp`,
    );
  }

  // A stamp authorises a delete of the tree it DESCRIBES, and nothing else. Without this the
  // `dir` field is dead data: a stamp taken of directory A would clear a same-shaped directory B,
  // with A's absolute path sitting in the record that authorised it.
  const stampedDir = resolve(parsed.dir);
  const targetDir = resolve(dir);
  if (stampedDir !== targetDir) {
    return agentWrite(
      present, [],
      `the render stamp at ${stampPath} describes ${stampedDir}, not ${targetDir}`,
    );
  }

  const stamped = new Set(parsed.files);
  const here = new Set(present);
  const unaccounted = present.filter((f) => !stamped.has(f));
  const missing = [...stamped].filter((f) => !here.has(f)).sort();

  if (unaccounted.length === 0 && missing.length === 0) {
    return { verdict: 'prior-render', files: present };
  }
  const parts = [];
  if (unaccounted.length) parts.push(`${unaccounted.length} file(s) the stamp does not list`);
  if (missing.length) {
    parts.push(
      `${missing.length} stamped file(s) no longer present (the tree was edited after the render — failing closed)`,
    );
  }
  return agentWrite(unaccounted, missing, parts.join('; '));
}

// Delete the CONTENTS of dir. Never dir itself, never the stamp (which lives
// outside dir anyway — the skip is belt-and-braces against a caller that
// ignored writeStamp's refusal), never anything outside dir.
function clearContents(dir, stampPath) {
  const resolvedStamp = stampPath ? resolve(stampPath) : null;
  const removed = [];
  for (const ent of readdirSync(dir, { withFileTypes: true })) {
    const abs = join(dir, ent.name);
    if (resolvedStamp && resolve(abs) === resolvedStamp) continue;
    rmSync(abs, { recursive: true, force: true });
    removed.push(ent.name);
  }
  return removed.sort();
}

const USAGE = [
  'usage: node assert-no-agent-writes.js <renderTargetDir>',
  '       node assert-no-agent-writes.js <renderTargetDir> --stamp <path> [--clear-prior-render]',
  '       node assert-no-agent-writes.js --write-stamp <renderTargetDir> --stamp <path>',
].join('\n');

export function parseCliArgs(argv) {
  const out = { dir: null, stamp: null, clear: false, writeStampDir: null, error: null };
  const needsValue = (flag, v) => {
    if (v === undefined || v.startsWith('--')) {
      out.error = `${flag} requires a value`;
      return false;
    }
    return true;
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--stamp') {
      const v = argv[i + 1];
      if (!needsValue('--stamp', v)) return out;
      out.stamp = v;
      i += 1;
    } else if (arg === '--write-stamp') {
      const v = argv[i + 1];
      if (!needsValue('--write-stamp', v)) return out;
      out.writeStampDir = v;
      i += 1;
    } else if (arg === '--clear-prior-render') {
      out.clear = true;
    } else if (arg.startsWith('--')) {
      out.error = `unknown option ${arg}`;
      return out;
    } else if (out.dir === null) {
      out.dir = arg;
    } else {
      out.error = `unexpected argument ${arg}`;
      return out;
    }
  }
  // `null` means the flag/positional was never seen; EMPTY STRING means it was seen and carried
  // nothing. Both are usage errors, and the difference between `=== null` and falsiness is the
  // whole gate: an unbound `$RENDER_TARGET` expands to `""`, `findFiles('')` short-circuits on
  // `existsSync('') === false`, and the one assertion enforcing "no agent may write page content"
  // would print OK on a directory it never looked at. Dispatch on `!== null` (was the flag
  // given?), validate on falsiness (did it carry a value?).
  if (out.writeStampDir !== null) {
    if (!out.writeStampDir) out.error = '--write-stamp requires a render target directory';
    else if (out.dir !== null) out.error = '--write-stamp takes the directory itself; drop the positional argument';
    else if (!out.stamp) out.error = '--write-stamp requires --stamp <path>';
    else if (out.clear) out.error = '--clear-prior-render is meaningless with --write-stamp';
    return out;
  }
  if (!out.dir) out.error = 'a render target directory is required';
  else if (out.clear && !out.stamp) out.error = '--clear-prior-render requires --stamp <path>';
  return out;
}

const isMain = process.argv[1] && process.argv[1].endsWith('assert-no-agent-writes.js');
if (isMain) {
  const args = parseCliArgs(process.argv.slice(2));
  if (args.error) {
    console.error(`${args.error}\n${USAGE}`);
    process.exit(2);
  }

  if (args.writeStampDir !== null) {
    let stamp;
    try {
      stamp = writeStamp(args.writeStampDir, args.stamp);
    } catch (err) {
      console.error(`FATAL: ${err.message}`);
      process.exit(2);
    }
    console.log(
      `no-agent-writes: stamped ${stamp.files.length} rendered file(s) from ${args.writeStampDir} -> ${args.stamp}`,
    );
    process.exit(0);
  }

  const dir = args.dir;

  // FORM (a), unchanged: no stamp, no tolerance. Neither runbook calls this
  // form any more — a first attempt starts from an empty target, but a retry
  // after a red gate does not, so both pass --stamp. It stays for ad-hoc use
  // and as the strictest reading of the gate.
  if (!args.stamp) {
    const files = findFiles(dir);
    if (files.length) {
      console.error(`ASSERT FAILED: render target ${dir} is not empty — an agent (or something else) wrote page content before the renderer ran:`);
      for (const f of files.slice(0, 20)) console.error(`  ${f}`);
      process.exit(1);
    }
    console.log(`no-agent-writes: OK — ${dir} is empty`);
    process.exit(0);
  }

  // FORM (b): discriminate by provenance.
  const result = classify(dir, args.stamp);
  if (result.verdict === 'empty') {
    console.log(`no-agent-writes: OK — ${dir} is empty`);
    process.exit(0);
  }

  if (result.verdict === 'prior-render') {
    if (!args.clear) {
      console.error(`ASSERT FAILED: render target ${dir} is not empty, but it IS a prior render of this runbook — every one of its ${result.files.length} file(s) is accounted for by the stamp at ${args.stamp}:`);
      for (const f of result.files.slice(0, 20)) console.error(`  ${f}`);
      console.error('No agent wrote this. Re-run with --clear-prior-render to clear it and continue.');
      process.exit(1);
    }
    const removed = clearContents(dir, args.stamp);
    console.log(`no-agent-writes: cleared ${removed.length} entr${removed.length === 1 ? 'y' : 'ies'} from ${dir} — a prior render of this runbook, accounted for in full by the stamp at ${args.stamp}:`);
    for (const f of removed.slice(0, 20)) console.log(`  ${f}`);
    process.exit(0);
  }

  console.error(`ASSERT FAILED: render target ${dir} is not empty — an agent (or something else) wrote page content before the renderer ran:`);
  console.error(`  (${result.reason})`);
  for (const f of result.unaccounted.slice(0, 20)) console.error(`  ${f}`);
  for (const f of result.missing.slice(0, 20)) console.error(`  MISSING (stamped, now gone): ${f}`);
  process.exit(1);
}
