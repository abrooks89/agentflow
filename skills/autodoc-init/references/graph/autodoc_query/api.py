"""Library API of the query layer (epic 705 scope 5, surface 1).

``query(question, adapter, mode='auto', ...)`` routes and runs;
``local_search()`` / ``global_search()`` are for callers that already know —
they are recorded as explicit overrides (rule='override'), never re-routed.

Model-match constraint (spec): the store's ``upsert_embeddings`` refuses any
model other than ``vocab.EMBEDDING_MODEL``, so asserting the QUERY embedder's
model against the same constant guarantees query and index share one
embedding space. A mismatch fails loudly here rather than querying anyway —
a query embedded by a different model than the index is a silent correctness
bug.
"""

from __future__ import annotations

from typing import Any

from autodoc_graph.embedder import OllamaEmbedder
from autodoc_graph.vocab import EMBEDDING_DIM, EMBEDDING_MODEL

from . import globalsearch as _global
from . import local as _local
from .router import route
from .scorer import ReportScorer
from .tuning import (
    DEFAULT_DEPTH,
    DEFAULT_FANOUT_CAP,
    DEFAULT_GLOBAL_BUDGET_MS,
    DEFAULT_K,
    DEFAULT_LOCAL_BUDGET_MS,
    DEFAULT_MAX_ENTITIES,
    SCORE_BATCH_SIZE,
    SCORE_PARALLELISM,
    SEED_FLOOR,
)
from .types import QueryError, QueryResult, RoutingDecision


def assert_embedding_model(embedder: Any) -> None:
    """Fail loudly on a query/index embedding-space mismatch."""
    model = getattr(embedder, "model", None)
    if model != EMBEDDING_MODEL:
        raise QueryError(
            f"query embedder model {model!r} does not match the index model "
            f"{EMBEDDING_MODEL!r} — refusing to query across embedding spaces"
        )


def _assert_corpus_nonempty(adapter) -> None:
    stats = adapter.stats()
    if not stats.get("entities"):
        raise QueryError(
            f"corpus {adapter.corpus_id!r} is empty — build the graph first "
            "(graph build --jsonl ...)"
        )


def _make_embed(embedder: Any):
    """A question -> vector callable with the dimension re-checked."""

    def embed(text: str) -> list[float]:
        vec = embedder.embed_texts([text])[0]
        if len(vec) != EMBEDDING_DIM:
            raise QueryError(
                f"query embedding has {len(vec)} dims; the index is fixed at "
                f"{EMBEDDING_DIM}"
            )
        return vec

    return embed


def local_search(
    question: str,
    adapter,
    *,
    embedder: Any | None = None,
    ollama_url: str | None = None,
    k: int = DEFAULT_K,
    depth: int = DEFAULT_DEPTH,
    expand_tiers: str = "both",
    include_dreamed: bool = False,
    fanout_cap: int = DEFAULT_FANOUT_CAP,
    max_entities: int = DEFAULT_MAX_ENTITIES,
    budget_ms: float = DEFAULT_LOCAL_BUDGET_MS,
) -> QueryResult:
    """Local search for callers that already know. Recorded as an explicit
    override — never silently re-routed."""
    decision = RoutingDecision(mode="local", rule="override",
                               evidence={"via": "local_search()"})
    return _run_local(
        question, adapter, decision, embedder=embedder, ollama_url=ollama_url,
        seeds=None, qvec=None, k=k, depth=depth, expand_tiers=expand_tiers,
        include_dreamed=include_dreamed, fanout_cap=fanout_cap,
        max_entities=max_entities, budget_ms=budget_ms,
    )


def _run_local(
    question: str,
    adapter,
    decision: RoutingDecision,
    *,
    embedder: Any | None,
    ollama_url: str | None,
    seeds,
    qvec,
    **knobs: Any,
) -> QueryResult:
    _local.validate_common(knobs["depth"], knobs["expand_tiers"])
    _assert_corpus_nonempty(adapter)
    if embedder is None:
        embedder = OllamaEmbedder(url=ollama_url)
    assert_embedding_model(embedder)
    return _local.run_local(
        adapter, question, _make_embed(embedder), decision=decision,
        seeds=seeds, qvec=qvec, **knobs,
    )


