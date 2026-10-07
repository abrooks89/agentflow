// Deterministic diagram-staleness detector for /autodoc-update.
// A diagrammed page is STALE when some commit after its diagram_source_sha, up to the manifest's
// last_sha, touched any of its source_paths. A flow or data-model page whose diagram field is not
// true is MISSING. Flag-don't-regenerate: this only REPORTS both; it never draws or rewrites a
// diagram.
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

// Page types that are expected to carry a diagram. One of these with no diagram is MISSING
// (gh#572): reconcile creates flow pages without ever drawing one, and a page that only looked
// at diagram: true pages could never see them.
const DIAGRAM_TYPES = new Set(['flow', 'data-model']);

// Minimal, dependency-free frontmatter reader for the four fields we need.
function parsePageFrontmatter(text) {
  const m = /^---\n([\s\S]*?)\n---/.exec(text);
  const fm = m ? m[1] : '';
  const out = { type: '', diagram: false, diagram_source_sha: '', source_paths: [] };
  const ty = /^type:\s*"?([^"\n]*?)"?\s*$/m.exec(fm);
  if (ty) out.type = ty[1].trim();
  const dm = /^diagram:\s*(true|false)\s*$/m.exec(fm);
  if (dm) out.diagram = dm[1] === 'true';
  const sh = /^diagram_source_sha:\s*"?([^"\n]*)"?\s*$/m.exec(fm);
  if (sh) out.diagram_source_sha = sh[1].trim();
  const spBlock = /^source_paths:\s*\n((?:\s*-\s*.*\n?)*)/m.exec(fm);
  if (spBlock) {
    out.source_paths = spBlock[1]
      .split('\n')
      .map((l) => /^\s*-\s*"?([^"\n]*)"?\s*$/.exec(l))
      .filter(Boolean)
      .map((mm) => mm[1].trim())
      .filter(Boolean);
  }
  return out;
}

// Shas are compared on a COMMON PREFIX, not by equality: pages record an abbreviated
// diagram_source_sha while the manifest carries the full 40-char last_sha, so equality
// can never hold and every diagram reads stale (gh#438). The floor keeps a short or
// empty value from matching everything; the manifest's full sha is the unambiguous one,
// so nothing is normalised downward.
const MIN_SHA_PREFIX = 7;

// A FLOOR WITHOUT A CEILING IS HALF A RULE. With only the minimum length, a page whose
// diagram_source_sha is a strict SUPERSTRING of the head sha read FRESH - 40 real hex characters
// followed by any garbage still starts with the head, so the prefix test passed where the old
// equality correctly said stale. A sha is 7..40 hex characters and nothing else, so say that.
const MAX_SHA = 40;
const HEX_ONLY = /^[0-9a-f]+$/;

function isShaLike(value) {
  return typeof value === 'string'
    && value.length >= MIN_SHA_PREFIX
    && value.length <= MAX_SHA
    && HEX_ONLY.test(value);
}

function shaMatches(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const x = a.trim().toLowerCase();
  const y = b.trim().toLowerCase();
  if (!isShaLike(x) || !isShaLike(y)) return false;
  const [shortSha, longSha] = x.length <= y.length ? [x, y] : [y, x];
  return longSha.startsWith(shortSha);
}

// Legacy rule, kept only for the labelled no-clone fallback: compare the page's sha to a head
// sha per source path. It cannot tell "drawn before an unrelated commit" from "drawn before a
// change to this source", so with only last_sha to go on every diagram not drawn at exactly
// last_sha reads stale (gh#533). Every head is checked, not just the last one.
function headShaFreshness(headSourceShas) {
  return (p) => {
    const heads = (p.source_paths || []).map((sp) => headSourceShas[sp]).filter(Boolean);
    // No head info at all: cannot prove stale; treat as fresh (don't false-flag).
    if (!heads.length) return { stale: false, unproven: true, reason: 'no head sha for any source path' };
    const moved = heads.find((h) => !shaMatches(h, p.diagram_source_sha));
    if (moved) return { stale: true, reason: `diagram_source_sha differs from head ${String(moved).slice(0, 8)}` };
    return { stale: false, reason: 'diagram_source_sha matches every head' };
  };
}

