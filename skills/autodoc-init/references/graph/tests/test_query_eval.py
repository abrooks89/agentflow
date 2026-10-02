"""Epic 705 success criterion 2 tooling: the fresh labelled query set
(>= 30, half local entity-specific over the agentflow corpus, half global
breadth) and the reimplemented recall@k / MRR / confusion harness.

No other repo's eval prior art is depended on — the set
and the harness here are built fresh for this corpus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodoc_graph.vocab import EMBEDDING_DIM, EMBEDDING_MODEL
from autodoc_query.eval.deterministic import DeterministicEmbedder
from autodoc_query.eval.metrics import (
    confusion_matrix,
    match_file,
    mrr,
    ranked_files,
    recall_at_k,
    routing_accuracy,
)

QUERIES = Path(__file__).resolve().parent.parent / "autodoc_query" / "eval" / "queries.json"
REPO_ROOT = Path(__file__).resolve().parents[5]


# ---------------------------------------------------------------- the query set
def test_query_set_size_and_balance():
    entries = json.loads(QUERIES.read_text(encoding="utf-8"))
    assert len(entries) >= 30
    modes = [e["expected_mode"] for e in entries]
    assert modes.count("local") >= 15
    assert modes.count("global") >= 15


def test_query_set_schema():
    entries = json.loads(QUERIES.read_text(encoding="utf-8"))
    ids = set()
    for e in entries:
        assert set(e) >= {"id", "query", "expected_mode"}
        assert e["expected_mode"] in ("local", "global")
        assert e["id"] not in ids
        ids.add(e["id"])
        if e["expected_mode"] == "local":
            assert e.get("expected_files"), (
                f"local query {e['id']} needs expected_files for recall/MRR"
            )
        else:
            assert "expected_files" not in e


def test_local_expected_files_exist_in_this_repo():
    # the set is labelled over the AGENTFLOW corpus: every expected file must
    # actually exist here, or recall would be measured against fiction
    entries = json.loads(QUERIES.read_text(encoding="utf-8"))
    for e in entries:
        for rel in e.get("expected_files", []):
            assert (REPO_ROOT / rel).exists(), (
                f"query {e['id']}: expected file {rel} not found under "
                f"{REPO_ROOT}"
            )


# ---------------------------------------------------------------- metrics
def test_match_file_is_suffix_based_and_separator_safe():
    assert match_file("skills/x/graph/autodoc_graph/records.py",
                      "autodoc_graph/records.py")
    assert match_file("skills\\x\\graph\\autodoc_graph\\records.py",
                      "autodoc_graph/records.py")
    assert not match_file("autodoc_graph/records.py", "other/records.py")
    # suffix match respects path boundaries: 'x_records.py' is not records.py
    assert not match_file("autodoc_graph/x_records.py",
                          ["autodoc_graph", "records.py"][-1])


def test_recall_at_k():
    ranked = ["a/one.py", "b/two.py", "c/three.py"]
    assert recall_at_k(ranked, ["a/one.py"], 1) == 1.0
    assert recall_at_k(ranked, ["c/three.py"], 2) == 0.0
    assert recall_at_k(ranked, ["c/three.py"], 5) == 1.0
    assert recall_at_k(ranked, ["a/one.py", "z/none.py"], 5) == 0.5
    assert recall_at_k([], ["a/one.py"], 5) == 0.0


def test_mrr():
    assert mrr(["a/one.py", "b/two.py"], ["a/one.py"]) == 1.0
    assert mrr(["x.py", "b/two.py"], ["b/two.py"]) == 0.5
    assert mrr(["x.py"], ["missing.py"]) == 0.0


def test_ranked_files_dedupes_in_rank_order():
    items = [
        {"source_paths": ["a/one.py"]},
        {"source_paths": ["a/one.py", "b/two.py"]},
        {"source_paths": ["c/three.py"]},
    ]
    assert ranked_files(items) == ["a/one.py", "b/two.py", "c/three.py"]


def test_confusion_and_accuracy():
    rows = [
        {"expected_mode": "local", "predicted_mode": "local"},
        {"expected_mode": "local", "predicted_mode": "global"},
        {"expected_mode": "global", "predicted_mode": "global"},
        {"expected_mode": "global", "predicted_mode": "global"},
    ]
    conf = confusion_matrix(rows)
    assert conf == {"local": {"local": 1, "global": 1},
                    "global": {"local": 0, "global": 2}}
    assert routing_accuracy(rows) == pytest.approx(0.75)


# ---------------------------------------------------------------- deterministic embedder
def test_deterministic_embedder_contract():
    emb = DeterministicEmbedder()
    assert emb.model == EMBEDDING_MODEL  # store-compatible, loudly non-semantic
    v1, v2 = emb.embed_texts(["write_records validates pages",
                              "write_records validates pages"])
    assert v1 == v2 and len(v1) == EMBEDDING_DIM
    norm = sum(x * x for x in v1) ** 0.5
    assert norm == pytest.approx(1.0)


def test_deterministic_embedder_lexical_overlap_beats_disjoint():
    emb = DeterministicEmbedder()
    q, near, far = emb.embed_texts([
        "how does write_records validate community reports?",
        "function write_records validate reports pages entities",
        "css grid layout flexbox styling colors",
    ])
    def cos(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))
    assert cos(q, near) > cos(q, far)
