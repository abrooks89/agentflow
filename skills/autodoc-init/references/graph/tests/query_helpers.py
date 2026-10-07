"""Shared fixtures for the autodoc_query test modules: a small two-tier
graph with known shape, and a duck-typed fake embedder (same interface as
``OllamaEmbedder``: ``.model`` + ``.embed_texts``)."""

from __future__ import annotations

from autodoc_graph.vocab import EMBEDDING_MODEL
from tests.conftest import entity_row


def unit_vec(axis: int, dim: int = 768) -> list[float]:
    vec = [0.0] * dim
    vec[axis] = 1.0
    return vec


class FakeEmbedder:
    """Embeds every text to a fixed vector; model matches the index."""

    def __init__(self, vec: list[float], model: str = EMBEDDING_MODEL) -> None:
        self.vec = vec
        self.model = model
        self.calls: list[str] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.extend(texts)
        return [list(self.vec) for _ in texts]


def concept_row(id: str, name: str, **over) -> dict:
    row = entity_row(id, tier="concept", kind="service", name=name,
                     qualified_name=f"services/{name}.md", origin="agent",
                     span=None, source_paths=[])
    row.update(over)
    return row


def build_query_graph(adapter) -> None:
    """The standard local-search fixture graph (corpus 'corp'):

    symbols:  ingest_records(A, embedded axis0) -calls-> validate_rows(B)
              B -imports-> persist_rows(C) ; C -calls-> deep_util(D)  [depth 3]
              A -related(agent,0.6)-> flag_metrics(F)
              A -related(dreamed,0.3)-> dreamed_target(E)
    concepts: Ingest Pipeline(K, embedded axis1) -documents-> A
              Storage Notes(K2) -part_of-> K
    """
    a = "sym:corp:app/ingest.py:function:ingest_records"
    b = "sym:corp:app/ingest.py:function:validate_rows"
    c = "sym:corp:app/store.py:function:persist_rows"
    d = "sym:corp:app/util.py:function:deep_util"
    e = "sym:corp:app/maybe.py:function:dreamed_target"
    f = "sym:corp:app/metrics.py:function:flag_metrics"
    k = "con:corp:services:ingest-pipeline"
    k2 = "con:corp:services:storage-notes"
    adapter.upsert_entities([
        entity_row(a, name="ingest_records", source_paths=["app/ingest.py"],
                   span={"file": "app/ingest.py", "start_line": 10, "end_line": 40}),
        entity_row(b, name="validate_rows", source_paths=["app/ingest.py"],
                   span={"file": "app/ingest.py", "start_line": 50, "end_line": 70}),
        entity_row(c, name="persist_rows", source_paths=["app/store.py"],
                   span={"file": "app/store.py", "start_line": 5, "end_line": 30}),
        entity_row(d, name="deep_util", source_paths=["app/util.py"],
                   span={"file": "app/util.py", "start_line": 1, "end_line": 9}),
        entity_row(e, name="dreamed_target", source_paths=["app/maybe.py"],
                   span={"file": "app/maybe.py", "start_line": 1, "end_line": 4}),
        entity_row(f, name="flag_metrics", source_paths=["app/metrics.py"],
                   span={"file": "app/metrics.py", "start_line": 1, "end_line": 8}),
        concept_row(k, "Ingest Pipeline"),
        concept_row(k2, "Storage Notes"),
    ])
    adapter.upsert_edges([
        {"src_id": a, "dst_id": b, "rel": "calls", "origin": "ast",
         "confidence": 1.0, "evidence": {"file": "app/ingest.py", "line": 22}},
        {"src_id": b, "dst_id": c, "rel": "imports", "origin": "ast",
         "confidence": 1.0, "evidence": {"file": "app/ingest.py", "line": 1}},
        {"src_id": c, "dst_id": d, "rel": "calls", "origin": "ast",
         "confidence": 1.0, "evidence": {"file": "app/store.py", "line": 12}},
        {"src_id": a, "dst_id": f, "rel": "related", "origin": "agent",
         "confidence": 0.6, "evidence": {"source": "agent-return"}},
        {"src_id": a, "dst_id": e, "rel": "related", "origin": "dreamed",
         "confidence": 0.3, "evidence": {"source": "dreaming"}},
        {"src_id": k, "dst_id": a, "rel": "documents", "origin": "agent",
         "confidence": 0.7, "evidence": {"source": "agent-return"}},
        {"src_id": k2, "dst_id": k, "rel": "part_of", "origin": "agent",
         "confidence": 0.7, "evidence": {"source": "agent-return"}},
    ])
    adapter.upsert_embeddings([
        {"entity_id": a, "vec": unit_vec(0), "model": EMBEDDING_MODEL,
         "embedded_at_sha": "s1", "entity_version": "v1"},
        {"entity_id": k, "vec": unit_vec(1), "model": EMBEDDING_MODEL,
         "embedded_at_sha": "s1", "entity_version": "v1"},
    ])


