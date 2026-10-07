"""Epic 705 scope 5 (surface 2 plumbing): the graph-query CLI — the ONE
composition root of the query layer. Skill-surface prefixes (`local:` /
`global:`) act as explicit overrides; output is JSON by default, --pretty
renders cited context for humans."""

from __future__ import annotations

import json
import sqlite3

import pytest

from autodoc_graph.sqlite_backend import SqliteGraphAdapter
from autodoc_query.cli import main
from tests.query_helpers import (
    A,
    FakeEmbedder,
    build_query_graph,
    unit_vec,
)


class StubScorer:
    def score_reports(self, question, reports):
        return [9.0] * len(reports)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "graph.db"
    a = SqliteGraphAdapter(path)
    a.open("corp")
    build_query_graph(a)
    # one live community + report so global-side runs work too
    a.upsert_entities([{
        **a.get_entity(A), "community_id": 1,
    }])
    a.upsert_communities([{
        "level": 0, "label": "ingest", "member_ids": [A],
        "report": json.dumps({
            "summary": "the ingest side",
            "findings": [{"text": "entry point", "entities": [A]}],
        }),
    }])
    a.close()
    return str(path)


def _run(db, question, *flags, embedder=None, scorer=None, capsys=None):
    argv = ["--db", db, "--corpus", "corp", question, *flags]
    code = main(argv, embedder=embedder or FakeEmbedder(unit_vec(0)),
                scorer=scorer or StubScorer())
    out, err = capsys.readouterr()
    return code, out, err


def test_json_output_with_routing_and_citations(db, capsys):
    code, out, err = _run(db, "what does `ingest_records` do?", capsys=capsys)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["routing"]["rule"] == "entity_resolution"
    assert payload["items"]
    assert any("app/ingest.py" in i["citation"] for i in payload["items"])


def test_local_prefix_is_an_override(db, capsys):
    code, out, _ = _run(db, "local: overview of everything", capsys=capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["routing"] == {
        "mode": "local", "rule": "override",
        "evidence": {"via": "cli prefix"}, "reroute_from": None,
    }


def test_global_prefix_is_an_override(db, capsys):
    code, out, _ = _run(db, "global: what does ingest_records do?", capsys=capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["routing"]["mode"] == "global"
    assert payload["routing"]["rule"] == "override"
    assert payload["communities_kept"]


def test_conflicting_prefix_and_mode_flag_is_loud(db, capsys):
    code, _, err = _run(db, "global: q", "--mode", "local", capsys=capsys)
    assert code == 1
    assert "conflict" in err


def test_empty_corpus_fails_loudly(tmp_path, capsys):
    path = tmp_path / "empty.db"
    a = SqliteGraphAdapter(path)
    a.open("corp")
    a.close()
    code, _, err = _run(str(path), "anything", capsys=capsys)
    assert code == 1
    assert "empty" in err


def test_a_wrong_corpus_is_refused_and_writes_nothing(db, capsys):
    """gh#778: a mistyped --corpus inserted a corpus row on a read and then reported
    the corpus empty, sending the reader to rebuild an intact store."""
    argv = ["--db", db, "--corpus", "corpx", "anything"]
    code = main(argv, embedder=FakeEmbedder(unit_vec(0)), scorer=StubScorer())
    err = capsys.readouterr().err
    assert code == 1
    assert "no corpus 'corpx'" in err and "'corp'" in err
    assert "empty" not in err
    con = sqlite3.connect(db)
    try:
        assert [r[0] for r in con.execute("SELECT id FROM corpus")] == ["corp"]
    finally:
        con.close()


def test_nonexistent_db_is_loud_and_creates_nothing(tmp_path, capsys):
    # adversarial finding 3: a --db typo silently CREATED a fresh store and
    # then misdiagnosed as 'empty corpus'. A read-only surface never writes.
    missing = tmp_path / "typo-graph.db"
    code, _, err = _run(str(missing), "anything", capsys=capsys)
    assert code == 1
    assert str(missing) in err and "autodoc-init" in err
    assert not missing.exists()


def test_missing_parent_dir_is_the_same_loud_error(tmp_path, capsys):
    missing = tmp_path / "no" / "such" / "dir" / "graph.db"
    code, _, err = _run(str(missing), "anything", capsys=capsys)
    assert code == 1
    assert "graph-query FAILED" in err and "autodoc-init" in err


def test_seed_floor_flag_is_plumbed(db, capsys):
    # findings 6+7: SEED_FLOOR must be operator-tunable end to end.
    # off-axis query vector -> best seed similarity 0.8, under the raised floor
    vec = [0.8, 0.6] + [0.0] * 766
    code, out, _ = _run(db, "persisting things somewhere",
                        "--seed-floor", "0.99",
                        embedder=FakeEmbedder(vec), capsys=capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["routing"]["rule"] == "seed_floor_reroute"
    assert payload["routing"]["evidence"]["seed_floor"] == 0.99


def test_score_batch_flags_are_plumbed(db, capsys):
    calls: list[list[int]] = []

    class CountingScorer:
        def score_reports(self, question, reports):
            calls.append([cid for cid, _ in reports])
            return [9.0] * len(reports)

    code, _, _ = _run(db, "global: overview please",
                      "--score-batch-size", "1", "--score-parallelism", "1",
                      scorer=CountingScorer(), capsys=capsys)
    assert code == 0
    assert calls and all(len(c) == 1 for c in calls)


def test_pretty_output_renders_citations(db, capsys):
    code, out, _ = _run(db, "what does `ingest_records` do?", "--pretty",
                        capsys=capsys)
    assert code == 0
    assert "app/ingest.py:10-40" in out
    assert "entity_resolution" in out
