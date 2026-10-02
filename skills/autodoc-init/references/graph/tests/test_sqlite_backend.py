"""SQLite backend contract tests: schema, upserts, atomic swap, traversal,
prune discipline, stats (epic 702 success criteria 2, 5 among others)."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import edge_row, entity_row

import autodoc_graph.sqlite_backend as sb
from autodoc_graph.adapter import AdapterError, GraphAdapter
from autodoc_graph.sqlite_backend import SqliteGraphAdapter
from autodoc_graph.vocab import VocabError


# ---------------------------------------------------------------- schema / lifecycle
def test_open_creates_schema_and_corpus_row(db_path):
    a = SqliteGraphAdapter(db_path)
    a.open("corp")
    try:
        assert a.stats()["entities"] == 0
    finally:
        a.close()
    con = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"corpus", "entity", "edge", "community", "embedding"} <= tables
        assert con.execute("SELECT id FROM corpus").fetchall() == [("corp",)]
        # v1 = schema.sql baseline; v2 = 0002_add_entity_version (finding 6);
        # v3 = 0003_community_descriptor (epic 703)
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        con.close()


def test_wal_mode_is_enabled(adapter, db_path):
    con = sqlite3.connect(db_path)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        con.close()


def test_reopen_is_idempotent(db_path):
    for _ in range(2):
        a = SqliteGraphAdapter(db_path)
        a.open("corp")
        a.close()


def test_is_a_graph_adapter(adapter):
    assert isinstance(adapter, GraphAdapter)


def test_operations_before_open_fail_loudly(db_path):
    a = SqliteGraphAdapter(db_path)
    with pytest.raises(AdapterError, match="open"):
        a.stats()


def test_migration_runner_applies_pending_migrations_in_order(db_path, tmp_path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0002_add_note.sql").write_text(
        "ALTER TABLE corpus ADD COLUMN note TEXT;", encoding="utf-8")
    (mdir / "0003_add_note2.sql").write_text(
        "ALTER TABLE corpus ADD COLUMN note2 TEXT;", encoding="utf-8")
    a = SqliteGraphAdapter(db_path, migrations_dir=mdir)
    a.open("corp")
    a.close()
    con = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(corpus)")}
        assert {"note", "note2"} <= cols
        # 3, not the schema's version: this test runs against its own two-file
        # fixture migrations dir, so the number is 1 + len(fixtures) and does
        # NOT move when a real migration is added to the package.
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        con.close()


# ---------------------------------------------------------------- entity upserts
def test_upsert_entities_insert_then_noop(adapter):
    rows = [entity_row("sym:corp:app/x.py:function:f"),
            entity_row("sym:corp:app/x.py:function:g")]
    first = adapter.upsert_entities(rows)
    assert first == {"inserted": 2, "updated": 0, "unchanged": 0}
    second = adapter.upsert_entities(rows)
    assert second == {"inserted": 0, "updated": 0, "unchanged": 2}


def test_upsert_entities_detects_content_change(adapter):
    row = entity_row("sym:corp:app/x.py:function:f")
    adapter.upsert_entities([row])
    row2 = dict(row, summary="def f(x):")
    assert adapter.upsert_entities([row2]) == {"inserted": 0, "updated": 1, "unchanged": 0}
    assert adapter.get_entity(row["id"])["summary"] == "def f(x):"


def test_first_seen_sha_survives_updates_last_seen_advances(adapter):
    row = entity_row("sym:corp:app/x.py:function:f", last_seen_sha="aaa")
    adapter.upsert_entities([row])
    adapter.upsert_entities([dict(row, last_seen_sha="bbb")])
    got = adapter.get_entity(row["id"])
    assert got["first_seen_sha"] == "aaa"
    assert got["last_seen_sha"] == "bbb"


def test_sha_restamp_with_identical_content_counts_unchanged(adapter):
    # the double-run guarantee: same content re-stamped => unchanged (not updated)
    row = entity_row("sym:corp:app/x.py:function:f", last_seen_sha="aaa")
    adapter.upsert_entities([row])
    res = adapter.upsert_entities([dict(row, last_seen_sha="bbb")])
    assert res == {"inserted": 0, "updated": 0, "unchanged": 1}
    assert adapter.get_entity(row["id"])["last_seen_sha"] == "bbb"


def test_entity_json_columns_roundtrip(adapter):
    row = entity_row("sym:corp:app/x.py:function:f",
                     frontmatter={"type": "service", "title": "X"})
    adapter.upsert_entities([row])
    got = adapter.get_entity(row["id"])
    assert got["source_paths"] == ["app/x.py"]
    assert got["span"] == {"file": "app/x.py", "start_line": 1, "end_line": 2}
    assert got["frontmatter"] == {"type": "service", "title": "X"}


def test_upsert_entity_bad_tier_or_origin_is_loud(adapter):
    with pytest.raises(VocabError):
        adapter.upsert_entities([entity_row("sym:corp:a:function:f", tier="vibes")])
    with pytest.raises(VocabError):
        adapter.upsert_entities([entity_row("sym:corp:a:function:f", origin="dreamed")])


# ---------------------------------------------------------------- edge upserts
def _two_entities(adapter):
    a = entity_row("sym:corp:app/x.py:function:f")
    b = entity_row("sym:corp:app/x.py:function:g")
    adapter.upsert_entities([a, b])
    return a["id"], b["id"]


def test_upsert_edges_idempotent_by_unique_tuple(adapter):
    f, g = _two_entities(adapter)
    rows = [edge_row(f, g)]
    assert adapter.upsert_edges(rows)["inserted"] == 1
    res = adapter.upsert_edges(rows)
    assert res["inserted"] == 0
    assert res["unchanged"] == 1


def test_upsert_edges_rejects_unknown_rel_and_bad_confidence(adapter):
    f, g = _two_entities(adapter)
    with pytest.raises(VocabError, match="CLOSED"):
        adapter.upsert_edges([edge_row(f, g, rel="vibes_with")])
    with pytest.raises(VocabError):
        adapter.upsert_edges([edge_row(f, g, origin="ast", confidence=0.5)])
    with pytest.raises(VocabError, match="dreamed"):
        adapter.upsert_edges([edge_row(f, g, rel="related", origin="dreamed", confidence=0.9)])


def test_agent_edge_defaults_confidence(adapter):
    f, g = _two_entities(adapter)
    row = edge_row(f, g, rel="related", origin="agent")
    del row["confidence"]
    adapter.upsert_edges([row])
    con = sqlite3.connect(adapter.db_path)
    try:
        assert con.execute("SELECT confidence FROM edge").fetchone()[0] == 0.7
    finally:
        con.close()


def test_dangling_edge_endpoint_is_loud(adapter):
    f, _ = _two_entities(adapter)
    with pytest.raises(AdapterError, match="ghost"):
        adapter.upsert_edges([edge_row(f, "sym:corp:nope:function:ghost")])


# ---------------------------------------------------------------- replace_ast_edges
def test_replace_ast_edges_swaps_ast_and_preserves_agent(adapter):
    f, g = _two_entities(adapter)
    adapter.upsert_edges([edge_row(f, g, rel="calls")])
    adapter.upsert_edges([edge_row(f, g, rel="related", origin="agent", confidence=0.7)])
    res = adapter.replace_ast_edges("corp", [edge_row(g, f, rel="calls")])
    assert res == {"deleted": 1, "inserted": 1}
    s = adapter.stats()
    assert s["edges_by_rel"]["calls"] == 1
    assert s["edges_by_rel"]["related"] == 1  # agent edge survived


def test_replace_ast_edges_rejects_non_ast_rows_atomically(adapter):
    f, g = _two_entities(adapter)
    adapter.upsert_edges([edge_row(f, g, rel="calls")])
    bad = [edge_row(g, f, rel="calls"),
           edge_row(f, g, rel="related", origin="agent", confidence=0.7)]
    with pytest.raises(VocabError, match="ast"):
        adapter.replace_ast_edges("corp", bad)
    # the failed swap must not have deleted the existing ast edges (atomicity)
    assert adapter.stats()["edges_by_rel"]["calls"] == 1


def test_replace_ast_edges_requires_the_open_corpus(adapter):
    with pytest.raises(AdapterError, match="corpus"):
        adapter.replace_ast_edges("other-corpus", [])


# ---------------------------------------------------------------- find / neighbors
def test_find_entities_filters(adapter):
    adapter.upsert_entities([
        entity_row("sym:corp:a.py:function:f", kind="function"),
        entity_row("sym:corp:a.py:class:Klass", kind="class", name="Klass"),
        entity_row("sym:corp:b.py:function:g", source_paths=["b.py"]),
    ])
    assert len(adapter.find_entities({"kind": "class"})) == 1
    assert len(adapter.find_entities({"path": "b.py"})) == 1
    assert len(adapter.find_entities({"name_like": "las"})) == 1
    assert len(adapter.find_entities({"tier": "symbol", "limit": 2})) == 2
    # epic 705 closed-set extension: qualified_name exact (the spec's router
    # resolves symbol tokens on name / qualified_name)
    hits = adapter.find_entities({"qualified_name": "app/x.py:Klass"})
    assert [e["id"] for e in hits] == ["sym:corp:a.py:class:Klass"]
    assert adapter.find_entities({"qualified_name": "app/x.py:Klas"}) == []


def test_find_entities_unknown_key_is_loud(adapter):
    with pytest.raises(AdapterError, match="unknown filter"):
        adapter.find_entities({"vibe": "good"})


# ------------------------------------------------ frontmatter containment (885)


def _fm_corpus(adapter):
    """Three rows whose frontmatter differs in the ways the contract cares about."""
    adapter.upsert_entities([
        entity_row("doc:a", frontmatter={"tier": "draft", "version": 3, "owner": "ab"}),
        entity_row("doc:b", frontmatter={"tier": "draft", "version": 4}),
        entity_row("doc:c", frontmatter={"tier": "active", "version": 3}),
        entity_row("doc:none"),  # frontmatter IS NULL — must never match a constraint
    ])


def _ids(rows):
    return sorted(r["id"] for r in rows)


def test_frontmatter_containment_single_and_multiple_keys(adapter):
    _fm_corpus(adapter)
    assert _ids(adapter.find_entities({"frontmatter": {"tier": "draft"}})) == ["doc:a", "doc:b"]
    # Multiple keys AND together; a partial match on a row with EXTRA keys still
    # matches — that is what containment means.
    assert _ids(
        adapter.find_entities({"frontmatter": {"tier": "draft", "version": 3}})
    ) == ["doc:a"]


def test_frontmatter_mismatched_value_and_missing_key_do_not_match(adapter):
    _fm_corpus(adapter)
    assert adapter.find_entities({"frontmatter": {"tier": "retired"}}) == []
    assert adapter.find_entities({"frontmatter": {"nosuchkey": "x"}}) == []
    # A NULL frontmatter row is absent from every constrained result.
    assert "doc:none" not in _ids(adapter.find_entities({"frontmatter": {"tier": "draft"}}))


def test_frontmatter_empty_dict_matches_every_row(adapter):
    _fm_corpus(adapter)
    assert _ids(adapter.find_entities({"frontmatter": {}})) == _ids(adapter.find_entities({}))


def test_frontmatter_composes_with_the_other_filter_keys(adapter):
    _fm_corpus(adapter)
    hits = adapter.find_entities({"frontmatter": {"tier": "draft"}, "limit": 1})
    assert len(hits) == 1
    # Deterministic id-ordering is unaffected by the new clause.
    assert _ids(adapter.find_entities({"frontmatter": {"version": 3}})) == ["doc:a", "doc:c"]


def test_unknown_filter_key_is_STILL_loud_after_the_addition(adapter):
    """Adding to a closed set is exactly where the guard gets loosened by accident."""
    with pytest.raises(AdapterError, match="unknown filter"):
        adapter.find_entities({"vibe": "good"})
    # And the new key must not have opened a nested namespace either.
    with pytest.raises(AdapterError, match="unknown filter"):
        adapter.find_entities({"frontmatter.tier": "draft"})


def test_frontmatter_nested_and_array_matching_are_REFUSED(adapter):
    """Pin the documented boundary, so it is deliberate rather than incidental.

    The ABC contract says top-level keys, exact equality, no nested/array
    partials - Postgres ``@>`` would give them free while SQLite's
    json_extract would not, and a capability that differs per backend is worse
    than one that exists on neither.

    The refusal is the contract, not a convenience: binding a dict/list raises
    sqlite3.ProgrammingError from the DRIVER (loud, but a backend internal
    escaping through the seam), and "matches nothing" would be worse still -
    an empty result reads as "no such document" and the caller never learns the
    query could not express what they asked.
    """
    adapter.upsert_entities([
        entity_row("doc:nested", frontmatter={"meta": {"tier": "draft"}, "tags": ["x", "y"]}),
    ])
    with pytest.raises(AdapterError, match="nested-object and array"):
        adapter.find_entities({"frontmatter": {"meta": {"tier": "draft"}}})
    with pytest.raises(AdapterError, match="nested-object and array"):
        adapter.find_entities({"frontmatter": {"tags": ["x"]}})
    # NOT a sqlite3 error leaking through the adapter seam.
    with pytest.raises(AdapterError):
        adapter.find_entities({"frontmatter": {"tags": ["x"]}})
    # Positive control: the row IS reachable by a top-level scalar, so the
    # raises above are about nesting, not an empty or unwritten fixture.
    assert _ids(adapter.find_entities({"frontmatter": {"meta": None}})) == []
    assert "doc:nested" in _ids(adapter.find_entities({"frontmatter": {}}))


def test_frontmatter_key_match_is_exact_not_a_pattern(adapter):
    """Keys match EXACTLY - not case-folded, and never as a wildcard pattern.

    Loosening the key comparison (`=` -> `LIKE`) is invisible to every other
    test here: it still finds the right row for ordinary keys. It would also
    silently make `tier` match `TIER`, and make `%` and `_` wildcards.
    """
    adapter.upsert_entities([
        entity_row("doc:case", frontmatter={"TIER": "shouty", "a_b": "under", "pre_fix": "x"}),
    ])
    # Case-insensitive key matching would return doc:case here.
    assert adapter.find_entities({"frontmatter": {"tier": "shouty"}}) == []
    # '_' is a single-char wildcard under LIKE; it must be a literal here.
    assert adapter.find_entities({"frontmatter": {"a%": "under"}}) == []
    assert _ids(adapter.find_entities({"frontmatter": {"a_b": "under"}})) == ["doc:case"]
    # Positive control: the row exists and is reachable by its exact keys.
    assert _ids(adapter.find_entities({"frontmatter": {"TIER": "shouty"}})) == ["doc:case"]


def test_frontmatter_non_dict_filter_is_loud(adapter):
    _fm_corpus(adapter)
    with pytest.raises(AdapterError, match="takes a dict"):
        adapter.find_entities({"frontmatter": "draft"})


def test_frontmatter_key_cannot_rewrite_the_json_path(adapter):
    """The path is BOUND, not interpolated — a key with a quote, a dot or a '$'
    is data, never path syntax. A dotted key is a literal key, NOT a nested
    traversal."""
    adapter.upsert_entities([
        entity_row("doc:odd", frontmatter={
            'a.b': "dotted", 'q"uote': "quoted", "$x": "dollar", "it's": "apostrophe",
        }),
    ])
    assert _ids(adapter.find_entities({"frontmatter": {"a.b": "dotted"}})) == ["doc:odd"]
    assert _ids(adapter.find_entities({"frontmatter": {'q"uote': "quoted"}})) == ["doc:odd"]
    assert _ids(adapter.find_entities({"frontmatter": {"$x": "dollar"}})) == ["doc:odd"]
    # The apostrophe is the character that separates a BOUND value from an
    # interpolated one - splice this key into the SQL text and it is a syntax
    # error, not a near-miss.
    assert _ids(adapter.find_entities({"frontmatter": {"it's": "apostrophe"}})) == ["doc:odd"]


def test_neighbors_depth_and_rel_filter(adapter):
    ids = [f"sym:corp:a.py:function:{n}" for n in ("f", "g", "h")]
    adapter.upsert_entities([entity_row(i) for i in ids])
    adapter.upsert_edges([edge_row(ids[0], ids[1], rel="calls"),
                          edge_row(ids[1], ids[2], rel="imports")])
    d1 = adapter.neighbors(ids[0])
    assert [n["entity"]["id"] for n in d1] == [ids[1]]
    assert d1[0]["direction"] == "out"
    d2 = adapter.neighbors(ids[0], depth=2)
    assert [n["entity"]["id"] for n in d2] == [ids[1], ids[2]]
    assert d2[1]["depth"] == 2
    only_calls = adapter.neighbors(ids[0], rels=["calls"], depth=2)
    assert [n["entity"]["id"] for n in only_calls] == [ids[1]]
    # inbound direction from the far node
    d_in = adapter.neighbors(ids[2])
    assert d_in[0]["direction"] == "in"


def test_neighbors_unknown_rel_is_loud(adapter):
    with pytest.raises(VocabError):
        adapter.neighbors("sym:corp:a.py:function:f", rels=["vibes"])


# ---------------------------------------------------------------- communities
def test_upsert_and_get_communities(adapter):
    res = adapter.upsert_communities([
        {"level": 0, "parent_id": None, "label": "core",
         "report": None, "member_ids": ["a", "b"], "summarized_at_sha": "s1"},
    ])
    assert res["inserted"] == 1
    got = adapter.get_communities(0)
    assert len(got) == 1
    assert got[0]["member_ids"] == ["a", "b"]
    cid = got[0]["id"]
    adapter.upsert_communities([{"id": cid, "level": 0, "parent_id": None,
                                 "label": "core2", "report": None,
                                 "member_ids": ["a"], "summarized_at_sha": "s2"}])
    assert adapter.get_communities(0)[0]["label"] == "core2"
    assert adapter.stats()["communities"] == 1


# ---------------------------------------------------------------- prune
def _seed_for_prune(adapter):
    live = entity_row("sym:corp:a.py:function:live", last_seen_sha="s2")
    stale = entity_row("sym:corp:a.py:function:stale", last_seen_sha="s1")
    adapter.upsert_entities([live, stale])
    adapter.upsert_edges([edge_row(live["id"], stale["id"], last_seen_sha="s1")])
    adapter.upsert_embeddings([{
        "entity_id": stale["id"], "vec": [0.0] * 768,
        "model": "nomic-embed-text", "embedded_at_sha": "s1"}])
    adapter.update_corpus(last_indexed_sha="s2")
    return live["id"], stale["id"]


def test_prune_dry_run_reports_and_deletes_nothing(adapter):
    _live, stale = _seed_for_prune(adapter)
    res = adapter.prune()  # dry_run defaults True: deletion is never automatic
    assert res["dry_run"] is True
    assert res["candidates"] == [stale]
    assert res["deleted"] == 0
    assert adapter.get_entity(stale) is not None
    assert adapter.stats()["entities"] == 2


def test_prune_real_run_deletes_candidates_and_their_edges_and_embeddings(adapter):
    live, stale = _seed_for_prune(adapter)
    res = adapter.prune(dry_run=False)
    assert res["deleted"] == 1
    assert adapter.get_entity(stale) is None
    assert adapter.get_entity(live) is not None
    s = adapter.stats()
    assert s["entities"] == 1
    assert s["edges"] == 0
    assert s["embedded"] == 0


# ---------------------------------------------------------------- stats
def test_stats_shape(adapter):
    f, g = _two_entities(adapter)
    adapter.upsert_edges([edge_row(f, g)])
    adapter.update_corpus(repo_ref="o/r", branch="main", last_indexed_sha="abc")
    s = adapter.stats()
    assert s["entities"] == 2
    assert s["edges"] == 1
    assert s["communities"] == 0
    assert s["embedded"] == 0
    assert s["last_indexed_sha"] == "abc"
    assert s["entities_by_tier"] == {"symbol": 2}
    assert s["edges_by_rel"] == {"calls": 1}


def test_corpus_scoping_isolates_corpora(db_path):
    a1 = SqliteGraphAdapter(db_path)
    a1.open("corp")
    a1.upsert_entities([entity_row("sym:corp:a.py:function:f")])
    a1.close()
    a2 = SqliteGraphAdapter(db_path)
    a2.open("other")
    try:
        assert a2.stats()["entities"] == 0
        assert a2.get_entity("sym:corp:a.py:function:f") is None
    finally:
        a2.close()


# =========================================================================== #
# Adversarial-review remediation regressions (2026-08-07)
# =========================================================================== #

# ---- finding 1: batch atomicity + close() must never commit ----------------
def _reopened(db_path):
    a = SqliteGraphAdapter(db_path)
    a.open("corp")
    return a


def test_upsert_entities_batch_with_bad_row_commits_nothing(adapter, db_path):
    rows = [entity_row("sym:corp:a.py:function:good1"),
            entity_row("sym:corp:a.py:function:good2"),
            entity_row("sym:corp:a.py:function:bad", tier="vibes")]
    with pytest.raises(VocabError):
        adapter.upsert_entities(rows)
    adapter.close()
    a = _reopened(db_path)
    try:
        assert a.stats()["entities"] == 0  # rows 1-2 must NOT survive the bad row 3
    finally:
        a.close()


def test_upsert_edges_batch_with_bad_row_commits_nothing(adapter, db_path):
    f, g = _two_entities(adapter)
    rows = [edge_row(f, g, rel="calls"),
            edge_row(g, f, rel="calls"),
            edge_row(f, g, rel="vibes_with")]
    with pytest.raises(VocabError):
        adapter.upsert_edges(rows)
    adapter.close()
    a = _reopened(db_path)
    try:
        assert a.stats()["edges"] == 0
    finally:
        a.close()


def test_upsert_embeddings_batch_with_bad_row_commits_nothing(adapter, db_path):
    f, g = _two_entities(adapter)
    rows = [
        {"entity_id": f, "vec": [0.0] * 768, "model": "nomic-embed-text",
         "embedded_at_sha": "s"},
        {"entity_id": g, "vec": [1.0], "model": "nomic-embed-text",  # bad dim
         "embedded_at_sha": "s"},
    ]
    with pytest.raises(AdapterError):
        adapter.upsert_embeddings(rows)
    adapter.close()
    a = _reopened(db_path)
    try:
        assert a.stats()["embedded"] == 0
    finally:
        a.close()


def test_sql_phase_failure_rolls_back_the_whole_entity_batch(adapter, db_path, monkeypatch):
    # force a failure AFTER row 1's INSERT executed: the batch must roll back
    real_now = sb._now
    calls = {"n": 0}

    def flaky_now():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("boom mid-batch")
        return real_now()

    monkeypatch.setattr(sb, "_now", flaky_now)
    with pytest.raises(RuntimeError, match="boom"):
        adapter.upsert_entities([entity_row("sym:corp:a.py:function:one"),
                                 entity_row("sym:corp:a.py:function:two")])
    monkeypatch.setattr(sb, "_now", real_now)
    adapter.close()
    a = _reopened(db_path)
    try:
        assert a.stats()["entities"] == 0  # row 'one' rolled back with the batch
    finally:
        a.close()


def test_close_never_commits_pending_writes(adapter, db_path):
    # write behind the public API without committing: close() must NOT flush it
    adapter._db().execute(
        "INSERT INTO entity (id, corpus_id, tier, kind, name, origin)"
        " VALUES ('sym:corp:x:function:sneak', 'corp', 'symbol', 'function', 'sneak', 'ast')"
    )
    adapter.close()
    a = _reopened(db_path)
    try:
        assert a.get_entity("sym:corp:x:function:sneak") is None
    finally:
        a.close()


# ---- finding 2: prune must never touch the concept tier --------------------
def _concept_row(id_, **over):
    row = entity_row(id_, tier="concept", kind="service", origin="agent",
                     span=None, source_paths=["docs/x.md"])
    row.update(over)
    return row


def test_prune_candidates_are_ast_origin_only(adapter):
    stale_sym = entity_row("sym:corp:a.py:function:stale", last_seen_sha="s1")
    stale_con = _concept_row("con:corp:service:repo-binding", last_seen_sha="s1")
    adapter.upsert_entities([stale_sym, stale_con])
    adapter.update_corpus(last_indexed_sha="s2")
    res = adapter.prune()
    assert res["candidates"] == [stale_sym["id"]]  # concept never a candidate


def test_prune_delete_spares_concepts_and_their_edges(adapter):
    live = entity_row("sym:corp:a.py:function:live", last_seen_sha="s2")
    stale_sym = entity_row("sym:corp:a.py:function:stale", last_seen_sha="s1")
    concept = _concept_row("con:corp:service:binding", last_seen_sha="s1")
    adapter.upsert_entities([live, stale_sym, concept])
    adapter.upsert_edges([{
        "src_id": concept["id"], "dst_id": live["id"], "rel": "documents",
        "origin": "agent", "confidence": 0.7, "evidence": "page cites symbol",
    }])
    adapter.update_corpus(last_indexed_sha="s2")
    res = adapter.prune(dry_run=False)
    assert res["deleted"] == 1
    assert adapter.get_entity(concept["id"]) is not None
    assert adapter.get_entity(stale_sym["id"]) is None
    assert adapter.stats()["edges_by_rel"] == {"documents": 1}  # tier join intact


# ---- finding 5: community_id is 703's column, never clobbered --------------
def test_reingesting_identical_row_preserves_community_id(adapter):
    row = entity_row("sym:corp:a.py:function:f")
    adapter.upsert_entities([row])
    # 703 assigns the community explicitly
    adapter.upsert_entities([dict(row, community_id=7)])
    assert adapter.get_entity(row["id"])["community_id"] == 7
    # a later extractor run re-sends the identical row WITHOUT community_id
    res = adapter.upsert_entities([row])
    assert res == {"inserted": 0, "updated": 0, "unchanged": 1}
    assert adapter.get_entity(row["id"])["community_id"] == 7  # intact


def test_explicit_community_id_change_counts_updated(adapter):
    row = entity_row("sym:corp:a.py:function:f")
    adapter.upsert_entities([row])
    res = adapter.upsert_entities([dict(row, community_id=3)])
    assert res["updated"] == 1
    assert adapter.get_entity(row["id"])["community_id"] == 3


# ---- finding 7: rel<->origin matrix + tier pairing -------------------------
def test_rel_origin_matrix_rejects_agent_ast_rels(adapter):
    f, g = _two_entities(adapter)
    for rel in ("imports", "calls", "defines", "routes_to"):
        with pytest.raises(VocabError, match="origin"):
            adapter.upsert_edges([edge_row(f, g, rel=rel, origin="agent",
                                           confidence=0.7)])


def test_rel_origin_matrix_rejects_ast_related_and_dreamed_documents(adapter):
    f, g = _two_entities(adapter)
    with pytest.raises(VocabError, match="origin"):
        adapter.upsert_edges([edge_row(f, g, rel="related", origin="ast",
                                       confidence=1.0)])
    concept = _concept_row("con:corp:service:x")
    adapter.upsert_entities([concept])
    with pytest.raises(VocabError, match="origin"):
        adapter.upsert_edges([edge_row(concept["id"], f, rel="documents",
                                       origin="dreamed", confidence=0.3)])


def test_tier_pairing_enforced_for_documents_and_depends_on(adapter):
    f, g = _two_entities(adapter)
    c1 = _concept_row("con:corp:service:one")
    c2 = _concept_row("con:corp:flow:two", kind="flow")
    adapter.upsert_entities([c1, c2])
    # documents must be concept -> symbol
    with pytest.raises(VocabError, match="tier"):
        adapter.upsert_edges([edge_row(f, g, rel="documents", origin="agent",
                                       confidence=0.7)])
    ok = adapter.upsert_edges([edge_row(c1["id"], f, rel="documents",
                                        origin="agent", confidence=0.7)])
    assert ok["inserted"] == 1
    # depends_on must be concept -> concept
    with pytest.raises(VocabError, match="tier"):
        adapter.upsert_edges([edge_row(c1["id"], f, rel="depends_on",
                                       origin="agent", confidence=0.7)])
    assert adapter.upsert_edges([edge_row(c1["id"], c2["id"], rel="depends_on",
                                          origin="agent", confidence=0.7)])["inserted"] == 1
    # related is tier-agnostic
    assert adapter.upsert_edges([edge_row(c1["id"], f, rel="related",
                                          origin="agent", confidence=0.7)])["inserted"] == 1


def test_ast_rels_require_symbol_endpoints(adapter):
    f, _ = _two_entities(adapter)
    c1 = _concept_row("con:corp:service:one")
    adapter.upsert_entities([c1])
    with pytest.raises(VocabError, match="tier"):
        adapter.upsert_edges([edge_row(c1["id"], f, rel="calls")])


# ---- finding 8: SQL bind-variable limits — chunked IN-lists ----------------
def test_prune_delete_chunks_large_candidate_sets(adapter, monkeypatch):
    monkeypatch.setattr(sb, "_SQL_CHUNK", 3)  # force many chunks
    live = entity_row("sym:corp:a.py:function:live", last_seen_sha="s2")
    adapter.upsert_entities([live])
    stale = [entity_row(f"sym:corp:a.py:function:stale{i:02d}", last_seen_sha="s1")
             for i in range(10)]
    adapter.upsert_entities(stale)
    adapter.upsert_edges([edge_row(live["id"], s["id"], last_seen_sha="s1")
                          for s in stale])
    adapter.upsert_embeddings([
        {"entity_id": s["id"], "vec": [0.0] * 768, "model": "nomic-embed-text",
         "embedded_at_sha": "s1"} for s in stale])
    adapter.update_corpus(last_indexed_sha="s2")
    res = adapter.prune(dry_run=False)
    assert res["deleted"] == 10
    s = adapter.stats()
    assert s["entities"] == 1
    assert s["edges"] == 0
    assert s["embedded"] == 0


def test_neighbors_frontier_is_chunked(adapter, monkeypatch):
    monkeypatch.setattr(sb, "_SQL_CHUNK", 2)
    center = entity_row("sym:corp:a.py:function:center")
    spokes = [entity_row(f"sym:corp:a.py:function:spoke{i}") for i in range(5)]
    far = entity_row("sym:corp:a.py:function:zfar")
    adapter.upsert_entities([center, *spokes, far])
    adapter.upsert_edges([edge_row(center["id"], s["id"]) for s in spokes])
    adapter.upsert_edges([edge_row(spokes[4]["id"], far["id"])])
    d1 = adapter.neighbors(center["id"])
    assert [n["entity"]["id"] for n in d1] == sorted(s["id"] for s in spokes)
    d2 = adapter.neighbors(center["id"], depth=2)
    assert d2[-1]["entity"]["id"] == far["id"]
    assert d2[-1]["depth"] == 2


# =========================================================================== #
# Round-2 adversarial-review remediation regressions (2026-08-07)
# =========================================================================== #

# ---- finding R2-2: upsert_communities batch atomicity ----------------------
def test_upsert_communities_batch_with_bad_row_commits_nothing(adapter, db_path):
    rows = [
        {"level": 0, "parent_id": None, "label": "good", "report": None,
         "member_ids": ["a"], "summarized_at_sha": "s1"},
        {"id": 999999, "level": 0, "parent_id": None, "label": "bad-update",
         "report": None, "member_ids": ["b"], "summarized_at_sha": "s1"},
    ]
    with pytest.raises(AdapterError, match="999999"):
        adapter.upsert_communities(rows)
    # the reviewer's escalation: a SUBSEQUENT successful method commits its own
    # work — that commit must not sweep the failed batch's pending insert along
    adapter.update_corpus(last_indexed_sha="s1")
    adapter.close()
    a = _reopened(db_path)
    try:
        assert a.get_communities(0) == []
        assert a.stats()["communities"] == 0
    finally:
        a.close()


def test_upsert_communities_missing_level_is_loud_before_any_write(adapter):
    rows = [
        {"level": 0, "parent_id": None, "label": "ok", "report": None,
         "member_ids": [], "summarized_at_sha": "s"},
        {"parent_id": None, "label": "no-level", "report": None,
         "member_ids": [], "summarized_at_sha": "s"},
    ]
    with pytest.raises(AdapterError, match="level"):
        adapter.upsert_communities(rows)
    assert adapter.stats()["communities"] == 0


# ---- finding R2-4: migration 0002 backfills entity_version -----------------
def test_migration_0002_backfills_entity_version_on_v1_db(tmp_path):
    # hand-build a v1 database (schema.sql only, no entity_version column)
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(sb._SCHEMA_PATH.read_text(encoding="utf-8"))
    con.execute("PRAGMA user_version = 1")
    eid = "sym:corp:a.py:function:f"
    con.execute("INSERT INTO corpus (id, last_indexed_sha) VALUES ('corp', 's1')")
    con.execute(
        "INSERT INTO entity (id, corpus_id, tier, kind, name, origin,"
        " last_seen_sha, updated_at) VALUES (?, 'corp', 'symbol', 'function',"
        " 'f', 'ast', 's1', '2026-01-01T00:00:00.000000+00:00')", (eid,))
    con.execute(
        "INSERT INTO embedding (entity_id, vec, model, embedded_at_sha)"
        " VALUES (?, ?, 'nomic-embed-text', 's1')", (eid, b"\x00" * (768 * 4)))
    con.commit()
    con.close()

    a = SqliteGraphAdapter(db)
    a.open("corp")  # applies migration 0002 + backfill
    try:
        # upgraded db: the stored embedding must NOT read as stale
        assert a.find_entities({"needs_embedding": True}) == []
        # a real content change afterward: exactly that entity is pending
        a.upsert_entities([entity_row(eid, summary="def f(changed):",
                                      last_seen_sha="s2")])
        pending = a.find_entities({"needs_embedding": True})
        assert [e["id"] for e in pending] == [eid]
    finally:
        a.close()


# --- retire_agent_subgraph: scoped retire of agent-written rows (downstream epic 889) ---
ROOT = "con:corp:service:binding"


def _agent_edge(src, dst, rel="part_of", **over):
    row = {"src_id": src, "dst_id": dst, "rel": rel, "origin": "agent",
           "confidence": 0.7, "evidence": "emitted by the doc agent"}
    row.update(over)
    return row


def _document(adapter, root=ROOT, children=("overview",)):
    """A root concept plus `#`-suffixed children, each part_of the root."""
    rows = [_concept_row(root)] + [_concept_row(f"{root}#{c}") for c in children]
    adapter.upsert_entities(rows)
    adapter.upsert_edges([_agent_edge(r["id"], root) for r in rows[1:]])
    return root, [r["id"] for r in rows[1:]]


def test_retire_drops_a_child_and_its_part_of_edge(adapter):
    root, (child,) = _document(adapter)
    res = adapter.retire_agent_subgraph("corp", root, [root], [])
    assert res == {"entities_deleted": 1, "edges_deleted": 1}
    assert adapter.get_entity(child) is None
    assert adapter.get_entity(root) is not None
    assert adapter.find_edges({"rel": "part_of"}) == []


def test_retire_drops_a_retracted_edge_while_root_and_child_survive(adapter):
    root, (child,) = _document(adapter, children=("overview",))
    adapter.upsert_edges([_agent_edge(child, root, rel="related")])
    # keep the child and its part_of edge; the 'related' edge was retracted
    res = adapter.retire_agent_subgraph(
        "corp", root, [root, child], [(child, root, "part_of")])
    assert res == {"entities_deleted": 0, "edges_deleted": 1}
    assert adapter.get_entity(child) is not None
    assert [e["rel"] for e in adapter.find_edges({})] == ["part_of"]


def test_retire_never_touches_ast_rows_inside_the_id_scope(adapter):
    root, _children = _document(adapter, children=())
    ast_in_scope = entity_row(f"{root}#extracted")
    other = entity_row("sym:corp:app/x.py:function:g")
    adapter.upsert_entities([ast_in_scope, other])
    adapter.upsert_edges([edge_row(ast_in_scope["id"], other["id"])])
    res = adapter.retire_agent_subgraph("corp", root, [root], [])
    assert res == {"entities_deleted": 0, "edges_deleted": 0}
    assert adapter.get_entity(ast_in_scope["id"]) is not None
    assert [e["rel"] for e in adapter.find_edges({})] == ["calls"]


def test_retire_leaves_a_sibling_document_untouched(adapter):
    root, _kids = _document(adapter)
    sib_root, (sib_child,) = _document(adapter, root="con:corp:service:other")
    res = adapter.retire_agent_subgraph("corp", root, [root], [])
    assert res == {"entities_deleted": 1, "edges_deleted": 1}
    assert adapter.get_entity(sib_root) is not None
    assert adapter.get_entity(sib_child) is not None


def test_retire_escapes_like_wildcards_in_the_root_id(adapter):
    # an unescaped '_' in the LIKE pattern would match the sibling's 'x';
    # a dropped ESCAPE clause would make the escaped pattern match NOTHING,
    # so the root's own child must be retired to pin both halves
    root, (child,) = _document(adapter, root="con:corp:service:a_b")
    sib_root, (sib_child,) = _document(adapter, root="con:corp:service:axb")
    res = adapter.retire_agent_subgraph("corp", root, [root], [])
    assert res == {"entities_deleted": 1, "edges_deleted": 1}
    assert adapter.get_entity(child) is None
    assert adapter.get_entity(sib_child) is not None
    assert adapter.get_entity(sib_root) is not None


def test_retire_rejects_a_corpus_mismatch(adapter):
    root, _c = _document(adapter, children=())
    with pytest.raises(AdapterError, match="retire_agent_subgraph corpus mismatch"):
        adapter.retire_agent_subgraph("other", root, [root], [])


def test_retire_rejects_a_non_concept_root(adapter):
    f, _g = _two_entities(adapter)
    with pytest.raises(VocabError, match="concept"):
        adapter.retire_agent_subgraph("corp", f, [], [])


def test_retire_with_nothing_out_of_scope_is_a_noop(adapter):
    root, (child,) = _document(adapter)
    res = adapter.retire_agent_subgraph(
        "corp", root, [root, child], [(child, root, "part_of")])
    assert res == {"entities_deleted": 0, "edges_deleted": 0}
    assert adapter.stats()["entities"] == 2
    assert adapter.stats()["edges"] == 1


class _FailingOn:
    """Connection proxy that raises on the first statement matching `needle`."""

    def __init__(self, con, needle):
        self._con = con
        self._needle = needle

    def execute(self, sql, *args):
        if self._needle in sql:
            raise sqlite3.OperationalError("forced failure")
        return self._con.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._con, name)


def test_retire_is_atomic_when_a_statement_fails(adapter):
    root, (child,) = _document(adapter)
    real = adapter._db()
    adapter._con = _FailingOn(real, "DELETE FROM entity")
    try:
        with pytest.raises(AdapterError, match="rolled back"):
            adapter.retire_agent_subgraph("corp", root, [root], [])
    finally:
        adapter._con = real
    # the edge delete that DID run must have rolled back with the entity delete
    assert adapter.get_entity(child) is not None
    assert [e["rel"] for e in adapter.find_edges({})] == ["part_of"]


# --------------------------------------------------------------------------- #
# corpus.project - the room's binding to a project codebase (E-0200 item 4b)
# --------------------------------------------------------------------------- #
def test_update_corpus_round_trips_project(db_path):
    a = SqliteGraphAdapter(db_path)
    a.open("corp")
    a.update_corpus(repo_ref="o/r", project="agentflow")
    assert a.get_corpus()["project"] == "agentflow"
    a.close()


def test_update_corpus_leaves_project_alone_when_not_passed(db_path):
    """Only non-None fields are written. A later build that omits --project must not
    silently unbind a room that was already told which project it documents."""
    a = SqliteGraphAdapter(db_path)
    a.open("corp")
    a.update_corpus(project="agentflow")
    a.update_corpus(last_indexed_sha="s2")
    assert a.get_corpus()["project"] == "agentflow"
    a.close()


def test_migration_0004_adds_project_to_an_existing_corpus(tmp_path):
    """The upgrade path, not just the fresh one: a store built before this migration
    gains the column and keeps every row it had."""
    p = tmp_path / "old.db"
    a = SqliteGraphAdapter(p)
    a.open("corp")
    a.update_corpus(repo_ref="o/r", last_indexed_sha="abc")
    a.close()

    con = sqlite3.connect(p)
    con.execute("ALTER TABLE corpus DROP COLUMN project")
    con.execute("PRAGMA user_version = 3")
    con.commit()
    con.close()

    a2 = SqliteGraphAdapter(p)
    a2.open("corp")
    row = a2.get_corpus()
    assert row["project"] is None       # present, and honestly empty
    assert row["repo_ref"] == "o/r"     # nothing lost on the way up
    a2.close()


# ------------------------------------------------ open(create=False) (gh#459)
def test_open_without_create_refuses_a_missing_path(db_path):
    a = SqliteGraphAdapter(db_path)
    with pytest.raises(AdapterError, match="does not exist"):
        a.open("corp", create=False)
    assert not db_path.exists()
    assert a.corpus_id is None


def test_open_without_create_refuses_a_zero_byte_file(db_path):
    db_path.write_bytes(b"")
    with pytest.raises(AdapterError, match="0 bytes"):
        SqliteGraphAdapter(db_path).open("corp", create=False)
    assert db_path.stat().st_size == 0


def test_open_without_create_refuses_a_non_sqlite_file(db_path):
    db_path.write_bytes(b"not sqlite at all, but long enough to hold a header")
    with pytest.raises(AdapterError, match="not a SQLite database"):
        SqliteGraphAdapter(db_path).open("corp", create=False)


def test_open_without_create_refuses_an_unversioned_sqlite_file(db_path):
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    con.close()
    before = db_path.read_bytes()
    with pytest.raises(AdapterError, match="not a graph store"):
        SqliteGraphAdapter(db_path).open("corp", create=False)
    # the refusal leaves the foreign file exactly as it was: no WAL switch, no schema
    assert db_path.read_bytes() == before
    assert not db_path.with_name(db_path.name + "-wal").exists()
    assert not db_path.with_name(db_path.name + "-shm").exists()


def test_open_without_create_refuses_a_directory(db_path):
    db_path.mkdir()
    with pytest.raises(AdapterError, match="is not a file"):
        SqliteGraphAdapter(db_path).open("corp", create=False)
    assert db_path.is_dir()


def test_open_without_create_opens_an_existing_store(db_path):
    a = SqliteGraphAdapter(db_path)
    a.open("corp")
    a.close()
    b = SqliteGraphAdapter(db_path)
    b.open("corp", create=False)
    try:
        assert b.stats()["entities"] == 0
    finally:
        b.close()


def test_a_freshly_opened_store_is_never_zero_bytes(db_path):
    # The 0-byte refusal relies on this: open() writes page 1 (the WAL switch)
    # before returning, so a store under construction already carries the header.
    a = SqliteGraphAdapter(db_path)
    a.open("corp")
    try:
        with open(db_path, "rb") as fh:
            assert fh.read(16) == b"SQLite format 3\x00"
    finally:
        a.close()
