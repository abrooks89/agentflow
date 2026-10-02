"""Global search: map → filter → synthesize over community reports
(epic 705 scope 3).

The community listing is APPEND-ONLY (epic 703): ``get_communities(level)``
mixes current and historical rows, so the LIVE hierarchy is derived here the
same way 703/704 do it — anchored on ``entity.community_id`` (level 0) and
``parent_id`` chains upward, with ``descriptor.dissolved`` rows excluded as
belt-and-braces. Raw listings are never trusted.

Map: reports batched deterministically (id-sorted, fixed batch size) and
scored for relevance in parallel. Filter: keep scores >= the floor, top-N.
Synthesize: the kept reports' ranked findings resolve to entities with
community provenance — the caller's model writes the prose (out of scope
here). Level selection starts at the highest live level and descends only
when the kept set is empty.

Honesty contract (success criterion 8): every scored community and its score
ships in ``communities_scored`` with a ``kept`` flag, so a wrong answer is
debuggable to the report that caused it.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from typing import Any

from autodoc_graph.vocab import AGENT_DEFAULT_CONFIDENCE

from .local import Budget, cross_tiers
from .tuning import (
    DEFAULT_GLOBAL_BUDGET_MS,
    DEFAULT_MAX_ENTITIES,
    MAX_KEPT_REPORTS,
    RELEVANCE_FLOOR,
    SCORE_BATCH_SIZE,
    SCORE_PARALLELISM,
)
from .types import ContextItem, QueryError, QueryResult, RoutingDecision


def live_hierarchy(adapter) -> dict[int, list[dict[str, Any]]]:
    """The CURRENT community hierarchy, level by level, id-sorted.

    Level 0 is the set of community ids entities actually point at; parents
    follow ``parent_id`` upward. Dissolved rows (``descriptor.dissolved``)
    are excluded even if referenced — they are history, not hierarchy."""
    anchored = {e["community_id"] for e in adapter.find_entities({})
                if e.get("community_id") is not None}
    levels: dict[int, list[dict[str, Any]]] = {}
    wanted = anchored
    level = 0
    while wanted:
        rows = [
            r for r in adapter.get_communities(level)
            if r["id"] in wanted
            and not (r.get("descriptor") or {}).get("dissolved")
        ]
        if not rows:
            break
        levels[level] = sorted(rows, key=lambda r: r["id"])
        wanted = {r["parent_id"] for r in rows if r.get("parent_id") is not None}
        level += 1
    return levels


def _parse_report(row: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(row["report"])
    except (json.JSONDecodeError, TypeError) as e:
        raise QueryError(
            f"community {row['id']} ({row.get('label')!r}) carries a report "
            f"that is not valid JSON — the store is corrupt or hand-edited: {e}"
        ) from e
    if not isinstance(payload, dict) or not payload.get("summary"):
        raise QueryError(
            f"community {row['id']} report lacks a summary — not a valid "
            "report payload"
        )
    return payload


def _report_text(row: dict[str, Any], payload: dict[str, Any]) -> str:
    lines = [f"Label: {row.get('label') or '(unlabelled)'}",
             f"Summary: {payload['summary']}"]
    findings = payload.get("findings") or []
    if findings:
        lines.append("Findings:")
        lines.extend(f"- {f.get('text', '')}" for f in findings)
    return "\n".join(lines)


def _chunks(rows: list, size: int) -> list[list]:
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def _resolve_finding_entity(
    adapter, ref: str, cid: int, member_ids: set[str]
) -> dict[str, Any] | None:
    """A report finding may cite an entity by id OR by bare name (704's real
    records do the latter — live-smoke finding). Names resolve preferring the
    kept community's own members; the corpus-wide fallback resolves ONLY a
    unique name (adversarial finding 9: guessing between out-of-community
    duplicates would fabricate a citation). Unresolvable/ambiguous citations are
    skipped, not fatal, and the caller records them in the result."""
    row = adapter.get_entity(ref)
    if row is not None:
        return row
    candidates = adapter.find_entities({"name": ref, "limit": 20})
    if not candidates:
        return None
    in_community = [c for c in candidates
                    if c.get("community_id") == cid or c["id"] in member_ids]
    if in_community:
        return in_community[0]
    if len(candidates) == 1:
        return candidates[0]
    return None  # ambiguous corpus-wide — skip and record


def run_global(
    adapter,
    question: str,
    scorer,
    *,
    decision: RoutingDecision,
    expand_tiers: str = "both",
    budget_ms: float = DEFAULT_GLOBAL_BUDGET_MS,
    relevance_floor: float = RELEVANCE_FLOOR,
    max_kept: int = MAX_KEPT_REPORTS,
    batch_size: int = SCORE_BATCH_SIZE,
    score_parallelism: int = SCORE_PARALLELISM,
    max_entities: int = DEFAULT_MAX_ENTITIES,
) -> QueryResult:
    budget = Budget(budget_ms)
    reasons: list[str] = []
    timings: dict[str, float] = {}

    levels = live_hierarchy(adapter)
    if not levels:
        raise QueryError(
            f"global search needs communities but corpus {adapter.corpus_id!r} "
            "has none — run `graph communities` first"
        )
    # Parse every live report up front: a corrupt payload fails loudly and
    # names its community, whether or not scoring would have kept it.
    payloads: dict[int, dict[str, Any]] = {}
    with_reports: dict[int, list[dict[str, Any]]] = {}
    for lvl, rows in levels.items():
        keep = [r for r in rows if (r.get("report") or "").strip()]
        for r in keep:
            payloads[r["id"]] = _parse_report(r)
        with_reports[lvl] = keep
    if not any(with_reports.values()):
        raise QueryError(
            f"global search against zero community reports in corpus "
            f"{adapter.corpus_id!r} — run the summarization pass "
            "(graph records) before asking breadth questions"
        )

    communities_scored: list[dict[str, Any]] = []
    kept_entries: list[dict[str, Any]] = []
    t0 = perf_counter()
    for lvl in sorted(with_reports, reverse=True):
        rows = with_reports[lvl]
        if not rows:
            continue
        if budget.exceeded():
            reasons.append(
                f"wall_clock_budget({budget.budget_ms:g}ms) exceeded before "
                f"scoring level {lvl}; descent stopped"
            )
            break
        batches = _chunks(rows, batch_size)  # rows are id-sorted already
        # Adversarial finding 1: enqueueing every batch up front made the
        # wall-clock budget dead code during the map step. Batches run in
        # parallelism-sized WAVES; the clock is re-checked as each wave
        # completes, and exceeding it names the unscored remainder loudly —
        # the filter step then proceeds over the partial scores.
        scores_by_batch: dict[int, list[float]] = {}
        scored_batches: list[tuple[int, list[dict[str, Any]]]] = []
        waves = _chunks(list(enumerate(batches)), max(1, score_parallelism))
        for w, wave in enumerate(waves):
            if budget.exceeded():
                left = sum(len(v) for v in waves[w:])
                reasons.append(
                    f"wall_clock_budget({budget.budget_ms:g}ms) exceeded at "
                    f"level {lvl}; {left} batch(es) left unscored"
                )
                break
            with ThreadPoolExecutor(max_workers=max(1, score_parallelism)) as ex:
                futures = {
                    ex.submit(
                        scorer.score_reports, question,
                        [(r["id"], _report_text(r, payloads[r["id"]])) for r in batch],
                    ): i
                    for i, batch in wave
                }
                for fut, i in futures.items():
                    scores_by_batch[i] = fut.result()  # scorer errors propagate loudly
            scored_batches.extend(wave)
        level_entries: list[dict[str, Any]] = []
        for i, batch in sorted(scored_batches):
            for row, score in zip(batch, scores_by_batch[i], strict=True):
                level_entries.append({
                    "community_id": row["id"], "level": lvl,
                    "label": row.get("label"), "score": score, "kept": False,
                })
        communities_scored.extend(level_entries)
        candidates = sorted(
            (e for e in level_entries if e["score"] >= relevance_floor),
            key=lambda e: (-e["score"], e["community_id"]),
        )[:max_kept]
        for e in candidates:
            e["kept"] = True
        if candidates:
            kept_entries = candidates
            break  # descend only when the kept set is empty
        if any("wall_clock" in r for r in reasons):
            break  # out of time: do not descend further on a partial map
    timings["score_ms"] = (perf_counter() - t0) * 1000.0

    row_by_id = {r["id"]: r for rows in with_reports.values() for r in rows}
    reports_out: list[dict[str, Any]] = []
    items: list[ContextItem] = []
    seen: set[str] = set()
    for entry in kept_entries:
        cid = entry["community_id"]
        payload = payloads[cid]
        report_out = {
            "community_id": cid,
            "level": entry["level"],
            "label": entry["label"],
            "score": entry["score"],
            "summary": payload["summary"],
            "findings": payload.get("findings") or [],
            # finding 9: skipped citations are visible, never silent
            "unresolved_entities": [],
        }
        reports_out.append(report_out)
        comm_row = row_by_id[cid]
        member_ids = set(comm_row.get("member_ids") or [])
        for rank, finding in enumerate(payload.get("findings") or []):
            for ref in finding.get("entities") or []:
                row = _resolve_finding_entity(adapter, ref, cid, member_ids)
                if row is None:
                    # stale or ambiguous citation — skipped AND recorded
                    if ref not in report_out["unresolved_entities"]:
                        report_out["unresolved_entities"].append(ref)
                    continue
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                items.append(ContextItem(
                    entity_id=row["id"], tier=row["tier"], kind=row["kind"],
                    name=row["name"],
                    source_paths=list(row.get("source_paths") or []),
                    span=row.get("span"), origin=row["origin"],
                    path=[],
                    # a report finding is an agent assertion, not an AST fact
                    min_confidence=AGENT_DEFAULT_CONFIDENCE,
                    score=entry["score"] / 10.0 * (0.95 ** rank),
                    depth=0,
                    summary=row.get("summary"),
                    community={
                        "community_id": cid, "level": entry["level"],
                        "label": entry["label"], "score": entry["score"],
                    },
                ))

    t0 = perf_counter()
    extras = cross_tiers(adapter, items, expand_tiers=expand_tiers)
    timings["tier_cross_ms"] = (perf_counter() - t0) * 1000.0
    items = sorted([*items, *extras], key=lambda i: (-i.score, i.entity_id))
    if len(items) > max_entities:
        reasons.append(
            f"retained_entity_cap({max_entities}) trimmed "
            f"{len(items) - max_entities} entities from the kept reports"
        )
        items = items[:max_entities]
    if expand_tiers == "symbols_only":
        items = [i for i in items if i.tier == "symbol"]
    elif expand_tiers == "concepts_only":
        items = [i for i in items if i.tier == "concept"]

    timings["total_ms"] = budget.elapsed_ms()
    return QueryResult(
        question=question,
        routing=decision,
        items=items,
        truncated=bool(reasons),
        truncation_reasons=reasons,
        reports=reports_out,
        communities_scored=communities_scored,
        timings_ms=timings,
    )
