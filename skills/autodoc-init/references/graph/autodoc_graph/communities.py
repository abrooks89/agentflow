"""Community detection over the symbol graph (epic 703).

The derived replacement for the hand-written AREAS map: NetworkX Louvain over
the ``origin='ast'`` edges (``imports``/``calls``/``defines``/``routes_to``),
weighted by the spec's fixed table, plus an in-memory-only ``same_file``
cohesion edge. The partition, its condensation hierarchy, deterministic
labels, Jaccard id carry-forward, and the churn report all live here.

Storage-agnostic core: this module sees storage ONLY through the abstract
GraphAdapter interface (enforced by test — the fork thesis). The ``graph
communities`` CLI subcommand is the composition root.

Hard rules (spec):
* Edge weights are ONE config block, not a per-repo knob (``WEIGHTS``),
  summed over parallel edges between a pair.
* ``same_file`` edges (weight 0.5) exist in memory only and are NEVER
  persisted — 702's relation vocabulary is closed.
* Every symbol lands in exactly ONE community; an unassigned symbol is a
  hard ``CommunityError`` naming it, never a silent catch-all bucket.
* Determinism: nodes and edges are inserted in sorted id order and Louvain
  gets an explicit seed — identical input yields an identical partition.
* Fail loudly. Nothing here swallows a partition failure into a default.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import networkx as nx

# The spec's weight block — one config block, not a per-repo knob.
WEIGHTS: dict[str, float] = {
    "defines": 3.0,
    "routes_to": 3.0,
    "calls": 2.0,
    "imports": 1.0,
}
SAME_FILE_WEIGHT = 0.5  # in-memory only; never persisted (closed vocabulary)

DEFAULT_SEED = 42                 # explicit seed: Louvain determinism
DEFAULT_BAND = (8, 24)            # fan-out community-count band
DEFAULT_MIN_MEMBERS = 3           # singleton-tail merge threshold
DEFAULT_JACCARD_THRESHOLD = 0.5   # id carry-forward match threshold
DEFAULT_CHURN_PCT = 0.0           # report ANY membership change by default

# ≥60% of members must share a directory prefix for it to name the community.
PREFIX_MAJORITY = 0.6

_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")

# Stored labels are constrained to this charset (review finding 7): labels are
# derived from repo-controlled strings (paths, symbol names, LLM output) and
# flow into downstream agent prompts — a hostile repo must not be able to
# smuggle markup or control characters through them.
_LABEL_ALLOWED = re.compile(r"[^A-Za-z0-9 _.\-]+")


def sanitize_label(text: str) -> str:
    """Strip a candidate label to [A-Za-z0-9 _.-], collapse whitespace.
    Returns '' when nothing safe remains — callers decide the fallback
    (deterministic path: 'community'; LLM path: loud NamingError)."""
    cleaned = _LABEL_ALLOWED.sub(" ", str(text))
    return re.sub(r"\s+", " ", cleaned).strip()


def dedup_labels(labels: list[str], priority: list[bool] | None = None) -> list[str]:
    """Batch-scoped ordinal-suffix dedup (review finding 3). ``priority``
    marks labels that keep their exact name (pin overrides — operator intent
    wins); every other collision gets '-2', '-3', ... in list order.
    Re-applied after ANY label rewrite (pins, LLM rename), per level."""
    seen: Counter[str] = Counter()
    out: list[str | None] = [None] * len(labels)
    if priority:
        for i, lab in enumerate(labels):
            if priority[i]:
                seen[lab] += 1
                out[i] = lab
    for i, lab in enumerate(labels):
        if out[i] is not None:
            continue
        seen[lab] += 1
        out[i] = lab if seen[lab] == 1 else f"{lab}-{seen[lab]}"
    return [str(lab) for lab in out]


class CommunityError(RuntimeError):
    """Loud community-detection failure — never a silent default bucket."""


# --------------------------------------------------------------------- graph
def _path_of(entity: dict[str, Any]) -> str | None:
    span = entity.get("span") or {}
    if span.get("file"):
        return str(span["file"]).replace("\\", "/")
    paths = entity.get("source_paths") or []
    return str(paths[0]).replace("\\", "/") if paths else None


def build_symbol_graph(adapter) -> nx.Graph:
    """Undirected weighted graph over symbol-tier entities.

    Real edges carry ``WEIGHTS[rel]`` summed over parallel rels between a
    pair; symbols sharing a file additionally get ``SAME_FILE_WEIGHT`` so a
    file's symbols stay together and degree-0 symbols are not orphaned.
    Nodes and edges are inserted in sorted order (determinism).
    """
    symbols = adapter.find_entities({"tier": "symbol"})  # id-ordered
    g = nx.Graph()
    for e in symbols:
        g.add_node(e["id"])
    node_set = set(g)

    def bump(u: str, v: str, w: float) -> None:
        if g.has_edge(u, v):
            g[u][v]["weight"] += w
        else:
            g.add_edge(u, v, weight=w)

    for row in adapter.find_edges({"origin": "ast"}):  # (rel, src, dst)-ordered
        rel = row["rel"]
        if rel not in WEIGHTS:
            raise CommunityError(
                f"ast edge {row['src_id']} -> {row['dst_id']} has rel {rel!r} "
                f"outside the weight table {sorted(WEIGHTS)} — refusing to guess"
            )
        u, v = row["src_id"], row["dst_id"]
        if u == v:
            continue  # self-loops carry no partition information
        if u not in node_set or v not in node_set:
            raise CommunityError(
                f"ast edge endpoint is not a symbol entity: {u} -> {v} "
                "(graph integrity is loud, not patched over)"
            )
        bump(u, v, WEIGHTS[rel])

    by_file: dict[str, list[str]] = {}
    for e in symbols:
        path = _path_of(e)
        if path:
            by_file.setdefault(path, []).append(e["id"])
    for path in sorted(by_file):
        ids = sorted(by_file[path])
        for i, u in enumerate(ids):
            for v in ids[i + 1:]:
                bump(u, v, SAME_FILE_WEIGHT)
    return g


# ----------------------------------------------------------------- partition
def _sorted_partition(parts: list[set[str]]) -> list[set[str]]:
    return sorted((set(p) for p in parts if p), key=min)


def _louvain(g: nx.Graph, seed: int) -> list[set[str]]:
    if g.number_of_nodes() == 0:
        return []
    comms = nx.community.louvain_communities(g, weight="weight", seed=seed)
    return _sorted_partition([set(c) for c in comms])


def assert_complete_partition(node_ids: set[str], partition: list[set[str]]) -> None:
    """Every symbol in exactly one community — hard error naming violators."""
    seen: set[str] = set()
    for comm in partition:
        dup = seen & comm
        if dup:
            raise CommunityError(
                f"symbol(s) assigned twice: {sorted(dup)[:5]} — a partition "
                "must be disjoint"
            )
        seen |= comm
    missing = node_ids - seen
    if missing:
        raise CommunityError(
            f"{len(missing)} symbol(s) ended the run with NO community: "
            f"{sorted(missing)[:5]} — refusing to bucket silently"
        )
    extra = seen - node_ids
    if extra:
        raise CommunityError(
            f"partition names unknown symbol(s): {sorted(extra)[:5]}"
        )


def _weight_between(g: nx.Graph, a: set[str], b: set[str]) -> float:
    total = 0.0
    for u in a:
        for v in g.neighbors(u):
            if v in b:
                total += g[u][v]["weight"]
    return total


def merge_small_communities(
    g: nx.Graph, partition: list[set[str]], min_members: int
) -> list[set[str]]:
    """Merge communities below ``min_members`` into their strongest-weighted
    neighbour community. A small community with NO connected neighbour is
    KEPT (its members stay assigned — forcing them anywhere would be the
    'infra' catch-all this epic deletes)."""
    comms = _sorted_partition(partition)
    while True:
        merged = False
        smalls = sorted(
            (i for i, c in enumerate(comms) if len(c) < min_members),
            key=lambda i: (len(comms[i]), min(comms[i])),
        )
        for i in smalls:
            candidates = []
            for j, other in enumerate(comms):
                if j == i or not other or not comms[i]:
                    continue
                w = _weight_between(g, comms[i], other)
                if w > 0:
                    candidates.append((-w, min(other), j))
            if candidates:
                candidates.sort()
                comms[candidates[0][2]] |= comms[i]
                comms[i] = set()
                merged = True
        comms = [c for c in comms if c]
        if not merged:
            return _sorted_partition(comms)


def _condense(g: nx.Graph, partition: list[set[str]]) -> nx.Graph:
    """One node per community; inter-community edge weights summed."""
    cg = nx.Graph()
    cg.add_nodes_from(range(len(partition)))
    owner = {n: i for i, comm in enumerate(partition) for n in comm}
    agg: dict[tuple[int, int], float] = {}
    for u, v in sorted(g.edges()):
        cu, cv = owner[u], owner[v]
        if cu == cv:
            continue
        key = (min(cu, cv), max(cu, cv))
        agg[key] = agg.get(key, 0.0) + g[u][v]["weight"]
    for (cu, cv) in sorted(agg):
        cg.add_edge(cu, cv, weight=agg[(cu, cv)])
    return cg


def build_hierarchy(
    g: nx.Graph, level0: list[set[str]], seed: int, band_min: int
) -> list[list[set[str]]]:
    """Level 0 plus at least one parent level via condensation. Stops when the
    count stops dropping or falls below the fan-out band."""
    levels = [level0]
    current = level0
    while len(current) > 1:
        part = _louvain(_condense(g, current), seed)
        if len(part) >= len(current):
            break
        new_level = _sorted_partition(
            [set().union(*(current[i] for i in idxs)) for idxs in part]
        )
        levels.append(new_level)
        current = new_level
        if len(new_level) < band_min:
            break
    if len(levels) == 1:
        # Louvain could not drop the count (tiny corpora): the mandatory
        # parent level is a single root over everything — still a valid,
        # deterministic condensation.
        all_nodes = set().union(*level0) if level0 else set()
        levels.append([all_nodes] if all_nodes else [])
    return levels


# -------------------------------------------------------------------- labels
def _tokens(name: str) -> list[str]:
    return [
        t.lower() for t in _TOKEN_SPLIT.split(name)
        if t and len(t) >= 3 and not t.isdigit()
    ]


def _dominant_token(names: list[str], skip: set[str] | None = None) -> str | None:
    counts = Counter(t for n in names for t in _tokens(n) if not (skip and t in skip))
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _dir_prefixes(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _majority_prefix(paths: list[str]) -> str | None:
    """Longest directory prefix covering >= PREFIX_MAJORITY of members."""
    if not paths:
        return None
    coverage: Counter[str] = Counter()
    for p in paths:
        for pref in _dir_prefixes(p):
            coverage[pref] += 1
    eligible = [
        pref for pref, n in coverage.items() if n / len(paths) >= PREFIX_MAJORITY
    ]
    if not eligible:
        return None
    return sorted(eligible, key=lambda p: (-p.count("/"), -len(p), p))[0]


def derive_labels(
    partition: list[set[str]],
    entities_by_id: dict[str, dict[str, Any]],
    graph: nx.Graph | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Deterministic (label, descriptor) per community — the primary labels,
    durable BEFORE any optional LLM naming pass runs.

    Label: directory-prefix majority (longest prefix covering >=60% of
    members), disambiguated by dominant symbol-name tokens when two
    communities in the batch collide. Descriptor: member count, top-degree
    symbols, top three path prefixes.
    """
    interim: list[dict[str, Any]] = []
    for comm in partition:
        members = sorted(comm)
        ents = [entities_by_id[m] for m in members if m in entities_by_id]
        paths = [p for p in (_path_of(e) for e in ents) if p]
        names = [e.get("name") or "" for e in ents]
        prefix = _majority_prefix(paths)
        base = prefix.rsplit("/", 1)[-1] if prefix else None
        token = _dominant_token(names)
        if graph is not None:
            ranked = sorted(
                members,
                key=lambda m: (-graph.degree(m, weight="weight") if m in graph else 0.0, m),
            )
        else:
            ranked = members
        top_symbols = [
            (entities_by_id.get(m) or {}).get("name") or m for m in ranked[:3]
        ]
        dir_counts = Counter(p.rsplit("/", 1)[0] if "/" in p else "." for p in paths)
        top_prefixes = [
            d for d, _ in sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        ]
        # stored labels are charset-constrained (finding 7); an all-hostile
        # source string degrades to the neutral 'community' + ordinal suffix
        interim.append({
            "base": sanitize_label(base or token or "community") or "community",
            "token": sanitize_label(token) if token else None,
            "descriptor": {
                "member_count": len(members),
                "top_symbols": top_symbols,
                "top_prefixes": top_prefixes,
            },
        })
    # disambiguate label collisions within the batch (shared prefix case)
    base_counts = Counter(item["base"] for item in interim)
    labels: list[str] = []
    for item in interim:
        label = item["base"]
        if base_counts[label] > 1 and item["token"] and item["token"] != label:
            label = f"{label}-{item['token']}"
        labels.append(label)
    # any residual collision gets a deterministic ordinal suffix
    final = dedup_labels(labels)
    return [(lab, item["descriptor"]) for lab, item in zip(final, interim, strict=True)]


