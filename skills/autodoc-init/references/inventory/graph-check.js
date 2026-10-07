// graph-check.js — deterministic graph-reviewer for the autodoc code-wiki.
// Task 1: page parser (parsePage) + page lister (listPages).
// Pure Node stdlib; reuses coverage.js for the symbol-coverage dimension (later tasks).
import { readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { checkCoverage } from './coverage.js';

// The target and label classes exclude '[' and newlines as well as ']' / '|'.
// Without those exclusions a stray '[[' in prose - a page documenting wikilink
// syntax, or a code span holding a regex like [[^\[\]] - opens a match that runs
// forward to the next ']]' many lines later, and this checker then reports the
// whole multi-line blob as ONE broken link on a page whose links are all fine.
// The renderer carried the identical defect (gh#432) and was MASKING this one:
// it destroyed the offending prose before the checker ever read it, so fixing
// the renderer is what made these false positives appear. `wiki.py`'s
// `_WIKILINK_RE` has always been written this way and states the same reason.
const WIKILINK_RE = /\[\[([^\]\[|\n]+)(?:\|[^\]\[\n]+)?\]\]/g;
const FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---/;
const NAV_PAGES = new Set(['_index.md', 'index.md', 'overview.md', 'log.md', 'hot.md', 'CLAUDE.md', 'Start Here.md', 'Glossary.md']);

// Universal frontmatter fields every content page must carry (taxonomy contract).
//
// `project` was added here in the SAME change that backfilled the 585 existing pages,
// never before it. The earlier note said requiring it first would turn every autodoc
// run RED; that was wrong and is corrected here - `missingFrontmatter` is classified
// as a WARNING, not a blocker, so the real cost was 585 advisory lines drowning the 3
// genuine gaps and the 24 broken links. Quieter than a red build and worse in one way:
// nobody would have looked.
const REQUIRED_FIELDS = ['type', 'title', 'created', 'updated', 'project', 'source_repo', 'source_paths', 'status'];

// Normalize a link target / stem / title for case-insensitive matching.
// Lowercase, then collapse spaces/hyphens/slashes/underscores to a single space.
// So `Create Project`, `create-project`, `create_project` all unify.
export function normKey(s) {
  return String(s)
    .toLowerCase()
    .replace(/[\s\-/_]+/g, ' ')
    .trim();
}

// `endpoints/create-project.md` -> `create-project`
function stemOf(file) {
  const base = file.split('/').pop();
  return base.replace(/\.md$/, '');
}

// [[Page#heading]] is valid Obsidian heading-link syntax: the link resolves
// against the page NAME only; the #fragment addresses a heading inside it.
// splitAnchor('SessionManager#check_budget') -> {name:'SessionManager', anchor:'#check_budget'}.
// A bare '#heading' (empty name) is a same-page anchor link — never broken.
export function splitAnchor(target) {
  const i = String(target).indexOf('#');
  if (i === -1) return { name: target, anchor: '' };
  return { name: target.slice(0, i).trim(), anchor: target.slice(i) };
}

// Endpoint contract links: request_model / response_model frontmatter values.
// Taxonomy: broken wikilinks are ADVISORY gap-markers EXCEPT these two fields —
// a dangling model link means the endpoint's contract is undocumented (blocker).
const MODEL_FIELDS = ['request_model', 'response_model'];

// Extract the wikilink target from a frontmatter value like "[[Project]]" or
// "[[Project|alias]]". Returns null for empty / non-link values.
function modelLinkTarget(v) {
  if (typeof v !== 'string') return null;
  // Same class as WIKILINK_RE above, and narrowed for the same reason.
  const m = v.match(/\[\[([^\]\[|\n]+)(?:\|[^\]\[\n]+)?\]\]/);
  return m ? m[1].trim() : null;
}

// Parse the leading YAML block into a flat object. Supports `key: value` and
// `- item` list entries (lists attach to the most recent key). Enough for the
// universal autodoc frontmatter fields (type/title/created/updated/source_repo/
// source_paths/status). Not a general YAML parser by design.
function parseFrontmatter(block) {
  const fm = {};
  let lastKey = null;
  for (const raw of block.split(/\r?\n/)) {
    const line = raw.replace(/\s+$/, '');
    if (!line.trim()) continue;
    const listMatch = line.match(/^\s*-\s+(.*)$/);
    if (listMatch && lastKey) {
      if (!Array.isArray(fm[lastKey])) fm[lastKey] = [];
      fm[lastKey].push(stripQuotes(listMatch[1].trim()));
      continue;
    }
    const idx = line.indexOf(':');
    if (idx === -1) continue;
    const key = line.slice(0, idx).trim();
    const value = line.slice(idx + 1).trim();
    if (value === '') {
      // value carried on following `- ` lines, or an empty scalar
      fm[key] = '';
      lastKey = key;
    } else {
      fm[key] = stripQuotes(value);
      lastKey = key;
    }
  }
  return fm;
}

function stripQuotes(s) {
  if (s.length >= 2 && ((s[0] === '"' && s.at(-1) === '"') || (s[0] === "'" && s.at(-1) === "'"))) {
    return s.slice(1, -1);
  }
  return s;
}

// Coerce a frontmatter value to a string list. Handles block-style lists
// (already arrays), single scalars, and inline flow arrays (`["a", "b"]`),
// which the line-based parser leaves as a raw string.
function toList(v) {
  if (Array.isArray(v)) return v;
  if (typeof v !== 'string' || !v.trim()) return [];
  const t = v.trim();
  if (t.startsWith('[') && t.endsWith(']')) {
    return t
      .slice(1, -1)
      .split(',')
      .map((x) => stripQuotes(x.trim()))
      .filter(Boolean);
  }
  return [t];
}

// parsePage(text) -> { frontmatter, links, source_paths, aliases, title }
export function parsePage(text) {
  const fmMatch = text.match(FRONTMATTER_RE);
  const frontmatter = fmMatch ? parseFrontmatter(fmMatch[1]) : {};

  const seen = new Set();
  const links = [];
  for (const m of text.matchAll(WIKILINK_RE)) {
    const target = m[1].trim();
    if (target && !seen.has(target)) {
      seen.add(target);
      links.push(target);
    }
  }

  const source_paths = toList(frontmatter.source_paths);
  const aliases = toList(frontmatter.aliases);

  return { frontmatter, links, source_paths, aliases, title: frontmatter.title };
}

// listPages(dir, {excludeNav=true}) -> [{file, abspath}]
// Recursive *.md under dir. file is the dir-relative POSIX path.
export function listPages(dir, { excludeNav = true } = {}) {
  const out = [];
  const walk = (current, relPrefix) => {
    for (const ent of readdirSync(current, { withFileTypes: true })) {
      if (ent.name.startsWith('.')) continue;
      const abspath = join(current, ent.name);
      const rel = relPrefix ? `${relPrefix}/${ent.name}` : ent.name;
      if (ent.isDirectory()) {
        walk(abspath, rel);
      } else if (ent.isFile() && ent.name.endsWith('.md')) {
        if (excludeNav && NAV_PAGES.has(ent.name)) continue;
        out.push({ file: rel, abspath });
      }
    }
  };
  walk(dir, '');
  return out;
}

// checkGraph({pagesDir, inventoryPath, repoPath, date}) -> integrity report.
// Resolves links against the UNION of {filename stems}, {frontmatter titles},
// and {frontmatter aliases} (Obsidian resolves [[Alias]] to the aliased page),
// normalized case-insensitively. Returns brokenLinks / orphans / missingFrontmatter
// / missingSourcePaths / coverage / summary.
export function checkGraph({ pagesDir, inventoryPath, repoPath, date } = {}) {
  // Parse every content (non-nav) page once (raw text kept for the diagram lint).
  const pages = listPages(pagesDir, { excludeNav: true }).map((p) => {
    const text = readFileSync(p.abspath, 'utf8');
    const parsed = parsePage(text);
    return { ...p, text, ...parsed };
  });

  // Resolution index: normalized stem, title, OR alias -> list of pages (the candidates).
  const byKey = buildKeyIndex(pages);

  const resolve = (target) => byKey.get(normKey(target)) || [];

  // brokenLinks: a link target that resolves to no page. candidates always [] here
  // (by definition unresolved); kept for the fixer's API shape. Single-candidate
  // case-insensitive matches DO resolve, so they are not broken. Resolution
  // strips any #heading anchor first ([[Page#h]] resolves to Page). Each entry
  // carries `class`: 'blocker' when the broken target is an endpoint page's
  // request_model/response_model link (contract undocumented), else 'warning'
  // (advisory gap-marker per taxonomy).
  const brokenLinks = [];
  for (const pg of pages) {
    // normalized targets of this page's endpoint model-contract fields
    const modelKeys = new Set();
    if (pg.frontmatter.type === 'endpoint') {
      for (const f of MODEL_FIELDS) {
        const t = modelLinkTarget(pg.frontmatter[f]);
        if (t) modelKeys.add(normKey(splitAnchor(t).name));
      }
    }
    for (const target of pg.links) {
      const { name } = splitAnchor(target);
      if (!name) continue; // [[#heading]] — same-page anchor, never broken
      if (resolve(name).length === 0) {
        const cls = modelKeys.has(normKey(name)) ? 'blocker' : 'warning';
        brokenLinks.push({ page: pg.file, target, candidates: [], class: cls });
      }
    }
  }

  // orphans: non-nav pages with zero inbound links from other content pages
  // (nav/index links do not count). A page linking to itself is not its own inbound.
  // Tour pages are top-level onboarding entry points reached FROM the nav layer
  // (index/hot/overview); no content page links down into a tour, so they are
  // orphan-by-design and exempt — matching their source_paths exemption above.
  const inbound = new Map(pages.map((p) => [p.file, 0]));
  for (const pg of pages) {
    for (const target of pg.links) {
      for (const dest of resolve(splitAnchor(target).name)) {
        if (dest.file !== pg.file) inbound.set(dest.file, inbound.get(dest.file) + 1);
      }
    }
  }
  const orphans = pages
    .filter((p) => inbound.get(p.file) === 0 && p.frontmatter.type !== 'tour')
    .map((p) => p.file);

  // missingFrontmatter: any universal required field absent/empty.
  // Tours have no source of their own (exempt source_paths). Meta docs
  // (type: meta — e.g. a Review Backlog or Skill-Hardening note) document no
  // code entity at all, so they carry neither source_repo nor source_paths.
  const missingFrontmatter = [];
  for (const pg of pages) {
    let required = REQUIRED_FIELDS;
    if (pg.frontmatter.type === 'tour') required = REQUIRED_FIELDS.filter((f) => f !== 'source_paths');
    else if (pg.frontmatter.type === 'meta') required = REQUIRED_FIELDS.filter((f) => f !== 'source_paths' && f !== 'source_repo');
    const missing = required.filter((f) => {
      const v = pg.frontmatter[f];
      if (v === undefined || v === null || v === '') return true;
      if (Array.isArray(v) && v.length === 0) return true;
      return false;
    });
    if (missing.length) missingFrontmatter.push({ page: pg.file, missing });
  }

  // missingSourcePaths: a page source_paths entry not present under repoPath.
  // Skipped entirely when repoPath is falsy or '-'. A deprecated page is exempt: it
  // documents code that was removed, its stale callout says so, and the record
  // contract still requires it to name what it documented.
  const missingSourcePaths = [];
  if (repoPath && repoPath !== '-') {
    for (const pg of pages) {
      if (pg.frontmatter.status === 'deprecated') continue;
      for (const sp of pg.source_paths) {
        if (!existsSync(join(repoPath, sp))) {
          missingSourcePaths.push({ page: pg.file, path: sp });
        }
      }
    }
  }

  // --- new check classes (A1–A8) ---------------------------------------------

  // Nav pages are excluded from the content resolution index, but a frontmatter
  // link pointing at one (e.g. related: "[[Glossary]]") DOES resolve in Obsidian
  // — resolve A2 links against content keys ∪ nav-page stems.
  const navKeys = new Set([...NAV_PAGES].map((n) => normKey(n.replace(/\.md$/, ''))));

  // A2 — frontmatter relational-link resolution (BLOCKER class).
  // Endpoint request_model/response_model stay on the existing brokenModelLink
  // blocker path (no double count); every other relational field is checked here.
  const frontmatterLinkUnresolved = [];
  for (const pg of pages) {
    for (const { field, target } of relationalLinks(pg)) {
      if (pg.frontmatter.type === 'endpoint' && MODEL_FIELDS.includes(field)) continue;
      const { name } = splitAnchor(target);
      if (!name) continue;
      if (resolve(name).length === 0 && !navKeys.has(normKey(name))) {
        frontmatterLinkUnresolved.push({ page: pg.file, field, target, class: 'blocker' });
      }
    }
  }

  // A1 — contract_with symmetry (WARN, safe-fixable). P lists Q ⇒ Q lists P back.
  const contractAsymmetry = [];
  for (const pg of pages) {
    for (const v of linkList(pg.frontmatter.contract_with)) {
      const t = modelLinkTarget(v);
      if (!t) continue;
      const cands = resolve(splitAnchor(t).name);
      if (cands.length !== 1) continue; // unresolved → A2; ambiguous → don't guess
      const other = cands[0];
      if (other.file === pg.file) continue; // self-link → A4
      const back = linkList(other.frontmatter.contract_with).map(modelLinkTarget).filter(Boolean);
      const listsBack = back.some((bt) =>
        resolve(splitAnchor(bt).name).some((c) => c.file === pg.file),
      );
      if (!listsBack) {
        contractAsymmetry.push({
          page: other.file,
          missingTarget: pg.title || stemOf(pg.file),
          linkedFrom: pg.file,
        });
      }
    }
  }

  // A3 — routes/aliases parity on route-family endpoint pages (WARN, safe-fixable).
  const aliasParity = [];
  for (const pg of pages) {
    if (pg.frontmatter.type !== 'endpoint' || pg.frontmatter.method !== 'multiple') continue;
    for (const r of toList(pg.frontmatter.routes)) {
      // A route is covered by an exact alias OR its `(service)`-suffixed form.
      if (!aliasCovers(pg.aliases, r)) aliasParity.push({ page: pg.file, route: r });
    }
  }

  // A4 — self-link: any wikilink (frontmatter or body) resolving to its own page (WARN).
  const selfLink = [];
  for (const pg of pages) {
    for (const target of pg.links) {
      const { name } = splitAnchor(target);
      if (!name) continue;
      if (resolve(name).some((c) => c === pg)) selfLink.push({ page: pg.file, target });
    }
  }

  // A5 — sanitized title (contains filename-invalid chars) must carry an alias
  // equal to the full title (WARN, safe-fixable).
  const titleAliasMissing = [];
  for (const pg of pages) {
    if (!pg.title || !INVALID_TITLE_CHARS.test(pg.title)) continue;
    // The full title, or its `(service)`-suffixed form on a cross-service
    // collision, satisfies the sanitized-title alias requirement.
    if (!aliasCovers(pg.aliases, pg.title)) {
      titleAliasMissing.push({ page: pg.file, title: pg.title });
    }
  }

  // A9 — alias collision: the same alias string on 2+ distinct pages (WARN).
  // Aliases are the wiki's global resolution keys; a shared alias makes a
  // [[link]] ambiguous. Cross-service METHOD/route collisions disambiguate with
  // a "(service)" suffix on BOTH sides (taxonomy endpoint-alias rule).
  const aliasCollision = [];
  const aliasOwners = new Map(); // alias string -> Set of page files
  for (const pg of pages) {
    for (const a of pg.aliases) {
      const key = String(a).trim();
      if (!key) continue;
      if (!aliasOwners.has(key)) aliasOwners.set(key, new Set());
      aliasOwners.get(key).add(pg.file);
    }
  }
  for (const [alias, owners] of aliasOwners) {
    if (owners.size > 1) aliasCollision.push({ alias, pages: [...owners].sort() });
  }

  // A6 — diagram grounding lint over ```mermaid fences.
  const diagramSourceMissing = []; // (a) BLOCKER — %% source: path not in the clone
  const diagramCommentOutsideFence = []; // (b) WARN — %% line just after a closing fence
  const diagramDuplicate = []; // (c) WARN — identical fence content on 2+ pages
  const diagramOverBudget = []; // (d) WARN — >20 nodes/entities in one fence
  const fenceMap = new Map(); // normalized fence content -> Set(pages)
  for (const pg of pages) {
    for (const fence of extractMermaidFences(pg.text)) {
      if (repoPath && repoPath !== '-') {
        for (const line of fence.content.split('\n')) {
          const m = line.match(/^\s*%%\s*source:\s*(.+?)\s*$/);
          if (m && !existsSync(join(repoPath, m[1]))) {
            diagramSourceMissing.push({ page: pg.file, path: m[1], class: 'blocker' });
          }
        }
      }
      fence.after.forEach((l, idx) => {
        if (/^\s*%%/.test(l)) {
          diagramCommentOutsideFence.push({ page: pg.file, line: fence.closeLine + 1 + idx });
        }
      });
      if (fence.content.trim()) {
        if (!fenceMap.has(fence.content)) fenceMap.set(fence.content, new Set());
        fenceMap.get(fence.content).add(pg.file);
      }
      const count = countDiagramNodes(fence.content);
      if (count > 20) diagramOverBudget.push({ page: pg.file, count });
    }
  }
  for (const [, files] of fenceMap) {
    if (files.size > 1) diagramDuplicate.push({ pages: [...files] });
  }

  // A7 — field-format validation (WARN, no autofix).
  const fieldFormat = [];
  for (const pg of pages) {
    const t = pg.frontmatter.type;
    const fmv = (k) => (typeof pg.frontmatter[k] === 'string' ? pg.frontmatter[k].trim() : '');
    if (t === 'endpoint' || t === 'job') {
      const h = fmv('handler');
      // Valid: module:entrypoint (incl. module:router for families, qualified
      // names, Go receiver methods) OR a bare CI workflow file for CI-triggered jobs.
      if (h && !HANDLER_RE.test(h) && !WORKFLOW_HANDLER_RE.test(h)) {
        fieldFormat.push({ page: pg.file, field: 'handler', value: h });
      }
    }
    if (t === 'decision') {
      const d = fmv('date');
      if (d && !/^\d{4}-\d{2}-\d{2}$/.test(d)) fieldFormat.push({ page: pg.file, field: 'date', value: d });
    }
    if (t === 'service') {
      const e = fmv('entrypoint');
      if (e && /\s/.test(e)) fieldFormat.push({ page: pg.file, field: 'entrypoint', value: e });
    }
    if (t === 'vendor') {
      const v = fmv('version');
      if (v && v.length > 80) fieldFormat.push({ page: pg.file, field: 'version', value: v });
    }
  }

  // A8 — exposes_endpoints reciprocity (WARN, editorial — no autofix).
  const exposesAsymmetry = [];
  for (const pg of pages) {
    if (pg.frontmatter.type !== 'endpoint') continue;
    const svcT = modelLinkTarget(pg.frontmatter.service);
    if (!svcT) continue;
    const svcCands = resolve(splitAnchor(svcT).name).filter((c) => c.frontmatter.type === 'service');
    if (!svcCands.length) continue;
    const listed = svcCands.some((svc) =>
      linkList(svc.frontmatter.exposes_endpoints)
        .map(modelLinkTarget)
        .filter(Boolean)
        .some((et) => resolve(splitAnchor(et).name).some((c) => c.file === pg.file)),
    );
    if (!listed) exposesAsymmetry.push({ page: pg.file, service: svcCands[0].file });
  }

  // coverage: delegate to coverage.js when an inventory is provided + readable.
  // The wiki carries no per-symbol covered/excluded record — its only coverage
  // evidence is file-level (page source_paths). fileLevelOnly suppresses the
  // symbol-level `uncovered` signal (which would otherwise flag EVERY symbol
  // and drown the blocker gate); fileGaps remains the coverage signal here.
  let coverage;
  if (inventoryPath && inventoryPath !== '-' && existsSync(inventoryPath)) {
    const inventory = JSON.parse(readFileSync(inventoryPath, 'utf8'));
    const covReport = {
      pages: pages.map((p) => ({ source_paths: p.source_paths })),
      covered: [],
      excluded: [],
    };
    coverage = checkCoverage(inventory, covReport, { fileLevelOnly: true });
  } else {
    coverage = { skipped: true };
  }

  const covGaps = coverage.skipped ? 0 : (coverage.fileGaps?.length || 0);
  const linkBlockers = brokenLinks.filter((b) => b.class === 'blocker').length;
  const summary =
    `links: ${brokenLinks.length} broken (${linkBlockers} blocker, ${brokenLinks.length - linkBlockers} warning), ` +
    `${orphans.length} orphan(s), ` +
    `${missingFrontmatter.length} frontmatter gap(s), ${missingSourcePaths.length} missing source path(s), ` +
    `coverage: ${coverage.skipped ? 'skipped' : `${covGaps} gap(s)`}, ` +
    `fm-links: ${frontmatterLinkUnresolved.length} unresolved, contract: ${contractAsymmetry.length} asymmetric, ` +
    `alias-parity: ${aliasParity.length}, self-links: ${selfLink.length}, title-alias: ${titleAliasMissing.length}, ` +
    `diagrams: ${diagramSourceMissing.length} missing-source/${diagramCommentOutsideFence.length} stray-comment/` +
    `${diagramDuplicate.length} duplicate/${diagramOverBudget.length} over-budget, ` +
    `field-format: ${fieldFormat.length}, exposes: ${exposesAsymmetry.length} asymmetric, ` +
    `alias-collision: ${aliasCollision.length}`;

  // Additive report shape: the original six keys keep their meaning; the A1–A8
  // classes ride alongside so existing consumers don't break.
  return {
    brokenLinks,
    orphans,
    missingFrontmatter,
    missingSourcePaths,
    coverage,
    summary,
    frontmatterLinkUnresolved,
    contractAsymmetry,
    aliasParity,
    selfLink,
    titleAliasMissing,
    diagramSourceMissing,
    diagramCommentOutsideFence,
    diagramDuplicate,
    diagramOverBudget,
    fieldFormat,
    exposesAsymmetry,
    aliasCollision,
  };
}

// --- A1–A8 helpers -----------------------------------------------------------

// Frontmatter fields whose wikilink entries are machine-consumed and must
// resolve (taxonomy "Cross-Linking Discipline"). `entrypoint` is included only
// on flow pages (service entrypoint: is a file path, not a link).
const RELATIONAL_FIELDS = [
  'calls_endpoints', 'renders', 'used_by', 'consumed_by', 'involves',
  'exposes_endpoints', 'runs', 'request_model', 'response_model', 'steps',
  'covers', 'contract_with', 'related', 'applies_to', 'affects', 'touches',
  'depends_on', 'service', 'owned_by',
];

// Filename-invalid chars whose presence in a title requires a full-title alias
// (taxonomy "Sanitized titles require an alias"): : / \ # | ^ [ ]
const INVALID_TITLE_CHARS = /[:/\\#|^[\]]/;

// endpoint/job handler: module-or-file path, colon, then an entrypoint name.
// The entrypoint may be a plain function (`fn`), a qualified name
// (`Class.method`, `lifespan._reaper_loop`), a Go receiver method
// (`(*BranchGC).sweep`), or the mounted router object of a route family
// (`:router` — the module:router convention for method:multiple pages).
const HANDLER_RE = /^[\w/.\-]+:[\w.()*]+$/;
// A CI-triggered job's "handler" is its workflow file (no function entrypoint).
const WORKFLOW_HANDLER_RE = /\.ya?ml$/;

// An alias `X (service)` disambiguates a cross-service route collision and
// counts as covering the bare form `X` (taxonomy endpoint-alias rule); an exact
// `X` alias also covers it. Used by aliasParity (route coverage) and
// titleAliasMissing (sanitized-title coverage) so the `(service)` suffix
// convention does not read as a missing alias.
function aliasCovers(aliases, bare) {
  const b = String(bare).trim();
  return (aliases || []).some((a) => {
    const s = String(a).trim();
    return s === b || s.startsWith(`${b} (`);
  });
}

// Coerce a frontmatter value to a list WITHOUT mangling scalar wikilinks:
// toList() would treat a bare `"[[X]]"` scalar as an inline flow array
// (starts with `[`, ends with `]`) and strip its brackets. A value starting
// with `[[` is always a single wikilink scalar.
function linkList(v) {
  if (typeof v === 'string' && v.trim().startsWith('[[')) return [v.trim()];
  return toList(v);
}

// All wikilink entries of a page's relational frontmatter fields, as
// [{field, target}]. Non-wikilink values (paths, empty strings) are skipped.
function relationalLinks(pg) {
  const fields = pg.frontmatter.type === 'flow' ? [...RELATIONAL_FIELDS, 'entrypoint'] : RELATIONAL_FIELDS;
  const out = [];
  for (const f of fields) {
    if (!(f in pg.frontmatter)) continue;
    for (const v of linkList(pg.frontmatter[f])) {
      const t = modelLinkTarget(v);
      if (t) out.push({ field: f, target: t });
    }
  }
  return out;
}

// Extract ```mermaid fences from page text (CRLF-tolerant). Each fence:
// {content, closeLine (1-based line of the closing ```), after: next 3 lines}.
function extractMermaidFences(text) {
  const lines = text.split(/\r?\n/);
  const fences = [];
  let i = 0;
  while (i < lines.length) {
    if (/^\s*```mermaid\s*$/.test(lines[i])) {
      i++;
      const buf = [];
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        buf.push(lines[i]);
        i++;
      }
      fences.push({
        content: buf.join('\n'),
        closeLine: i + 1,
        after: lines.slice(i + 1, i + 4),
      });
    }
    i++;
  }
  return fences;
}

// Heuristic node/entity count for the >20-element diagram budget, per diagram
// type: erDiagram entity blocks `X {`; sequenceDiagram participant/actor
// declarations; flowchart/graph unique node ids (shape declarations + edge ids).
const FLOWCHART_KEYWORDS = new Set([
  'subgraph', 'end', 'direction', 'flowchart', 'graph', 'classDef', 'class',
  'style', 'linkStyle', 'click', 'TB', 'TD', 'LR', 'RL', 'BT',
]);
function countDiagramNodes(content) {
  const lines = content
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('%%'));
  const header = lines[0] || '';
  if (/^erDiagram/.test(header)) {
    return lines.filter((l) => /^[A-Za-z_][\w-]*\s*\{/.test(l)).length;
  }
  if (/^sequenceDiagram/.test(header)) {
    const ids = new Set();
    for (const l of lines) {
      const m = l.match(/^(?:participant|actor)\s+([\w-]+)/);
      if (m) ids.add(m[1]);
    }
    return ids.size;
  }
  if (/^(?:flowchart|graph)\b/.test(header)) {
    const ids = new Set();
    for (const l of lines.slice(1)) {
      for (const m of l.matchAll(/(?:^|[\s>|])([A-Za-z_][\w-]*)\s*(?=[[({>])/g)) ids.add(m[1]);
      for (const m of l.matchAll(/(?:^|\s)([A-Za-z_][\w-]*)\s*(?=--|==|-\.)/g)) ids.add(m[1]);
      for (const m of l.matchAll(/(?:-->|---|==>|-\.->)\s*(?:\|[^|]*\|\s*)?([A-Za-z_][\w-]*)/g)) ids.add(m[1]);
    }
    for (const kw of FLOWCHART_KEYWORDS) ids.delete(kw);
    return ids.size;
  }
  return 0; // unknown diagram type: no budget heuristic
}

// Normalize a "METHOD path" string for the A2 endpoint-link autofix: method
// lowercased, every non-alphanumeric char stripped from the path part.
// Returns null when the string is not METHOD-path shaped.
function epNormalize(s) {
  const m = String(s).trim().match(/^(GET|POST|PUT|PATCH|DELETE|WS) (.+)$/);
  if (!m) return null;
  return `${m[1].toLowerCase()} ${m[2].replace(/[^a-zA-Z0-9]/g, '').toLowerCase()}`;
}

// Append one item to a frontmatter list field, preserving corpus YAML style
// (block lists with `- ` items; the item string is passed pre-quoted).
// Creates the key when absent; converts a scalar value to a block list;
// appends inside an inline flow array. Returns {text, added}.
function appendToFrontmatterList(text, key, item) {
  const m = text.match(FRONTMATTER_RE);
  if (!m) return { text, added: false };
  const eol = m[0].includes('\r\n') ? '\r\n' : '\n';
  const lines = m[1].split(/\r?\n/);
  const keyRe = new RegExp(`^${key}:(.*)$`);
  let keyIdx = -1;
  let inlineVal = '';
  for (let i = 0; i < lines.length; i++) {
    const km = lines[i].match(keyRe);
    if (km) {
      keyIdx = i;
      inlineVal = km[1].trim();
      break;
    }
  }
  let newLines;
  if (keyIdx === -1) {
    newLines = [...lines, `${key}:`, `  - ${item}`];
  } else if (inlineVal === '') {
    // block list: insert after the last consecutive `- ` item (same indent)
    let end = keyIdx;
    let indent = '  ';
    for (let i = keyIdx + 1; i < lines.length; i++) {
      const im = lines[i].match(/^(\s*)-\s+/);
      if (!im) break;
      end = i;
      indent = im[1];
    }
    newLines = [...lines.slice(0, end + 1), `${indent}- ${item}`, ...lines.slice(end + 1)];
  } else if (inlineVal.startsWith('[') && inlineVal.endsWith(']')) {
    const inner = inlineVal.slice(1, -1).trim();
    const joined = inner ? `${inner}, ${item}` : item;
    newLines = lines.map((l, i) => (i === keyIdx ? `${key}: [${joined}]` : l));
  } else {
    // scalar -> convert to a block list keeping the existing value first
    newLines = [
      ...lines.slice(0, keyIdx),
      `${key}:`,
      `  - ${inlineVal}`,
      `  - ${item}`,
      ...lines.slice(keyIdx + 1),
    ];
  }
  const newText = text.replace(FRONTMATTER_RE, () => `---${eol}${newLines.join(eol)}${eol}---`);
  return { text: newText, added: true };
}

// --- Task 3: safe auto-fix ---------------------------------------------------

// Frontmatter fields autoFix is allowed to default. Everything else
// (type/title/created/source_repo/source_paths) stays flagged, never fabricated.
const SAFE_DEFAULT_FIELDS = ['status', 'updated'];

// Bounded edit distance: returns true iff the two strings are within `max`
// single-character insert/delete/substitute operations. Short-circuits on a
// length gap > max. Conservative by design — autoFix only uses max=1.
function withinEditDistance(a, b, max) {
  const la = a.length;
  const lb = b.length;
  if (Math.abs(la - lb) > max) return false;
  // classic DP row, but we only care about <= max, so we can early-bail
  let prev = Array.from({ length: lb + 1 }, (_, j) => j);
  for (let i = 1; i <= la; i++) {
    const cur = [i];
    let rowMin = i;
    for (let j = 1; j <= lb; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      const v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
      cur[j] = v;
      if (v < rowMin) rowMin = v;
    }
    if (rowMin > max) return false; // whole row already exceeds budget
    prev = cur;
  }
  return prev[lb] <= max;
}

// Build the normKey -> [pages] resolution index (stems ∪ titles ∪ aliases) for a
// page set — the same key set Obsidian resolves wikilinks against.
export function buildKeyIndex(pages) {
  const byKey = new Map();
  const add = (key, page) => {
    const k = normKey(key);
    if (!k) return;
    if (!byKey.has(k)) byKey.set(k, []);
    const arr = byKey.get(k);
    if (!arr.includes(page)) arr.push(page);
  };
  for (const pg of pages) {
    add(stemOf(pg.file), pg);
    if (pg.title) add(pg.title, pg);
    for (const a of pg.aliases || []) add(a, pg);
  }
  return byKey;
}

// Conservative fuzzy candidate discovery for a broken link target.
// Returns the unique set of page stems whose normalized key is edit-distance <= 1
// from the normalized target. A target that EXACTLY resolves is not broken and
// never reaches here. Returns an array of {page, stem} candidates.
function fuzzyCandidates(target, byKey, pages) {
  const nt = normKey(target);
  if (!nt) return [];
  const hitPages = new Set();
  for (const [key, keyPages] of byKey) {
    if (key === nt) continue; // exact match isn't "broken"; skip
    if (withinEditDistance(nt, key, 1)) {
      for (const p of keyPages) hitPages.add(p);
    }
  }
  return [...hitPages].map((p) => ({ page: p, stem: stemOf(p.file) }));
}

// Rewrite a single wikilink target -> newStem in `text`, preserving any |alias.
// Matches the exact bracketed token to avoid clobbering substrings.
function rewriteLink(text, oldTarget, newStem) {
  // [[oldTarget]] or [[oldTarget|alias]] (oldTarget may contain spaces)
  const esc = oldTarget.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(`\\[\\[${esc}(\\|[^\\]]*)?\\]\\]`, 'g');
  return text.replace(re, (_m, alias) => `[[${newStem}${alias || ''}]]`);
}

// Inject safe-default frontmatter fields into a page's YAML block. Only adds
// fields in `defaults` that are absent/empty; never overwrites existing values.
// If the page has no frontmatter block, creates a minimal one with the defaults.
function addFrontmatterDefaults(text, defaults) {
  const m = text.match(FRONTMATTER_RE);
  const added = [];
  if (!m) {
    const lines = Object.entries(defaults).map(([k, v]) => `${k}: ${v}`);
    for (const k of Object.keys(defaults)) added.push(k);
    return { text: `---\n${lines.join('\n')}\n---\n${text}`, added };
  }
  const existing = parseFrontmatter(m[1]);
  const toAdd = [];
  for (const [k, v] of Object.entries(defaults)) {
    const cur = existing[k];
    const empty = cur === undefined || cur === null || cur === '' || (Array.isArray(cur) && cur.length === 0);
    if (empty) {
      toAdd.push(`${k}: ${v}`);
      added.push(k);
    }
  }
  if (!toAdd.length) return { text, added };
  // append new fields to the end of the existing frontmatter block
  const newBlock = `${m[1]}\n${toAdd.join('\n')}`;
  const newText = text.replace(FRONTMATTER_RE, `---\n${newBlock}\n---`);
  return { text: newText, added };
}

// autoFix(report, {pagesDir, date}) -> { fixed, residual }
// Applies ONLY the six safe fix classes below, each only when exactly one answer
// exists, then re-runs checkGraph for residual.
// (a) single-candidate (fuzzy, edit-distance<=1) broken link -> rewrite in-file.
// (b) missing universal frontmatter field -> add status:active / updated:<date>.
// (c) unresolved endpoint-form frontmatter link matching exactly one endpoint
//     page -> rewrite to that page's slash-form alias (its title when none).
// (d) one-way contract_with pair -> add the reciprocal entry on the other side.
// (e) route-family alias gap -> add the slash-form alias from the routes entry.
// (f) sanitized title with no alias -> add the full title as an alias.
// Never touches: ambiguous links (>1 candidate), 0-candidate links, missing
// pages, missingSourcePaths, orphans, or any non-safe frontmatter field.
//
// dryRun: compute `fixed` (what WOULD be repaired) without writing any file,
// and return the input report as the residual. Used by --fail-on-fixable on a
// rendered tree, where a page repair would be undone by the next render from
// the store (gh#694): the finding must be named, not applied.
export function autoFix(report, { pagesDir, date, repoPath, inventoryPath, dryRun = false } = {}) {
  const fixed = [];
  const write = dryRun ? () => {} : writeFileSync;

  // Re-parse the current page set to build the fuzzy resolution index.
  const pages = listPages(pagesDir, { excludeNav: true }).map((p) => {
    const parsed = parsePage(readFileSync(p.abspath, 'utf8'));
    return { ...p, ...parsed };
  });
  const byKey = buildKeyIndex(pages);
  const abspathOf = new Map(pages.map((p) => [p.file, p.abspath]));

  // Fix (a): single-candidate broken links. Group broken links by page so we
  // read/write each file once. Fuzzy matching runs on the anchor-stripped page
  // name ([[Page#h]] resolves to Page); any #heading anchor is preserved on
  // rewrite.
  const linkFixesByPage = new Map();
  const rewritten = new Set(); // "page target" pairs already fixed by (a)
  for (const bl of report.brokenLinks || []) {
    const { name, anchor } = splitAnchor(bl.target);
    if (!name) continue; // same-page anchor link, nothing to resolve
    const cands = fuzzyCandidates(name, byKey, pages);
    if (cands.length !== 1) continue; // 0 or >1 -> leave broken (flag, don't fix)
    // Rewrite to the candidate page's title when it has one (matches the
    // Title-Case wikilink style of generated pages); else its filename stem.
    const c = cands[0];
    const newTarget = (c.page.title || c.stem) + anchor;
    if (!linkFixesByPage.has(bl.page)) linkFixesByPage.set(bl.page, []);
    linkFixesByPage.get(bl.page).push({ target: bl.target, stem: newTarget });
    rewritten.add(`${bl.page} ${bl.target}`);
  }
  for (const [page, fixes] of linkFixesByPage) {
    const abspath = abspathOf.get(page);
    if (!abspath) continue;
    let text = readFileSync(abspath, 'utf8');
    for (const { target, stem } of fixes) {
      text = rewriteLink(text, target, stem);
      fixed.push({ page, kind: 'link', detail: `[[${target}]] -> [[${stem}]]` });
    }
    write(abspath, text);
  }

  // Fix (b): missing universal frontmatter -> safe defaults only.
  const defaults = { status: 'active', updated: date };
  for (const mf of report.missingFrontmatter || []) {
    // only act if at least one safe field is among the missing set
    const safeMissing = (mf.missing || []).filter((f) => SAFE_DEFAULT_FIELDS.includes(f));
    if (!safeMissing.length) continue;
    const abspath = abspathOf.get(mf.page);
    if (!abspath) continue;
    const safeDefaults = {};
    for (const f of safeMissing) safeDefaults[f] = defaults[f];
    const { text, added } = addFrontmatterDefaults(readFileSync(abspath, 'utf8'), safeDefaults);
    if (added.length) {
      write(abspath, text);
      fixed.push({ page: mf.page, kind: 'frontmatter', detail: `added ${added.join(', ')}` });
    }
  }

  // Fix (c): A2 dangling endpoint-form frontmatter links. A "METHOD path"
  // target is normalized (method lowercased, path stripped to alphanumerics)
  // and compared against every endpoint page's aliases + title normalized the
  // same way. EXACTLY ONE matching page -> rewrite to its slash-form alias;
  // zero or >1 -> leave flagged (never guess).
  const epMap = new Map(); // norm -> Map(pageFile -> alias/title string)
  for (const pg of pages) {
    if (pg.frontmatter.type !== 'endpoint') continue;
    const cands = [...(pg.aliases || [])];
    if (pg.title) cands.push(pg.title); // aliases first: slash-form alias preferred
    for (const c of cands) {
      const n = epNormalize(c);
      if (!n) continue;
      if (!epMap.has(n)) epMap.set(n, new Map());
      const perPage = epMap.get(n);
      if (!perPage.has(pg.file)) perPage.set(pg.file, c);
    }
  }
  const epFixesByPage = new Map();
  const epFixSeen = new Set();
  for (const e of report.frontmatterLinkUnresolved || []) {
    const pairKey = `${e.page} ${e.target}`;
    if (rewritten.has(pairKey) || epFixSeen.has(pairKey)) continue;
    const n = epNormalize(e.target);
    if (!n) continue;
    const perPage = epMap.get(n);
    if (!perPage || perPage.size !== 1) continue; // 0 or ambiguous -> report, no guess
    const alias = [...perPage.values()][0];
    if (!epFixesByPage.has(e.page)) epFixesByPage.set(e.page, []);
    epFixesByPage.get(e.page).push({ target: e.target, alias });
    epFixSeen.add(pairKey);
  }
  for (const [page, fixes] of epFixesByPage) {
    const abspath = abspathOf.get(page);
    if (!abspath) continue;
    let text = readFileSync(abspath, 'utf8');
    for (const { target, alias } of fixes) {
      text = rewriteLink(text, target, alias);
      fixed.push({ page, kind: 'frontmatterLink', detail: `[[${target}]] -> [[${alias}]]` });
    }
    write(abspath, text);
  }

  // Fix (d): one-way contract_with pair -> append the reciprocal entry on the
  // other side (creating the list when absent).
  const cwSeen = new Set();
  for (const e of report.contractAsymmetry || []) {
    const dedupe = `${e.page} ${e.missingTarget}`;
    if (cwSeen.has(dedupe)) continue;
    cwSeen.add(dedupe);
    const abspath = abspathOf.get(e.page);
    if (!abspath) continue;
    const { text, added } = appendToFrontmatterList(
      readFileSync(abspath, 'utf8'),
      'contract_with',
      `"[[${e.missingTarget}]]"`,
    );
    if (added) {
      write(abspath, text);
      fixed.push({ page: e.page, kind: 'contractWith', detail: `added "[[${e.missingTarget}]]"` });
    }
  }

  // Fix (e): route-family alias gap -> append the slash-form alias verbatim
  // from the routes entry.
  for (const e of report.aliasParity || []) {
    const abspath = abspathOf.get(e.page);
    if (!abspath) continue;
    const { text, added } = appendToFrontmatterList(readFileSync(abspath, 'utf8'), 'aliases', `"${e.route}"`);
    if (added) {
      write(abspath, text);
      fixed.push({ page: e.page, kind: 'aliasParity', detail: `added alias "${e.route}"` });
    }
  }

  // Fix (f): sanitized title with no alias -> add the full title as an alias.
  for (const e of report.titleAliasMissing || []) {
    const abspath = abspathOf.get(e.page);
    if (!abspath) continue;
    const { text, added } = appendToFrontmatterList(readFileSync(abspath, 'utf8'), 'aliases', `"${e.title}"`);
    if (added) {
      write(abspath, text);
      fixed.push({ page: e.page, kind: 'titleAlias', detail: `added alias "${e.title}"` });
    }
  }

  // residual: a fresh checkGraph reflecting on-disk state after the fixes.
  // repoPath/inventoryPath are threaded through so the residual keeps the same
  // dimensions (source_paths existence, coverage) the caller checked originally.
  const residual = dryRun ? report : checkGraph({ pagesDir, date, repoPath, inventoryPath });

  return { fixed, residual };
}

// --- Task 4: blocker classification + CLI ------------------------------------

// classify(report) -> { blockers, warnings }
// Each entry is a small {kind, detail} object.
//
// blockers (fail the run): genuinely unresolvable problems (taxonomy: blockers
// = nonexistent source_paths + unexplained coverage gaps + dangling endpoint
// contract links) —
//   - coverageGap       : a symbol whose file no page documents (coverage.fileGaps),
//                         plus any symbol-level uncovered entries when present.
//   - brokenModelLink   : a broken link that is the value of an endpoint page's
//                         request_model/response_model frontmatter (class: 'blocker'
//                         on the report entry) — the endpoint contract is undocumented.
//   - missingSourcePath : a page source_paths entry that does not exist in the repo.
//   - frontmatterLinkUnresolved : a frontmatter relational link that does not resolve.
//   - diagramSourceMissing      : a diagram `%% source:` path absent from the clone.
// warnings (reported, non-fatal): advisory gap-markers / style —
//   - brokenLink           : a plain broken link (advisory gap-marker per taxonomy;
//                            entries without a `class` field — older reports —
//                            default here). checkGraph records no candidates, so
//                            every non-model broken link lands here.
//   - brokenLinkCandidate  : a broken link whose report entry carries candidates;
//                            checkGraph never produces one.
//   - orphan               : a page with no inbound links.
//   - missingFrontmatter   : a page still missing a required field (unfixed).
//   - contractAsymmetry    : a page missing the reciprocal contract_with link.
//   - aliasParity          : a route with no matching alias.
//   - aliasCollision       : one alias shared by several pages.
//   - selfLink             : a wikilink that resolves to its own page.
//   - titleAliasMissing    : a title with no full-title alias.
//   - diagramCommentOutsideFence : a `%%` comment outside a mermaid fence.
//   - diagramDuplicate     : an identical mermaid fence on several pages.
//   - diagramOverBudget    : a diagram over the node/entity budget.
//   - fieldFormat          : a frontmatter field whose value is malformed.
//   - exposesAsymmetry     : an endpoint missing from its service's exposes_endpoints.
export function classify(report = {}) {
  const blockers = [];
  const warnings = [];

  const cov = report.coverage || {};
  if (!cov.skipped) {
    for (const s of cov.fileGaps || []) {
      blockers.push({ kind: 'coverageGap', detail: `undocumented file: ${s.file}` });
    }
    for (const s of cov.uncovered || []) {
      blockers.push({
        kind: 'coverageGap',
        detail: `uncovered symbol: ${s.name || '?'} (${s.file})`,
      });
    }
  }

  for (const bl of report.brokenLinks || []) {
    if (bl.class === 'blocker') {
      blockers.push({
        kind: 'brokenModelLink',
        detail: `${bl.page}: [[${bl.target}]] (endpoint request/response model link must resolve)`,
      });
    } else if ((bl.candidates || []).length > 0) {
      warnings.push({
        kind: 'brokenLinkCandidate',
        detail: `${bl.page}: [[${bl.target}]] (${bl.candidates.length} candidate(s))`,
      });
    } else {
      warnings.push({
        kind: 'brokenLink',
        detail: `${bl.page}: [[${bl.target}]] (no candidates — advisory gap-marker)`,
      });
    }
  }

  for (const ms of report.missingSourcePaths || []) {
    blockers.push({ kind: 'missingSourcePath', detail: `${ms.page}: ${ms.path}` });
  }

  for (const o of report.orphans || []) {
    warnings.push({ kind: 'orphan', detail: o });
  }

  for (const mf of report.missingFrontmatter || []) {
    warnings.push({ kind: 'missingFrontmatter', detail: `${mf.page}: ${(mf.missing || []).join(', ')}` });
  }

  // A2 blocker: frontmatter relational links are machine-consumed, never advisory.
  for (const e of report.frontmatterLinkUnresolved || []) {
    blockers.push({
      kind: 'frontmatterLinkUnresolved',
      detail: `${e.page}: ${e.field}: [[${e.target}]] (frontmatter relational link must resolve)`,
    });
  }

  // A6(a) blocker: a diagram grounding path that doesn't exist in the clone —
  // same severity as a nonexistent source_paths entry.
  for (const e of report.diagramSourceMissing || []) {
    blockers.push({ kind: 'diagramSourceMissing', detail: `${e.page}: %% source: ${e.path}` });
  }

  // Remaining new classes are warnings: reported, never fatal.
  for (const e of report.contractAsymmetry || []) {
    warnings.push({
      kind: 'contractAsymmetry',
      detail: `${e.page}: missing contract_with "[[${e.missingTarget}]]" (listed by ${e.linkedFrom})`,
    });
  }
  for (const e of report.aliasParity || []) {
    warnings.push({ kind: 'aliasParity', detail: `${e.page}: route "${e.route}" has no matching alias` });
  }
  for (const e of report.aliasCollision || []) {
    warnings.push({ kind: 'aliasCollision', detail: `alias "${e.alias}" shared by ${(e.pages || []).join(', ')} — disambiguate with (service) suffix` });
  }
  for (const e of report.selfLink || []) {
    warnings.push({ kind: 'selfLink', detail: `${e.page}: [[${e.target}]] resolves to itself` });
  }
  for (const e of report.titleAliasMissing || []) {
    warnings.push({ kind: 'titleAliasMissing', detail: `${e.page}: title "${e.title}" needs a full-title alias` });
  }
  for (const e of report.diagramCommentOutsideFence || []) {
    warnings.push({ kind: 'diagramCommentOutsideFence', detail: `${e.page}: %% comment outside fence near line ${e.line}` });
  }
  for (const e of report.diagramDuplicate || []) {
    warnings.push({ kind: 'diagramDuplicate', detail: `identical mermaid fence on: ${(e.pages || []).join(', ')}` });
  }
  for (const e of report.diagramOverBudget || []) {
    warnings.push({ kind: 'diagramOverBudget', detail: `${e.page}: ${e.count} nodes/entities (>20 budget)` });
  }
  for (const e of report.fieldFormat || []) {
    warnings.push({
      kind: 'fieldFormat',
      detail: `${e.page}: ${e.field}: "${String(e.value).slice(0, 60)}"`,
    });
  }
  for (const e of report.exposesAsymmetry || []) {
    warnings.push({ kind: 'exposesAsymmetry', detail: `${e.page}: not listed in ${e.service} exposes_endpoints` });
  }

  return { blockers, warnings };
}

// CLI: node graph-check.js <pagesDir> <inventoryPath|-> <repoPath|-> <outPath> [--fix | --fail-on-fixable]
// Runs checkGraph; with --fix, applies safe auto-fixes and uses the residual.
// With --fail-on-fixable (read-only; mutually exclusive with --fix), computes
// what --fix WOULD repair without writing any file, records it in the report
// as `fixable: [{page, kind, detail}]`, names each page on stderr, and fails.
// That mode is for a RENDERED tree: a page there is regenerated from the graph
// store, so a repair written into the page is lost on the next render and must
// be made in the records instead (gh#694).
// Writes the (residual) report JSON to outPath, prints a one-line summary to
// stderr, and exits 1 iff classify(report).blockers is non-empty (or, with
// --fail-on-fixable, any fixable finding exists), else 0.
// '-' for inventory skips coverage; '-' for repo skips source-path existence.
const isMain = process.argv[1] && process.argv[1].endsWith('graph-check.js');
if (isMain) {
  const FLAGS = ['--fix', '--fail-on-fixable'];
  const USAGE = 'usage: node graph-check.js <pagesDir> <inventoryPath|-> <repoPath|-> <outPath> [--fix | --fail-on-fixable]';
  const args = process.argv.slice(2);
  const fix = args.includes('--fix');
  const failOnFixable = args.includes('--fail-on-fixable');
  const [pagesDir, inventoryPath, repoPath, outPath] = args.filter((a) => !FLAGS.includes(a));
  if (!pagesDir || !inventoryPath || !repoPath || !outPath) {
    console.error(USAGE);
    process.exit(2);
  }
  if (fix && failOnFixable) {
    console.error('graph-check: --fix and --fail-on-fixable are mutually exclusive (one writes pages, the other must not)');
    console.error(USAGE);
    process.exit(2);
  }
  const date = new Date().toISOString().slice(0, 10);
  const opts = { pagesDir, inventoryPath, repoPath, date };

  let report = checkGraph(opts);
  let fixedCount = 0;
  let fixable = [];
  if (fix) {
    const result = autoFix(report, opts);
    fixedCount = result.fixed.length;
    report = result.residual;
  } else if (failOnFixable) {
    fixable = autoFix(report, { ...opts, dryRun: true }).fixed;
    report = { ...report, fixable };
  }

  writeFileSync(outPath, JSON.stringify(report, null, 2));

  const { blockers, warnings } = classify(report);
  let note = '';
  if (fix) note = ` (${fixedCount} auto-fixed)`;
  else if (failOnFixable) {
    const fixablePages = [...new Set(fixable.map((f) => f.page))].sort();
    note = `, ${fixable.length} fixable on ${fixablePages.length} page(s)`;
    for (const f of fixable) {
      console.error(`graph-check: fixable (not applied): ${f.page}: ${f.kind}: ${f.detail}`);
    }
  }
  console.error(
    `graph-check: ${blockers.length} blocker(s), ${warnings.length} warning(s)${note} \u2014 ${report.summary} \u2192 ${outPath}`,
  );
  process.exit(blockers.length > 0 || fixable.length > 0 ? 1 : 0);
}
