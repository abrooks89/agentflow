"""Local search: embed → vector_search seeds → bounded graph traversal →
rank → assemble provenance-carrying context (epic 705 scope 2, 4, 6).

Storage is reached ONLY through adapter primitives (``vector_search``,
``neighbors``, ``get_entity``, ``find_edges``) — no SQL, no backend import.

Traversal design note: expansion uses ``neighbors(node, depth=1)`` per
frontier node (exact parent tracking), then annotates each traversed pair
with its full edge rows via ``find_edges`` — because ``neighbors()`` reports
only the first edge that reached a node and carries no origin/confidence,
while the provenance contract (scope 6) requires every hop's
rel/origin/confidence and dreamed-edge handling requires knowing ALL
parallel edges between a pair. Between two entities the best edge wins:
non-dreamed before dreamed, then highest confidence, then rel name — a node
whose only route is a dreamed edge is excluded entirely unless
``include_dreamed=True``.

Every budget guard is loud: the fired cap is named in
``truncation_reasons`` and ``truncated`` is a visible property of the
result — never a silent trim.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from .tuning import (
    DEFAULT_DEPTH,
    DEFAULT_FANOUT_CAP,
    DEFAULT_K,
    DEFAULT_LOCAL_BUDGET_MS,
    DEFAULT_MAX_ENTITIES,
    DEPTH_CAP,
    DOC_CROSS_DECAY,
    HOP_DECAY,
    MAX_TIER_REFS_PER_ITEM,
)
from .types import ContextItem, PathHop, QueryError, QueryResult, RoutingDecision, TierRef

EXPAND_TIERS = ("both", "symbols_only", "concepts_only")


class Budget:
    """Wall-clock budget; exceeding it truncates loudly, never silently."""

    def __init__(self, budget_ms: float) -> None:
        self.budget_ms = budget_ms
        self._t0 = perf_counter()

    def elapsed_ms(self) -> float:
        return (perf_counter() - self._t0) * 1000.0

    def exceeded(self) -> bool:
        return self.elapsed_ms() > self.budget_ms


def validate_common(depth: int, expand_tiers: str) -> None:
    if depth > DEPTH_CAP or depth < 0:
        raise QueryError(
            f"depth={depth} is invalid: the hard cap is {DEPTH_CAP} "
            "(a validation error, not a clamp)"
        )
    if expand_tiers not in EXPAND_TIERS:
        raise QueryError(
            f"unknown expand_tiers {expand_tiers!r}: expected one of {EXPAND_TIERS}"
        )


class _Node:
    __slots__ = ("depth", "min_conf", "path", "row", "score", "seed_sim", "speculative")

    def __init__(self, *, row: dict[str, Any], score: float, depth: int,
                 path: list[PathHop], min_conf: float, speculative: bool,
                 seed_sim: float) -> None:
        self.row = row
        self.score = score
        self.depth = depth
        self.path = path
        self.min_conf = min_conf
        self.speculative = speculative
        self.seed_sim = seed_sim


def _best_edge(edges: list[dict[str, Any]], include_dreamed: bool) -> dict[str, Any] | None:
    """The winning parallel edge between a pair: non-dreamed first, then
    highest confidence, then rel name (deterministic)."""
    usable = edges if include_dreamed else [e for e in edges if e["origin"] != "dreamed"]
    if not usable:
        return None
    return min(usable, key=lambda e: (e["origin"] == "dreamed", -e["confidence"], e["rel"]))


def _edges_between(adapter, a: str, b: str) -> list[dict[str, Any]]:
    return (adapter.find_edges({"src_id": a, "dst_id": b})
            + adapter.find_edges({"src_id": b, "dst_id": a}))


def traverse(
    adapter,
    seeds: list[tuple[str, float]],
    *,
    depth: int,
    include_dreamed: bool,
    fanout_cap: int,
    budget: Budget,
    reasons: list[str],
) -> dict[str, _Node]:
    """Deterministic BFS from the seeds, ranking as it goes:
    ``score = seed_similarity * HOP_DECAY**depth * min_confidence``."""
    nodes: dict[str, _Node] = {}
    for eid, sim in seeds:
        row = adapter.get_entity(eid)
        if row is None:  # an embedding without its entity is store corruption
            raise QueryError(f"vector_search returned unknown entity {eid!r}")
        nodes[eid] = _Node(row=row, score=sim, depth=0, path=[], min_conf=1.0,
                           speculative=False, seed_sim=sim)

    frontier = [eid for eid, _ in seeds]
    for d in range(1, depth + 1):
        if not frontier:
            break
        next_frontier: list[str] = []
        for node_id in frontier:
            if budget.exceeded():
                reasons.append(
                    f"wall_clock_budget({budget.budget_ms:g}ms) exceeded at "
                    f"depth {d}; traversal stopped early"
                )
                return nodes
            parent = nodes[node_id]
            candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for n in adapter.neighbors(node_id, rels=[], depth=1):
                nid = n["entity"]["id"]
                if nid in nodes:
                    continue
                best = _best_edge(_edges_between(adapter, node_id, nid), include_dreamed)
                if best is None:
                    continue  # only dreamed routes reach it; excluded by default
                candidates.append((n, best))
            candidates.sort(key=lambda c: (-c[1]["confidence"], c[0]["entity"]["id"]))
            if len(candidates) > fanout_cap:
                reasons.append(
                    f"fanout_cap({fanout_cap}) dropped "
                    f"{len(candidates) - fanout_cap} neighbors of {node_id}"
                )
                candidates = candidates[:fanout_cap]
            for n, edge in candidates:
                nid = n["entity"]["id"]
                if nid in nodes:  # racing entries within one frontier pass
                    continue
                hop = PathHop(
                    src_id=edge["src_id"], dst_id=edge["dst_id"], rel=edge["rel"],
                    direction=n["direction"], origin=edge["origin"],
                    confidence=float(edge["confidence"]),
                )
                min_conf = min(parent.min_conf, hop.confidence)
                nodes[nid] = _Node(
                    row=n["entity"],
                    score=parent.seed_sim * (HOP_DECAY ** d) * min_conf,
                    depth=d,
                    path=[*parent.path, hop],
                    min_conf=min_conf,
                    speculative=parent.speculative or hop.origin == "dreamed",
                    seed_sim=parent.seed_sim,
                )
                next_frontier.append(nid)
        frontier = next_frontier
    return nodes


def _item_from_node(node: _Node) -> ContextItem:
    row = node.row
    return ContextItem(
        entity_id=row["id"],
        tier=row["tier"],
        kind=row["kind"],
        name=row["name"],
        source_paths=list(row.get("source_paths") or []),
        span=row.get("span"),
        origin=row["origin"],
        path=node.path,
        min_confidence=node.min_conf,
        score=node.score,
        depth=node.depth,
        summary=row.get("summary"),
        speculative=node.speculative,
    )


def _attach_capped_refs(item: ContextItem,
                        resolved: list[tuple[dict[str, Any], TierRef]]) -> None:
    """Fill the item's applicable tier-ref list, capped at
    MAX_TIER_REFS_PER_ITEM: a concept page documenting dozens of symbols
    otherwise repeats every ref on every item and dominates the output.
    Survivors are chosen like the best parallel edge — non-dreamed first,
    then highest confidence, then entity id — so the cut is deterministic
    and keeps the strongest facts."""
    ranked = sorted(resolved, key=lambda er: (er[0]["origin"] == "dreamed",
                                              -er[0]["confidence"],
                                              er[1].entity_id))
    target = item.documented_by if item.tier == "symbol" else item.documents
    target.extend(r for _e, r in ranked[:MAX_TIER_REFS_PER_ITEM])
    item.tier_refs_total = len(ranked)
    if len(ranked) > MAX_TIER_REFS_PER_ITEM:
        which = "documented_by" if item.tier == "symbol" else "documents"
        item.tier_refs_truncation = (
            f"tier_ref_cap({MAX_TIER_REFS_PER_ITEM}) kept "
            f"{MAX_TIER_REFS_PER_ITEM} of {len(ranked)} {which} refs"
        )


def tier_ref_reason(items: list[ContextItem]) -> str | None:
    """One result-level reason naming how many RETAINED items had their
    tier-ref list capped (per-item detail stays on the item), so the cap is
    visible in ``truncated`` without one line per hub."""
    capped = [i for i in items if i.tier_refs_truncation]
    if not capped:
        return None
    return (f"tier_ref_cap({MAX_TIER_REFS_PER_ITEM}) trimmed the "
            f"documents/documented_by refs of {len(capped)} items")


def cross_tiers(
    adapter,
    items: list[ContextItem],
    *,
    expand_tiers: str,
    entity_cache: dict[str, dict[str, Any] | None] | None = None,
) -> list[ContextItem]:
    """Scope 4: the ``documents`` tier join as an explicit API contract,
    applied OUTSIDE the depth budget to every retained item.

    Symbols get ``documented_by`` (explicitly ``[]`` when undocumented — a
    signal, not an omission); concepts get ``documents``. With
    ``expand_tiers='both'`` the cross-tier entities also join the result as
    full items, path-extended through the ``documents`` edge."""
    cache = entity_cache if entity_cache is not None else {}

    def entity(eid: str) -> dict[str, Any] | None:
        if eid not in cache:
            cache[eid] = adapter.get_entity(eid)
        return cache[eid]

    def ref(eid: str) -> TierRef | None:
        row = entity(eid)
        if row is None:
            return None
        return TierRef(entity_id=row["id"], name=row["name"], kind=row["kind"],
                       span=row.get("span"))

    present = {i.entity_id for i in items}
    extras: list[ContextItem] = []
    for item in items:
        if item.tier == "symbol":
            edges = adapter.find_edges({"dst_id": item.entity_id, "rel": "documents"})
            other_key = "src_id"
            item.documented_by = []
        else:
            edges = adapter.find_edges({"src_id": item.entity_id, "rel": "documents"})
            other_key = "dst_id"
            item.documents = []
        resolved = [(edge, r) for edge in edges
                    if (r := ref(edge[other_key])) is not None]
        _attach_capped_refs(item, resolved)
        for edge, _r in resolved:
            other_id = edge[other_key]
            if expand_tiers != "both" or other_id in present:
                continue
            row = entity(other_id)
            hop = PathHop(
                src_id=edge["src_id"], dst_id=edge["dst_id"], rel="documents",
                direction="in" if item.tier == "symbol" else "out",
                origin=edge["origin"], confidence=float(edge["confidence"]),
            )
            min_conf = min(item.min_confidence, hop.confidence)
            extras.append(ContextItem(
                entity_id=row["id"], tier=row["tier"], kind=row["kind"],
                name=row["name"],
                source_paths=list(row.get("source_paths") or []),
                span=row.get("span"), origin=row["origin"],
                path=[*item.path, hop],
                min_confidence=min_conf,
                score=item.score * DOC_CROSS_DECAY * hop.confidence,
                depth=item.depth,  # documents traversal is outside the depth budget
                summary=row.get("summary"),
                speculative=item.speculative,
            ))
            present.add(other_id)
    # the cross-tier items themselves need their own tier-join annotation
    # (a concept pulled in via a symbol must still list what it documents);
    # annotation mutates in place — the recursive return is deliberately
    # discarded because 'annotate_only' can produce no further items
    if extras:
        cross_tiers(adapter, extras, expand_tiers="annotate_only",
                    entity_cache=cache)
    return extras


def run_local(
    adapter,
    question: str,
    embed,
    *,
    decision: RoutingDecision,
    seeds: list[tuple[str, float]] | None = None,
    qvec: list[float] | None = None,
    k: int = DEFAULT_K,
    depth: int = DEFAULT_DEPTH,
    expand_tiers: str = "both",
    include_dreamed: bool = False,
    fanout_cap: int = DEFAULT_FANOUT_CAP,
    max_entities: int = DEFAULT_MAX_ENTITIES,
    budget_ms: float = DEFAULT_LOCAL_BUDGET_MS,
    priority_seeds: list[str] | None = None,
) -> QueryResult:
    """The local pipeline. ``seeds``/``qvec`` may be handed over from the
    router's default path so the question is never embedded twice.

    ``priority_seeds`` are entity ids the ROUTER resolved from the question
    text (adversarial finding 8): the asker literally named them, so they
    seed at similarity 1.0 ahead of the vector seeds — a weak embedding must
    never lose an explicitly named symbol."""
    budget = Budget(budget_ms)
    reasons: list[str] = []
    timings: dict[str, float] = {}

    if seeds is None:
        t0 = perf_counter()
        qvec = embed(question)
        timings["embed_ms"] = (perf_counter() - t0) * 1000.0
        seeds = adapter.vector_search(qvec, k)
    if priority_seeds:
        named = [(eid, 1.0) for eid in priority_seeds]
        named_ids = {eid for eid, _ in named}
        seeds = named + [s for s in seeds if s[0] not in named_ids]
    if not seeds:
        raise QueryError(
            "local search found no embedded entities to seed from — has the "
            "embedding pass run? (graph build --embed)"
        )

    t0 = perf_counter()
    nodes = traverse(adapter, seeds, depth=depth, include_dreamed=include_dreamed,
                     fanout_cap=fanout_cap, budget=budget, reasons=reasons)
    timings["traverse_ms"] = (perf_counter() - t0) * 1000.0

    ranked = sorted(nodes.values(), key=lambda n: (-n.score, n.row["id"]))
    if len(ranked) > max_entities:
        reasons.append(
            f"retained_entity_cap({max_entities}) trimmed "
            f"{len(ranked) - max_entities} lower-ranked entities"
        )
        ranked = ranked[:max_entities]
    items = [_item_from_node(n) for n in ranked]

    t0 = perf_counter()
    extras = cross_tiers(adapter, items, expand_tiers=expand_tiers)
    timings["tier_cross_ms"] = (perf_counter() - t0) * 1000.0
    items = sorted([*items, *extras], key=lambda i: (-i.score, i.entity_id))
    if len(items) > max_entities:
        reasons.append(
            f"retained_entity_cap({max_entities}) trimmed "
            f"{len(items) - max_entities} tier-crossed entities"
        )
        items = items[:max_entities]

    if expand_tiers == "symbols_only":
        items = [i for i in items if i.tier == "symbol"]
    elif expand_tiers == "concepts_only":
        items = [i for i in items if i.tier == "concept"]
    if (reason := tier_ref_reason(items)) is not None:
        reasons.append(reason)

    timings["total_ms"] = budget.elapsed_ms()
    return QueryResult(
        question=question,
        routing=decision,
        items=items,
        truncated=bool(reasons),
        truncation_reasons=reasons,
        timings_ms=timings,
    )
