"""`graph` CLI tests — build / stats / prune / verify, all in-process."""

from __future__ import annotations

import json
import sqlite3

import pytest

from autodoc_graph.cli import main
from autodoc_graph.sqlite_backend import SqliteGraphAdapter

# gh#152: `build` refuses a corpus it would leave unrenderable, so every FIRST build
# below carries the attribution the renderer needs. Held in one constant because these
# tests are about ingest/prune/verify/community behaviour, not attribution -- the
# refusal itself is exercised in tests/test_cli_attribution.py.
ATTRIB = ["--repo-ref", "owner/corp", "--project", "demo"]


def _entity(id, **over):
    e = {
        "type": "entity", "id": id, "tier": "symbol", "kind": "function",
        "name": id.rsplit(":", 1)[-1], "qualified_name": None,
        "source_paths": ["a.py"], "span": {"file": "a.py", "start_line": 1, "end_line": 2},
        "summary": None, "origin": "ast",
    }
    e.update(over)
    return e


F = "sym:corp:a.py:function:f"
G = "sym:corp:a.py:function:g"


def _jsonl(tmp_path, sha="sha1", entities=None):
    entities = entities if entities is not None else [_entity(F), _entity(G)]
    lines = [*entities,
             {"type": "edge", "rel": "calls", "src_id": F, "dst_id": G,
              "origin": "ast", "confidence": 1.0,
              "evidence": {"file": "a.py", "line": 3}},
             {"type": "meta", "corpus": "corp", "repo": "", "sha": sha, "counts": {}}]
    if entities == []:
        lines = [{"type": "meta", "corpus": "corp", "repo": "", "sha": sha, "counts": {}}]
    p = tmp_path / f"graph-{sha}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def test_build_then_stats(tmp_path, capsys):
    db = tmp_path / "g.db"
    rc = main(["build", "--jsonl", str(_jsonl(tmp_path)), "--db", str(db),
               "--corpus", "corp", *ATTRIB])
    assert rc == 0
    capsys.readouterr()  # discard build output; stats prints a single JSON blob
    rc = main(["stats", "--db", str(db), "--corpus", "corp"])
    assert rc == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["entities"] == 2
    assert stats["edges_by_rel"] == {"calls": 1}
    assert stats["last_indexed_sha"] == "sha1"


def test_build_twice_reports_noop(tmp_path, capsys):
    db = tmp_path / "g.db"
    j = _jsonl(tmp_path)
    assert main(["build", "--jsonl", str(j), "--db", str(db),
                 "--corpus", "corp", *ATTRIB]) == 0
    capsys.readouterr()
    # The rebuild passes no attribution on purpose: update_corpus skips None
    # columns, so the stored values survive and the guard must permit this.
    assert main(["build", "--jsonl", str(j), "--db", str(db), "--corpus", "corp"]) == 0
    out = capsys.readouterr().out
    assert '"inserted": 0' in out


def test_build_missing_jsonl_fails_loudly(tmp_path, capsys):
    rc = main(["build", "--jsonl", str(tmp_path / "nope.jsonl"),
               "--db", str(tmp_path / "g.db"), "--corpus", "corp", *ATTRIB])
    assert rc == 1
    assert "nope.jsonl" in capsys.readouterr().err