// Pure over its inputs: git access lives in the injected isFresh, so unit tests can drive the
// verdicts without a repository. Without isFresh it applies the legacy head-sha rule.
function computeStaleDiagrams({ pages, headSourceShas = {}, isFresh }) {
  const judge = isFresh || headShaFreshness(headSourceShas);
  const stale = [];
  const fresh = [];
  const missing = [];
  for (const p of pages) {
    if (p.diagram !== true) {
      // Undiagrammed pages are judged only on whether their type needs a diagram.
      if (DIAGRAM_TYPES.has(p.type)) missing.push(p);
      continue;
    }
    const v = judge(p);
    const entry = { ...p, reason: v.reason, unproven: Boolean(v.unproven) };
    (v.stale ? stale : fresh).push(entry);
  }
  const budget = {
    staleCount: stale.length,
    missingCount: missing.length,
    // Rough regen cost estimate for the budget report (one agent pass per diagram, drawn or redrawn).
    estRegenTokens: (stale.length + missing.length) * 8000,
  };
  return { stale, fresh, missing, budget };
}

// Runs git without a shell. A non-zero exit is data here (is-ancestor answers with exit 1), so it
// is returned; anything without an exit status (git missing, killed) is rethrown.
function runGit(clone, args) {
  try {
    const stdout = execFileSync('git', ['-C', clone, ...args], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    return { status: 0, stdout, stderr: '' };
  } catch (err) {
    if (typeof err.status !== 'number') throw err;
    return { status: err.status, stdout: String(err.stdout || ''), stderr: String(err.stderr || '') };
  }
}

// gh#533: diagram_source_sha is the COMMIT the diagram was drawn at, so the question is whether
// any commit in (diagram_source_sha, last_sha] touched the page's sources -- not whether two shas
// are equal, which fails every diagram after any unrelated commit. One log call covers all of a
// page's source_paths, so a change to any of them counts.
// A per-page manifest source_sha is not consulted here: it could only be a weaker upper bound
// than last_sha, and git history already answers the question it was standing in for.
function gitFreshness({ clone, lastSha, git = runGit }) {
  const head = git(clone, ['rev-parse', '--verify', '--quiet', `${lastSha}^{commit}`]);
  if (head.status !== 0) {
    throw new Error(`manifest last_sha ${lastSha} does not resolve to a commit in ${clone}`);
  }
  const last = head.stdout.trim();
  return (p) => {
    const paths = p.source_paths || [];
    if (!paths.length) {
      return { stale: false, unproven: true, reason: 'no source_paths; staleness cannot be proven' };
    }
    const raw = String(p.diagram_source_sha ?? '');
    const drawnSha = raw.trim().toLowerCase();
    if (!isShaLike(drawnSha)) {
      return { stale: true, reason: `diagram_source_sha ${JSON.stringify(raw)} is not a commit sha` };
    }
    const drawn = git(clone, ['rev-parse', '--verify', '--quiet', `${drawnSha}^{commit}`]);
    if (drawn.status !== 0) {
      return { stale: true, reason: `diagram_source_sha ${drawnSha} does not resolve to a commit in the clone` };
    }
    // Literal pathspecs: a source path like "app/[id].tsx" is a file, not a glob.
    const log = git(clone, ['--literal-pathspecs', 'log', '-1', '--format=%H', last, '--', ...paths]);
    if (log.status !== 0) {
      throw new Error(`git log over ${p.path} source_paths exited ${log.status}: ${log.stderr.trim()}`);
    }
    const touched = log.stdout.trim();
    // Nothing in history up to last_sha touches ANY listed path, so none of them exists there: a
    // typo, a path renamed away, a backslash path. That proves nothing, so say so rather than
    // reporting a clean fresh.
    if (!touched) return { stale: false, unproven: true, reason: 'no listed source path exists in history up to last_sha' };
    const anc = git(clone, ['merge-base', '--is-ancestor', touched, drawn.stdout.trim()]);
    if (anc.status === 0) {
      return { stale: false, reason: `last source change ${touched.slice(0, 8)} is already in ${drawnSha.slice(0, 8)}` };
    }
    if (anc.status === 1) {
      return { stale: true, reason: `source changed at ${touched.slice(0, 8)} after ${drawnSha.slice(0, 8)}` };
    }
    throw new Error(`git merge-base --is-ancestor exited ${anc.status} for ${p.path}: ${anc.stderr.trim()}`);
  };
}

// Picks the rule. The fallback is only for a manifest or corpus that cannot support the git rule,
// and the report always says it was taken.
function resolveMode(autodocDir, manifest) {
  if (!manifest.source_path) return { mode: 'fallback', why: 'manifest has no source_path' };
  if (!manifest.last_sha) return { mode: 'fallback', why: 'manifest has no last_sha' };
  const clone = path.resolve(autodocDir, manifest.source_path);
  if (!fs.existsSync(clone)) return { mode: 'fallback', why: `source clone ${manifest.source_path} not found` };
  return { mode: 'git', clone };
}

// CLI: node diagram-staleness.js <autodoc-dir> <manifest.json>
function main(argv) {
  const [autodocDir, manifestPath] = argv;
  if (!autodocDir || !manifestPath) {
    console.error('usage: diagram-staleness.js <autodoc-dir> <manifest.json>');
    return 0;
  }
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const pages = [];
  const walk = (dir) => {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, ent.name);
      if (ent.isDirectory()) {
        if (!ent.name.startsWith('.')) walk(full);
      } else if (ent.name.endsWith('.md')) {
        const fm = parsePageFrontmatter(fs.readFileSync(full, 'utf8'));
        pages.push({ path: path.relative(autodocDir, full).replace(/\\/g, '/'), ...fm });
      }
    }
  };
  walk(autodocDir);

  const mode = resolveMode(autodocDir, manifest);
  let result;
  let modeLine;
  if (mode.mode === 'git') {
    result = computeStaleDiagrams({ pages, isFresh: gitFreshness({ clone: mode.clone, lastSha: manifest.last_sha }) });
    modeLine = `Mode: git history of ${manifest.source_path} up to last_sha ${String(manifest.last_sha).slice(0, 8)}`;
  } else {
    // manifest.pages may be an array [{path, source_paths, source_sha?}] or an object
    // map keyed by page path { "<path>": {source_paths, source_sha?} }. Normalize to array.
    const rawPages = manifest.pages || [];
    const manifestPages = Array.isArray(rawPages)
      ? rawPages
      : Object.entries(rawPages).map(([p, v]) => ({ path: p, ...v }));
    const headSourceShas = {};
    for (const pg of manifestPages) {
      for (const sp of pg.source_paths || []) {
        if (pg.source_sha) headSourceShas[sp] = pg.source_sha;
      }
    }
    // With no per-page source_sha, every known source maps to the single last_sha.
    if (Object.keys(headSourceShas).length === 0 && manifest.last_sha) {
      for (const pg of manifestPages) {
        for (const sp of pg.source_paths || []) headSourceShas[sp] = manifest.last_sha;
      }
    }
    result = computeStaleDiagrams({ pages, headSourceShas });
    // Name the heads actually compared: per-page source_sha, the single last_sha, or none at all.
    const against = manifestPages.some((pg) => pg.source_sha)
      ? 'per-page source_sha'
      : manifest.last_sha ? 'last_sha; any diagram not drawn at that exact commit reads stale' : 'nothing (no heads; every page is unproven)';
    modeLine = `Mode: FALLBACK (${mode.why}) - sha comparison against ${against}`;
  }
  const { stale, fresh, missing, budget } = result;
  console.log(`Diagram staleness report: ${budget.staleCount} stale of ${pages.filter((p) => p.diagram === true).length} diagrammed pages; `
    + `${budget.missingCount} missing (flow/data-model pages with no diagram)`);
  console.log(modeLine);
  for (const p of stale) console.log(`  STALE  ${p.path}  (was ${p.diagram_source_sha}; ${p.reason})`);
  for (const p of missing) console.log(`  MISSING  ${p.path}  (type ${p.type}; diagram is not true)`);
  for (const p of fresh.filter((f) => f.unproven)) console.log(`  UNPROVEN  ${p.path}  (${p.reason}; counted fresh)`);
  console.log(`Est. regeneration budget: ~${budget.estRegenTokens} tokens (flag-only; regen is an explicit follow-up)`);
  return 0;
}

export { computeStaleDiagrams, gitFreshness, parsePageFrontmatter, shaMatches, MIN_SHA_PREFIX };

// Run as CLI only when invoked directly (ESM equivalent of require.main === module).
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  // Stale diagrams are advisory (exit 0); a git failure is not a verdict, so it exits non-zero.
  let code;
  try {
    code = main(process.argv.slice(2));
  } catch (err) {
    console.error(`diagram-staleness: ERROR ${err.message}`);
    code = 2;
  }
  process.exit(code);
}
