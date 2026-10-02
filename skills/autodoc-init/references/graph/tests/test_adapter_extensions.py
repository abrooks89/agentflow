"""Epic 703 adapter extensions: the `find_edges` bulk edge read and the
`community.descriptor` column (migration 0003).

`find_edges` exists because community detection needs EVERY ast edge of the
corpus in one deterministic read — per-node `neighbors()` collapses parallel
rels (it reports only the edge that FIRST reached a neighbor), which would
destroy the summed-over-parallel-edges weight rule. `descriptor` exists
because scope item 3 stores the label's evidence (member count, top-degree
symbols, top path prefixes) alongside the label.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import edge_row, entity_row

from autodoc_graph.adapter import AdapterError
from autodoc_graph.sqlite_backend import _SCHEMA_PATH, SqliteGraphAdapter


def _seed(adapter):
    adapter.upsert_entities([entity_row(f"sym:corp:app/x.py:function:f{i}") for i in range(4)])
    ids = [f"sym:corp:app/x.py:function:f{i}" for i in range(4)]
    adapter.upsert_edges([
        edge_row(ids[0], ids[1], "calls"),
        edge_row(ids[0], ids[1], "imports"),   # parallel rel, same pair
        edge_row(ids[1], ids[2], "defines"),
        edge_row(ids[2], ids[3], "routes_to"),
    ])
    return ids


# ---------------------------------------------------------------- find_edges
def test_find_edges_returns_all_edges_deterministically(adapter):
    ids = _seed(adapter)
    edges = adapter.find_edges({})
    assert len(edges) == 4
    # deterministic order: (rel, src_id, dst_id)
    keys = [(e["rel"], e["src_id"], e["dst_id"]) for e in edges]
    assert keys == sorted(keys)
    # parallel rels between one pair are distinct rows
    pair = [e for e in edges if e["src_id"] == ids[0] and e["dst_id"] == ids[1]]
    assert sorted(e["rel"] for e in pair) == ["calls", "imports"]


def test_find_edges_filters_by_rel_and_origin(adapter):
    _seed(adapter)
    assert [e["rel"] for e in adapter.find_edges({"rel": "calls"})] == ["calls"]
    assert len(adapter.find_edges({"origin": "ast"})) == 4
    assert adapter.find_edges({"origin": "agent"}) == []


def test_find_edges_filters_by_endpoint(adapter):
    ids = _seed(adapter)
    out = adapter.find_edges({"src_id": ids[2]})
    assert [(e["src_id"], e["dst_id"]) for e in out] == [(ids[2], ids[3])]


def test_find_edges_unknown_filter_key_is_loud(adapter):
    with pytest.raises(AdapterError, match="unknown"):
        adapter.find_edges({"nope": 1})


def test_find_edges_deserializes_evidence(adapter):
    _seed(adapter)
    e = adapter.find_edges({"rel": "defines"})[0]
    assert e["evidence"] == {"file": "app/x.py", "line": 3}


def test_find_edges_scoped_to_open_corpus(adapter, db_path):
    _seed(adapter)
    other = SqliteGraphAdapter(db_path)
    other.open("other-corp")
    try:
        assert other.find_edges({}) == []
    finally:
        other.close()


# ---------------------------------------------------------------- descriptor
def test_community_descriptor_round_trips(adapter):
    adapter.upsert_entities([entity_row("sym:corp:app/x.py:function:f0")])
    desc = {
        "member_count": 1,
        "top_symbols": ["f0"],
        "top_prefixes": ["app"],
    }
    adapter.upsert_communities([{
        "level": 0,
        "label": "app",
        "descriptor": desc,
        "member_ids": ["sym:corp:app/x.py:function:f0"],
    }])
    rows = adapter.get_communities(0)
    assert len(rows) == 1
    assert rows[0]["descriptor"] == desc
    assert rows[0]["label"] == "app"


def test_community_descriptor_optional_and_updatable(adapter):
    adapter.upsert_communities([{"level": 0, "label": "a", "member_ids": []}])
    row = adapter.get_communities(0)[0]
    assert row["descriptor"] is None
    adapter.upsert_communities([{
        "id": row["id"], "level": 0, "label": "a",
        "descriptor": {"member_count": 0, "top_symbols": [], "top_prefixes": []},
        "member_ids": [],
    }])
    assert adapter.get_communities(0)[0]["descriptor"]["member_count"] == 0


def test_upsert_communities_absent_key_preserves_explicit_none_nulls(adapter):
    """Review blocker 2: a carried-forward update must not wipe columns the
    caller did not send. Missing key = keep the stored value; explicit None =
    write NULL. Without this, every rerun would silently destroy 704's
    community reports."""
    adapter.upsert_communities([{
        "level": 0, "label": "a", "member_ids": ["x"],
        "descriptor": {"member_count": 1, "top_symbols": [], "top_prefixes": []},
    }])
    row = adapter.get_communities(0)[0]
    adapter.upsert_communities([{
        "id": row["id"], "level": 0,
        "report": "the 704-written summary", "summarized_at_sha": "abc123",
    }])
    row = adapter.get_communities(0)[0]
    assert row["report"] == "the 704-written summary"
    assert row["summarized_at_sha"] == "abc123"
    # absent keys preserved: label/member_ids/descriptor survived the update
    assert row["label"] == "a"
    assert row["member_ids"] == ["x"]
    assert row["descriptor"]["member_count"] == 1

    # a label-only update keeps the report (the rerun/naming-pass shape)
    adapter.upsert_communities([{"id": row["id"], "level": 0, "label": "b"}])
    row = adapter.get_communities(0)[0]
    assert row["label"] == "b"
    assert row["report"] == "the 704-written summary"
    assert row["summarized_at_sha"] == "abc123"

    # explicit None writes NULL (deliberate clearing still possible)
    adapter.upsert_communities([{"id": row["id"], "level": 0, "report": None}])
    row = adapter.get_communities(0)[0]
    assert row["report"] is None
    assert row["summarized_at_sha"] == "abc123"  # untouched: key was absent


def test_prune_reports_community_ids_of_deleted_entities(adapter):
    """Review finding 6a: prune must name the communities whose anchors it
    deletes — the anchor entities are still alive at prune time."""
    adapter.upsert_entities([
        entity_row("sym:corp:app/x.py:function:f0", last_seen_sha="s1"),
        entity_row("sym:corp:app/x.py:function:f1", last_seen_sha="s1"),
    ])
    adapter.upsert_communities([{
        "level": 0, "label": "x",
        "member_ids": ["sym:corp:app/x.py:function:f0", "sym:corp:app/x.py:function:f1"],
    }])
    cid = adapter.get_communities(0)[0]["id"]
    adapter.upsert_entities([
        {**entity_row("sym:corp:app/x.py:function:f0"), "community_id": cid},
        {**entity_row("sym:corp:app/x.py:function:f1"), "community_id": cid},
    ])
    # advance the corpus sha so both entities become stale candidates
    adapter.update_corpus(last_indexed_sha="s2")
    dry = adapter.prune(dry_run=True)
    assert dry["communities"] == [cid]
    wet = adapter.prune(dry_run=False)
    assert wet["communities"] == [cid]
    assert wet["deleted"] == 2


def test_descriptor_migration_applies_to_existing_v1_database(tmp_path):
    """A database created before the descriptor migration gains the column on open()."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()

    a = SqliteGraphAdapter(db)
    a.open("corp")
    try:
        a.upsert_communities([{
            "level": 0, "label": "x",
            "descriptor": {"member_count": 0, "top_symbols": [], "top_prefixes": []},
            "member_ids": [],
        }])
        assert a.get_communities(0)[0]["descriptor"]["member_count"] == 0
    finally:
        a.close()
