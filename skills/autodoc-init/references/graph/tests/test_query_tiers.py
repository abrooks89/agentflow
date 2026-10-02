"""Epic 705 scope 4 / success criterion 4: tier crossing as an explicit API
contract — both directions, outside the depth budget, with the undocumented
case explicit (`documented_by: []`), and one-tier filters."""

from __future__ import annotations

import pytest

from autodoc_query.api import local_search
from tests.query_helpers import (
    A,
    B,
    FakeEmbedder,
    K,
    build_query_graph,
    unit_vec,
)


@pytest.fixture
def graph(adapter):
    build_query_graph(adapter)
    return adapter


def _by_id(res):
    return {i.entity_id: i for i in res.items}


def test_symbol_question_surfaces_documenting_concept(graph):
    # criterion 4, direction 1: a symbol-level question returns >= 1
    # tier='concept' item. k=1/depth=0 pins the seed set to the symbol alone,
    # proving the documents join runs OUTSIDE the depth budget.
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(0)),
                       k=1, depth=0)
    items = _by_id(res)
    assert items[K].tier == "concept"
    # the concept was reached through the documents join, outside the budget
    assert items[K].path[-1].rel == "documents"
    # and the symbol side names the concept documenting it
    refs = items[A].documented_by
    assert [r.entity_id for r in refs] == [K]


def test_concept_question_lands_on_symbols_with_spans(graph):
    # criterion 4, direction 2: a concept-level question returns >= 1
    # tier='symbol' item with a non-null span (exact file:line citation).
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(1)))
    items = _by_id(res)
    assert items[K].tier == "concept"     # the seed
    symbol_items = [i for i in res.items if i.tier == "symbol"]
    assert symbol_items
    assert any(i.span is not None for i in symbol_items)
    # the concept item lists the symbols it documents
    assert A in [r.entity_id for r in items[K].documents]


def test_undocumented_symbol_reports_empty_documented_by(graph):
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(0)))
    items = _by_id(res)
    assert items[B].documented_by == []   # a signal, not an omission
    assert items[B].documents is None     # inapplicable field stays None


def test_symbols_only_filter(graph):
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(0)),
                       expand_tiers="symbols_only")
    assert res.items
    assert all(i.tier == "symbol" for i in res.items)
    # the annotation contract still holds on the one tier returned
    assert _by_id(res)[A].documented_by is not None


def test_concepts_only_filter(graph):
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(1)),
                       expand_tiers="concepts_only")
    assert res.items
    assert all(i.tier == "concept" for i in res.items)


def test_cross_tier_items_dedupe_against_traversal(graph):
    # K is reachable BOTH by graph traversal from A (documents is an edge)
    # and by the tier-crossing pass; it must appear exactly once.
    res = local_search("q", graph, embedder=FakeEmbedder(unit_vec(0)))
    ids = [i.entity_id for i in res.items]
    assert ids.count(K) == 1