# ---------------------------------------------------------------------- pins
def apply_pins(
    partition: list[set[str]],
    entities_by_id: dict[str, dict[str, Any]],
    pins: dict[str, str],
) -> tuple[list[set[str]], dict[int, str], list[str]]:
    """The deliberately weak escape hatch: pin named path prefixes to a named
    community. No catch-all (an empty/root prefix hard-fails), never required,
    unlisted paths stay derived. Returns (partition, {index: pinned label},
    unmatched prefixes)."""
    norm_pins: dict[str, str] = {}
    for prefix, name in pins.items():
        clean = str(prefix).replace("\\", "/").strip("/")
        if not clean or clean == ".":
            raise CommunityError(
                f"pin prefix {prefix!r} would be a catch-all — the escape "
                "hatch pins NAMED path prefixes only"
            )
        if not name or not str(name).strip():
            raise CommunityError(f"pin for prefix {prefix!r} has an empty community name")
        safe_name = sanitize_label(name)
        if not safe_name:
            raise CommunityError(
                f"pin name {name!r} for prefix {prefix!r} has no safe "
                "characters (labels are constrained to [A-Za-z0-9 _.-])"
            )
        norm_pins[clean] = safe_name

    pinned: dict[str, set[str]] = {}
    matched: set[str] = set()
    moved: set[str] = set()
    prefixes = sorted(norm_pins, key=lambda p: (-len(p), p))  # longest wins
    for eid in sorted(entities_by_id):
        path = _path_of(entities_by_id[eid])
        if not path:
            continue
        for prefix in prefixes:
            if path == prefix or path.startswith(prefix + "/"):
                pinned.setdefault(norm_pins[prefix], set()).add(eid)
                matched.add(prefix)
                moved.add(eid)
                break

    remaining = [c - moved for c in partition]
    new_partition = [c for c in remaining if c]
    pin_labels: dict[int, str] = {}
    for name in sorted(pinned):
        pin_labels[len(new_partition)] = name
        new_partition.append(pinned[name])
    unmatched = [p for p in sorted(norm_pins) if p not in matched]
    return new_partition, pin_labels, unmatched