def test_build_embed_unreachable_ollama_names_url(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AUTODOC_OLLAMA_URL", "http://127.0.0.1:9")
    db = tmp_path / "g.db"
    rc = main(["build", "--jsonl", str(_jsonl(tmp_path)), "--db", str(db),
               "--corpus", "corp", "--embed", *ATTRIB])
    assert rc == 1
    assert "127.0.0.1:9" in capsys.readouterr().err


def test_prune_default_is_dry_run(tmp_path, capsys):
    db = tmp_path / "g.db"
    main(["build", "--jsonl", str(_jsonl(tmp_path, sha="sha1")), "--db", str(db),
          "--corpus", "corp", *ATTRIB])
    # second run without f/g: both become stale candidates
    main(["build", "--jsonl", str(_jsonl(tmp_path, sha="sha2", entities=[])),
          "--db", str(db), "--corpus", "corp"])
    capsys.readouterr()
    rc = main(["prune", "--db", str(db), "--corpus", "corp"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert F in out
    a = SqliteGraphAdapter(db)
    a.open("corp")
    try:
        assert a.stats()["entities"] == 2  # nothing deleted
    finally:
        a.close()


def test_prune_delete_requires_explicit_flag_and_deletes(tmp_path, capsys):
    db = tmp_path / "g.db"
    main(["build", "--jsonl", str(_jsonl(tmp_path, sha="sha1")), "--db", str(db),
          "--corpus", "corp", *ATTRIB])
    main(["build", "--jsonl", str(_jsonl(tmp_path, sha="sha2", entities=[])),
          "--db", str(db), "--corpus", "corp"])
    capsys.readouterr()
    rc = main(["prune", "--db", str(db), "--corpus", "corp", "--delete"])
    assert rc == 0
    assert "deleted 2" in capsys.readouterr().out
    a = SqliteGraphAdapter(db)
    a.open("corp")
    try:
        assert a.stats()["entities"] == 0
    finally:
        a.close()


def _stale_store_with_a_community(tmp_path):
    db = tmp_path / "g.db"
    main(["build", "--jsonl", str(_jsonl(tmp_path, sha="sha1")), "--db", str(db),
          "--corpus", "corp", *ATTRIB])
    main(["build", "--jsonl", str(_jsonl(tmp_path, sha="sha2", entities=[])),
          "--db", str(db), "--corpus", "corp"])
    # Anchored by hand: these tests are about what prune SAYS about the community, and the
    # partition that would assign it is exercised elsewhere.
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE entity SET community_id = 7")
        con.commit()
    finally:
        con.close()
    return db


def test_prune_dry_run_does_not_claim_anchored_communities_are_dissolving(tmp_path, capsys):
    """gh#697: nothing is deleted on a dry run, so the next `communities` run dissolves nothing,
    and a line saying they are dissolving told the operator a cleanup was under way."""
    db = _stale_store_with_a_community(tmp_path)
    capsys.readouterr()
    assert main(["prune", "--db", str(db), "--corpus", "corp"]) == 0
    out = capsys.readouterr().out
    assert "  community 7" in out
    assert "would dissolve if deleted" in out
    assert "dissolving" not in out


def test_prune_delete_still_reports_anchored_communities_as_dissolving(tmp_path, capsys):
    db = _stale_store_with_a_community(tmp_path)
    capsys.readouterr()
    assert main(["prune", "--db", str(db), "--corpus", "corp", "--delete"]) == 0
    out = capsys.readouterr().out
    assert "dissolving" in out and "  community 7" in out
    assert "would dissolve" not in out


def test_verify_clean_and_corrupted(tmp_path, capsys):
    db = tmp_path / "g.db"
    main(["build", "--jsonl", str(_jsonl(tmp_path)), "--db", str(db),
          "--corpus", "corp", *ATTRIB])
    assert main(["verify", "--db", str(db), "--corpus", "corp"]) == 0
    capsys.readouterr()
    # corrupt: illegal confidence on an ast edge, written behind the adapter's back
    con = sqlite3.connect(db)
    con.execute("UPDATE edge SET confidence = 0.2 WHERE origin = 'ast'")
    con.commit()
    con.close()
    rc = main(["verify", "--db", str(db), "--corpus", "corp"])
    assert rc == 1
    assert "confidence" in capsys.readouterr().err


def test_unknown_subcommand_is_usage_error(capsys):
    rc = main(["frobnicate"])
    assert rc == 2


# ------------------------------------------------ not-a-store refusal (gh#459)
# Only `build` creates a store. Every other subcommand opens an existing one, so a
# wrong --db path must fail loudly instead of reading as an empty corpus -- and must
# never leave a fresh store (or a 0-byte file) behind at the wrong path.
def _extra_args(cmd, tmp_path):
    if cmd == "render":
        return ["--out", str(tmp_path / "rendered")]
    if cmd == "reports-stale":
        art = tmp_path / "artifact.json"
        art.write_text(json.dumps({"units": [], "parents": [], "churn": []}),
                       encoding="utf-8")
        return ["--artifact", str(art)]
    if cmd == "records":
        rec = tmp_path / "records.json"
        rec.write_text(json.dumps({"pages": [], "reports": [], "meta_pages": [],
                                   "diagrams": []}), encoding="utf-8")
        return ["--records", str(rec), "--sha", "s1", "--date", "2026-09-29"]
    return []


_NON_BUILD = ("stats", "prune", "verify", "check", "render", "reports-stale",
              "communities", "records")


@pytest.mark.parametrize("cmd", _NON_BUILD)
def test_non_build_subcommand_refuses_a_missing_db(cmd, tmp_path, capsys):
    db = tmp_path / "nowhere" / "graph.db"
    db.parent.mkdir()
    rc = main([cmd, "--db", str(db), "--corpus", "corp", *_extra_args(cmd, tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"graph {cmd} FAILED" in err and "does not exist" in err
    assert "graph build" in err  # names the one subcommand that creates a store
    assert not db.exists()  # nothing created at the wrong path


@pytest.mark.parametrize("cmd", _NON_BUILD)
def test_non_build_subcommand_refuses_a_zero_byte_db(cmd, tmp_path, capsys):
    db = tmp_path / "graph.db"
    db.write_bytes(b"")
    rc = main([cmd, "--db", str(db), "--corpus", "corp", *_extra_args(cmd, tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"graph {cmd} FAILED" in err and "0 bytes" in err
    assert db.stat().st_size == 0  # left exactly as found


@pytest.mark.parametrize("cmd", _NON_BUILD)
def test_non_build_subcommand_refuses_a_non_sqlite_file(cmd, tmp_path, capsys):
    db = tmp_path / "graph.db"
    db.write_text("this is not a database\n", encoding="utf-8")
    rc = main([cmd, "--db", str(db), "--corpus", "corp", *_extra_args(cmd, tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"graph {cmd} FAILED" in err and "not a SQLite database" in err
    assert db.read_text(encoding="utf-8") == "this is not a database\n"


def test_stats_refuses_a_sqlite_file_that_is_not_a_graph_store(tmp_path, capsys):
    db = tmp_path / "other.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    con.close()
    rc = main(["stats", "--db", str(db), "--corpus", "corp"])
    assert rc == 1
    assert "not a graph store" in capsys.readouterr().err
    con = sqlite3.connect(db)
    try:  # the graph schema was NOT applied to someone else's database
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert tables == {"unrelated"}


def test_build_still_creates_a_store_at_a_missing_path(tmp_path, capsys):
    db = tmp_path / "fresh" / "graph.db"
    db.parent.mkdir()
    assert main(["build", "--jsonl", str(_jsonl(tmp_path)), "--db", str(db),
                 "--corpus", "corp", *ATTRIB]) == 0
    assert db.stat().st_size > 0
    capsys.readouterr()
    assert main(["stats", "--db", str(db), "--corpus", "corp"]) == 0
    assert json.loads(capsys.readouterr().out)["entities"] == 2