A = "sym:corp:app/ingest.py:function:ingest_records"
B = "sym:corp:app/ingest.py:function:validate_rows"
C = "sym:corp:app/store.py:function:persist_rows"
D = "sym:corp:app/util.py:function:deep_util"
E = "sym:corp:app/maybe.py:function:dreamed_target"
F = "sym:corp:app/metrics.py:function:flag_metrics"
K = "con:corp:services:ingest-pipeline"
K2 = "con:corp:services:storage-notes"


# ---------------------------------------------------------------- hub graph
HUB_SEED = "sym:corp:app/hub/s00.py:function:s00"
HUB_CONCEPT = "con:corp:services:hub-00"


def _hub_sym(i: int) -> str:
    return f"sym:corp:app/hub/s{i:02d}.py:function:s{i:02d}"


def build_hub_graph(adapter, *, n_mid: int = 8, leaves_per_mid: int = 7,
                    n_concepts: int = 20, docs_per_concept: int = 40) -> None:
    """A documentation-heavy local-search fixture (corpus 'corp'), shaped
    like a real autodoc store where a few concept pages document dozens of
    symbols each — the case that made tier-crossing output unbounded.

    symbols:  s00 (seed, embedded axis0) -calls-> n_mid mids, each mid
              -calls-> leaves_per_mid leaves (1 + 8 + 56 = 65 by default,
              all within depth 2, under the default fanout cap)
    concepts: hub-NN documents a sliding window of docs_per_concept symbols
              (every concept is a hub); every concept ALSO documents s00,
              so the seed's documented_by list is n_concepts long. The
              defaults saturate the tier-ref cap on every retained item, so
              output measured on this graph is the per-item worst case.
    """
    n_sym = 1 + n_mid + n_mid * leaves_per_mid
    rows = []
    for i in range(n_sym):
        path = f"app/hub/s{i:02d}.py"
        rows.append(entity_row(
            _hub_sym(i), name=f"s{i:02d}", source_paths=[path],
            span={"file": path, "start_line": 10, "end_line": 60},
            summary=f"def s{i:02d}(records, *, strict=False) -> list[Row]: "
                    "validates and persists one batch of ingest records"))
    for c in range(n_concepts):
        rows.append(concept_row(
            f"con:corp:services:hub-{c:02d}", f"Hub Service {c:02d}",
            summary=f"Hub Service {c:02d}: the subsystem page covering one "
                    "slice of the ingest and persistence path"))
    adapter.upsert_entities(rows)
    edges = []
    for m in range(1, n_mid + 1):
        edges.append({"src_id": HUB_SEED, "dst_id": _hub_sym(m), "rel": "calls",
                      "origin": "ast", "confidence": 1.0,
                      "evidence": {"file": "app/hub/s00.py", "line": m}})
        for j in range(leaves_per_mid):
            leaf = n_mid + 1 + (m - 1) * leaves_per_mid + j
            edges.append({"src_id": _hub_sym(m), "dst_id": _hub_sym(leaf),
                          "rel": "calls", "origin": "ast", "confidence": 1.0,
                          "evidence": {"file": f"app/hub/s{m:02d}.py", "line": j}})
    for c in range(n_concepts):
        cid = f"con:corp:services:hub-{c:02d}"
        window = {(c * 4 + w) % n_sym for w in range(docs_per_concept)} | {0}
        for i in sorted(window):
            edges.append({"src_id": cid, "dst_id": _hub_sym(i), "rel": "documents",
                          "origin": "agent", "confidence": 0.7,
                          "evidence": {"source": "agent-return"}})
    adapter.upsert_edges(edges)
    adapter.upsert_embeddings([
        {"entity_id": HUB_SEED, "vec": unit_vec(0), "model": EMBEDDING_MODEL,
         "embedded_at_sha": "s1", "entity_version": "v1"},
    ])
