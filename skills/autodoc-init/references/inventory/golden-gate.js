// golden-gate.js — structural-equivalence gate between a rendered autodoc
// tree and the frozen baseline (epic 704 scope 6, Behavior-Frozen Port
// Pattern). Exact equality is impossible (bodies are LLM prose); the gate
// asserts STRUCTURE, with the page-set tolerance a declared number in the
// config, never a judgement call at review time.
//
//   node golden-gate.js <renderedDir> <baselineDir> <config.json> <repoPath|-> <inventoryPath|->
//
// config.json: { "pageSetSymmetricDiffBudget": <int> }          (default: fresh init)
//              { "pageSetLostBudget": <int> }                   (opt-in: refresh)
//
// Assertions (one PASS/FAIL line each; exit 1 on any FAIL):
//   page-set        symmetric difference of content-page paths <= budget,
//                   or — with pageSetLostBudget — lost pages <= that budget
//   type-match      every common page keeps its baseline `type`
//   frontmatter     every rendered page carries its type's required fields
//                   non-empty; structural fields match baseline exactly
//   links-resolve   zero broken wikilinks in the rendered tree
//   link-recall     per-page resolved outbound link count >= baseline
//   source-paths    every rendered source_paths entry exists under the repo
//   blocker-parity  checkGraph blockers(rendered) <= blockers(baseline)
//   coverage-parity rendered covered file set ⊇ baseline covered file set
//
// Nav pages (index/_index/overview/Start Here/Glossary/log/hot) are excluded:
// the renderer builds nav deterministically, so agent-era nav differs by
// design and is validated by its own checks.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  buildKeyIndex, checkGraph, classify, listPages, normKey, parsePage, splitAnchor,
} from './graph-check.js';

const here = dirname(fileURLToPath(import.meta.url));
// Single source of truth for the per-type page contract (shared with Python).
const TYPE_FIELDS = JSON.parse(
  readFileSync(join(here, '..', 'graph', 'autodoc_graph', 'type_fields.json'), 'utf8'),
);
const UNIVERSAL_REQUIRED = ['type', 'title', 'created', 'updated', 'source_repo', 'status'];

function loadTree(dir) {
  const pages = listPages(dir, { excludeNav: true }).map((p) => {
    const text = readFileSync(p.abspath, 'utf8');
    return { ...p, text, ...parsePage(text) };
  });
  const byFile = new Map(pages.map((p) => [p.file, p]));
  const index = buildKeyIndex(pages);
  return { dir, pages, byFile, index };
}

const empty = (v) => v === undefined || v === null || v === ''
  || (Array.isArray(v) && v.length === 0);

function resolvedOutbound(page, index) {
  let n = 0;
  for (const target of page.links) {
    const { name } = splitAnchor(target);
    if (!name) continue;
    if ((index.get(normKey(name)) || []).length > 0) n += 1;
  }
  return n;
}

