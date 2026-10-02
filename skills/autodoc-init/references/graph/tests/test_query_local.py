"""Epic 705 scope 2 + constraints: local search — seed, traverse, rank —
with every budget guard loud and every failure mode named."""

from __future__ import annotations

import pytest

from autodoc_query.api import local_search
from autodoc_query.tuning import HOP_DECAY
from autodoc_query.types import QueryError
from tests.conftest import entity_row
from tests.query_helpers import (
    A,
    B,
    C,
    D,
    E,
    F,
    FakeEmbedder,
    build_query_graph,
    unit_vec,
)


@pytest.fixture
def graph(adapter):
    build_query_graph(adapter)
    return adapter


def _embedder(axis: int = 0) -> FakeEmbedder:
    return FakeEmbedder(unit_vec(axis))


def _ids(result):
    return [i.entity_id for i in result.items]


# ---------------------------------------------------------------- traversal
def test_seed_and_two_hop_expansion(graph):
    res = local_search("how does ingest_records work?", graph,
                       embedder=_embedder())
    ids = _ids(res)
    assert ids[0] == A                     # seed ranks first
    assert B in ids and C in ids           # depth 1 and 2
    assert D not in ids                    # depth 3 is beyond the budget
    assert res.routing.mode == "local"
    assert not res.truncated


def test_depth_beyond_cap_is_a_validation_error(graph):
    with pytest.raises(QueryError, match="depth"):
        local_search("q", graph, embedder=_embedder(), depth=3)


def test_ranking_blends_similarity_decay_and_confidence(graph):
    res = local_search("q", graph, embedder=_embedder())
    by_id = {i.entity_id: i for i in res.items}
    assert by_id[A].score == pytest.approx(1.0)
    assert by_id[B].score == pytest.approx(1.0 * HOP_DECAY * 1.0)
    assert by_id[C].score == pytest.approx(1.0 * HOP_DECAY**2 * 1.0)
    # agent 'related' edge (confidence 0.6) ranks below the ast chain
    assert by_id[F].score == pytest.approx(1.0 * HOP_DECAY * 0.6)
    scores = [i.score for i in res.items]
    assert scores == sorted(scores, reverse=True)


def test_path_provenance_on_expanded_items(graph):
    res = local_search("q", graph, embedder=_embedder())
    by_id = {i.entity_id: i for i in res.items}
    assert by_id[A].path == [] and by_id[A].depth == 0
    hop_b = by_id[B].path
    assert len(hop_b) == 1
    assert hop_b[0].rel == "calls" and hop_b[0].origin == "ast"
    assert hop_b[0].confidence == 1.0
    assert [h.rel for h in by_id[C].path] == ["calls", "imports"]
    assert by_id[F].min_confidence == pytest.approx(0.6)
    assert by_id[B].min_confidence == pytest.approx(1.0)


# ---------------------------------------------------------------- dreamed edges
def test_dreamed_edges_excluded_by_default(graph):
    res = local_search("q", graph, embedder=_embedder())
    assert E not in _ids(res)
    assert all(not i.speculative for i in res.items)


def test_include_dreamed_flags_speculative_and_marks_citation(graph):
    res = local_search("q", graph, embedder=_embedder(), include_dreamed=True)
    by_id = {i.entity_id: i for i in res.items}
    assert E in by_id
    item = by_id[E]
    assert item.speculative is True
    assert item.min_confidence == pytest.approx(0.3)
    assert "hypothesized" in item.citation
    # items NOT reached through a dreamed edge stay non-speculative
    assert by_id[B].speculative is False


# ---------------------------------------------------------------- budget guards
def test_fanout_cap_fires_loudly(adapter):
    hub = "sym:corp:app/config.py:function:load_config"
    adapter.upsert_entities([entity_row(hub, name="load_config")])
    spokes = []
    for i in range(6):
        sid = f"sym:corp:app/m{i}.py:function:use_{i}"
        adapter.upsert_entities([entity_row(sid, name=f"use_{i}")])
        spokes.append(sid)
    adapter.upsert_edges([
        {"src_id": s, "dst_id": hub, "rel": "imports", "origin": "ast",
         "confidence": 1.0, "evidence": {"file": "x", "line": 1}}
        for s in spokes
    ])
    adapter.upsert_embeddings([{
        "entity_id": hub, "vec": unit_vec(0), "model": "nomic-embed-text",
        "embedded_at_sha": "s1", "entity_version": "v1",
    }])
    res = local_search("q", adapter, embedder=_embedder(), fanout_cap=2)
    assert res.truncated is True
    assert any("fanout_cap" in r for r in res.truncation_reasons)
    # cap kept exactly 2 neighbors of the hub (deterministically)
    assert len(res.items) == 3


def test_retained_entity_cap_fires_loudly(graph):
    res = local_search("q", graph, embedder=_embedder(), max_entities=2)
    assert res.truncated is True
    assert any("retained_entity_cap" in r for r in res.truncation_reasons)
    assert len(res.items) == 2
    assert _ids(res)[0] == A  # best-scored survive the trim


def test_wall_clock_budget_returns_partial_with_reason(graph):
    res = local_search("q", graph, embedder=_embedder(), budget_ms=0)
    assert res.truncated is True
    assert any("wall_clock" in r for r in res.truncation_reasons)
    # seeds still present — partial results, never an empty lie
    assert A in _ids(res)


# ---------------------------------------------------------------- loud failures
def test_empty_corpus_is_loud(adapter):
    with pytest.raises(QueryError, match="empty"):
        local_search("q", adapter, embedder=_embedder())


def test_no_embedded_entities_is_loud(adapter):
    adapter.upsert_entities([entity_row("sym:corp:app/x.py:function:f1")])
    with pytest.raises(QueryError, match="embed"):
        local_search("q", adapter, embedder=_embedder())


def test_query_embedder_model_mismatch_is_loud(graph):
    bad = FakeEmbedder(unit_vec(0), model="text-embedding-3-small")
    with pytest.raises(QueryError, match="model"):
        local_search("q", graph, embedder=bad)


def test_invalid_expand_tiers_is_loud(graph):
    with pytest.raises(QueryError, match="expand_tiers"):
        local_search("q", graph, embedder=_embedder(), expand_tiers="nope")


# ---------------------------------------------------------------- determinism
def test_local_search_is_deterministic(graph):
    r1 = local_search("q", graph, embedder=_embedder())
    r2 = local_search("q", graph, embedder=_embedder())
    assert _ids(r1) == _ids(r2)
    assert [i.score for i in r1.items] == [i.score for i in r2.items]
