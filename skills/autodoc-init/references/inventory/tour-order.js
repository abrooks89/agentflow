// tour-order.js — deterministic dependency ordering for autodoc guided tours.
//
// Convention: if page A's body links page B, then A DEPENDS ON B (B is the
// foundation and must come first). buildTourGraph turns a page set into a
// dependency graph (edge A->B == "A depends on B"); orderTour topologically
// sorts it bottom-up (Kahn's algorithm, dependencies before dependents),
// restricts to a seed's dependency closure when asked, and breaks cycles
// deterministically — always emitting every in-scope page exactly once.
//
// Pure Node stdlib. Reuses graph-check's parsePage/listPages (imported, not
// modified); the link/title/stem normalization below mirrors graph-check.normKey
// so resolution stays identical (graph-check does not export it).
import { parsePage, listPages } from './graph-check.js';

export { parsePage, listPages };

// Mirror of graph-check.normKey: lowercase, then collapse runs of
// space/hyphen/slash/underscore to a single space. Keeps `Create Project`,
// `create-project`, `create_project` unified — same contract graph-check uses.
function normKey(s) {
  return String(s)
    .toLowerCase()
    .replace(/[\s\-/_]+/g, ' ')
    .trim();
}

// `endpoints/create-project.md` -> `create-project`
function stemOf(file) {
  const base = String(file).split('/').pop();
  return base.replace(/\.md$/, '');
}

// buildTourGraph(pages) -> { nodes, edges }
//   pages: [{file, title, links}]
//   nodes: the input pages (one node each), preserving input order.
//   edges: directed dependency edges { from, to } meaning "from depends on to"
//          (i.e. `from` links `to`). Link targets resolve to pages via the
//          stem ∪ title normalization graph-check uses. Unresolved targets and
//          self-links are skipped. Duplicate edges are collapsed.
export function buildTourGraph(pages) {
  const nodes = pages.slice();

  // Resolution index: normalized stem OR title -> page. First writer wins so
  // resolution is stable for a given input order.
  const byKey = new Map();
  const addKey = (key, page) => {
    const k = normKey(key);
    if (!k || byKey.has(k)) return;
    byKey.set(k, page);
  };
  for (const pg of pages) {
    addKey(stemOf(pg.file), pg);
    if (pg.title) addKey(pg.title, pg);
  }
  const resolve = (target) => byKey.get(normKey(target)) || null;

  const edges = [];
  const seen = new Set();
  for (const pg of pages) {
    const from = labelOf(pg);
    for (const target of pg.links || []) {
      const dest = resolve(target);
      if (!dest || dest === pg) continue;
      const to = labelOf(dest);
      if (from === to) continue;
      const sig = `${from}\0${to}`;
      if (seen.has(sig)) continue;
      seen.add(sig);
      edges.push({ from, to });
    }
  }

  return { nodes, edges };
}

// A page's canonical label: its title when present, else its filename stem.
// Used as the stable node identity throughout ordering.
function labelOf(page) {
  return page.title || stemOf(page.file);
}

// orderTour(pages, {seedTitles}) -> [{file, title, reason}]
//   Topological order with DEPENDENCIES FIRST (Kahn's algorithm on the
//   dependency graph). When seedTitles is given, the result is restricted to
//   those pages plus their transitive dependency closure. Cycles are broken
//   deterministically (stable alphabetical by label among the remaining nodes
//   on each emit), and EVERY in-scope page is emitted EXACTLY once.
//
//   reason is a deterministic placeholder ("foundational" for a page with no
//   in-scope dependency, "builds on <prevTitle>" otherwise) — the Tours agent
//   rewrites the prose later.
export function orderTour(pages, { seedTitles } = {}) {
  const { nodes, edges } = buildTourGraph(pages);

  // Map label -> node (page). Labels are unique node identities; if two pages
  // share a label the first wins, matching buildTourGraph resolution.
  const nodeByLabel = new Map();
  for (const pg of nodes) {
    const label = labelOf(pg);
    if (!nodeByLabel.has(label)) nodeByLabel.set(label, pg);
  }

  // Adjacency: deps[X] = set of labels X depends on (X -> dep). Restricted to
  // labels that actually exist as nodes.
  const deps = new Map();
  for (const label of nodeByLabel.keys()) deps.set(label, new Set());
  for (const { from, to } of edges) {
    if (!nodeByLabel.has(from) || !nodeByLabel.has(to)) continue;
    deps.get(from).add(to);
  }

  // Determine the in-scope label set. With no seed, that's every node. With a
  // seed, it's the seed labels plus their transitive dependency closure.
  let scope;
  if (seedTitles && seedTitles.length) {
    scope = new Set();
    const seedLabels = seedTitles
      .map((t) => resolveSeedLabel(t, nodeByLabel))
      .filter(Boolean);
    const stack = [...seedLabels];
    while (stack.length) {
      const label = stack.pop();
      if (scope.has(label)) continue;
      scope.add(label);
      for (const dep of deps.get(label) || []) {
        if (!scope.has(dep)) stack.push(dep);
      }
    }
  } else {
    scope = new Set(nodeByLabel.keys());
  }

  // In-degree under the dependency graph, counting only in-scope deps. A node
  // is ready to emit when all its in-scope dependencies are already emitted, so
  // we order by remaining unsatisfied-dependency count (= out-degree in the
  // dependency graph). Emit count 0 first (foundational).
  const remainingDeps = new Map();
  for (const label of scope) {
    const set = new Set([...(deps.get(label) || [])].filter((d) => scope.has(d)));
    remainingDeps.set(label, set);
  }

  const emitted = [];
  const emittedSet = new Set();
  const placed = (label) => emittedSet.has(label);

  while (emitted.length < scope.size) {
    // Candidates: in-scope, not yet placed, with zero remaining deps.
    let candidates = [...scope].filter(
      (l) => !placed(l) && remainingDeps.get(l).size === 0,
    );
    // Cycle break: if nothing is ready, every remaining node still has an
    // unsatisfied dependency. Force-emit the alphabetically-first remaining
    // node so we make progress and never drop a page.
    if (candidates.length === 0) {
      candidates = [...scope].filter((l) => !placed(l));
    }
    candidates.sort(byLabelAsc);
    const next = candidates[0];

    const node = nodeByLabel.get(next);
    emitted.push({ file: node.file, title: node.title ?? labelOf(node), label: next });
    emittedSet.add(next);

    // Relax: drop `next` from every remaining node's outstanding dep set.
    for (const set of remainingDeps.values()) set.delete(next);
  }

  // Assign deterministic reason placeholders based on emitted position.
  return emitted.map((e, i) => ({
    file: e.file,
    title: e.title,
    reason: i === 0 ? 'foundational' : `builds on ${emitted[i - 1].title}`,
  }));
}

// Stable, case-insensitive-then-exact alphabetical compare on labels.
function byLabelAsc(a, b) {
  const na = normKey(a);
  const nb = normKey(b);
  if (na < nb) return -1;
  if (na > nb) return 1;
  return a < b ? -1 : a > b ? 1 : 0;
}

// Resolve a seed title (as a user would type it) to an existing node label via
// the same normalization. Tries label identity first, then any node whose
// normalized label matches.
function resolveSeedLabel(title, nodeByLabel) {
  if (nodeByLabel.has(title)) return title;
  const nt = normKey(title);
  for (const label of nodeByLabel.keys()) {
    if (normKey(label) === nt) return label;
  }
  return null;
}