def global_search(
    question: str,
    adapter,
    *,
    scorer: Any | None = None,
    scorer_url: str | None = None,
    scorer_model: str | None = None,
    expand_tiers: str = "both",
    budget_ms: float = DEFAULT_GLOBAL_BUDGET_MS,
    max_entities: int = DEFAULT_MAX_ENTITIES,
    **knobs: Any,
) -> QueryResult:
    """Global search for callers that already know. Explicit override —
    a corpus with zero reports is a loud error, never an empty result."""
    decision = RoutingDecision(mode="global", rule="override",
                               evidence={"via": "global_search()"})
    return _run_global(
        question, adapter, decision, scorer=scorer, scorer_url=scorer_url,
        scorer_model=scorer_model, expand_tiers=expand_tiers,
        budget_ms=budget_ms, max_entities=max_entities, **knobs,
    )


def _run_global(
    question: str,
    adapter,
    decision: RoutingDecision,
    *,
    scorer: Any | None,
    scorer_url: str | None,
    scorer_model: str | None,
    **knobs: Any,
) -> QueryResult:
    _local.validate_common(0, knobs.get("expand_tiers", "both"))
    _assert_corpus_nonempty(adapter)
    if scorer is None:
        scorer = ReportScorer(url=scorer_url, model=scorer_model)
    return _global.run_global(adapter, question, scorer, decision=decision, **knobs)


def query(
    question: str,
    adapter,
    *,
    mode: str = "auto",
    embedder: Any | None = None,
    ollama_url: str | None = None,
    scorer: Any | None = None,
    scorer_url: str | None = None,
    scorer_model: str | None = None,
    k: int = DEFAULT_K,
    depth: int = DEFAULT_DEPTH,
    expand_tiers: str = "both",
    include_dreamed: bool = False,
    fanout_cap: int = DEFAULT_FANOUT_CAP,
    max_entities: int = DEFAULT_MAX_ENTITIES,
    local_budget_ms: float = DEFAULT_LOCAL_BUDGET_MS,
    global_budget_ms: float = DEFAULT_GLOBAL_BUDGET_MS,
    score_batch_size: int = SCORE_BATCH_SIZE,
    score_parallelism: int = SCORE_PARALLELISM,
    seed_floor: float = SEED_FLOOR,
) -> QueryResult:
    """Route, then run. The routing decision — mode, the rule that fired,
    its evidence, and any re-route — is attached to every result.

    The embedder is constructed lazily: a question that routes global via an
    override or a breadth marker never needs an embedding endpoint at all."""
    _local.validate_common(depth, expand_tiers)
    _assert_corpus_nonempty(adapter)

    embedder_ref: dict[str, Any] = {"e": embedder}

    def embed(text: str) -> list[float]:
        if embedder_ref["e"] is None:
            embedder_ref["e"] = OllamaEmbedder(url=ollama_url)
        assert_embedding_model(embedder_ref["e"])
        return _make_embed(embedder_ref["e"])(text)

    decision, seeds, qvec = route(question, adapter, mode=mode, embed=embed,
                                  k=k, seed_floor=seed_floor)
    if decision.mode == "local":
        if embedder_ref["e"] is None:
            embedder_ref["e"] = OllamaEmbedder(url=ollama_url)
        assert_embedding_model(embedder_ref["e"])
        # finding 8: entities the router resolved from the question text seed
        # at similarity 1.0 — the asker literally named them
        priority = (list(decision.evidence.get("entity_ids") or [])
                    if decision.rule == "entity_resolution" else None)
        return _local.run_local(
            adapter, question, _make_embed(embedder_ref["e"]), decision=decision,
            seeds=seeds, qvec=qvec, k=k, depth=depth, expand_tiers=expand_tiers,
            include_dreamed=include_dreamed, fanout_cap=fanout_cap,
            max_entities=max_entities, budget_ms=local_budget_ms,
            priority_seeds=priority,
        )
    return _run_global(
        question, adapter, decision, scorer=scorer, scorer_url=scorer_url,
        scorer_model=scorer_model, expand_tiers=expand_tiers,
        budget_ms=global_budget_ms, max_entities=max_entities,
        batch_size=score_batch_size, score_parallelism=score_parallelism,
    )