// Two page-set modes, because the two callers are asking different questions.
//
// A FRESH init re-renders a tree that already exists and must reproduce it: any
// difference in either direction is a regression, so the default compares the
// SYMMETRIC difference against `pageSetSymmetricDiffBudget`.
//
// A REFRESH documents new code, so creating pages IS the job and losing one is
// silent damage. Measured on the first end-to-end /autodoc-update run (gh#431,
// E-0743 item 3): the render created 88 pages, `only-baseline` was EMPTY — not
// a single page lost — and the symmetric gate failed it at `88 > budget 0`.
// That budget was the count of root-level *.md minus nav pages, which is 0
// because every root page IS nav; a budget of 0 can only be met by a refresh
// that creates nothing, so the gate was unpassable by construction for exactly
// the case it exists to guard. Setting `pageSetLostBudget` budgets LOSS and
// leaves growth uncapped — but the message still reports the created count,
// because a gate that hides growth is how an unexpected 400-page render slips
// through on a PASS line.
//
// `pageSetSymmetricDiffBudget` stays REQUIRED on the default path and becomes
// OPTIONAL once `pageSetLostBudget` is given: the symmetric number is not read
// in that mode, and demanding a declared tolerance that nothing consults would
// be its own small lie. Supplied anyway, it is still validated — a malformed
// value must never read as fine just because this mode ignores it.
export function runGoldenGate({ renderedDir, baselineDir, config, repoPath, inventoryPath }) {
  const lostBudget = config.pageSetLostBudget;
  const lostMode = lostBudget !== undefined && lostBudget !== null;
  const budget = config.pageSetSymmetricDiffBudget;
  const declared = (name, v) => {
    if (!Number.isInteger(v) || v < 0) {
      throw new Error(`config.${name} must be a declared non-negative integer — an open-ended tolerance would make the gate a lie`);
    }
  };
  if (lostMode) declared('pageSetLostBudget', lostBudget);
  if (!lostMode || budget !== undefined) declared('pageSetSymmetricDiffBudget', budget);
  const rendered = loadTree(renderedDir);
  const baseline = loadTree(baselineDir);
  const results = [];
  const fail = (id, detail) => results.push({ id, ok: false, detail });
  const pass = (id, detail) => results.push({ id, ok: true, detail });

  // page-set --------------------------------------------------------------
  const rSet = new Set(rendered.pages.map((p) => p.file));
  const bSet = new Set(baseline.pages.map((p) => p.file));
  const onlyRendered = [...rSet].filter((f) => !bSet.has(f)).sort();
  const onlyBaseline = [...bSet].filter((f) => !rSet.has(f)).sort();
  const symDiff = onlyRendered.length + onlyBaseline.length;
  const common = [...rSet].filter((f) => bSet.has(f)).sort();
  const sample = (xs) => `${xs.slice(0, 5).join(', ')}${xs.length > 5 ? ', …' : ''}`;
  if (lostMode) {
    const created = `${onlyRendered.length} created${onlyRendered.length ? ` (${sample(onlyRendered)})` : ''}`;
    if (onlyBaseline.length <= lostBudget) {
      pass('page-set', `${onlyBaseline.length} lost <= budget ${lostBudget}; `
        + `${created}, uncapped; ${common.length} common`);
    } else {
      fail('page-set', `${onlyBaseline.length} lost > budget ${lostBudget}; `
        + `only-baseline: ${sample(onlyBaseline)}; ${created}`);
    }
  } else if (symDiff <= budget) {
    pass('page-set', `symmetric difference ${symDiff} <= budget ${budget} (${common.length} common)`);
  } else {
    fail('page-set', `symmetric difference ${symDiff} > budget ${budget}; `
      + `only-rendered: ${onlyRendered.slice(0, 5).join(', ')}${onlyRendered.length > 5 ? ', …' : ''}; `
      + `only-baseline: ${onlyBaseline.slice(0, 5).join(', ')}${onlyBaseline.length > 5 ? ', …' : ''}`);
  }

  // type-match over the non-delta set -------------------------------------
  const typeMismatch = [];
  for (const f of common) {
    const rt = rendered.byFile.get(f).frontmatter.type;
    const bt = baseline.byFile.get(f).frontmatter.type;
    if (rt !== bt) typeMismatch.push(`${f}: ${bt} -> ${rt}`);
  }
  if (typeMismatch.length) fail('type-match', typeMismatch.slice(0, 8).join('; '));
  else pass('type-match', `${common.length} common pages keep their type`);

  // frontmatter: required per type + structural equality -------------------
  const fmMissing = [];
  for (const pg of rendered.pages) {
    const type = pg.frontmatter.type;
    const spec = TYPE_FIELDS.types[type];
    if (!spec) {
      fmMissing.push(`${pg.file}: unknown type ${JSON.stringify(type)}`);
      continue;
    }
    let required = [...UNIVERSAL_REQUIRED, ...spec.required];
    if (type !== 'tour') required.push('source_paths');
    const missing = required.filter((k) => empty(pg.frontmatter[k]));
    if (type === 'endpoint' && empty(pg.frontmatter.route) && empty(pg.frontmatter.routes)) {
      missing.push('route|routes');
    }
    if (missing.length) fmMissing.push(`${pg.file}: ${missing.join(', ')}`);
  }
  const structuralMismatch = [];
  for (const f of common) {
    const r = rendered.byFile.get(f);
    const b = baseline.byFile.get(f);
    for (const key of TYPE_FIELDS.structural_fields) {
      if (key === 'aliases' && b.frontmatter.type !== 'endpoint') continue;
      const rv = key === 'aliases' ? JSON.stringify(r.aliases) : r.frontmatter[key];
      const bv = key === 'aliases' ? JSON.stringify(b.aliases) : b.frontmatter[key];
      if (bv !== undefined && rv !== bv) {
        structuralMismatch.push(`${f}: ${key} ${JSON.stringify(bv)} -> ${JSON.stringify(rv)}`);
      }
    }
  }
  if (fmMissing.length || structuralMismatch.length) {
    fail('frontmatter', [...fmMissing.slice(0, 6), ...structuralMismatch.slice(0, 6)].join('; '));
  } else {
    pass('frontmatter', 'required fields present; structural fields match baseline');
  }

  // full graph-check on both trees (links, source paths, blockers) ---------
  const date = new Date().toISOString().slice(0, 10);
  const rReport = checkGraph({ pagesDir: renderedDir, inventoryPath, repoPath, date });
  const bReport = checkGraph({ pagesDir: baselineDir, inventoryPath, repoPath, date });

  if (rReport.brokenLinks.length === 0) {
    pass('links-resolve', 'zero broken wikilinks in the rendered tree');
  } else {
    fail('links-resolve', `${rReport.brokenLinks.length} broken wikilink(s), e.g. `
      + rReport.brokenLinks.slice(0, 5).map((b) => `${b.page}: [[${b.target}]]`).join('; '));
  }

  // link recall over common pages ------------------------------------------
  const recallRegressions = [];
  for (const f of common) {
    const rCount = resolvedOutbound(rendered.byFile.get(f), rendered.index);
    const bCount = resolvedOutbound(baseline.byFile.get(f), baseline.index);
    if (rCount < bCount) recallRegressions.push(`${f}: ${bCount} -> ${rCount}`);
  }
  if (recallRegressions.length) {
    fail('link-recall', `${recallRegressions.length} page(s) regressed: `
      + recallRegressions.slice(0, 8).join('; '));
  } else {
    pass('link-recall', 'no page lost resolved outbound links vs baseline');
  }

  // source paths ------------------------------------------------------------
  if (repoPath && repoPath !== '-') {
    if (rReport.missingSourcePaths.length === 0) {
      pass('source-paths', 'every rendered source_paths entry exists in the clone');
    } else {
      fail('source-paths', rReport.missingSourcePaths.slice(0, 8)
        .map((m) => `${m.page}: ${m.path}`).join('; '));
    }
  } else {
    pass('source-paths', 'skipped (no repoPath)');
  }

  // blocker parity ----------------------------------------------------------
  const rBlockers = classify(rReport).blockers.length;
  const bBlockers = classify(bReport).blockers.length;
  if (rBlockers <= bBlockers) {
    pass('blocker-parity', `rendered ${rBlockers} <= baseline ${bBlockers}`);
  } else {
    fail('blocker-parity', `rendered ${rBlockers} > baseline ${bBlockers}: `
      + classify(rReport).blockers.slice(0, 5).map((b) => b.detail).join('; '));
  }

  // coverage parity (file-level, against the inventory file set) ------------
  if (inventoryPath && inventoryPath !== '-') {
    const inventory = JSON.parse(readFileSync(inventoryPath, 'utf8'));
    const invFiles = new Set((inventory.symbols || []).map((s) => s.file));
    const coveredFiles = (tree) => {
      const covered = new Set();
      for (const pg of tree.pages) {
        for (const sp of pg.source_paths) if (invFiles.has(sp)) covered.add(sp);
      }
      return covered;
    };
    const rCovered = coveredFiles(rendered);
    const lost = [...coveredFiles(baseline)].filter((f) => !rCovered.has(f)).sort();
    if (lost.length === 0) {
      pass('coverage-parity', `rendered covers ${rCovered.size} inventory file(s) ⊇ baseline`);
    } else {
      fail('coverage-parity', `${lost.length} baseline-covered file(s) lost: `
        + lost.slice(0, 8).join(', '));
    }
  } else {
    pass('coverage-parity', 'skipped (no inventory)');
  }

  return { results, ok: results.every((r) => r.ok) };
}

const isMain = process.argv[1] && process.argv[1].endsWith('golden-gate.js');
if (isMain) {
  const [renderedDir, baselineDir, configPath, repoPath, inventoryPath] = process.argv.slice(2);
  if (!renderedDir || !baselineDir || !configPath) {
    console.error('usage: node golden-gate.js <renderedDir> <baselineDir> <config.json> <repoPath|-> <inventoryPath|->');
    process.exit(2);
  }
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  const { results, ok } = runGoldenGate({
    renderedDir, baselineDir, config,
    repoPath: repoPath || '-', inventoryPath: inventoryPath || '-',
  });
  for (const r of results) {
    console.log(`${r.ok ? 'PASS' : 'FAIL'} ${r.id}: ${r.detail}`);
  }
  console.log(ok ? 'golden-gate: PASS' : 'golden-gate: FAIL');
  process.exit(ok ? 0 : 1);
}
