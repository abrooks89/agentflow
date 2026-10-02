"""Epic 705 scope 5 (surface 1): query() end-to-end wiring — routing plumbed
into the right pipeline, overrides honored, lazy embedder construction, and
the RoutingDecision attached to every result."""

from __future__ import annotations

import json

import pytest

from autodoc_query.api import global_search, query
from autodoc_query.types import QueryError
from tests.conftest import entity_row
from tests.query_helpers import (
    A,
    FakeEmbedder,
    build_query_graph,
    unit_vec,
)


class StubScorer:
    def __init__(self, score: float = 9.0):
        self.score = score
        self.calls = 0

    def score_reports(self, question, reports):
        self.calls += 1
        return [self.score] * len(reports)


@pytest.fixture
def full_graph(adapter):
    """The local fixture graph plus one live community with a report."""
    build_query_graph(adapter)
    adapter.upsert_entities([
        entity_row(A, name="ingest_records", community_id=1,
                   source_paths=["app/ingest.py"],
                   span={"file": "app/ingest.py", "start_line": 10, "end_line": 40}),
    ])
    adapter.upsert_communities([{
        "level": 0, "label": "ingest", "member_ids": [A],
        "report": json.dumps({
            "summary": "the ingest side",
            "findings": [{"text": "ingest_records is the entry", "entities": [A]}],
        }),
    }])
    return adapter


def test_auto_routes_entity_question_to_local(full_graph):
    res = query("what does ingest_records call?", full_graph,
                embedder=FakeEmbedder(unit_vec(0)))
    assert res.routing.mode == "local"
    assert res.routing.rule == "entity_resolution"
    assert A in [i.entity_id for i in res.items]


def test_auto_routes_breadth_question_to_global_without_embedder(full_graph):
    # A breadth-marker question must NOT construct or touch any embedding
    # endpoint (portability: global-only corpora need no Ollama at all).
    scorer = StubScorer()
    res = query("give me an overview of the system", full_graph,
                embedder=None, scorer=scorer)
    assert res.routing.mode == "global"
    assert res.routing.rule == "breadth_marker"
    assert scorer.calls > 0
    assert res.items


def test_mode_override_local_is_plumbed(full_graph):
    res = query("overview of everything", full_graph, mode="local",
                embedder=FakeEmbedder(unit_vec(0)))
    assert res.routing.mode == "local"
    assert res.routing.rule == "override"


def test_mode_override_global_on_entity_question(full_graph):
    res = query("what does `ingest_records` do?", full_graph, mode="global",
                scorer=StubScorer())
    assert res.routing.mode == "global"
    assert res.routing.rule == "override"
    assert res.reports


def test_seed_floor_reroute_reaches_global(full_graph):
    # no markers, no resolvable symbols, orthogonal embedding -> reroute
    res = query("tell me about deployment strategy", full_graph,
                embedder=FakeEmbedder(unit_vec(5)), scorer=StubScorer())
    assert res.routing.mode == "global"
    assert res.routing.rule == "seed_floor_reroute"
    assert res.routing.reroute_from == "local"


def test_default_local_reuses_router_embedding(full_graph):
    emb = FakeEmbedder(unit_vec(0))
    res = query("persisting things somewhere", full_graph, embedder=emb)
    assert res.routing.rule == "default_local"
    # embedded exactly once: the router's spend is handed to local search
    assert len(emb.calls) == 1


def test_global_override_with_zero_reports_is_loud(adapter):
    adapter.upsert_entities([entity_row(A, name="ingest_records", community_id=1)])
    adapter.upsert_communities([{"level": 0, "label": "bare", "member_ids": [A]}])
    with pytest.raises(QueryError, match="report"):
        query("q", adapter, mode="global", scorer=StubScorer())


def test_depth_validation_happens_before_any_work(full_graph):
    with pytest.raises(QueryError, match="depth"):
        query("q", full_graph, embedder=FakeEmbedder(unit_vec(0)), depth=99)


def test_query_results_serialize_round_trip(full_graph):
    res = query("what does ingest_records call?", full_graph,
                embedder=FakeEmbedder(unit_vec(0)))
    blob = json.dumps(res.to_dict())
    parsed = json.loads(blob)
    assert parsed["routing"]["mode"] == "local"
    assert parsed["items"]


def test_resolved_entities_seed_local_search_even_with_a_weak_embedder(full_graph):
    # adversarial finding 8: entity-resolution evidence must UNION into the
    # seed set at similarity 1.0. 'lonely_fn' has no embedding and no edges —
    # it can appear in results ONLY via the evidence union.
    lonely = "sym:corp:app/lonely.py:function:lonely_fn"
    full_graph.upsert_entities([
        entity_row(lonely, name="lonely_fn", source_paths=["app/lonely.py"],
                   span={"file": "app/lonely.py", "start_line": 1, "end_line": 3}),
    ])
    res = query("what does lonely_fn do?", full_graph,
                embedder=FakeEmbedder(unit_vec(7)))  # orthogonal to everything
    assert res.routing.rule == "entity_resolution"
    items = {i.entity_id: i for i in res.items}
    assert lonely in items
    assert items[lonely].score == pytest.approx(1.0)
    assert items[lonely].depth == 0 and items[lonely].path == []


def test_global_search_helper_is_an_override(full_graph):
    res = global_search("anything", full_graph, scorer=StubScorer())
    assert res.routing.mode == "global"
    assert res.routing.rule == "override"
