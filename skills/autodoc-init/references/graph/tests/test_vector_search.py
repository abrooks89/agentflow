"""vector_search + upsert_embeddings (success criterion 3, with deterministic
fake vectors — live Ollama is exercised later over the SSH tunnel, not here)."""

from __future__ import annotations

import math

import pytest
from conftest import entity_row

from autodoc_graph.adapter import AdapterError
from autodoc_graph.vocab import EMBEDDING_DIM, EMBEDDING_MODEL


def unit_vec(i: int, j: int, wi: float = 1.0, wj: float = 0.0) -> list[float]:
    """Deterministic 768-dim unit vector concentrated on axes i and j."""
    v = [0.0] * EMBEDDING_DIM
    n = math.sqrt(wi * wi + wj * wj)
    v[i] = wi / n
    if wj:
        v[j] = wj / n
    return v


def _seed(adapter):
    ids = {
        "anchor": "sym:corp:a.py:function:anchor",
        "near": "sym:corp:a.py:function:near",
        "far": "sym:corp:b.py:class:Far",
    }
    adapter.upsert_entities([
        entity_row(ids["anchor"]),
        entity_row(ids["near"]),
        entity_row(ids["far"], kind="class", name="Far", source_paths=["b.py"]),
    ])
    # entity_version mirrors what embed_pending stamps: the entity's updated_at
    # at embed time (the content-change marker; finding 6)
    ver = {k: adapter.get_entity(v)["updated_at"] for k, v in ids.items()}
    adapter.upsert_embeddings([
        {"entity_id": ids["anchor"], "vec": unit_vec(0, 1),
         "model": EMBEDDING_MODEL, "embedded_at_sha": "s",
         "entity_version": ver["anchor"]},
        {"entity_id": ids["near"], "vec": unit_vec(0, 1, 0.9, 0.1),
         "model": EMBEDDING_MODEL, "embedded_at_sha": "s",
         "entity_version": ver["near"]},
        {"entity_id": ids["far"], "vec": unit_vec(5, 6),
         "model": EMBEDDING_MODEL, "embedded_at_sha": "s",
         "entity_version": ver["far"]},
    ])
    return ids


def test_vector_search_returns_sane_neighbors_best_first(adapter):
    ids = _seed(adapter)
    res = adapter.vector_search(unit_vec(0, 1), k=3)
    assert [r[0] for r in res] == [ids["anchor"], ids["near"], ids["far"]]
    scores = [r[1] for r in res]
    assert scores[0] == pytest.approx(1.0, abs=1e-6)
    assert scores[0] >= scores[1] >= scores[2]
    assert scores[2] == pytest.approx(0.0, abs=1e-6)


def test_vector_search_respects_k_and_filter(adapter):
    ids = _seed(adapter)
    assert len(adapter.vector_search(unit_vec(0, 1), k=1)) == 1
    only_classes = adapter.vector_search(unit_vec(0, 1), k=5, filter={"kind": "class"})
    assert [r[0] for r in only_classes] == [ids["far"]]


def test_vector_search_wrong_dim_is_loud(adapter):
    _seed(adapter)
    with pytest.raises(AdapterError, match="768"):
        adapter.vector_search([1.0, 2.0], k=1)


def test_upsert_embeddings_wrong_dim_or_model_is_loud(adapter):
    ids = _seed(adapter)
    with pytest.raises(AdapterError, match="768"):
        adapter.upsert_embeddings([{"entity_id": ids["anchor"], "vec": [1.0],
                                    "model": EMBEDDING_MODEL, "embedded_at_sha": "s"}])
    with pytest.raises(AdapterError, match="model"):
        adapter.upsert_embeddings([{"entity_id": ids["anchor"],
                                    "vec": unit_vec(0, 1),
                                    "model": "other-model", "embedded_at_sha": "s"}])


def test_needs_embedding_is_keyed_on_content_change_not_sha_restamp(adapter):
    # finding 6: an unchanged build re-stamps last_seen_sha; that must NOT
    # re-embed the whole corpus. Staleness keys on CONTENT change (updated_at).
    ids = _seed(adapter)
    assert adapter.find_entities({"needs_embedding": True}) == []
    # sha-only restamp (content identical, the every-build path): still none pending
    adapter.upsert_entities([entity_row(ids["anchor"], last_seen_sha="s2")])
    assert adapter.find_entities({"needs_embedding": True}) == []
    # a real content change: exactly that entity becomes pending
    adapter.upsert_entities([entity_row(ids["anchor"], last_seen_sha="s2",
                                        summary="def anchor(changed):")])
    pending = adapter.find_entities({"needs_embedding": True})
    assert [e["id"] for e in pending] == [ids["anchor"]]
    # a brand-new entity with no embedding row is pending too
    adapter.upsert_entities([entity_row("sym:corp:c.py:function:new")])
    pending = {e["id"] for e in adapter.find_entities({"needs_embedding": True})}
    assert pending == {ids["anchor"], "sym:corp:c.py:function:new"}
