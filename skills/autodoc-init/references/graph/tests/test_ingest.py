"""JSONL ingest tests: validation, upsert flow, ast-edge swap, double-run no-op
(success criterion 2 at the ingest level)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from autodoc_graph.ingest import IngestError, ingest_graph, load_jsonl
from autodoc_graph.vocab import VocabError


def _entity(id, **over):
    e = {
        "type": "entity", "id": id, "tier": "symbol", "kind": "function",
        "name": id.rsplit(":", 1)[-1], "qualified_name": f"a.py:{id.rsplit(':', 1)[-1]}",
        "source_paths": ["a.py"], "span": {"file": "a.py", "start_line": 1, "end_line": 2},
        "summary": None, "origin": "ast",
    }
    e.update(over)
    return e


def _edge(src, dst, rel="calls", **over):
    e = {
        "type": "edge", "rel": rel, "src_id": src, "dst_id": dst,
        "origin": "ast", "confidence": 1.0, "evidence": {"file": "a.py", "line": 3},
    }
    e.update(over)
    return e


F = "sym:corp:a.py:function:f"
G = "sym:corp:a.py:function:g"


def _write(path, lines):
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def jsonl(tmp_path):
    return _write(tmp_path / "graph.jsonl", [
        _entity(F), _entity(G), _edge(F, G),
        {"type": "meta", "corpus": "corp", "repo": "", "sha": "sha1",
         "counts": {"entities": 2}},
    ])


# ---------------------------------------------------------------- load_jsonl
def test_load_jsonl_parses_entities_edges_meta(jsonl):
    got = load_jsonl(jsonl)
    assert len(got["entities"]) == 2
    assert len(got["edges"]) == 1
    assert got["meta"]["sha"] == "sha1"


def test_load_jsonl_unknown_line_type_is_loud(tmp_path):
    p = _write(tmp_path / "bad.jsonl", [{"type": "mystery"}])
    with pytest.raises(IngestError, match="mystery"):
        load_jsonl(p)


def test_load_jsonl_unknown_rel_is_loud(tmp_path):
    p = _write(tmp_path / "bad.jsonl", [_entity(F), _entity(G),
                                        _edge(F, G, rel="vibes_with")])
    with pytest.raises(VocabError, match="CLOSED"):
        load_jsonl(p)


def test_load_jsonl_ast_confidence_must_be_exact(tmp_path):
    p = _write(tmp_path / "bad.jsonl", [_entity(F), _entity(G),
                                        _edge(F, G, confidence=0.9)])
    with pytest.raises(VocabError):
        load_jsonl(p)


def test_load_jsonl_dreamed_cap_enforced(tmp_path):
    p = _write(tmp_path / "bad.jsonl",
               [_entity(F), _entity(G),
                _edge(F, G, rel="related", origin="dreamed", confidence=0.8)])
    with pytest.raises(VocabError, match="dreamed"):
        load_jsonl(p)


def test_load_jsonl_malformed_json_is_loud_with_line_number(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"type": "entity"\n', encoding="utf-8")
    with pytest.raises(IngestError, match="line 1"):
        load_jsonl(p)


# ---------------------------------------------------------------- ingest_graph
def test_ingest_populates_graph_and_stamps_sha(adapter, jsonl):
    report = ingest_graph(adapter, jsonl)
    assert report["sha"] == "sha1"
    assert report["entities"]["inserted"] == 2
    assert report["edges"] == {"deleted": 0, "inserted": 1}
    s = adapter.stats()
    assert s["entities"] == 2
    assert s["edges_by_rel"] == {"calls": 1}
    assert s["last_indexed_sha"] == "sha1"
    got = adapter.get_entity(F)
    assert got["first_seen_sha"] == "sha1"
    assert got["last_seen_sha"] == "sha1"


def test_double_ingest_is_a_noop(adapter, jsonl):
    ingest_graph(adapter, jsonl)
    stats1 = adapter.stats()
    report2 = ingest_graph(adapter, jsonl)
    assert report2["entities"]["inserted"] == 0
    assert report2["entities"]["updated"] == 0
    assert report2["entities"]["unchanged"] == 2
    assert adapter.stats() == stats1  # identical stats — THE regeneration test


def test_ingest_new_sha_restamps_without_duplicating(adapter, tmp_path, jsonl):
    ingest_graph(adapter, jsonl)
    moved = _write(tmp_path / "graph2.jsonl", [
        _entity(F), _entity(G), _edge(F, G),
        {"type": "meta", "corpus": "corp", "repo": "", "sha": "sha2", "counts": {}},
    ])
    report = ingest_graph(adapter, moved)
    assert report["entities"]["inserted"] == 0
    s = adapter.stats()
    assert s["entities"] == 2
    assert s["last_indexed_sha"] == "sha2"
    assert adapter.get_entity(F)["last_seen_sha"] == "sha2"
    assert adapter.get_entity(F)["first_seen_sha"] == "sha1"


def test_ingest_replaces_ast_edges_but_preserves_agent_edges(adapter, jsonl):
    ingest_graph(adapter, jsonl)
    adapter.upsert_edges([{
        "src_id": F, "dst_id": G, "rel": "related",
        "origin": "agent", "confidence": 0.7, "evidence": "hand-curated",
    }])
    report = ingest_graph(adapter, jsonl)
    assert report["edges"] == {"deleted": 1, "inserted": 1}
    s = adapter.stats()
    assert s["edges_by_rel"] == {"calls": 1, "related": 1}


def test_ingest_missing_meta_requires_explicit_sha_and_corpus(adapter, tmp_path):
    p = _write(tmp_path / "nometa.jsonl", [_entity(F)])
    with pytest.raises(IngestError, match="sha"):
        ingest_graph(adapter, p)
    with pytest.raises(IngestError, match="corpus"):
        ingest_graph(adapter, p, sha="forced")
    ingest_graph(adapter, p, sha="forced", corpus_id="corp")
    assert adapter.stats()["last_indexed_sha"] == "forced"


def test_ingest_wrong_corpus_jsonl_fails_before_any_write(adapter, tmp_path):
    # finding 4: the corpus check must run BEFORE entities are upserted —
    # a mismatched JSONL must leave the database completely untouched
    p = _write(tmp_path / "other.jsonl", [
        _entity(F),
        {"type": "meta", "corpus": "OTHER", "repo": "", "sha": "s", "counts": {}},
    ])
    with pytest.raises(IngestError, match="corpus"):
        ingest_graph(adapter, p)
    s = adapter.stats()
    assert s["entities"] == 0  # no contamination
    assert s["edges"] == 0
    assert s["last_indexed_sha"] is None


def test_ingest_sets_repo_ref_and_branch(adapter, jsonl, db_path):
    ingest_graph(adapter, jsonl, repo_ref="o/r", branch="master")
    con = sqlite3.connect(db_path)
    try:
        assert con.execute(
            "SELECT repo_ref, branch FROM corpus WHERE id='corp'").fetchone() == ("o/r", "master")
    finally:
        con.close()