# ------------------------------------------------------------------- packing
# Review blocker 1 ruling: agent fan-out is DECOUPLED from the community
# count. Louvain communities stay exactly as built (ownership, labels,
# coverage, community_id — zero storage change); a deterministic packing
# step bins the packing level's WHOLE communities into N work units, N
# clamped into the band. The band is an agent-count/cost budget, not a
# partition property — a run completes with shipped defaults on any
# non-empty corpus (agentflow's connected-component floor is 81; a large
# host repo's is in the thousands; neither can ever reach 8-24 communities
# from graph evidence, and
# forcing them to would re-invent the deleted catch-all).
def select_packing_level(counts: list[int], band: tuple[int, int]) -> int:
    """The coarsest hierarchy level with at least band-min communities
    (packing wants whole communities to bin); level 0 when none qualifies
    (tiny corpora — the forced single root is useless to pack)."""
    lo, _hi = band
    for level in range(len(counts) - 1, -1, -1):
        if counts[level] >= lo:
            return level
    return 0


def select_packables(
    levels: list[list[set[str]]],
    pack_level: int,
    band: tuple[int, int],
) -> list[tuple[int, int]]:
    """The (level, index) communities to pack. Starts from the packing
    level; any community larger than the ideal unit share is recursively
    replaced by its WHOLE child communities (a 16k-symbol connected
    component packed as one unit would defeat the ruling's balance goal —
    descending a level unpacks it into finer whole communities without ever
    merging or splitting one). Deterministic order."""
    lo, hi = band
    top = levels[pack_level]
    total = sum(len(c) for c in top)
    top_count = len(top)
    if top_count == 0:
        return []
    n_target = min(top_count, max(lo, min(hi, top_count)))
    ideal = total / n_target
    queue: list[tuple[int, int]] = [(pack_level, i) for i in range(top_count)]
    out: list[tuple[int, int]] = []
    while queue:
        level, i = queue.pop(0)
        comm = levels[level][i]
        if len(comm) > ideal and level > 0:
            kids = [
                (level - 1, j)
                for j, child in enumerate(levels[level - 1])
                if child <= comm
            ]
            if kids:
                queue = kids + queue  # depth-first: children stay adjacent
                continue
        out.append((level, i))
    return out


