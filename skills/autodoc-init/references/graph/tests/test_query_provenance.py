"""Epic 705 scope 6 / success criterion 5: provenance is non-optional on
every ContextItem, dreamed edges are invisible by default, and speculative
items render the hypothesized-link marker."""

from __future__ import annotations

import pytest

from autodoc_query.api import local_search
from autodoc_query.types import SPECULATIVE_MARKER
from tests.query_helpers import (
    E,
    FakeEmbedder,
    build_query_graph,
    unit_vec,
)


@pytest.fixture
def graph(adapter):
    build_query_graph(adapter)
    return adapter


def _results(graph, **kw):
    return local_search("q", graph, embedder=FakeEmbedder(unit_vec(0)), **kw)


def test_no_item_lacks_origin_or_min_confidence(graph):
    # criterion 5, stated as a test: walk EVERY item of a mixed result.
    res = _results(graph, include_dreamed=True)
    assert res.items
    for item in res.items:
        assert item.origin in ("ast", "agent"), item.entity_id
        assert isinstance(item.min_confidence, float)
        assert 0.0 <= item.min_confidence <= 1.0
        assert item.tier in ("symbol", "concept")
        assert item.entity_id and item.name


def test_every_hop_carries_rel_origin_confidence(graph):
    res = _results(graph, include_dreamed=True)
    for item in res.items:
        for hop in item.path:
            assert hop.rel and hop.origin in ("ast", "agent", "dreamed")
            assert 0.0 <= hop.confidence <= 1.0
            assert hop.direction in ("in", "out")


def test_min_confidence_is_min_along_path(graph):
    res = _results(graph, include_dreamed=True)
    for item in res.items:
        if item.path:
            assert item.min_confidence == pytest.approx(
                min(h.confidence for h in item.path)
            )


def test_default_settings_return_nothing_dreamed(graph):
    # criterion 5: with default settings the dreamed fixture yields nothing
    # dreamed at all — no item, no hop, no speculative flag.
    res = _results(graph)
    ids = [i.entity_id for i in res.items]
    assert E not in ids
    for item in res.items:
        assert item.speculative is False
        assert all(h.origin != "dreamed" for h in item.path)
        assert SPECULATIVE_MARKER not in item.citation


def test_speculative_citation_renders_marker(graph):
    res = _results(graph, include_dreamed=True)
    spec_items = [i for i in res.items if i.speculative]
    assert spec_items  # the dreamed fixture is reachable when opted in
    for item in spec_items:
        assert SPECULATIVE_MARKER in item.citation


def test_result_serializes_with_full_provenance(graph):
    d = _results(graph, include_dreamed=True).to_dict()
    assert d["routing"]["rule"]
    for item in d["items"]:
        assert "origin" in item and "min_confidence" in item
        assert "citation" in item
        for hop in item["path"]:
            assert {"rel", "origin", "confidence"} <= set(hop)
