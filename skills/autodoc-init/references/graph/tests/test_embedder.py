"""Embedder tests — ALL HTTP is mocked via the injectable transport (Ollama is
exercised live later over an SSH tunnel, never from this suite).

URL resolution contract (portability requirement): explicit argument >
AUTODOC_OLLAMA_URL env var > http://localhost:11434 (Ollama's universal
default). No other default exists anywhere.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import entity_row

from autodoc_graph.embedder import EmbedderError, OllamaEmbedder, resolve_ollama_url
from autodoc_graph.vocab import EMBEDDING_DIM, EMBEDDING_MODEL

TAGS_OK = {"models": [{"name": f"{EMBEDDING_MODEL}:latest"}, {"name": "qwen3:8b"}]}


class FakeTransport:
    """Records requests; serves /api/tags and /api/embed."""

    def __init__(self, tags=None, dim=EMBEDDING_DIM, model=EMBEDDING_MODEL):
        self.calls = []
        self.tags = tags if tags is not None else TAGS_OK
        self.dim = dim
        self.model = model

    def __call__(self, url, payload=None):
        self.calls.append((url, payload))
        if url.endswith("/api/tags"):
            return self.tags
        if url.endswith("/api/embed"):
            texts = payload["input"]
            return {"model": self.model,
                    "embeddings": [[0.5] * self.dim for _ in texts]}
        raise AssertionError(f"unexpected url {url}")


# ---------------------------------------------------------------- URL resolution
def test_default_url_is_localhost_11434(monkeypatch):
    monkeypatch.delenv("AUTODOC_OLLAMA_URL", raising=False)
    assert resolve_ollama_url(None) == "http://localhost:11434"


def test_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("AUTODOC_OLLAMA_URL", "http://example.test:9999")
    assert resolve_ollama_url(None) == "http://example.test:9999"


def test_explicit_argument_beats_env_var(monkeypatch):
    monkeypatch.setenv("AUTODOC_OLLAMA_URL", "http://example.test:9999")
    assert resolve_ollama_url("http://other.test:1234") == "http://other.test:1234"


def test_trailing_slash_is_normalized(monkeypatch):
    monkeypatch.delenv("AUTODOC_OLLAMA_URL", raising=False)
    assert resolve_ollama_url("http://localhost:11434/") == "http://localhost:11434"


# ---------------------------------------------------------------- embed_texts
def test_embed_texts_batches_of_50():
    t = FakeTransport()
    emb = OllamaEmbedder(url="http://localhost:11434", transport=t)
    vecs = emb.embed_texts([f"t{i}" for i in range(120)])
    assert len(vecs) == 120
    embed_calls = [c for c in t.calls if c[0].endswith("/api/embed")]
    assert [len(c[1]["input"]) for c in embed_calls] == [50, 50, 20]
    assert all(c[1]["model"] == EMBEDDING_MODEL for c in embed_calls)


def test_missing_model_error_is_actionable():
    t = FakeTransport(tags={"models": [{"name": "qwen3:8b"}]})
    emb = OllamaEmbedder(url="http://localhost:11434", transport=t)
    with pytest.raises(EmbedderError, match=r"ollama pull nomic-embed-text"):
        emb.embed_texts(["x"])


def test_response_model_mismatch_is_loud():
    t = FakeTransport(model="other-embedder")
    emb = OllamaEmbedder(url="http://localhost:11434", transport=t)
    with pytest.raises(EmbedderError, match="model"):
        emb.embed_texts(["x"])


def test_wrong_dimension_is_loud():
    t = FakeTransport(dim=384)
    emb = OllamaEmbedder(url="http://localhost:11434", transport=t)
    with pytest.raises(EmbedderError, match="768"):
        emb.embed_texts(["x"])


def test_unreachable_ollama_names_the_url():
    def dead_transport(url, payload=None):
        raise OSError("connection refused")

    emb = OllamaEmbedder(url="http://unreachable.test:11434", transport=dead_transport)
    with pytest.raises(EmbedderError, match=r"unreachable\.test:11434"):
        emb.embed_texts(["x"])


def test_embedding_count_mismatch_is_loud():
    class ShortTransport(FakeTransport):
        def __call__(self, url, payload=None):
            if url.endswith("/api/embed"):
                self.calls.append((url, payload))
                return {"model": EMBEDDING_MODEL, "embeddings": [[0.5] * EMBEDDING_DIM]}
            return super().__call__(url, payload)

    emb = OllamaEmbedder(url="http://localhost:11434", transport=ShortTransport())
    with pytest.raises(EmbedderError, match="count"):
        emb.embed_texts(["a", "b"])


# ---------------------------------------------------------------- embed_pending
def test_embed_pending_is_incremental(adapter):
    adapter.upsert_entities([
        entity_row("sym:corp:a.py:function:f", last_seen_sha="s1"),
        entity_row("sym:corp:a.py:function:g", last_seen_sha="s1"),
    ])
    t = FakeTransport()
    emb = OllamaEmbedder(url="http://localhost:11434", transport=t)
    assert emb.embed_pending(adapter) == {"embedded": 2}
    assert adapter.stats()["embedded"] == 2
    # second pass: nothing advanced, nothing embedded, no embed HTTP call
    calls_before = len(t.calls)
    assert emb.embed_pending(adapter) == {"embedded": 0}
    assert len([c for c in t.calls[calls_before:] if c[0].endswith("/api/embed")]) == 0
    # sha-only restamp (the unchanged-build path, finding 6): still nothing to embed
    adapter.upsert_entities([entity_row("sym:corp:a.py:function:f", last_seen_sha="s2")])
    assert emb.embed_pending(adapter) == {"embedded": 0}
    # a real content change: exactly one re-embed
    adapter.upsert_entities([entity_row("sym:corp:a.py:function:f", last_seen_sha="s2",
                                        summary="def f(changed):")])
    assert emb.embed_pending(adapter) == {"embedded": 1}


def test_embed_pending_stamps_embedded_at_sha_and_entity_version(adapter, db_path):
    adapter.upsert_entities([entity_row("sym:corp:a.py:function:f", last_seen_sha="s7")])
    updated_at = adapter.get_entity("sym:corp:a.py:function:f")["updated_at"]
    OllamaEmbedder(url="http://localhost:11434",
                   transport=FakeTransport()).embed_pending(adapter)
    con = sqlite3.connect(db_path)
    try:
        sha, ver = con.execute(
            "SELECT embedded_at_sha, entity_version FROM embedding").fetchone()
        assert sha == "s7"
        assert ver == updated_at  # the content-change marker (finding 6)
    finally:
        con.close()