def pack_units(
    partition: list[set[str]],
    entities_by_id: dict[str, dict[str, Any]],
    band: tuple[int, int],
) -> list[list[int]]:
    """Deterministically bin WHOLE communities into band-clamped work units.

    No community is merged and no membership changes — packing is pure work
    assignment, invisible to storage and to epics 704/705. Unit
    page-ownership stays disjoint because the constituent communities are.
    Sort by dominant directory prefix (locality) then size, then greedy-fill
    sequentially toward balanced symbol counts. Returns lists of community
    indexes into ``partition``; every community lands in exactly one unit
    and no unit is empty.
    """
    count = len(partition)
    if count == 0:
        return []
    lo, hi = band
    n_units = min(count, max(lo, min(hi, count)))
    sizes = [len(c) for c in partition]

    def dominant_prefix(i: int) -> str:
        paths = [
            p for p in (
                _path_of(entities_by_id[m])
                for m in partition[i] if m in entities_by_id
            ) if p
        ]
        dirs = Counter(p.rsplit("/", 1)[0] if "/" in p else "." for p in paths)
        if not dirs:
            return ""
        return sorted(dirs.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    order = sorted(
        range(count),
        key=lambda i: (dominant_prefix(i), -sizes[i], min(partition[i])),
    )
    units: list[list[int]] = []
    idx = 0
    remaining_symbols = sum(sizes)
    for u in range(n_units):
        units_left = n_units - u
        target = remaining_symbols / units_left
        unit = [order[idx]]
        acc = sizes[order[idx]]
        idx += 1
        # keep filling while enough communities remain to seed later units;
        # the last unit absorbs everything left
        while idx < count and (count - idx) > (units_left - 1):
            if u == n_units - 1 or acc + sizes[order[idx]] <= target:
                unit.append(order[idx])
                acc += sizes[order[idx]]
                idx += 1
            else:
                break
        remaining_symbols -= acc
        units.append(unit)
    while idx < count:  # belt-and-braces; the guard above should prevent this
        units[-1].append(order[idx])
        idx += 1
    return units


# ----------------------------------------------------------- carry-forward
def _match_partition(
    prev_rows: list[dict[str, Any]],
    partition: list[set[str]],
    threshold: float,
) -> tuple[list[tuple[int, float] | None], list[int]]:
    """Greedy best-match one-to-one Jaccard matching of new communities to the
    previous run's rows. Returns (per-community (prev_id, jaccard) | None,
    dissolved prev ids)."""
    pairs: list[tuple[float, int, int]] = []
    for row in prev_rows:
        prev_members = set(row["member_ids"] or [])
        if not prev_members:
            continue
        for i, comm in enumerate(partition):
            inter = len(prev_members & comm)
            if inter == 0:
                continue
            jac = inter / len(prev_members | comm)
            if jac >= threshold:
                pairs.append((jac, row["id"], i))
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    taken_prev: set[int] = set()
    taken_new: set[int] = set()
    match: list[tuple[int, float] | None] = [None] * len(partition)
    for jac, prev_id, i in pairs:
        if prev_id in taken_prev or i in taken_new:
            continue
        match[i] = (prev_id, jac)
        taken_prev.add(prev_id)
        taken_new.add(i)
    dissolved = [row["id"] for row in prev_rows if row["id"] not in taken_prev]
    return match, dissolved


def _previous_current_rows(adapter, symbols: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """The previous run's CURRENT hierarchy, level by level. Level 0 is the
    set of community ids entities actually point at; parents follow
    ``parent_id`` upward. Rows dissolved in EARLIER runs (never deleted —
    disappearance is reported, not pruned) are thereby excluded from
    matching forever."""
    current0 = {e["community_id"] for e in symbols if e.get("community_id") is not None}
    if not current0:
        return {}
    prev: dict[int, list[dict[str, Any]]] = {}
    level = 0
    wanted = current0
    while wanted:
        rows = [r for r in adapter.get_communities(level) if r["id"] in wanted]
        if not rows:
            break
        prev[level] = rows
        wanted = {r["parent_id"] for r in rows if r["parent_id"] is not None}
        level += 1
    return prev


# ----------------------------------------------------------------- run
def _resolve_db_ids(
    adapter, level: int, written: list[dict[str, Any]]
) -> list[int]:
    """DB ids for just-written rows, aligned with ``written``. Updates carry
    their id; inserts are matched back by exact member_ids (partitions are
    disjoint, so member sets are unique within a level). Stale rows from
    prior runs are skipped by preferring the newest matching id."""
    rows = adapter.get_communities(level)
    by_members: dict[tuple[str, ...], list[int]] = {}
    for r in rows:
        by_members.setdefault(tuple(r["member_ids"] or []), []).append(r["id"])
    claimed: set[int] = set()
    out: list[int] = []
    for w in written:
        if w["id"] is not None:
            claimed.add(w["id"])
            out.append(w["id"])
            continue
        candidates = [
            i for i in by_members.get(tuple(w["member_ids"]), []) if i not in claimed
        ]
        if not candidates:
            raise CommunityError(
                f"just-inserted community at level {level} could not be "
                "resolved back to a row id — store integrity is loud"
            )
        db_id = max(candidates)  # inserts get the newest rowids
        claimed.add(db_id)
        out.append(db_id)
    return out


def run_communities(
    adapter,
    *,
    seed: int = DEFAULT_SEED,
    band: tuple[int, int] = DEFAULT_BAND,
    min_members: int = DEFAULT_MIN_MEMBERS,
    pins: dict[str, str] | None = None,
    jaccard_threshold: float = DEFAULT_JACCARD_THRESHOLD,
    churn_pct: float = DEFAULT_CHURN_PCT,
) -> dict[str, Any]:
    """Detect, label, and persist the community hierarchy for the open corpus.

    Always completes on a non-empty corpus: the fan-out band is an
    agent-count/cost budget served by the packing step, never a partition
    property. Hard failures are reserved for an empty corpus, invalid band
    values, and partition-integrity violations. Carried-forward rows omit
    ``report``/``summarized_at_sha`` so the adapter's absent-key-preserves
    contract keeps 704's summaries intact across reruns.
    """
    symbols = adapter.find_entities({"tier": "symbol"})
    if not symbols:
        raise CommunityError(
            f"corpus {adapter.corpus_id!r} has no symbol entities — run "
            "`graph build` first"
        )
    ents = {e["id"]: e for e in symbols}

    g = build_symbol_graph(adapter)
    component_floor = nx.number_connected_components(g)
    lo, hi = band
    if lo < 1 or hi < lo:
        raise CommunityError(
            f"invalid --band {lo}:{hi} — need 1 <= LO <= HI. The band is the "
            "agent-count budget for packed work units, so any 1 <= LO <= HI "
            f"completes (smallest satisfiable configuration: --band 1:1). "
            f"FYI this corpus's connected-component floor is {component_floor} "
            "communities — graph evidence can never merge below that."
        )
    level0 = merge_small_communities(g, _louvain(g, seed), min_members)
    pin_labels: dict[int, str] = {}
    pins_unmatched: list[str] = []
    if pins:
        level0, pin_labels, pins_unmatched = apply_pins(level0, ents, pins)
    assert_complete_partition(set(ents), level0)

    levels = build_hierarchy(g, level0, seed, band_min=band[0])
    for part in levels:
        assert_complete_partition(set(ents), part)

    labeled: list[list[tuple[str, dict[str, Any]]]] = []
    for k, part in enumerate(levels):
        lab = derive_labels(part, ents, graph=g)
        if k == 0 and pin_labels:
            # pin overrides, then batch-scoped re-dedup (finding 3): the pin
            # keeps its exact configured name; a colliding derived label gets
            # the ordinal suffix.
            raw = [pin_labels.get(i, entry[0]) for i, entry in enumerate(lab)]
            priority = [i in pin_labels for i in range(len(lab))]
            deduped = dedup_labels(raw, priority)
            lab = [(deduped[i], entry[1]) for i, entry in enumerate(lab)]
        labeled.append(lab)

    prev = _previous_current_rows(adapter, symbols)

    # match + write, top level first so children can reference parent db ids
    matches: dict[int, list[tuple[int, float] | None]] = {}
    dissolved: dict[int, list[int]] = {}
    for k, part in enumerate(levels):
        matches[k], dissolved[k] = _match_partition(
            prev.get(k, []), part, jaccard_threshold
        )

    db_ids: dict[int, list[int]] = {}
    for k in range(len(levels) - 1, -1, -1):
        part = levels[k]
        parent_of: list[int | None] = [None] * len(part)
        if k + 1 < len(levels):
            for i, comm in enumerate(part):
                probe = min(comm)
                for j, parent_comm in enumerate(levels[k + 1]):
                    if probe in parent_comm:
                        if not comm <= parent_comm:
                            raise CommunityError(
                                f"hierarchy violation: level-{k} community {i} "
                                f"straddles level-{k + 1} parents"
                            )
                        parent_of[i] = db_ids[k + 1][j]
                        break
                if parent_of[i] is None:
                    raise CommunityError(
                        f"level-{k} community {i} has no level-{k + 1} parent"
                    )
        rows = []
        for i, comm in enumerate(part):
            label, descriptor = labeled[k][i]
            # report/summarized_at_sha keys are deliberately ABSENT: the
            # adapter preserves absent columns, so a carried-forward id keeps
            # 704's community report across reruns (review blocker 2).
            rows.append({
                "id": matches[k][i][0] if matches[k][i] else None,
                "level": k,
                "parent_id": parent_of[i],
                "label": label,
                "descriptor": descriptor,
                "member_ids": sorted(comm),
            })
        adapter.upsert_communities(rows)
        db_ids[k] = _resolve_db_ids(adapter, k, rows)

    # every symbol gets its level-0 community id (exactly one, by assertion)
    owner0: dict[str, int] = {}
    for i, comm in enumerate(levels[0]):
        for eid in comm:
            owner0[eid] = db_ids[0][i]
    adapter.upsert_entities([
        {**e, "community_id": owner0[e["id"]]} for e in symbols
    ])

    # Dissolution accounting (review finding 6b). The store is append-only,
    # so every non-current row that has not yet been marked gets a
    # `descriptor.dissolved` stamp NOW — including rows whose anchor
    # entities were pruned away (invisible to the entity-anchored matcher)
    # and rows at hierarchy levels the new run no longer has. The stamp
    # makes each dissolution reported exactly once and lets 704/705 filter.
    dissolved_now: dict[int, list[int]] = {}
    level = 0
    while True:
        stored = adapter.get_communities(level)
        if not stored:
            break
        current = set(db_ids.get(level, []))
        stamps = []
        for r in stored:
            if r["id"] in current:
                continue
            desc = dict(r.get("descriptor") or {})
            if desc.get("dissolved"):
                continue  # already reported by an earlier run
            desc["dissolved"] = True
            stamps.append({"id": r["id"], "level": r["level"], "descriptor": desc})
            dissolved_now.setdefault(level, []).append(r["id"])
        if stamps:
            adapter.upsert_communities(stamps)
        level += 1
    known_levels = sorted(set(range(len(levels))) | set(dissolved_now))

    churn: list[dict[str, Any]] = []
    for k in known_levels:
        part = levels[k] if k < len(levels) else []
        prev_by_id = {r["id"]: set(r["member_ids"] or []) for r in prev.get(k, [])}
        created = [db_ids[k][i] for i, m in enumerate(matches.get(k, [])) if m is None]
        changed = []
        for i, m in enumerate(matches.get(k, [])):
            if m is None:
                continue
            prev_members = prev_by_id[m[0]]
            now = set(part[i])
            if prev_members == now:
                continue
            change_ratio = 1.0 - m[1]
            if change_ratio > churn_pct:
                changed.append({
                    "id": m[0],
                    "jaccard": round(m[1], 4),
                    "added": len(now - prev_members),
                    "removed": len(prev_members - now),
                })
        churn.append({
            "level": k,
            "created": sorted(created),
            "dissolved": sorted(set(dissolved.get(k, [])) | set(dissolved_now.get(k, []))),
            "changed": changed,
        })

    counts = [len(part) for part in levels]
    pack_level = select_packing_level(counts, band)
    packables = select_packables(levels, pack_level, band)
    flat = [levels[lv][i] for lv, i in packables]
    packed = pack_units(flat, ents, band)
    units = []
    for u_idx, comm_idxs in enumerate(packed):
        communities = []
        for flat_i in comm_idxs:
            lv, i = packables[flat_i]
            label, descriptor = labeled[lv][i]
            communities.append({
                "id": db_ids[lv][i],
                "label": label,
                "descriptor": descriptor,
                "member_ids": sorted(levels[lv][i]),
            })
        first = communities[0]["label"]
        units.append({
            "key": f"unit-{u_idx + 1:02d}",
            "label": first if len(communities) == 1
            else f"{first} (+{len(communities) - 1} more)",
            "symbol_count": sum(len(c["member_ids"]) for c in communities),
            "communities": communities,
        })
    # Parent chain for 704's report roll-up: every ancestor of a fan-out
    # community gets one summarization agent reading ONLY its children's
    # reports. Children-first order == ascending level (a parent is always
    # at a strictly higher level than every child in its chain).
    fanout_ids = {c["id"] for u in units for c in u["communities"]}
    rows_by_id: dict[int, dict[str, Any]] = {}
    lvl = 0
    while True:
        stored = adapter.get_communities(lvl)
        if not stored:
            break
        for r in stored:
            rows_by_id[r["id"]] = r
        lvl += 1
    parent_children: dict[int, set[int]] = {}
    for cid in sorted(fanout_ids):
        child = cid
        row = rows_by_id.get(child)
        while row is not None and row["parent_id"] is not None:
            pid = row["parent_id"]
            if pid not in rows_by_id:
                raise CommunityError(
                    f"community {child} names parent {pid} which is not stored — "
                    "hierarchy integrity is loud"
                )
            parent_children.setdefault(pid, set()).add(child)
            child = pid
            row = rows_by_id[pid]
    parents = [
        {
            "id": pid,
            "level": rows_by_id[pid]["level"],
            "label": rows_by_id[pid]["label"],
            "children": sorted(kids),
        }
        for pid, kids in parent_children.items()
    ]
    parents.sort(key=lambda p: (p["level"], p["id"]))

    return {
        "corpus": adapter.corpus_id,
        "seed": seed,
        "band": list(band),
        "component_floor": component_floor,
        "levels": [
            {"level": k, "count": counts[k], "ids": db_ids[k]}
            for k in range(len(levels))
        ],
        "fanout_level": pack_level,
        "units": units,
        "parents": parents,
        "churn": churn,
        "pins_unmatched": pins_unmatched,
    }
